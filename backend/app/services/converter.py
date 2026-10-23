"""Orchestration seam between the HTTP layer and the conversion core.

This module is deliberately **framework-free** - it imports nothing from
fastapi/starlette - so the whole request pipeline (validate the upload, analyze
it, validate the spec, build the deck, hand back bytes and a preview) can be
exercised without an HTTP client, and so the core modules keep exactly one
caller shape whether they are driven by ``cli.py`` or by ``app.api.routes``.

Three things live here that the route layer deliberately does NOT own:

* **The error map.** Every failure the core can raise is classified by
  EXCEPTION TYPE (never by message text) into the ``(status, code, message,
  hint)`` tuple the HTTP contract pins. See :func:`failure_from_exception`.
* **stderr capture.** ``pptx_builder`` and ``vision_analyzer`` report non-fatal
  problems by printing to ``sys.stderr``, and the API contract requires those
  as a JSON array. They are captured here rather than by changing either
  module's behaviour: the warning stays on stderr for the CLI, and the capture
  is thread-scoped so two concurrent requests cannot steal each other's lines.
* **The download store.** An in-process, TTL'd, capped dict of built decks.

The deck itself NEVER touches the disk: :func:`build_bytes` renders into an
``io.BytesIO`` via ``build_presentation(spec).save(buffer)``, so nothing is
written to a temporary path and nothing has to be cleaned up.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from pydantic import ValidationError

from .. import config, pptx_builder, vision_analyzer
from ..schemas import (
    ElementsAllDroppedError,
    SlideSpec,
    all_dropped_reasons,
    normalize_spec_payload,
)
from . import preview as preview_module

__all__ = [
    "ConversionFailure",
    "ConversionResult",
    "DownloadEntry",
    "DownloadStore",
    "MAX_JSON_BODY_BYTES",
    "MAX_UPLOAD_BYTES",
    "PPTX_MEDIA_TYPE",
    "DOWNLOAD_TTL_SECONDS",
    "DOWNLOAD_MAX_ENTRIES",
    "DEBUG_UPSTREAM_ENV_VAR",
    "build_bytes",
    "build_deck",
    "build_from_payload",
    "capture_stderr",
    "convert_upload",
    "counts_for",
    "display_pptx_filename",
    "downloads",
    "preview_for",
    "failure_from_exception",
    "health_payload",
    "is_download_id",
    "json_body_too_large_failure",
    "new_download_id",
    "parse_json_body",
    "safe_pptx_filename",
    "scrub_secrets",
    "validate_upload_bytes",
]

logger = logging.getLogger("app.services.converter")

#: Hard cap on an uploaded image, in bytes (the contract's 20 MB -> 413).
MAX_UPLOAD_BYTES: int = 20 * 1024 * 1024

#: Hard cap on a JSON request body (POST /api/build-from-json), in bytes.
#: A SlideSpec is kilobytes; anything past this is refused before it is decoded,
#: so a caller cannot make the process buffer (and then re-materialize as python
#: objects) an unbounded body. See :func:`json_body_too_large_failure`.
MAX_JSON_BODY_BYTES: int = 2 * 1024 * 1024

#: The OOXML media type for a .pptx, used for every binary response.
PPTX_MEDIA_TYPE: str = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)

#: How long a built deck stays downloadable.
DOWNLOAD_TTL_SECONDS: float = 30 * 60.0

#: How many built decks are kept at once (oldest evicted first).
DOWNLOAD_MAX_ENTRIES: int = 64

#: Element types always reported in ``counts``, so a client can read
#: ``counts.shape`` without an existence check even when a spec has none.
KNOWN_ELEMENT_TYPES: tuple[str, ...] = ("shape", "table", "text")


# --------------------------------------------------------------------------- #
# secret scrubbing
# --------------------------------------------------------------------------- #
#: Set this env var to a truthy value to keep upstream error text VERBATIM in
#: responses while debugging a proxy problem. It is off by default and has to be
#: set on the server, so a client can never ask for the unredacted text.
DEBUG_UPSTREAM_ENV_VAR: str = "I2P_DEBUG_UPSTREAM_ERRORS"

#: What replaces anything that looks like credential material.
_REDACTED = "[redacted]"

#: An OpenAI/LiteLLM-style key, whole or already truncated ("sk-...1234").
_SECRET_KEY_RE = re.compile(r"sk-[A-Za-z0-9_.\-]*")

#: A hex digest - md5/sha1/sha256. LiteLLM's 401 body carries
#: ``Key Hash (Token) =<sha256 of the whole key>``, which is a verifiable
#: commitment to the secret: an attacker holding a candidate key can confirm it
#: offline. It never leaves the process.
_HEX_DIGEST_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b")

#: ``API Key = ...``, ``Key Hash (Token) =...``, ``token: ...``, ``Bearer ...``.
#: The LABEL is kept (it says what went wrong) and only the value is dropped.
_LABELLED_SECRET_RE = re.compile(
    r"(?i)\b(api[ _-]?keys?|keys?[ _-]?hash|hashed[ _-]?keys?|tokens?"
    r"|authorization|secret|password|passwd)\b"
    r"(\s*\([^)\n]{0,24}\))?\s*[:=]\s*\S+"
)

_BEARER_RE = re.compile(r"(?i)\bbearer\s+\S+")


def _debug_upstream_errors() -> bool:
    """Whether the server operator asked for unredacted upstream text.

    Read at call time (not at import) so a deployment can flip it with a
    restart and so a test can set it for one assertion.
    """
    raw = os.environ.get(DEBUG_UPSTREAM_ENV_VAR, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def scrub_secrets(text: Any) -> str:
    """``text`` with anything that looks like credential material removed.

    Upstream error bodies are NOT trustworthy prose: when LiteLLM rejects a key
    it answers with ``Received API Key = sk-...1234, Key Hash (Token) =<sha256>``,
    and the OpenAI SDK puts that body verbatim into the exception message that
    ``vision_analyzer`` then wraps. Relaying it would hand an unauthenticated
    caller the key's suffix *and* a hash of the whole key, so every string this
    service is about to show a client passes through here first.

    The scrub is deliberately pattern-based rather than an allow-list of known
    upstream shapes, because the next proxy version can word its error however
    it likes. The configured key's own value is also removed literally, so even
    a shape not matched below cannot carry it out.

    Returns the text unchanged only when the operator set
    ``I2P_DEBUG_UPSTREAM_ERRORS`` (see :data:`DEBUG_UPSTREAM_ENV_VAR`).
    """
    if text is None:
        return ""
    value = text if isinstance(text, str) else str(text)
    if not value or _debug_upstream_errors():
        return value

    # The literal key first: it is the one secret whose exact value is known
    # here, so it is removed even if the surrounding text matches nothing.
    key = getattr(config, "LITELLM_API_KEY", "")
    if isinstance(key, str) and len(key.strip()) >= 8:
        value = value.replace(key.strip(), _REDACTED)

    value = _LABELLED_SECRET_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2) or ''} {_REDACTED}", value
    )
    value = _BEARER_RE.sub(f"Bearer {_REDACTED}", value)
    value = _SECRET_KEY_RE.sub(_REDACTED, value)
    value = _HEX_DIGEST_RE.sub(_REDACTED, value)
    return value


# --------------------------------------------------------------------------- #
# failures
# --------------------------------------------------------------------------- #
class ConversionFailure(Exception):
    """A failure already mapped onto the HTTP contract.

    Carries everything the ``{"detail": {code, message, hint}}`` envelope needs.
    Raised by this module so the route layer only has to re-dress it as an
    ``HTTPException`` - no error mapping lives in the routes.

    ``message`` and ``hint`` are :func:`scrub_secrets`-ed HERE, at construction,
    rather than at each call site: this object is the single thing the route
    layer turns into a response body, so scrubbing it once means no future error
    branch can reintroduce a credential leak by interpolating an exception it
    did not write. The raw text is logged by :func:`failure_from_exception`
    instead, where it is useful and stays server-side.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        hint: str = "",
    ) -> None:
        message = scrub_secrets(message)
        hint = scrub_secrets(hint)
        super().__init__(f"{code}: {message}")
        self.status_code = int(status_code)
        self.code = str(code)
        self.message = str(message)
        self.hint = str(hint or "")

    @property
    def detail(self) -> dict[str, str]:
        """The envelope body, exactly as the contract pins it."""
        return {"code": self.code, "message": self.message, "hint": self.hint}


