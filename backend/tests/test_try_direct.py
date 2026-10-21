"""try-direct.sh: the keyless-safe parts (dry run, usage errors, no key leak)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "try-direct.sh"

# The Docker image carries backend/ only; the repo-root script is absent there.
pytestmark = pytest.mark.skipif(not SCRIPT.is_file(), reason="try-direct.sh not in this checkout")


def _run(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    clean.update(env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=clean
    )


@pytest.mark.parametrize(
    "provider, host, model",
    [
        ("gemini", "generativelanguage.googleapis.com/v1beta/openai/", "gemini-3.8-flash"),
        ("openai", "api.openai.com/v1/", "gpt-4o-mini"),
    ],
)
def test_dry_run_names_endpoint_and_model(provider, host, model):
    res = _run(provider, "--dry-run")
    assert res.returncode == 0
    assert host in res.stdout and model in res.stdout
    assert "NOT set" in res.stdout


def test_dry_run_never_prints_the_key():
    res = _run("openai", "--dry-run", OPENAI_API_KEY="sk-unit-test-secret")
    assert res.returncode == 0
    assert "is set" in res.stdout
    assert "sk-unit-test-secret" not in res.stdout + res.stderr


def test_missing_key_stops_before_any_call():
    res = _run("gemini")
    assert res.returncode == 2
    assert "GEMINI_API_KEY" in res.stderr


def test_unknown_provider_is_a_usage_error():
    assert _run("bogus").returncode == 2

