"""The FastAPI application: ``uvicorn app.main:app``.

Wires three things together and nothing else:

* **CORS** for the two dev origins the frontend runs on;
* the ``/api`` router from :mod:`app.api.routes`;
* **one error envelope**. Every failure this app can produce - a route's own
  ``HTTPException``, starlette's built-in 404/405, a malformed request body, or
  an unexpected crash - leaves as ``{"detail": {"code", "message", "hint"}}``.
  A client can therefore parse exactly one error shape, and an unexpected
  exception never leaks a traceback or an internal path into the response body
  (it is logged server-side instead). The 500 is produced by
  :class:`ErrorEnvelopeMiddleware`, which sits INSIDE the CORS layer so that
  the browser can actually read it; the ``Exception`` handler starlette installs
  outside CORS remains as the net behind it.

The conversion core is untouched by all of this: it is imported through
``app.services.converter``, which is framework-free.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

# A .env beside the repo root is loaded BEFORE anything imports app.config,
# which reads os.environ at import time. A real export still wins (see
# app.env_file), and the file is optional.
from .env_file import find_env_file, load_env_file

_ENV_FILE = find_env_file()
_ENV_APPLIED = load_env_file(_ENV_FILE)

from .api.routes import router as api_router  # noqa: E402  (must follow the load)

__all__ = ["app", "error_body", "ALLOWED_ORIGINS", "ErrorEnvelopeMiddleware"]

logger = logging.getLogger("app.main")

#: The dev servers allowed to call this API (Vite's default and Next's default).
ALLOWED_ORIGINS: list[str] = [
    "http://localhost:5173",
    "http://localhost:3000",
]

DESCRIPTION = """
Turn an image of a slide into a **native, editable** PowerPoint deck.

A vision model describes the image as a `SlideSpec` on a normalized 0..1000
coordinate grid, and `python-pptx` redraws it as real shapes, tables and text
boxes - never a flattened picture. Every response is JSON except the two that
return a `.pptx` binary.

Errors always use one envelope: `{"detail": {"code", "message", "hint"}}`.
""".strip()

#: Fallback error codes for HTTPExceptions raised outside our own routes
#: (starlette's 404/405 carry a plain string detail).
_CODE_BY_STATUS: dict[int, str] = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
    422: "invalid_request",
    500: "internal_error",
}


def error_body(status_code: int, detail: Any) -> dict[str, dict[str, str]]:
    """Coerce any exception detail into the pinned error envelope.

    Our routes raise ``HTTPException(detail={"code", "message", "hint"})`` and
    that dict is passed through as-is (with missing keys filled in). Anything
    else - starlette's string details, a list, ``None`` - is wrapped, so there
    is no reachable response shape a client has to special-case.
    """
    if isinstance(detail, dict):
        code = detail.get("code") or _CODE_BY_STATUS.get(status_code, "error")
        message = detail.get("message")
        if message is None:
            message = detail.get("detail") or "The request failed."
        hint = detail.get("hint") or ""
        return {
            "detail": {
                "code": str(code),
                "message": str(message),
                "hint": str(hint),
            }
        }

    message = "The request failed."
    if isinstance(detail, str) and detail.strip():
        message = detail.strip()
    elif detail is not None:
        message = str(detail)
    return {
        "detail": {
            "code": _CODE_BY_STATUS.get(status_code, "error"),
            "message": message,
            "hint": "",
        }
    }


def _internal_error_body(exc: BaseException) -> dict[str, dict[str, str]]:
    """The 500 envelope: the code, a generic message and the exception's CLASS
    name - never its message text, a file path or a frame."""
    return error_body(
        500,
        {
            "code": "internal_error",
            "message": f"The server hit an unexpected error ({type(exc).__name__}).",
            "hint": "This is a bug. Retry, and check the server log for the "
            "traceback if it persists.",
        },
    )


class ErrorEnvelopeMiddleware:
    """Turn an unhandled exception into the 500 envelope *inside* the CORS layer.

    starlette installs the ``Exception`` handler into ``ServerErrorMiddleware``,
    which is the OUTERMOST layer of the stack - outside ``CORSMiddleware``. A
    500 produced there therefore never passes back through the CORS layer and
    carries no ``Access-Control-Allow-Origin``, so the browser rejects it as a
    network failure and the UI cannot read the very envelope the contract
    guarantees for the cases where a user most needs the hint (a 400 from the
    same origin does get the header).

    This is a plain ASGI middleware, registered BEFORE ``CORSMiddleware`` -
    ``add_middleware`` inserts at the front of the list, so the LAST one
    registered is the outermost - which puts it inside the CORS layer, where its
    response picks up the CORS headers like any other. If the response has
    already started there is nothing left to replace, so the exception is
    re-raised and the outer handler deals with the half-sent response.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def sending(message: Any) -> None:
            nonlocal started
            if message.get("type") == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, sending)
        except Exception as exc:
            if started:  # pragma: no cover - the body is already on the wire
                raise
            logger.exception(
                "unhandled error while serving %s %s",
                scope.get("method", "?"),
                scope.get("path", "?"),
            )
            response = JSONResponse(status_code=500, content=_internal_error_body(exc))
            await response(scope, receive, send)


