"""Suite-wide hygiene: the tests must not depend on the developer's shell."""

import pytest

#: Provider selection is read from the environment (backend/app/config.py). A
#: developer who exported PROVIDER / a key for a real run must not change a
#: test result - the same promise the suite already makes about ``.env``.
_PROVIDER_VARS = ("PROVIDER", "PROVIDER_MODEL", "GEMINI_API_KEY", "OPENAI_API_KEY")


@pytest.fixture(autouse=True)
def _no_provider_env(monkeypatch):
    for name in _PROVIDER_VARS:
        monkeypatch.delenv(name, raising=False)
