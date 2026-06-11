"""Shared helpers for the chromaprint audio-fingerprint layer.

`fpcalc` (chromaprint CLI, Homebrew) does the actual fingerprint compute. We
shell out, parse `-raw` decimal hash output → numpy uint32 array, persist, and
later compare arrays via vectorized bit-level Hamming similarity.

Design notes:

- Bit-level (Hamming) similarity is the standard chromaprint match measure.
  Random noise sits around 0.5 (32 bits independent → 16 bit collisions).
  Real-world same-source recordings hit 0.7+ once aligned.
- We slide the shorter fingerprint across the longer one at every frame offset
  and report the best similarity + offset.
- Popcount is implemented via an 8-bit byte LUT (numpy 1.x doesn't have
  bitwise_count). ~10× faster than per-element bin().count('1').
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

FPCALC = "/opt/homebrew/bin/fpcalc"

# How much of each WAV to fingerprint. 0 = entire file. For 60-sec we get
# ~470 hashes (chromaprint's ~7.8 Hz). Enough to identify a recording even on
# long bag-recordings.
DEFAULT_LENGTH_SEC = 0  # full file


# 8-bit popcount LUT (256 bytes).
_POPCOUNT_LUT = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def fingerprint_wav(wav_path: Path, length_sec: int = DEFAULT_LENGTH_SEC,
                    timeout_sec: int = 90) -> tuple[float, np.ndarray] | None:
    """Compute chromaprint fingerprint via fpcalc -raw.

    Returns (duration_sec, hashes_uint32) or None on error. Hashes are
    chromaprint's per-frame 32-bit features at ~7.8 Hz."""
    cmd = [FPCALC, "-raw"]
    if length_sec and length_sec > 0:
        cmd += ["-length", str(length_sec)]
    cmd.append(str(wav_path))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    dur = 0.0
    hashes: list[int] | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("DURATION="):
            try:
                dur = float(line.split("=", 1)[1])
            except ValueError:
                pass
        elif line.startswith("FINGERPRINT="):
            raw = line.split("=", 1)[1].strip()
            try:
                # int32 may be NEGATIVE in fpcalc's signed-decimal output;
                # convert to unsigned uint32 for bit ops.
                hashes = [int(x) & 0xFFFFFFFF for x in raw.split(",") if x]
            except ValueError:
                return None
    if hashes is None or not hashes:
        return None
    return dur, np.asarray(hashes, dtype=np.uint32)


def pack_fp(arr: np.ndarray) -> bytes:
    """Serialize uint32 array → bytes for SQLite BLOB."""
    return arr.astype(np.uint32, copy=False).tobytes()


def unpack_fp(blob: bytes) -> np.ndarray:
    """SQLite BLOB → uint32 array."""
    return np.frombuffer(blob, dtype=np.uint32)


def _popcount_u32(arr_u32: np.ndarray) -> np.ndarray:
    """Per-element popcount on a uint32 array. Returns int array of same length."""
    bytes_view = arr_u32.view(np.uint8).reshape(-1, 4)
    return _POPCOUNT_LUT[bytes_view].sum(axis=1)


def match_fingerprints(
    fp_short: np.ndarray, fp_long: np.ndarray,
    max_slide: int | None = None,
) -> tuple[float, int]:
    """Slide `fp_short` across `fp_long`, returning (best_bit_similarity,
    best_offset_frames). Offset is in chromaprint frames (~128 ms each).

    Similarity = 1 - mean_popcount / 32. Range [0..1], ≈0.5 = random,
    ≥0.7 = real source overlap, ≥0.85 = aligned same recording.

    `max_slide` caps how far we search (useful for long recordings — if you
    know the recording start is within ±N frames of zero, set max_slide = N).
    """
    if len(fp_short) > len(fp_long):
        # Auto-swap so we always slide the smaller across the larger
        s, o = match_fingerprints(fp_long, fp_short, max_slide=max_slide)
        return s, -o
    n = len(fp_short)
    m = len(fp_long)
    max_off = m - n + 1
    if max_slide is not None and max_slide > 0:
        max_off = min(max_off, max_slide)
    best_sim = -1.0
    best_off = 0
    if n == 0 or max_off <= 0:
        return 0.0, 0
    for off in range(max_off):
        xor = fp_short ^ fp_long[off:off + n]
        popcnt = int(_popcount_u32(xor).sum())
        sim = 1.0 - popcnt / (32.0 * n)
        if sim > best_sim:
            best_sim = sim
            best_off = off
    return best_sim, best_off


def quick_prefilter_score(fp_a: np.ndarray, fp_b: np.ndarray,
                          n_probe: int = 16) -> float:
    """Cheap exact-equality score on the FIRST n_probe hashes of each (after
    a small sliding window) to filter pairs before the full slide. Avoids
    O(n*m) on obviously-unrelated pairs.

    Returns max equality fraction across a ±4-frame slide.
    """
    a = fp_a[:n_probe]
    if len(a) < n_probe:
        return 0.0
    best = 0.0
    for off in range(-4, 5):
        if off < 0:
            b = fp_b[: n_probe + off]
            aslice = a[-len(b):]
        else:
            b = fp_b[off: off + n_probe]
            aslice = a[: len(b)]
        if len(aslice) == 0 or len(b) == 0:
            continue
        eq = float(np.mean(aslice == b))
        if eq > best:
            best = eq
    return best
