"""Tests for the instruction assist: ``app.services.instructions`` + its route.

Everything here is **fully offline**, and that is asserted rather than assumed:

* the service takes an injectable ``client``, so every unit test passes a fake
  chat-completions object and no test ever builds a real one;
* the endpoint tests reach the same fake by patching ``config.get_client``, and
  the ``_no_real_key`` default leaves the key BLANK so a path that forgot to
  ask for one answers 503 instead of dialling the proxy.

The API key is never real: ``config.LITELLM_API_KEY`` is a module global read at
call time, so the fake below is patched onto the module instead of the
environment - and it is an obvious placeholder, because
``tests/test_config.py`` scans this tree for the real key's literal.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import config, mock_data, vision_analyzer
from app.main import app
from app.services import converter, instructions
from app.services.converter import ConversionFailure
from app.services.instructions import RefineResult

from make_sample_image import make_sample_image

#: An obviously fake key. Never the real one (see the module docstring).
FAKE_KEY = "test-key-not-a-real-credential"

#: A Korean note like the ones the feature exists for.
NOTE_KO = "표가 이상해"

#: What a good model answers for that note, given the sample spec's digest.
GOOD_KO = (
    "우측 상단 영역은 3열 3행 표입니다. 첫 행(Region / Users / Growth)을 헤더로 "
    "인식하고, 표 안의 값을 개별 텍스트 상자로 분리하지 마세요."
)


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class FakeMessage:
    def __init__(self, content, refusal=None):
        self.content = content
        self.refusal = refusal


class FakeChoice:
    def __init__(self, content, refusal=None, finish_reason="stop"):
        self.message = FakeMessage(content, refusal)
        self.finish_reason = finish_reason


class FakeResponse:
    def __init__(self, content, refusal=None, finish_reason="stop"):
        self.choices = [FakeChoice(content, refusal, finish_reason)]


class FakeCompletions:
    """Records every call, so a test can assert on the PROMPT that was sent."""

    def __init__(self, reply, error=None):
        self.reply = reply
        self.error = error
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if isinstance(self.reply, BaseException):
            raise self.reply
        if self.reply is None or isinstance(self.reply, (str, list)):
            # the common case: canned assistant text
            return FakeResponse(self.reply)
        # an already-built response object, including a deliberately broken one
        return self.reply


class FakeClient:
    def __init__(self, reply="", error=None):
        self.chat = type("_Chat", (), {})()
        self.chat.completions = FakeCompletions(reply, error)

    @property
    def calls(self) -> list[dict]:
        return self.chat.completions.calls

    def sent_text(self, index: int = 0) -> str:
        """All the text of the user turn of call ``index``, concatenated."""
        content = self.calls[index]["messages"][1]["content"]
        if isinstance(content, str):
            return content
        return "\n".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )

    def sent_system(self, index: int = 0) -> str:
        return str(self.calls[index]["messages"][0]["content"])

    def image_urls(self, index: int = 0) -> list[str]:
        content = self.calls[index]["messages"][1]["content"]
        if isinstance(content, str):
            return []
        return [
            str(part.get("image_url", {}).get("url", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "image_url"
        ]


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def client() -> TestClient:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _no_real_key(monkeypatch):
    """Default to "no key configured" so nothing can accidentally use one."""
    monkeypatch.setattr(config, "LITELLM_API_KEY", "", raising=False)


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setattr(config, "LITELLM_API_KEY", FAKE_KEY, raising=False)
    return FAKE_KEY


@pytest.fixture
def served(monkeypatch, with_key):
    """Install a fake client behind ``config.get_client`` and hand it back."""

    def _install(reply=GOOD_KO, error=None) -> FakeClient:
        fake = FakeClient(reply, error)
        monkeypatch.setattr(config, "get_client", lambda: fake)
        return fake

    return _install


@pytest.fixture(scope="module")
def png_bytes(tmp_path_factory) -> bytes:
    """A real, decodable PNG (the project's own sample generator)."""
    path = tmp_path_factory.mktemp("images") / "sample.png"
    make_sample_image(str(path))
    return path.read_bytes()


SPEC_JSON = mock_data.SAMPLE_SPEC_JSON


def envelope(response) -> dict:
    """The error envelope, asserted to have exactly the contract's shape."""
    body = response.json()
    assert set(body) == {"detail"}, body
    detail = body["detail"]
    assert set(detail) == {"code", "message", "hint"}, detail
    for value in detail.values():
        assert isinstance(value, str)
    assert detail["code"] and detail["message"]
    return detail


def ok(response) -> dict:
    """A 200 body, asserted to be exactly the contract's keys.

    ``fell_back`` sits beside ``context_used`` rather than inside it: the
    contract's three context values keep their pinned meaning ("what informed
    the answer"), and the flag says whether the model produced anything at all.
    The UI needs both, because ``context_used == "none"`` alone cannot tell a
    genuine no-context run from an echo of the user's own note.
    """
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "status",
        "instruction",
        "note",
        "context_used",
        "fell_back",
    }, body
    assert body["status"] == "success"
    assert isinstance(body["instruction"], str) and body["instruction"].strip()
    assert body["context_used"] in {"spec", "image", "none"}
    assert isinstance(body["fell_back"], bool)
    return body