def _chain(exc: BaseException | None) -> Iterator[BaseException]:
    """Walk ``exc`` and its ``__cause__``/``__context__`` chain once each."""
    seen: set[int] = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _first_in_chain(exc: BaseException, *types: type) -> BaseException | None:
    """The first exception in ``exc``'s chain that is one of ``types``."""
    if not types:
        return None
    for link in _chain(exc):
        if isinstance(link, types):
            return link
    return None


_CONNECTION_TYPES: tuple[type, ...] | None = None


def _connection_error_types() -> tuple[type, ...]:
    """Exception types that mean "the proxy could not be reached".

    Resolved lazily and cached: ``openai`` / ``httpx`` are optional at import
    time everywhere else in this project, and this module must not be the one
    place that makes them mandatory. ``openai.APIConnectionError`` is the real
    signal (``APITimeoutError`` subclasses it); the httpx transport errors are a
    safety net for a client that is not the OpenAI SDK.
    """
    global _CONNECTION_TYPES
    if _CONNECTION_TYPES is not None:
        return _CONNECTION_TYPES

    found: list[type] = []
    for module_name, attr_names in (
        ("openai", ("APIConnectionError",)),
        ("anthropic", ("APIConnectionError",)),
        ("httpx", ("TransportError",)),
        ("httpx2", ("TransportError",)),
    ):
        try:
            module = __import__(module_name)
        except Exception:  # pragma: no cover - depends on the environment
            continue
        for attr in attr_names:
            candidate = getattr(module, attr, None)
            if isinstance(candidate, type) and issubclass(candidate, BaseException):
                found.append(candidate)

    _CONNECTION_TYPES = tuple(found)
    return _CONNECTION_TYPES


#: Appended to a relayed upstream error so a scrubbed line does not read as a
#: dead end: the full text is one `docker logs`/journal entry away.
_UPSTREAM_LOG_HINT = (
    "Credential-looking text is redacted here; the server log has the "
    "upstream error in full."
)

_MISSING_KEY_HINT = (
    "Restart the server with a key. "
    + config.KEY_SETUP_HELP
    + "\nPOST /api/build-from-json and POST /api/preview need no key: they build "
    "from a spec you already have."
)


def _proxy_unreachable_hint() -> str:
    base_url = getattr(config, "LITELLM_BASE_URL", "<unset>")
    if getattr(config, "PROVIDER", "litellm") != "litellm":
        return (
            f"{base_url!r} could not be reached. Check this machine's network "
            "connection. POST /api/build-from-json and POST /api/preview need "
            "no network at all."
        )
    return (
        f"LITELLM_BASE_URL is {base_url!r}. Check that the LiteLLM proxy is "
        "running and reachable at that address, or point LITELLM_BASE_URL at "
        "one that is. POST /api/build-from-json and POST /api/preview need no "
        "proxy at all."
    )


def _drop_reasons_hint(reasons: list[str]) -> str:
    listing = "\n".join(f"  - {reason}" for reason in reasons)
    return f"{len(reasons)} element(s) were rejected:\n{listing}"


def _log_upstream(exc: BaseException, *, context: str) -> None:
    """Record the UNREDACTED failure where only the operator can read it.

    The response body is scrubbed (see :func:`scrub_secrets`), which would
    otherwise make a rejected key look like a mystery. The full text - including
    the proxy's own error body - goes to the server log instead, so the
    information is not lost, just not published.
    """
    try:
        # Outside the capture: this call happens INSIDE ``capture_stderr`` (the
        # pipeline wraps the whole conversion in it), and python's default
        # logging handler resolves ``sys.stderr`` when it emits - so without
        # this the one copy of the upstream error would be written into the
        # request's warning buffer and thrown away, leaving the operator with
        # nothing but the redacted line the client got.
        with _uncaptured_stderr():
            logger.warning("%s failed: %s: %s", context, type(exc).__name__, exc)
            logger.debug("%s traceback", context, exc_info=exc)
    except Exception:  # pragma: no cover - logging must never break a response
        pass


