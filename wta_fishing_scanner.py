#!/usr/bin/env python3
"""Scan WTA.org trip reports for a hike, looking for fishing info and photos."""

import argparse
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

FISH_TERMS = [
    "fish", "fishing", "fisherman", "fishermen", "anglers?",
    "trout", "salmon", "steelhead", "kokanee", "grayling", "creel",
    "fly fishing", "fly rod", "fishing rod", "fishing pole",
    "fishing line", "fishing hole", "fishing spot", "tackle box",
    "stocked with fish", "pack[\\s-]?rafts?", "pack[\\s-]?rafting",
    "pack[\\s-]?rafters?",
]
FISH_PATTERN = re.compile(r"\b(" + "|".join(FISH_TERMS) + r")\b", re.IGNORECASE)

# Non-fishing terms that contain fish-related words: place names and plants
# (common in Washington trip reports).
PLACE_NAME_PATTERNS = [
    re.compile(r"\bsalmon[\s-]+l[ae][\s-]+sac\b", re.IGNORECASE),
    re.compile(r"\bsalmon\s*berr(?:y|ies)\b", re.IGNORECASE),
    re.compile(r"\bfish\s+hatchery\b", re.IGNORECASE),
]

SNIPPET_RADIUS = 90


def find_matches(text):
    excluded_spans = [m.span() for pat in PLACE_NAME_PATTERNS for m in pat.finditer(text)]

    matches = []
    seen_spans = set()
    for m in FISH_PATTERN.finditer(text):
        if any(m.start() >= s and m.end() <= e for s, e in excluded_spans):
            continue
        span = (max(0, m.start() - SNIPPET_RADIUS), min(len(text), m.end() + SNIPPET_RADIUS))
        if any(abs(span[0] - s[0]) < 20 for s in seen_spans):
            continue
        seen_spans.add(span)
        snippet = text[span[0]:span[1]].strip()
        snippet = re.sub(r"\s+", " ", snippet)
        matches.append({"term": m.group(0), "snippet": snippet})
    return matches


def make_image_filename(report_url, idx, img_src):
    slug = report_url.rstrip("/").split("/")[-1]
    ext = os.path.splitext(urlparse(img_src).path)[1] or ".jpg"
    return f"{slug}_{idx:02d}{ext}"


def normalize_hike_url(url):
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path.endswith("/hike_view"):
        path = path[: -len("/hike_view")]
    return f"{parsed.scheme}://{parsed.netloc}{path}"