# =========================================================================== #
# the spec digest
# =========================================================================== #
class TestSpecDigest:
    def test_it_summarizes_the_sample_spec(self):
        digest = instructions.spec_digest(SPEC_JSON)

        assert digest.startswith("9 elements (")
        # count by type, as the contract asks for
        assert "shape 4" in digest and "text 4" in digest and "table 1" in digest
        assert "background #FFFFFF" in digest

    def test_it_names_positions_in_words_not_numbers(self):
        digest = instructions.spec_digest(SPEC_JSON)

        # the table sits at the right, below the header band
        assert "table - 우측 중앙" in digest
        assert "상단" in digest and "하단" in digest and "좌측" in digest

    def test_it_carries_no_raw_coordinates(self):
        """The whole point: a digest, not a dump of the spec."""
        digest = instructions.spec_digest(SPEC_JSON)

        assert "bbox" not in digest
        # every distinctive bbox component of the sample spec
        for coordinate in ("1000", "890", "640", "420", "470", "365", "330"):
            assert coordinate not in digest, coordinate

    def test_it_is_far_smaller_than_the_spec_itself(self):
        digest = instructions.spec_digest(SPEC_JSON)

        assert len(digest) <= instructions.DIGEST_MAX_CHARS
        assert len(digest) < len(SPEC_JSON) / 2

    def test_a_text_element_contributes_a_short_excerpt(self):
        digest = instructions.spec_digest(SPEC_JSON)

        assert '"Quarterly Platform Review"' in digest
        assert "32pt bold" in digest
        # the multi-line element is one line, excerpted, with no newlines in it
        assert "Highlights - p95" in digest
        for line in digest.splitlines():
            assert len(line) <= 120, line

    def test_a_table_contributes_its_shape_and_header_row(self):
        digest = instructions.spec_digest(SPEC_JSON)

        assert "3 rows x 3 cols" in digest
        assert "header: Region | Users | Growth" in digest

    def test_a_shape_contributes_its_kind_and_fill(self):
        digest = instructions.spec_digest(SPEC_JSON)

        assert "rounded_rectangle - fill #F2F5FA" in digest
        # the unfilled frame is described as a frame, not as a filled shape
        assert "no fill - border #9AA9C2" in digest

    def test_the_element_count_is_capped_and_the_rest_is_counted(self):
        spec = {
            "elements": [
                {"type": "text", "content": f"line {n}", "bbox": [10, 10, 100, 20]}
                for n in range(50)
            ]
        }
        digest = instructions.spec_digest(spec, max_elements=5)

        assert digest.count("\n") == 6  # header + 5 elements + the omitted line
        assert "and 45 more element(s) not listed" in digest

    def test_the_character_cap_wins_over_the_element_cap(self):
        spec = {
            "elements": [
                {"type": "text", "content": "x" * 200, "bbox": [10, 10, 100, 20]}
                for _ in range(30)
            ]
        }
        digest = instructions.spec_digest(spec, max_chars=400)

        assert len(digest) <= 400 + 40  # the omitted-count line is added after
        assert "more element(s) not listed" in digest

    @pytest.mark.parametrize(
        "spec",
        [
            None,
            "",
            "   ",
            "not json at all",
            '{"elements": [1, 2, 3}',  # truncated AND not element-shaped
            "[]",
            '{"elements": []}',
            '{"elements": "nope"}',
            "42",
            '"a string"',
            '{"background_color": "#FFF"}',
        ],
    )
    def test_an_unusable_spec_is_ignored_rather_than_fatal(self, spec):
        assert instructions.spec_digest(spec) == ""
        assert instructions.coerce_spec(spec) is None

    def test_a_spec_wrapped_in_an_envelope_key_still_works(self):
        digest = instructions.spec_digest(json.dumps({"spec": json.loads(SPEC_JSON)}))
        assert "3 rows x 3 cols" in digest

    def test_a_bare_element_array_still_works(self):
        digest = instructions.spec_digest(
            json.dumps(json.loads(SPEC_JSON)["elements"])
        )
        assert digest.startswith("9 elements (")

    def test_a_validated_slidespec_object_still_works(self):
        digest = instructions.spec_digest(mock_data.sample_spec())
        assert "header: Region | Users | Growth" in digest

    def test_an_absurdly_large_spec_string_is_ignored_not_parsed(self):
        huge = '{"elements": [' + '{"type": "text"},' * 200000 + "]}"
        assert len(huge) > instructions.SPEC_MAX_CHARS
        assert instructions.coerce_spec(huge) is None

    def test_an_element_with_a_broken_bbox_is_still_described(self):
        digest = instructions.spec_digest(
            {"elements": [{"type": "text", "content": "hi", "bbox": "nonsense"}]}
        )
        assert "위치 불명" in digest
        assert '"hi"' in digest
        # no position means no extent either - neither is invented
        assert "크기" not in digest and "영역" not in digest


