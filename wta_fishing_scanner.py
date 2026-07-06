#!/usr/bin/env python3
"""Scan WTA.org trip reports for a hike, looking for fishing info and photos."""

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def parse_trip_report_count(soup):
    count_el = soup.select_one("#count-data, .count-data")
    if not count_el:
        return None
    match = re.search(r"[\d,]+", count_el.get_text(" ", strip=True))
    if not match:
        return None
    return int(match.group(0).replace(",", ""))


def parse_trip_report_urls(soup):
    urls = []
    for item in soup.select("div.item"):
        link = item.select_one("h3.listitem-title a")
        if link and link.get("href"):
            urls.append(link["href"])
    return urls


def parse_next_listing_url(soup, current_url):
    next_link = soup.select_one("nav.pagination li.next a")
    if next_link and next_link.get("href"):
        return urljoin(current_url, next_link["href"])
    return None


def make_listing_url(hike_url, offset=0):
    base_url = hike_url.rstrip("/") + "/@@related_tripreport_listing"
    if offset <= 0:
        return base_url
    return f"{base_url}?b_start:int={offset}"


def dedupe_in_order(urls, limit=None):
    if limit is not None and limit <= 0:
        return []
    seen = set()
    unique_urls = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        unique_urls.append(url)
        if limit is not None and len(unique_urls) >= limit:
            break
    return unique_urls


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


class ProgressBar:
    def __init__(self, enabled=True, stream=None):
        self.enabled = enabled
        self.stream = stream or sys.stderr
        self.last_len = 0

    def __call__(self, event):
        if not self.enabled:
            return
        name = event["event"]
        if name == "start":
            title = event.get("title") or event.get("hike_url")
            self._write(f"Preparing scan: {title}")
        elif name == "total":
            total = event.get("total")
            if total is not None:
                self._write(f"Found {total} trip report(s); collecting report list...")
            else:
                self._write("Collecting report list...")
        elif name == "listing":
            self._draw_listing(
                event.get("pages_done", 0),
                event.get("pages_total"),
                event.get("urls", 0),
            )
        elif name == "report":
            self._draw(
                event.get("scanned", 0),
                event.get("total"),
                event.get("matches", 0),
                event.get("images", 0),
            )
        elif name == "finish":
            self._draw(
                event.get("scanned", 0),
                event.get("total"),
                event.get("matches", 0),
                event.get("images", 0),
                done=True,
            )

    def _write(self, message):
        padding = " " * max(0, self.last_len - len(message))
        print(f"\r{message}{padding}", end="", file=self.stream, flush=True)
        self.last_len = len(message)

    def _draw_listing(self, pages_done, pages_total, urls):
        width = 28
        if pages_total:
            filled = min(width, int(width * pages_done / pages_total))
            bar = "#" * filled + "-" * (width - filled)
            message = f"Listing  [{bar}] {pages_done}/{pages_total} pages | urls: {urls}"
        else:
            message = f"Listing pages: {pages_done} | urls: {urls}"
        self._write(message)

    def _draw(self, scanned, total, matches, images, done=False):
        width = 28
        if total:
            filled = min(width, int(width * scanned / total))
            bar = "#" * filled + "-" * (width - filled)
            message = f"Scanning [{bar}] {scanned}/{total} reports"
        else:
            message = f"Scanning {scanned} report(s)"
        message += f" | matches: {matches}"
        if images:
            message += f" | images: {images}"
        if done:
            message += " | done"
        self._write(message)
        if done:
            print(file=self.stream, flush=True)
            self.last_len = 0


