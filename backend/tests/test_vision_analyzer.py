"""Vision analyzer tests - fully offline.

No test in this file may open a socket.  Every LLM interaction goes through
:class:`FakeClient`, which mimics the shape of the OpenAI SDK response
(``response.choices[0].message.content``) and records the kwargs it was called
with, so we can assert on the exact request the analyzer builds.
"""

from __future__ import annotations

import base64
import importlib
import json
import os
import re

import pytest
from PIL import Image

from app import config
from app import mock_data
from app import vision_analyzer
from app.schemas import SlideSpec
from app.vision_analyzer import (
    SYSTEM_PROMPT,
    VisionAnalysisError,
    analyze_image,
    build_messages,
    encode_image,
    extract_json,
)


# ---------------------------------------------------------------------------
# fake OpenAI-compatible client
# ---------------------------------------------------------------------------
class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)
        self.finish_reason = "stop"


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]
        self.model = "fake"


class _FakeCompletions:
    def __init__(self, script: list):
        self.script = script
        self.calls: list[dict] = []

    def create(self, *args, **kwargs):
        self.calls.append(kwargs)
        item = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(item, BaseException):
            raise item
        return _FakeResponse(item)


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeClient:
    """Stand-in for ``openai.OpenAI``.

    ``script`` entries are returned (or raised) one per call; the last entry
    repeats if the analyzer calls more times than we scripted.
    """

    def __init__(self, *script):
        self.chat = _FakeChat(_FakeCompletions(list(script) or [""]))

    @property
    def calls(self) -> list[dict]:
        return self.chat.completions.calls


class FakeBadRequestError(Exception):
    """Looks like what a proxy returns when it hates ``response_format``."""

    status_code = 400
    code = "invalid_request_error"

    def __init__(self, message: str | None = None):
        super().__init__(
            message
            or "Error code: 400 - Unsupported parameter: 'response_format' "
            "is not supported with this model."
        )


def _png(tmp_path, name: str = "img.png", size=(12, 8)):
    path = tmp_path / name
    Image.new("RGB", size, "#1F3B73").save(path, format="PNG")
    return path


# ---------------------------------------------------------------------------
# config (the analyzer's only dependency besides schemas)
# ---------------------------------------------------------------------------
class TestConfig:
    def test_connection_settings(self):
        assert isinstance(config.LITELLM_BASE_URL, str) and config.LITELLM_BASE_URL
        # LITELLM_API_KEY is a string, but it is a CREDENTIAL: it comes from the
        # environment and defaults to "" (see test_documented_defaults_when_env_is_clean).
        # It may not be asserted truthy - that only held while a literal key was
        # baked into config.py, which is exactly what must never be in the source.
        assert isinstance(config.LITELLM_API_KEY, str)
        assert isinstance(config.DEFAULT_MODEL, str) and config.DEFAULT_MODEL
        assert config.LLM_TIMEOUT == 120.0
        assert config.LLM_MAX_RETRIES == 2

    def test_documented_defaults_when_env_is_clean(self):
        if "VISION_MODEL" not in os.environ:
            assert config.DEFAULT_MODEL == "gemini-flash"
        if "LITELLM_BASE_URL" not in os.environ:
            assert config.LITELLM_BASE_URL == "http://localhost:4000/"
        if "LITELLM_API_KEY" not in os.environ:
            assert config.LITELLM_API_KEY == "", "no key may be baked into the source"

    def test_geometry_and_font_defaults(self):
        assert config.SLIDE_WIDTH_IN == 13.333
        assert config.SLIDE_HEIGHT_IN == 7.5
        assert config.COORD_SCALE == 1000
        assert config.DEFAULT_TEXT_FONT_SIZE == 12
        assert config.DEFAULT_TABLE_FONT_SIZE == 10
        assert config.DEFAULT_BORDER_WIDTH_PT == 1.0

    def test_supported_extensions_and_mime_map(self):
        assert isinstance(config.SUPPORTED_EXTENSIONS, frozenset)
        assert config.SUPPORTED_EXTENSIONS == frozenset({".png", ".jpg", ".jpeg", ".webp"})
        assert config.MIME_BY_EXTENSION[".png"] == "image/png"
        assert config.MIME_BY_EXTENSION[".jpg"] == "image/jpeg"
        assert config.MIME_BY_EXTENSION[".jpeg"] == "image/jpeg"
        assert config.MIME_BY_EXTENSION[".webp"] == "image/webp"
        assert set(config.MIME_BY_EXTENSION) >= config.SUPPORTED_EXTENSIONS

    def test_openai_is_imported_lazily_so_config_stays_importable(self):
        """``import config`` must not require the openai package."""
        globals_ = vars(config)
        assert "openai" not in globals_
        assert "OpenAI" not in globals_
        assert callable(config.get_client)

    @pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"])
    def test_a_blank_env_value_falls_back_to_the_documented_default(
        self, monkeypatch, blank
    ):
        """``VISION_MODEL=`` is PRESENT and empty, which is not "unset".

        ``os.environ.get(name, default)`` falls back only when the variable is
        missing, so a hand-edited ``.env`` line with nothing after the ``=``
        made ``DEFAULT_MODEL`` the empty string - and ``env_file.load_env_file``
        copies exactly that into the environment. Nothing downstream could
        recover it: ``vision_analyzer``'s ``model or getattr(config,
        "DEFAULT_MODEL", ...)`` keeps the empty string (the attribute exists),
        the proxy answers "Invalid model name passed in model=" as a 502, and
        ``/api/health`` reported an empty model, which the status pill - the only
        place the model is named now that the UI has no picker - rendered as a
        dangling " · 키 설정됨". A blank is not a configuration; it is the
        absence of one, and configuration falls back rather than breaks.
        """
        monkeypatch.setenv("VISION_MODEL", blank)
        monkeypatch.setenv("LITELLM_BASE_URL", blank)
        # The proxy's defaults are the ones under test. A direct-provider key -
        # from the shell, or from a developer's .env that importing cli or
        # app.main loads - would select another provider and its defaults.
        for name in ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        try:
            reloaded = importlib.reload(config)
            assert reloaded.DEFAULT_MODEL == "gemini-flash"
            assert reloaded.LITELLM_BASE_URL == "http://localhost:4000/"
            # ...so the name the analyzer resolves an absent ``model`` to (see
            # ``test_model_defaults_to_config``) and the one the header pill
            # shows are both a real model rather than an empty string.
            from app.services import converter

            assert converter.health_payload()["proxy"]["model"] == "gemini-flash"
        finally:
            monkeypatch.undo()
            importlib.reload(config)

    def test_a_padded_env_value_is_used_without_its_padding(self, monkeypatch):
        monkeypatch.setenv("VISION_MODEL", "  gpt-4o  ")
        try:
            assert importlib.reload(config).DEFAULT_MODEL == "gpt-4o"
        finally:
            monkeypatch.undo()
            importlib.reload(config)

    def test_env_vars_override_the_defaults(self, monkeypatch):
        monkeypatch.setenv("VISION_MODEL", "some-other-model")
        monkeypatch.setenv("LITELLM_BASE_URL", "http://example.invalid:9999/")
        monkeypatch.setenv("LITELLM_API_KEY", "sk-test-override")
        try:
            reloaded = importlib.reload(config)
            assert reloaded.DEFAULT_MODEL == "some-other-model"
            assert reloaded.LITELLM_BASE_URL == "http://example.invalid:9999/"
            assert reloaded.LITELLM_API_KEY == "sk-test-override"
        finally:
            monkeypatch.undo()
            importlib.reload(config)