# =========================================================================== #
# the digest's extent words
# =========================================================================== #
class TestSizeWords:
    """Every element's rough EXTENT, in words.

    Without it the digest gave type, position and content but no size at all,
    so a 460x5 divider and a full-slide card produced the same line and the
    commonest complaint class there is - 너무 크다 / 작다 / 두껍다 / 얇다 -
    had nothing to attach to. Measured against the live proxy, "구분선이 너무
    두꺼워" mis-targeted the 1000x155 header band in 5 of 8 runs without an
    extent word, and hit the real 460x5 rule in 3 of 3 with one.
    """

    def _line(self, bbox, **extra):
        spec = {"elements": [{"type": "shape", "bbox": bbox, **extra}]}
        return instructions.spec_digest(spec).splitlines()[1]

    def test_a_hairline_rule_is_not_described_as_a_box(self):
        """The finding's own element: an amber rule 460 wide and 5 tall."""
        assert "얇은 가로선" in self._line([505, 601, 460, 5], fill_color="#FFB000")

    def test_a_vertical_hairline_too(self):
        assert "얇은 세로선" in self._line([500, 100, 4, 700])

    def test_a_line_of_text_is_not_called_a_rule(self):
        """The mirror of the same bug: a 930x11 caption is thin and wide, and
        calling it "얇은 가로선" would point the analyzer at a divider that does
        not exist. Only a shape gets the rule wording."""
        spec = {
            "elements": [
                {"type": "text", "bbox": [35, 880, 930, 11], "content": "Source: ..."},
                {"type": "table", "bbox": [35, 500, 930, 20], "data": [["a", "b"]]},
                {"type": "shape", "bbox": [35, 300, 930, 11], "kind": "rectangle"},
            ]
        }
        text_line, table_line, shape_line = instructions.spec_digest(spec).splitlines()[1:]

        assert "한 줄 높이" in text_line and "얇은 가로선" not in text_line
        assert "한 줄 높이" in table_line
        assert "얇은 가로선" in shape_line

    def test_a_rule_and_a_card_no_longer_produce_the_same_line(self):
        """The literal collision this fixes: same kind, same fill, same centre
        third, wildly different objects."""
        spec = {
            "elements": [
                {"type": "shape", "kind": "rectangle", "bbox": [270, 498, 460, 5],
                 "fill_color": "#FFB000"},
                {"type": "shape", "kind": "rectangle", "bbox": [100, 200, 800, 600],
                 "fill_color": "#FFB000"},
            ]
        }
        first, second = instructions.spec_digest(spec).splitlines()[1:3]

        # identical but for the leading number, before the extent word existed
        assert first[2:] != second[2:]
        assert "얇은 가로선" in first
        assert "얇은 가로선" not in second

    def test_a_full_bleed_background_says_so(self):
        assert "슬라이드 전체 크기" in self._line([0, 0, 1000, 1000])

    def test_a_header_band_is_a_band_and_a_sidebar_is_a_column(self):
        assert "가로로 넓고 낮은 띠" in self._line([0, 0, 1000, 155])
        assert "세로로 좁고 긴 영역" in self._line([0, 100, 150, 800])

    def test_big_small_and_middling_are_told_apart(self):
        assert "큰 영역" in self._line([100, 100, 500, 400])
        assert "작은 영역" in self._line([890, 30, 80, 90])
        assert "중간 크기" in self._line([300, 300, 300, 150])

    def test_a_degenerate_box_is_reported_as_having_no_size(self):
        assert "크기 없음" in self._line([100, 100, 0, 0])
        assert "크기 없음" in self._line([100, 100, -5, 200])

    def test_every_element_of_the_sample_spec_carries_an_extent(self):
        lines = instructions.spec_digest(SPEC_JSON).splitlines()[1:]
        assert len(lines) == 9
        for line in lines:
            assert any(
                word in line
                for word in (
                    "얇은 가로선",
                    "얇은 세로선",
                    "한 줄 높이",
                    "슬라이드 전체 크기",
                    "가로로 넓고 낮은 띠",
                    "세로로 좁고 긴 영역",
                    "큰 영역",
                    "작은 영역",
                    "중간 크기",
                    "크기 없음",
                )
            ), line

    def test_the_extent_is_words_never_numbers(self):
        """A digest that carried "460x5" got the number quoted straight back at
        the analyzer, which works on its own 0..1000 grid."""
        line = self._line([505, 601, 460, 5])

        for digit in "0123456789":
            assert digit not in line.split(" - ", 1)[1], line
        assert "%" not in line

    def test_the_extent_words_fit_the_budget(self):
        """Compactness is the digest's whole reason to exist: a 200-element
        spec still comes back far under the cap WITH the extent words."""
        spec = {
            "elements": [
                {"type": "text", "content": f"line {n}", "bbox": [10, 10 + n, 300, 40]}
                for n in range(200)
            ]
        }
        digest = instructions.spec_digest(spec)

        assert len(digest) <= instructions.DIGEST_MAX_CHARS
        assert "and 170 more element(s) not listed" in digest


# =========================================================================== #
# a spec that parses but cannot be ENCODED
# =========================================================================== #
class TestAnUnencodableSpecIsStillOnlyContext:
    """``json.loads('"\\ud800"')`` returns a lone surrogate, which no UTF-8
    encoder accepts. Copied into the digest it did not fail in this module - it
    failed inside the openai client's request build, so a spec the contract
    promises is "parsed leniently; unusable JSON is ignored rather than
    rejected" turned into a 502 ``refine_failed`` whose hint was a raw python
    codec message in a Korean UI, with no way for that caller to get an assist
    at all.
    """

    #: Written out as JSON TEXT, not via ``json.dumps``: the defect needs the
    #: escape ``\ud800`` to survive into the parsed value, which is what a
    #: caller's file contains and what ``json.dumps`` of a python string would
    #: have escaped away.
    SURROGATE_SPEC = (
        '{"elements": [{"type": "text", '
        '"bbox": {"x": 10, "y": 10, "w": 300, "h": 50}, '
        '"content": "\\ud800 broken"}]}'
    )

    def test_the_fixture_really_is_the_defect(self):
        """Guards the test itself: an escape that parses back to text instead of
        a lone surrogate would make every assertion below vacuous."""
        content = json.loads(self.SURROGATE_SPEC)["elements"][0]["content"]

        assert content.startswith("\ud800")
        with pytest.raises(UnicodeEncodeError):
            content.encode("utf-8")

    def test_the_digest_is_encodable(self):
        digest = instructions.spec_digest(self.SURROGATE_SPEC)

        assert digest  # still summarized, not thrown away
        digest.encode("utf-8")  # the call that used to raise

    def test_a_surrogate_in_any_string_field_is_survivable(self):
        spec = {
            "background_color": "#FF\ud800",
            "elements": [
                {"type": "text\ud801", "bbox": [0, 0, 100, 20], "content": "\ud802"},
                {"type": "shape", "bbox": [0, 0, 100, 20], "kind": "rect\ud803",
                 "fill_color": "#0\ud804"},
                {"type": "table", "bbox": [0, 0, 100, 20],
                 "data": [["a\ud805", "b"], ["c", "d"]]},
            ],
        }
        instructions.spec_digest(spec).encode("utf-8")

    def test_the_request_goes_out_and_the_answer_comes_back(self, served):
        """The point of the fix: the assist still WORKS for that caller."""
        fake = served()
        result = instructions.refine_instruction(NOTE_KO, spec=self.SURROGATE_SPEC)

        assert result.instruction == GOOD_KO
        assert result.context_used == "spec"
        fake.sent_text().encode("utf-8")  # the body the client would encode

    def test_the_endpoint_answers_200_not_502(self, client, served):
        served()
        body = ok(
            client.post(
                "/api/refine-instructions",
                data={"note": NOTE_KO, "spec": self.SURROGATE_SPEC},
            )
        )

        assert body["context_used"] == "spec"

    def test_a_digest_that_somehow_raises_costs_only_the_context(
        self, monkeypatch, served, png_bytes
    ):
        """Structural version of the same promise: whatever a spec turns out to
        contain, it can cost the spec CONTEXT and never the request."""
        def _boom(*args, **kwargs):
            raise RuntimeError("some future spec shape nobody thought of")

        monkeypatch.setattr(instructions, "spec_digest", _boom)
        served()
        result = instructions.refine_instruction(
            NOTE_KO, spec=SPEC_JSON, image_bytes=png_bytes, mime="image/png"
        )

        # degraded to the image rather than failing the call
        assert result.context_used == "image"
        assert result.instruction == GOOD_KO


