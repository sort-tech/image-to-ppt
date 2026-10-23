"""CLI entry point: turn an image into a native, editable .pptx.

usage:
    python cli.py --input sample.png --output result.pptx
    python cli.py --from-json spec.json -o rebuilt.pptx

Exit codes:
    0  success
    1  runtime failure (LLM call, parse, build, save)
    2  bad usage / unsupported input
"""

from __future__ import annotations

import argparse
import errno
import json
import sys
import traceback
from pathlib import Path
from typing import Iterator, Sequence

# A .env beside the repo root is loaded BEFORE app.config, which reads
# os.environ at import time. A real export still wins; the file is optional.
from app.env_file import find_env_file, load_env_file

ENV_FILE = find_env_file()
ENV_APPLIED = load_env_file(ENV_FILE)

from app import config  # noqa: E402  (must follow the .env load)

# Heavier modules (pydantic / python-pptx / openai) are imported lazily inside the
# functions that need them so that `--help`, argparse wiring and validate_input_path
# keep working even when an optional dependency is missing.

__all__ = ["main", "validate_input_path", "CliUsageError", "build_parser"]

PROG = "cli.py"


class CliUsageError(ValueError):
    """Raised for bad invocation / unusable input -> exit code 2."""


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Convert an image (slide capture, document, diagram) into a .pptx made of "
            "real, native, editable PowerPoint shapes, tables and text boxes - never a "
            "single flattened picture."
        ),
        epilog=(
            "examples:\n"
            f"  python {PROG} --input sample.png --output result.pptx\n"
            f"  python {PROG} -i slide.jpg --save-json spec.json -v\n"
            f"  python {PROG} --from-json spec.json -o rebuilt.pptx\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "-i",
        "--input",
        metavar="PATH",
        help="image to analyze (required unless --from-json). "
        "Supported: " + ", ".join(sorted(config.SUPPORTED_EXTENSIONS)),
    )
    source.add_argument(
        "--from-json",
        metavar="PATH",
        dest="from_json",
        help="skip the LLM and build the deck from a saved JSON spec",
    )

    parser.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        help="output .pptx path (default: derived from the input stem, else out.pptx)",
    )
    parser.add_argument(
        "-m",
        "--model",
        metavar="NAME",
        help=f"vision model to use (default: {config.DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--save-json",
        metavar="PATH",
        dest="save_json",
        help="dump the validated spec as JSON for inspection / reuse with --from-json",
    )
    parser.add_argument(
        "--extra",
        metavar="TEXT",
        help="extra instruction appended to the user prompt sent to the model",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="print payload diagnostics and full tracebacks on failure",
    )
    return parser