def _startup_logger() -> logging.Logger:
    """A logger whose output actually reaches the console.

    uvicorn installs handlers for its OWN loggers only, so a plain
    ``logging.getLogger("app.main").info(...)`` is swallowed under
    ``uvicorn app.main:app`` - which would make this diagnostic useless exactly
    when it is needed. Prefer ``uvicorn.error`` (the logger uvicorn itself uses
    for "Application startup complete", so the line lands beside it with the
    same formatting); fall back to our own logger, giving it a stderr handler if
    nothing else configured one.
    """
    uvicorn_logger = logging.getLogger("uvicorn.error")
    if uvicorn_logger.handlers:
        return uvicorn_logger
    if not logger.handlers and not logging.getLogger().handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Say where settings came from, so a never-loaded .env is obvious.

    Names only - a value read from .env is a secret. ``api_key_configured`` on
    ``/api/health`` reports the outcome; this reports the cause.
    """
    from . import config

    logger = _startup_logger()
    if _ENV_FILE is None:
        logger.info("no .env file found; using the process environment only")
    elif _ENV_APPLIED:
        logger.info("loaded %s -> set %s", _ENV_FILE, ", ".join(sorted(_ENV_APPLIED)))
    else:
        logger.info(
            "found %s but set nothing from it (those names were already in the "
            "environment, which takes precedence)",
            _ENV_FILE,
        )
    if not config.LITELLM_API_KEY.strip():
        logger.warning(
            "LITELLM_API_KEY is not set - real conversions will answer 503 "
            "missing_api_key. /api/preview and /api/build-from-json still "
            "work: they build from a spec instead of calling the model."
        )
    yield


app = FastAPI(
    title="image-to-pptx API",
    description=DESCRIPTION,
    version="1.0.0",
    lifespan=lifespan,
)

# Registered FIRST so it ends up INSIDE CORSMiddleware (add_middleware inserts
# at the front of the stack, so the last registration is the outermost layer).
app.add_middleware(ErrorEnvelopeMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # The UI reads the element counts straight off a .pptx download, so the
    # custom headers have to be readable by browser JS as well as present.
    expose_headers=["Content-Disposition", "X-Element-Counts", "X-Dropped-Count"],
)

app.include_router(api_router)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Render every HTTPException - ours and starlette's - as the envelope."""
    headers = getattr(exc, "headers", None)
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(exc.status_code, exc.detail),
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """A malformed request (e.g. ``/api/convert`` with no ``file`` part).

    fastapi would answer with its own ``{"detail": [ ... ]}`` list here, which
    is the one error shape in the app that would not match the contract.
    """
    problems = []
    for error in exc.errors():
        location = ".".join(str(part) for part in (error.get("loc") or ())[1:])
        problems.append(
            f"{location or 'body'}: {error.get('msg') or 'invalid'}"
        )
    return JSONResponse(
        status_code=422,
        content=error_body(
            422,
            {
                "code": "invalid_request",
                "message": "The request could not be read.",
                "hint": "; ".join(problems),
            },
        ),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """The safety net BEHIND :class:`ErrorEnvelopeMiddleware`.

    Anything raised by the app itself is already turned into this same envelope
    one layer further in, where the response still passes through CORS. This
    handler stays for what cannot be caught there - an exception raised by the
    CORS layer itself, or by the middleware above it - so there is no reachable
    500 without the envelope, at the cost of that one case being unreadable to a
    cross-origin browser client.

    The traceback is logged with the request path (server-side, where it is
    useful) and the body carries only the code, a generic message and the
    exception's class name - never the message text, a file path or a frame.
    """
    logger.exception("unhandled error while serving %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content=_internal_error_body(exc))


@app.get("/", include_in_schema=False)
def index() -> dict[str, Any]:
    """A tiny pointer so hitting the root is not a bare 404."""
    return {
        "name": "image-to-pptx API",
        "docs": "/docs",
        "health": "/api/health",
        "endpoints": [
            "GET  /api/health",
            "POST /api/convert",
            "POST /api/build-from-json",
            "GET  /api/download/{file_id}",
        ],
    }