# =========================================================================== #
# context selection
# =========================================================================== #
class TestContextSelection:
    def test_a_spec_is_used_and_reported(self, served):
        fake = served()
        result = instructions.refine_instruction(NOTE_KO, spec=SPEC_JSON)

        assert result.context_used == "spec"
        assert result.instruction == GOOD_KO
        # text only: the cheap path makes no vision call
        assert fake.image_urls() == []
        assert "3 rows x 3 cols" in fake.sent_text()

    def test_an_image_is_used_when_there_is_no_spec(self, served, png_bytes):
        fake = served()
        result = instructions.refine_instruction(
            NOTE_KO, image_bytes=png_bytes, mime="image/png"
        )

        assert result.context_used == "image"
        urls = fake.image_urls()
        assert len(urls) == 1
        assert urls[0].startswith("data:image/png;base64,")
        assert len(urls[0]) > 100

    def test_neither_is_reported_as_none(self, served):
        fake = served()
        result = instructions.refine_instruction(NOTE_KO)

        assert result.context_used == "none"
        assert fake.image_urls() == []
        assert "None available" in fake.sent_text()

    def test_a_spec_beats_an_image(self, served, png_bytes):
        fake = served()
        result = instructions.refine_instruction(
            NOTE_KO, spec=SPEC_JSON, image_bytes=png_bytes, mime="image/png"
        )

        assert result.context_used == "spec"
        assert fake.image_urls() == [], "the cheap context must win"

    def test_a_broken_spec_falls_through_to_the_image(self, served, png_bytes):
        fake = served()
        result = instructions.refine_instruction(
            NOTE_KO, spec="{not json", image_bytes=png_bytes, mime="image/webp"
        )

        assert result.context_used == "image"
        assert fake.image_urls()[0].startswith("data:image/webp;base64,")

    def test_a_broken_spec_with_no_image_falls_through_to_none(self, served):
        served()
        result = instructions.refine_instruction(NOTE_KO, spec="{not json")
        assert result.context_used == "none"


# =========================================================================== #
# the prompt
# =========================================================================== #
class TestPrompt:
    def test_the_note_travels_verbatim(self, served):
        fake = served()
        instructions.refine_instruction("표 안의 숫자가 다 따로 떨어져 있어", spec=SPEC_JSON)

        assert "표 안의 숫자가 다 따로 떨어져 있어" in fake.sent_text()

    def test_the_system_prompt_pins_the_language_to_the_note(self, served):
        fake = served()
        instructions.refine_instruction(NOTE_KO)

        system = fake.sent_system()
        assert "SAME LANGUAGE" in system
        assert "match the note" in system.lower()

    def test_the_system_prompt_forbids_a_chat_reply(self, served):
        fake = served()
        instructions.refine_instruction(NOTE_KO)

        system = fake.sent_system()
        for forbidden in ("물론이죠", "code fences", "nothing else"):
            assert forbidden in system, forbidden
        assert "Never answer the user" in system

    def test_no_json_output_format_is_requested(self, served):
        """The reply is prose. Asking a proxied model for json_object here is
        exactly what the prompt forbids, and some proxies 400 on it."""
        fake = served()
        instructions.refine_instruction(NOTE_KO, spec=SPEC_JSON)

        assert "response_format" not in fake.calls[0]

    def test_the_configured_model_is_used_by_default(self, served, monkeypatch):
        monkeypatch.setattr(config, "DEFAULT_MODEL", "configured-model")
        fake = served()
        instructions.refine_instruction(NOTE_KO)

        assert fake.calls[0]["model"] == "configured-model"

    def test_an_explicit_model_overrides_it(self, served):
        fake = served()
        instructions.refine_instruction(NOTE_KO, model="other-model")

        assert fake.calls[0]["model"] == "other-model"

    def test_a_pasted_wall_of_text_is_capped_in_the_prompt(self, served):
        fake = served()
        note = "가" * 9000
        result = instructions.refine_instruction(note)

        assert len(fake.sent_text()) < 6000
        # ...but the note is still echoed back in full, untouched
        assert result.note == note


# =========================================================================== #
# language matching
# =========================================================================== #
class TestLanguage:
    def test_a_korean_note_keeps_the_korean_answer(self, served):
        served(GOOD_KO)
        result = instructions.refine_instruction(NOTE_KO, spec=SPEC_JSON)

        assert result.instruction == GOOD_KO
        assert "표" in result.instruction

    def test_an_english_note_keeps_the_english_answer(self, served):
        english = (
            "Treat the region at the top right as one 3-column, 3-row table and "
            "keep its first row (Region / Users / Growth) as the header."
        )
        served(english)
        result = instructions.refine_instruction(
            "the table looks wrong", spec=SPEC_JSON
        )

        assert result.instruction == english
        assert not any("가" <= char <= "힣" for char in result.instruction)

    def test_nothing_hardcodes_korean_into_the_answer(self, served):
        served("SOMETHING ENTIRELY DIFFERENT")
        result = instructions.refine_instruction("aiuto con la tabella")

        assert result.instruction == "SOMETHING ENTIRELY DIFFERENT"