class Scanner:
    def __init__(self, delay=0.5, verbose=False, retries=1):
        self.delay = delay
        self.verbose = verbose
        self.retries = retries
        self._thread_local = threading.local()

    def log(self, msg):
        if self.verbose:
            print(msg, file=sys.stderr)

    def get_session(self):
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": USER_AGENT})
            self._thread_local.session = session
        return session

    def request(self, url, binary=False):
        last_error = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.get_session().get(url, timeout=20)
                resp.raise_for_status()
                if self.delay:
                    time.sleep(self.delay)
                return resp.content if binary else resp.text
            except requests.RequestException as e:
                last_error = e
                if attempt >= self.retries:
                    raise
                self.log(f"Retrying after request failure ({attempt + 1}/{self.retries}): {url}")
                time.sleep(min(2.0, 0.5 * (attempt + 1)))
        raise last_error

    def get(self, url):
        return self.request(url)

    def get_binary(self, url):
        return self.request(url, binary=True)

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

    def fetch_listing_page(self, listing_url):
        self.log(f"Fetching listing page: {listing_url}")
        html = self.get(listing_url)
        soup = BeautifulSoup(html, "lxml")
        return {
            "url": listing_url,
            "total": parse_trip_report_count(soup),
            "report_urls": parse_trip_report_urls(soup),
            "next_url": parse_next_listing_url(soup, listing_url),
        }

    def collect_trip_report_urls(self, hike_url, max_reports=None, workers=1,
                                 progress_callback=None):
        first_url = make_listing_url(hike_url)
        try:
            first_page = self.fetch_listing_page(first_url)
        except requests.RequestException as e:
            self.log(f"Failed to fetch listing page: {e}")
            if progress_callback:
                progress_callback({"event": "total", "total": None})
            return [], None

        raw_total = first_page["total"]
        total = raw_total
        if total is not None and max_reports is not None:
            total = min(total, max_reports)
        if progress_callback:
            progress_callback({"event": "total", "total": total})

        first_urls = first_page["report_urls"]
        page_size = len(first_urls)
        if max_reports is not None and page_size >= max_reports:
            urls = dedupe_in_order(first_urls, limit=max_reports)
            if progress_callback:
                progress_callback({
                    "event": "listing",
                    "pages_done": 1,
                    "pages_total": 1,
                    "urls": len(urls),
                })
            return urls, total

        if not raw_total or not page_size or workers <= 1:
            return self.collect_trip_report_urls_serial(
                first_page, max_reports=max_reports, progress_callback=progress_callback,
            )

        offsets = list(range(page_size, total or raw_total, page_size))
        pages_total = 1 + len(offsets)
        pages_done = 1
        urls_by_offset = {0: first_urls}
        if progress_callback:
            progress_callback({
                "event": "listing",
                "pages_done": pages_done,
                "pages_total": pages_total,
                "urls": len(dedupe_in_order(first_urls, limit=max_reports)),
            })

        if offsets:
            max_workers = min(max(1, workers), len(offsets))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_offset = {
                    executor.submit(self.fetch_listing_page, make_listing_url(hike_url, offset)): offset
                    for offset in offsets
                }
                for future in as_completed(future_to_offset):
                    offset = future_to_offset[future]
                    try:
                        page = future.result()
                        urls_by_offset[offset] = page["report_urls"]
                    except requests.RequestException as e:
                        self.log(f"Failed to fetch listing page at offset {offset}: {e}")
                        urls_by_offset[offset] = []
                    pages_done += 1
                    if progress_callback:
                        urls_so_far = []
                        for done_offset in sorted(urls_by_offset):
                            urls_so_far.extend(urls_by_offset[done_offset])
                        progress_callback({
                            "event": "listing",
                            "pages_done": pages_done,
                            "pages_total": pages_total,
                            "urls": len(dedupe_in_order(urls_so_far, limit=max_reports)),
                        })

        ordered_urls = []
        for offset in sorted(urls_by_offset):
            ordered_urls.extend(urls_by_offset[offset])
        return dedupe_in_order(ordered_urls, limit=max_reports), total

    def collect_trip_report_urls_serial(self, first_page, max_reports=None,
                                        progress_callback=None):
        pages = [first_page]
        urls = list(first_page["report_urls"])
        page_size = len(first_page["report_urls"]) or 1
        total = first_page["total"]
        pages_total = None
        if total:
            capped_total = min(total, max_reports) if max_reports is not None else total
            pages_total = (capped_total + page_size - 1) // page_size

        if progress_callback:
            progress_callback({
                "event": "listing",
                "pages_done": 1,
                "pages_total": pages_total,
                "urls": len(dedupe_in_order(urls, limit=max_reports)),
            })

        next_url = first_page["next_url"]
        while next_url and (max_reports is None or len(dedupe_in_order(urls)) < max_reports):
            try:
                page = self.fetch_listing_page(next_url)
            except requests.RequestException as e:
                self.log(f"Failed to fetch listing page: {e}")
                break
            pages.append(page)
            urls.extend(page["report_urls"])
            next_url = page["next_url"]
            if progress_callback:
                progress_callback({
                    "event": "listing",
                    "pages_done": len(pages),
                    "pages_total": pages_total,
                    "urls": len(dedupe_in_order(urls, limit=max_reports)),
                })

        return dedupe_in_order(urls, limit=max_reports), (
            min(total, max_reports) if total is not None and max_reports is not None else total
        )

    def iter_trip_report_urls(self, hike_url, max_reports=None, total_callback=None):
        def progress(event):
            if total_callback and event["event"] == "total":
                total_callback(event.get("total"))

        urls, _ = self.collect_trip_report_urls(
            hike_url, max_reports=max_reports, workers=1, progress_callback=progress,
        )
        yield from urls

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

    def scan_report_safe(self, index, report_url, text_only=False, save_images_dir=None,
                         photos_mode="all"):
        manifest = [] if save_images_dir else None
        try:
            report = self.scan_trip_report(
                report_url, text_only=text_only,
                save_images_dir=save_images_dir, manifest=manifest,
                photos_mode=photos_mode,
            )
        except Exception as e:
            self.log(f"Failed to scan trip report {report_url}: {e}")
            report = {
                "url": report_url,
                "date": None,
                "text_matches": [],
                "image_matches": [],
                "error": str(e),
            }
        return index, report, manifest or []

    def scan_hike(self, hike_url, max_reports=None, text_only=False, save_images_dir=None,
                  photos_mode="all", progress_callback=None, workers=1):
        hike_url = normalize_hike_url(hike_url)
        if progress_callback:
            progress_callback({"event": "start", "hike_url": hike_url})
        title = self.get_hike_title(hike_url)
        if progress_callback and title:
            progress_callback({"event": "start", "hike_url": hike_url, "title": title})
        if save_images_dir:
            os.makedirs(save_images_dir, exist_ok=True)

        report_urls, listed_total = self.collect_trip_report_urls(
            hike_url, max_reports=max_reports, workers=workers,
            progress_callback=progress_callback,
        )
        total_reports = listed_total or len(report_urls)
        result_slots = [None] * len(report_urls)
        manifest_slots = [[] for _ in report_urls]
        scanned = 0
        matched_reports = 0
        image_count = 0

        if workers <= 1 or len(report_urls) <= 1:
            for index, report_url in enumerate(report_urls):
                self.log(f"Scanning trip report: {report_url}")
                _, report, report_manifest = self.scan_report_safe(
                    index, report_url, text_only=text_only,
                    save_images_dir=save_images_dir, photos_mode=photos_mode,
                )
                result_slots[index] = report
                manifest_slots[index] = report_manifest
                scanned += 1
                if report["text_matches"] or report["image_matches"]:
                    matched_reports += 1
                image_count += len(report_manifest)
                if progress_callback:
                    progress_callback({
                        "event": "report",
                        "scanned": scanned,
                        "total": total_reports,
                        "matches": matched_reports,
                        "images": image_count,
                    })
        else:
            max_workers = min(max(1, workers), len(report_urls))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [
                    executor.submit(
                        self.scan_report_safe, index, report_url,
                        text_only=text_only, save_images_dir=save_images_dir,
                        photos_mode=photos_mode,
                    )
                    for index, report_url in enumerate(report_urls)
                ]
                for future in as_completed(futures):
                    index, report, report_manifest = future.result()
                    result_slots[index] = report
                    manifest_slots[index] = report_manifest
                    scanned += 1
                    if report["text_matches"] or report["image_matches"]:
                        matched_reports += 1
                    image_count += len(report_manifest)
                    if progress_callback:
                        progress_callback({
                            "event": "report",
                            "scanned": scanned,
                            "total": total_reports,
                            "matches": matched_reports,
                            "images": image_count,
                        })

        reports = [
            report for report in result_slots
            if report and (report["text_matches"] or report["image_matches"])
        ]
        manifest = [
            entry for report_manifest in manifest_slots for entry in report_manifest
        ]

        if save_images_dir:
            with open(os.path.join(save_images_dir, "manifest.json"), "w") as f:
                json.dump(manifest, f, indent=2)

        if progress_callback:
            progress_callback({
                "event": "finish",
                "scanned": scanned,
                "total": total_reports,
                "matches": len(reports),
                "images": len(manifest) if save_images_dir else 0,
            })

        return {
            "hike_url": hike_url,
            "hike_title": title,
            "matching_reports": reports,
            "images_downloaded": len(manifest) if save_images_dir else None,
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
                         help="Delay in seconds after each HTTP request, per worker "
                              "(default: 0.5)")
    parser.add_argument("--workers", type=int, default=6,
                         help="Number of trip reports/listing pages to fetch concurrently "
                              "(default: 6; use 1 for serial scanning)")
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
                         help="Print detailed fetch progress to stderr")
    parser.add_argument("--no-progress", action="store_true",
                         help="Disable the live progress bar")
    args = parser.parse_args()

    if args.vision_review and not args.save_images:
        parser.error("--vision-review requires --save-images")

    scanner = Scanner(delay=args.delay, verbose=args.verbose)
    progress = ProgressBar(enabled=(
        sys.stderr.isatty() and not args.verbose and not args.no_progress
    ))
    results = scanner.scan_hike(
        args.hike_url, max_reports=args.max_reports, text_only=args.text_only,
        save_images_dir=args.save_images, photos_mode=args.photos_mode,
        progress_callback=progress, workers=args.workers,
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
