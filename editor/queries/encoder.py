"""SigLIP text/image encoders.

Lazy-loads `google/siglip-so400m-patch14-384`. Weights cache to
`indexes/_cache/hf/`. First call pulls ~3.5 GB; subsequent loads ~5s.
Per-query encode latency on CPU is ~50ms.

The HF cache env var is set BEFORE `transformers` is imported so the
download lands on the right disk.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Union

import numpy as np

from ._paths import hf_cache_dir

# Set HF cache env before any transformers import (yes, this is order-sensitive).
_HF_CACHE = str(hf_cache_dir())
os.environ.setdefault("HF_HOME", _HF_CACHE)
os.environ.setdefault("TRANSFORMERS_CACHE", str(Path(_HF_CACHE) / "transformers"))
os.environ.setdefault("HF_HUB_CACHE", str(Path(_HF_CACHE) / "hub"))

DEFAULT_MODEL_ID = "google/siglip-so400m-patch14-384"


@dataclass
class SigLIPEncoder:
    """Lazy-loaded SigLIP encoder.

    Usage:
        enc = SigLIPEncoder()                       # CPU default
        v = enc.encode_text("ranger station")       # (1152,) float32, L2-normalized
        v = enc.encode_image("/path/to/frame.jpg")  # same shape, same space

    Pass `device="cuda"` to run on GPU, or `device="auto"` to pick the best
    available device. Model weights are downloaded on first use.
    """

    model_id: str = DEFAULT_MODEL_ID
    device: str = "cpu"
    _device_resolved: bool = field(default=False, init=False, repr=False)

    def _resolve_device(self) -> None:
        if self._device_resolved:
            return
        if self.device == "auto":
            try:
                import torch  # noqa: F401

                self.device = "cuda" if torch.cuda.is_available() else "cpu"
            except Exception:
                self.device = "cpu"
        object.__setattr__(self, "_device_resolved", True)

    @cached_property
    def _model(self):
        self._resolve_device()
        from transformers import AutoModel

        m = AutoModel.from_pretrained(self.model_id)
        m.eval()
        m.to(self.device)
        return m

    @cached_property
    def _processor(self):
        from transformers import AutoProcessor

        return AutoProcessor.from_pretrained(self.model_id)

    def encode_text(self, text: str) -> np.ndarray:
        """Encode a text query into the SigLIP 1152-d L2-normalized vector space."""
        import torch

        self._resolve_device()
        inputs = self._processor(
            text=[text], padding="max_length", return_tensors="pt"
        ).to(self.device)
        with torch.no_grad():
            # NOTE: `get_text_features` now returns a
            # `BaseModelOutputWithPooling` dataclass in current transformers
            # (was a tensor in older versions). Indexing [0] grabs
            # `last_hidden_state` (shape (1, 64, 1152)) instead of the
            # batch's first element, producing a 73,728-element vector and a
            # cosine_topk dim mismatch downstream. SigLIP-so400m has no
            # text_projection layer (verified `hasattr(m, 'text_projection') is False`),
            # so `text_model(...).pooler_output` IS the canonical 1152-d
            # contrastive space — matches what the cached chunk-mean vectors
            # were built in. See indexes/_cache/clip_chunk_means.npy.
            feats = self._model.text_model(**inputs).pooler_output
        v = feats[0].detach().cpu().numpy().astype(np.float32)
        n = float(np.linalg.norm(v))
        return v / max(n, 1e-12)

    def encode_image(self, image_path: Union[str, Path]) -> np.ndarray:
        """Encode an image into the SigLIP 1152-d L2-normalized vector space."""
        import torch
        from PIL import Image

        self._resolve_device()
        img = Image.open(str(image_path)).convert("RGB")
        inputs = self._processor(images=img, return_tensors="pt").to(self.device)
        with torch.no_grad():
            # See encode_text() note: same dataclass-vs-tensor change applies
            # to `get_image_features`. `vision_model(...).pooler_output` is
            # the canonical 1152-d space (no visual_projection layer in
            # SigLIP-so400m), matching the cached chunk-mean vectors.
            feats = self._model.vision_model(**inputs).pooler_output
        v = feats[0].detach().cpu().numpy().astype(np.float32)
        n = float(np.linalg.norm(v))
        return v / max(n, 1e-12)