def failure_from_exception(
    exc: BaseException, *, context: str = "convert"
) -> ConversionFailure:
    """Classify ``exc`` onto the contract's error codes, by TYPE.

    Message text is never matched against: every branch below keys off an
    imported exception class, so a reworded core error cannot silently change
    an HTTP status code.

    Args:
        exc: the exception the core raised.
        context: ``"convert"`` for the image pipeline (a bad spec came from the
            model, so it is a 502 ``analysis_failed``) or ``"build"`` for
            ``/api/build-from-json`` (a bad spec came from the caller, so it is
            a 400 ``invalid_spec``).
    """
    if isinstance(exc, ConversionFailure):
        return exc

    _log_upstream(exc, context=context)

    # A missing key beats everything else in the chain: it is a configuration
    # problem, and the SDK would otherwise surface it as a connection/auth error.
    missing_key_type = getattr(config, "MissingApiKeyError", None)
    missing_key = (
        _first_in_chain(exc, missing_key_type)
        if isinstance(missing_key_type, type)
        else None
    )
    if missing_key is not None:
        return ConversionFailure(
            503,
            "missing_api_key",
            "LITELLM_API_KEY is not set on the server, so the image cannot be "
            "analyzed.",
            _MISSING_KEY_HINT,
        )

    connection = _first_in_chain(exc, *_connection_error_types())
    if connection is not None:
        return ConversionFailure(
            502,
            "proxy_unreachable",
            "The vision model endpoint could not be reached "
            f"({type(connection).__name__}: {connection}).",
            _proxy_unreachable_hint(),
        )

    if isinstance(exc, pptx_builder.PptxBuildError):
        return ConversionFailure(
            500,
            "build_failed",
            f"The presentation could not be built ({exc}).",
            "The spec validated but python-pptx refused to render it. Retry, or "
            "simplify the offending element.",
        )

    # "Every element was dropped" arrives as a pydantic ValidationError wrapping
    # ElementsAllDroppedError; all_dropped_reasons digs the reasons back out.
    reasons = all_dropped_reasons(exc)
    if isinstance(exc, vision_analyzer.VisionAnalysisError):
        # str(exc) can quote the proxy's own error body verbatim (the OpenAI SDK
        # puts it in the exception message), which is why ConversionFailure
        # scrubs it. _log_upstream above kept the unredacted text server-side.
        return ConversionFailure(
            502,
            "analysis_failed",
            "The vision model replied but its answer could not be used.",
            f"{str(exc).rstrip()}\n{_UPSTREAM_LOG_HINT}",
        )

    if reasons or isinstance(
        exc, (ValidationError, ElementsAllDroppedError, ValueError, RecursionError)
    ):
        if context == "build":
            return ConversionFailure(
                400,
                "invalid_spec",
                "The request body is not a usable SlideSpec.",
                _drop_reasons_hint(reasons) if reasons else str(exc),
            )
        return ConversionFailure(
            502,
            "analysis_failed",
            "The vision model replied but its answer did not match the slide "
            "schema.",
            _drop_reasons_hint(reasons) if reasons else str(exc),
        )

    # The last resort stays INSIDE the contract's error table. Both endpoints
    # pin exactly one 500 - ``build_failed`` - so an exception type nobody
    # anticipated is reported with that code rather than with an `internal_error`
    # no client was ever told to expect. Only the exception's CLASS NAME is
    # shown; its message could be anything, including upstream text.
    if context == "build":
        return ConversionFailure(
            500,
            "build_failed",
            f"The presentation could not be built ({type(exc).__name__}: {exc}).",
            "This is unexpected; retry, and report it if it persists.",
        )
    return ConversionFailure(
        500,
        "build_failed",
        f"The conversion failed unexpectedly ({type(exc).__name__}), so no deck "
        "was built.",
        "This is unexpected; retry, and report it if it persists. The server "
        "log has the details.",
    )


# --------------------------------------------------------------------------- #
# stderr capture (thread-scoped)
# --------------------------------------------------------------------------- #
_PPTX_WARN_PREFIX = "[pptx_builder] warning: "
_VISION_WARN_PREFIX = "[vision_analyzer] "
#: ``preview.py`` reports a field it had to give up on - a crop too large to
#: ship, a picture whose bytes could not be read - the same way every other
#: module reports a non-fatal problem: on stderr. Stripping the prefix here is
#: what puts those lines in the response's ``warnings`` array.
_PREVIEW_WARN_PREFIX = preview_module.WARN_PREFIX

_capture_local = threading.local()
_capture_install_lock = threading.Lock()
_capture_depth = 0
_capture_saved_stderr: Any = None


class _RoutedStderr(io.TextIOBase):
    """A ``sys.stderr`` stand-in that routes per THREAD, not per process.

    ``contextlib.redirect_stderr`` swaps a process-global, so two conversions
    running in fastapi's threadpool would write into each other's buffer (or
    into a buffer that has already been handed back). This object sends a write
    to the calling thread's capture buffer when it has one, and to the real
    stderr otherwise - so an unrelated thread's diagnostics are never swallowed
    while a request is capturing.
    """

    def writable(self) -> bool:  # pragma: no cover - interface completeness
        return True

    def isatty(self) -> bool:
        return False

    def _target(self) -> Any:
        buffer = getattr(_capture_local, "buffer", None)
        if buffer is not None:
            return buffer
        return _capture_saved_stderr or sys.__stderr__

    def write(self, text: str) -> int:
        target = self._target()
        if target is None:  # pragma: no cover - only with a detached stderr
            return len(text)
        return target.write(text)

    def flush(self) -> None:
        target = self._target()
        flush = getattr(target, "flush", None)
        if callable(flush):
            try:
                flush()
            except Exception:  # pragma: no cover - never fail on a flush
                pass