# --------------------------------------------------------------------------- #
# input validation
# --------------------------------------------------------------------------- #
def _verify_decodable(candidate: Path) -> None:
    """Reject a file that carries an image extension but is not an image.

    The extension check alone passes a truncated download, an HTML error page
    saved as ``.png`` or an arbitrary file that was merely renamed. Without this
    the FIRST thing to notice is the vision model: the whole file gets
    base64-encoded and uploaded (twice, because of the response_format retry),
    and the proxy answers with a generic HTTP 400 that ``_diagnose`` can only
    explain as "the model may not accept image input" - which sends the user
    hunting for the wrong problem. Pillow is already a dependency, so decoding
    the header locally is free and the error can name the real cause.

    Pillow missing is not fatal: the check is skipped rather than blocking a
    conversion that would otherwise work.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is in requirements.txt
        return

    # Only judge a format this Pillow build can actually open. A Pillow
    # compiled without WebP raises the same "cannot identify" error for a
    # perfectly good .webp, and rejecting a file the vision model would have
    # read fine is worse than letting it through.
    try:
        Image.init()
        if candidate.suffix.lower() not in Image.registered_extensions():
            return
    except Exception:  # pragma: no cover - never block on the guard itself
        return

    try:
        with Image.open(candidate) as img:
            img.verify()  # reads/validates the data without decoding pixels
    except (OSError, SyntaxError, ValueError) as exc:
        # UnidentifiedImageError subclasses OSError, so this covers "not an
        # image at all" as well as "image, but corrupt/truncated".
        raise CliUsageError(
            f"'{candidate}' has an image extension but is not a readable image "
            f"({type(exc).__name__}: {exc}).\n"
            "  Fix: make sure the file is a complete, uncorrupted PNG/JPEG/WebP "
            "(re-download or re-export it). To convert another format:\n"
            "    python -c \"from PIL import Image; "
            "Image.open('in.tiff').convert('RGB').save('in.png')\""
        ) from exc


def validate_input_path(path: str) -> Path:
    """Return a resolved Path for a usable input image.

    Raises CliUsageError (a ValueError) when the path is missing, is not a file,
    does not carry a supported image extension, or is not actually a decodable
    image.
    """
    if path is None or str(path).strip() == "":
        raise CliUsageError("No input image given.")

    candidate = Path(str(path)).expanduser()
    if not candidate.exists():
        raise CliUsageError(f"Input image not found: {candidate}")
    if not candidate.is_file():
        raise CliUsageError(f"Input path is not a file: {candidate}")

    suffix = candidate.suffix.lower()
    if suffix not in config.SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(config.SUPPORTED_EXTENSIONS))
        # NOT `suffix or candidate.name`: Path(".png").suffix is '' (POSIX reads
        # a leading-dot name as a name, not a suffix), so quoting the filename
        # produced "Unsupported image type '.png'. Supported extensions: ...,
        # .png, ..." - a sentence that rejects and allows the same thing.
        if suffix:
            raise CliUsageError(
                f"Unsupported image type '{suffix}'. "
                f"Supported extensions: {supported}"
            )
        raise CliUsageError(
            "Unsupported image type '<no extension>'. "
            f"Supported extensions: {supported}. The file name has to END with "
            "one of them ('slide.png', not '.png')."
        )

    _verify_decodable(candidate)
    return candidate.resolve()


def _resolve_output_path(args: argparse.Namespace) -> Path:
    if args.output:
        return Path(args.output).expanduser()
    if args.input:
        return Path(args.input).expanduser().with_suffix(".pptx")
    if args.from_json:
        return Path(args.from_json).expanduser().with_suffix(".pptx")
    return Path("out.pptx")


# --------------------------------------------------------------------------- #
# error reporting helpers
# --------------------------------------------------------------------------- #
def _exception_chain(exc: BaseException | None) -> Iterator[BaseException]:
    seen: set[int] = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _chain_text(exc: BaseException) -> str:
    parts = []
    for item in _exception_chain(exc):
        parts.append(f"{type(item).__name__}: {item}")
    return " | ".join(parts).lower()


_PROJECT_MODULES = frozenset(
    {"config", "schemas", "vision_analyzer", "pptx_builder", "main"}
)
_THIRD_PARTY_HINT = {
    "openai": "openai",
    "pptx": "python-pptx",
    "PIL": "Pillow",
    "pydantic": "pydantic",
}


def _missing_module_name(text: str) -> str | None:
    marker = "no module named"
    index = text.find(marker)
    if index < 0:
        return None
    tail = text[index + len(marker) :].strip()
    name = tail.strip("'\"").split("'")[0].split('"')[0].split()[0] if tail else ""
    return (name.split(".")[0] or None) if name else None


def _first_in_chain(exc: BaseException, *types: type) -> BaseException | None:
    """The first exception in the chain that is an instance of *types*."""
    for item in _exception_chain(exc):
        if isinstance(item, types):
            return item
    return None


#: ``errno`` values that mean "this local path cannot be used", as opposed to
#: the connection errnos an ``OSError`` raised by the network layer carries
#: (``ConnectionRefusedError`` is an ``OSError`` too, and belongs to the proxy
#: diagnosis, not to the output-file one).
_FS_ERRNOS = frozenset(
    value
    for value in (
        getattr(errno, name, None)
        for name in (
            "EACCES",
            "EPERM",
            "EROFS",
            "EISDIR",
            "ENOTDIR",
            "ENOSPC",
            "EDQUOT",
            "ENAMETOOLONG",
            "EEXIST",
            "EFBIG",
            "ETXTBSY",
        )
    )
    if value is not None
)


def _validation_error_types() -> tuple[type, ...]:
    """Exception types that mean "this payload is not a slide spec"."""
    types: list[type] = [json.JSONDecodeError]
    try:  # pydantic is imported lazily everywhere else in this module
        from pydantic import ValidationError

        types.append(ValidationError)
    except Exception:  # pragma: no cover - pydantic is in requirements.txt
        pass
    try:
        from app.schemas import ElementsAllDroppedError

        types.append(ElementsAllDroppedError)
    except Exception:  # pragma: no cover - only if the project is broken
        pass
    return tuple(types)


def _os_failure_hint(exc: BaseException) -> str | None:
    """A hint for a genuine operating-system failure, or ``None``.

    Decided from the exception TYPE (and ``errno``) alone. This has to run
    before anything that searches the message text, because the text contains
    the PATHS involved and a path can hold any substring at all: a real
    ``PermissionError: [Errno 13] ... /tmp/ro/x.json`` used to be reported as
    "the model's answer could not be parsed" purely because the file name ended
    in ``.json``.
    """
    found = _first_in_chain(exc, OSError)
    if found is None:
        return None

    if isinstance(found, FileNotFoundError):
        return (
            "A file in the request does not exist.\n"
            "  Fix: check the -i/--input, --from-json and -o/--output paths."
        )
    if isinstance(found, (PermissionError, IsADirectoryError, NotADirectoryError)) or (
        getattr(found, "errno", None) in _FS_ERRNOS
    ):
        target = getattr(found, "filename", None)
        where = f"\n  Path: {target}" if target else ""
        return (
            "The operating system refused a file operation "
            f"({type(found).__name__}: {found.strerror or found})."
            f"{where}\n"
            "  Fix: pick a writable -o/--output (and --save-json) path, or fix the "
            "permissions / free space on that one."
        )
    return None


def _parse_hint(context: str) -> str:
    """The "this is not a slide spec" hint, worded for the payload's source."""
    if context == "file":
        return (
            "That file is not a valid slide spec.\n"
            "  Fix: it must be JSON shaped like "
            '{"background_color": "#RRGGBB", "elements": [...]} - the kind of file '
            "--save-json writes. Rerun with -v to see which field failed."
        )
    return (
        "The model's answer could not be parsed into a slide spec.\n"
        "  Fix: retry (models are non-deterministic), sharpen the request with "
        "--extra \"...\", or try another --model. Use -v to see the request payload "
        "and the model's raw reply."
    )