class Scanner:
    def __init__(self, delay=0.5, verbose=False):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.delay = delay
        self.verbose = verbose

    def log(self, msg):
        if self.verbose:
            print(msg, file=sys.stderr)

    def get(self, url):
        resp = self.session.get(url, timeout=20)
        resp.raise_for_status()
        time.sleep(self.delay)
        return resp.text

    def get_binary(self, url):
        resp = self.session.get(url, timeout=20)
        resp.raise_for_status()
        time.sleep(self.delay)
        return resp.content

    def get_hike_title(self, hike_url):
        try:
            html = self.get(hike_url)
        except requests.RequestException as e:
            self.log(f"Could not load hike page: {e}")
            return None
        soup = BeautifulSoup(html, "lxml")
        heading = soup.select_one("h1.documentFirstHeading")
        if heading:
            return heading.get_text(strip=True)
        if soup.title:
            return soup.title.get_text(strip=True)
        return None

    def iter_trip_report_urls(self, hike_url, max_reports=None):
        listing_url = hike_url + "/@@related_tripreport_listing"
        seen = set()
        count = 0
        while listing_url:
            self.log(f"Fetching listing page: {listing_url}")
            try:
                html = self.get(listing_url)
            except requests.RequestException as e:
                self.log(f"Failed to fetch listing page: {e}")
                break
            soup = BeautifulSoup(html, "lxml")

            for item in soup.select("div.item"):
                link = item.select_one("h3.listitem-title a")
                if not link or not link.get("href"):
                    continue
                url = link["href"]
                if url in seen:
                    continue
                seen.add(url)
                yield url
                count += 1
                if max_reports is not None and count >= max_reports:
                    return

            next_link = soup.select_one("nav.pagination li.next a")
            if next_link and next_link.get("href"):
                listing_url = urljoin(listing_url, next_link["href"])
            else:
                listing_url = None

    def scan_trip_report(self, url, text_only=False, save_images_dir=None, manifest=None,
                          photos_mode="all"):
        result = {"url": url, "date": None, "text_matches": [], "image_matches": []}
        try:
            html = self.get(url)
        except requests.RequestException as e:
            self.log(f"Failed to fetch trip report {url}: {e}")
            result["error"] = str(e)
            return result

        soup = BeautifulSoup(html, "lxml")

        heading = soup.select_one("h1.documentFirstHeading")
        if heading:
            heading_text = heading.get_text(" ", strip=True)
            parts = heading_text.split(" — ")
            if len(parts) > 1:
                result["date"] = parts[-1].strip()

        body = soup.select_one("#tripreport-body-text")
        if body:
            result["text_matches"] = find_matches(body.get_text(" "))

        if not text_only:
            photos = []
            for idx, fig in enumerate(soup.select("div.captioned-image")):
                caption_el = fig.select_one(".photo-caption-wrapper span")
                img_el = fig.select_one("img")
                if not img_el or not img_el.get("src"):
                    continue
                img_src = img_el["src"]
                caption = caption_el.get_text(strip=True) if caption_el else ""
                photos.append((idx, img_src, caption))
                if caption and FISH_PATTERN.search(caption):
                    result["image_matches"].append({
                        "image_url": img_src,
                        "caption": caption,
                    })

            # Decide once, using the complete match info for this report, so a
            # caption match found late in the photo list still qualifies photos
            # earlier in the list ("matched" mode downloads all-or-nothing).
            if photos_mode == "all":
                save_all = True
            elif photos_mode == "matched":
                save_all = bool(result["text_matches"]) or bool(result["image_matches"])
            else:
                save_all = False

            if save_images_dir and save_all:
                for idx, img_src, caption in photos:
                    filename = make_image_filename(url, idx, img_src)
                    try:
                        data = self.get_binary(img_src)
                    except requests.RequestException as e:
                        self.log(f"Failed to download image {img_src}: {e}")
                        continue
                    with open(os.path.join(save_images_dir, filename), "wb") as f:
                        f.write(data)
                    if manifest is not None:
                        manifest.append({
                            "file": filename,
                            "report_url": url,
                            "date": result["date"],
                            "caption": caption,
                        })

        return result

    def scan_hike(self, hike_url, max_reports=None, text_only=False, save_images_dir=None,
                  photos_mode="all"):
        hike_url = normalize_hike_url(hike_url)
        title = self.get_hike_title(hike_url)
        manifest = [] if save_images_dir else None
        if save_images_dir:
            os.makedirs(save_images_dir, exist_ok=True)
        reports = []
        for report_url in self.iter_trip_report_urls(hike_url, max_reports=max_reports):
            self.log(f"Scanning trip report: {report_url}")
            report = self.scan_trip_report(
                report_url, text_only=text_only,
                save_images_dir=save_images_dir, manifest=manifest,
                photos_mode=photos_mode,
            )
            if report["text_matches"] or report["image_matches"]:
                reports.append(report)

        if save_images_dir:
            with open(os.path.join(save_images_dir, "manifest.json"), "w") as f:
                json.dump(manifest, f, indent=2)

        return {
            "hike_url": hike_url,
            "hike_title": title,
            "matching_reports": reports,
            "images_downloaded": len(manifest) if manifest is not None else None,
        }


