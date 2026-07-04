"""Pure stress-chart extraction: image array in, structured data out.

This is the web-facing core, decoupled from the filesystem, the CLI, and any
date-from-filename assumptions. The original daystar CLI built timestamps from a
date embedded in the screenshot's filename; here the caller passes an explicit
reference_date (entered by the user in the web form).

Two device profiles are supported: iPhone SE/8-class (640×1136) and Pixel 9
(864×1939). validate_dimensions() auto-selects the profile or raises if the
resolution matches neither.
"""
from dataclasses import dataclass
from datetime import timedelta

import cv2
import numpy as np

from .image_helpers import preprocess_array, detect_dots, zone_for_y, detect_gaps
from .ocr_helpers import extract_times_from_chart
from .visualization_helpers import create_visualization

# Boundary offset for fine-tuning timeslot alignment (device-independent)
BOUNDARY_OFFSET = 2.75

# Boundary tolerance for filtering dots (HoughCircles imprecision)
BOUNDARY_TOLERANCE = 5

# Expected interval between data points (minutes)
EXPECTED_INTERVAL_MINUTES = 15

DIMENSION_TOLERANCE = 4  # px of slack on each axis

# Pixel 9: horizontal separator lines between zones have brightness exactly 183.
# Real data dots at the same y-positions are brighter (≥184). This threshold
# is used to discriminate the two when a detected circle center falls on a sep row.
SEPARATOR_BRIGHTNESS = 183


@dataclass(frozen=True)
class DeviceProfile:
    name: str
    width: int
    height: int
    # Chart crop bounds (absolute y in the full screenshot)
    y_min: int
    y_max: int
    # Leftmost/rightmost possible dot x-positions
    first_dot_x: int
    last_dot_x: int
    # Zone y-ranges (absolute, matching y_min/y_max coordinate space)
    zones: dict
    # HoughCircles radii
    hough_min_radius: int
    hough_max_radius: int
    hough_min_dist: int
    # OCR region: (y_min, y_max, left_x_start, left_x_end, right_x_start, right_x_end)
    ocr_y_min: int
    ocr_y_max: int
    ocr_left_x_start: int
    ocr_left_x_end: int
    ocr_right_x_start: int
    ocr_right_x_end: int
    # Whether the time axis uses 24h format (Pixel 9) vs 12h (iPhone)
    time_24h: bool
    # Whether to apply the bundled mask (iPhone only — mask is device-specific)
    use_mask: bool
    # Minimum mean pixel brightness at a detected dot's center; dots below this
    # are zone-separator artifacts. 0.0 disables the filter (iPhone uses a mask
    # instead). Pixel 9 needs this because data dots are near-white (~238) while
    # separator artifacts are ~70-150.
    min_dot_brightness: float
    # Pixel 9 only: Oura dims dots under the zone-label overlay on the right side
    # of the chart (~x>650). In that region the dots drop to ~113-183 brightness
    # so min_dot_brightness (160) would cut them. We use a lower threshold there
    # but also exclude dots at the known separator y-positions (which are ~183 in
    # that region too). label_x_start=0 disables this logic (iPhone).
    label_x_start: int
    label_region_min_brightness: float
    separator_ys: tuple  # mid-zone grid line y positions; artifacts here are b≤183


_IPHONE_PROFILE = DeviceProfile(
    name="iPhone SE/8",
    width=640, height=1136,
    y_min=260, y_max=724,
    first_dot_x=40, last_dot_x=600,
    zones={
        "stressed": (260, 374),
        "engaged": (375, 486),
        "relaxed": (487, 596),
        "restored": (597, 724),
    },
    hough_min_radius=3, hough_max_radius=8, hough_min_dist=8,
    ocr_y_min=780, ocr_y_max=825,
    ocr_left_x_start=20, ocr_left_x_end=160,
    ocr_right_x_start=450, ocr_right_x_end=640,
    time_24h=False,
    use_mask=True,
    min_dot_brightness=0.0,
    label_x_start=0,
    label_region_min_brightness=0.0,
    separator_ys=(),
)

_PIXEL9_PROFILE = DeviceProfile(
    name="Pixel 9",
    width=864, height=1939,
    y_min=429, y_max=963,
    first_dot_x=54, last_dot_x=814,
    zones={
        "stressed": (429, 559),
        "engaged": (560, 691),
        "relaxed": (692, 822),
        "restored": (823, 962),
    },
    hough_min_radius=4, hough_max_radius=8, hough_min_dist=12,
    ocr_y_min=1005, ocr_y_max=1065,
    ocr_left_x_start=20, ocr_left_x_end=200,
    ocr_right_x_start=620, ocr_right_x_end=864,
    time_24h=True,
    use_mask=False,
    min_dot_brightness=160.0,
    # Right ~25% of chart: Oura dims dots under "Stressed/Engaged/Relaxed/Restored"
    # labels to ~113-183. Use lower threshold there but exclude separator lines
    # which are also ~183 at fixed y positions (midpoint of each zone + 70px).
    label_x_start=650,
    label_region_min_brightness=80.0,
    separator_ys=(499, 629, 761, 892),
)

