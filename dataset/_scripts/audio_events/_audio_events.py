"""Shared helpers for the audio_events layer (CLAP — Contrastive Language-Audio
Pretraining). Provides the project vocabulary, dual-engine loaders (LAION-CLAP +
MS-CLAP), and WAV-window decoding via ffmpeg.

Mirrors the dual-engine pattern from `dataset/_scripts/ocr/_ocr.py`. Each
engine loads once per process and is reused across many audio queries.

Project vocabulary is film-specific. It mixes:
  - outdoor / weather (wind, rain, water)
  - race / event (crowd, applause, announcer, starting horn)
  - movement / human (footsteps, breathing, trekking poles)
  - voice (talking, laughter, shouting, single voice)
  - vehicles (car, truck, motorcycle, helicopter, drone, plane)
  - music (music, guitar, singing)
  - environment (silence, room tone, traffic, indoor ambient)

CLAP scores any English phrase against the audio; the vocab can grow without a
schema change. After the pilot the user picks a final set and a confidence
threshold.
"""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path
from typing import Iterable

import numpy as np

FFMPEG = "/opt/homebrew/bin/ffmpeg"

# Sample rate CLAP models expect natively.
LAION_SR = 48_000
MS_SR = 44_100

# Default analysis window (seconds). CLAP was trained on ~10-sec clips; this
# matches that horizon and gives stable scores. For full-corpus runs we slide
# the window over each WAV; for the pilot we take a single 10-sec window
# centered on the asset midpoint.
WINDOW_SEC = 10.0


# Project vocabulary — flat list. Grouped into themes for the pilot report.
VOCAB: dict[str, list[str]] = {
    "outdoor": [
        "wind", "strong wind", "rain", "thunder",
        "running water", "river or stream", "ocean waves",
        "birds singing", "crickets", "leaves rustling",
        "snow underfoot",
    ],
    "race_event": [
        # Removed after pilot: "race announcer over PA system" was
        # firing on 67% of MS-CLAP clips (default label, not a real signal).
        "crowd cheering", "applause", "starting horn or air horn",
        "whistle blowing", "cowbell", "bib timing beep",
        "loud amplified voice or megaphone",
    ],
    "movement": [
        "footsteps on trail", "footsteps on pavement",
        "running footsteps", "trekking poles clicking on rock",
        "heavy breathing or panting", "boots on snow",
        "ski edges on snow", "bike chain or gears",
    ],
    "voice": [
        # Removed after pilot: "phone or video call voice" was
        # firing on interview-room audio (compressed mid-range), false positive.
        "one person talking calmly", "multiple people talking",
        "laughter", "shouting or yelling",
        "child or kid voice", "crying",
        "clean studio voice or narration",
    ],
    "vehicle": [
        "car engine", "truck engine", "motorcycle",
        "helicopter rotor", "small airplane engine",
        "drone propeller hum", "bicycle",
    ],
    "music": [
        # Removed after layer-QA pass: "acoustic guitar" fired 149×
        # at score 0.30-0.41 (all MS-CLAP, all near-threshold), almost
        # exclusively on interview-classified assets (Ed Interview, Jaz,
        # several quiet home/interview assets) where there's no acoustic guitar
        # actually playing. False-positive pattern; re-add if a project
        # legitimately captures acoustic-guitar performance.
        "music playing", "piano",
        "drums", "singing voice", "background score",
    ],
    "environment": [
        "silence or room tone", "indoor ambient",
        "traffic noise", "construction or hammering",
        "kitchen sounds",
    ],
    "media_artifact": [
        # Removed after pilot: "podcast intro music" was firing on
        # 57% of MS-CLAP clips, used as a default-confidence label.
        "phone notification ding", "camera shutter click",
        "video glitch or zip noise", "microphone handling noise",
    ],
}


def all_tags() -> list[str]:
    """Flat list of all tags across all themes (stable order)."""
    out: list[str] = []
    for theme in sorted(VOCAB.keys()):
        out.extend(VOCAB[theme])
    return out


def tag_to_theme() -> dict[str, str]:
    """Reverse map tag → theme for grouping report output."""
    out = {}
    for theme, tags in VOCAB.items():
        for t in tags:
            out[t] = theme
    return out


# --------------------------------------------------------------- audio loading