# =========================================================================== #
# cleaning the reply
# =========================================================================== #
class TestReplyCleaning:
    @pytest.mark.parametrize(
        "reply",
        [
            "```\n우측 상단 표를 하나로 인식하세요.\n```",
            "```text\n우측 상단 표를 하나로 인식하세요.\n```",
            "```markdown\n우측 상단 표를 하나로 인식하세요.",  # never closed
            '"우측 상단 표를 하나로 인식하세요."',
            "'우측 상단 표를 하나로 인식하세요.'",
            "“우측 상단 표를 하나로 인식하세요.”",
            "`우측 상단 표를 하나로 인식하세요.`",
            "   우측 상단 표를 하나로 인식하세요.   ",
        ],
    )
    def test_fences_and_quotes_are_stripped(self, served, reply):
        served(reply)
        result = instructions.refine_instruction(NOTE_KO)

        assert result.instruction == "우측 상단 표를 하나로 인식하세요."
        assert "```" not in result.instruction
        assert result.fell_back is False

    @pytest.mark.parametrize(
        "reply",
        [
            '첫 행("Region")을 헤더로 인식하세요.',
            # begins AND ends with a quoted word - not a wrapper
            '"Region" 열을 헤더로 쓰고 마지막 열은 "Growth"',
            "표의 'Users' 열을 그대로 두세요.",
        ],
    )
    def test_a_quote_inside_the_sentence_is_kept(self, served, reply):
        served(reply)

        assert instructions.refine_instruction(NOTE_KO).instruction == reply

    def test_blank_lines_are_collapsed(self, served):
        served("첫 줄.\n\n\n\n두 번째 줄.")
        result = instructions.refine_instruction(NOTE_KO)

        assert result.instruction == "첫 줄.\n\n두 번째 줄."

    def test_the_output_is_capped(self, served):
        served("가나다라마바사. " * 400)
        result = instructions.refine_instruction(NOTE_KO)

        assert len(result.instruction) <= instructions.MAX_INSTRUCTION_CHARS
        assert result.instruction.endswith(".")  # cut at a sentence boundary
        assert result.fell_back is False

    def test_a_cap_with_no_sentence_boundary_still_fits(self, served):
        served("가" * 5000)
        result = instructions.refine_instruction(NOTE_KO)

        assert len(result.instruction) == instructions.MAX_INSTRUCTION_CHARS
        assert result.instruction.endswith("…")


# =========================================================================== #
# the fallback: the user's own text is never lost
# =========================================================================== #
class TestNeverEmpty:
    @pytest.mark.parametrize("reply", ["", "   ", "\n\n", None, "```\n```", "...", "!!"])
    def test_an_unusable_reply_falls_back_to_the_note(self, served, reply):
        served(reply)
        result = instructions.refine_instruction("  표가   이상해\n\n정말  ", spec=SPEC_JSON)

        assert result.instruction == "표가 이상해 정말"
        assert result.fell_back is True
        # never "": the frontend writes this straight into the textarea
        assert result.instruction.strip()

    def test_a_fallback_never_claims_the_context_was_used(self, served):
        """The spec WAS sent, but nothing of it reached the answer - so the UI
        must not say '슬라이드 분석 결과를 참고했습니다' about the user's own words."""
        served("")
        result = instructions.refine_instruction(NOTE_KO, spec=SPEC_JSON)

        assert result.instruction == NOTE_KO
        assert result.context_used == "none"
        assert result.fell_back is True

    def test_a_fallback_is_distinguishable_from_a_real_no_context_run(
        self, client, served
    ):
        """The two states that both answer ``context_used == "none"``.

        The UI writes a sentence about each, and they are different sentences:
        one says "there was nothing to go on, so this was written from your
        note", the other "the model could not complete it, so your note is
        untouched". Reporting only ``context_used`` made the second render as
        the first - blaming a missing context although a digest had been built
        and sent, and offering a 되돌리기 that restores identical text.
        """
        # (a) a genuine no-context run: the model really did write something
        served(GOOD_KO)
        genuine = ok(client.post("/api/refine-instructions", data={"note": NOTE_KO}))
        assert genuine["context_used"] == "none"
        assert genuine["fell_back"] is False
        assert genuine["instruction"] == GOOD_KO

        # (b) the fallback: a usable digest went up, nothing came back
        fake = served("")
        fallback = ok(
            client.post(
                "/api/refine-instructions",
                data={"note": NOTE_KO, "spec": SPEC_JSON},
            )
        )
        assert fallback["context_used"] == "none"
        assert fallback["fell_back"] is True
        assert fallback["instruction"] == NOTE_KO
        # ...and the context really was there, which is why "없어" would be false
        assert "header: Region | Users | Growth" in fake.sent_text()

    def test_the_digest_that_was_sent_is_still_reported_internally(self, served):
        """The operator and the CLI can tell the two apart without the body."""
        served("")
        result = instructions.refine_instruction(NOTE_KO, spec=SPEC_JSON)

        assert result.fell_back is True
        assert "3 rows x 3 cols" in result.digest

    def test_a_refusal_falls_back_too(self, served):
        served(FakeResponse(None, refusal="I cannot help with that"))
        result = instructions.refine_instruction(NOTE_KO)

        assert result.instruction == NOTE_KO
        assert result.fell_back is True

    def test_a_long_note_is_capped_in_the_fallback(self, served):
        served("")
        result = instructions.refine_instruction("가" * 5000)

        assert len(result.instruction) <= instructions.MAX_INSTRUCTION_CHARS
        assert result.instruction.strip()

    def test_the_note_is_echoed_exactly_as_received(self, served):
        served()
        note = "  표가\t이상해  "
        result = instructions.refine_instruction(note, spec=SPEC_JSON)

        assert result.note == note