# ---------------------------------------------------------------------------
# encode_image
# ---------------------------------------------------------------------------
class TestEncodeImage:
    def test_returns_base64_of_the_exact_bytes_plus_png_mime(self, tmp_path):
        path = _png(tmp_path)
        b64, mime = encode_image(path)
        assert mime == "image/png"
        assert base64.b64decode(b64) == path.read_bytes()
        assert "\n" not in b64, "base64 payload must be a single data-url-safe line"

    def test_accepts_a_string_path(self, tmp_path):
        path = _png(tmp_path)
        b64, mime = encode_image(str(path))
        assert mime == "image/png" and b64

    def test_jpeg_mime(self, tmp_path):
        path = tmp_path / "photo.jpg"
        Image.new("RGB", (10, 10), "#FFB000").save(path, format="JPEG")
        b64, mime = encode_image(path)
        assert mime == "image/jpeg"
        assert base64.b64decode(b64) == path.read_bytes()

    def test_jpeg_alternate_extension(self, tmp_path):
        path = tmp_path / "photo.jpeg"
        Image.new("RGB", (10, 10), "#FFB000").save(path, format="JPEG")
        assert encode_image(path)[1] == "image/jpeg"

    def test_webp_mime(self, tmp_path):
        path = tmp_path / "shot.webp"
        try:
            Image.new("RGB", (10, 10), "#9AA9C2").save(path, format="WEBP")
        except (KeyError, OSError, ValueError) as exc:  # pragma: no cover
            pytest.skip(f"Pillow build without WebP support: {exc}")
        assert encode_image(path)[1] == "image/webp"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(Exception):
            encode_image(tmp_path / "nope.png")