def _diagnose(exc: BaseException, context: str = "llm") -> str:
    """Map a failure onto a short, actionable hint for the user.

    'context' selects the wording for parse failures: "llm" blames the model's
    reply, "file" blames a local JSON spec.

    Checks that can be made from the exception TYPE run first and win: message
    text is the last resort, because the text embeds file paths, URLs and model
    names, any of which can contain a needle that belongs to a different
    failure.
    """
    text = _chain_text(exc)

    def hit(*needles: str) -> bool:
        return any(needle in text for needle in needles)

    # ---- by exception type (never fooled by a path or a URL) ------------ #
    if _first_in_chain(exc, getattr(config, "MissingApiKeyError", ())) is not None:
        return getattr(config, "_MISSING_KEY_HINT", "LITELLM_API_KEY is not set.")

    # Missing imports first: "ModuleNotFoundError" would otherwise be mistaken
    # for the proxy's "model not found" response.
    import_error = _first_in_chain(exc, ImportError)
    missing = (
        getattr(import_error, "name", None) if import_error is not None else None
    ) or _missing_module_name(text)
    if missing:
        missing = str(missing).split(".")[0]
        lower = missing.lower()
        if lower in _PROJECT_MODULES:
            return (
                f"The project module '{missing}.py' could not be imported.\n"
                "  Fix: run this CLI from the project root (the directory holding "
                "main.py, config.py, schemas.py, vision_analyzer.py, pptx_builder.py)."
            )
        package = next(
            (pkg for mod, pkg in _THIRD_PARTY_HINT.items() if mod.lower() == lower),
            missing,
        )
        return (
            f"The '{package}' package is not installed.\n"
            "  Fix: pip install -r requirements.txt"
        )

    os_hint = _os_failure_hint(exc)
    if os_hint is not None:
        return os_hint

    if _first_in_chain(exc, *_validation_error_types()) is not None:
        return _parse_hint(context)

    # ---- by message text (API / proxy failures have no local type) ------ #
    if hit(
        "authenticationerror",
        "permissiondenied",
        "unauthorized",
        "invalid api key",
        "incorrect api key",
        "valid api key",
        "api key not valid",
        "api_key_invalid",
        "no api key",
        "401",
        "403",
    ):
        return (
            "The provider rejected the credentials (authentication failure).\n"
            f"  Current key source: {config.KEY_SOURCE} (base URL {config.LITELLM_BASE_URL}).\n"
            f"  Fix: export {config.KEY_SOURCE}=<a valid key> and retry."
        )
    if hit(
        "notfounderror",
        "model not found",
        "model_not_found",
        "invalid model name",
        "invalid model",
        "does not exist",
        "unknown model",
        "no such model",
        "available models",
        "404",
    ):
        return (
            "The model was not found.\n"
            f"  Requested model: check --model / VISION_MODEL (default {config.DEFAULT_MODEL}).\n"
            + (
                "  Fix: list the proxy's models (e.g. curl "
                f"{config.LITELLM_BASE_URL.rstrip('/')}/v1/models) and pass one with --model."
                if not config.PROVIDER
                else f"  Fix: pass a {config.PROVIDER} vision model name with --model."
            )
        )
    if hit("ratelimit", "rate limit", "429", "quota", "insufficient_quota"):
        return (
            "The proxy or upstream provider is rate limiting / out of quota.\n"
            "  Fix: wait a moment and retry, or use a different --model."
        )
    if hit(
        "apiconnectionerror",
        "connectionerror",
        "connection refused",
        "connection error",
        "failed to connect",
        "max retries exceeded",
        "econnrefused",
        "name or service not known",
        "nodename nor servname",
        "temporary failure in name resolution",
        "timed out",
        "timeout",
        "unreachable",
        "getaddrinfo",
    ):
        if config.PROVIDER:
            return (
                f"Could not reach {config.LITELLM_BASE_URL}.\n"
                "  Fix: check this machine's network connection and retry.\n"
                "  Offline? `--from-json SPEC.json` exercises the whole build "
                "path with no network."
            )
        return (
            "Could not reach the LiteLLM proxy.\n"
            f"  Tried: {config.LITELLM_BASE_URL}\n"
            "  Fix: make sure the proxy is actually running and that LITELLM_BASE_URL "
            "points at it (e.g. export LITELLM_BASE_URL=http://localhost:4000/).\n"
            "  No proxy handy? `--from-json SPEC.json` exercises the whole build "
            "path offline."
        )
    if hit("badrequesterror", "invalid_request_error", "400"):
        return (
            "The proxy rejected the request as malformed (HTTP 400).\n"
            f"  Endpoint: {config.LITELLM_BASE_URL}\n"
            "  Fix: the model may not accept image input or JSON mode - try another "
            "--model, and rerun with -v to see the request payload the proxy objected "
            "to (image bytes omitted)."
        )
    # Parse failures, from the text this time - a VisionAnalysisError carries no
    # parse-specific TYPE. Every needle names something only a parse failure
    # says: the bare substring "json" used to live here and matched any PATH
    # ending in ".json", which is how a PermissionError on --save-json
    # /tmp/ro/x.json came out as "the model's answer could not be parsed".
    if hit(
        "no json object",
        "json object",
        "invalid json",
        "jsondecodeerror",
        "no usable slide elements",
        "looks truncated",
        "could not be parsed",
        "validationerror",
        "validation error",
        "expecting value",
        "slide schema",
        "slide spec",
    ):
        return _parse_hint(context)
    if hit("permission denied", "read-only file system", "errno 13"):
        return (
            "The output file could not be written.\n"
            "  Fix: pick a writable -o/--output path."
        )
    return (
        "Unexpected failure.\n"
        "  Fix: rerun with -v for the full traceback; `--from-json SPEC.json` "
        "verifies the local build path without any network."
    )


