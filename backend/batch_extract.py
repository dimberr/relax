"""Batch-extract all Pixel 9 screenshots to CSV.

The date of each screenshot is read from the Oura date-strip in the image
itself (top center), not from the filename (which is the capture time).
"""
import csv
import datetime
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import cv2
import pytesseract

from app.extractor.core import decode_image, extract_from_array
from app.extractor.ocr_helpers import configure_tesseract

configure_tesseract()

SCREENSHOTS_DIR = Path(__file__).parents[1] / "oura_screenshots2" / "Oura daystress"
MASK_PATH = str(Path(__file__).parent / "app" / "extractor" / "mask_scaled.png")
OUT_CSV = Path(__file__).parents[1] / "results.csv"
ANNOTATED_DIR = Path(__file__).parents[1] / "annotated"

# All files were captured on 2026-07-04 (from filename); used to resolve
# "Today"/"Yesterday" and to pick the right year for month/day labels.
CAPTURE_DATE = datetime.date(2026, 7, 4)
IMG_W = 864
CENTER_X = IMG_W // 2
SCALE = 3
WINDOW = 300  # scaled px


def date_from_strip(img):
    """Read the selected date from the Oura date-navigation strip."""
    region = img[255:325, :]
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    up = cv2.resize(gray, (gray.shape[1] * SCALE, gray.shape[0] * SCALE),
                    interpolation=cv2.INTER_CUBIC)
    data = pytesseract.image_to_data(up, config="--psm 6",
                                     output_type=pytesseract.Output.DICT)
    center_s = CENTER_X * SCALE
    words = []
    for i, word in enumerate(data["text"]):
        w = word.strip()
        # pytesseract may return conf as a string; treat empty/unparseable as -1.
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if not w or conf < 0:
            continue
        x_mid = data["left"][i] + data["width"][i] // 2
        if abs(x_mid - center_s) <= WINDOW:
            words.append((data["left"][i], w))
    words.sort()
    label = " ".join(w for _, w in words).strip().rstrip(",")
    label_lower = label.lower()
    if "today" in label_lower:
        return CAPTURE_DATE
    if "yesterday" in label_lower:
        return CAPTURE_DATE - datetime.timedelta(days=1)
    m = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[.,]?\s*(\d{1,2})",
                  label, re.IGNORECASE)
    if m:
        parsed_month = datetime.datetime.strptime(m.group(1).capitalize(), "%b").month
        day = int(m.group(2))
        year = CAPTURE_DATE.year
        if parsed_month > CAPTURE_DATE.month:
            year -= 1
        return datetime.date(year, parsed_month, day)
    raise ValueError(f"Cannot parse date strip: '{label}'")


files = sorted(SCREENSHOTS_DIR.glob("*.png"))
ANNOTATED_DIR.mkdir(exist_ok=True)
print(f"Processing {len(files)} screenshots...")

rows = []
errors = []
for f in files:
    img_raw = f.read_bytes()
    try:
        img = decode_image(img_raw)
    except Exception as e:
        errors.append((f.name, f"decode: {e}"))
        print(f"  DECODE ERR {f.name}: {e}")
        continue
    try:
        ref_date = date_from_strip(img)
    except Exception as e:
        errors.append((f.name, f"date strip: {e}"))
        print(f"  DATE ERR {f.name}: {e}")
        continue
    try:
        result = extract_from_array(img, MASK_PATH, ref_date)
        cv2.imwrite(str(ANNOTATED_DIR / f"{ref_date}_{f.stem}.png"), result["annotated"])
        for p in result["points"]:
            rows.append({"date": ref_date.isoformat(),
                         "timestamp": p["timestamp"], "zone": p["zone"],
                         "stress_score": p["stress_score"], "filename": f.name})
        warn = f", {result['warnings'][0]}" if result["warnings"] else ""
        print(f"  {f.name}: {ref_date} {len(result['points'])} pts{warn}")
    except Exception as e:
        errors.append((f.name, str(e)))
        print(f"  EXTRACT ERR {f.name}: {e}")

with open(OUT_CSV, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=["date", "timestamp", "zone", "stress_score", "filename"])
    w.writeheader()
    w.writerows(rows)

print(f"\nWrote {len(rows)} rows to {OUT_CSV}")
if errors:
    print(f"{len(errors)} error(s):")
    for name, msg in errors:
        print(f"  {name}: {msg}")