# ---------------------------------------------------------------------------
# extract_json
# ---------------------------------------------------------------------------
class TestExtractJson:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # already clean
            ('{"a": 1}', {"a": 1}),
            ("  {\n \"a\": 1\n}  ", {"a": 1}),
            # markdown fences, labelled and bare
            ('```json\n{"a": 1}\n```', {"a": 1}),
            ('```JSON\n{"a": 1}\n```', {"a": 1}),
            ('```\n{"a": 1}\n```', {"a": 1}),
            # leading prose / trailing prose
            ('Sure, here you go:\n{"a": 1}', {"a": 1}),
            ('{"a": 1}\nHope that helps!', {"a": 1}),
            ('Here it is:\n```json\n{"a": 1}\n```\nAnything else?', {"a": 1}),
            # nested braces must not terminate the scan early
            ('{"a": {"b": {"c": [1, 2]}}, "d": 3}', {"a": {"b": {"c": [1, 2]}}, "d": 3}),
            # braces *inside string literals* must be ignored by the scanner
            ('{"note": "use {curly} braces }", "b": 1}', {"note": "use {curly} braces }", "b": 1}),
            ('{"note": "} } } {", "b": 2}', {"note": "} } } {", "b": 2}),
            # escaped quotes inside a string that also contains braces
            (
                '{"note": "he said \\"}\\" and left {", "b": 3}',
                {"note": 'he said "}" and left {', "b": 3},
            ),
            # escaped backslash right before a quote must not escape it
            ('{"path": "C:\\\\dir\\\\", "b": 4}', {"path": "C:\\dir\\", "b": 4}),
        ],
    )
    def test_recovers_the_json_object(self, raw, expected):
        cleaned = extract_json(raw)
        assert json.loads(cleaned) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "I am afraid I cannot help with that.",
            "```json\nno braces in here\n```",
            '{"a": 1',  # never balances
            "[1, 2, 3]",  # an array is not a slide spec object
        ],
    )
    def test_raises_when_there_is_no_balanced_object(self, raw):
        with pytest.raises(VisionAnalysisError):
            extract_json(raw)

    def test_handles_the_realistic_fenced_sample_response(self):
        raw = mock_data.sample_llm_response_with_fences()
        assert json.loads(extract_json(raw)) == json.loads(mock_data.SAMPLE_SPEC_JSON)

    def test_result_is_directly_parseable_into_a_slide_spec(self):
        spec = SlideSpec.model_validate_json(
            extract_json(mock_data.sample_llm_response_with_fences())
        )
        assert len(spec.elements) == len(mock_data.sample_spec().elements)

    @pytest.mark.parametrize(
        "raw",
        [
            # chatty metadata object emitted BEFORE the real payload
            'Metadata first: {"note": "3 columns", "confidence": 0.9}\n'
            'Spec:\n{"background_color": "#FFFFFF", "elements": '
            '[{"type": "text", "bbox": [0, 0, 500, 100], "content": "Real Title"}]}',
            # ... and the same thing with the payload fenced
            'Here is what I found: {"tokens": 812}\n```json\n'
            '{"elements": [{"type": "text", "bbox": [0, 0, 500, 100], '
            '"content": "Real Title"}]}\n```',
        ],
    )
    def test_prefers_the_slide_spec_over_a_chatty_metadata_object(self, raw):
        """Taking the *first* balanced object would silently yield an empty deck.

        A model that prefixes its answer with a little ``{"note": ...}`` object
        must not cost us the whole slide: the spec-shaped object wins.
        """
        payload = json.loads(extract_json(raw))
        assert "elements" in payload, f"picked the wrong object: {payload}"
        assert payload["elements"][0]["content"] == "Real Title"

    def test_a_non_spec_object_is_still_returned_when_it_is_all_there_is(self):
        """The preference must not turn a lone non-spec object into an error."""
        assert json.loads(extract_json('Sure:\n{"a": 1}')) == {"a": 1}


# ---------------------------------------------------------------------------
# extract_json: replies that must never degrade into an EMPTY spec
# ---------------------------------------------------------------------------
#: A reply cut off at the model's output limit, after one complete element.
TRUNCATED_REPLY = (
    '{"background_color":"#FFFFFF","elements":['
    '{"type":"text","bbox":[40,40,600,90],"content":"Q4 Revenue Review"},'
    '{"type":"table","bbox":[40,200,900,400],"data":[["Region","Users"],["EMEA",'
)

ELEMENTS_ARRAY_REPLY = (
    '[{"type":"text","bbox":[40,40,600,90],"content":"Q4 Revenue Review"},'
    '{"type":"table","bbox":[40,200,900,400],"data":[["Region","Users"],["EMEA","18,400"]]}]'
)

ENVELOPE_REPLY = (
    '{"slide": {"background_color":"#FFFFFF","elements":'
    '[{"type":"text","bbox":[40,40,600,90],"content":"Q4 Revenue Review"}]}}'
)


