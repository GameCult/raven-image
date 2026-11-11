"""Flux3 caption-span encoder: encode reference image + caption together in the Qwen3-VL encoder, then control how
much of the image side of the hidden states reaches the DiT.

Stock TextEncodeQwenImageEditPlus returns every position, image tokens included. Here the image positions (the
vision-tower tokens that replaced <|image_pad|>) are first average-pooled down to pool_tokens positions (0 = keep all),
then multiplied by alpha. alpha 1 with no pooling is stock (arm B); alpha 0 drops the positions altogether (arm C:
caption-only, image-informed, because the caption and template tokens keep the hidden states they got by attending to
the image); alpha > 1 over-weights them. No vae input: reference latents are ReferenceMultiLatent's job, as in arm A.

One image only. Install: copy this file to ComfyUI/custom_nodes/ on Raven and restart ComfyUI.
Written against ComfyUI master 74eb0c4 (0.45.0); Raven runs 0.44.0.
"""
import math

import torch
import torch.nn.functional as F

import comfy.utils
from comfy_api.latest import ComfyExtension, io

TEMPLATE = ("<|im_start|>system\nDescribe the key features of the input image (color, shape, size, texture, objects, "
            "background), then explain how the user's text instruction should alter or modify the image. Generate a new "
            "image that meets the user's requirements while maintaining consistency with the original input where "
            "appropriate.<|im_end|>\n<|im_start|>user\n{}<|im_end|>\n<|im_start|>assistant\n")
IMAGE_PROMPT = "Picture 1: <|vision_start|><|image_pad|><|vision_end|>"


def splice(t, start, n, alpha, pool_tokens=0):
    """t [B, L, C]: positions [start, start+n) become alpha * (optionally pooled) positions; alpha 0 removes them."""
    head, span, tail = t[:, :start], t[:, start:start + n], t[:, start + n:]
    if alpha == 0:
        return torch.cat([head, tail], dim=1)
    if pool_tokens > 0:
        span = F.adaptive_avg_pool1d(span.transpose(1, 2).float(), pool_tokens).transpose(1, 2).to(t.dtype)
    return torch.cat([head, span * alpha, tail], dim=1)


def image_span(token_list, out_len):
    """(start, n) of the image positions in the encoder output, from the tokenizer's list and the output length."""
    idx = [i for i, e in enumerate(token_list) if isinstance(e[0], dict) and e[0].get("type") == "image"]
    if len(idx) != 1:
        raise ValueError(f"expected exactly one image placeholder in the token list, found {len(idx)}")
    n = out_len - (len(token_list) - 1)
    if n <= 0:
        raise ValueError(f"output length {out_len} leaves no room for image tokens in a list of {len(token_list)}")
    return idx[0], n


class Flux3CaptionSpanEncode(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="Flux3CaptionSpanEncode",
            display_name="Flux3 Caption Span Encode (image seen, image positions dropped)",
            category="model/conditioning",
            inputs=[
                io.Clip.Input("clip"),
                io.String.Input("prompt", multiline=True, dynamic_prompts=True),
                io.Image.Input("image1"),
                io.Float.Input("alpha", default=1.0, min=0.0, max=4.0, step=0.05,
                               tooltip="Scale of the image positions' hidden states. 0 drops them (arm C), 1 is stock, >1 over-weights."),
                io.Int.Input("pool_tokens", default=0, min=0, max=64,
                             tooltip="0 keeps every image position; N>0 average-pools them to N first (arm C2)."),
            ],
            outputs=[io.Conditioning.Output()],
        )

    @classmethod
    def execute(cls, clip, prompt, image1, alpha=1.0, pool_tokens=0) -> io.NodeOutput:
        samples = image1.movedim(-1, 1)
        scale = math.sqrt(384 * 384 / (samples.shape[3] * samples.shape[2]))
        s = comfy.utils.common_upscale(samples, round(samples.shape[3] * scale), round(samples.shape[2] * scale), "area", "disabled")
        tokens = clip.tokenize(IMAGE_PROMPT + prompt, images=[s.movedim(1, -1)], llama_template=TEMPLATE)
        groups = next(iter(tokens.values()))
        if len(groups) != 1:
            raise ValueError(f"expected one token group, got {len(groups)}")
        cond = clip.encode_from_tokens_scheduled(tokens)
        out = []
        for t, d in cond:
            full = t.shape[1]
            start, n = image_span(groups[0], full)
            d = dict(d)
            kept = 0 if alpha == 0 else (pool_tokens or n)
            for k, v in d.items():  # position-indexed extras (attention masks) follow the same cut
                if torch.is_tensor(v) and v.dim() >= 2 and v.shape[1] == full:
                    d[k] = torch.cat([v[:, :start], v[:, start:start + kept], v[:, start + n:]], dim=1)
            t2 = splice(t, start, n, alpha, pool_tokens)
            print(f"Flux3CaptionSpanEncode: {full} positions, image at [{start}, {start + n}) -> {t2.shape[1]} (alpha={alpha}, pool_tokens={pool_tokens})")
            out.append([t2, d])
        return io.NodeOutput(out)


class Flux3CaptionSpanExtension(ComfyExtension):
    async def get_node_list(self):
        return [Flux3CaptionSpanEncode]


async def comfy_entrypoint():
    return Flux3CaptionSpanExtension()
