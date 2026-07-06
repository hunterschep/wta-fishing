# WTA Fishing Scanner

Scans [wta.org](https://www.wta.org) trip reports for a given hike and looks for fishing-related information: mentions in report text, and photos captioned with fishing terms.

## Setup

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Usage

```
.venv/bin/python wta_fishing_scanner.py <hike-url>
```

Example:

```
.venv/bin/python wta_fishing_scanner.py https://www.wta.org/go-hiking/hikes/kendall-peak-lakes
```

This walks every trip report for the hike (via WTA's paginated `@@related_tripreport_listing` endpoint), fetches each report page, and checks the report text and photo captions against a list of fishing-related terms (fish, trout, salmon, angler, fly rod, packraft, etc.). Known false positives are filtered out (e.g. the "Salmon La Sac" trailhead name, "salmonberry" the plant).

### Options

- `--max-reports N` — limit how many trip reports to scan (useful for a quick test)
- `--workers N` — fetch listing pages and trip reports concurrently (default 6; use 1 for the old serial behavior)
- `--text-only` — skip photo captions entirely, fastest option
- `--save-images DIR` — download every photo from every scanned report into `DIR`, plus a `manifest.json` mapping each file back to its report URL, date, and caption. Useful because caption text alone misses photos that show fishing/fish but aren't captioned as such.
- `-o, --output FILE` — write the full results as JSON
- `-v, --verbose` — print detailed fetch progress to stderr
- `--no-progress` — suppress the live progress bar shown during normal terminal runs
- `--delay SECONDS` — delay after each HTTP request, per worker (default 0.5s; be polite to WTA's servers)

For faster single-hike scans, keep the default workers or tune explicitly:

```
.venv/bin/python wta_fishing_scanner.py https://www.wta.org/go-hiking/hikes/gravel-lake --workers 8
```

`run_region_scan.py` already scans multiple hikes at once with `--workers`; it also supports `--hike-workers` if you want concurrency inside each hike. Increase that carefully because total request concurrency is roughly `--workers * --hike-workers`.

## Reviewing downloaded photos

Caption text only tells you what the photographer chose to write, not what's actually in a photo. `--vision-review` runs an automated visual pass over every downloaded photo, no manual review step needed — it's all part of the one run:

```
.venv/bin/python wta_fishing_scanner.py <hike-url> --save-images output/<hike>-images --vision-review
```

There are two backends, picked with `--vision-backend {ollama,anthropic}` (default: `ollama`):

### `ollama` (default): local, free, per-photo

Requires [Ollama](https://ollama.com) running locally with a vision-capable model pulled:

```
ollama pull qwen2.5vl:7b
ollama serve   # if not already running
```

Reviews each downloaded photo individually at full resolution — one local model call per photo, asking it to describe the photo and then decide if it shows a fish, someone fishing, or fishing gear. Free and fully offline, no API key needed. Override the model with `--vision-model`, or point at a non-default Ollama host with `--vision-base-url` (or the `OLLAMA_BASE_URL` env var).

**Accuracy caveat:** a 7B local model reliably catches clear, close-up shots (someone holding a rod, a fish in a net) but misses a meaningful fraction of subtler cases — fish visible in water at a distance, or a small/distant human figure fishing across a lake. In one validation run (68 photos, 4 known fishing photos) it caught 2 of the 4 known photos, missed 2 subtler ones, and also surfaced one genuinely new uncaptioned fishing photo that both caption-matching and manual contact-sheet review had missed. Treat it as a free supplementary pass that adds signal, not a substitute for the caption-match results or a manual look at the contact sheets.

### `anthropic`: cloud, paid, batched by contact sheet

```
export ANTHROPIC_API_KEY=sk-ant-...
.venv/bin/python wta_fishing_scanner.py <hike-url> --save-images output/<hike>-images --vision-review --vision-backend anthropic
```

Tiles photos into contact sheets (grids of ~20 numbered thumbnails) and sends each sheet to the Claude API in one call. This batching is cheap and, unlike the local model, a frontier model stays accurate even on the shrunk thumbnails in a grid — so this is the more accurate option when it matters and cost isn't a concern.

Requires the `ANTHROPIC_API_KEY` environment variable (billed separately from any Claude Code/Desktop usage — get one at https://console.anthropic.com/settings/keys). Defaults to `claude-haiku-4-5-20251001` for cost efficiency; override with `--vision-model` for a more capable model if accuracy matters more than cost.

Findings (with the source report URL, date, caption, local file path, and description) are printed and included in `-o` JSON output under `vision_findings`.

`run_region_scan.py` supports the same `--vision-review`/`--vision-backend`/`--vision-model`/`--vision-base-url` flags, running the vision review per hike right after that hike's contact sheets are built, and folds all findings into the combined markdown report automatically (a per-hike findings list, embedded photos, and a summary-table column).

### Manual: contact sheets only

If you'd rather review yourself instead of (or in addition to) `--vision-review`, contact sheets are built automatically whenever `--vision-review` runs, or standalone:

```
.venv/bin/python make_contact_sheets.py DIR
```

This writes numbered grid images (default 5x4 per sheet) to `DIR/contact_sheets/`, with each thumbnail labeled by its index into `manifest.json`. Flip through the sheets, note any index showing fishing activity, and look it up in `manifest.json` for the source report/caption.

## Notes and limitations

- Keyword matching is heuristic — it can't see what's actually in a photo, only its caption. A caught fish with no caption, or a caption like "nice view here," will be missed by text/caption matching alone; that's what `--vision-review` (or manual contact-sheet review) is for. It's also caught non-English captions and colloquial spellings (e.g. "fishin'") that the keyword regex doesn't match.
- Popular hikes can have hundreds of trip reports and 1000+ photos; `--save-images` downloads everything it finds (or use `--photos-mode matched` in the region harness to only download from reports that already have a text/caption hit), so expect the full run to take a while (and use `--max-reports` for a quick sanity check first).
- The `ollama` backend reviews one photo at a time, so it's slower than the batched `anthropic` backend, but has no per-call cost and needs no API key.
- The local Ollama model needs its context window capped (`num_ctx`) — the default 128k context for a vision model blows past available memory for a single-image prompt and crashes the Ollama runner. `vision_review.py` sets this explicitly; if you swap in a different local model and see it crash or hang, check this first.
- WTA blocks the default `requests`/`urllib` user agent, so the scanner sends a browser-like User-Agent string.
