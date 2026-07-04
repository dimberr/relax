"""Shared test fixtures and Tesseract setup."""
import os
from pathlib import Path

import pytest

from app.extractor.ocr_helpers import configure_tesseract

# Tests exercise real OCR, so make sure pytesseract can find the binary
# (PATH on CI/Linux, Program Files on Windows, or TESSERACT_CMD override).
configure_tesseract(os.environ.get("TESSERACT_CMD"))

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_DATE = "2026-02-10"


@pytest.fixture(autouse=True)
def isolated_ratelimit_db(tmp_path, monkeypatch):
    """Point the rate limiter at a fresh per-test SQLite file so tests don't
    share counter state (and don't write to the real default DB)."""
    monkeypatch.setenv("RATE_LIMIT_DB", str(tmp_path / "ratelimit.db"))


@pytest.fixture(autouse=True)
def isolated_request_log_db(tmp_path, monkeypatch):
    """Point the request log at a fresh per-test SQLite file so tests don't
    write to the real default DB."""
    monkeypatch.setenv("REQUEST_LOG_DB", str(tmp_path / "requests.db"))


@pytest.fixture
def sample_png_bytes() -> bytes:
    return (FIXTURES / f"stress_chart_{SAMPLE_DATE}.png").read_bytes()


@pytest.fixture
def golden_rows() -> list[tuple[str, str]]:
    """Expected (timestamp, zone) rows from the daystar CLI golden CSV."""
    lines = (FIXTURES / f"stress_zones_{SAMPLE_DATE}.csv").read_text().splitlines()
    rows = []
    for line in lines[1:]:  # skip header
        ts, zone = line.split(",")
        rows.append((ts, zone))
    return rows


PIXEL9_DATE = "2026-04-27"


@pytest.fixture
def pixel9_png_bytes() -> bytes:
    return (FIXTURES / f"stress_chart_pixel9_{PIXEL9_DATE}.png").read_bytes()


@pytest.fixture
def pixel9_baseline_rows() -> list[tuple[str, str, str]]:
    """Regression baseline (timestamp, zone, stress_score) for the Pixel 9 sample.

    Unlike the iPhone golden (pinned to the independent daystar CLI oracle),
    daystar never handled Pixel 9, so this is the extractor's own known-good
    output — it guards against silent drift, not independent correctness.
    """
    lines = (FIXTURES / f"stress_zones_pixel9_{PIXEL9_DATE}.csv").read_text().splitlines()
    rows = []
    for line in lines[1:]:  # skip header
        ts, zone, score = line.split(",")
        rows.append((ts, zone, score))
    return rows
