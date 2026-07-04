"""API routes for the stress extractor web app."""
import asyncio
import base64
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import cv2
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool

from . import ratelimit, requestlog
from .extractor.core import (
    ExtractionError,
    decode_image,
    extract_from_array,
    validate_dimensions,
)

logger = logging.getLogger("oura_extractor")

router = APIRouter(prefix="/api")

MASK_PATH = str(Path(__file__).parent / "extractor" / "mask_scaled.png")
# Oura screenshots are 250–500 KB (640×1136 iPhone, 864×1939 Pixel 9); 1 MB is
# generous headroom. Caddy enforces the same cap at the edge so oversized bodies
# never reach Python.
MAX_UPLOAD_BYTES = 1 * 1024 * 1024

# OCR is CPU-bound and synchronous; cap how many run at once so a burst of
# uploads can't pin every core (the public box has no auth in front of it).
_MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_EXTRACTIONS", "2"))
_extract_semaphore = asyncio.Semaphore(_MAX_CONCURRENT)

# Bound the waiting room too: every queued request holds its uploaded bytes and
# a connection in memory, so an unbounded queue turns CPU pressure into an OOM.
# Allow a few to wait for a free slot; reject the rest fast with 503 rather than
# letting the queue grow without limit. asyncio is single-threaded, so the
# check-then-increment below is atomic as long as no `await` sits between them.
_MAX_INFLIGHT = int(os.environ.get("MAX_INFLIGHT_EXTRACTIONS", "6"))
_inflight = 0  # running + waiting

# Safety-net timeout for a single extraction. Dimension validation already bounds
# the work, so this only fires on a pathological hang. Note: the worker thread
# can't be force-killed, so on timeout we free the client and the slot and let
# the orphaned thread finish on its own.
_EXTRACT_TIMEOUT = float(os.environ.get("EXTRACT_TIMEOUT_SECONDS", "30"))


def _run_pipeline(raw: bytes, reference_date, include_image: bool) -> dict:
    """Synchronous decode → extract → encode, run off the event loop.

    Lets ExtractionError (and anything unexpected) propagate to the caller for
    HTTP mapping; does no error handling of its own.
    """
    img = decode_image(raw)
    validate_dimensions(img)
    result = extract_from_array(img, MASK_PATH, reference_date)

    annotated = result.pop("annotated")
    if include_image:
        ok, buf = cv2.imencode(".png", annotated)
        if ok:
            result["annotated_png"] = base64.b64encode(buf).decode("ascii")
    return result


@router.get("/health")
def health():
    return {"status": "ok"}


@router.post("/extract")
async def extract(
    request: Request,
    file: UploadFile = File(...),
    date: str = Form(...),
    include_image: bool = Form(False),
):
    """Extract stress points from an uploaded Oura screenshot.

    Form fields:
        file:           the screenshot (PNG/JPEG, 640x1136).
        date:           the day the chart represents, YYYY-MM-DD.
        include_image:  if true, return the annotated chart as a base64 PNG.
    """
    start = time.monotonic()
    # Caddy is the only thing that reaches the backend, and it sets X-Real-IP from
    # the verified client IP, so we trust that header here (used for the per-IP
    # quota and the hashed request log).
    ip = request.headers.get("X-Real-IP") or (
        request.client.host if request.client else "unknown"
    )

    async def _log(status: int, error: str | None = None) -> None:
        """Record this request's outcome. Never lets a logging failure break the
        request path — the durable log is best-effort."""
        if not requestlog.enabled():
            return
        ms = int((time.monotonic() - start) * 1000)
        try:
            await run_in_threadpool(requestlog.log, ip, ms, status, error)
        except Exception:  # noqa: BLE001 - logging must never break the request
            logger.exception("Request logging failed")

    try:
        try:
            reference_date = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")

        raw = await file.read(MAX_UPLOAD_BYTES + 1)
        if not raw:
            raise HTTPException(status_code=422, detail="Empty upload")
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Image too large (max 1 MB)")

        # Per-IP daily quota.
        if ratelimit.enabled():
            used = await run_in_threadpool(ratelimit.count_recent, ip)
            if used >= ratelimit.max_hits():
                retry = await run_in_threadpool(ratelimit.seconds_until_reset, ip)
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"Daily limit of {ratelimit.max_hits()} extractions reached "
                        f"for your IP. Run it locally instead — "
                        f"https://github.com/cyanobac/relax — or try "
                        f"again tomorrow."
                    ),
                    headers={"Retry-After": str(retry)},
                )

        # Shed load before committing memory: if the running+waiting count is
        # already at the limit, reject fast instead of growing the queue. (Atomic
        # in asyncio: no `await` between the check and the increment.)
        global _inflight
        if _inflight >= _MAX_INFLIGHT:
            raise HTTPException(
                status_code=503,
                detail="Server busy, please try again in a minute.",
                headers={"Retry-After": "60"},
            )
        _inflight += 1
        try:
            # Run the heavy, blocking OCR pipeline in a worker thread so it doesn't
            # freeze the event loop, and gate concurrency so a flood can't exhaust
            # CPU.
            async with _extract_semaphore:
                try:
                    result = await asyncio.wait_for(
                        run_in_threadpool(
                            _run_pipeline, raw, reference_date, include_image
                        ),
                        timeout=_EXTRACT_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Extraction timed out after %ss", _EXTRACT_TIMEOUT)
                    raise HTTPException(
                        status_code=504,
                        detail="Extraction timed out. Please try again.",
                    )
                except ExtractionError as e:
                    # Expected, user-facing problems (bad size, undecodable, no dots).
                    raise HTTPException(status_code=422, detail=str(e))
                except Exception:  # noqa: BLE001 - surface anything unexpected as 500
                    # Log full detail server-side; never leak internals to client.
                    logger.exception("Extraction failed")
                    raise HTTPException(
                        status_code=500,
                        detail="Extraction failed. Please try again.",
                    )

            # Only successful extractions count against the daily quota.
            if ratelimit.enabled():
                await run_in_threadpool(ratelimit.record, ip)
        finally:
            _inflight -= 1

        await _log(200)
        return result
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, str) else None
        await _log(e.status_code, detail)
        raise