class TestExtractJsonNeverBlanksTheSlide:
    """``SlideSpec`` validates ANY object (``extra="ignore"``, ``elements=[]``).

    So whatever ``extract_json`` hands back decides whether the user gets their
    slide or a blank one - a wrong pick here is total, silent data loss.
    """

    def test_a_truncated_reply_is_an_error_not_a_fragment(self):
        """The first *complete element* must not be passed off as the spec."""
        with pytest.raises(VisionAnalysisError) as excinfo:
            extract_json(TRUNCATED_REPLY)
        assert "truncat" in str(excinfo.value).lower()

    def test_a_reply_truncated_mid_string_is_also_reported(self):
        with pytest.raises(VisionAnalysisError) as excinfo:
            extract_json('{"elements":[{"type":"text","content":"unfinished')
        assert "cut off" in str(excinfo.value).lower()

    def test_a_bare_element_array_is_wrapped_as_a_spec(self):
        payload = json.loads(extract_json(ELEMENTS_ARRAY_REPLY))
        assert [el["type"] for el in payload["elements"]] == ["text", "table"]

    def test_a_fenced_element_array_is_wrapped_too(self):
        payload = json.loads(extract_json(f"Here you go:\n```json\n{ELEMENTS_ARRAY_REPLY}\n```"))
        assert len(payload["elements"]) == 2

    def test_an_envelope_key_is_unwrapped(self):
        payload = json.loads(extract_json(ENVELOPE_REPLY))
        assert payload["background_color"] == "#FFFFFF"
        assert payload["elements"][0]["content"] == "Q4 Revenue Review"

    def test_an_envelope_holding_a_bare_array_is_unwrapped(self):
        raw = '{"result": [{"type":"text","bbox":[0,0,100,100],"content":"Hi"}]}'
        assert json.loads(extract_json(raw))["elements"][0]["content"] == "Hi"

    def test_a_non_element_array_is_still_an_error(self):
        """Wrapping must be element-aware: ``[1, 2, 3]`` is not a slide."""
        with pytest.raises(VisionAnalysisError):
            extract_json("[1, 2, 3]")


# ---------------------------------------------------------------------------
# SYSTEM_PROMPT
# ---------------------------------------------------------------------------
#: The prompt on one line, so a rule that wraps across source lines can still
#: be matched as the sentence it is.
_FLAT_PROMPT = " ".join(SYSTEM_PROMPT.split())


class TestSystemPrompt:
    def test_is_a_substantive_instruction(self):
        assert isinstance(SYSTEM_PROMPT, str)
        assert len(SYSTEM_PROMPT) > 200

    @pytest.mark.parametrize(
        "token",
        [
            "1000",
            "bbox",
            "json",
            "shape",
            "table",
            "text",
            "elements",
            "connector",
            "image",
            "start_point",
            "end_point",
            "arrow_start",
            "arrow_end",
            "dash_style",
            "elbow",
            "arrow_right",
        ],
    )
    def test_states_the_contract(self, token):
        assert token in SYSTEM_PROMPT.lower(), f"SYSTEM_PROMPT never mentions {token!r}"

    def test_it_says_a_connector_carries_endpoints_instead_of_a_bbox(self):
        assert re.search(r'connector has NO "bbox"', _FLAT_PROMPT), (
            "the model has to be told the one element type that has no bbox"
        )

    def test_it_separates_a_block_arrow_from_a_connector(self):
        """Both are "arrows"; the model must know which is which, or every
        diagram line comes back as a fat rectangle-with-a-point."""
        assert re.search(r"BLOCK arrows?", _FLAT_PROMPT)
        arrow_rule = re.search(r"arrow_\*[^.]*\.", _FLAT_PROMPT)
        assert arrow_rule is not None
        assert "connector" in re.search(
            r'thin arrow that\s+merely joins two boxes[^.]*\.', _FLAT_PROMPT
        ).group(0)

    def test_it_forbids_marking_native_objects_as_an_image(self):
        """The whole point of the project: a box, a label or a grid must stay
        editable, so it may never be handed back as a cropped picture."""
        rule = re.search(r'NEVER mark an[^.]*as "image"[^.]*\.', _FLAT_PROMPT)
        assert rule is not None, 'SYSTEM_PROMPT never forbids native-object-as-"image"'
        matched = rule.group(0).lower()
        for word in ("box", "text", "table"):
            assert word in matched, f"the rule does not mention a {word}"

    def test_it_forbids_one_image_covering_the_whole_slide(self):
        assert re.search(
            r'NEVER emit one "image" covering the whole slide', _FLAT_PROMPT
        ), "SYSTEM_PROMPT never forbids the whole-slide image - that IS the flattening"

    def test_it_also_forbids_a_few_big_images_tiling_the_slide(self):
        """One 95%-of-the-slide picture is not the only way to flatten a slide;
        four quarter-slide pictures do it too, so the prompt says both."""
        assert "tile the slide" in _FLAT_PROMPT

    def test_a_photo_is_no_longer_simply_omitted(self):
        """The old prompt told the model to DROP photographs and logos, which is
        exactly the region "image" now exists to keep."""
        assert re.search(r"photograph.*is NOT omitted", _FLAT_PROMPT)