def _indented(text: str, prefix: str = "  ") -> Iterator[str]:
    """Yield every line of *text* with *prefix*, without double-indenting.

    Both exception messages and diagnosis hints are multi-line and some of their
    lines are already indented, so a naive f-string prefix leaves the block
    ragged (continuation lines flush left, sub-lines over-indented)."""
    for line in text.splitlines():
        yield line if line.startswith(prefix) else f"{prefix}{line}"


def _fail(exc: BaseException, what: str, verbose: bool, context: str = "llm") -> int:
    print(f"{PROG}: error: {what}", file=sys.stderr)
    for line in _indented(f"{type(exc).__name__}: {exc}"):
        print(line, file=sys.stderr)
    for line in _indented(_diagnose(exc, context)):
        print(line, file=sys.stderr)
    if verbose:
        print("--- traceback ---", file=sys.stderr)
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
    else:
        print("  (rerun with -v/--verbose for the full traceback)", file=sys.stderr)
    return 1


def _usage_error(message: str) -> int:
    print(f"{PROG}: error: {message}", file=sys.stderr)
    print(f"  Try: python {PROG} --help", file=sys.stderr)
    return 2


# --------------------------------------------------------------------------- #
# spec sources
# --------------------------------------------------------------------------- #
def _load_spec_from_json(path: Path):
    from app.schemas import SlideSpec, normalize_spec_payload

    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    # Recover an envelope-wrapped spec or a bare element array instead of letting
    # `extra="ignore"` validate it into a silently empty (blank) deck.
    return SlideSpec.model_validate(normalize_spec_payload(payload))