# =========================================================================== #
# errors
# =========================================================================== #
class TestErrors:
    @pytest.mark.parametrize("note", ["", "   ", "\t\n ", None])
    def test_a_blank_note_is_empty_note(self, served, note):
        served()
        with pytest.raises(ConversionFailure) as info:
            instructions.refine_instruction(note)

        failure = info.value
        assert failure.status_code == 400
        assert failure.code == "empty_note"
        assert failure.hint

    def test_a_blank_note_is_refused_before_the_model_is_called(self, served):
        fake = served()
        with pytest.raises(ConversionFailure):
            instructions.refine_instruction("   ", spec=SPEC_JSON)

        assert fake.calls == []

    def test_no_key_is_missing_api_key(self):
        with pytest.raises(ConversionFailure) as info:
            instructions.refine_instruction(NOTE_KO, spec=SPEC_JSON)

        failure = info.value
        assert failure.status_code == 503
        assert failure.code == "missing_api_key"
        assert "LITELLM_API_KEY" in failure.hint

    @pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
    def test_a_blank_key_counts_as_missing(self, monkeypatch, blank):
        monkeypatch.setattr(config, "LITELLM_API_KEY", blank, raising=False)
        with pytest.raises(ConversionFailure) as info:
            instructions.refine_instruction(NOTE_KO)

        assert info.value.code == "missing_api_key"

    def test_the_key_check_happens_before_any_client_is_built(self, monkeypatch):
        def _bomb():  # pragma: no cover - a call is the failure
            raise AssertionError("no client may be built without a key")

        monkeypatch.setattr(config, "get_client", _bomb)
        with pytest.raises(ConversionFailure) as info:
            instructions.refine_instruction(NOTE_KO)

        assert info.value.code == "missing_api_key"

    def test_a_missing_key_raised_by_get_client_is_still_a_503(
        self, monkeypatch, with_key
    ):
        def _no_key():
            raise config.MissingApiKeyError("no key")

        monkeypatch.setattr(config, "get_client", _no_key)
        with pytest.raises(ConversionFailure) as info:
            instructions.refine_instruction(NOTE_KO)

        assert info.value.status_code == 503
        assert info.value.code == "missing_api_key"

    def test_an_unreachable_proxy_is_proxy_unreachable(self, served):
        import httpx
        import openai

        served(error=openai.APIConnectionError(
            request=httpx.Request("POST", "http://localhost:4000/")
        ))
        with pytest.raises(ConversionFailure) as info:
            instructions.refine_instruction(NOTE_KO)

        failure = info.value
        assert failure.status_code == 502
        assert failure.code == "proxy_unreachable"
        assert "LITELLM_BASE_URL" in failure.hint

    def test_any_other_upstream_error_is_refine_failed(self, served):
        served(error=RuntimeError("the proxy said 500 Internal Server Error"))
        with pytest.raises(ConversionFailure) as info:
            instructions.refine_instruction(NOTE_KO)

        failure = info.value
        assert failure.status_code == 502
        assert failure.code == "refine_failed"
        assert "RuntimeError" in failure.message
        assert "500 Internal Server Error" in failure.hint
        # a scrubbed line must not read as a dead end
        assert converter._UPSTREAM_LOG_HINT in failure.hint

    def test_the_hint_does_not_restate_the_korean_copy(self, served):
        """``refine_failed`` is a ``keepServerDetail`` code: whatever is in this
        hint is printed UNDER the Korean sentence in the banner. It used to end
        with "Your note was not changed - edit it and try again, or just convert
        with the note as it is", which is a word-for-word English translation of
        that Korean sentence - so the banner gave the same advice twice, once
        per language, in a Korean UI. Only what a fixed Korean sentence cannot
        know belongs here: the upstream text, and where to read it unredacted.
        """
        served(error=RuntimeError("the proxy said 500 Internal Server Error"))
        with pytest.raises(ConversionFailure) as info:
            instructions.refine_instruction(NOTE_KO)

        hint = info.value.hint.lower()
        for restatement in ("try again", "your note", "was not changed", "convert with"):
            assert restatement not in hint, restatement

    def test_a_silent_upstream_error_still_leaves_a_usable_hint(self, served):
        """``RuntimeError("")`` used to make the hint nothing BUT the English
        duplicate. Now the only thing left is the pointer to the server log,
        which is the one sentence that is still true and still useful."""
        served(error=RuntimeError("   "))
        with pytest.raises(ConversionFailure) as info:
            instructions.refine_instruction(NOTE_KO)

        assert info.value.hint.strip() == converter._UPSTREAM_LOG_HINT

    def test_a_malformed_response_is_refine_failed_not_a_fallback(self, served):
        """No choices at all is not "the model said nothing" - it is a broken
        response, and pretending otherwise would hide a real proxy fault behind
        an echo of the user's note."""
        fake = served()
        fake.chat.completions.reply = type("_NoChoices", (), {"choices": []})()

        with pytest.raises(ConversionFailure) as info:
            instructions.refine_instruction(NOTE_KO)

        assert info.value.status_code == 502
        assert info.value.code == "refine_failed"

    def test_a_client_that_is_not_an_openai_client_is_refine_failed(self):
        with pytest.raises(ConversionFailure) as info:
            instructions.refine_instruction(NOTE_KO, client=object())

        assert info.value.status_code == 502
        assert info.value.code == "refine_failed"
        assert "OpenAI client" in info.value.hint

    def test_build_failed_is_never_used_here(self, served):
        served(error=RuntimeError("anything at all"))
        with pytest.raises(ConversionFailure) as info:
            instructions.refine_instruction(NOTE_KO)

        assert info.value.code != "build_failed"
        assert info.value.status_code != 500