# ---------------------------------------------------------------------------
# build_messages
# ---------------------------------------------------------------------------
class TestBuildMessages:
    def test_openai_vision_payload_shape(self):
        messages = build_messages("QUJD", "image/png")
        assert [m["role"] for m in messages] == ["system", "user"]
        assert messages[0]["content"] == SYSTEM_PROMPT

        parts = messages[1]["content"]
        assert isinstance(parts, list) and len(parts) == 2

        text_part = next(p for p in parts if p["type"] == "text")
        image_part = next(p for p in parts if p["type"] == "image_url")
        assert isinstance(text_part["text"], str) and text_part["text"].strip()
        assert image_part["image_url"] == {"url": "data:image/png;base64,QUJD"}

    def test_data_url_uses_the_supplied_mime(self):
        parts = build_messages("Zm9v", "image/webp")[1]["content"]
        image_part = next(p for p in parts if p["type"] == "image_url")
        assert image_part["image_url"]["url"] == "data:image/webp;base64,Zm9v"

    def test_extra_instructions_reach_the_user_prompt(self):
        extra = "The table on the right has merged header cells."
        parts = build_messages("QUJD", "image/png", extra)[1]["content"]
        text_part = next(p for p in parts if p["type"] == "text")
        assert extra in text_part["text"]

    def test_extra_instructions_are_optional(self):
        base = build_messages("QUJD", "image/png")
        assert len(base) == 2
        text_part = next(p for p in base[1]["content"] if p["type"] == "text")
        assert "None" not in text_part["text"]


# ---------------------------------------------------------------------------
# analyze_image
# ---------------------------------------------------------------------------
class TestAnalyzeImage:
    def test_happy_path_returns_a_validated_spec(self, tmp_path):
        client = FakeClient(mock_data.SAMPLE_SPEC_JSON)
        spec = analyze_image(_png(tmp_path), model="test-model", client=client)
        assert isinstance(spec, SlideSpec)
        assert len(spec.elements) == len(mock_data.sample_spec().elements)
        assert spec.model_dump() == mock_data.sample_spec().model_dump()

    def test_request_parameters_match_the_contract(self, tmp_path):
        client = FakeClient(mock_data.SAMPLE_SPEC_JSON)
        analyze_image(_png(tmp_path), model="test-model", client=client)
        assert len(client.calls) == 1
        kwargs = client.calls[0]
        assert kwargs["model"] == "test-model"
        assert kwargs["temperature"] == 0.1
        assert kwargs["response_format"] == {"type": "json_object"}
        assert [m["role"] for m in kwargs["messages"]] == ["system", "user"]

    def test_image_is_actually_embedded_in_the_request(self, tmp_path):
        path = _png(tmp_path)
        client = FakeClient(mock_data.SAMPLE_SPEC_JSON)
        analyze_image(path, client=client)
        parts = client.calls[0]["messages"][1]["content"]
        url = next(p for p in parts if p["type"] == "image_url")["image_url"]["url"]
        expected_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        assert url == f"data:image/png;base64,{expected_b64}"

    def test_model_defaults_to_config(self, tmp_path):
        client = FakeClient(mock_data.SAMPLE_SPEC_JSON)
        analyze_image(_png(tmp_path), client=client)
        assert client.calls[0]["model"] == config.DEFAULT_MODEL

    def test_extra_instructions_are_forwarded(self, tmp_path):
        client = FakeClient(mock_data.SAMPLE_SPEC_JSON)
        analyze_image(_png(tmp_path), client=client, extra_instructions="focus on the table")
        blob = json.dumps(client.calls[0]["messages"])
        assert "focus on the table" in blob

    def test_accepts_a_fenced_chatty_response(self, tmp_path):
        client = FakeClient(mock_data.sample_llm_response_with_fences())
        spec = analyze_image(_png(tmp_path), client=client)
        assert len(spec.elements) == len(mock_data.sample_spec().elements)

    def test_metadata_prefixed_reply_does_not_yield_an_empty_spec(self, tmp_path):
        """End to end: a leading ``{"note": ...}`` must not blank the slide."""
        client = FakeClient(
            'Quick note: {"note": "dense slide", "confidence": 0.8}\n'
            f"{mock_data.SAMPLE_SPEC_JSON}"
        )
        spec = analyze_image(_png(tmp_path), client=client)
        assert len(spec.elements) == len(mock_data.sample_spec().elements)

    def test_default_client_comes_from_config(self, tmp_path, monkeypatch):
        """No client argument must still mean no network in tests."""
        client = FakeClient(mock_data.SAMPLE_SPEC_JSON)
        monkeypatch.setattr(config, "get_client", lambda: client)
        if hasattr(vision_analyzer, "get_client"):
            monkeypatch.setattr(vision_analyzer, "get_client", lambda: client)
        spec = analyze_image(_png(tmp_path))
        assert isinstance(spec, SlideSpec)
        assert len(client.calls) == 1

    @pytest.mark.parametrize(
        "content",
        [
            "I cannot read this image, sorry.",
            "```json\n{broken json}\n```",
            "",
            None,
        ],
    )
    def test_unusable_content_raises_vision_analysis_error(self, tmp_path, content):
        client = FakeClient(content)
        with pytest.raises(VisionAnalysisError):
            analyze_image(_png(tmp_path), client=client)

    def test_schema_violation_raises_vision_analysis_error(self, tmp_path):
        client = FakeClient('{"elements": [{"type": "hologram", "bbox": [0, 0, 1, 1]}]}')
        with pytest.raises(VisionAnalysisError):
            analyze_image(_png(tmp_path), client=client)

    def test_error_message_is_actionable(self, tmp_path):
        client = FakeClient("definitely not json")
        with pytest.raises(VisionAnalysisError) as excinfo:
            analyze_image(_png(tmp_path), client=client)
        assert str(excinfo.value).strip(), "VisionAnalysisError must carry a message"

    def test_transport_failure_is_wrapped(self, tmp_path):
        client = FakeClient(RuntimeError("Connection refused"), RuntimeError("Connection refused"))
        with pytest.raises(VisionAnalysisError):
            analyze_image(_png(tmp_path), client=client)

    def test_missing_image_is_wrapped_or_raised_clearly(self, tmp_path):
        client = FakeClient(mock_data.SAMPLE_SPEC_JSON)
        with pytest.raises(Exception):
            analyze_image(tmp_path / "missing.png", client=client)

    def test_retries_without_response_format_when_the_proxy_rejects_it(self, tmp_path, capsys):
        """Some providers 400 on response_format; we must fall back, not fail."""
        client = FakeClient(FakeBadRequestError(), mock_data.SAMPLE_SPEC_JSON)
        spec = analyze_image(_png(tmp_path), model="picky-model", client=client)

        assert isinstance(spec, SlideSpec)
        assert len(spec.elements) == len(mock_data.sample_spec().elements)
        assert len(client.calls) == 2, "expected exactly one retry"
        assert client.calls[0].get("response_format") == {"type": "json_object"}
        assert "response_format" not in client.calls[1]
        assert client.calls[1]["model"] == "picky-model"
        assert client.calls[1]["messages"] == client.calls[0]["messages"]
        assert capsys.readouterr().err.strip(), "the fallback should be noted on stderr"

    def test_retry_failure_still_surfaces_as_vision_analysis_error(self, tmp_path):
        client = FakeClient(FakeBadRequestError(), FakeBadRequestError("still unhappy"))
        with pytest.raises(VisionAnalysisError):
            analyze_image(_png(tmp_path), client=client)


