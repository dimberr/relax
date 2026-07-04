"""Tests for the vendored extraction core and the /api/extract endpoint.

The golden test pins the core's output to the daystar CLI's known-good result
for the 2026-02-10 sample, so the vendored copy can't silently drift.
"""
import datetime
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.extractor.core import (
    ExtractionError,
    decode_image,
    extract_from_array,
    stress_score_for_y,
    validate_dimensions,
    _PIXEL9_PROFILE,
    _IPHONE_PROFILE,
)
from app.main import app

MASK_PATH = str(Path(__file__).parents[1] / "app" / "extractor" / "mask_scaled.png")


def test_stress_score_zone_boundaries():
    """Zone band edges land at exactly 100/75/50/25/0 on both device profiles."""
    for profile in (_PIXEL9_PROFILE, _IPHONE_PROFILE):
        z = profile.zones
        assert stress_score_for_y(z["stressed"][0], profile) == 100.0
        assert stress_score_for_y(z["stressed"][1], profile) == 75.0
        assert stress_score_for_y(z["engaged"][1], profile) == 50.0
        assert stress_score_for_y(z["relaxed"][1], profile) == 25.0
        assert stress_score_for_y(z["restored"][1], profile) == 0.0


def test_stress_score_midpoints():
    """Zone midpoints land at 87.5 / 62.5 / 37.5 / 12.5."""
    for profile in (_PIXEL9_PROFILE, _IPHONE_PROFILE):
        z = profile.zones
        for zone_name, expected_mid in [
            ("stressed", 87.5),
            ("engaged", 62.5),
            ("relaxed", 37.5),
            ("restored", 12.5),
        ]:
            y0, y1 = z[zone_name]
            mid_y = (y0 + y1) // 2
            score = stress_score_for_y(mid_y, profile)
            assert abs(score - expected_mid) <= 0.5, (
                f"{profile.name} {zone_name} midpoint: got {score}, expected ~{expected_mid}"
            )


def test_stress_score_zone_membership():
    """Every score for a dot in zone X falls in that zone's 25-pt band."""
    for profile in (_PIXEL9_PROFILE, _IPHONE_PROFILE):
        expected_ranges = {
            "stressed": (75.0, 100.0),
            "engaged": (50.0, 75.0),
            "relaxed": (25.0, 50.0),
            "restored": (0.0, 25.0),
        }
        for zone_name, (lo, hi) in expected_ranges.items():
            y0, y1 = profile.zones[zone_name]
            for y in range(y0, y1 + 1):
                score = stress_score_for_y(y, profile)
                assert lo <= score <= hi, (
                    f"{profile.name} y={y} zone={zone_name}: score {score} outside [{lo},{hi}]"
                )
SAMPLE_DATE = datetime.date(2026, 2, 10)


# ---- core ----------------------------------------------------------------

def test_golden_matches_daystar_cli(sample_png_bytes, golden_rows):
    img = decode_image(sample_png_bytes)
    result = extract_from_array(img, MASK_PATH, SAMPLE_DATE)

    got = [(p["timestamp"].replace("T", " "), p["zone"]) for p in result["points"]]
    assert got == golden_rows


PIXEL9_DATE = datetime.date(2026, 4, 27)


def test_pixel9_matches_baseline(pixel9_png_bytes, pixel9_baseline_rows):
    """Pin the Pixel 9 extractor output (zone + stress_score) against drift.

    This is a self-baseline, not an independent oracle: daystar never handled
    Pixel 9, so it captures current known-good behavior. Regenerate the fixture
    CSV deliberately (and review the diff) if extraction logic legitimately
    changes — don't edit it just to make this pass.
    """
    img = decode_image(pixel9_png_bytes)
    # Pixel 9 uses a brightness filter, not the mask, but extract_from_array
    # takes the mask path unconditionally (ignored for non-mask profiles).
    result = extract_from_array(img, MASK_PATH, PIXEL9_DATE)

    got = [
        (p["timestamp"].replace("T", " "), p["zone"], str(p["stress_score"]))
        for p in result["points"]
    ]
    assert got == pixel9_baseline_rows
    assert result["meta"]["device"] == "Pixel 9"


def test_decode_rejects_garbage():
    with pytest.raises(ExtractionError):
        decode_image(b"not an image")


def test_decode_rejects_decompression_bomb():
    # A PNG header declaring 60000x60000 (3.6 GP) — rejected before any decode.
    import struct

    header = (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13) + b"IHDR"
        + struct.pack(">II", 60000, 60000)
    )
    with pytest.raises(ExtractionError, match="too large"):
        decode_image(header)


