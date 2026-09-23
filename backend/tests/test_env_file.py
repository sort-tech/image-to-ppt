"""Tests for app.env_file - the .env loader wired into the real entry points.

The bug this guards against: a user copies .env.example to .env, fills in the
key, and every conversion still answers 503 missing_api_key because nothing
read the file. These tests pin both that the file IS read and that a real
export still beats it.

Every test passes its own `environ` dict, so nothing here touches the real
process environment or depends on the developer's own .env.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.env_file import (
    DEFAULT_FILENAME,
    find_env_file,
    load_env_file,
    parse_env_text,
)


class TestParseEnvText:
    def test_plain_assignments(self):
        assert parse_env_text("A=1\nB=two\n") == {"A": "1", "B": "two"}

    def test_comments_and_blank_lines_are_skipped(self):
        text = "# a comment\n\n  \nA=1\n#B=2\n"
        assert parse_env_text(text) == {"A": "1"}

    def test_export_prefix_is_accepted(self):
        # .env.example documents `export NAME=...` lines, so people paste them.
        assert parse_env_text("export A=1\nexport\tB=2\n") == {"A": "1", "B": "2"}

    @pytest.mark.parametrize(
        "line, expected",
        [
            ("A='single'", "single"),
            ('A="double"', "double"),
            ("A=  spaced  ", "spaced"),
            ("A = 1", "1"),
            ("A=", ""),
            ("A==b", "=b"),
        ],
    )
    def test_quoting_and_whitespace(self, line, expected):
        assert parse_env_text(line) == {"A": expected}

    def test_crlf_line_endings(self):
        assert parse_env_text("A=1\r\nB=2\r\n") == {"A": "1", "B": "2"}

    def test_a_leading_byte_order_mark_does_not_eat_the_first_name(self):
        assert parse_env_text("﻿A=1\n") == {"A": "1"}

    def test_an_inline_comment_is_dropped_from_an_unquoted_value(self):
        assert parse_env_text("A=1   # trailing note\n") == {"A": "1"}

    def test_a_hash_inside_a_value_is_kept(self):
        # A key may legitimately contain '#'; truncating it would be silent
        # corruption that looks like a wrong key.
        assert parse_env_text("A=sk-abc#def\n") == {"A": "sk-abc#def"}

    def test_a_quoted_value_keeps_hashes_and_spaces(self):
        assert parse_env_text('A="keep # this"\n') == {"A": "keep # this"}

    @pytest.mark.parametrize(
        "line",
        ["no equals sign here", "=novalue", "1BAD=x", "BAD NAME=x", "BAD-NAME=x"],
    )
    def test_malformed_lines_are_skipped_not_fatal(self, line):
        # One typo must not cost the user the rest of the file.
        assert parse_env_text(line + "\nGOOD=1\n") == {"GOOD": "1"}

    def test_later_wins_within_one_file(self):
        assert parse_env_text("A=1\nA=2\n") == {"A": "2"}


class TestLoadEnvFile:
    def test_it_sets_names_that_are_absent(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("LITELLM_API_KEY=sk-from-file\n", encoding="utf-8")
        environ: dict[str, str] = {}
        applied = load_env_file(env_path, environ=environ)
        assert applied == {"LITELLM_API_KEY": "sk-from-file"}
        assert environ["LITELLM_API_KEY"] == "sk-from-file"

    def test_a_real_environment_value_wins(self, tmp_path):
        """`LITELLM_API_KEY=x ./run.sh` must override the file for that run."""
        env_path = tmp_path / ".env"
        env_path.write_text("LITELLM_API_KEY=sk-from-file\n", encoding="utf-8")
        environ = {"LITELLM_API_KEY": "sk-exported"}
        applied = load_env_file(env_path, environ=environ)
        assert applied == {}
        assert environ["LITELLM_API_KEY"] == "sk-exported"

    def test_a_blank_environment_value_is_treated_as_unset(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("LITELLM_API_KEY=sk-from-file\n", encoding="utf-8")
        environ = {"LITELLM_API_KEY": ""}
        load_env_file(env_path, environ=environ)
        assert environ["LITELLM_API_KEY"] == "sk-from-file"

    def test_override_true_replaces_an_existing_value(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("A=file\n", encoding="utf-8")
        environ = {"A": "env"}
        load_env_file(env_path, override=True, environ=environ)
        assert environ["A"] == "file"

    def test_it_only_reports_what_it_actually_set(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("A=1\nB=2\n", encoding="utf-8")
        environ = {"A": "already"}
        assert load_env_file(env_path, environ=environ) == {"B": "2"}

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        environ: dict[str, str] = {}
        assert load_env_file(tmp_path / "nope.env", environ=environ) == {}
        assert environ == {}

    def test_a_directory_in_place_of_the_file_is_not_an_error(self, tmp_path):
        target = tmp_path / "adir"
        target.mkdir()
        assert load_env_file(target, environ={}) == {}

    def test_undecodable_bytes_do_not_raise(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_bytes(b"A=1\n\xff\xfe\nB=2\n")
        environ: dict[str, str] = {}
        applied = load_env_file(env_path, environ=environ)
        assert applied["A"] == "1" and applied["B"] == "2"

    def test_an_empty_file_sets_nothing(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("", encoding="utf-8")
        assert load_env_file(env_path, environ={}) == {}


class TestFindEnvFile:
    def test_it_finds_the_file_in_a_parent_directory(self, tmp_path):
        (tmp_path / DEFAULT_FILENAME).write_text("A=1\n", encoding="utf-8")
        nested = tmp_path / "backend" / "app"
        nested.mkdir(parents=True)
        assert find_env_file(nested) == tmp_path / DEFAULT_FILENAME

    def test_it_returns_none_when_there_is_no_file(self, tmp_path):
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert find_env_file(nested, filename="definitely-absent.env") is None

    def test_a_file_path_is_treated_as_its_directory(self, tmp_path):
        (tmp_path / DEFAULT_FILENAME).write_text("A=1\n", encoding="utf-8")
        probe = tmp_path / "module.py"
        probe.write_text("", encoding="utf-8")
        assert find_env_file(probe) == tmp_path / DEFAULT_FILENAME

    def test_the_nearest_file_wins(self, tmp_path):
        (tmp_path / DEFAULT_FILENAME).write_text("A=outer\n", encoding="utf-8")
        inner = tmp_path / "inner"
        inner.mkdir()
        (inner / DEFAULT_FILENAME).write_text("A=inner\n", encoding="utf-8")
        assert find_env_file(inner) == inner / DEFAULT_FILENAME


class TestTheEntryPointsLoadIt:
    """The loader is useless if the entry points do not call it."""

    def test_cli_loads_the_env_file_before_importing_config(self):
        source = (Path(__file__).resolve().parent.parent / "cli.py").read_text(
            encoding="utf-8"
        )
        load_at = source.index("load_env_file")
        config_at = source.index("from app import config")
        assert load_at < config_at, "cli.py must load .env before app.config"

    def test_app_main_loads_the_env_file_before_importing_the_routes(self):
        source = (
            Path(__file__).resolve().parent.parent / "app" / "main.py"
        ).read_text(encoding="utf-8")
        load_at = source.index("load_env_file")
        routes_at = source.index("from .api.routes import")
        assert load_at < routes_at, "app/main.py must load .env before the routes"

    def test_config_itself_does_not_load_the_env_file(self):
        """config stays a pure reader of os.environ, so tests stay hermetic."""
        source = (
            Path(__file__).resolve().parent.parent / "app" / "config.py"
        ).read_text(encoding="utf-8")
        assert "load_env_file" not in source
        assert "env_file" not in source

    def test_run_sh_loads_the_env_file(self):
        source = (
            Path(__file__).resolve().parent.parent.parent / "run.sh"
        ).read_text(encoding="utf-8")
        assert "load_env_file" in source


class TestTheStartupDiagnosticIsActuallyVisible:
    """The .env diagnostic is worthless if it is swallowed by logging config.

    uvicorn installs handlers for its own loggers only, so logging to
    ``app.main`` alone produced no output under ``uvicorn app.main:app`` -
    exactly when someone is trying to find out why their .env did nothing.
    """

    def test_it_prefers_uvicorns_logger_when_uvicorn_configured_one(self):
        import logging

        from app.main import _startup_logger

        uvicorn_logger = logging.getLogger("uvicorn.error")
        handler = logging.NullHandler()
        uvicorn_logger.addHandler(handler)
        try:
            assert _startup_logger() is uvicorn_logger
        finally:
            uvicorn_logger.removeHandler(handler)

    def test_it_gives_itself_a_handler_when_nothing_is_configured(self, monkeypatch):
        import logging

        from app import main as app_main

        monkeypatch.setattr(logging.getLogger("uvicorn.error"), "handlers", [])
        monkeypatch.setattr(logging.getLogger(), "handlers", [])
        monkeypatch.setattr(app_main.logger, "handlers", [])
        chosen = app_main._startup_logger()
        assert chosen.handlers, "the diagnostic would have been silently dropped"

    def test_the_startup_message_names_variables_but_never_values(self, caplog):
        """A .env value is a secret; only its NAME may be logged."""
        import logging

        from app import main as app_main

        secret = "sk-this-must-never-be-logged"
        monkeypatch_applied = {"LITELLM_API_KEY": secret}
        with caplog.at_level(logging.INFO):
            logging.getLogger("uvicorn.error").info(
                "loaded %s -> set %s",
                app_main._ENV_FILE,
                ", ".join(sorted(monkeypatch_applied)),
            )
        assert "LITELLM_API_KEY" in caplog.text
        assert secret not in caplog.text

    def test_lifespan_is_used_instead_of_the_deprecated_on_event(self):
        source = (
            Path(__file__).resolve().parent.parent / "app" / "main.py"
        ).read_text(encoding="utf-8")
        assert "on_event" not in source, "on_event is deprecated in FastAPI"
        assert "lifespan=lifespan" in source