# ---------------------------------------------------------------------------
# analyze_image: a blank deck is a failure, never a success
# ---------------------------------------------------------------------------
class TestAnalyzeImageRefusesToBlankTheSlide:
    @pytest.mark.parametrize(
        "reply",
        [
            TRUNCATED_REPLY,  # cut off at the output limit
            '{"title":"Q4 Revenue Review","notes":"a title and a table"}',  # no elements key
            '{"elements": []}',  # explicitly nothing
            '{"background_color": "#FFFFFF"}',  # colours only
        ],
    )
    def test_a_reply_with_no_elements_raises(self, tmp_path, reply):
        client = FakeClient(reply)
        with pytest.raises(VisionAnalysisError) as excinfo:
            analyze_image(_png(tmp_path), client=client)
        message = str(excinfo.value).lower()
        assert "element" in message or "truncat" in message, message

    def test_a_bare_element_array_still_yields_a_full_spec(self, tmp_path):
        client = FakeClient(ELEMENTS_ARRAY_REPLY)
        spec = analyze_image(_png(tmp_path), client=client)
        assert [el.type for el in spec.elements] == ["text", "table"]

    def test_an_envelope_wrapped_reply_still_yields_a_full_spec(self, tmp_path):
        client = FakeClient(ENVELOPE_REPLY)
        spec = analyze_image(_png(tmp_path), client=client)
        assert len(spec.elements) == 1
        assert spec.background_color == "#FFFFFF"


# ---------------------------------------------------------------------------
# analyze_image: ONE malformed element may not cost the whole conversion
# ---------------------------------------------------------------------------
_GOOD_TEXT = {"type": "text", "bbox": [1, 1, 100, 50], "content": "good one"}
_GOOD_SHAPE = {
    "type": "shape",
    "bbox": [2, 2, 100, 50],
    "kind": "circle",
    "fill_color": "#FF0000",
}
#: What models actually do wrong: one required key ("data") simply missing.
_TABLE_NO_DATA = {"type": "table", "bbox": [1, 1, 900, 400], "header_bg_color": "#112233"}


def _reply(*elements) -> str:
    return json.dumps({"elements": list(elements)})