# =========================================================================== #
# secret hygiene
# =========================================================================== #
class TestSecretsAreScrubbed:
    def test_the_configured_key_never_reaches_a_hint(self, served, with_key):
        served(error=RuntimeError(
            f"AuthenticationError: Received API Key = {with_key}, "
            "Key Hash (Token) = 0123456789abcdef0123456789abcdef0123456789abcdef"
        ))
        with pytest.raises(ConversionFailure) as info:
            instructions.refine_instruction(NOTE_KO)

        failure = info.value
        body = f"{failure.message}\n{failure.hint}"
        assert with_key not in body
        assert "0123456789abcdef" not in body
        assert "[redacted]" in body
        # the LABEL survives, so the operator still knows what went wrong
        assert "API Key" in body

    def test_an_sk_style_key_in_the_error_is_scrubbed(self, served):
        served(error=RuntimeError("invalid key sk-abcd1234efgh5678 rejected"))
        with pytest.raises(ConversionFailure) as info:
            instructions.refine_instruction(NOTE_KO)

        assert "sk-abcd1234efgh5678" not in f"{info.value.message}{info.value.hint}"

    def test_the_endpoint_body_carries_no_secret(self, client, served, with_key):
        served(error=RuntimeError(f"Bearer {with_key} was refused"))
        response = client.post(
            "/api/refine-instructions", data={"note": NOTE_KO}
        )

        assert response.status_code == 502
        assert envelope(response)["code"] == "refine_failed"
        assert with_key not in response.text


# =========================================================================== #
# POST /api/refine-instructions
# =========================================================================== #
class TestEndpoint:
    def test_note_only(self, client, served):
        served()
        body = ok(client.post("/api/refine-instructions", data={"note": NOTE_KO}))

        assert body["instruction"] == GOOD_KO
        assert body["note"] == NOTE_KO
        assert body["context_used"] == "none"

    def test_note_and_spec(self, client, served):
        fake = served()
        body = ok(
            client.post(
                "/api/refine-instructions",
                data={"note": NOTE_KO, "spec": SPEC_JSON},
            )
        )

        assert body["context_used"] == "spec"
        assert "header: Region | Users | Growth" in fake.sent_text()

    def test_note_and_file(self, client, served, png_bytes):
        fake = served()
        body = ok(
            client.post(
                "/api/refine-instructions",
                data={"note": NOTE_KO},
                files={"file": ("sample.png", png_bytes, "image/png")},
            )
        )

        assert body["context_used"] == "image"
        assert fake.image_urls()[0].startswith("data:image/png;base64,")

    def test_note_spec_and_file_prefers_the_spec(self, client, served, png_bytes):
        fake = served()
        body = ok(
            client.post(
                "/api/refine-instructions",
                data={"note": NOTE_KO, "spec": SPEC_JSON},
                files={"file": ("sample.png", png_bytes, "image/png")},
            )
        )

        assert body["context_used"] == "spec"
        assert fake.image_urls() == []

    def test_an_explicit_model_is_honoured(self, client, served):
        fake = served()
        ok(
            client.post(
                "/api/refine-instructions",
                data={"note": NOTE_KO, "model": "  picked-model  "},
            )
        )

        assert fake.calls[0]["model"] == "picked-model"

    def test_a_blank_model_field_falls_back_to_the_server_default(
        self, client, served, monkeypatch
    ):
        monkeypatch.setattr(config, "DEFAULT_MODEL", "server-default")
        fake = served()
        ok(
            client.post(
                "/api/refine-instructions",
                data={"note": NOTE_KO, "model": "   "},
            )
        )

        assert fake.calls[0]["model"] == "server-default"

    def test_file_is_not_required(self, client, served):
        served()
        response = client.post("/api/refine-instructions", data={"note": NOTE_KO})
        assert response.status_code == 200, response.text

    def test_an_empty_file_part_is_not_an_error(self, client, served):
        """A browser posting a form with an empty file input still sends the
        part; that is "no image yet", not a 0-byte upload."""
        served()
        body = ok(
            client.post(
                "/api/refine-instructions",
                data={"note": NOTE_KO},
                files={"file": ("", b"", "application/octet-stream")},
            )
        )

        assert body["context_used"] == "none"

    @pytest.mark.parametrize(
        "spec",
        ["{not json", "null", "[]", "", "   ", "<html>nope</html>", "{" * 5000],
    )
    def test_a_malformed_spec_never_500s(self, client, served, spec):
        served()
        body = ok(
            client.post(
                "/api/refine-instructions", data={"note": NOTE_KO, "spec": spec}
            )
        )

        assert body["context_used"] == "none"

    @pytest.mark.parametrize("note", ["", "   ", "\t\n"])
    def test_a_blank_note_is_a_400_empty_note(self, client, served, note):
        served()
        response = client.post("/api/refine-instructions", data={"note": note})

        assert response.status_code == 400
        assert envelope(response)["code"] == "empty_note"

    def test_a_missing_note_field_is_also_empty_note(self, client, served):
        """Not fastapi's 422: the contract pins one code for "no note"."""
        served()
        response = client.post("/api/refine-instructions", data={})

        assert response.status_code == 400
        assert envelope(response)["code"] == "empty_note"

    def test_a_blank_note_beats_a_bad_file(self, client, served):
        served()
        response = client.post(
            "/api/refine-instructions",
            data={"note": "  "},
            files={"file": ("x.png", b"not a png", "image/png")},
        )

        assert response.status_code == 400
        assert envelope(response)["code"] == "empty_note"

    def test_an_oversized_file_is_413_file_too_large(
        self, client, served, monkeypatch
    ):
        served()
        monkeypatch.setattr(converter, "MAX_UPLOAD_BYTES", 1024)
        response = client.post(
            "/api/refine-instructions",
            data={"note": NOTE_KO},
            files={"file": ("big.png", b"\x89PNG" + b"0" * 4096, "image/png")},
        )

        assert response.status_code == 413
        assert envelope(response)["code"] == "file_too_large"

    def test_a_file_that_is_not_an_image_is_400_not_an_image(self, client, served):
        served()
        response = client.post(
            "/api/refine-instructions",
            data={"note": NOTE_KO},
            files={"file": ("slide.png", b"<html>not a png</html>", "image/png")},
        )

        assert response.status_code == 400
        assert envelope(response)["code"] == "not_an_image"

    def test_an_unsupported_extension_is_refused(self, client, served):
        served()
        response = client.post(
            "/api/refine-instructions",
            data={"note": NOTE_KO},
            files={"file": ("notes.pdf", b"%PDF-1.4", "application/pdf")},
        )

        assert response.status_code == 400
        assert envelope(response)["code"] == "unsupported_extension"

    def test_no_key_is_503(self, client):
        response = client.post("/api/refine-instructions", data={"note": NOTE_KO})

        assert response.status_code == 503
        assert envelope(response)["code"] == "missing_api_key"

    def test_an_unreachable_proxy_is_502(self, client, served):
        import httpx
        import openai

        served(error=openai.APIConnectionError(
            request=httpx.Request("POST", "http://localhost:4000/")
        ))
        response = client.post("/api/refine-instructions", data={"note": NOTE_KO})

        assert response.status_code == 502
        assert envelope(response)["code"] == "proxy_unreachable"

    def test_an_empty_reply_still_answers_200_with_the_note(self, client, served):
        served("")
        body = ok(client.post("/api/refine-instructions", data={"note": NOTE_KO}))

        assert body["instruction"] == NOTE_KO
        assert body["context_used"] == "none"

    def test_a_crash_inside_the_service_is_a_500_envelope_not_a_traceback(
        self, client, served, monkeypatch
    ):
        served()

        def _boom(*args, **kwargs):
            raise RuntimeError("secret internal detail at /srv/app/thing.py:42")

        monkeypatch.setattr(instructions, "refine_instruction", _boom)
        response = client.post("/api/refine-instructions", data={"note": NOTE_KO})

        assert response.status_code == 500
        assert envelope(response)["code"] == "internal_error"
        assert "secret internal detail" not in response.text
        assert "Traceback" not in response.text

    def test_a_client_that_cannot_be_built_is_a_mapped_failure(
        self, client, monkeypatch, with_key
    ):
        """Proves the endpoint never reaches a real client: the only way to the
        proxy is ``config.get_client``, and a failure there stays inside the
        contract's codes instead of leaving as an unhandled 500."""

        def _refuse():
            raise RuntimeError("openai is not installed")

        monkeypatch.setattr(config, "get_client", _refuse)
        response = client.post("/api/refine-instructions", data={"note": NOTE_KO})

        assert response.status_code == 502
        assert envelope(response)["code"] == "refine_failed"