def _spec_to_json(spec) -> str:
    """The validated spec as pretty JSON, even when its text carries junk.

    A lone UTF-16 surrogate (``json.loads('"\\ud800"')`` produces one) has no
    UTF-8 encoding, so pydantic's Rust serializer aborts with a raw
    ``PydanticSerializationError`` and --save-json used to exit 1 on a spec that
    otherwise builds a perfectly good deck. The stdlib encoder can escape such a
    character instead (``ensure_ascii=True``), which keeps the dump writable,
    valid JSON and readable straight back by --from-json.
    """
    try:
        return spec.model_dump_json(indent=2)
    except AttributeError:  # pragma: no cover - pydantic v2 always has this
        return json.dumps(_spec_payload(spec), indent=2, ensure_ascii=False)
    except Exception:  # noqa: BLE001 - unencodable text, see the docstring
        return json.dumps(_spec_payload(spec), indent=2, ensure_ascii=True, default=str)


def _spec_payload(spec):
    try:
        return spec.model_dump()
    except AttributeError:  # pragma: no cover - pydantic v2 always has this
        return spec


def _save_spec_json(spec, path: Path) -> Path:
    target = Path(path).expanduser()
    if target.parent and str(target.parent) not in ("", "."):
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_spec_to_json(spec) + "\n", encoding="utf-8")
    return target.resolve()


# --------------------------------------------------------------------------- #
# summary
# --------------------------------------------------------------------------- #
def _element_counts(spec) -> dict[str, int]:
    counts: dict[str, int] = {}
    for element in getattr(spec, "elements", []) or []:
        kind = getattr(element, "type", None) or type(element).__name__
        counts[str(kind)] = counts.get(str(kind), 0) + 1
    return counts


