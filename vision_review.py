#!/usr/bin/env python3
"""Automated vision-based review of downloaded photos for fishing content.

Two backends, two different strategies (chosen per backend's real-world
accuracy, not just for symmetry):

  - "anthropic" (Claude API): batches ~20 photos per contact sheet (see
    make_contact_sheets.py) into one numbered grid and asks about the whole
    sheet in a single call -- cheap, and accurate enough at that resolution
    for a frontier model.

  - "ollama" (local model, default): reviews each photo individually at full
    resolution. Testing showed a 7B local vision model reliably identifies a
    fish or fishing gear in a full photo, but misses the same content once
    shrunk into a small grid cell alongside 19 others -- so batching would
    silently undercount. Individual review is slower (one call per photo
    instead of per ~20) but it's free and local, so that trade is worth it.
    No API key, no per-call cost, runs fully offline. Requires `ollama serve`
    running and a vision-capable model pulled (e.g. `ollama pull qwen2.5vl:7b`).
"""

import argparse
import base64
import io
import json
import os
import re
import sys
import time

import requests
from PIL import Image

DEFAULT_BACKEND = os.environ.get("WTA_VISION_BACKEND", "ollama")
DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5-20251001",
    "ollama": "qwen2.5vl:7b",
}
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

SHEET_PROMPT = """This image is a contact sheet: a grid of numbered photo thumbnails \
(yellow-on-black index labels in the bottom-left corner of each photo) from \
hiking trip reports in the Washington Cascades.

Look at every thumbnail and identify any that show:
- A fish (held in hand, on a line/hook, in a net, cooking, or visible in the water)
- A person actively fishing (holding a rod, fly fishing, casting)
- Fishing gear (rod, reel, tackle box, net, stringer, lure) as the clear subject

Respond with ONLY a JSON array of objects, one per matching thumbnail, in this \
exact form:
[{"index": <int>, "description": "<one short sentence describing what's shown>"}]

If nothing on this sheet shows fishing, respond with an empty JSON array: []
Do not include any other text before or after the JSON.
"""

PHOTO_PROMPT = """Look at this photo from a hiking trip report.

First, describe in one sentence exactly what is shown in the photo.
Then decide whether it shows any of: a fish (held in hand, on a line/hook, in \
a net, cooking, or visible in the water), a person actively fishing (holding \
a rod, fly fishing, casting), or fishing gear (rod, reel, tackle box, net, \
stringer, lure) as the clear subject.

Respond with ONLY a JSON object in this exact form, with the description \
field first so you reason about the image before deciding:
{"description": "<one sentence describing the photo>", "is_fishing": true or false}
Do not include any other text before or after the JSON.
"""


def _extract_json(text, opener="[", closer="]"):
    match = re.search(re.escape(opener) + r".*" + re.escape(closer), text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _encode_image(path, max_dim=None):
    if max_dim is None:
        with open(path, "rb") as f:
            return base64.standard_b64encode(f.read()).decode("utf-8")
    img = Image.open(path).convert("RGB")
    img.thumbnail((max_dim, max_dim))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.standard_b64encode(buf.getvalue()).decode("utf-8")


def classify_sheet_anthropic(client, sheet_path, model, retries=1):
    data = _encode_image(sheet_path)
    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=1024,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {
                            "type": "base64", "media_type": "image/jpeg", "data": data,
                        }},
                        {"type": "text", "text": SHEET_PROMPT},
                    ],
                }],
            )
            text = "".join(
                block.text for block in resp.content if getattr(block, "type", None) == "text"
            ).strip()
            return _extract_json(text, "[", "]") or []
        except Exception as e:
            last_err = e
            time.sleep(1.0)
    raise last_err


def _ollama_chat(prompt, image_b64, model, base_url, num_ctx=4096, timeout=180):
    resp = requests.post(
        f"{base_url}/api/chat",
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt, "images": [image_b64]}],
            "stream": False,
            # Default context (128k for qwen2.5vl) blows the compute-graph
            # memory budget for a single-turn image prompt and crashes the
            # runner; a few thousand tokens is plenty here.
            "options": {"temperature": 0, "num_ctx": num_ctx},
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"]


def classify_photo_ollama(photo_path, model, base_url=OLLAMA_BASE_URL, retries=1):
    data = _encode_image(photo_path, max_dim=896)
    last_err = None
    for attempt in range(retries + 1):
        try:
            text = _ollama_chat(PHOTO_PROMPT, data, model, base_url)
            result = _extract_json(text, "{", "}")
            if not result:
                return False, ""
            return bool(result.get("is_fishing")), result.get("description", "")
        except Exception as e:
            last_err = e
            time.sleep(1.0)
    raise last_err


