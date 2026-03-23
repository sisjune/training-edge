"""统一 LLM 客户端 — 支持 OpenRouter / OpenAI 兼容 API。

OpenRouter 兼容 OpenAI SDK，支持所有主流模型：
  - GPT-5.4, GPT-4.5, GPT-4o
  - Claude 4 Sonnet/Opus
  - Gemini, DeepSeek, Llama 等

配置优先级: 数据库 settings > 环境变量 > 默认值
"""

from __future__ import annotations

import json
import logging
import os
import re
import base64
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------

DEFAULT_API_BASE = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "openai/gpt-5.4"  # sensible default; user can change
DEFAULT_VISION_MODEL = "openai/gpt-4.1"  # vision still uses 4.1 (cost efficient)

# ---------------------------------------------------------------------------
# Settings helpers (read from DB if available)
# ---------------------------------------------------------------------------

def _get_setting(key: str, default: str = "") -> str:
    """Try to read from DB settings, fall back to env var."""
    try:
        from engine import database
        with database.get_db() as conn:
            val = database.get_setting(conn, key)
            if val:
                return val
    except Exception:
        pass
    return os.environ.get(key.upper().replace(".", "_"), default)


def get_api_key() -> str:
    """Get the LLM API key."""
    key = _get_setting("llm_api_key")
    if not key:
        key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise ValueError(
            "LLM API Key 未设置。请在设置页面配置 OpenRouter API Key，"
            "或设置环境变量 OPENROUTER_API_KEY"
        )
    return key


def get_api_base() -> str:
    return _get_setting("llm_api_base", DEFAULT_API_BASE)


def get_model() -> str:
    return _get_setting("llm_model", DEFAULT_MODEL)


def get_vision_model() -> str:
    return _get_setting("llm_vision_model", DEFAULT_VISION_MODEL)


def get_proxy() -> Optional[str]:
    """Get HTTP/SOCKS proxy URL for LLM API requests.

    Supports:
      - DB setting 'llm_proxy' (e.g. 'socks5://vps-ip:1080' or 'http://vps-ip:7890')
      - Env var OPENROUTER_PROXY or HTTPS_PROXY
    """
    proxy = _get_setting("llm_proxy")
    if not proxy:
        proxy = os.environ.get("OPENROUTER_PROXY", "")
    if not proxy:
        proxy = os.environ.get("HTTPS_PROXY", "")
    return proxy or None


# ---------------------------------------------------------------------------
# Chat completion (OpenAI-compatible)
# ---------------------------------------------------------------------------

def chat_completion(
    messages: List[Dict[str, Any]],
    model: Optional[str] = None,
    max_tokens: int = 4000,
    temperature: float = 0.7,
) -> str:
    """Call LLM via OpenAI-compatible API (works with OpenRouter).

    Supports HTTP/SOCKS5 proxy for region-restricted APIs.

    Args:
        messages: OpenAI-format messages [{"role": "user", "content": "..."}]
        model: Model name override
        max_tokens: Max response tokens
        temperature: Sampling temperature

    Returns:
        Response text content
    """
    import requests

    api_key = get_api_key()
    api_base = get_api_base().rstrip("/")
    model = model or get_model()
    proxy = get_proxy()

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://training-edge.local",
        "X-Title": "TrainingEdge",
    }

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    # Proxy config
    proxies = None
    if proxy:
        proxies = {"https": proxy, "http": proxy}
        logger.info("LLM request: model=%s, base=%s, proxy=%s, messages=%d",
                     model, api_base, proxy, len(messages))
    else:
        logger.info("LLM request: model=%s, base=%s, messages=%d", model, api_base, len(messages))

    resp = requests.post(
        f"{api_base}/chat/completions",
        headers=headers,
        json=payload,
        timeout=120,
        proxies=proxies,
    )

    if resp.status_code != 200:
        error_detail = resp.text[:500]
        raise ValueError(f"LLM API 错误 ({resp.status_code}): {error_detail}")

    data = resp.json()
    text = data["choices"][0]["message"]["content"]
    logger.info("LLM response: %d chars, model=%s", len(text), data.get("model", model))
    return text


def vision_completion(
    images: List[bytes],
    prompt: str,
    model: Optional[str] = None,
    max_tokens: int = 2000,
) -> str:
    """Call Vision LLM with images + text prompt.

    Args:
        images: List of image bytes
        prompt: Text prompt
        model: Model name override (defaults to vision model)
        max_tokens: Max response tokens

    Returns:
        Response text content
    """
    model = model or get_vision_model()

    # Build content array with images
    content: List[Dict[str, Any]] = []
    for img_bytes in images:
        media_type = _detect_media_type(img_bytes)
        b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:{media_type};base64,{b64}",
            },
        })
    content.append({
        "type": "text",
        "text": prompt,
    })

    messages = [{"role": "user", "content": content}]
    return chat_completion(messages, model=model, max_tokens=max_tokens, temperature=0.3)


def _detect_media_type(image_bytes: bytes) -> str:
    """Detect image MIME type from magic bytes."""
    if image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        return "image/png"
    if image_bytes[:2] == b'\xff\xd8':
        return "image/jpeg"
    if image_bytes[:4] == b'RIFF' and image_bytes[8:12] == b'WEBP':
        return "image/webp"
    if image_bytes[:3] == b'GIF':
        return "image/gif"
    return "image/jpeg"


# ---------------------------------------------------------------------------
# JSON extraction helper
# ---------------------------------------------------------------------------

def extract_json(text: str, expect_array: bool = False) -> Any:
    """Extract JSON from LLM response (handles markdown code blocks)."""
    text = text.strip()

    # Try to extract from code block
    pattern = r'```(?:json)?\s*(\[.*?\])\s*```' if expect_array else r'```(?:json)?\s*(\{.*?\})\s*```'
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return json.loads(match.group(1))

    # Try raw JSON
    start_char = '[' if expect_array else '{'
    if text.startswith(start_char):
        return json.loads(text)

    # Last resort: find the first JSON-like structure
    idx = text.find(start_char)
    if idx >= 0:
        end_char = ']' if expect_array else '}'
        # Find matching end
        depth = 0
        for i in range(idx, len(text)):
            if text[i] == start_char:
                depth += 1
            elif text[i] == end_char:
                depth -= 1
                if depth == 0:
                    return json.loads(text[idx:i+1])

    raise ValueError(f"无法从 LLM 响应中提取 JSON: {text[:200]}")