def test_guard_reads_real_dimensions():
    # The guard must parse real PNG/JPEG headers and let normal sizes through.
    import cv2

    from app.extractor.core import _jpeg_dimensions, _png_dimensions

    img = np.zeros((1136, 640, 3), np.uint8)
    ok, png = cv2.imencode(".png", img)
    assert ok and _png_dimensions(png.tobytes()) == (640, 1136)
    ok, jpg = cv2.imencode(".jpg", img)
    assert ok and _jpeg_dimensions(jpg.tobytes()) == (640, 1136)


def test_validate_dimensions_accepts_expected():
    # iPhone profile
    profile = validate_dimensions(np.zeros((1136, 640, 3), np.uint8))
    assert profile.name == "iPhone SE/8"
    # Pixel 9 profile
    profile = validate_dimensions(np.zeros((1939, 864, 3), np.uint8))
    assert profile.name == "Pixel 9"


def test_validate_dimensions_rejects_wrong_size():
    with pytest.raises(ExtractionError):
        validate_dimensions(np.zeros((800, 600, 3), np.uint8))


def test_parse_time_24h_clamps_garbled_minute():
    """A garbled OCR minute (e.g. '8:99') must not raise; it's clamped to 0–59."""
    from app.extractor.ocr_helpers import parse_time_string

    ref = datetime.date(2026, 4, 27)
    dt = parse_time_string("8:99", ref, time_24h=True)
    assert (dt.hour, dt.minute) == (8, 59)
    # Sanity: a normal 24h value is unaffected.
    dt2 = parse_time_string("18:27", ref, time_24h=True)
    assert (dt2.hour, dt2.minute) == (18, 27)


# ---- API -----------------------------------------------------------------

client = TestClient(app)


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_extract_endpoint_returns_points(sample_png_bytes, golden_rows):
    r = client.post(
        "/api/extract",
        files={"file": ("chart.png", sample_png_bytes, "image/png")},
        data={"date": "2026-02-10"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["points"]) == len(golden_rows)
    assert body["meta"]["reference_date"] == "2026-02-10"
    assert "annotated_png" not in body


def test_extract_endpoint_includes_image_when_requested(sample_png_bytes):
    r = client.post(
        "/api/extract",
        files={"file": ("chart.png", sample_png_bytes, "image/png")},
        data={"date": "2026-02-10", "include_image": "true"},
    )
    assert r.status_code == 200
    assert r.json()["annotated_png"]


def test_extract_endpoint_rejects_bad_date(sample_png_bytes):
    r = client.post(
        "/api/extract",
        files={"file": ("chart.png", sample_png_bytes, "image/png")},
        data={"date": "10-02-2026"},
    )
    assert r.status_code == 422


def test_extract_endpoint_rejects_wrong_size():
    # A valid 1x1 PNG that fails the dimension check.
    import cv2

    ok, buf = cv2.imencode(".png", np.zeros((10, 10, 3), np.uint8))
    assert ok
    r = client.post(
        "/api/extract",
        files={"file": ("tiny.png", buf.tobytes(), "image/png")},
        data={"date": "2026-02-10"},
    )
    assert r.status_code == 422


def test_extract_endpoint_sheds_load_when_busy(sample_png_bytes, monkeypatch):
    # With the in-flight count already at the limit, a new request is rejected
    # fast (503 + Retry-After) before any OCR runs, rather than queueing.
    from app import routes

    monkeypatch.setattr(routes, "_inflight", routes._MAX_INFLIGHT)
    r = client.post(
        "/api/extract",
        files={"file": ("chart.png", sample_png_bytes, "image/png")},
        data={"date": "2026-02-10"},
    )
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "60"


def test_extract_endpoint_rejects_oversized():
    # Bodies over the 1 MB cap are rejected before any decode/OCR.
    from app import routes

    blob = b"\x00" * (routes.MAX_UPLOAD_BYTES + 1)
    r = client.post(
        "/api/extract",
        files={"file": ("big.png", blob, "image/png")},
        data={"date": "2026-02-10"},
    )
    assert r.status_code == 413


def test_extract_endpoint_times_out(sample_png_bytes, monkeypatch):
    # A near-zero timeout fires before OCR finishes → 504, not a hung request.
    from app import routes

    monkeypatch.setattr(routes, "_EXTRACT_TIMEOUT", 0.0001)
    r = client.post(
        "/api/extract",
        files={"file": ("chart.png", sample_png_bytes, "image/png")},
        data={"date": "2026-02-10"},
    )
    assert r.status_code == 504


def test_extract_endpoint_enforces_daily_limit(sample_png_bytes):
    # Pre-seed the quota for the TestClient's IP so the limit is hit before any
    # OCR runs, then assert the next request is rejected with 429 + Retry-After.
    from app import ratelimit

    for _ in range(ratelimit.max_hits()):
        ratelimit.record("testclient")

    r = client.post(
        "/api/extract",
        files={"file": ("chart.png", sample_png_bytes, "image/png")},
        data={"date": "2026-02-10"},
    )
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0
    assert "github.com/cyanobac/relax" in r.json()["detail"]