def _format_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "0 elements"
    preferred = ["shape", "table", "text"]
    ordered = [k for k in preferred if k in counts]
    ordered += sorted(k for k in counts if k not in preferred)
    total = sum(counts.values())
    detail = ", ".join(f"{key}: {counts[key]}" for key in ordered)
    return f"{total} element{'s' if total != 1 else ''} ({detail})"


def _warn_if_blank(spec) -> bool:
    """Say so, loudly, when a spec would produce an empty slide."""
    if getattr(spec, "elements", None):
        return False
    print(
        f"{PROG}: warning: the slide spec contains no elements - the deck will be BLANK.",
        file=sys.stderr,
    )
    print(
        "  Fix: with --from-json, check that the file is shaped like "
        '{"background_color": "#RRGGBB", "elements": [...]}; from an image, re-run '
        "(and -v shows the model's raw reply).",
        file=sys.stderr,
    )
    return True


def _warn_dropped(spec, source: str = "the model's reply") -> int:
    """List the elements the parser could not read; returns how many were lost.

    A partial drop is still a usable deck, so this is a warning on stderr (never
    stdout) and the exit code stays 0 - but it must not be silent, or the user
    compares the deck to the image and wonders where a row went.
    """
    dropped = [str(reason) for reason in (getattr(spec, "dropped", None) or [])]
    if not dropped:
        return 0
    plural = "s" if len(dropped) != 1 else ""
    print(
        f"{PROG}: warning: {len(dropped)} element{plural} skipped "
        f"(unreadable in {source}):",
        file=sys.stderr,
    )
    for reason in dropped:
        print(f"  - {reason}", file=sys.stderr)
    print(
        "  Everything else was built normally. Fix: repair the listed field(s) and "
        "rebuild with --from-json, or re-run the conversion.",
        file=sys.stderr,
    )
    return len(dropped)


def _embedded_picture_count(output: Path) -> int | None:
    """How many pictures the WRITTEN deck really contains, or ``None`` if unknown.

    Read back off the saved file through the shared
    :func:`pptx_builder.flattening_report`, not inferred from the spec: an
    ``image`` element does not always become a picture (no source image to crop
    from, or the anti-flattening pass refused it), and the summary line below is
    a claim about the deck, so it has to come from the deck.

    A deck that was just written successfully must never fail the run because
    this bookkeeping could not be re-read, so every failure degrades to
    ``None`` and the caller simply says less.
    """
    try:
        from pptx import Presentation

        from app.pptx_builder import flattening_report

        return int(flattening_report(Presentation(str(output)))["picture_count"])
    except Exception:  # noqa: BLE001 - a summary line may never break a build
        return None


