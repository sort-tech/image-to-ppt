"""Load a ``.env`` file into ``os.environ`` for the real entry points.

Why this exists: the settings in :mod:`app.config` are read from the process
environment, and the obvious thing to do with the shipped ``.env.example`` is to
copy it to ``.env`` and fill in the key. Without this module nothing reads that
file, so the key sits there and every conversion answers
``503 missing_api_key`` - which looks like a broken key rather than a file that
was never loaded.

Two deliberate choices:

* **The real environment always wins.** A value already present in
  ``os.environ`` is never replaced, so ``LITELLM_API_KEY=... ./run.sh`` or a
  plain ``export`` still overrides the file for one run. This is the usual
  dotenv convention and it keeps the file from silently shadowing a deliberate
  override.
* **Only entry points call this** - :mod:`app.main`, :mod:`cli` and
  ``run.sh``. :mod:`app.config` stays a pure reader of ``os.environ`` so the
  test suite is not at the mercy of a developer's local ``.env``.

Nothing here raises: a missing, unreadable or malformed file leaves the
environment untouched. Values are never logged - only the path and the names of
the variables that were set.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "DEFAULT_FILENAME",
    "SKIP_ENV_VAR",
    "find_env_file",
    "parse_env_text",
    "load_env_file",
]

#: Set (to anything) and the real process environment is never filled from a
#: file. The test suite sets it: importing ``cli`` or ``app.main`` used to load
#: a developer's ``.env`` into ``os.environ``, and any later reload of
#: ``config`` then picked that file's provider - the very dependency on a
#: local file the suite promises not to have.
SKIP_ENV_VAR = "I2P_NO_DOTENV"

#: The filename looked for when no explicit path is given.
DEFAULT_FILENAME = ".env"

#: How many parent directories to search. ``backend/app`` -> ``backend`` ->
#: repo root is two hops, so three gives a little slack without wandering off
#: into the user's home directory.
_MAX_PARENTS = 3


def find_env_file(start: str | Path | None = None, filename: str = DEFAULT_FILENAME) -> Path | None:
    """The nearest *filename* at or above *start*, or ``None``.

    Walks upward so that the same code works whether the process was started
    from the repo root (``./run.sh``), from ``backend/`` (``uvicorn
    app.main:app``) or from anywhere else with an absolute path.
    """
    here = Path(start) if start is not None else Path(__file__).resolve().parent
    here = here.resolve()
    if here.is_file():
        here = here.parent
    for directory in [here, *list(here.parents)[:_MAX_PARENTS]]:
        candidate = directory / filename
        try:
            if candidate.is_file():
                return candidate
        except OSError:  # pragma: no cover - unreadable directory
            continue
    return None


def _strip_inline_comment(value: str) -> str:
    """Drop a trailing ``# comment`` from an UNQUOTED value.

    A ``#`` only starts a comment when it follows whitespace, so a value like
    ``sk-abc#def`` keeps its hash - dropping it would silently truncate a key.
    """
    for index, char in enumerate(value):
        if char == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index]
    return value


def parse_env_text(text: str) -> dict[str, str]:
    """``KEY=value`` pairs from *text*, in file order.

    Understands what a hand-edited ``.env`` actually contains: ``#`` comment
    lines, blank lines, an optional ``export `` prefix, surrounding single or
    double quotes, whitespace around the ``=``, CRLF line endings, and a
    trailing inline comment on unquoted values. A line that is not a valid
    assignment is skipped rather than treated as fatal - one typo must not cost
    the user the rest of the file.
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("﻿")
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[len("export"):].lstrip()
        name, separator, value = line.partition("=")
        if not separator:
            continue
        name = name.strip()
        if not name or not (name[0].isalpha() or name[0] == "_"):
            continue
        if not all(char.isalnum() or char == "_" for char in name):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            # Quoted: take it literally, comments and whitespace included.
            value = value[1:-1]
        else:
            value = _strip_inline_comment(value).strip()
        values[name] = value
    return values


def load_env_file(
    path: str | Path | None = None,
    *,
    override: bool = False,
    environ: dict[str, str] | None = None,
) -> dict[str, str]:
    """Merge a ``.env`` file into the environment; return what was SET.

    Args:
        path: the file to read. When ``None``, :func:`find_env_file` locates it.
        override: when false (the default) a name already present in the
            environment is left alone, so a real export beats the file.
        environ: the mapping to mutate. Defaults to ``os.environ``; injectable
            so tests never touch the real process environment.

    Returns:
        Only the names/values this call actually wrote - an empty dict when
        there was no file, nothing parseable, or everything was already set.
        The caller can log the KEYS of this dict safely; the values are secret.
    """
    if environ is None and os.environ.get(SKIP_ENV_VAR):
        return {}
    target = os.environ if environ is None else environ
    resolved = Path(path) if path is not None else find_env_file()
    if resolved is None:
        return {}
    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    applied: dict[str, str] = {}
    for name, value in parse_env_text(text).items():
        if not override and target.get(name, "") != "":
            # Already set to something real - the environment wins.
            continue
        target[name] = value
        applied[name] = value
    return applied
