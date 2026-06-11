"""Shared helpers for the dense_captions layer.

Three caption engines wrapped behind a uniform interface:
  - MiniCPM-V 2.6  (local, 8B, MPS, ~16 GB first-run download)
  - Gemini 2.5 Flash (API, cheap, ~$3 for full corpus)
  - Gemini 2.5 Pro   (API, gold standard, ~$50-65 for full corpus)

Also:
  - extract_frame_jpeg(): ffmpeg seek + scale → JPEG bytes for the model
  - build_meta_block(): per-shot metadata enrichment block (Gemini chunk, faces,
    OCR nearby, audio events nearby, transcript nearby) with token caps
  - PROMPT_BODY / PROMPT_PROJECT: project + task strings (single source of truth)
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

FFMPEG = "/opt/homebrew/bin/ffmpeg"

# Frame sizing — matches the OCR / shot_quality ingest scale to keep IO predictable
SAMPLE_FRAME_WIDTH = 512


# --------------------------------------------------------------- prompt

# Fill in for your film. The bucket taxonomy is the load-bearing part — it stops
# the captioner from blending unrelated domains in one description. (Ours read:
# "a documentary interweaving ultrarunning in a mountain range with a
# legal-advocacy storyline … (a) outdoor running / mountain content, (b) legal /
# courtroom / interview-room content, (c) behind-the-scenes preparation.")
PROMPT_PROJECT = (
    "<YOUR FILM> is a documentary about <subject / domain>. Scenes generally "
    "fall into one of <N> buckets and rarely combine: (a) <bucket>, "
    "(b) <bucket>, (c) <bucket>. When describing a frame, lean into whichever "
    "bucket fits — don't blend categories that aren't actually in the image."
)

PROMPT_TASK_INSTRUCTION = (
    "Describe THIS specific frame. If a context block is provided above, it is "
    "BACKGROUND — your caption must reflect what is visually IN THIS SPECIFIC "
    "FRAME. If the chunk summary says one thing but you see another, describe "
    "what you see.\n"
    "\n"
    "Output JSON only (no commentary): "
    "{\"subject\": str, \"action\": str, \"setting\": str, "
    "\"framing\": one of (extreme-close-up, close-up, medium, wide, extreme-wide, aerial), "
    "\"mood\": str, "
    "\"editorial_hooks\": str /* 1-2 specific cut points like 'looks at watch' */}"
)


def build_prompt(meta_block: str | None = None) -> str:
    """Assemble the full prompt. meta_block is None → no-meta variant."""
    parts = [f"PROJECT: {PROMPT_PROJECT}"]
    if meta_block:
        parts.append("")
        parts.append(meta_block)
    parts.append("")
    parts.append(f"TASK: {PROMPT_TASK_INSTRUCTION}")
    return "\n".join(parts)


def build_meta_block(
    *,
    shoot_label: str | None,
    asset_type: str | None,
    camera_id: str | None,
    chunk_subject: str | None,
    chunk_action: str | None,
    people_in_frame: list[str] | None,
    ocr_phrases_nearby: list[str] | None,
    audio_events_nearby: list[tuple[str, float]] | None,
    transcript_snippet: str | None,
    shot_idx: int | None,
    n_shots_in_asset: int | None,
) -> str:
    """Render the per-shot metadata enrichment block. Each field is capped to
    keep total ~265 tokens so image stays the dominant signal (~258 tokens)."""

    def _trim(s: str | None, max_chars: int) -> str:
        if not s:
            return ""
        s = " ".join(s.split())[:max_chars]
        return s

    lines = []
    asset_bits = []
    if shoot_label:
        asset_bits.append(f"shoot={shoot_label}")
    if asset_type:
        asset_bits.append(f"asset_type={asset_type}")
    if camera_id:
        asset_bits.append(f"camera={camera_id}")
    if asset_bits:
        lines.append(f"ASSET: {', '.join(asset_bits)}")

    subj = _trim(chunk_subject, 200)
    act = _trim(chunk_action, 200)
    if subj or act:
        lines.append("CHUNK SUMMARY (Gemini Pro, F-pass):")
        if subj:
            lines.append(f'  subject: "{subj}"')
        if act:
            lines.append(f'  action:  "{act}"')

    people = (people_in_frame or [])[:5]
    if people:
        lines.append(f"PEOPLE LIKELY IN FRAME (face cluster, K.1): {', '.join(people)}")
    else:
        lines.append("PEOPLE LIKELY IN FRAME: (no recognized face)")

    ocr = ", ".join((ocr_phrases_nearby or [])[:5])
    lines.append(f"TEXT VISIBLE NEARBY (K.3 OCR, ±2 sec): {ocr or '(none)'}")

    ae = audio_events_nearby or []
    ae_disp = ", ".join(f"{t} ({s:.2f})" for t, s in ae[:3])
    lines.append(f"AUDIO EVENTS NEARBY (K.7 CLAP, ±5 sec): {ae_disp or '(none)'}")

    tr = _trim(transcript_snippet, 200)
    lines.append(f"TRANSCRIPT NEARBY (±5 sec): {tr or '(no speech)'}")

    if shot_idx is not None and n_shots_in_asset is not None:
        lines.append(f"(this frame is one of three samples within shot {shot_idx} of {n_shots_in_asset} in the asset)")

    return "\n".join(lines)


# --------------------------------------------------------------- frame extraction

def extract_frame_jpeg(proxy_path: Path, t_sec: float, width: int = SAMPLE_FRAME_WIDTH,
                       quality: int = 4) -> bytes | None:
    """ffmpeg seek + decode → JPEG bytes. ~5-20 KB at width=512."""
    try:
        proc = subprocess.run(
            [
                FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
                "-ss", f"{max(0.0, t_sec):.2f}",
                "-i", str(proxy_path),
                "-frames:v", "1",
                "-vf", f"scale={width}:-1",
                "-q:v", str(quality),
                "-f", "image2pipe", "-c:v", "mjpeg", "-",
            ],
            capture_output=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    return proc.stdout


# --------------------------------------------------------------- engines

class CaptionEngine:
    name = "base"
    def caption(self, image_bytes: bytes, prompt: str) -> dict | None:
        """Returns {'text': str, 'json': dict|None, 'latency_sec': float}."""
        raise NotImplementedError


def _parse_json_caption(text: str) -> tuple[str, dict | None]:
    """Best-effort JSON extraction from model output. Returns (flat_text, parsed_json|None)."""
    if not text:
        return "", None
    # Strip markdown code fences
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"```\s*$", "", s)
    s = s.strip()
    try:
        parsed = json.loads(s)
        # Flatten to text: subject + action + editorial_hooks
        if isinstance(parsed, dict):
            bits = []
            for k in ("subject", "action", "setting", "framing", "mood", "editorial_hooks"):
                v = parsed.get(k)
                if v:
                    bits.append(f"{k}: {v}")
            flat = " | ".join(bits) if bits else s
            return flat, parsed
    except Exception:
        pass
    return text, None


# --- Gemini Flash / Pro ---------------------------------------------------------

def _api_key() -> str:
    k = os.environ.get("GEMINI_API_KEY")
    if k:
        return k
    rc = Path.home() / ".zshrc"
    if rc.exists():
        for line in rc.read_text().splitlines():
            m = re.match(r'^\s*export\s+GEMINI_API_KEY\s*=\s*"?([^"\s]+)"?', line)
            if m:
                return m.group(1)
    raise RuntimeError("GEMINI_API_KEY not in env nor .zshrc")


class GeminiCaptionEngine(CaptionEngine):
    """Wraps Gemini 2.5 Flash or Pro via the google-genai SDK."""

    def __init__(self, model: str = "gemini-2.5-flash"):
        from google import genai
        self.name = model
        self.model = model
        self._client = genai.Client(api_key=_api_key())

    def caption(self, image_bytes: bytes, prompt: str) -> dict | None:
        from google.genai import types
        cfg = types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
        )
        img_part = types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
        t0 = time.time()
        try:
            resp = self._client.models.generate_content(
                model=self.model,
                contents=[img_part, prompt],
                config=cfg,
            )
        except Exception as e:
            return {"text": f"[ERROR] {type(e).__name__}: {e}", "json": None,
                    "latency_sec": time.time() - t0, "engine": self.name}
        latency = time.time() - t0
        text = resp.text or ""
        flat, parsed = _parse_json_caption(text)
        return {"text": flat, "json": parsed, "latency_sec": latency, "engine": self.name}


# --- MiniCPM-V 2.6 -------------------------------------------------------------

class MiniCpmVCaptionEngine(CaptionEngine):
    """Local MiniCPM-V 2.6 via transformers. First call downloads ~16 GB."""

    name = "minicpm_v_2_6"

    def __init__(self, device: str = "mps"):
        from transformers import AutoModel, AutoTokenizer
        import torch
        self.device = device
        print(f"  [minicpm_v_2_6] loading model on {device} (first run downloads ~16 GB)...", file=sys.stderr, flush=True)
        # The MiniCPM-V 2.6 model id
        model_id = "openbmb/MiniCPM-V-2_6"
        self.tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
        # MPS doesn't support all ops; load as fp16 on MPS, fall back to bf16 CPU
        dtype = torch.float16 if device == "mps" else torch.bfloat16
        self.model = AutoModel.from_pretrained(
            model_id, trust_remote_code=True, torch_dtype=dtype,
        ).to(device).eval()
        print(f"  [minicpm_v_2_6] loaded", file=sys.stderr, flush=True)

    def caption(self, image_bytes: bytes, prompt: str) -> dict | None:
        from PIL import Image
        import torch
        try:
            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        except Exception as e:
            return {"text": f"[ERROR] image decode: {e}", "json": None,
                    "latency_sec": 0.0, "engine": self.name}
        t0 = time.time()
        try:
            # MiniCPM-V's chat API
            msgs = [{"role": "user", "content": [img, prompt]}]
            with torch.no_grad():
                resp = self.model.chat(
                    image=None,  # passed via msgs[0]['content']
                    msgs=msgs,
                    tokenizer=self.tokenizer,
                    sampling=False,
                    temperature=0.1,
                    max_new_tokens=400,
                )
        except Exception as e:
            return {"text": f"[ERROR] {type(e).__name__}: {e}", "json": None,
                    "latency_sec": time.time() - t0, "engine": self.name}
        latency = time.time() - t0
        text = resp if isinstance(resp, str) else str(resp)
        flat, parsed = _parse_json_caption(text)
        return {"text": flat, "json": parsed, "latency_sec": latency, "engine": self.name}


class Qwen2VLCaptionEngine(CaptionEngine):
    """Local Qwen2-VL-2B-Instruct via transformers. Public weights on HuggingFace
    (no gating), ~5 GB download, MPS-compatible.

    Picked as the local-model slot after:
      - MiniCPM-V 2.6 turned out to be a gated repo (HF token + terms required)
      - Florence-2-large turned out to be incompatible with transformers 4.57
        (its `prepare_inputs_for_generation` assumes non-None past_key_values)

    Qwen2-VL is Alibaba's general-purpose vision-language model. The 2B variant
    is the sweet spot for M4 Max: enough capability for editorial-style
    captions; small enough to load + infer comfortably.
    """

    name = "qwen2_vl_2b"

    def __init__(self, device: str = "mps"):
        from transformers import Qwen2VLForConditionalGeneration, AutoProcessor
        import torch
        self.device = device
        print(f"  [qwen2_vl_2b] loading model on {device} (first run downloads ~5 GB)...",
              file=sys.stderr, flush=True)
        model_id = "Qwen/Qwen2-VL-2B-Instruct"
        self.processor = AutoProcessor.from_pretrained(model_id)
        dtype = torch.float16 if device == "mps" else torch.bfloat16
        self.model = Qwen2VLForConditionalGeneration.from_pretrained(
            model_id, torch_dtype=dtype,
        ).to(device).eval()
        self._torch = torch
        print(f"  [qwen2_vl_2b] loaded", file=sys.stderr, flush=True)

    def caption(self, image_bytes: bytes, prompt: str) -> dict | None:
        from PIL import Image
        torch = self._torch
        try:
            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        except Exception as e:
            return {"text": f"[ERROR] image decode: {e}", "json": None,
                    "latency_sec": 0.0, "engine": self.name}
        t0 = time.time()
        try:
            messages = [{
                "role": "user",
                "content": [
                    {"type": "image", "image": img},
                    {"type": "text", "text": prompt},
                ],
            }]
            text_prompt = self.processor.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True,
            )
            inputs = self.processor(
                text=[text_prompt], images=[img], padding=True, return_tensors="pt",
            ).to(self.device)
            with torch.no_grad():
                output_ids = self.model.generate(
                    **inputs, max_new_tokens=400, do_sample=False,
                )
            # Strip the input portion from the output
            generated_ids = output_ids[:, inputs["input_ids"].shape[1]:]
            text = self.processor.batch_decode(generated_ids, skip_special_tokens=True)[0]
        except Exception as e:
            return {"text": f"[ERROR] {type(e).__name__}: {e}", "json": None,
                    "latency_sec": time.time() - t0, "engine": self.name}
        latency = time.time() - t0
        flat, parsed = _parse_json_caption(text)
        return {"text": flat, "json": parsed, "latency_sec": latency, "engine": self.name}


class Florence2CaptionEngine(CaptionEngine):
    """Florence-2-large (771M) via transformers. Caption-only specialist; does
    NOT reliably follow long structured prompts — we ignore the JSON ask and
    just use Florence-2's native task prompt (`<MORE_DETAILED_CAPTION>`).

    Honest expectation: Florence-2 returns 1-2 sentence prose captions that
    are barely richer than what SigLIP already encodes. Included for fair
    comparison so the pilot report is complete.
    """

    name = "florence_2_large"

    def __init__(self, device: str = "cpu"):
        from transformers import AutoModelForCausalLM, AutoProcessor
        import torch
        # Florence-2 + MPS hits "NoneType.shape" mid-generate on Apple Silicon
        # (model.generate path doesn't fully handle MPS tensor placement for
        # the BART decoder). CPU works reliably at the cost of ~3x slower
        # inference; still fast enough for the pilot since Florence-2 is
        # ~0.77B params (fp32 fits in ~3 GB RAM).
        self.device = device
        print(f"  [florence_2_large] loading model on {device} (first run downloads ~1.5 GB)...",
              file=sys.stderr, flush=True)
        model_id = "microsoft/Florence-2-large"
        self.processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
        # `attn_implementation='eager'` bypasses transformers 4.57's SDPA-dispatch
        # check, which trips on Florence-2's `_supports_sdpa` attr (known compat
        # bug — Florence-2 was authored before the SDPA dispatch refactor).
        self.model = AutoModelForCausalLM.from_pretrained(
            model_id, trust_remote_code=True, torch_dtype=torch.float32,
            attn_implementation="eager",
        ).to(device).eval()
        self._torch = torch
        print(f"  [florence_2_large] loaded", file=sys.stderr, flush=True)

    def caption(self, image_bytes: bytes, prompt: str) -> dict | None:
        from PIL import Image
        torch = self._torch
        try:
            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        except Exception as e:
            return {"text": f"[ERROR] image decode: {e}", "json": None,
                    "latency_sec": 0.0, "engine": self.name}
        # Florence-2 uses task prompts, not free-form text. Ignore the long
        # structured prompt and use the model's intended detailed-caption mode.
        task_prompt = "<MORE_DETAILED_CAPTION>"
        t0 = time.time()
        try:
            inputs = self.processor(text=task_prompt, images=img, return_tensors="pt").to(self.device)
            with torch.no_grad():
                generated_ids = self.model.generate(
                    input_ids=inputs["input_ids"],
                    pixel_values=inputs["pixel_values"],
                    max_new_tokens=512,
                    num_beams=3,
                    do_sample=False,
                )
            generated_text = self.processor.batch_decode(generated_ids, skip_special_tokens=False)[0]
            parsed = self.processor.post_process_generation(
                generated_text, task=task_prompt,
                image_size=(img.width, img.height),
            )
            text = parsed.get(task_prompt, generated_text) if isinstance(parsed, dict) else generated_text
        except Exception as e:
            return {"text": f"[ERROR] {type(e).__name__}: {e}", "json": None,
                    "latency_sec": time.time() - t0, "engine": self.name}
        latency = time.time() - t0
        # Florence-2 returns prose, not JSON. Wrap into a uniform shape.
        return {"text": text if isinstance(text, str) else str(text),
                "json": None, "latency_sec": latency, "engine": self.name}


def load_engine(name: str) -> CaptionEngine:
    if name == "gemini_flash":
        return GeminiCaptionEngine(model="gemini-2.5-flash")
    if name == "gemini_pro":
        return GeminiCaptionEngine(model="gemini-2.5-pro")
    if name == "qwen2_vl_2b":
        return Qwen2VLCaptionEngine()
    if name == "minicpm_v_2_6":
        # Gated HuggingFace repo. Requires `huggingface-cli login` + accepting
        # terms at https://huggingface.co/openbmb/MiniCPM-V-2_6 before this works.
        return MiniCpmVCaptionEngine()
    if name == "florence_2_large":
        # Broken on transformers 4.57: Florence-2's bundled
        # `prepare_inputs_for_generation` references `past_key_values[0][0].shape[2]`
        # unconditionally, but transformers 4.57 passes None on the first call.
        # Pinning transformers down breaks other K-layers. Deprecated in this
        # workspace; left wired for completeness only.
        return Florence2CaptionEngine()
    raise ValueError(f"unknown engine: {name}")