def _print_summary(output: Path, spec, saved_json: Path | None) -> None:
    counts = _element_counts(spec)
    print(f"Wrote {output}")
    print(f"  {_format_counts(counts)}")
    if saved_json is not None:
        print(f"  spec JSON: {saved_json}")
    if not counts:
        print("  The slide is blank - nothing was rendered.")
        return

    # "All objects are native" is a claim, and since `image` elements exist it is
    # no longer automatically true: a cropped region IS an embedded picture. Say
    # which it is, rather than printing the reassuring sentence over a deck that
    # contains a photo. Everything else on the slide is still native, and that is
    # the part worth promising.
    pictures = _embedded_picture_count(output)
    if pictures is None:
        print("  Shapes, tables and text boxes are native PowerPoint objects - editable.")
    elif pictures == 0:
        print("  All objects are native PowerPoint shapes/tables/text boxes - fully editable.")
    else:
        plural = "s" if pictures != 1 else ""
        print(
            f"  {pictures} cropped image region{plural} embedded as picture{plural}; "
            "every other object is a native PowerPoint shape/table/text box - editable."
        )


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:  # --help / argparse usage errors
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 2

    if not (args.from_json or args.input):
        return _usage_error(
            "no input source. Pass -i/--input IMAGE, or --from-json SPEC.json."
        )

    verbose = bool(args.verbose)
    output_path = _resolve_output_path(args)

    if verbose:
        print(f"[verbose] output      : {output_path}", file=sys.stderr)
        # Names only - a value read from .env is a secret.
        if ENV_FILE is None:
            print("[verbose] .env        : none found", file=sys.stderr)
        else:
            applied = ", ".join(sorted(ENV_APPLIED)) or "nothing (already in the environment)"
            print(f"[verbose] .env        : {ENV_FILE} -> {applied}", file=sys.stderr)
        print(f"[verbose] base url    : {config.LITELLM_BASE_URL}", file=sys.stderr)
        print(f"[verbose] key source  : {config.KEY_SOURCE} ({config.PROVIDER or 'litellm proxy'})", file=sys.stderr)
        print(
            f"[verbose] model       : {args.model or config.DEFAULT_MODEL}",
            file=sys.stderr,
        )
        print(
            f"[verbose] slide size  : {config.SLIDE_WIDTH_IN} x {config.SLIDE_HEIGHT_IN} in",
            file=sys.stderr,
        )

    # ---- 1. obtain a validated SlideSpec -------------------------------- #
    spec = None
    spec_source = "the model's reply"  # names the source in the "skipped" warning
    #: The image the analyzer looked at, for cropping ``image`` elements back out
    #: of. Stays None on the --from-json path, which genuinely has no source
    #: image - those elements are then skipped with a warning by the builder.
    source_image: Path | None = None
    if args.from_json:
        json_path = Path(args.from_json).expanduser()
        if not json_path.exists():
            return _usage_error(f"JSON spec not found: {json_path}")
        if not json_path.is_file():
            return _usage_error(f"JSON spec path is not a file: {json_path}")
        try:
            spec = _load_spec_from_json(json_path)
        except Exception as exc:
            return _fail(
                exc,
                f"could not read a valid slide spec from {json_path}",
                verbose,
                context="file",
            )
        spec_source = str(json_path)
        if verbose:
            print(f"[verbose] source      : {json_path.resolve()}", file=sys.stderr)

    else:
        try:
            image_path = validate_input_path(args.input)
        except CliUsageError as exc:
            return _usage_error(str(exc))
        # This path DOES have the original image, so image elements can really be
        # cropped here; the builder takes a path as readily as bytes.
        source_image = image_path

        if verbose:
            try:
                size = image_path.stat().st_size
                print(
                    f"[verbose] image       : {image_path} ({size} bytes)",
                    file=sys.stderr,
                )
            except OSError:
                print(f"[verbose] image       : {image_path}", file=sys.stderr)

        try:
            from app.vision_analyzer import analyze_image
        except Exception as exc:
            return _fail(exc, "could not load the vision analyzer", verbose)

        print(
            f"Analyzing {image_path.name} with {args.model or config.DEFAULT_MODEL} ...",
            file=sys.stderr,
        )
        try:
            spec = analyze_image(
                image_path,
                model=args.model,
                extra_instructions=args.extra,
                verbose=verbose,
            )
        except Exception as exc:
            return _fail(exc, f"vision analysis of {image_path.name} failed", verbose)

    _warn_if_blank(spec)
    _warn_dropped(spec, spec_source)

    # ---- 2. optionally persist the spec --------------------------------- #
    saved_json: Path | None = None
    if args.save_json:
        try:
            saved_json = _save_spec_json(spec, Path(args.save_json))
        except Exception as exc:
            return _fail(exc, f"could not write the spec JSON to {args.save_json}", verbose)

    # ---- 3. build the deck ---------------------------------------------- #
    try:
        from app.pptx_builder import build_pptx
    except Exception as exc:
        return _fail(exc, "could not load the pptx builder", verbose)

    try:
        written = build_pptx(spec, output_path, source_image=source_image)
    except Exception as exc:
        return _fail(exc, f"could not build {output_path}", verbose)

    _print_summary(Path(written), spec, saved_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