class TestPartialElementDrop:
    def test_a_bad_element_is_dropped_and_the_good_ones_still_build(self, tmp_path):
        client = FakeClient(_reply(_GOOD_TEXT, _TABLE_NO_DATA, _GOOD_SHAPE))
        spec = analyze_image(_png(tmp_path), client=client)

        assert [el.type for el in spec.elements] == ["text", "shape"]
        assert spec.elements[0].content == "good one"
        assert spec.dropped == ["element 1 (table): data - Field required"]

    def test_the_drop_is_reported_on_stderr_one_line_per_element(self, tmp_path, capsys):
        client = FakeClient(_reply(_GOOD_TEXT, _TABLE_NO_DATA, {"type": "hologram"}))
        spec = analyze_image(_png(tmp_path), client=client)
        err = capsys.readouterr().err

        assert len(spec.elements) == 1 and len(spec.dropped) == 2
        for reason in spec.dropped:
            assert reason in err, "every dropped element must be named on stderr"
        assert "2 of 3 elements" in err

    def test_a_partial_drop_does_not_raise(self, tmp_path):
        """The old behaviour - one bad element aborting everything - is the bug."""
        client = FakeClient(_reply(_GOOD_TEXT, _TABLE_NO_DATA))
        assert len(analyze_image(_png(tmp_path), client=client).elements) == 1

    def test_every_element_dropped_raises_and_names_each_reason(self, tmp_path):
        client = FakeClient(_reply(_TABLE_NO_DATA, {"type": "hologram", "bbox": [0, 0, 1, 1]}))
        with pytest.raises(VisionAnalysisError) as excinfo:
            analyze_image(_png(tmp_path), client=client)

        message = str(excinfo.value)
        assert "element 0 (table): data - Field required" in message
        assert "hologram" in message
        assert "blank" in message.lower(), "the user must learn why nothing was built"

    def test_a_reply_whose_only_element_is_broken_raises(self, tmp_path):
        client = FakeClient(_reply(_TABLE_NO_DATA))
        with pytest.raises(VisionAnalysisError) as excinfo:
            analyze_image(_png(tmp_path), client=client)
        assert "data - Field required" in str(excinfo.value)

    def test_a_clean_reply_stays_silent_and_drops_nothing(self, tmp_path, capsys):
        client = FakeClient(_reply(_GOOD_TEXT, _GOOD_SHAPE))
        spec = analyze_image(_png(tmp_path), client=client)
        assert spec.dropped == []
        assert capsys.readouterr().err == "", "nothing to warn about"

    def test_a_bare_array_reply_with_one_bad_entry_keeps_the_good_ones(self, tmp_path, capsys):
        """Bare-array recovery used to be all-or-nothing, so one bad entry here
        cost the whole conversion - the same defect ``--from-json`` showed."""
        reply = json.dumps([_GOOD_TEXT, {"type": "hologram", "bbox": [0, 0, 1, 1]}, _GOOD_SHAPE])
        spec = analyze_image(_png(tmp_path), client=FakeClient(reply))
        assert [el.type for el in spec.elements] == ["text", "shape"]
        assert len(spec.dropped) == 1 and "hologram" in spec.dropped[0]
        assert "hologram" in capsys.readouterr().err

    def test_an_unreadable_bbox_is_dropped_and_reported_not_silently_slivered(
        self, tmp_path, capsys
    ):
        no_height = {"type": "text", "bbox": {"x": 100, "y": 200, "w": 600}, "content": "hi"}
        reply = _reply(no_height, _GOOD_SHAPE)
        spec = analyze_image(_png(tmp_path), client=FakeClient(reply))
        assert [el.type for el in spec.elements] == ["shape"]
        assert spec.dropped == ["element 0 (text): bbox - 'h' is missing or unreadable"]
        assert "missing or unreadable" in capsys.readouterr().err

    def test_a_dropped_key_in_the_models_reply_is_never_echoed(self, tmp_path, capsys):
        """``dropped`` is our diagnostic; the model may not put words in it."""
        reply = json.dumps(
            {"elements": [_GOOD_TEXT], "dropped": ["element 9 (text): FROM THE MODEL"]}
        )
        spec = analyze_image(_png(tmp_path), client=FakeClient(reply))
        assert spec.dropped == []
        assert len(spec.elements) == 1
        assert capsys.readouterr().err == "", "nothing was dropped, so nothing to say"


# ---------------------------------------------------------------------------
# connectors, block arrows and image regions: end to end through the analyzer
# ---------------------------------------------------------------------------
_CONNECTOR = {
    "type": "connector",
    "start_point": [120, 300],
    "end_point": [480, 300],
    "kind": "elbow",
    "dash_style": "dashed",
    "arrow_end": True,
    "color": "#1E3A6E",
    "width_pt": 1.5,
}
_IMAGE = {"type": "image", "bbox": [600, 200, 300, 400], "description": "team photo"}
_BLOCK_ARROW = {"type": "shape", "bbox": [400, 500, 120, 60], "kind": "arrow_right"}


