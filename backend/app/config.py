"""Central configuration for the image-to-pptx Vision CLI.

Holds LLM connection settings (LiteLLM proxy, OpenAI-compatible), slide geometry,
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
    "PROVIDER",
    "PROVIDERS",
    "LITELLM_BASE_URL",
    "LITELLM_API_KEY",
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
# LLM / LiteLLM proxy settings
# --------------------------------------------------------------------------- #

#: Direct providers: name -> (OpenAI-compatible base URL, that provider's own key
#: variable, default vision model). ``PROVIDER=gemini`` / ``openai`` skips the
#: LiteLLM proxy and talks to the provider with its own key; anything else
#: (unset, ``litellm``) keeps the proxy settings below.
PROVIDERS: dict[str, tuple[str, str, str]] = {
    "gemini": (
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        "GEMINI_API_KEY",
        "gemini-3.8-flash",
    ),
    "openai": ("https://api.openai.com/v1/", "OPENAI_API_KEY", "gpt-4o-mini"),
}

#: Selected direct provider ("" = LiteLLM proxy mode) (env: ``PROVIDER``).
PROVIDER: str = _env_text("PROVIDER", "").lower()
if PROVIDER in ("litellm", "proxy"):
    PROVIDER = ""
if PROVIDER and PROVIDER not in PROVIDERS:
    import sys as _sys

    print(
        f"[config] WARNING: unknown PROVIDER {PROVIDER!r} (use "
        f"{' | '.join(PROVIDERS)}); falling back to the LiteLLM proxy settings.",
        file=_sys.stderr,
    )
    PROVIDER = ""

if PROVIDER:
    _base, _key_var, _default_model = PROVIDERS[PROVIDER]
    #: Base URL of the OpenAI-compatible endpoint.
    LITELLM_BASE_URL: str = _base
    #: The provider's own key (``GEMINI_API_KEY`` / ``OPENAI_API_KEY``).
    LITELLM_API_KEY: str = os.environ.get(_key_var, "")
    #: ``PROVIDER_MODEL`` overrides the provider default.
    DEFAULT_MODEL: str = _env_text("PROVIDER_MODEL", _default_model)
else:
    #: Base URL of the OpenAI-compatible LiteLLM proxy (env: ``LITELLM_BASE_URL``).
    LITELLM_BASE_URL: str = _env_text("LITELLM_BASE_URL", "http://localhost:4000/")

    #: API key presented to the proxy (env: ``LITELLM_API_KEY``). There is
    #: deliberately NO default: a credential does not belong in the source tree,
    #: and a baked-in key also hides a misconfigured environment behind a 401
    #: from the proxy. Empty means "not configured" - :func:`get_client` says so,
    #: by name, while ``--from-json`` never needs a key at all.
    LITELLM_API_KEY: str = os.environ.get("LITELLM_API_KEY", "")

    #: Vision model name routed by the proxy (env: ``VISION_MODEL``). A blank
    #: value falls back to the default - see :func:`_env_text`.
    DEFAULT_MODEL: str = _env_text("VISION_MODEL", "gemini-flash")

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
    """``LITELLM_API_KEY`` is unset or blank, so no client can be built.

    A ``RuntimeError`` so existing callers that catch ``RuntimeError`` (or
    ``Exception``) keep working unchanged; a distinct class so the CLI can
    diagnose it by TYPE instead of by guessing at message text.
    """


#: The one message that explains a missing key, used by the error above.
_MISSING_KEY_HINT = (
    "LITELLM_API_KEY is not set (or is blank), so the LiteLLM proxy cannot be "
    "called.\n"
    "  Fix: export the key in your shell, then retry:\n"
    "    export LITELLM_API_KEY='<your proxy key>'\n"
    "  (see .env.example; keep the key in your environment, never in a file in "
    "this repository)\n"
    "  Direct to Google/OpenAI instead? Set PROVIDER=gemini + GEMINI_API_KEY, or\n"
    "  PROVIDER=openai + OPENAI_API_KEY (no proxy needed).\n"
    "  No key handy? '--from-json SPEC.json' builds a deck with no key and no "
    "network at all."
)


def get_client() -> "openai.OpenAI":
    """Build an OpenAI-compatible client pointed at the LiteLLM proxy.

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

    try:
        import openai
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError(
            "The 'openai' package is required to talk to the LiteLLM proxy. "
            "Install it with: pip install -r requirements.txt"
        ) from exc

    return openai.OpenAI(
        base_url=LITELLM_BASE_URL,
        api_key=api_key,
        timeout=LLM_TIMEOUT,
        max_retries=LLM_MAX_RETRIES,
    )
