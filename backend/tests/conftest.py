"""Suite-wide hygiene: the tests must not depend on the developer's shell.

Provider selection is read from the environment (backend/app/config.py), at
import time as well as on every reload. A developer who exported PROVIDER or a
provider key for a real run - or for some other tool - must not change a test
result, the same promise the suite already makes about ``.env``. The variables
are dropped once here, before any test module imports ``config``, and again
around every test; tests that exercise them set them explicitly.
"""

import os

import pytest

_PROVIDER_VARS = (
    "PROVIDER",
    "PROVIDER_MODEL",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)

for _name in _PROVIDER_VARS:
    os.environ.pop(_name, None)


@pytest.fixture(autouse=True)
def _no_provider_env(monkeypatch):
    for name in _PROVIDER_VARS:
        monkeypatch.delenv(name, raising=False)