@contextmanager
def capture_stderr() -> Iterator[io.StringIO]:
    """Capture what THIS thread writes to ``sys.stderr`` into a buffer.

    The core modules keep printing to ``sys.stderr`` exactly as they always
    have; only the destination of this thread's writes changes, and only for
    the duration of the block. Nesting is supported, and the original stderr is
    restored once the outermost capture leaves (unless something else swapped it
    meanwhile, which is left alone).
    """
    global _capture_depth, _capture_saved_stderr

    buffer = io.StringIO()
    previous = getattr(_capture_local, "buffer", None)

    with _capture_install_lock:
        if _capture_depth == 0:
            _capture_saved_stderr = sys.stderr
            sys.stderr = _RoutedStderr()
        _capture_depth += 1

    _capture_local.buffer = buffer
    try:
        yield buffer
    finally:
        _capture_local.buffer = previous
        with _capture_install_lock:
            _capture_depth -= 1
            if _capture_depth == 0:
                if isinstance(sys.stderr, _RoutedStderr):
                    sys.stderr = _capture_saved_stderr
                _capture_saved_stderr = None


@contextmanager
def _uncaptured_stderr() -> Iterator[None]:
    """Write to the REAL stderr for the duration, even mid-capture.

    A diagnostic meant for the operator (see :func:`_log_upstream`) must not be
    diverted into a request's warning buffer: that buffer is a RESPONSE field,
    so the line would be published rather than logged, and dropped on a failure
    path where the buffer is discarded.
    """
    previous = getattr(_capture_local, "buffer", None)
    _capture_local.buffer = None
    try:
        yield
    finally:
        _capture_local.buffer = previous


def warning_lines(text: str, *, ignore: list[str] | None = None) -> list[str]:
    """Captured stderr -> the contract's ``warnings`` array.

    Splits into lines, drops blanks and strips the tool prefixes so the client
    sees the message rather than the emitting module. ``ignore`` holds the
    per-element drop reasons already reported in ``dropped``: ``analyze_image``
    echoes each one to stderr, and repeating the same list in two response
    fields is noise, so those exact lines are skipped (everything else the
    analyzer said - including its own "N of M elements" summary - is kept).
    """
    skip = {f"dropped {reason}" for reason in (ignore or ())}
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith(_PPTX_WARN_PREFIX):
            line = line[len(_PPTX_WARN_PREFIX) :]
        elif line.startswith(_VISION_WARN_PREFIX):
            line = line[len(_VISION_WARN_PREFIX) :]
        elif line.startswith(_PREVIEW_WARN_PREFIX):
            line = line[len(_PREVIEW_WARN_PREFIX) :]
        if line in skip:
            continue
        # The second way an upstream error body reaches a client: when the
        # first attempt fails and the retry succeeds, ``vision_analyzer``
        # prints the first failure - proxy error body and all - as a non-fatal
        # warning, and the contract puts that array in a 200 response.
        lines.append(scrub_secrets(line))
    return lines


# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #
@dataclass
class ConversionResult:
    """Everything one conversion produced, ready for the JSON response."""

    spec: SlideSpec
    pptx_bytes: bytes
    dropped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: A render-ready description of the deck that was built, or ``None`` when
    #: it was not asked for (``/api/build-from-json``) or could not be derived
    #: (in which case ``warnings`` carries a line saying so).
    preview: dict[str, Any] | None = None

    @property
    def counts(self) -> dict[str, int]:
        return counts_for(self.spec)

    @property
    def total(self) -> int:
        return len(getattr(self.spec, "elements", None) or [])

    def spec_json(self) -> dict[str, Any]:
        """The validated spec as JSON. ``dropped`` is ``exclude=True``, so it
        is absent by construction - the contract requires that."""
        return self.spec.model_dump(mode="json")


def counts_for(spec: SlideSpec) -> dict[str, int]:
    """``{"shape": 4, "table": 1, "text": 9}`` with the three known types always
    present (zero when a spec has none of them), so a client never has to guard
    against a missing key."""
    raw = spec.counts_by_type() if spec is not None else {}
    counts = {name: int(raw.get(name, 0)) for name in KNOWN_ELEMENT_TYPES}
    for name, value in raw.items():  # a future element type still shows up
        if name not in counts:
            counts[name] = int(value)
    return counts


# --------------------------------------------------------------------------- #
# upload validation
# --------------------------------------------------------------------------- #
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._ -]+")

#: The same idea as :data:`_UNSAFE_FILENAME_CHARS`, but it keeps the letters.
#:
#: Only what could hurt goes: C0/C1 control characters (a CRLF is header
#: injection), path separators, and the punctuation Windows forbids in a name.
#: Hangul, kana, accents and the rest survive, because this value is
#: percent-encoded into ``filename*`` rather than interpolated raw.
_UNSAFE_DISPLAY_FILENAME_CHARS = re.compile(r'[\x00-\x1f\x7f-\x9f"\\/:;*?<>|]+')


def supported_extensions() -> list[str]:
    """The accepted image extensions, sorted - as the contract lists them."""
    return sorted(getattr(config, "SUPPORTED_EXTENSIONS", frozenset()))


def upload_extension(filename: str | None) -> str:
    """The lowercase extension of an UPLOAD's own name.

    The declared content-type is not consulted on purpose: a browser will label
    almost anything ``application/octet-stream``, and a hostile client can claim
    ``image/png`` for a text file. The name the client chose is at least the
    thing the user sees, and Pillow decides whether the bytes agree.
    """
    name = (filename or "").replace("\\", "/")
    return Path(name).suffix.lower()


def _pptx_stem(filename: str | None) -> str:
    """An upload's own stem: no directory part, no ``.pptx``/image suffix."""
    raw = (filename or "").replace("\\", "/")
    stem = Path(raw).name
    if stem.lower().endswith(".pptx"):
        return stem[: -len(".pptx")]
    return Path(stem).stem