# =========================================================================== #
# reuse: no duplicated openai plumbing
# =========================================================================== #
class TestReuse:
    def test_the_reply_reader_is_the_analyzer_s(self, monkeypatch, served):
        """``vision_analyzer._message_content`` is the one place that knows how
        to read an OpenAI-shaped reply; this module must not grow a second."""
        seen: list[object] = []
        original = vision_analyzer._message_content

        def _spy(response):
            seen.append(response)
            return original(response)

        monkeypatch.setattr(vision_analyzer, "_message_content", _spy)
        served()
        instructions.refine_instruction(NOTE_KO)

        assert len(seen) == 1

    def test_a_list_of_content_parts_is_understood(self, served):
        """Some proxies return content as parts - the analyzer's reader already
        handles that shape, which is why it is reused."""
        served(FakeResponse([{"text": "앞부분 "}, {"text": "뒷부분."}]))
        result = instructions.refine_instruction(NOTE_KO)

        assert result.instruction == "앞부분 뒷부분."

    def test_the_spec_cap_is_converter_s_json_cap(self):
        assert instructions.SPEC_MAX_CHARS == converter.MAX_JSON_BODY_BYTES

    def test_the_mime_table_is_config_s(self):
        assert instructions.mime_for_upload("a.png") == "image/png"
        assert instructions.mime_for_upload("a.JPG") == "image/jpeg"
        assert instructions.mime_for_upload("a.webp") == "image/webp"
        assert instructions.mime_for_upload(None) == "image/png"
        assert instructions.mime_for_upload("a.bmp") == "image/png"

    def test_the_service_imports_no_web_framework(self):
        """Framework-free, so the CLI could use it: asserted, not hoped for.

        The IMPORTS are inspected rather than the text, so the docstring may go
        on explaining the rule without breaking it."""
        import ast
        import pathlib

        tree = ast.parse(
            pathlib.Path(instructions.__file__).read_text(encoding="utf-8")
        )
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")

        assert imported, "nothing imported at all - the parse must have failed"
        for name in imported:
            assert not name.startswith(("fastapi", "starlette")), name


# =========================================================================== #
# the result object
# =========================================================================== #
class TestRefineResult:
    def test_the_payload_is_exactly_the_contract(self):
        result = RefineResult(instruction="do the thing", note="note", context_used="spec")

        assert result.payload() == {
            "status": "success",
            "instruction": "do the thing",
            "note": "note",
            "context_used": "spec",
            "fell_back": False,
        }

    def test_the_fallback_flag_is_reported_not_hidden(self):
        """The UI cannot describe a fallback without it.

        ``context_used == "none"`` covers two different events - "there was no
        context, so the instruction was written from the note alone" and "a
        digest was built and sent, and the model gave nothing back" - and the
        Korean copy for them is not the same sentence. Inferring one from the
        other is how a UI ends up blaming a missing context that was present.
        """
        result = RefineResult(
            instruction="i", note="n", context_used="none", fell_back=True, digest="d"
        )

        assert result.payload()["fell_back"] is True

    def test_the_digest_stays_out_of_the_body(self):
        """Prompt-sized, for the log and for tests. No client has a use for it."""
        result = RefineResult(
            instruction="i", note="n", context_used="none", fell_back=True, digest="d"
        )

        assert "digest" not in result.payload()