def _setup_backend(backend, model, base_url):
    if backend == "anthropic":
        import anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Vision review with backend='anthropic' "
                "requires an Anthropic API key: https://console.anthropic.com/settings/keys"
            )
        return anthropic.Anthropic(api_key=api_key)

    if backend == "ollama":
        base_url = base_url or OLLAMA_BASE_URL
        try:
            r = requests.get(f"{base_url}/api/tags", timeout=5)
            r.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError(
                f"Could not reach Ollama at {base_url} ({e}). Is `ollama serve` running?"
            )
        available = [m["name"] for m in r.json().get("models", [])]
        if model not in available and not any(a.startswith(model + ":") for a in available):
            raise RuntimeError(
                f"Model '{model}' not found in Ollama (have: {available}). "
                f"Run `ollama pull {model}` first."
            )
        return None

    raise ValueError(f"Unknown vision backend: {backend!r} (expected 'anthropic' or 'ollama')")


def _review_via_sheets(images_dir, manifest, client, model, verbose, delay):
    sheets_dir = os.path.join(images_dir, "contact_sheets")
    if not os.path.isdir(sheets_dir):
        return []
    sheet_files = sorted(
        f for f in os.listdir(sheets_dir) if f.startswith("sheet_") and f.endswith(".jpg")
    )

    findings = []
    for sheet_file in sheet_files:
        sheet_path = os.path.join(sheets_dir, sheet_file)
        if verbose:
            print(f"Vision review [anthropic/{model}]: {sheet_path}", file=sys.stderr)
        try:
            hits = classify_sheet_anthropic(client, sheet_path, model=model)
        except Exception as e:
            if verbose:
                print(f"  vision error on {sheet_file}: {e}", file=sys.stderr)
            hits = []
        time.sleep(delay)

        for hit in hits:
            try:
                idx = int(hit["index"])
            except (KeyError, ValueError, TypeError):
                continue
            if not (0 <= idx < len(manifest)):
                continue
            entry = manifest[idx]
            findings.append({
                "file": entry["file"],
                "path": os.path.join(images_dir, entry["file"]),
                "report_url": entry["report_url"],
                "date": entry["date"],
                "caption": entry["caption"],
                "vision_description": hit.get("description", ""),
                "source": f"vision review (anthropic/{model})",
            })
    return findings


def _review_per_photo(images_dir, manifest, model, base_url, verbose, delay):
    findings = []
    for i, entry in enumerate(manifest):
        photo_path = os.path.join(images_dir, entry["file"])
        if not os.path.exists(photo_path):
            continue
        if verbose:
            print(f"Vision review [ollama/{model}] ({i + 1}/{len(manifest)}): {photo_path}",
                  file=sys.stderr)
        try:
            is_fishing, desc = classify_photo_ollama(photo_path, model=model, base_url=base_url)
        except Exception as e:
            if verbose:
                print(f"  vision error on {entry['file']}: {e}", file=sys.stderr)
            is_fishing, desc = False, ""
        time.sleep(delay)

        if is_fishing:
            findings.append({
                "file": entry["file"],
                "path": photo_path,
                "report_url": entry["report_url"],
                "date": entry["date"],
                "caption": entry["caption"],
                "vision_description": desc,
                "source": f"vision review (ollama/{model})",
            })
    return findings


def review_images_dir(images_dir, backend=DEFAULT_BACKEND, model=None, base_url=None,
                       verbose=False, delay=0.2):
    manifest_path = os.path.join(images_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        return []

    with open(manifest_path) as f:
        manifest = json.load(f)
    if not manifest:
        return []

    model = model or DEFAULT_MODELS[backend]
    base_url = base_url or OLLAMA_BASE_URL
    client = _setup_backend(backend, model, base_url)

    if backend == "anthropic":
        return _review_via_sheets(images_dir, manifest, client, model, verbose, delay)
    return _review_per_photo(images_dir, manifest, model, base_url, verbose, delay)


def main():
    parser = argparse.ArgumentParser(
        description="Vision-review a --save-images directory's photos for fishing "
                    "content, via a local Ollama model (per-photo) or the Claude API "
                    "(batched contact sheets)."
    )
    parser.add_argument("images_dir", help="Directory produced by --save-images "
                         "(must contain manifest.json)")
    parser.add_argument("--backend", choices=["ollama", "anthropic"], default=DEFAULT_BACKEND)
    parser.add_argument("--model", default=None,
                         help="Default: qwen2.5vl:7b (ollama) or claude-haiku-4-5-20251001 (anthropic)")
    parser.add_argument("--base-url", default=None, help="Ollama server URL (ollama backend only)")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-o", "--output", help="Write findings as JSON to this file")
    args = parser.parse_args()

    findings = review_images_dir(
        args.images_dir, backend=args.backend, model=args.model,
        base_url=args.base_url, verbose=args.verbose,
    )

    print(f"\nFound {len(findings)} fishing photo(s) via vision review:\n")
    for f in findings:
        print(f"- {f['date']}: {f['report_url']}")
        print(f"    {f['path']}")
        print(f"    \"{f['vision_description']}\"")

    if args.output:
        with open(args.output, "w") as fh:
            json.dump(findings, fh, indent=2)
        print(f"\nFindings written to {args.output}")


if __name__ == "__main__":
    main()