def _tidy_stem(stem: str, default: str) -> str:
    """Collapse the separators a substitution leaves behind."""
    stem = re.sub(r"\s+", " ", stem)
    stem = re.sub(r"[\s_]*_[\s_]*", "_", stem)  # "x_ evil_1" -> "x_evil_1"
    stem = stem.strip(" ._-")
    return stem or default


def safe_pptx_filename(filename: str | None, *, default: str = "slide") -> str:
    """A ``*.pptx`` name derived from an upload's stem, safe in a header.

    Path separators, control characters, quotes and non-ASCII are removed rather
    than escaped: this string is interpolated into ``Content-Disposition`` (which
    is latin-1 only and where a stray quote or CRLF would be header injection)
    and it is never used to touch the filesystem.

    Because non-ASCII is *removed*, this alone loses a name written in any
    non-Latin script entirely: ``분기보고서.png`` has no surviving character, so
    it collapses to ``slide.pptx``, and ``매출표_Q3.png`` keeps only ``Q3``.
    That is why :func:`display_pptx_filename` exists and why
    :func:`pptx_headers` sends both.
    """
    stem = _UNSAFE_FILENAME_CHARS.sub("_", _pptx_stem(filename))
    return f"{_tidy_stem(stem, default)[:100]}.pptx"


def display_pptx_filename(filename: str | None, *, default: str = "slide") -> str:
    """The same ``*.pptx`` name, with the upload's own letters kept.

    This is the name the user recognises. It is NEVER interpolated into a header
    as-is - :func:`pptx_headers` percent-encodes it into the RFC 5987
    ``filename*`` parameter, which is UTF-8 and is what browsers prefer - so it
    only has to survive being a filename on the client, not latin-1.
    """
    stem = _UNSAFE_DISPLAY_FILENAME_CHARS.sub("_", _pptx_stem(filename))
    return f"{_tidy_stem(stem, default)[:100]}.pptx"


def _shown_extension(extension: str, filename: str | None) -> str:
    """What the ``unsupported_extension`` message calls the rejected type.

    Never the whole filename: ``Path(".png").suffix`` is ``''`` (POSIX reads a
    leading-dot name as a name, not a suffix), and substituting the filename
    there produced "Unsupported image type '.png'. Supported extensions: .jpeg,
    .jpg, .png, .webp." - a sentence that rejects .png and lists .png as
    supported in the same breath, leaving the user nothing to act on. A file
    with no extension is described as having none, which is the truth and what
    :func:`missing_extension_hint` then explains how to fix.
    """
    if extension:
        return extension
    return "<no extension>" if (filename or "").strip() else "<no filename>"


def missing_extension_hint(listed: str) -> str:
    """What to do about a file whose name carries no extension at all."""
    return (
        "The file name has no extension. Rename it so the name ENDS with one "
        f"of {listed} - 'slide.png', not '.png', which POSIX reads as a name "
        "with no suffix - and upload it again."
    )


def json_body_too_large_failure() -> ConversionFailure:
    """The failure for a JSON body past :data:`MAX_JSON_BODY_BYTES`.

    The code is the endpoint's own ``invalid_spec`` rather than a 413: the
    contract pins exactly two failures for ``/api/build-from-json`` (400
    ``invalid_spec`` and 500 ``build_failed``), and a body this big IS an
    unusable spec. Refusing it before ``json.loads`` is what keeps the peak
    memory of a request bounded - decoding a body materializes it a second and
    third time as python objects.
    """
    megabytes = MAX_JSON_BODY_BYTES / (1024 * 1024)
    return ConversionFailure(
        400,
        "invalid_spec",
        f"The request body is larger than the {megabytes:.0f} MB limit.",
        "A SlideSpec is a few kilobytes of JSON - send the spec alone, not an "
        "embedded image or a whole deck. Use POST /api/convert for image bytes.",
    )


def parse_json_body(raw: bytes | str) -> Any:
    """Decode a request body, mapping EVERY parse failure onto ``invalid_spec``.

    ``RecursionError`` is caught next to ``ValueError`` on purpose: CPython
    raises it (not a ``ValueError``) for a deeply nested document, so guarding
    only ``ValueError`` let a 20 KB body of ``[[[[...]]]]`` escape the route and
    leave as a 500 ``internal_error`` - a code this endpoint's contract does not
    list. Caller-supplied garbage is ``invalid_spec``, however it is malformed.

    Raises:
        ConversionFailure: 400 ``invalid_spec``.
    """
    if len(raw) > MAX_JSON_BODY_BYTES:
        raise json_body_too_large_failure()

    if not raw or not raw.strip():
        raise ConversionFailure(
            400,
            "invalid_spec",
            "The request body is empty.",
            'Send a SlideSpec JSON object, {"spec": {...}}, or a bare array '
            "of elements.",
        )

    try:
        return json.loads(raw)
    except RecursionError:
        # Deliberately not str(exc): "maximum recursion depth exceeded while
        # decoding a JSON array" describes the parser, not the caller's mistake.
        raise ConversionFailure(
            400,
            "invalid_spec",
            "The request body is nested too deeply to parse.",
            "A SlideSpec nests only a few levels deep. Send the spec itself, "
            "not a deeply nested document.",
        ) from None
    except ValueError as exc:
        raise ConversionFailure(
            400,
            "invalid_spec",
            f"The request body is not valid JSON ({exc}).",
            "Send a SlideSpec as application/json.",
        ) from None


