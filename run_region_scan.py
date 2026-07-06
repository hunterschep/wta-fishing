#!/usr/bin/env python3
"""Run wta_fishing_scanner.py across a list of hikes and build one combined markdown index.

By default, photos are only downloaded from trip reports that already have a
text or caption fishing match ("matched" mode) -- caption text alone misses
photos that show fishing but aren't captioned as such, so those reports' other
photos are pulled too, for a follow-up visual review. Pass --photos all to
download every photo from every report region-wide instead (much larger).

Hikes are scanned in parallel (one worker thread per in-flight hike, each with
its own HTTP session), biggest hikes first, so a couple of unusually popular
hikes don't serialize behind the rest of the region.
"""

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from make_contact_sheets import build_contact_sheets
from vision_review import (
    review_images_dir, _setup_backend,
    DEFAULT_BACKEND as DEFAULT_VISION_BACKEND, DEFAULT_MODELS as DEFAULT_VISION_MODELS,
)
from wta_fishing_scanner import (
    Scanner, USER_AGENT, normalize_hike_url, parse_trip_report_count,
)


def load_hikes(path):
    with open(path) as f:
        return json.load(f)


def slugify(name):
    return "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")


def get_report_count(session, url):
    try:
        r = session.get(url.rstrip("/") + "/@@related_tripreport_listing", timeout=15)
        r.raise_for_status()
        count = parse_trip_report_count(BeautifulSoup(r.text, "lxml"))
        return count or 0
    except requests.RequestException:
        return 0


def scan_one_hike(hike, delay, max_reports, out_dir, images_dir, photos_mode,
                   vision_review=False, vision_backend=None, vision_model=None,
                   vision_base_url=None, hike_workers=1):
    name, url = hike["name"], hike["url"]
    scanner = Scanner(delay=delay, verbose=True)
    slug = slugify(name)
    hike_images_dir = os.path.join(images_dir, slug) if images_dir else None

    start = time.time()
    try:
        result = scanner.scan_hike(
            url, max_reports=max_reports,
            save_images_dir=hike_images_dir, photos_mode=photos_mode,
            workers=hike_workers,
        )
    except Exception as e:
        result = {
            "hike_url": normalize_hike_url(url), "hike_title": name,
            "matching_reports": [], "images_downloaded": None, "error": str(e),
        }
    result["hike_name"] = name
    result["vision_findings"] = []

    if hike_images_dir and result.get("images_downloaded"):
        try:
            sheets = build_contact_sheets(
                hike_images_dir, os.path.join(hike_images_dir, "contact_sheets"),
            )
            result["contact_sheets"] = sheets
        except Exception as e:
            result["contact_sheets_error"] = str(e)
            sheets = None

        if vision_review and sheets:
            try:
                kwargs = {"verbose": True}
                if vision_backend:
                    kwargs["backend"] = vision_backend
                if vision_model:
                    kwargs["model"] = vision_model
                if vision_base_url:
                    kwargs["base_url"] = vision_base_url
                result["vision_findings"] = review_images_dir(hike_images_dir, **kwargs)
            except Exception as e:
                result["vision_review_error"] = str(e)

    result["elapsed_seconds"] = round(time.time() - start, 1)

    with open(os.path.join(out_dir, f"{slug}.json"), "w") as f:
        json.dump(result, f, indent=2)

    return result


def scan_region(hikes, out_dir, delay=0.5, max_reports=None, workers=6,
                 images_dir=None, photos_mode="matched",
                 vision_review=False, vision_backend=None, vision_model=None,
                 vision_base_url=None, hike_workers=1):
    os.makedirs(out_dir, exist_ok=True)

    probe_session = requests.Session()
    probe_session.headers.update({"User-Agent": USER_AGENT})
    print(f"Checking trip report counts for {len(hikes)} hikes...", file=sys.stderr, flush=True)
    for h in hikes:
        h["_count"] = get_report_count(probe_session, h["url"])
        time.sleep(0.1)
    hikes = sorted(hikes, key=lambda h: h["_count"], reverse=True)
    for h in hikes:
        print(f"  {h['_count']:>6}  {h['name']}", file=sys.stderr, flush=True)

    results = []
    results_lock = threading.Lock()
    total = len(hikes)
    done_count = 0

    def write_combined():
        with open(os.path.join(out_dir, "_combined.json"), "w") as f:
            json.dump(results, f, indent=2)

    print(f"Scanning {total} hikes with {workers} parallel workers "
          f"(hike_workers={hike_workers}, photos_mode={photos_mode}, "
          f"vision_review={vision_review})...",
          file=sys.stderr, flush=True)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_hike = {
            executor.submit(scan_one_hike, h, delay, max_reports, out_dir,
                             images_dir, photos_mode, vision_review, vision_backend,
                             vision_model, vision_base_url, hike_workers): h
            for h in hikes
        }
        for future in as_completed(future_to_hike):
            hike = future_to_hike[future]
            try:
                result = future.result()
            except Exception as e:
                result = {
                    "hike_url": normalize_hike_url(hike["url"]), "hike_title": hike["name"],
                    "hike_name": hike["name"], "matching_reports": [],
                    "images_downloaded": None, "error": str(e),
                }
            with results_lock:
                results.append(result)
                done_count += 1
                write_combined()
            n = len(result["matching_reports"])
            print(f"[{done_count}/{total}] done: {hike['name']} -- {n} matching report(s) "
                  f"in {result.get('elapsed_seconds', '?')}s", file=sys.stderr, flush=True)

    return results


