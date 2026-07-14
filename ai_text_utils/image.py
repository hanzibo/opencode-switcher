"""Image processing and vision model utilities.

Pure functions for converting images to data URIs, handling vision
content parts from multimodal LLM responses, and detecting vision
capabilities from model names.

Zero GTK dependency. Standalone module (no dependency on other
ai_text_utils submodules except cleanup for _close_unclosed_code_blocks).
"""

import os
import base64
import mimetypes
from typing import Optional, List

from clipboard_store import CONFIG_DIR
from .cleanup import _close_unclosed_code_blocks


def _image_to_data_uri(image_path: str) -> Optional[str]:
    """Read an image file and return a base64 data URI string.

    Detects mime type dynamically (fallback to image/png).
    """
    try:
        with open(image_path, "rb") as f:
            raw = f.read()
        b64 = base64.b64encode(raw).decode("utf-8")
        mime, _ = mimetypes.guess_type(image_path)
        if not mime or not mime.startswith("image/"):
            mime = "image/png"
        return f"data:{mime};base64,{b64}"
    except Exception:
        return None


def _image_hash_path(image_hash: str) -> str:
    """Return the absolute path to a cached image by its SHA-256 hash (16-char prefix)."""
    img_dir = os.path.join(CONFIG_DIR, "images")
    if os.path.isdir(img_dir):
        try:
            for f in os.listdir(img_dir):
                if f.startswith(image_hash):
                    return os.path.join(img_dir, f)
        except Exception:
            pass
    return os.path.join(img_dir, f"{image_hash}.png")


def _cached_image_to_data_uri(image_hash: str) -> Optional[str]:
    """Read a cached image by hash and return its data URI, or None if missing."""
    path = _image_hash_path(image_hash)
    if not os.path.isfile(path):
        return None
    return _image_to_data_uri(path)


def _vision_content_to_text(content: list) -> str:
    """Extract plain text from a vision content parts list, ignoring images."""
    texts = []
    for p in content:
        if isinstance(p, dict) and p.get("type") == "text":
            texts.append(p.get("text", ""))
    return "\n".join(texts)


def _vision_content_to_markdown(content: list) -> str:
    """Convert a vision content parts list to markdown+HTML representation.

    Text parts are joined and run through code-block closure.
    Image parts are resolved (hash->data URI) and rendered as ``<img>`` tags.
    """
    md_parts = []
    for p in content:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text":
            md_parts.append(p.get("text", ""))
        elif p.get("type") == "image_url":
            src = _resolve_vision_image_src([p])
            if src:
                md_parts.append(
                    f'<img src="{src}" class="chat-image" onclick="showLightbox(this.src)">'
                )
    md_text = "\n".join(md_parts)
    return _close_unclosed_code_blocks(md_text)


def _resolve_vision_image_src(content: list) -> Optional[str]:
    """Extract image source from vision content parts list.

    Handles both direct ``url`` (legacy) and ``hash`` reference formats.
    Returns a local ``file://`` URI or ``data:`` URI string, or None if no image part found/failed.
    """
    for p in content:
        if not isinstance(p, dict) or p.get("type") != "image_url":
            continue
        iu = p.get("image_url", {})
        url = iu.get("url", "")
        if url.startswith("data:") or url.startswith("file:"):
            return url
        h = iu.get("hash", "")
        if h:
            return "file://" + _image_hash_path(h)
    return None


def _model_supports_vision(model_name: str) -> bool:
    """启发式判断模型名称是否支持多模态视觉输入"""
    name_lower = model_name.lower()
    vision_keywords = [
        "vision", "vl", "gpt-4o", "claude-3", "gemini", "mimo",
        "minicpm", "internvl", "llava", "qwen2.5-vl", "qwen-vl", "deepseek-vl"
    ]
    return any(kw in name_lower for kw in vision_keywords)
