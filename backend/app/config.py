"""Central configuration for the image-to-pptx Vision CLI.

Holds LLM connection settings (a LiteLLM proxy, or Gemini / OpenAI directly -
all OpenAI-compatible), slide geometry,
the normalized coordinate scale used by the vision schema, supported input image
formats and rendering defaults.

This module is intentionally dependency-free at import time: ``openai`` is imported
lazily inside :func:`get_client` so that every other module (and the test suite) can
import ``config`` even when the OpenAI SDK is not installed.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, never executed at runtime
    import openai


def _env_text(name: str, default: str) -> str:
    """An environment variable as text, falling back when it is BLANK too.

    ``os.environ.get(name, default)`` falls back only when the variable is
    absent. A hand-edited ``.env`` line like ``VISION_MODEL=`` is present and
    empty, and the project's ``.env`` loader copies exactly that into the
    environment - so the documented default became unreachable and the blank
    travelled on: the proxy answers ``Invalid model name passed in model=``, and
    ``/api/health`` reported an empty model, which the UI's status pill - the
    only place the model is named now that there is no picker - rendered as a
    dangling separator. Whitespace is stripped for the same reason the route
    strips the per-request ``model``: "   " is not a model name either.

    House rule: configuration falls back rather than breaks.
    """
    return (os.environ.get(name) or "").strip() or default

__all__ = [
    "LITELLM_BASE_URL",
    "LITELLM_API_KEY",
    "KEY_SOURCE",
    "PROVIDER",
    "KEY_SETUP_HELP",
    "MissingApiKeyError",
    "DEFAULT_MODEL",
    "LLM_TIMEOUT",
    "LLM_MAX_RETRIES",
    "SLIDE_WIDTH_IN",
    "SLIDE_HEIGHT_IN",
    "COORD_SCALE",
    "SUPPORTED_EXTENSIONS",
    "MIME_BY_EXTENSION",
    "DEFAULT_TEXT_FONT_SIZE",
    "DEFAULT_TABLE_FONT_SIZE",
    "DEFAULT_BORDER_WIDTH_PT",
    "get_client",
]

# --------------------------------------------------------------------------- #
# LLM settings: a LiteLLM proxy, or Gemini / OpenAI directly
# --------------------------------------------------------------------------- #

#: Where a key can come from, first match wins:
#: ``(env var, provider, base URL, default model)``. The proxy comes first so an
#: existing LiteLLM setup behaves exactly as before; the other two are there so
#: that trying the project needs one provider key and no proxy at all - both
#: providers speak the OpenAI chat API this client already uses. A base URL of
#: ``None`` means "read ``LITELLM_BASE_URL``".
_KEY_SOURCES: tuple[tuple[str, str, str | None, str], ...] = (
    ("LITELLM_API_KEY", "litellm", None, "gemini-flash"),
    (
        "GEMINI_API_KEY",
        "gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        "gemini-3.8-flash",
    ),
    ("OPENAI_API_KEY", "openai", "https://api.openai.com/v1/", "gpt-4.1-mini"),
    # Claude goes through the official ``anthropic`` SDK, not a compatibility
    # endpoint - see app/anthropic_client.py.
    ("ANTHROPIC_API_KEY", "anthropic", "https://api.anthropic.com/", "claude-opus-5-5"),
)


def _resolve_key_source() -> tuple[str, str, str, str, str]:
    """``(env var, provider, key, base URL, default model)`` for this process.

    With no key anywhere the proxy is reported, key empty, so every "not
    configured" message keeps naming the variables to set.
    """
    for name, provider, base_url, model in _KEY_SOURCES:
        key = (os.environ.get(name) or "").strip()
        if key:
            break
    else:
        name, provider, base_url, model = _KEY_SOURCES[0]
        key = ""
    if base_url is None:
        # Only the proxy reads LITELLM_BASE_URL: a stale proxy URL left in .env
        # must not send a Gemini key to localhost:4000.
        base_url = _env_text("LITELLM_BASE_URL", "http://localhost:4000/")
    elif provider == "anthropic":
        # The anthropic SDK reads ANTHROPIC_BASE_URL itself; mirror it so the
        # endpoint /api/health and the error hints name is the one called.
        base_url = _env_text("ANTHROPIC_BASE_URL", base_url)
    return name, provider, key, base_url, model


(KEY_SOURCE, PROVIDER, LITELLM_API_KEY, LITELLM_BASE_URL, _PROVIDER_MODEL) = (
    _resolve_key_source()
)
#: ``KEY_SOURCE`` is the variable that supplied the key and ``PROVIDER`` one of
#: ``litellm`` / ``gemini`` / ``openai`` / ``anthropic``. ``LITELLM_API_KEY`` and
#: ``LITELLM_BASE_URL`` keep their names for every caller, but hold the
#: EFFECTIVE key and endpoint, whichever source supplied them. There is
#: deliberately NO default key: a credential does not belong in the source
#: tree, and a baked-in key also hides a misconfigured environment behind a
#: 401. Empty means "not configured" - :func:`get_client` says so, by name,
#: while ``--from-json`` never needs a key at all.

#: Vision model name (env: ``VISION_MODEL``): an alias the proxy routes, or the
#: provider's own model name when calling Gemini / OpenAI directly. A blank
#: value falls back to the provider's default - see :func:`_env_text`.
DEFAULT_MODEL: str = _env_text("VISION_MODEL", _PROVIDER_MODEL)

#: The ways to configure a key, for every "no key" message to quote.
KEY_SETUP_HELP = (
    "Set ONE of these in the environment (or in .env, git-ignored):\n"
    "    export GEMINI_API_KEY='<your Gemini key>'    # https://aistudio.google.com/apikey\n"
    "    export OPENAI_API_KEY='<your OpenAI key>'\n"
    "    export ANTHROPIC_API_KEY='<your Anthropic key>'\n"
    "    export LITELLM_API_KEY='<your proxy key>'    # a LiteLLM proxy at LITELLM_BASE_URL"
)

#: Per-request timeout in seconds. Vision calls on large images are slow.
LLM_TIMEOUT: float = 120.0

#: How many times the SDK itself retries a failed request.
LLM_MAX_RETRIES: int = 2

# --------------------------------------------------------------------------- #
# Slide geometry and coordinate system
# --------------------------------------------------------------------------- #

#: Slide width in inches (16:9 widescreen).
SLIDE_WIDTH_IN: float = 13.333

#: Slide height in inches (16:9 widescreen).
SLIDE_HEIGHT_IN: float = 7.5

#: The vision model reports bounding boxes as integers in ``0..COORD_SCALE`` on
#: both axes; the builder maps that range onto the slide dimensions above.
COORD_SCALE: int = 1000

# --------------------------------------------------------------------------- #
# Input image handling
# --------------------------------------------------------------------------- #

#: Image extensions the CLI accepts (lowercase, leading dot).
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".webp"})

#: Extension -> MIME type, used to build the ``data:`` URL for the Vision payload.
MIME_BY_EXTENSION: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

# --------------------------------------------------------------------------- #
# Rendering defaults
# --------------------------------------------------------------------------- #

#: Fallback font size (pt) for text boxes when the model gives none.
DEFAULT_TEXT_FONT_SIZE: int = 12

#: Fallback font size (pt) for table cells. Deliberately compact: python-pptx
#: tables cannot shrink below their content, so a large font makes a table
#: outgrow the slide.
DEFAULT_TABLE_FONT_SIZE: int = 10

#: Fallback shape border width in points.
DEFAULT_BORDER_WIDTH_PT: float = 1.0


class MissingApiKeyError(RuntimeError):
    """No key is configured (see :data:`KEY_SETUP_HELP`), so no client can be built.

    A ``RuntimeError`` so existing callers that catch ``RuntimeError`` (or
    ``Exception``) keep working unchanged; a distinct class so the CLI can
    diagnose it by TYPE instead of by guessing at message text.
    """


#: The one message that explains a missing key, used by the error above.
_MISSING_KEY_HINT = (
    "No API key is set (GEMINI_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY or "
    "LITELLM_API_KEY), so "
    "the vision model cannot be called.\n"
    "  Fix: " + KEY_SETUP_HELP.replace("\n", "\n  ") + "\n"
    "  (see .env.example; keep the key in your environment, never in a file "
    "committed to this repository)\n"
    "  No key handy? '--from-json SPEC.json' builds a deck with no key and no "
    "network at all."
)


def get_client() -> "openai.OpenAI":
    """Build an OpenAI-compatible client for the configured provider.

    ``openai`` is imported here rather than at module scope so that importing
    ``config`` never fails when the SDK is missing.

    The key is read from :data:`LITELLM_API_KEY` at call time (not at import
    time) so that a test or a caller can set it on the module and get a client
    that uses it.

    Raises:
        MissingApiKeyError: if ``LITELLM_API_KEY`` is unset or blank. Passing an
            empty key to the SDK would either raise an opaque ``OpenAIError``
            ("api_key client option must be set") or reach the proxy and come
            back as a bare 401 - neither of which names the variable to set.
        RuntimeError: if the ``openai`` package is not installed.
    """
    api_key = LITELLM_API_KEY
    if not isinstance(api_key, str) or not api_key.strip():
        raise MissingApiKeyError(_MISSING_KEY_HINT)

    if PROVIDER == "anthropic":
        try:
            from .anthropic_client import AnthropicChatClient

            # Adaptive thinking on a vision call can outlast the 120 s that
            # suits the other providers.
            return AnthropicChatClient(
                api_key=api_key,
                timeout=max(LLM_TIMEOUT, 300.0),
                max_retries=LLM_MAX_RETRIES,
            )
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise RuntimeError(
                "The 'anthropic' package is required for ANTHROPIC_API_KEY. "
                "Install it with: pip install -r requirements.txt"
            ) from exc

    try:
        import openai
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError(
            "The 'openai' package is required to talk to the vision model. "
            "Install it with: pip install -r requirements.txt"
        ) from exc

    return openai.OpenAI(
        base_url=LITELLM_BASE_URL,
        api_key=api_key,
        timeout=LLM_TIMEOUT,
        max_retries=LLM_MAX_RETRIES,
    )