_PROFILES = [_IPHONE_PROFILE, _PIXEL9_PROFILE]


class ExtractionError(ValueError):
    """Raised for recoverable, user-facing extraction problems (e.g. bad image)."""


# Decompression-bomb guard. cv2.imdecode allocates the full bitmap before we ever
# check dimensions, so a tiny but highly-compressed file could blow up memory. A
# real Oura screenshot is 640x1136 (~0.73 MP); even high-res phone screenshots are
# only a few MP, so this leaves huge headroom while blocking multi-gigabyte bombs.
MAX_DECODE_PIXELS = 40_000_000  # ~40 MP -> ~120 MB decoded at 3 bytes/px


def _png_dimensions(data):
    """(width, height) from a PNG IHDR header, or None if not a PNG."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _jpeg_dimensions(data):
    """(width, height) from a JPEG SOF marker, or None if not a parseable JPEG."""
    if len(data) < 4 or data[0] != 0xFF or data[1] != 0xD8:  # SOI
        return None
    i, n = 2, len(data)
    while i + 1 < n:
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        # Standalone markers carry no length field.
        if marker == 0x01 or 0xD0 <= marker <= 0xD9:
            i += 2
            continue
        if i + 4 > n:
            return None
        seg_len = (data[i + 2] << 8) | data[i + 3]
        # Start-of-Frame markers (excluding DHT/JPG/DAC) hold the dimensions.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if i + 9 > n:
                return None
            height = (data[i + 5] << 8) | data[i + 6]
            width = (data[i + 7] << 8) | data[i + 8]
            return width, height
        i += 2 + seg_len
    return None


def _guard_decompression_bomb(file_bytes):
    """Reject images whose header declares an absurd pixel count, before decode.

    Only PNG/JPEG headers are parsed (the formats this tool expects); unknown
    headers fall through to cv2.imdecode + validate_dimensions, and the upload
    size cap bounds the worst case for those.
    """
    dims = _png_dimensions(file_bytes) or _jpeg_dimensions(file_bytes)
    if dims is None:
        return
    width, height = dims
    if width * height > MAX_DECODE_PIXELS:
        raise ExtractionError(
            f"Image dimensions {width}x{height} are too large to process."
        )


def decode_image(file_bytes):
    """Decode uploaded bytes into a BGR image array, or raise ExtractionError."""
    _guard_decompression_bomb(file_bytes)
    arr = np.frombuffer(file_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ExtractionError("Could not decode image. Upload a PNG or JPEG screenshot.")
    return img


def detect_profile(img) -> DeviceProfile:
    """Return the DeviceProfile matching this image's resolution, or raise ExtractionError."""
    h, w = img.shape[:2]
    for p in _PROFILES:
        if abs(w - p.width) <= DIMENSION_TOLERANCE and abs(h - p.height) <= DIMENSION_TOLERANCE:
            return p
    supported = " or ".join(f"{p.width}×{p.height} ({p.name})" for p in _PROFILES)
    raise ExtractionError(
        f"Unexpected image size {w}×{h}. This tool supports {supported} "
        f"Oura 'Daytime Stress' screenshots. Other resolutions will produce wrong results."
    )



def stress_score_for_y(y: int, profile: "DeviceProfile") -> float:
    """Map a dot's pixel-y to a 0–100 stress score with equal 25-pt zone bands.

    stressed=100..75 (top), engaged=75..50, relaxed=50..25, restored=25..0 (bottom).
    Interpolates linearly within each zone band so boundaries land at exactly 75/50/25.
    """
    zone_order = ["stressed", "engaged", "relaxed", "restored"]
    for i, name in enumerate(zone_order):
        y0, y1 = profile.zones[name]
        if y0 <= y <= y1:
            t = (y - y0) / (y1 - y0)
            return round(100 - i * 25 - t * 25, 1)
    return 100.0 if y < profile.zones["stressed"][0] else 0.0


def validate_dimensions(img):
    """Ensure the screenshot matches a supported resolution. Returns the DeviceProfile."""
    return detect_profile(img)


def calculate_timestamp_from_timeslot(x_pos, first_dot_x, last_dot_x,
                                      first_time, last_time, offset=0):
    """Map an x-position to a timestamp by dividing the chart into equal slots."""
    total_minutes = (last_time - first_time).total_seconds() / 60
    total_timeslots = total_minutes / EXPECTED_INTERVAL_MINUTES

    x_range = last_dot_x - first_dot_x
    pixels_per_timeslot = x_range / total_timeslots

    pixels_from_start = x_pos - first_dot_x - offset
    timeslot_index = round(pixels_from_start / pixels_per_timeslot)

    return first_time + timedelta(minutes=timeslot_index * EXPECTED_INTERVAL_MINUTES)


