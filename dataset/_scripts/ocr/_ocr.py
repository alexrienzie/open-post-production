"""Shared helpers for the OCR pipeline (INGEST.md candidate Phase M /
the OCR layer).

Two engines:
  - RapidOCR (rapidocr-onnxruntime): pure ONNX, runs on CPU, MIT licensed.
    Operates on BGR numpy arrays. Returns quad bboxes.
  - Apple Vision (pyobjc-framework-Vision): native macOS framework, uses
    Apple Neural Engine when available. Operates on CGImage from a file
    path or numpy bytes. Returns normalized [0,1] axis-aligned bboxes.

The pilot exercises both. After the engine + threshold decision, the full
pipeline uses the winner (or a fallback chain).
"""
from __future__ import annotations

import re
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Iterable

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import (  # noqa: E402
    OCR_DB, INDEXES_DIR, DERIVATIVE_MEDIA, RUNS_DIR,
)

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

FFMPEG = "/opt/homebrew/bin/ffmpeg"

# Numeric bib pattern: 2-4 digits standalone, optionally with leading zero
BIB_RE = re.compile(r"^\d{2,4}$")


# ---------------- engine: RapidOCR ----------------

_RAPIDOCR = None


def get_rapidocr():
    """Lazy-init singleton. ~50 MB model cached on first call."""
    global _RAPIDOCR
    if _RAPIDOCR is not None:
        return _RAPIDOCR
    from rapidocr_onnxruntime import RapidOCR
    _RAPIDOCR = RapidOCR()
    return _RAPIDOCR


def run_rapidocr(img_bgr: np.ndarray) -> list[dict]:
    """Run RapidOCR on a BGR numpy image. Returns [{bbox, text, confidence}].

    bbox is a 4-point polygon (list of [x, y] in absolute pixels). text is the
    UTF-8 string. confidence is 0..1.
    """
    ocr = get_rapidocr()
    result, _ = ocr(img_bgr)
    out = []
    for entry in result or []:
        # RapidOCR returns: [bbox (list of 4 [x,y]), text, score]
        bbox, text, score = entry
        out.append({
            "bbox": [[float(x), float(y)] for x, y in bbox],
            "text": str(text),
            "confidence": float(score),
            "engine": "rapidocr",
        })
    return out


# ---------------- engine: Apple Vision ----------------

def _load_cgimage_from_path(path: Path):
    """Use CoreGraphics to load any macOS-readable image format from a file path."""
    import Quartz
    from Foundation import NSURL
    url = NSURL.fileURLWithPath_(str(path))
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    if src is None:
        return None
    return Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)


def _cgimage_from_bgr(img_bgr: np.ndarray):
    """Convert BGR numpy array -> CGImage via in-memory PNG round-trip.
    Slower than path-based loading; prefer _load_cgimage_from_path when the
    image is on disk."""
    import io
    import cv2
    import Quartz
    from Foundation import NSData
    ok, buf = cv2.imencode(".png", img_bgr)
    if not ok:
        return None
    data = NSData.dataWithBytes_length_(bytes(buf), len(buf))
    src = Quartz.CGImageSourceCreateWithData(data, None)
    if src is None:
        return None
    return Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)


def run_apple_vision(*, path: Path | None = None,
                     img_bgr: np.ndarray | None = None,
                     recognition_level: str = "accurate",
                     languages: list[str] | None = None) -> list[dict]:
    """Run Apple Vision text recognition. Prefer `path=` when the image is on
    disk (no numpy round-trip). Pass `img_bgr=` for in-memory frames extracted
    via ffmpeg.

    bbox returned in NORMALIZED [0,1] axis-aligned form (x, y, w, h) with
    origin at BOTTOM-LEFT (CoreGraphics convention). Convert to absolute pixels
    + top-left origin downstream if needed.
    """
    import Vision
    if path is not None:
        cg = _load_cgimage_from_path(Path(path))
    elif img_bgr is not None:
        cg = _cgimage_from_bgr(img_bgr)
    else:
        raise ValueError("either path= or img_bgr= must be provided")
    if cg is None:
        return []
    req = Vision.VNRecognizeTextRequest.alloc().init()
    if recognition_level == "fast":
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelFast)
    else:
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    if languages:
        req.setRecognitionLanguages_(languages)
    req.setUsesLanguageCorrection_(True)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
    handler.performRequests_error_([req], None)
    out = []
    for obs in (req.results() or []):
        bbox = obs.boundingBox()  # CGRect: origin (x,y) + size (w,h), all normalized
        x, y = float(bbox.origin.x), float(bbox.origin.y)
        w, h = float(bbox.size.width), float(bbox.size.height)
        top = obs.topCandidates_(1)
        if not top:
            continue
        cand = top[0]
        out.append({
            "bbox_norm": [x, y, w, h],
            "text": str(cand.string()),
            "confidence": float(cand.confidence()),
            "engine": "apple_vision",
        })
    return out


def normalized_bbox_to_pixels(bbox_norm: list[float], img_w: int, img_h: int) -> list[list[float]]:
    """Convert Apple Vision normalized (bottom-left origin) bbox to a 4-point
    polygon in absolute pixels with TOP-LEFT origin (matches RapidOCR/cv2)."""
    x, y, w, h = bbox_norm
    px1 = x * img_w
    py1 = (1.0 - y - h) * img_h
    px2 = (x + w) * img_w
    py2 = (1.0 - y) * img_h
    return [[px1, py1], [px2, py1], [px2, py2], [px1, py2]]


# ---------------- frame extraction ----------------

def extract_frame_at(proxy_path: Path, timestamp_sec: float,
                     timeout_sec: int = 30) -> np.ndarray | None:
    """Seek into a proxy via ffmpeg and decode a single frame as BGR numpy.
    Mirrors faces._faces.extract_frame_at — copied here so the ocr/ subdir is
    independent of the faces/ subdir."""
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", f"{timestamp_sec:.3f}",
        "-i", str(proxy_path),
        "-frames:v", "1",
        "-f", "image2pipe", "-vcodec", "mjpeg", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    import cv2
    arr = np.frombuffer(proc.stdout, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


# ---------------- post-filters ----------------

def is_bib_text(text: str) -> bool:
    """True if `text` looks like a race bib (2-4 digits, no other chars)."""
    return bool(BIB_RE.match(text.strip()))
