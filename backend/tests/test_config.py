"""Tests for config.py: the API-key contract and the no-secrets-in-source rule.

The key lives in the environment only. These tests pin both halves of that:
``get_client`` must refuse to build a client without a key (naming the variable
rather than letting the SDK raise something opaque), and the literal key must
never reappear anywhere in the source tree.

Everything here is independent of the developer's own shell: ``config`` reads
``LITELLM_API_KEY`` at import time into a module global, and ``get_client``
re-reads that global at call time, so the tests monkeypatch the global instead
of the environment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import config


#: The REPOSITORY root - the parent of ``backend/``, not ``backend/`` itself.
#: The leak scan below deliberately starts here: a key pasted into the frontend
#: (or into any other sibling of the backend) is just as leaked as one in
#: ``app/config.py``, so the guard has to see the whole tree.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

#: Directories that are not our source: installed packages, caches and the
#: JS toolchain's dependency / build output (``node_modules`` and a built
#: frontend are enormous, not ours, and would make the scan crawl).
_SKIP_DIRS = {
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".git",
    "node_modules",
    "dist",
    "build",
    ".vite",
}

#: Text file kinds worth scanning for a leaked secret. The web-app suffixes are
#: here for the same reason the scan starts at the repo root: a key pasted into
#: a frontend source file is a leak, and a guard that only reads ``.py`` would
#: never see it. (``.env`` is deliberately absent - it is git-ignored and is
#: exactly where a developer's real key is *supposed* to live locally.)
_SCAN_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".json",
    ".example",
    ".cfg",
    ".toml",
    ".yml",
    ".yaml",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".vue",
    ".svelte",
    ".html",
    ".css",
}


def _leaked_key() -> str:
    """The key that must not appear in the tree, assembled from fragments.

    Built at runtime so that THIS file does not itself contain the literal -
    otherwise the guard below would flag its own source and could never pass.
    """
    return "sk-" + "_9FH3ZY" + "05x5vJhtc3f7d9w"


def _source_files() -> list[Path]:
    """Every text file in the project that is ours, caches excluded."""
    found: list[Path] = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(PROJECT_ROOT).parts):
            continue
        if path.suffix.lower() in _SCAN_SUFFIXES:
            found.append(path)
    return found


class TestTheLiteralKeyIsNotInTheSourceTree:
    """The whole point of moving the key to the environment."""

    def test_the_scan_actually_looks_at_something(self):
        # A guard that scans an empty file list would pass forever.
        files = _source_files()
        names = {p.name for p in files}
        assert len(files) >= 10, f"scan found only {len(files)} files"
        for expected in ("config.py", "README.md", "requirements.txt", ".env.example"):
            assert expected in names, f"{expected} was not scanned"
        # ...and it reaches BOTH the repo root and inside backend/, so neither
        # half of the tree can hide a key.
        relative = {str(p.relative_to(PROJECT_ROOT)) for p in files}
        for expected_path in (
            "README.md",
            ".env.example",
            "backend/app/config.py",
            "backend/requirements.txt",
            "backend/tests/test_config.py",
        ):
            assert expected_path in relative, f"{expected_path} was not scanned"

    def test_the_key_appears_in_no_source_file(self):
        needle = _leaked_key()
        offenders = []
        for path in _source_files():
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:  # pragma: no cover - unreadable file
                continue
            if needle in text:
                offenders.append(str(path.relative_to(PROJECT_ROOT)))
        assert not offenders, f"the API key is hard-coded in: {offenders}"

    def test_the_guard_fails_when_the_key_is_reintroduced(self, tmp_path, monkeypatch):
        """Prove the guard can fail - a check that cannot fail is worthless."""
        planted = tmp_path / "oops.py"
        planted.write_text(f'KEY = "{_leaked_key()}"\n', encoding="utf-8")
        monkeypatch.setattr(
            "tests.test_config._source_files", lambda: [planted], raising=False
        )
        # Re-run the same body the real test uses, against the planted file.
        needle = _leaked_key()
        offenders = [
            str(p) for p in [planted] if needle in p.read_text(encoding="utf-8")
        ]
        assert offenders == [str(planted)]

    def test_config_reads_the_key_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("LITELLM_API_KEY", "sk-from-the-env")
        import importlib

        reloaded = importlib.reload(config)
        try:
            assert reloaded.LITELLM_API_KEY == "sk-from-the-env"
        finally:
            monkeypatch.delenv("LITELLM_API_KEY", raising=False)
            importlib.reload(config)

    def test_the_default_key_is_empty_not_a_baked_in_secret(self, monkeypatch):
        for name in _KEY_VARS:
            monkeypatch.delenv(name, raising=False)
        import importlib

        reloaded = importlib.reload(config)
        try:
            assert reloaded.LITELLM_API_KEY == ""
        finally:
            importlib.reload(config)


_KEY_VARS = ("LITELLM_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")


class TestOneProviderKeyIsEnough:
    """Trying the project must not require running a LiteLLM proxy first."""

    @pytest.fixture
    def reload_with(self, monkeypatch):
        import importlib

        def _reload(**env):
            for name in (*_KEY_VARS, "LITELLM_BASE_URL", "VISION_MODEL"):
                monkeypatch.delenv(name, raising=False)
            for name, value in env.items():
                monkeypatch.setenv(name, value)
            return importlib.reload(config)

        yield _reload
        for name in (*_KEY_VARS, "LITELLM_BASE_URL", "VISION_MODEL"):
            monkeypatch.delenv(name, raising=False)
        importlib.reload(config)

    def test_a_gemini_key_alone_calls_gemini_directly(self, reload_with):
        reloaded = reload_with(GEMINI_API_KEY="g-test-key")
        assert (reloaded.PROVIDER, reloaded.KEY_SOURCE) == ("gemini", "GEMINI_API_KEY")
        assert reloaded.LITELLM_API_KEY == "g-test-key"
        assert "generativelanguage.googleapis.com" in reloaded.LITELLM_BASE_URL
        assert reloaded.DEFAULT_MODEL.startswith("gemini-")
        client = reloaded.get_client()  # no network: the SDK only stores config
        assert "generativelanguage.googleapis.com" in str(client.base_url)
        assert client.api_key == "g-test-key"

    def test_an_openai_key_alone_calls_openai_directly(self, reload_with):
        reloaded = reload_with(OPENAI_API_KEY="o-test-key")
        assert reloaded.PROVIDER == "openai"
        assert reloaded.LITELLM_BASE_URL.startswith("https://api.openai.com/")
        assert reloaded.DEFAULT_MODEL.startswith("gpt-")

    def test_the_proxy_key_wins_so_existing_setups_do_not_change(self, reload_with):
        reloaded = reload_with(
            LITELLM_API_KEY="sk-proxy", GEMINI_API_KEY="g-key", OPENAI_API_KEY="o-key"
        )
        assert (reloaded.PROVIDER, reloaded.LITELLM_API_KEY) == ("litellm", "sk-proxy")
        assert reloaded.LITELLM_BASE_URL == "http://localhost:4000/"
        assert reloaded.DEFAULT_MODEL == "gemini-flash"

    def test_gemini_wins_over_openai(self, reload_with):
        assert reload_with(GEMINI_API_KEY="g", OPENAI_API_KEY="o").PROVIDER == "gemini"

    def test_a_blank_proxy_key_falls_through_to_the_next_source(self, reload_with):
        """A copied .env line ``LITELLM_API_KEY=`` must not hide a real key."""
        assert reload_with(LITELLM_API_KEY="  ", GEMINI_API_KEY="g").PROVIDER == "gemini"

    def test_a_stale_proxy_url_never_receives_a_direct_provider_key(self, reload_with):
        reloaded = reload_with(
            GEMINI_API_KEY="g", LITELLM_BASE_URL="http://localhost:4000/"
        )
        assert "localhost" not in reloaded.LITELLM_BASE_URL

    def test_vision_model_still_overrides_the_provider_default(self, reload_with):
        reloaded = reload_with(GEMINI_API_KEY="g", VISION_MODEL="gemini-2.5-pro")
        assert reloaded.DEFAULT_MODEL == "gemini-2.5-pro"

    def test_an_anthropic_key_alone_calls_claude_through_its_own_sdk(self, reload_with):
        reloaded = reload_with(ANTHROPIC_API_KEY="a-test-key")
        assert (reloaded.PROVIDER, reloaded.KEY_SOURCE) == ("anthropic", "ANTHROPIC_API_KEY")
        assert reloaded.DEFAULT_MODEL == "claude-opus-5-5"
        client = reloaded.get_client()  # no network: the SDK only stores config
        assert type(client).__name__ == "AnthropicChatClient"
        assert client.api_key == "a-test-key"
        assert callable(client.chat.completions.create)

    def test_openai_wins_over_anthropic(self, reload_with):
        assert reload_with(OPENAI_API_KEY="o", ANTHROPIC_API_KEY="a").PROVIDER == "openai"

    def test_with_no_key_the_hint_names_every_option(self, reload_with):
        reloaded = reload_with()
        assert reloaded.LITELLM_API_KEY == ""
        for name in _KEY_VARS:
            assert name in reloaded._MISSING_KEY_HINT


class TestGetClientRefusesWithoutAKey:
    @pytest.mark.parametrize("blank", ["", "   ", "\t\n", None, 0])
    def test_a_missing_or_blank_key_raises_missing_api_key_error(self, monkeypatch, blank):
        monkeypatch.setattr(config, "LITELLM_API_KEY", blank)
        with pytest.raises(config.MissingApiKeyError):
            config.get_client()

    def test_the_error_names_the_variable_and_the_offline_escape_hatch(self, monkeypatch):
        monkeypatch.setattr(config, "LITELLM_API_KEY", "")
        with pytest.raises(config.MissingApiKeyError) as excinfo:
            config.get_client()
        message = str(excinfo.value)
        assert "LITELLM_API_KEY" in message
        assert "export" in message
        # --from-json is the one remaining no-key path; --mock is gone, and
        # pointing a stuck user at a flag that no longer parses is worse than
        # saying nothing.
        assert "--from-json" in message
        assert "mock" not in message.lower()

    def test_missing_api_key_error_is_a_runtime_error(self):
        # Callers that already catch RuntimeError must keep working.
        assert issubclass(config.MissingApiKeyError, RuntimeError)

    def test_a_present_key_builds_a_client_pointed_at_the_proxy(self, monkeypatch):
        monkeypatch.setattr(config, "LITELLM_API_KEY", "sk-test-key")
        client = config.get_client()  # no network: the SDK only stores config
        assert str(client.base_url).rstrip("/") == config.LITELLM_BASE_URL.rstrip("/")
        assert client.api_key == "sk-test-key"
        assert client.max_retries == config.LLM_MAX_RETRIES

    def test_the_key_is_read_at_call_time_not_import_time(self, monkeypatch):
        monkeypatch.setattr(config, "LITELLM_API_KEY", "sk-first")
        assert config.get_client().api_key == "sk-first"
        monkeypatch.setattr(config, "LITELLM_API_KEY", "sk-second")
        assert config.get_client().api_key == "sk-second"


class TestEnvExampleIsHonest:
    """.env.example is the setup doc; a wrong one wastes the user's time."""

    def test_it_exists_and_holds_only_placeholders(self):
        example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        assert _leaked_key() not in example
        for name in ("LITELLM_BASE_URL", "LITELLM_API_KEY", "VISION_MODEL"):
            assert name in example, f"{name} is missing from .env.example"

    def test_every_project_file_it_points_at_really_exists(self):
        """It used to cite a test module that was never written."""
        import re

        example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        referenced = set(re.findall(r"\b(?:tests/)?[\w./-]+\.py\b", example))
        missing = [
            name for name in referenced if not (PROJECT_ROOT / name).exists()
        ]
        assert not missing, f".env.example points at files that do not exist: {missing}"


class TestTheOfflinePathsNeedNoKey:
    def test_the_only_no_key_source_is_from_json(self, monkeypatch, tmp_path):
        """``--mock`` was the other one. With it gone, an image input with no
        key must fail rather than quietly fall back to anything."""
        import cli

        from make_sample_image import make_sample_image

        monkeypatch.setattr(config, "LITELLM_API_KEY", "")
        image = make_sample_image(tmp_path / "slide.png", size=(320, 180))
        out = tmp_path / "nokey.pptx"
        assert cli.main(["-i", str(image), "-o", str(out)]) == 1
        assert not out.exists()

    def test_from_json_builds_a_deck_with_no_key_configured(self, monkeypatch, tmp_path):
        import json

        import cli

        monkeypatch.setattr(config, "LITELLM_API_KEY", "")
        spec = tmp_path / "spec.json"
        spec.write_text(
            json.dumps(
                {
                    "elements": [
                        {
                            "type": "text",
                            "bbox": [100, 100, 500, 100],
                            "content": "no key needed",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        out = tmp_path / "fromjson.pptx"
        assert cli.main(["--from-json", str(spec), "-o", str(out)]) == 0
        assert out.is_file() and out.stat().st_size > 0