def validate_upload_bytes(data: bytes, filename: str | None) -> None:
    """Reject an upload that cannot possibly convert. Raises ConversionFailure.

    Order matters and is the contract's: the extension first (cheapest, and the
    message can name the whole supported list), then emptiness, then the size
    cap, then an actual decode. Anything that gets past here is a real image of
    a supported kind.
    """
    extension = upload_extension(filename)
    allowed = supported_extensions()
    if extension not in allowed:
        listed = ", ".join(allowed)
        raise ConversionFailure(
            400,
            "unsupported_extension",
            f"Unsupported image type '{_shown_extension(extension, filename)}'. "
            f"Supported extensions: {listed}.",
            missing_extension_hint(listed)
            if not extension
            else f"Convert the image to one of {listed} and upload it again.",
        )

    if not data:
        raise ConversionFailure(
            400,
            "empty_file",
            "The uploaded file is empty (0 bytes).",
            "Pick the file again - the upload may have been interrupted.",
        )

    if len(data) > MAX_UPLOAD_BYTES:
        megabytes = MAX_UPLOAD_BYTES / (1024 * 1024)
        # Deliberately NOT "the file is N MB": the reader stops one byte past
        # the cap, so any size reported here would be the cap, not the upload -
        # a message that reads as "20.0 MB is over the 20 MB limit".
        raise ConversionFailure(
            413,
            "file_too_large",
            f"The uploaded file is larger than the {megabytes:.0f} MB limit.",
            "Downscale or re-export the image (a slide screenshot rarely needs "
            "more than 2000 px on its long edge) and upload it again.",
        )

    _verify_decodable(data, extension)