def decode_window(
    wav_path: Path, start_sec: float, dur_sec: float, sample_rate: int
) -> np.ndarray | None:
    """Decode a single window from `wav_path` via ffmpeg to mono float32 at
    `sample_rate`. Returns 1-D float32 array of length `int(dur_sec * sample_rate)`
    or None on error. Pads with zeros if the source is shorter than the window.
    """
    try:
        proc = subprocess.run(
            [
                FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
                "-ss", f"{max(0.0, start_sec):.3f}",
                "-i", str(wav_path),
                "-t", f"{dur_sec:.3f}",
                "-ac", "1",
                "-ar", str(sample_rate),
                "-f", "f32le", "-",
            ],
            capture_output=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    samples = np.frombuffer(proc.stdout, dtype=np.float32).copy()
    want = int(round(dur_sec * sample_rate))
    if samples.size < want:
        pad = np.zeros(want - samples.size, dtype=np.float32)
        samples = np.concatenate([samples, pad])
    elif samples.size > want:
        samples = samples[:want]
    return samples


def asset_duration_sec(wav_path: Path) -> float | None:
    """Cheap ffprobe duration lookup."""
    try:
        proc = subprocess.run(
            [
                "/opt/homebrew/bin/ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(wav_path),
            ],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return float(proc.stdout.strip())
    except Exception:
        pass
    return None


# --------------------------------------------------------------- engine loaders


class LaionClapEngine:
    """LAION-CLAP wrapper. First call downloads the 630k-best.pt checkpoint
    (~2 GB) to the HuggingFace cache."""

    name = "laion_clap"
    sample_rate = LAION_SR

    def __init__(self) -> None:
        import laion_clap  # local import — heavy
        self.model = laion_clap.CLAP_Module(enable_fusion=False)
        # load_ckpt() with no arg pulls the default 630k-best checkpoint
        self.model.load_ckpt()
        self._text_cache: dict[tuple[str, ...], np.ndarray] = {}

    def embed_audio(self, samples: np.ndarray) -> np.ndarray:
        """samples: 1-D float32 at self.sample_rate. Returns (1, 512) L2-norm vec."""
        # LAION-CLAP expects shape (1, N) and float32
        x = samples.reshape(1, -1).astype(np.float32)
        emb = self.model.get_audio_embedding_from_data(x=x, use_tensor=False)
        # L2-normalize defensively
        n = np.linalg.norm(emb, axis=1, keepdims=True)
        return emb / np.where(n == 0, 1.0, n)

    def embed_text(self, tags: list[str]) -> np.ndarray:
        key = tuple(tags)
        if key in self._text_cache:
            return self._text_cache[key]
        emb = self.model.get_text_embedding(tags, use_tensor=False)
        n = np.linalg.norm(emb, axis=1, keepdims=True)
        emb = emb / np.where(n == 0, 1.0, n)
        self._text_cache[key] = emb
        return emb


class MsClapEngine:
    """Microsoft CLAP-2023 wrapper. First call downloads the model
    (~340 MB)."""

    name = "ms_clap"
    sample_rate = MS_SR

    def __init__(self) -> None:
        from msclap import CLAP
        self.model = CLAP(version="2023", use_cuda=False)
        self._text_cache: dict[tuple[str, ...], np.ndarray] = {}

    def embed_audio(self, samples: np.ndarray) -> np.ndarray:
        """samples: 1-D float32 at self.sample_rate. Returns (1, D) L2-norm vec."""
        # MS-CLAP API takes file paths. We write a temp WAV using soundfile.
        import soundfile as sf
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            tmp_path = tf.name
        try:
            sf.write(tmp_path, samples, self.sample_rate, subtype="FLOAT")
            emb = self.model.get_audio_embeddings([tmp_path]).detach().cpu().numpy()
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        n = np.linalg.norm(emb, axis=1, keepdims=True)
        return emb / np.where(n == 0, 1.0, n)

    def embed_text(self, tags: list[str]) -> np.ndarray:
        key = tuple(tags)
        if key in self._text_cache:
            return self._text_cache[key]
        emb = self.model.get_text_embeddings(tags).detach().cpu().numpy()
        n = np.linalg.norm(emb, axis=1, keepdims=True)
        emb = emb / np.where(n == 0, 1.0, n)
        self._text_cache[key] = emb
        return emb


def load_engine(name: str):
    """Factory. `name` ∈ {'laion_clap', 'ms_clap'}."""
    if name == "laion_clap":
        return LaionClapEngine()
    if name == "ms_clap":
        return MsClapEngine()
    raise ValueError(f"unknown engine: {name}")


# --------------------------------------------------------------- scoring


def score_tags(engine, samples: np.ndarray, tags: list[str]) -> list[tuple[str, float]]:
    """Return (tag, cosine_sim) pairs sorted by sim descending."""
    audio_emb = engine.embed_audio(samples)  # (1, D)
    text_emb = engine.embed_text(tags)       # (N, D)
    sims = (audio_emb @ text_emb.T).flatten()
    out = list(zip(tags, sims.tolist()))
    out.sort(key=lambda kv: kv[1], reverse=True)
    return out