class TestTheNewElementTypesReachTheSpec:
    def test_a_reply_using_every_new_type_validates(self, tmp_path, capsys):
        client = FakeClient(_reply(_GOOD_SHAPE, _BLOCK_ARROW, _CONNECTOR, _IMAGE, _GOOD_TEXT))
        spec = analyze_image(_png(tmp_path), client=client)

        assert [el.type for el in spec.elements] == [
            "shape",
            "shape",
            "connector",
            "image",
            "text",
        ]
        assert spec.dropped == []
        assert capsys.readouterr().err == "", "a clean reply warns about nothing"

        assert spec.elements[1].kind == "arrow_right"
        connector = spec.elements[2]
        assert connector.kind == "elbow"
        assert connector.dash_style == "dashed"
        assert (connector.arrow_start, connector.arrow_end) == (False, True)
        assert connector.start_point.as_tuple() == (120, 300)
        assert connector.end_point.as_tuple() == (480, 300)
        assert connector.color == "#1E3A6E" and connector.width_pt == 1.5
        assert not hasattr(connector, "bbox"), "a connector has no bounding box"
        assert spec.elements[3].description == "team photo"

    def test_counts_by_type_reports_them(self, tmp_path):
        client = FakeClient(_reply(_CONNECTOR, _CONNECTOR, _IMAGE, _GOOD_TEXT))
        counts = analyze_image(_png(tmp_path), client=client).counts_by_type()
        assert counts == {"connector": 2, "image": 1, "text": 1}

    def test_a_bare_array_of_connectors_is_still_recovered(self, tmp_path):
        """``extract_json`` recovers a bare array only if the entries LOOK like
        elements - and a connector has no bbox to recognize it by."""
        reply = json.dumps([_CONNECTOR, _IMAGE])
        spec = analyze_image(_png(tmp_path), client=FakeClient(reply))
        assert [el.type for el in spec.elements] == ["connector", "image"]

    def test_a_bare_array_of_connectors_is_recognized_by_extract_json(self):
        payload = json.loads(extract_json(json.dumps([_CONNECTOR])))
        assert [el["type"] for el in payload["elements"]] == ["connector"]

    def test_a_connector_with_a_broken_endpoint_costs_only_itself(self, tmp_path, capsys):
        broken = {"type": "connector", "start_point": [1, 2], "end_point": {"x": 5}}
        client = FakeClient(_reply(broken, _GOOD_SHAPE))
        spec = analyze_image(_png(tmp_path), client=client)

        assert [el.type for el in spec.elements] == ["shape"]
        assert spec.dropped == [
            "element 0 (connector): end_point - 'y' is missing or unreadable"
        ]
        assert "missing or unreadable" in capsys.readouterr().err

    def test_a_slide_of_nothing_but_broken_connectors_still_raises(self, tmp_path):
        broken = {"type": "connector", "start_point": [1, 2]}
        with pytest.raises(VisionAnalysisError) as excinfo:
            analyze_image(_png(tmp_path), client=FakeClient(_reply(broken)))
        message = str(excinfo.value)
        assert "end_point - Field required" in message
        assert "blank" in message.lower()

    def test_the_schema_hint_describes_both_new_types(self, tmp_path):
        """The user turn carries ``llm_json_schema()``; a type missing from it is
        a type the model will not emit."""
        parts = build_messages("QUJD", "image/png")[1]["content"]
        hint = next(p for p in parts if p["type"] == "text")["text"]
        for token in ("connector", "start_point", "end_point", "arrow_end", "image"):
            assert token in hint, f"{token!r} missing from the request's schema hint"


# ---------------------------------------------------------------------------
# analyze_image(verbose=True): the payload diagnostics main.py -v promises
# ---------------------------------------------------------------------------
class TestVerbosePayloadDiagnostics:
    def test_verbose_prints_the_request_shape_and_the_raw_reply(self, tmp_path, capsys):
        client = FakeClient(mock_data.SAMPLE_SPEC_JSON)
        analyze_image(_png(tmp_path), model="test-model", client=client, verbose=True)
        err = capsys.readouterr().err

        assert "test-model" in err
        assert "image/png" in err, "the image part's mime type must be reported"
        assert "base64 length=" in err, "the payload size must be reported"
        assert '"role": "system"' in err, "the request payload itself must be shown"
        assert "raw model reply" in err
        assert '"Quarterly Platform Review"' in err, "the model's reply must be echoed"

    def test_verbose_never_dumps_the_base64_image_body(self, tmp_path, capsys):
        path = _png(tmp_path)
        expected_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        analyze_image(path, client=FakeClient(mock_data.SAMPLE_SPEC_JSON), verbose=True)
        err = capsys.readouterr().err
        assert expected_b64 not in err
        assert "base64 chars omitted" in err

    def test_quiet_by_default(self, tmp_path, capsys):
        analyze_image(_png(tmp_path), client=FakeClient(mock_data.SAMPLE_SPEC_JSON))
        assert capsys.readouterr().err == "", "no diagnostics without verbose=True"

    def test_verbose_shows_the_payload_of_the_response_format_retry(self, tmp_path, capsys):
        client = FakeClient(FakeBadRequestError(), mock_data.SAMPLE_SPEC_JSON)
        analyze_image(_png(tmp_path), client=client, verbose=True)
        err = capsys.readouterr().err
        assert 'response_format={"type":"json_object"}' in err
        assert "response_format=omitted" in err, "the retry's payload must be shown too"

    def test_verbose_shows_the_reply_that_failed_to_parse(self, tmp_path, capsys):
        client = FakeClient("I am afraid I cannot read this image.")
        with pytest.raises(VisionAnalysisError):
            analyze_image(_png(tmp_path), client=client, verbose=True)
        err = capsys.readouterr().err
        assert "I am afraid I cannot read this image." in err

    def test_verbose_is_optional_so_the_pinned_signature_still_works(self, tmp_path):
        """Positional call shape from the contract must keep working."""
        client = FakeClient(mock_data.SAMPLE_SPEC_JSON)
        spec = analyze_image(_png(tmp_path), "test-model", client, "extra hint")
        assert isinstance(spec, SlideSpec)