def _verify_decodable(data: bytes, extension: str) -> None:
    """Make Pillow prove the bytes really are an image of that kind.

    Mirrors ``cli._verify_decodable``: without it, an HTML error page saved as
    ``.png`` gets base64-encoded and uploaded to the proxy (twice, because of
    the response_format retry) only to come back as a generic 400 that points
    at the model instead of the file. Pillow missing, or a Pillow built without
    this format, skips the check rather than rejecting a file the model would
    have read fine.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is in requirements.txt
        return

    try:
        Image.init()
        if extension not in Image.registered_extensions():
            return
    except Exception:  # pragma: no cover - never block on the guard itself
        return

    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
    except (OSError, SyntaxError, ValueError) as exc:
        # UnidentifiedImageError subclasses OSError, so this covers "not an
        # image at all" as well as "an image, but corrupt or truncated".
        raise ConversionFailure(
            400,
            "not_an_image",
            f"The file has an image extension but is not a readable image "
            f"({type(exc).__name__}: {_scrub_repr(exc, extension)}).",
            "Make sure the file is a complete, uncorrupted PNG/JPEG/WebP and "
            "upload it again.",
        ) from exc


#: ``<_io.BytesIO object at 0x10a0caf70>`` and friends.
_OBJECT_REPR_RE = re.compile(r"<[^<>]*object at 0x[0-9a-fA-F]+>")


def _scrub_repr(exc: BaseException, extension: str) -> str:
    """Pillow's message, with the in-memory buffer's repr made presentable.

    ``Image.open`` interpolates ``fp`` into its own error text, so an upload
    validated from a ``BytesIO`` produces "cannot identify image file
    <_io.BytesIO object at 0x10a0caf70>" - a heap address in a response body.
    The repr is swapped for a neutral stand-in rather than the client's own
    filename, so nothing is echoed back either.
    """
    return _OBJECT_REPR_RE.sub(f"'upload{extension}'", str(exc)).strip()


def require_api_key() -> None:
    """Raise the contract's 503 when no key is configured.

    Checked BEFORE the image is encoded and uploaded so a misconfigured server
    answers immediately instead of after a two-attempt round trip to the proxy.
    There is no exemption: every path that reaches the model needs a key.
    """
    key = getattr(config, "LITELLM_API_KEY", "")
    if not isinstance(key, str) or not key.strip():
        raise ConversionFailure(
            503,
            "missing_api_key",
            "No API key (GEMINI_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY or "
            "LITELLM_API_KEY) is set on the server, so the vision model cannot be called.",
            _MISSING_KEY_HINT,
        )


def api_key_configured() -> bool:
    """Whether a key is present. Never reveals the key, or any part of it."""
    key = getattr(config, "LITELLM_API_KEY", "")
    return bool(isinstance(key, str) and key.strip())


# --------------------------------------------------------------------------- #
# the pipeline
# --------------------------------------------------------------------------- #
@contextmanager
def _temp_image(data: bytes, extension: str) -> Iterator[Path]:
    """Hand ``data`` to the core as a path, then remove it.

    ``vision_analyzer.encode_image`` takes a path and derives the MIME type from
    the suffix. Rather than re-implement (and risk drifting from) that logic for
    an in-memory upload, the bytes are spilled to a private temporary file with
    the right suffix for exactly the length of the analyze call. The DECK is
    never written to disk - see :func:`build_bytes`.
    """
    handle, name = tempfile.mkstemp(suffix=extension or ".png", prefix="i2p_upload_")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        yield Path(name)
    finally:
        try:
            os.unlink(name)
        except OSError:  # pragma: no cover - already gone
            pass


def build_deck(
    spec: SlideSpec, *, source_image: bytes | str | Path | None = None
) -> tuple[Any, bytes]:
    """Render ``spec`` IN MEMORY, returning the ``Presentation`` AND its bytes.

    ``build_pptx`` only writes to a path, so ``build_presentation`` is used
    directly and saved into a ``BytesIO``: no temporary file, no cleanup, no
    permissions to get wrong, and the bytes can be streamed straight out.

    The live ``Presentation`` comes back so :func:`preview_for` can read the
    geometry and styling python-pptx ACTUALLY produced rather than re-deriving
    it from the spec - which is the only way a preview can show the
    min-dimension floor, the off-slide clipping and the cleaned text that the
    builder applied. The bytes are produced FIRST, deliberately: see
    :func:`preview_for`.

    ``source_image`` is the ORIGINAL uploaded image - bytes here, since that is
    what the HTTP layer holds; the CLI passes a path. The builder needs it to
    crop an ``image`` element's region back out of it. It is keyword-only and
    optional because two of the three entry points genuinely do not have one:
    ``/api/preview`` and ``/api/build-from-json`` are handed a spec and nothing
    else. Passing ``None`` is not a failure - the builder skips each ``image``
    element through its per-element render isolation, which lands a warning
    saying so in the response.
    """
    presentation = pptx_builder.build_presentation(spec, source_image=source_image)
    buffer = io.BytesIO()
    presentation.save(buffer)
    return presentation, buffer.getvalue()


def build_bytes(
    spec: SlideSpec, *, source_image: bytes | str | Path | None = None
) -> bytes:
    """Render ``spec`` to .pptx bytes IN MEMORY, discarding the deck object."""
    return build_deck(spec, source_image=source_image)[1]


def preview_for(presentation: Any) -> tuple[dict[str, Any] | None, str | None]:
    """``(preview, warning)`` - never raises, so a preview cannot fail a build.

    The preview is a convenience on top of the deck, and the deck is the
    product: any failure in the derivation therefore degrades to
    ``(None, "<a warning line>")`` and the caller still answers 200 with the
    real .pptx. The message is scrubbed like every other relayed text.

    Call this only AFTER the bytes have been saved. Reading
    ``slide.background.fill`` makes python-pptx materialize a ``<p:bg>`` element
    on a slide that had none, so deriving the preview first would add 14 bytes
    of inherited-background XML to the deck the user downloads - measured.
    """
    try:
        return preview_module.build_preview(presentation), None
    except Exception as exc:  # noqa: BLE001 - a preview may never break a build
        _log_upstream(exc, context="preview")
        return None, scrub_secrets(
            f"the slide preview could not be derived "
            f"({type(exc).__name__}: {exc}); the .pptx itself is unaffected."
        )


def _analyze(
    data: bytes,
    filename: str | None,
    *,
    model: str | None,
    extra_instructions: str | None,
    client: Any = None,
) -> SlideSpec:
    extension = upload_extension(filename)
    with _temp_image(data, extension) as path:
        return vision_analyzer.analyze_image(
            path,
            model=model,
            client=client,
            extra_instructions=extra_instructions,
        )


def convert_upload(
    data: bytes,
    filename: str | None,
    *,
    model: str | None = None,
    extra_instructions: str | None = None,
    client: Any = None,
) -> ConversionResult:
    """The whole POST /api/convert pipeline, minus HTTP.

    Validate the upload -> analyze it -> build the deck in memory -> describe
    the deck that was built, capturing every non-fatal warning the core printed
    on the way.

    Raises:
        ConversionFailure: always pre-classified onto the contract's codes, so
            the caller never has to interpret a core exception itself.
    """
    validate_upload_bytes(data, filename)
    require_api_key()

    with capture_stderr() as captured:
        try:
            spec = _analyze(
                data,
                filename,
                model=model,
                extra_instructions=extra_instructions,
                client=client,
            )
        except Exception as exc:  # noqa: BLE001 - re-raised as a mapped failure
            raise failure_from_exception(exc, context="convert") from exc

        dropped = [str(reason) for reason in (getattr(spec, "dropped", None) or [])]

        try:
            # The uploaded bytes go down with the spec: an ``image`` element is
            # a REGION OF THIS IMAGE, and the builder crops it back out. This is
            # the only entry point that has the source image at all, which is
            # why /api/preview and /api/build-from-json can only warn about
            # those elements rather than render them.
            presentation, pptx_bytes = build_deck(spec, source_image=data)
        except Exception as exc:  # noqa: BLE001
            raise failure_from_exception(exc, context="build") from exc

        preview, preview_warning = preview_for(presentation)
        warnings = warning_lines(captured.getvalue(), ignore=dropped)

    if preview_warning:
        warnings.append(preview_warning)

    return ConversionResult(
        spec=spec,
        pptx_bytes=pptx_bytes,
        dropped=dropped,
        warnings=warnings,
        preview=preview,
    )


def build_from_payload(payload: Any, *, with_preview: bool = False) -> ConversionResult:
    """The pipeline behind POST /api/build-from-json and POST /api/preview.

    ``normalize_spec_payload`` is reused verbatim, so both endpoints accept the
    same shapes the CLI's ``--from-json`` does: a bare SlideSpec, a spec under a
    wrapper key such as ``{"spec": {...}}``, and a bare element array.

    ``with_preview`` is off by default because ``/api/build-from-json`` answers
    with the binary and would only pay for a payload it then discards.
    ``/api/preview`` asks for it and throws the BYTES away instead - the deck is
    still built either way, which is what makes the preview's geometry real.

    Neither endpoint has the source image: a spec is all that was sent. So
    ``build_deck`` is called WITHOUT ``source_image``, and an ``image`` element
    in the payload cannot be cropped. That is not made into a failure and it is
    not hidden either - the builder skips the element and warns, and the warning
    travels out in ``warnings`` like any other, saying that the conversion has
    to be re-run for the crop. Everything else in the spec still builds.

    Raises:
        ConversionFailure: 400 ``invalid_spec`` or 500 ``build_failed``.
    """
    with capture_stderr() as captured:
        try:
            spec = SlideSpec.model_validate(normalize_spec_payload(payload))
        except Exception as exc:  # noqa: BLE001
            raise failure_from_exception(exc, context="build") from exc

        dropped = [str(reason) for reason in (getattr(spec, "dropped", None) or [])]

        try:
            presentation, pptx_bytes = build_deck(spec)
        except Exception as exc:  # noqa: BLE001
            raise failure_from_exception(exc, context="build") from exc

        preview: dict[str, Any] | None = None
        preview_warning: str | None = None
        if with_preview:
            preview, preview_warning = preview_for(presentation)

        warnings = warning_lines(captured.getvalue(), ignore=dropped)

    if preview_warning:
        warnings.append(preview_warning)

    return ConversionResult(
        spec=spec,
        pptx_bytes=pptx_bytes,
        dropped=dropped,
        warnings=warnings,
        preview=preview,
    )


# --------------------------------------------------------------------------- #
# the download store
# --------------------------------------------------------------------------- #
#: A uuid4 in hex: 32 lowercase hex digits, version nibble 4, variant 8/9/a/b.
_DOWNLOAD_ID_RE = re.compile(
    r"\A[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}\Z"
)


def new_download_id() -> str:
    return uuid.uuid4().hex


def is_download_id(value: Any) -> bool:
    """Whether ``value`` is a uuid4 hex string, checked BEFORE any lookup.

    The id arrives from the URL path, so it is validated in full (shape *and*
    uuid version) rather than merely quoted. It is only ever a dict key - it is
    never joined onto a path, opened, or interpolated into anything - but a
    strict gate keeps it that way even if this store ever grows a disk backend.
    """
    if not isinstance(value, str) or len(value) != 32:
        return False
    if _DOWNLOAD_ID_RE.match(value) is None:
        return False
    try:
        return uuid.UUID(hex=value).version == 4
    except (ValueError, AttributeError, TypeError):  # pragma: no cover
        return False


@dataclass(frozen=True)
class DownloadEntry:
    """One built deck, waiting to be fetched."""

    file_id: str
    filename: str
    data: bytes
    counts: dict[str, int]
    dropped_count: int
    created_at: float

    @property
    def size(self) -> int:
        return len(self.data)


class DownloadStore:
    """In-process, TTL'd, capped store of built decks.

    * keys are uuid4 hex, generated here (never supplied by a client);
    * an entry expires ``ttl`` seconds after it was stored;
    * at most ``max_entries`` live at once, the OLDEST evicted first;
    * :meth:`get` does NOT consume - re-downloading the same id works for as
      long as the entry lives, which is what a browser's "save again" does.

    Deliberately memory-only: decks are already built in memory and a restart is
    allowed to forget them (the client re-runs the conversion). A lock guards
    every mutation because fastapi serves sync endpoints from a threadpool.
    """

    def __init__(
        self,
        ttl: float = DOWNLOAD_TTL_SECONDS,
        max_entries: int = DOWNLOAD_MAX_ENTRIES,
    ) -> None:
        self.ttl = float(ttl)
        self.max_entries = max(1, int(max_entries))
        self._entries: "OrderedDict[str, DownloadEntry]" = OrderedDict()
        self._lock = threading.Lock()

    # -- internals ---------------------------------------------------------- #
    def _expired(self, entry: DownloadEntry, now: float) -> bool:
        return (now - entry.created_at) > self.ttl

    def _purge(self, now: float) -> None:
        for file_id in [
            key for key, entry in self._entries.items() if self._expired(entry, now)
        ]:
            self._entries.pop(file_id, None)

    # -- api ---------------------------------------------------------------- #
    def put(
        self,
        data: bytes,
        filename: str,
        *,
        counts: dict[str, int] | None = None,
        dropped_count: int = 0,
        file_id: str | None = None,
    ) -> str:
        """Store ``data`` and return its download id."""
        now = time.monotonic()
        key = file_id or new_download_id()
        entry = DownloadEntry(
            file_id=key,
            filename=filename,
            data=bytes(data),
            counts=dict(counts or {}),
            dropped_count=int(dropped_count),
            created_at=now,
        )
        with self._lock:
            self._purge(now)
            self._entries.pop(key, None)
            self._entries[key] = entry
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)  # oldest first
        return key

    def get(self, file_id: Any) -> DownloadEntry | None:
        """The entry for ``file_id``, or ``None`` if unknown/expired/malformed.

        Non-consuming, and it re-checks :func:`is_download_id` so a caller that
        forgets to validate still cannot reach the dict with arbitrary input.
        """
        if not is_download_id(file_id):
            return None
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            entry = self._entries.get(file_id)
            if entry is None:
                return None
            if self._expired(entry, now):  # pragma: no cover - purged above
                self._entries.pop(file_id, None)
                return None
            return entry

    def __len__(self) -> int:
        with self._lock:
            self._purge(time.monotonic())
            return len(self._entries)

    def ids(self) -> list[str]:
        """Live ids, oldest first (diagnostics and tests)."""
        with self._lock:
            self._purge(time.monotonic())
            return list(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


#: The process-wide store the routes use.
downloads = DownloadStore()


# --------------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------------- #
def health_payload() -> dict[str, Any]:
    """The GET /api/health body.

    Configuration only: it never calls the proxy, so it stays 200 while the
    proxy is down (that is what makes it usable as a liveness probe). The key
    itself is reduced to a single bool here - no prefix, no length, no hint of
    its value ever leaves the process.
    """
    return {
        "status": "ok",
        "proxy": {
            "base_url": getattr(config, "LITELLM_BASE_URL", ""),
            "model": getattr(config, "DEFAULT_MODEL", ""),
            "api_key_configured": api_key_configured(),
        },
        "supported_extensions": supported_extensions(),
        "slide": {
            "width_in": getattr(config, "SLIDE_WIDTH_IN", 0.0),
            "height_in": getattr(config, "SLIDE_HEIGHT_IN", 0.0),
            "coord_scale": getattr(config, "COORD_SCALE", 0),
        },
    }


def pptx_headers(
    filename: str, counts: dict[str, int] | None, dropped_count: int
) -> dict[str, str]:
    """The headers every .pptx response carries.

    ``X-Element-Counts`` lets the UI report what it just downloaded without a
    second round trip, which is the whole reason it exists in the contract.

    ``Content-Disposition`` carries the latin-1 ``filename=`` always, and adds
    RFC 5987's ``filename*=UTF-8''...`` only when the name actually has
    characters that ``filename=`` cannot spell. Without it every upload named in
    Hangul (or any non-Latin script) downloaded as ``slide.pptx``; with it the
    browser - which prefers ``filename*`` - saves ``분기보고서.pptx``, while a
    client that only understands ``filename=`` still gets a valid ASCII name.
    Emitting it only when it adds information keeps the header byte-identical
    for the ASCII uploads that are the common case.

    ``filename`` may already be a display name (the download store keeps one):
    both parameters are re-derived here rather than trusted, so this is the one
    place either form is produced and neither caller can get it wrong.
    """
    safe = safe_pptx_filename(filename)
    disposition = f'attachment; filename="{safe}"'
    shown = display_pptx_filename(filename)
    if not shown.isascii():
        # quote(safe="") percent-encodes every reserved character, so nothing
        # here can introduce a quote, a semicolon or a CRLF into the header.
        disposition += f"; filename*=UTF-8''{quote(shown, safe='')}"
    return {
        "Content-Disposition": disposition,
        "X-Element-Counts": json.dumps(counts or {}, separators=(",", ":")),
        "X-Dropped-Count": str(int(dropped_count)),
    }