def relpath_for_markdown(path, out_path):
    return os.path.relpath(path, os.path.dirname(out_path) or ".")


def build_markdown(results, out_path, region_name):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    total_matching_reports = sum(len(r["matching_reports"]) for r in results)
    total_photo_matches = sum(
        sum(len(rep["image_matches"]) for rep in r["matching_reports"]) for r in results
    )
    total_downloaded = sum(r.get("images_downloaded") or 0 for r in results)
    total_vision_findings = sum(len(r.get("vision_findings") or []) for r in results)

    lines = [
        f"# Fishing Info Index -- {region_name}",
        "",
        f"Generated {now}. Scanned {len(results)} hikes' WTA trip reports for "
        "fishing-related text and photo captions.",
        "",
        f"**{total_matching_reports} trip report(s)** across the region mention fishing; "
        f"**{total_photo_matches} photo(s)** have fishing-related captions; "
        f"**{total_downloaded} photo(s)** downloaded for visual review; "
        f"**{total_vision_findings} additional fishing photo(s)** found by automated "
        "vision review (missed by text/caption matching).",
        "",
        "Every fishing photo found (caption-matched or vision-confirmed) is embedded "
        "inline under its hike in the Details section below.",
        "",
    ]

    ranked = sorted(results, key=lambda r: len(r["matching_reports"]), reverse=True)

    lines += ["## Summary", "",
               "| Hike | Matching Reports | Photo Matches | Photos Downloaded | "
               "Contact Sheets | Vision Findings |",
               "|---|---|---|---|---|---|"]
    for r in ranked:
        name = r.get("hike_name") or r.get("hike_title") or r["hike_url"]
        n = len(r["matching_reports"])
        photos = sum(len(rep["image_matches"]) for rep in r["matching_reports"])
        downloaded = r.get("images_downloaded") or 0
        n_sheets = len(r.get("contact_sheets") or [])
        n_vision = len(r.get("vision_findings") or [])
        anchor = slugify(name)
        lines.append(f"| [{name}](#{anchor}) | {n} | {photos} | {downloaded} | {n_sheets} | {n_vision} |")
    lines.append("")

    lines += ["## Details", ""]
    for r in ranked:
        name = r.get("hike_name") or r.get("hike_title") or r["hike_url"]
        lines.append(f"### {name}")
        lines.append("")
        lines.append(f"{r['hike_url']}")
        lines.append("")
        if r.get("error"):
            lines.append(f"_Error scanning this hike: {r['error']}_")
            lines.append("")
            continue
        if r.get("images_downloaded"):
            n_sheets = len(r.get("contact_sheets") or [])
            lines.append(
                f"_{r['images_downloaded']} photo(s) downloaded for visual review, "
                f"{n_sheets} contact sheet(s) generated._"
            )
            lines.append("")

        # Gather every known-relevant photo for this hike: caption matches (remote
        # URLs) plus vision-review findings (local files), and embed them inline so
        # the report is a self-contained photo gallery per hike, not just links.
        photo_entries = []
        for rep in r["matching_reports"]:
            for im in rep["image_matches"]:
                photo_entries.append({
                    "src": im["image_url"], "caption": im["caption"],
                    "date": rep["date"], "report_url": rep["url"], "source": "caption match",
                })
        for f in (r.get("vision_findings") or []):
            photo_entries.append({
                "src": relpath_for_markdown(f["path"], out_path),
                "caption": f["vision_description"], "date": f["date"],
                "report_url": f["report_url"], "source": f.get("source", "vision review"),
            })

        if photo_entries:
            lines.append(f"**Fishing photos ({len(photo_entries)}):**")
            lines.append("")
            for p in photo_entries:
                cap = (p["caption"] or "").replace('"', "'")
                date = p["date"] or "unknown date"
                lines.append(
                    f'<img src="{p["src"]}" width="320" alt="{cap}"><br>'
                    f'<em>{date} -- {cap or "(no caption)"} [{p["source"]}]</em> -- '
                    f'<a href="{p["report_url"]}">{p["report_url"]}</a>'
                )
                lines.append("")

        if not r["matching_reports"]:
            if not r.get("vision_findings"):
                lines.append("No fishing-related trip reports found.")
                lines.append("")
            continue

        lines.append("**Text mentions:**")
        lines.append("")
        for rep in r["matching_reports"]:
            date = rep["date"] or "unknown date"
            lines.append(f"- **{date}** -- {rep['url']}")
            for tm in rep["text_matches"][:3]:
                snippet = tm["snippet"].replace("|", "\\|")
                lines.append(f"    - text (\"{tm['term']}\"): \"...{snippet}...\"")
        lines.append("")

    with open(out_path, "w") as f:
        f.write("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(
        description="Scan multiple WTA hikes in parallel for fishing info and build a "
                    "combined markdown index."
    )
    parser.add_argument("--hikes-file", default="snoqualmie_region_hikes.json")
    parser.add_argument("--region-name", default="Snoqualmie Region")
    parser.add_argument("--out-dir", default="output/region")
    parser.add_argument("--markdown", default="output/snoqualmie-region-fishing-report.md")
    parser.add_argument("--delay", type=float, default=0.5,
                         help="Delay in seconds between requests, per worker (default: 0.5)")
    parser.add_argument("--workers", type=int, default=6,
                         help="Number of hikes to scan concurrently (default: 6)")
    parser.add_argument("--hike-workers", type=int, default=1,
                         help="Number of trip reports/listing pages to fetch concurrently "
                              "inside each hike (default: 1; increase carefully with "
                              "--workers because total concurrency multiplies)")
    parser.add_argument("--max-reports", type=int, default=None,
                         help="Cap trip reports scanned per hike (default: no cap, full history)")
    parser.add_argument("--images-dir", default="output/region_images",
                         help="Where to save downloaded photos (default: output/region_images)")
    parser.add_argument("--photos", choices=["matched", "all"], default="matched",
                         help="'matched' (default) downloads photos only from reports that "
                              "already have a text/caption fishing match; 'all' downloads "
                              "every photo from every report region-wide (much larger)")
    parser.add_argument("--no-photos", action="store_true",
                         help="Skip photo downloads entirely (fastest, caption-text matching only)")
    parser.add_argument("--vision-review", action="store_true",
                         help="Automatically classify downloaded photos for fishing content, "
                              "per hike, right after its contact sheets are built. No manual "
                              "review needed.")
    parser.add_argument("--vision-backend", choices=["ollama", "anthropic"], default=None,
                         help="'ollama' (default): local model, no API key, no per-call cost. "
                              "'anthropic': Claude API, requires ANTHROPIC_API_KEY, generally "
                              "more accurate on subtle cases")
    parser.add_argument("--vision-model", default=None,
                         help="Model to use for --vision-review "
                              "(default: qwen2.5vl:7b for ollama, claude-haiku-4-5-20251001 "
                              "for anthropic)")
    parser.add_argument("--vision-base-url", default=None,
                         help="Ollama server URL (ollama backend only, default: "
                              "http://localhost:11434)")
    parser.add_argument("--rebuild-markdown-from", metavar="COMBINED_JSON",
                         help="Skip scanning; just rebuild the markdown report from an "
                              "existing _combined.json (e.g. after editing build_markdown)")
    args = parser.parse_args()

    if args.rebuild_markdown_from:
        with open(args.rebuild_markdown_from) as f:
            results = json.load(f)
        build_markdown(results, args.markdown, args.region_name)
        print(f"Wrote {args.markdown}", file=sys.stderr)
        return

    if args.vision_review:
        if args.no_photos:
            parser.error("--vision-review requires photo downloads (remove --no-photos)")
        backend = args.vision_backend or DEFAULT_VISION_BACKEND
        model = args.vision_model or DEFAULT_VISION_MODELS[backend]
        try:
            _setup_backend(backend, model, args.vision_base_url)
        except Exception as e:
            parser.error(str(e))

    hikes = load_hikes(args.hikes_file)
    results = scan_region(
        hikes, args.out_dir, delay=args.delay, max_reports=args.max_reports,
        workers=args.workers,
        images_dir=None if args.no_photos else args.images_dir,
        photos_mode=args.photos,
        vision_review=args.vision_review,
        vision_backend=args.vision_backend,
        vision_model=args.vision_model,
        vision_base_url=args.vision_base_url,
        hike_workers=args.hike_workers,
    )
    build_markdown(results, args.markdown, args.region_name)
    print(f"Wrote {args.markdown}", file=sys.stderr)


if __name__ == "__main__":
    main()
