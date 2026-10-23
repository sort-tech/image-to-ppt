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
    "PROVIDERS",
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
# LLM settings: a LiteLLM proxy, or Gemini / OpenAI / Claude directly
# --------------------------------------------------------------------------- #

#: Direct providers: name -> (base URL, that provider's own key variable,
#: default vision model). Gemini and OpenAI speak the OpenAI chat API this
#: client already uses; Claude goes through the official ``anthropic`` SDK, not
#: a compatibility endpoint - see app/anthropic_client.py.
PROVIDERS: dict[str, tuple[str, str, str]] = {
    "gemini": (
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        "GEMINI_API_KEY",
        "gemini-3.8-flash",
    ),
    "openai": ("https://api.openai.com/v1/", "OPENAI_API_KEY", "gpt-4o-mini"),
    "anthropic": ("https://api.anthropic.com/", "ANTHROPIC_API_KEY", "claude-opus-5-5"),
}


def _resolve_provider() -> tuple[str, str, str]:
    """``(provider, key variable, how it was chosen)``; provider ``""`` = proxy.

    ``PROVIDER=gemini|openai|anthropic`` picks one explicitly and
    ``PROVIDER=litellm`` forces the proxy. Unset, the first key present wins -
    ``LITELLM_API_KEY`` first, so an existing proxy setup is never redirected by
    a provider key exported for some other tool - and one key is all a
    first-time user has to set.
    """
    chosen = _env_text("PROVIDER", "").lower()
    if chosen in ("litellm", "proxy"):
        return "", "LITELLM_API_KEY", "PROVIDER"
    if chosen in PROVIDERS:
        return chosen, PROVIDERS[chosen][1], "PROVIDER"
    if chosen:
        import sys as _sys

        print(
            f"[config] WARNING: unknown PROVIDER {chosen!r} (use "
            f"{' | '.join(PROVIDERS)} | litellm); picking by key instead.",
            file=_sys.stderr,
        )
    if (os.environ.get("LITELLM_API_KEY") or "").strip():
        return "", "LITELLM_API_KEY", "key"
    for name, (_base, key_var, _model) in PROVIDERS.items():
        if (os.environ.get(key_var) or "").strip():
            return name, key_var, "key"
    return "", "LITELLM_API_KEY", "none"


#: Selected direct provider, ``""`` = LiteLLM proxy mode (env: ``PROVIDER``,
#: else picked by key). ``KEY_SOURCE`` names the variable the key comes from.
PROVIDER, KEY_SOURCE, _CHOSEN_BY = _resolve_provider()

if PROVIDER:
    _base, _key_var, _default_model = PROVIDERS[PROVIDER]
    #: Base URL of the endpoint. Only the proxy reads LITELLM_BASE_URL: a stale
    #: proxy URL left in .env must not receive a provider key. The anthropic
    #: SDK reads ANTHROPIC_BASE_URL itself; it is mirrored so /api/health and
    #: the error hints name the endpoint really called.
    LITELLM_BASE_URL: str = (
        _env_text("ANTHROPIC_BASE_URL", _base) if PROVIDER == "anthropic" else _base
    )
    #: The provider's own key (``GEMINI_API_KEY`` / ``OPENAI_API_KEY`` /
    #: ``ANTHROPIC_API_KEY``).
    LITELLM_API_KEY: str = os.environ.get(_key_var, "")
    #: ``PROVIDER_MODEL`` overrides the provider default. ``VISION_MODEL`` is
    #: the proxy's alias and is deliberately NOT read here: a stale
    #: ``VISION_MODEL=gemini-flash`` in .env would be sent to Google as is.
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

#: The ways to configure a key, for every "no key" message to quote.
KEY_SETUP_HELP = (
    "Set ONE of these in the environment (or in .env, git-ignored):\n"
    "    export GEMINI_API_KEY='<your Gemini key>'    # https://aistudio.google.com/apikey\n"
    "    export OPENAI_API_KEY='<your OpenAI key>'\n"
    "    export ANTHROPIC_API_KEY='<your Anthropic key>'\n"
    "    export LITELLM_API_KEY='<your proxy key>'    # a LiteLLM proxy at LITELLM_BASE_URL\n"
    "  With several set, PROVIDER=gemini|openai|anthropic|litellm picks one."
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
