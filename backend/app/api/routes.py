"""The ``/api`` routes.

This layer is deliberately thin. It speaks HTTP - multipart parsing, status
codes, streaming, headers - and delegates every decision about *meaning* to
``app.services.converter``: the conversion pipeline, the error map and the
download store all live there, so the same behaviour is reachable (and testable)
without a client.

Every error path raises ``HTTPException`` with a ``dict`` detail, which
``app.main``'s handler renders as the contract's one envelope:
``{"detail": {"code": ..., "message": ..., "hint": ...}}``.

The two endpoints that do real work are ``async def`` because they read the
request body themselves (``UploadFile.read``, ``Request.body``), but the
pipeline they then call is fully BLOCKING: a vision call is ~10 s of network
wait and ``python-pptx`` is CPU-bound. Calling it inline would run it ON THE
EVENT LOOP and stall every other request in the process for that whole time, so
each blocking call is handed to the threadpool with
:func:`starlette.concurrency.run_in_threadpool`. That is also the execution
model ``converter.capture_stderr`` is built for: its capture buffer is
thread-scoped precisely so two conversions in the threadpool cannot steal each
other's warnings.
"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from ..services import converter
from ..services.converter import ConversionFailure

__all__ = ["router", "http_error"]

router = APIRouter(prefix="/api", tags=["convert"])

#: How much of an upload is read at a time while enforcing the size cap.
_CHUNK_BYTES = 1024 * 1024


def http_error(failure: ConversionFailure) -> HTTPException:
    """A pre-classified failure as the HTTPException the contract describes."""
    return HTTPException(status_code=failure.status_code, detail=failure.detail)


def _fail(failure: ConversionFailure) -> "HTTPException":
    return http_error(failure)


def _clean_text(value: str | None) -> str | None:
    """A blank/whitespace-only form field means "not supplied"."""
    if value is None:
        return None
    text = value.strip()
    return text or None


async def _read_json_body_capped(request: Request) -> bytes:
    """Read a JSON body, refusing anything past the JSON cap.

    ``await request.body()`` would buffer the WHOLE body first, and
    ``json.loads`` then materializes it again as python objects, so an
    unbounded body meant a request could cost several times its own size in
    resident memory (measured: a 150 MB body -> ~500 MB peak RSS) and still
    answer 200. The declared ``Content-Length`` is checked first so an honest
    client is refused before it uploads anything, and the stream is counted as
    it arrives so a chunked body cannot dodge the cap by omitting the header.
    """
    limit = converter.MAX_JSON_BODY_BYTES
    declared = (request.headers.get("content-length") or "").strip()
    if declared.isdigit() and int(declared) > limit:
        raise _fail(converter.json_body_too_large_failure()) from None

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        size += len(chunk)
        if size > limit:
            # Stop reading: the rest of the body is never buffered.
            raise _fail(converter.json_body_too_large_failure()) from None
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_capped(upload: UploadFile) -> bytes:
    """Read the upload, stopping one byte past the cap.

    Reading in chunks means a 500 MB body is refused after 20 MB rather than
    after it has all been buffered; the extra byte is what lets
    ``validate_upload_bytes`` see that the limit was actually exceeded.
    """
    limit = converter.MAX_UPLOAD_BYTES
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await upload.read(_CHUNK_BYTES)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            break
    return b"".join(chunks)[: limit + 1]


def _pptx_response(
    data: bytes, filename: str, counts: dict[str, int], dropped_count: int
) -> Response:
    """A .pptx body with the contract's headers.

    ``Response`` (not ``StreamingResponse``) because the deck is already one
    complete in-memory blob: starlette sends it in one pass and sets an accurate
    ``Content-Length``, which a stream of a known-length buffer would lose.
    """
    return Response(
        content=data,
        media_type=converter.PPTX_MEDIA_TYPE,
        headers=converter.pptx_headers(filename, counts, dropped_count),
    )


# --------------------------------------------------------------------------- #
# GET /api/health
# --------------------------------------------------------------------------- #
@router.get("/health", summary="Configuration and readiness report")
def health() -> JSONResponse:
    """Report how the server is configured.

    Never fails because the proxy is down: nothing here makes a network call, so
    a 200 means "this process is up and this is how it is wired", which is what
    a health check should answer. ``proxy.api_key_configured`` is a bare bool -
    the key, and every prefix of it, stays inside the process.
    """
    return JSONResponse(content=converter.health_payload())


# --------------------------------------------------------------------------- #
# POST /api/convert
# --------------------------------------------------------------------------- #
@router.post("/convert", summary="Image -> native editable slide")
async def convert(
    file: UploadFile = File(..., description="PNG / JPEG / WebP slide image"),
    model: str | None = Form(None, description="Vision model; defaults to the server's"),
    extra_instructions: str | None = Form(None, description="Extra prompt guidance"),
) -> JSONResponse:
    """Convert an uploaded image into a SlideSpec, a preview and a download."""
    data = await _read_capped(file)

    try:
        # Off the event loop: this blocks for the length of the vision call.
        result = await run_in_threadpool(
            converter.convert_upload,
            data,
            file.filename,
            model=_clean_text(model),
            extra_instructions=_clean_text(extra_instructions),
        )
    except ConversionFailure as failure:
        raise _fail(failure) from None

    # The DISPLAY name: the upload's own characters, path parts and anything
    # structural removed. It is what the UI shows and what the store keeps,
    # because ``pptx_headers`` needs those characters to emit RFC 5987's
    # ``filename*`` - and it derives the latin-1 ``filename=`` itself, which is
    # the only place the ASCII-only form is required. Sanitizing to ASCII here
    # instead threw the characters away before either was built, so a Hangul
    # upload was called "slide.pptx" in the panel AND in the saved file.
    # Nothing downstream needs latin-1: this travels as UTF-8 JSON and ends up
    # in an `<a download>` attribute.
    filename = converter.display_pptx_filename(file.filename)
    counts = result.counts
    download_id = converter.downloads.put(
        result.pptx_bytes,
        filename,
        counts=counts,
        dropped_count=len(result.dropped),
    )

    return JSONResponse(
        content={
            "status": "success",
            "counts": counts,
            "total": result.total,
            "dropped": result.dropped,
            "warnings": result.warnings,
            "spec": result.spec_json(),
            "preview": result.preview,
            "download_id": download_id,
            "filename": filename,
        }
    )


# --------------------------------------------------------------------------- #
# POST /api/preview
# --------------------------------------------------------------------------- #
@router.post("/preview", summary="SlideSpec JSON -> a preview of the real deck")
async def preview(request: Request) -> JSONResponse:
    """Describe the deck a spec would build, without storing anything.

    The same shape as ``POST /api/convert`` minus ``download_id``/``filename``,
    because nothing is kept: the deck is built (so the preview reports the
    geometry python-pptx really produced, floors and clipping included) and the
    bytes are thrown away. The UI uses this to refresh the preview after the
    user edits a spec, then downloads through ``/api/build-from-json``.

    The body is read exactly as ``/api/build-from-json`` reads it - bounded, and
    normalized by ``normalize_spec_payload`` - so a bare SlideSpec, a
    ``{"spec": {...}}`` wrapper and a bare element array all work here too.
    """
    raw = await _read_json_body_capped(request)

    try:
        # Off the event loop, for the same reasons as /api/build-from-json.
        payload = await run_in_threadpool(converter.parse_json_body, raw)
    except ConversionFailure as failure:
        raise _fail(failure) from None

    try:
        result = await run_in_threadpool(
            converter.build_from_payload, payload, with_preview=True
        )
    except ConversionFailure as failure:
        raise _fail(failure) from None

    return JSONResponse(
        content={
            "status": "success",
            "counts": result.counts,
            "total": result.total,
            "dropped": result.dropped,
            "warnings": result.warnings,
            "spec": result.spec_json(),
            "preview": result.preview,
        }
    )


# --------------------------------------------------------------------------- #
# POST /api/build-from-json
# --------------------------------------------------------------------------- #
@router.post("/build-from-json", summary="SlideSpec JSON -> .pptx binary")
async def build_from_json(request: Request, filename: str = "slide.pptx") -> Response:
    """Build a deck from a spec the caller already has.

    The body is read and parsed here rather than declared as a pydantic model
    so that ``normalize_spec_payload`` sees the payload exactly as sent - that
    is what lets a bare element array and a ``{"spec": {...}}`` wrapper work as
    the CLI's ``--from-json`` does, and it keeps a malformed body inside the
    contract's ``invalid_spec`` envelope instead of fastapi's own 422 shape.

    Both of those steps are bounded: the body is capped at
    ``converter.MAX_JSON_BODY_BYTES`` while it is still arriving, and the parse
    runs in the threadpool, so neither the process's memory nor the event loop
    is at the mercy of the body's size.
    """
    raw = await _read_json_body_capped(request)

    try:
        # Off the event loop as well: decoding a multi-megabyte body is CPU
        # work, and doing it inline stalled every other request in the process
        # for its duration. ``parse_json_body`` maps every parse failure -
        # including the RecursionError a deeply nested document raises - onto
        # the contract's 400 ``invalid_spec``.
        payload = await run_in_threadpool(converter.parse_json_body, raw)
    except ConversionFailure as failure:
        raise _fail(failure) from None

    try:
        # Off the event loop: validating and rendering a spec is CPU-bound.
        result = await run_in_threadpool(converter.build_from_payload, payload)
    except ConversionFailure as failure:
        raise _fail(failure) from None

    return _pptx_response(
        result.pptx_bytes,
        # Passed through unsanitized on purpose: ``pptx_headers`` derives BOTH
        # the latin-1 ``filename=`` and the UTF-8 ``filename*`` from it, and
        # sanitizing first would strip the non-ASCII characters the second one
        # exists to carry. Nothing else uses this value.
        filename,
        result.counts,
        len(result.dropped),
    )


# --------------------------------------------------------------------------- #
# GET /api/download/{file_id}
# --------------------------------------------------------------------------- #
@router.get("/download/{file_id}", summary="Fetch a deck built by /api/convert")
def download(file_id: str) -> Response:
    """Serve a previously built deck.

    ``file_id`` is validated as a uuid4 hex BEFORE it is used for anything, and
    it is only ever a dict key - it is never joined onto a path or opened - so
    no traversal is reachable from this route. A fetch does not consume the
    entry: the same id can be downloaded again until it expires.
    """
    if not converter.is_download_id(file_id):
        raise _fail(
            ConversionFailure(
                404,
                "unknown_download",
                "That download id is not a valid identifier.",
                "Download ids are issued by POST /api/convert; re-run the "
                "conversion to get a fresh one.",
            )
        ) from None

    entry = converter.downloads.get(file_id)
    if entry is None:
        raise _fail(
            ConversionFailure(
                404,
                "unknown_download",
                "That download is unknown or has expired.",
                "Downloads are kept for "
                f"{int(converter.DOWNLOAD_TTL_SECONDS // 60)} minutes; re-run "
                "the conversion to get a fresh download id.",
            )
        ) from None

    return _pptx_response(
        entry.data, entry.filename, entry.counts, entry.dropped_count
    )
