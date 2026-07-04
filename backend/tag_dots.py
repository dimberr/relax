"""Interactive dot-tagging tool for ground-truth collection.

Shows each annotated screenshot. Click missed dots to tag them.
Results saved to ../tagged_dots.json.

Controls:
  left-click   — add tag at cursor
  right-click  — remove nearest tag (within 20px)
  s            — save tags for this image and advance
  b            — go back to previous image
  r            — reset tags for this image
  q            — quit and save all
"""
import json
import sys
from pathlib import Path

import cv2
import numpy as np

ANNOTATED_DIR = Path(__file__).parents[1] / "annotated"
OUT_FILE = Path(__file__).parents[1] / "tagged_dots.json"

files = sorted(ANNOTATED_DIR.glob("*.png"))
if not files:
    print("No annotated images found. Run batch_extract.py first.")
    sys.exit(1)

# Load existing tags if resuming
all_tags: dict[str, list] = {}
if OUT_FILE.exists():
    all_tags = json.loads(OUT_FILE.read_text())
    print(f"Resuming — {len(all_tags)} images already tagged.")

# Skip already-tagged images unless --all passed
if "--all" not in sys.argv:
    files = [f for f in files if f.name not in all_tags]
    print(f"{len(files)} images left to tag. Pass --all to re-tag everything.")

if not files:
    print("All images already tagged.")
    sys.exit(0)

idx = 0
tags: list[tuple[int, int]] = []
img: np.ndarray | None = None
display: np.ndarray | None = None


def redraw():
    global display
    display = img.copy()
    for (x, y) in tags:
        cv2.circle(display, (x, y), 8, (0, 255, 255), 2)   # cyan = manual tag
        cv2.circle(display, (x, y), 2, (0, 255, 255), -1)
    fname = files[idx].name
    label = f"[{idx+1}/{len(files)}] {fname}  tags={len(tags)}  (s=save/next  b=back  r=reset  q=quit)"
    cv2.putText(display, label, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 1)
    cv2.imshow("tag_dots", display)


def load_image():
    global img, tags
    img = cv2.imread(str(files[idx]))
    if img is None:
        # Corrupt/unreadable file: show a placeholder so the display matches
        # the current filename and clicks/redraw stay crash-safe (img is never None).
        print(f"Unreadable image: {files[idx].name}")
        img = np.zeros((600, 900, 3), dtype=np.uint8)
        cv2.putText(img, f"UNREADABLE: {files[idx].name}", (10, 300),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        tags = []
        redraw()
        return
    fname = files[idx].name
    tags = list(map(tuple, all_tags.get(fname, [])))  # resume if re-visiting
    redraw()


def save_current():
    all_tags[files[idx].name] = tags
    OUT_FILE.write_text(json.dumps(all_tags, indent=2))


def on_mouse(event, x, y, flags, _param):
    global tags
    if event == cv2.EVENT_LBUTTONDOWN:
        tags.append((x, y))
        redraw()
    elif event == cv2.EVENT_RBUTTONDOWN:
        if tags:
            nearest = min(range(len(tags)), key=lambda i: (tags[i][0]-x)**2 + (tags[i][1]-y)**2)
            if abs(tags[nearest][0]-x) <= 20 and abs(tags[nearest][1]-y) <= 20:
                tags.pop(nearest)
                redraw()


cv2.namedWindow("tag_dots", cv2.WINDOW_NORMAL)
cv2.resizeWindow("tag_dots", 900, 600)
cv2.setMouseCallback("tag_dots", on_mouse)
load_image()

while True:
    key = cv2.waitKey(20) & 0xFF
    if key == ord("s"):
        save_current()
        print(f"  Saved {len(tags)} tags for {files[idx].name}")
        idx = min(idx + 1, len(files) - 1)
        load_image()
    elif key == ord("b"):
        save_current()
        idx = max(idx - 1, 0)
        load_image()
    elif key == ord("r"):
        tags = []
        redraw()
    elif key == ord("q"):
        save_current()
        break

cv2.destroyAllWindows()
print(f"\nDone. Tags saved to {OUT_FILE}")
print(f"Total tagged images: {len(all_tags)}")