def extract_from_array(screenshot, mask_path, reference_date, debug_ocr=False):
    """Extract stress points from a decoded BGR screenshot.

    Args:
        screenshot: BGR image array. The DeviceProfile (and thus the supported
            resolution) is auto-detected here via detect_profile; an unsupported
            size raises ExtractionError.
        mask_path: path to the bundled mask_scaled.png (used only for iPhone profile).
        reference_date: date the chart represents (from the user's form input).
        debug_ocr: forwarded to the OCR helper (writes debug crops to cwd).

    Returns:
        dict with:
          points:   list of {timestamp (ISO str), zone, stress_score, x_pos, y_pos}
          gaps:     list of gap descriptors
          warnings: list of human-readable strings
          meta:     detection/timeslot diagnostics
          annotated: BGR image array with detected dots drawn (for optional preview)
    """
    warnings = []

    profile = detect_profile(screenshot)
    _, _, mask, blur = preprocess_array(
        screenshot, mask_path if profile.use_mask else None, profile
    )

    first_time, last_time = extract_times_from_chart(
        screenshot, reference_date, profile, debug=debug_ocr
    )

    circles = detect_dots(blur, profile)
    for c in circles:
        c[1] += profile.y_min  # back to original-image coordinates

    def _keep(c):
        x, y = c[0], c[1]
        if not (profile.first_dot_x - BOUNDARY_TOLERANCE) <= x <= (profile.last_dot_x + BOUNDARY_TOLERANCE):
            return False
        if profile.min_dot_brightness == 0.0:
            return True
        brightness = float(np.mean(screenshot[y, x]))
        # Separator-line artifacts at known grid y positions have b≤183;
        # real data dots near those lines are b≥184. Apply in both regions.
        SEP_TOL = 4
        if any(abs(y - sy) <= SEP_TOL for sy in profile.separator_ys):
            return brightness > SEPARATOR_BRIGHTNESS
        if profile.label_x_start and x >= profile.label_x_start:
            return brightness >= profile.label_region_min_brightness
        return brightness >= profile.min_dot_brightness

    filtered = [c for c in circles if _keep(c)]
    if not filtered:
        raise ExtractionError("No data points detected in the chart area.")

    # Zones in top→bottom order (stressed=100..75, engaged=75..50, relaxed=50..25, restored=25..0)

    points = []
    for (x, y, _r) in filtered:
        ts = calculate_timestamp_from_timeslot(
            x, profile.first_dot_x, profile.last_dot_x, first_time, last_time, BOUNDARY_OFFSET
        )
        points.append({"timestamp": ts, "zone": zone_for_y(y, profile),
                       "stress_score": stress_score_for_y(y, profile),
                       "x_pos": int(x), "y_pos": int(y)})

    points.sort(key=lambda p: p["timestamp"])

    # Duplicate-timestamp warning (two dots collapsed into one timeslot)
    seen = {}
    for p in points:
        seen[p["timestamp"]] = seen.get(p["timestamp"], 0) + 1
    dupes = sorted(ts for ts, n in seen.items() if n > 1)
    if dupes:
        warnings.append(
            f"{len(dupes)} duplicate timestamp(s) detected — two dots fell into the "
            f"same 15-min slot. Times may be slightly off."
        )

    # Build a small DataFrame-free gap check via the vendored helper.
    import pandas as pd
    df = pd.DataFrame([{"timestamp": p["timestamp"]} for p in points])
    gaps_raw = detect_gaps(df, EXPECTED_INTERVAL_MINUTES)
    gaps = [
        {
            "after": g["after"].isoformat(),
            "before": g["before"].isoformat(),
            "gap_minutes": g["gap_minutes"],
            "missing_points": g["missing_points"],
        }
        for g in gaps_raw
    ]

    # NB: we intentionally do *not* warn when the first/last dot sits inside the
    # chart's time axis. Oura's chart spans a fixed axis range but only plots
    # dots where it has data, so leading/trailing empty space is normal framing,
    # not missing data — flagging it was just noise. Real anomalies (duplicate
    # timestamps above, and interior gaps below) are still surfaced.

    annotated = create_visualization(screenshot, filtered, df.assign(
        timestamp=[p["timestamp"] for p in points]))

    return {
        "points": [
            {"timestamp": p["timestamp"].isoformat(), "zone": p["zone"],
             "stress_score": p["stress_score"]}
            for p in points
        ],
        "gaps": gaps,
        "warnings": warnings,
        "meta": {
            "reference_date": reference_date.isoformat(),
            "first_time": first_time.isoformat(),
            "last_time": last_time.isoformat(),
            "detected_dots": len(circles),
            "used_dots": len(filtered),
            "device": profile.name,
        },
        "annotated": annotated,
    }
