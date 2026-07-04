# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A web app that extracts Oura's **Daytime Stress** screen from of a screenshot (Oura
exposes no API for it) and returns `(timestamp, stress zone)` rows. FastAPI +
OpenCV + Tesseract OCR backend, React/Vite frontend, Caddy for TLS/auth in prod.

## Commands

Backend (from `backend/`, needs Tesseract installed locally — or use Docker):

```bash
python -m venv .venv && .venv\Scripts\activate    # PowerShell/Windows
pip install -r requirements.txt                   # runtime
pip install -r requirements-dev.txt               # + pytest
uvicorn app.main:app --reload --port 8000         # dev server
pytest                                            # all tests
pytest tests/test_extract.py::test_golden_matches_daystar_cli   # single test
```

Frontend (from `frontend/`, proxies `/api` → `127.0.0.1:8000`):

```bash
npm install
npm run dev        # http://localhost:5173
npm run build      # tsc -b && vite build
```

Docker (full stack, Caddy on :80 serving plain HTTP at http://localhost):

```bash
docker compose up --build
```

`pytest` runs **real OCR**, so Tesseract must be installed. `TESSERACT_CMD`
overrides the binary path; otherwise `configure_tesseract()` auto-locates it
(`C:\Program Files\Tesseract-OCR\tesseract.exe` on Windows, PATH elsewhere).

## Architecture

Request flow: `frontend/src/api.ts` POSTs multipart to `/api/extract` →
`backend/app/routes.py` (decode, size-guard, HTTP error mapping) →
`extractor/core.py::extract_from_array` orchestrates the pipeline using the three
helper modules:

- **`image_helpers.py`** — mask + crop the chart, `detect_dots` via
  `cv2.HoughCircles`, `zone_for_y` (y-pixel → stress zone), `detect_gaps`.
- **`ocr_helpers.py`** — `extract_times_from_chart` OCRs the left/right x-axis
  time labels to anchor the chart's start/end, with Oura-specific edge-case
  handling (late-night start ≥11 PM, two-midnight spans). `parse_time_string`
  corrects common OCR misreads (`O`→`0`, `l`/`I`/`|`→`1`).
- **`visualization_helpers.py`** — draws the annotated preview image.

Each detected dot's x maps to a 15-minute timeslot → timestamp; its y maps to a
zone. The user supplies the **reference date** via the form (the original daystar
CLI derived it from the filename — this is the key decoupling point of the
vendored core).

## Non-obvious constraints

- **Two supported resolutions: 640×1136 (iPhone SE/8) and 864×1939 (Pixel 9).**
  All pixel constants live in `DeviceProfile` instances in `core.py`; the profile
  is auto-selected by resolution. `validate_dimensions()` *rejects* other sizes
  (±4px tolerance) rather than silently producing wrong data. iPhone uses a
  bundled `mask_scaled.png` to suppress noise; Pixel 9 uses a brightness filter
  instead (`min_dot_brightness=160.0`) because data dots are near-white (~238)
  while zone-separator artifacts are ~70–150. Adding a new device means adding a
  new `DeviceProfile` to `_PROFILES` in `core.py` — no other files need changing.

- **The extractor core is vendored from the larger *daystar* project.** The
  golden test (`test_golden_matches_daystar_cli`) pins this copy's output to
  daystar's known-good CSV for the 2026-02-10 fixture so the vendored copy can't
  silently drift. If you change extraction logic, the golden fixtures in
  `backend/tests/fixtures/` are the source of truth — don't edit them to make a
  test pass without understanding why output changed.

- **Two error classes by design.** `ExtractionError` (subclass of `ValueError`)
  is for expected, user-facing problems (bad image, wrong size, no dots) and
  maps to HTTP 422; anything else surfaces as 500. Keep that distinction when
  adding failure modes.

- **`debug_ocr` writes `debug_ocr_*.png` files to the current working
  directory** — leave it off in the request path.

## Prod deployment

Caddy terminates TLS (auto Let's Encrypt) and reverse-proxies `/api/*` to the
backend; only Caddy publishes ports. The site is public and unauthenticated —
abuse is handled by the per-IP rate limit (`ratelimit.py`), the in-flight
concurrency cap (`routes.py`), and Cloudflare in front (see README). Config via
`.env` (`CADDY_SITE_ADDR` + `CADDY_TLS_SNIPPET`; the domain lives in
`CADDY_SITE_ADDR`).

Every request is logged to SQLite by `requestlog.py` (separate DB from the
rate-limiter, which prunes aggressively — the log is durable): timestamp, hashed
IP, processing time, and outcome status, for *all* requests including
rejections. Raw IPs are never stored — they're SHA-256'd with `REQUEST_LOG_SALT`,
which **must stay constant across restarts** or repeat-visitor correlation
breaks. There is deliberately no admin HTTP endpoint (the box has no auth);
inspect the log out-of-band with the read-only CLI: `python -m app.logdump`
(CSV to stdout, `--limit N`, `--out file`). Disable logging with
`REQUEST_LOG=0`. The footer "Contact" mailto is `CONTACT_EMAIL` in `.env`, baked
into the frontend bundle at build time (Vite build arg → `VITE_CONTACT_EMAIL`);
empty hides the link so no address ships in source.
