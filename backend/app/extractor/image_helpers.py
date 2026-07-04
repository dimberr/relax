"""Image and time-series helper functions for stress chart extraction."""

from datetime import timedelta

import cv2
import numpy as np

# Mask parameters (iPhone only)
_MASK_THRESHOLD = 50
_DILATE_KERNEL_SIZE = 5
_DILATE_ITERATIONS = 2


def load_precise_mask(mask_path, target_shape):
    """Load and process the mask_scaled.png file."""
    mask_img = cv2.imread(mask_path)
    if mask_img is None:
        raise FileNotFoundError(f"Could not load mask: {mask_path}")

    mask_img = cv2.resize(mask_img, (target_shape[1], target_shape[0]))
    mask_gray = cv2.cvtColor(mask_img, cv2.COLOR_BGR2GRAY)
    _, mask_binary = cv2.threshold(mask_gray, _MASK_THRESHOLD, 255, cv2.THRESH_BINARY)

    mask_text = cv2.bitwise_not(mask_binary)
    kernel = np.ones((_DILATE_KERNEL_SIZE, _DILATE_KERNEL_SIZE), np.uint8)
    mask_text_dilated = cv2.dilate(mask_text, kernel, iterations=_DILATE_ITERATIONS)
    return cv2.bitwise_not(mask_text_dilated)


def preprocess_array(screenshot, mask_path, profile):
    """Apply mask (if any), crop to chart area, and preprocess.

    mask_path=None skips masking (used for Pixel 9 which has no bundled mask).
    profile supplies y_min/y_max crop bounds and device-specific HoughCircles
    parameters so the blur step is shared with detect_dots.
    """
    if mask_path is not None:
        mask = load_precise_mask(mask_path, screenshot.shape)
        masked = cv2.bitwise_and(screenshot, screenshot, mask=mask)
    else:
        mask = None
        masked = screenshot

    cropped = masked[profile.y_min:profile.y_max, :]
    gray = cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (3, 3), 0)

    return screenshot, masked, mask, blur


def load_and_preprocess(image_path, mask_path, profile):
    """Load image from disk, apply mask, crop, preprocess."""
    screenshot = cv2.imread(image_path)
    if screenshot is None:
        raise FileNotFoundError(f"Could not load image: {image_path}")

    return preprocess_array(screenshot, mask_path, profile)


def detect_dots(blur, profile):
    """Detect circular dots using HoughCircles with profile-specific radii."""
    circles = cv2.HoughCircles(
        blur,
        cv2.HOUGH_GRADIENT,
        dp=1.0,
        minDist=profile.hough_min_dist,
        param1=40,
        param2=10,
        minRadius=profile.hough_min_radius,
        maxRadius=profile.hough_max_radius,
    )

    if circles is None:
        raise RuntimeError("No dots detected")

    circles = np.round(circles[0, :]).astype("int")
    circles = sorted(circles, key=lambda c: c[0])
    return circles


def zone_for_y(y, profile):
    """Return zone name for a given y-coordinate using the device profile's zone map."""
    for zone, (ymin, ymax) in profile.zones.items():
        if ymin <= y <= ymax:
            return zone
    return "unknown"


def detect_gaps(df, expected_interval_minutes=15):
    """Detect gaps in the time series data."""
    gaps = []
    for i in range(len(df) - 1):
        current_time = df.iloc[i]["timestamp"]
        next_time = df.iloc[i + 1]["timestamp"]
        time_diff = (next_time - current_time).total_seconds() / 60

        if time_diff > expected_interval_minutes * 1.5:
            gaps.append(
                {
                    "after": current_time,
                    "before": next_time,
                    "gap_minutes": time_diff,
                    "expected_minutes": expected_interval_minutes,
                    "missing_points": int(time_diff / expected_interval_minutes) - 1,
                }
            )

    return gaps
