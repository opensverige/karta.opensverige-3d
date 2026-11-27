"""Redraw core moved verbatim from scripts/redraw_tile.py.

The bodies of the moved functions are unchanged. redraw_image is the same
pad, encode, denoise, decode and crop, without a cell, a claim or a file.
"""

from __future__ import annotations

import importlib
import sys

from rutor.errors import environment
from rutor.pipeline import LORA_ID, MODEL_ID, PINS

WINDOW = 1024
STEP = 512
VAE_ALIGN = 32
MAX_SIDE = 4096
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

def pad_size(n: int, window: int = WINDOW, step: int = STEP, multiple: int = VAE_ALIGN) -> int:
    if n <= window:
        target = window
    else:
        k = -(-(n - window) // step)  # ceil division
        target = window + k * step
    if target % multiple != 0:
        raise ValueError(f"Padded size {target} is not a multiple of {multiple}")
    return target

def get_views(width: int, height: int, window: int = WINDOW, step: int = STEP) -> list[tuple[int, int]]:
    """Top-left corners of each window. The tile must already be padded to the grid."""
    if width < window or height < window:
        raise ValueError("Tile is smaller than one window; pad it first")
    if (width - window) % step != 0 or (height - window) % step != 0:
        raise ValueError("Stride does not land on the far edge; pad the tile first")
    views = []
    for top in range(0, height - window + 1, step):
        for left in range(0, width - window + 1, step):
            views.append((top, left))
    if not views:
        raise ValueError("No windows")
    return views

def window_token_indices(
    full_h: int,
    full_w: int,
    top: int,
    left: int,
    window: int = WINDOW,
    scale: int = 16,
) -> list[int]:
    """Row-major latent tokens for one window. Matches QwenImage21Pipeline._pack_latents."""
    if full_h % scale or full_w % scale or window % scale or top % scale or left % scale:
        raise ValueError("Pixel coordinates must be divisible by the VAE scale")
    latent_w = full_w // scale
    y0 = top // scale
    x0 = left // scale
    side = window // scale
    indices = []
    for y in range(y0, y0 + side):
        row = y * latent_w
        for x in range(x0, x0 + side):
            indices.append(row + x)
    return indices

def edge_pad(image, width: int, height: int):
    from PIL import Image

    nearest = getattr(getattr(Image, "Resampling", Image), "NEAREST")
    src_w, src_h = image.size
    if image.mode != "RGB":
        image = image.convert("RGB")
    if (src_w, src_h) == (width, height):
        return image
    canvas = Image.new("RGB", (width, height))
    canvas.paste(image, (0, 0))
    if width > src_w:
        strip = image.crop((src_w - 1, 0, src_w, src_h)).resize((width - src_w, src_h), nearest)
        canvas.paste(strip, (src_w, 0))
    if height > src_h:
        strip = canvas.crop((0, src_h - 1, width, src_h)).resize((width, height - src_h), nearest)
        canvas.paste(strip, (0, src_h))
    return canvas

def preflight_message() -> str | None:
    try:
        import torch
    except ImportError:
        return "PyTorch is not installed. Follow the README install for your OS, then run this preflight again."
    if not torch.cuda.is_available():
        if sys.platform == "darwin":
            return (
                "No NVIDIA CUDA GPU is visible. macOS can clone, install, check the SE3 price, and list sheets. "
                "The 4-bit checkpoint is a bitsandbytes quant and the redraw runs on CUDA. "
                "Do not claim a cell on this Mac."
            )
        return (
            "No GPU is visible to PyTorch. On an NVIDIA card install the CUDA build of PyTorch, on an AMD card "
            "under Linux the ROCm build, and check the driver. Do not claim a cell until preflight passes."
        )
    try:
        import bitsandbytes
    except ImportError:
        return "bitsandbytes is not installed. Run pip install -r requirements.txt on this machine."
    del bitsandbytes
    return None

def _quiet_loading_bars() -> None:
    """Keep the bars for loading the model off a pipe.

    Read through a pipe, as an agent or the app page reads, each tick of a bar is a line of
    its own: hundreds of them around the few that say something. A person at a terminal keeps
    them, and the bars of the first download are huggingface_hub's own and are left alone.
    """
    if sys.stderr.isatty():
        return
    for name in ("diffusers.utils.logging", "transformers.utils.logging"):
        try:
            getattr(importlib.import_module(name), "disable_progress_bar")()
        except (ImportError, AttributeError):
            continue


def _load_pipe():
    import torch
    from diffusers import QwenImage21Pipeline

    _quiet_loading_bars()

    # Safetensors only: a pickle file runs code when it is loaded, a safetensors file cannot.
    kwargs = {"low_cpu_mem_usage": True, "use_safetensors": True}
    model_revision = PINS.get("model_revision", "")
    if model_revision:
        kwargs["revision"] = model_revision
    try:
        pipe = QwenImage21Pipeline.from_pretrained(MODEL_ID, dtype=torch.bfloat16, **kwargs)
    except TypeError:
        pipe = QwenImage21Pipeline.from_pretrained(MODEL_ID, torch_dtype=torch.bfloat16, **kwargs)
    lora_kwargs = {"use_safetensors": True}
    lora_revision = PINS.get("lora_revision", "")
    if lora_revision:
        lora_kwargs["revision"] = lora_revision
    try:
        pipe.load_lora_weights(LORA_ID, **lora_kwargs)
    except Exception as exc:
        raise environment(
            f"Could not load LoRA {LORA_ID} on {MODEL_ID}. No PNG was written. "
            f"Keep this cell and retry after fixing the environment. ({exc.__class__.__name__}: {exc})"
        ) from exc
    pipe.to("cuda")
    if hasattr(pipe.vae, "enable_tiling"):
        pipe.vae.enable_tiling()
    pipe.set_progress_bar_config(disable=True)
    return pipe

def _prepare_window(pipe, crop, prompt: str, device, dtype):
    import torch

    crop = crop.convert("RGBA")
    prompt_embeds, prompt_mask, image_pad_mask = pipe.encode_prompt(
        image=[crop],
        prompt=prompt,
        device=device,
    )
    scale = pipe.vae_scale_factor
    target_tokens = (WINDOW // scale) * (WINDOW // scale)
    if target_tokens % 4 != 0:
        raise environment("Window token count is not divisible by 4")
    # Same target-slot extension as QwenImage21Pipeline.__call__.
    image_pad_mask = torch.cat(
        [image_pad_mask, image_pad_mask.new_ones(image_pad_mask.shape[0], target_tokens // 4)],
        dim=1,
    )
    channels = pipe.transformer.config.in_channels
    tensor = pipe.image_processor.preprocess(crop, width=WINDOW, height=WINDOW).unsqueeze(2)
    dummy = torch.zeros(1, target_tokens, channels, device=device, dtype=dtype)
    _ignored, image_latents = pipe.prepare_latents(
        [tensor],
        1,
        channels,
        WINDOW,
        WINDOW,
        dtype,
        device,
        None,
        dummy,
    )
    side = WINDOW // scale
    return {
        "prompt_embeds": prompt_embeds.detach().to("cpu"),
        "prompt_mask": prompt_mask.detach().to("cpu") if prompt_mask is not None else None,
        "image_pad_mask": image_pad_mask.detach().to("cpu"),
        "image_latents": image_latents.detach().to("cpu"),
        "img_shapes": [[(1, side, side), (1, side, side)]],
        "tokens": target_tokens,
    }

def _denoise(pipe, full, views, contexts, full_h: int, full_w: int, device, dtype):
    import torch
    from diffusers.pipelines.qwenimage21.pipeline_qwenimage21 import calculate_shift, retrieve_timesteps

    scale = pipe.vae_scale_factor
    if WINDOW % scale or STEP % scale:
        raise environment(f"Window {WINDOW} and step {STEP} must be divisible by VAE scale {scale}")
    sigmas = pipe.config.sample_sigmas
    if not sigmas:
        raise environment("The Turbo checkpoint has no sample_sigmas. Refusing to invent a schedule.")
    seq = (WINDOW // scale) * (WINDOW // scale)
    scheduler_config = pipe.scheduler.config
    mu = calculate_shift(
        seq,
        scheduler_config.get("base_image_seq_len", 256),
        scheduler_config.get("max_image_seq_len", 4096),
        scheduler_config.get("base_shift", 0.5),
        scheduler_config.get("max_shift", 1.15),
    )
    timesteps, count = retrieve_timesteps(
        pipe.scheduler,
        len(list(sigmas)),
        device,
        sigmas=list(sigmas),
        mu=mu,
    )
    pipe.scheduler.set_begin_index(0)
    index_cache = [window_token_indices(full_h, full_w, top, left, WINDOW, scale) for top, left in views]

    with torch.inference_mode():
        for step_index, timestep in enumerate(timesteps):
            acc = torch.zeros_like(full)
            weight = torch.zeros(full.shape[0], full.shape[1], 1, device=full.device, dtype=full.dtype)
            for window_index, context in enumerate(contexts):
                print(
                    f"step {step_index + 1}/{count} window {window_index + 1}/{len(views)}",
                    flush=True,
                )
                indices = torch.tensor(index_cache[window_index], device=full.device, dtype=torch.long)
                crop = full.index_select(1, indices).clone()
                image_latents = context["image_latents"].to(device=device, dtype=dtype)
                hidden = torch.cat([image_latents, crop], dim=1)
                mark = timestep.expand(crop.shape[0]).to(dtype=dtype)
                noise_pred = pipe.transformer(
                    hidden_states=hidden,
                    timestep=mark / 1000,
                    encoder_hidden_states=context["prompt_embeds"].to(device=device, dtype=dtype),
                    encoder_hidden_states_mask=context["prompt_mask"].to(device=device) if context["prompt_mask"] is not None else None,
                    img_shapes=context["img_shapes"],
                    img_mask=context["image_pad_mask"].to(device=device),
                    attention_kwargs={},
                    kv_cache=None,
                    kv_cache_mode=None,
                    return_dict=False,
                )[0]
                noise_pred = noise_pred[:, -crop.shape[1] :].to(dtype=full.dtype)
                acc.index_add_(1, indices, noise_pred)
                weight.index_add_(
                    1,
                    indices,
                    torch.ones(crop.shape[0], crop.shape[1], 1, device=full.device, dtype=full.dtype),
                )
                del hidden, noise_pred, image_latents, crop
            if int((weight < 1).sum().item()) != 0:
                raise environment("The window grid missed part of the tile. No PNG was written.")
            full = pipe.scheduler.step(acc / weight, timestep, full, return_dict=False)[0]
    return full

# Which parts sit on the GPU in each phase. Encoding needs the text encoder and the
# VAE, denoising needs the transformer, decoding needs the VAE. Keeping the rest on
# the CPU is what lets the redraw fit a 24 GB card such as an L4.
_PHASES: dict[str, dict[str, str]] = {
    "encode": {"text_encoder": "cuda", "transformer": "cpu", "vae": "cuda"},
    "denoise": {"text_encoder": "cpu", "transformer": "cuda", "vae": "cuda"},
    "decode": {"text_encoder": "cpu", "transformer": "cpu", "vae": "cuda"},
}


def _place(pipe, phase: str, empty_cache=None) -> None:
    placement = _PHASES.get(phase)
    if placement is None:
        raise ValueError(f"unknown redraw phase {phase}")
    # Move parts off the GPU before moving others on, so the peak never holds both.
    ordered = sorted(placement.items(), key=lambda item: item[1] != "cpu")
    for name, device in ordered:
        part = getattr(pipe, name, None)
        if part is not None:
            part.to(device)
    if empty_cache is None:
        import torch

        empty_cache = torch.cuda.empty_cache
    empty_cache()


def _decode(pipe, latents, height: int, width: int):
    import torch

    _place(pipe, "decode")
    lat = pipe._unpack_latents(latents, height, width, pipe.vae_scale_factor).to(pipe.vae.dtype)
    z_dim = pipe.vae.config.z_dim
    mean = torch.tensor(pipe.vae.config.latents_mean).view(1, z_dim, 1, 1, 1).to(lat.device, lat.dtype)
    std = torch.tensor(pipe.vae.config.latents_std).view(1, z_dim, 1, 1, 1).to(lat.device, lat.dtype)
    lat = lat * std + mean
    b, c, t, h, w = lat.shape
    f = height // h
    tile, overlap = 96, 32
    stride = tile - overlap
    out = None
    wsum = torch.zeros(1, 1, height, width, dtype=torch.float32)
    ov = overlap * f

    def ramp(n):
        i = torch.arange(n, dtype=torch.float32)
        return torch.clamp(torch.minimum((i + 1) / ov, (n - i) / ov), max=1.0)

    ys = sorted({min(y, max(0, h - tile)) for y in range(0, h, stride)})
    xs = sorted({min(x, max(0, w - tile)) for x in range(0, w, stride)})
    with torch.inference_mode():
        for y0 in ys:
            for x0 in xs:
                y1, x1 = min(y0 + tile, h), min(x0 + tile, w)
                piece = pipe.vae.decode(lat[:, :, :, y0:y1, x0:x1], return_dict=False)[0][:, :, 0].float().cpu()
                ph, pw = piece.shape[-2], piece.shape[-1]
                if out is None:
                    out = torch.zeros(b, piece.shape[1], height, width, dtype=torch.float32)
                wgt = (ramp(ph)[:, None] * ramp(pw)[None, :])[None, None]
                out[..., y0 * f:y0 * f + ph, x0 * f:x0 * f + pw] += piece * wgt
                wsum[..., y0 * f:y0 * f + ph, x0 * f:x0 * f + pw] += wgt
                del piece
                torch.cuda.empty_cache()
    out = out / wsum
    return pipe.image_processor.postprocess(out, output_type="pil")[0]


def redraw_image(image: object, prompt: str, seed: int | None) -> object:
    """Pad, encode, denoise, decode and crop, as redraw() did for one tile.

    Runs under torch.inference_mode: without it autograd keeps every VAE and text
    encoder activation, which needed over 21 GB for one window and broke 24 GB cards.
    """
    import torch

    with torch.inference_mode():
        return _redraw_image(image, prompt, seed)


def _redraw_image(image: object, prompt: str, seed: int | None) -> object:
    from PIL import Image

    if not isinstance(image, Image.Image):
        raise TypeError("redraw_image expects a PIL image")
    rgb = image.convert("RGB")
    src_w, src_h = rgb.size
    if max(src_w, src_h) > MAX_SIDE:
        raise ValueError(f"Input is {src_w}x{src_h}. One cell must be at most {MAX_SIDE}px on the long side.")
    pad_w, pad_h = pad_size(src_w), pad_size(src_h)
    padded = edge_pad(rgb, pad_w, pad_h)
    views = get_views(pad_w, pad_h)

    import torch

    pipe = _load_pipe()
    device = torch.device("cuda")
    dtype = torch.bfloat16
    generator = torch.Generator(device="cpu")
    if seed is not None:
        generator.manual_seed(seed)
    channels = pipe.transformer.config.in_channels
    full, _unused = pipe.prepare_latents(
        None,
        1,
        channels,
        pad_h,
        pad_w,
        dtype,
        device,
        generator,
        None,
    )
    _place(pipe, "encode")
    contexts = []
    for index, (top, left) in enumerate(views):
        print(f"encode window {index + 1}/{len(views)}", flush=True)
        crop = padded.crop((left, top, left + WINDOW, top + WINDOW))
        contexts.append(_prepare_window(pipe, crop, prompt, device, dtype))
    _place(pipe, "denoise")
    full = _denoise(pipe, full, views, contexts, pad_h, pad_w, device, dtype)
    return _decode(pipe, full, pad_h, pad_w).convert("RGB").crop((0, 0, src_w, src_h))
