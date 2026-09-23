"""Test package for image-to-pptx.

Keeping this a real package (rather than loose test files) means pytest adds
``backend/`` - the first directory above the tests without an ``__init__.py`` -
to ``sys.path``, so ``from app import config`` / ``import cli`` resolve to the
application package and the CLI entry point next to it.  The explicit insertion
below makes the suite runnable from any working directory, with or without
``python -m``.
"""

from __future__ import annotations

import sys
from pathlib import Path

#: ``backend/``: the import root that holds the ``app`` package and ``cli.py``.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