def print_results(results):
    title = results["hike_title"] or results["hike_url"]
    print(f"\nFishing scan for: {title}")
    print(f"({results['hike_url']})\n")

    matches = results["matching_reports"]
    if not matches:
        print("No fishing-related trip reports found.")
        return

    print(f"Found fishing mentions in {len(matches)} trip report(s):\n")
    for report in matches:
        date = report["date"] or "unknown date"
        print(f"- {date}: {report['url']}")
        for tm in report["text_matches"][:3]:
            print(f"    text: \"...{tm['snippet']}...\"  [{tm['term']}]")
        for im in report["image_matches"]:
            print(f"    photo: {im['image_url']}")
            print(f"           caption: \"{im['caption']}\"")
        print()


def print_vision_findings(findings):
    if not findings:
        print("\nVision review found no additional fishing photos.")
        return
    print(f"\nVision review found {len(findings)} additional fishing photo(s) "
          "(missed by text/caption matching):\n")
    for f in findings:
        print(f"- {f['date']}: {f['report_url']}")
        print(f"    {f['path']}")
        print(f"    \"{f['vision_description']}\"")


def main():
    parser = argparse.ArgumentParser(
        description="Scan WTA.org trip reports for a hike for fishing info/photos."
    )
    parser.add_argument("hike_url", help="URL of the hike, e.g. "
                         "https://www.wta.org/go-hiking/hikes/kendall-peak-lakes")
    parser.add_argument("--max-reports", type=int, default=None,
                         help="Limit the number of trip reports scanned")
    parser.add_argument("--delay", type=float, default=0.5,
                         help="Delay in seconds between HTTP requests (default: 0.5)")
    parser.add_argument("--text-only", action="store_true",
                         help="Skip checking photo captions (faster, text-only scan)")
    parser.add_argument("--save-images", metavar="DIR",
                         help="Download trip report photos to DIR, with a "
                              "manifest.json mapping each file back to its report/caption "
                              "(for manual or vision-based review)")
    parser.add_argument("--photos-mode", choices=["all", "matched"], default="all",
                         help="With --save-images: 'all' downloads every photo from every "
                              "report (default); 'matched' only downloads photos from reports "
                              "that already have a text or caption fishing match")
    parser.add_argument("--vision-review", action="store_true",
                         help="Automatically classify downloaded photos for fishing content "
                              "(requires --save-images); builds contact sheets and reviews "
                              "them, no manual step needed")
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
    parser.add_argument("-o", "--output", help="Write full JSON results to this file")
    parser.add_argument("-v", "--verbose", action="store_true",
                         help="Print progress to stderr")
    args = parser.parse_args()

    if args.vision_review and not args.save_images:
        parser.error("--vision-review requires --save-images")

    scanner = Scanner(delay=args.delay, verbose=args.verbose)
    results = scanner.scan_hike(
        args.hike_url, max_reports=args.max_reports, text_only=args.text_only,
        save_images_dir=args.save_images, photos_mode=args.photos_mode,
    )
    print_results(results)

    if args.save_images:
        print(f"\nDownloaded {results['images_downloaded']} photo(s) to {args.save_images}")
        print(f"(see {os.path.join(args.save_images, 'manifest.json')} for report/caption mapping)")

    if args.vision_review:
        from make_contact_sheets import build_contact_sheets
        from vision_review import review_images_dir, DEFAULT_BACKEND

        sheets_dir = os.path.join(args.save_images, "contact_sheets")
        print(f"\nBuilding contact sheets in {sheets_dir} ...")
        build_contact_sheets(args.save_images, sheets_dir)

        backend = args.vision_backend or DEFAULT_BACKEND
        print(f"Running vision review (backend: {backend})...")
        vision_findings = review_images_dir(
            args.save_images, backend=backend, model=args.vision_model,
            base_url=args.vision_base_url, verbose=args.verbose,
        )
        print_vision_findings(vision_findings)
        results["vision_findings"] = vision_findings

    if args.output:
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nFull results written to {args.output}")


if __name__ == "__main__":
    main()
