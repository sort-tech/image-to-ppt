"""Source-level guards for the frontend defects fixed alongside the API.

Why these are assertions on the SOURCE rather than DOM tests: the three bugs
below are layout and lifecycle properties, and the project ships no browser test
runner (no vitest/jsdom in ``frontend/package.json``) - and jsdom has no layout
engine anyway, so even with one it could not measure the overflow that is the
whole point of two of them. What a DOM test would really be asserting is that
the containment rule is still on the element; that is exactly what is checked
here, in the suite the project actually runs, with the reason each rule exists
written next to it.

Each check quotes the failure it prevents, so a future edit that drops a class
fails with the bug description rather than with "string not found".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"

pytestmark = pytest.mark.skipif(
    not FRONTEND.is_dir(), reason="frontend/src is not present in this checkout"
)


def source(*parts: str) -> str:
    path = FRONTEND.joinpath(*parts)
    assert path.is_file(), f"missing frontend source: {path}"
    return path.read_text(encoding="utf-8")


def class_lists(text: str, *, containing: str) -> list[str]:
    """Every ``className`` value in ``text`` that mentions ``containing``."""
    found = []
    for match in re.finditer(r'className=(?:"([^"]*)"|\{`([^`]*)`\})', text):
        value = match.group(1) or match.group(2) or ""
        if containing in value:
            found.append(value)
    return found


def has_break_rule(class_list: str) -> bool:
    """Whether a class list contains a rule that breaks an unbreakable token.

    ``[overflow-wrap:anywhere]`` is the one that also shrinks the element's
    MIN-CONTENT width, which is what stops a long token from widening a grid
    column and dragging the page into a horizontal scroll; ``break-all`` has the
    same effect with harsher typography. ``break-words`` alone is not enough.
    """
    return "[overflow-wrap:anywhere]" in class_list or "break-all" in class_list


# --------------------------------------------------------------------------- #
# ResultView: `dropped` / `warnings` are backend text of unbounded length
# --------------------------------------------------------------------------- #
class TestResultViewContainsLongDiagnostics:
    """A drop reason quotes the rejected element's raw ``type`` verbatim, so one
    junk value from the model puts a 300-character unbreakable token in the
    list. Without a break rule that token set the list's min-content width,
    widened the grid column and dragged the whole document into a horizontal
    scroll (measured: document.scrollWidth 2731 at a 1009px viewport, and ~42
    unbroken characters is enough at 360px). A render warning is forwarded
    stderr, so it has no length guarantee either.
    """

    #: What both diagnostic ``<li>``s look like, and nothing else in the file.
    LI_MARKER = "px-3 py-2 font-mono text-xs leading-relaxed"

    def test_the_diagnostic_list_items_can_wrap(self):
        text = source("components", "ResultView.tsx")
        lists = class_lists(text, containing=self.LI_MARKER)
        assert len(lists) == 2, (
            "expected exactly the dropped and warnings <li> class lists - "
            "re-point this guard"
        )
        assert any("amber" in value for value in lists), "the dropped <li> moved"
        assert any("slate-300" in value for value in lists), "the warnings <li> moved"
        for class_list in lists:
            assert has_break_rule(class_list), (
                "a diagnostic <li> can no longer break a long token: a single "
                "unbroken reason will widen the column and scroll the page"
            )

    def test_the_lists_cannot_widen_their_column(self):
        text = source("components", "ResultView.tsx")
        assert text.count('className="min-w-0 space-y-1.5"') == 2, (
            "both diagnostic <ul>s need min-w-0 so their min-content width "
            "cannot push the grid track wider than its share of the page"
        )

    def test_the_two_columns_are_allowed_to_shrink(self):
        app = source("App.tsx")
        assert 'className="min-w-0 space-y-5"' in app
        assert 'className="min-w-0 space-y-4"' in app


# --------------------------------------------------------------------------- #
# ErrorBanner: the hint can be a concatenated list of per-element drop reasons
# --------------------------------------------------------------------------- #
class TestErrorBannerHintStaysInsideTheBanner:
    """``POST /api/build-from-json`` answers ``invalid_spec`` with a hint that
    concatenates the drop reasons, so the same 300-character token lands in a
    banner that is 343px wide at mobile size. Measured before the fix: the hint
    span was 1948px wide with its right edge 1668px outside a 375px viewport,
    and no scroll container existed to reach it.
    """

    def test_the_hint_span_wraps_and_may_shrink(self):
        text = source("App.tsx")
        match = re.search(r"<span className=\"([^\"]*)\">\s*\{error\.hint\}", text)
        assert match, "the hint <span> changed shape - re-point this guard"
        class_list = match.group(1)
        assert has_break_rule(class_list), "a long hint will leave the banner"
        assert "min-w-0" in class_list, (
            "without min-w-0 the span cannot shrink below its content inside "
            "the flex parent, so the break rule never gets a chance"
        )

    def test_the_message_paragraph_wraps_too(self):
        text = source("App.tsx")
        match = re.search(r"<p className=\"([^\"]*)\">\s*\{error\.message\}", text)
        assert match, "the message <p> changed shape - re-point this guard"
        assert has_break_rule(match.group(1))

    def test_a_huge_hint_scrolls_instead_of_growing_past_the_viewport(self):
        """Wrapping a 100-line hint would otherwise make the banner taller than
        the screen, with its top unreachable (it is position: fixed)."""
        text = source("App.tsx")
        match = re.search(r"<span className=\"([^\"]*)\">\s*\{error\.hint\}", text)
        assert match
        assert "overflow-y-auto" in match.group(1)
        assert re.search(r"max-h-\d+", match.group(1))


# --------------------------------------------------------------------------- #
# FileUploader: the focus ring has to be on a SIBLING of the peer input
# --------------------------------------------------------------------------- #
class TestFileUploaderFocusRingCanMatch:
    """Tailwind compiles ``peer-*`` to a general-sibling selector
    (``.peer:focus-visible ~ .peer-focus-visible\\:ring-2``). The ring classes
    used to sit on the nested "Replace" chip, which is a grandchild of the
    input's sibling, so the rule could never match - and the input's own
    ``:focus-visible`` ring is drawn on an ``sr-only`` 1x1 clipped box. Tabbing
    to the file picker with a file selected therefore showed no focus indicator
    anywhere (WCAG 2.4.7).
    """

    def test_the_input_is_still_the_peer(self):
        text = source("components", "FileUploader.tsx")
        assert 'className="peer sr-only"' in text

    def test_the_ringed_element_is_the_inputs_next_sibling(self):
        text = source("components", "FileUploader.tsx")
        for class_list in class_lists(text, containing="peer-focus-visible:ring-2"):
            # Each ring target must be introduced immediately after the input
            # element (the empty-state drop zone) or be the preview wrapper that
            # directly follows it.
            assert "peer-focus-visible:ring-sky-400" in class_list
        after_input = text.split('className="peer sr-only"', 1)[1]
        next_element = re.search(r"<(\w+)[^>]*className=(?:\"([^\"]*)\"|\{`([^`]*)`\})", after_input)
        assert next_element, "nothing follows the file input any more"
        wrapper_classes = next_element.group(2) or next_element.group(3) or ""
        assert "peer-focus-visible:ring-2" in wrapper_classes, (
            "the element right after the peer input carries no focus ring, so "
            "the file picker has no visible focus indicator in this branch"
        )

    def test_the_replace_chip_no_longer_claims_a_ring_it_cannot_get(self):
        text = source("components", "FileUploader.tsx")
        chip = re.search(r"<label\s+htmlFor=\{inputId\}\s+className=\{`([^`]*)`", text)
        assert chip, "the Replace chip changed shape - re-point this guard"
        assert "peer-focus-visible" not in chip.group(1), (
            "a nested element cannot match the peer sibling selector; leaving "
            "the classes there only hides the missing focus indicator"
        )

    def test_a_leading_dot_name_has_no_extension_client_side_either(self):
        """``.png`` is a name, not an extension - both layers have to agree, or
        the client forwards a file the backend can only answer with 400."""
        text = source("components", "FileUploader.tsx")
        assert "dot <= 0" in text, (
            "extensionOf() accepts '.png' as an extension again, which the "
            "backend (Path('.png').suffix == '') rejects"
        )
        # ...and the user is told which of the two things went wrong. The copy
        # is localized, so this anchors on the BRANCH and on the two literals
        # that carry the explanation in any language - pinning an English
        # sentence here would only break the next translation.
        assert re.search(r"\bext\s*\n?\s*\?", text), (
            "the rejection no longer branches on whether an extension was "
            "found, so a name with no extension gets the same message as an "
            "unsupported one - the two need different fixes"
        )
        assert '".png"' in text and '"slide.png"' in text, (
            "the no-extension message no longer contrasts '.png' with "
            "'slide.png', which is the one thing that tells the user what to "
            "actually rename the file to"
        )


# --------------------------------------------------------------------------- #
# App: a failed conversion may not leave the previous run downloadable
# --------------------------------------------------------------------------- #
class TestStaleResultIsCleared:
    """``handleConvert`` cleared ``error`` and ``rebuildNote`` but never
    ``result``, so after a failure the panel still read "CONVERSION COMPLETE"
    with the OLD filename, the old tallies and a working Download button bound
    to the old ``download_id`` - verified end to end: the click served the first
    run's bytes - while the Spec JSON tab still held the old spec, so "Rebuild"
    silently rebuilt the previous slide.
    """

    def test_handle_convert_drops_the_previous_result_before_awaiting(self):
        text = source("App.tsx")
        body = text.split("const handleConvert", 1)[1].split("const handleDownload", 1)[0]
        clear = body.find("setResult(null)")
        request = body.find("await convert(")
        assert clear != -1, "handleConvert no longer clears the previous result"
        assert request != -1, "handleConvert no longer calls convert()"
        assert clear < request, (
            "the stale result survives while the new request is in flight, so "
            "a failure leaves the old deck downloadable"
        )

    def test_choosing_another_file_drops_it_too(self):
        text = source("App.tsx")
        body = text.split("const chooseFile", 1)[1].split("const disabledReason", 1)[0]
        assert "setFile(next)" in body
        assert "setResult(null)" in body
        assert "onSelect={chooseFile}" in text
        assert "onClear={() => chooseFile(null)}" in text


# --------------------------------------------------------------------------- #
# The UI is Korean: the copy has to read like Korean, and cover every code
# --------------------------------------------------------------------------- #
#: Everything under ``frontend/src`` whose text can reach a user.
_COPY_SUFFIXES = {".ts", ".tsx", ".css"}

#: The bracketed either/or particles a translator leaves for someone else to
#: resolve. Shipped copy has to pick one.
PLACEHOLDER_PARTICLE = re.compile(r"은\(는\)|이\(가\)|을\(를\)|와\(과\)|으로\(로\)")

#: A particle floating one space after a closing delimiter - which in practice
#: means after a ``${...}`` interpolation, a quoted filename or a code span.
#: Korean binds a particle to the word before it with no space, so this pattern
#: only matches broken orthography; a particle attached to its own word (the
#: correct form) has no space to match on.
DETACHED_PARTICLE = re.compile(
    r"""["'`)\}\]] (은|는|이|가|을|를|에|에는|의|으로|로|와|과|처럼|에서|부터|까지)"""
    r"""(?=[\s.,!?]|$)"""
)

HANGUL = re.compile(r"[가-힣]")


def copy_files() -> list[Path]:
    found = [p for p in sorted(FRONTEND.rglob("*")) if p.suffix in _COPY_SUFFIXES]
    assert len(found) >= 8, f"the copy scan found only {len(found)} files"
    return found


def copy_lines() -> list[tuple[Path, int, str]]:
    """Every source line that is not a whole-line comment.

    Whole-line comments are dropped so a comment *describing* a defect (or
    quoting the wrong form to warn against it) cannot fail the guard; only a
    line that can carry a string literal is scanned. Mid-line ``//`` is left
    alone on purpose - stripping it would eat the ``http://`` inside a hint.
    """
    out: list[tuple[Path, int, str]] = []
    for path in copy_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith(("//", "/*", "*")):
                continue
            out.append((path, number, line))
    return out


class TestTheKoreanCopyReadsLikeKorean:
    """Three orthography defects the first Korean pass shipped, all of the same
    kind: a particle was pasted after a value instead of onto a word. The file
    picker said ``"my report.gif" 은(는) 지원하지 않는...`` (a space before the
    particle AND the fill-in-the-blank form), and a hint said
    ``` ...--port 8000` 으로 실행한 뒤 ```.
    """

    def test_no_bracketed_either_or_particle_is_shipped(self):
        offenders = [
            f"{path.relative_to(FRONTEND)}:{number}: {line.strip()}"
            for path, number, line in copy_lines()
            if PLACEHOLDER_PARTICLE.search(line)
        ]
        assert offenders == [], (
            "shipped copy still leaves the reader to pick the particle; "
            f"restructure so none follows a variable: {offenders}"
        )

    def test_no_particle_floats_after_a_value(self):
        offenders = [
            f"{path.relative_to(FRONTEND)}:{number}: {line.strip()}"
            for path, number, line in copy_lines()
            if DETACHED_PARTICLE.search(line)
        ]
        assert offenders == [], (
            "a Korean particle is separated from the word it binds to - put "
            f"the interpolated value at the end of its clause instead: {offenders}"
        )

    def test_the_guard_can_actually_fail(self):
        """A pattern that matches nothing would pass forever."""
        assert PLACEHOLDER_PARTICLE.search('`"${name}" 은(는) 지원하지 않습니다`')
        assert DETACHED_PARTICLE.search('`"${name}" 에는 확장자가 없습니다.`')
        # ...and the correct form does NOT match, or the guard is unsatisfiable.
        assert not DETACHED_PARTICLE.search("`확장자가 없습니다: \"${name}\".`")
        assert not DETACHED_PARTICLE.search("`${name}에는 확장자가 없습니다.`")

    def test_the_file_picker_puts_the_filename_at_the_end_of_its_clause(self):
        text = source("components", "FileUploader.tsx")
        for message in re.findall(r"setRejection\(\s*`([^`]*)`", text) + re.findall(
            r"^\s*[?:]\s*`([^`]*)`", text, re.M
        ):
            if "${candidate.name}" not in message:
                continue
            # Past the name and its closing quote, the clause must END - a
            # full stop or the end of the string. Anything else is room for a
            # particle, which is exactly what `" 은(는) ..." ` was.
            rest = message.split("${candidate.name}", 1)[1].lstrip("\"'")
            assert rest[:1] in {"", ".", ",", "!", "?"}, (
                "the filename is no longer followed immediately by the end of "
                f"its clause, so a particle can creep back in: {message!r}"
            )

    def test_korean_copy_does_not_use_programming_range_syntax(self):
        """``0..1000`` is Rust/Python range syntax; Korean writes ``0~1000``.
        English comments and type docs may keep ``..`` - this only looks at the
        lines that actually carry Korean."""
        offenders = [
            f"{path.relative_to(FRONTEND)}:{number}: {line.strip()}"
            for path, number, line in copy_lines()
            if HANGUL.search(line) and re.search(r"\d\.\.\d", line)
        ]
        assert offenders == [], f"Korean copy uses `..` for a range: {offenders}"

    def test_the_hero_paragraph_does_not_leak_the_internal_grid(self):
        """The normalized grid is a SlideSpec detail. A first-time visitor sees
        the hero before choosing a file and has no use for it."""
        app = source("App.tsx")
        header = app.split("<header", 1)[1].split("</header>", 1)[0]
        assert "1000" not in header, (
            "the hero copy names the coordinate grid again; it belongs in the "
            "Spec JSON tab's own explanation, not in the first paragraph"
        )
        assert "Vision" in header, "the hero paragraph moved - re-point this guard"


class TestKoreanTextDoesNotBreakMidWord:
    """Without ``word-break: keep-all`` the browser may break between any two
    Hangul syllables, so at 375px every wrapped paragraph splits words in half
    (measured: "이미지가" -> "이미지" / "가", "그대로" -> "그대" / "로"). It is the
    single most visible sign of un-localized Korean typography.
    """

    def test_the_body_rule_keeps_words_whole(self):
        css = source("index.css")
        body = css.split("body {", 1)[1].split("}", 1)[0]
        assert "word-break: keep-all" in body, (
            "Korean paragraphs will break mid-word again at mobile width"
        )
        assert "overflow-wrap: break-word" in body, (
            "keep-all alone lets a single over-long token overflow instead of "
            "breaking, so it needs break-word beside it"
        )

    def test_the_diagnostic_break_rules_are_still_there(self):
        """``keep-all`` must not be paid for by dropping the rules that stop a
        300-character junk token from widening a grid track. Those use
        ``[overflow-wrap:anywhere]``, which still shrinks min-content under
        ``keep-all`` (measured: 7px either way)."""
        for parts in (("components", "ResultView.tsx"), ("App.tsx",)):
            assert "[overflow-wrap:anywhere]" in source(*parts), (
                f"{parts[-1]} lost its containment rule"
            )


class TestTheSpecEditorStatusLineIsOneSentence:
    """``JSON 오류 {state.error}`` glued a fixed label onto two different
    things: the browser's English parse message (an English sentence in a Korean
    UI, with no separator) and - for an empty buffer - the Korean "편집기가 비어
    있습니다.", which is not a JSON error at all and read as a run-on.
    """

    def test_the_empty_buffer_is_not_reported_as_a_json_error(self):
        text = source("components", "SpecEditor.tsx")
        assert 'kind: "empty"' in text, (
            "parse() no longer distinguishes an empty editor from a syntax "
            "error, so one label has to cover both again"
        )
        assert 'state.kind === "empty"' in text, (
            "the status line no longer branches on the empty case"
        )
        assert not re.search(r"JSON 오류 \{?state\.error", text), (
            "the label is glued straight onto the detail again"
        )

    def test_the_engine_message_is_introduced_rather_than_substituted(self):
        text = source("components", "SpecEditor.tsx")
        assert "브라우저 메시지" in text, (
            "the browser's English parse message is shown with no Korean "
            "sentence framing it"
        )
        assert "{state.error}" in text, (
            "the engine message names the offending position - it is the one "
            "genuinely useful part and must still be shown"
        )


class TestMockModeIsGoneFromTheFrontendToo:
    """The product surface was removed on both sides. The backend guards catch
    a python resurrection; this catches a UI one."""

    def test_no_frontend_source_mentions_mock_mode(self):
        offenders = []
        for path in copy_files():
            text = path.read_text(encoding="utf-8")
            for needle in ("is_mock", "isMock", "mock_data", "Mock 모드"):
                if needle in text:
                    offenders.append(f"{path.relative_to(FRONTEND)}: {needle}")
        assert offenders == [], f"Mock mode is back in the UI: {offenders}"


# --------------------------------------------------------------------------- #
# Every code the backend can emit needs Korean copy in the client
# --------------------------------------------------------------------------- #
BACKEND = Path(__file__).resolve().parents[1] / "app"

#: ``ConversionFailure(<status>, "<code>", ...)`` - the routes' own failures.
_FAILURE_CODE = re.compile(r'ConversionFailure\(\s*\d+,\s*"([a-z_]+)"')


def backend_error_codes() -> set[str]:
    """Every ``code`` the API can put in the one error envelope.

    Two sources, because there are two ways to produce one: our routes raise
    ``ConversionFailure``, and ``app.main`` maps a status onto a code for the
    HTTPExceptions raised *outside* our routes - a wrong URL or method, an
    unreadable body. The second set is read live from the map rather than
    copied, so adding an entry there fails this test until the UI has copy.
    """
    from app.main import _CODE_BY_STATUS

    codes = set(_CODE_BY_STATUS.values())
    for path in BACKEND.rglob("*.py"):
        if "mock_data" in path.name:
            continue
        codes |= set(_FAILURE_CODE.findall(path.read_text(encoding="utf-8")))
    return codes


def error_copy_entries() -> dict[str, str]:
    """``ERROR_COPY``'s keys, mapped to their own source text."""
    text = source("api", "client.ts")
    head = "const ERROR_COPY: Record<string, ErrorCopy> = {"
    assert head in text, "ERROR_COPY changed shape - re-point this guard"
    block = text.split(head, 1)[1].split("\n};", 1)[0]
    entries = {
        match.group(1): match.group(2)
        for match in re.finditer(
            r"^  ([a-z_][a-z_0-9]*): \{\n(.*?)^  \},$", block, re.M | re.S
        )
    }
    assert len(entries) >= 12, f"only parsed {len(entries)} ERROR_COPY entries"
    return entries


class TestEveryBackendCodeHasKoreanCopy:
    """``not_found`` / ``method_not_allowed`` / ``bad_request`` were missing, so
    a stale ``VITE_API_BASE`` or dev-proxy target - the single most likely
    misconfiguration - rendered a banner reading nothing but "Not Found", with
    no Korean and no hint at all (those envelopes carry starlette's English
    string and an empty hint).
    """

    def test_the_inventory_found_the_codes_it_should(self):
        codes = backend_error_codes()
        for expected in (
            "invalid_spec",
            "missing_api_key",
            "unknown_download",
            "not_found",
            "method_not_allowed",
            "bad_request",
            "invalid_request",
        ):
            assert expected in codes, f"the code scan missed {expected}"

    def test_error_copy_covers_them_all(self):
        missing = sorted(backend_error_codes() - set(error_copy_entries()))
        assert missing == [], (
            "these codes reach the Korean UI with English text and no hint: "
            f"{missing}"
        )

    def test_no_copy_entry_is_written_for_a_code_that_cannot_happen(self):
        """Dead copy rots. Anything here must be reachable from the backend."""
        extra = sorted(set(error_copy_entries()) - backend_error_codes())
        assert extra == [], f"ERROR_COPY has copy for unreachable codes: {extra}"


class TestTheKoreanBannerDoesNotStackEnglishUnderneath:
    """Every mapped error appended the server's English ``hint`` AND its English
    ``message`` under the Korean copy, so the banner showed one to two English
    sentences in a Korean UI - and for ``invalid_spec`` the trailing one ("The
    request body is not a usable SlideSpec.") was a verbatim English restatement
    of the Korean headline directly above it.
    """

    def test_the_server_message_is_never_appended(self):
        text = source("api", "client.ts")
        body = text.split("export function localizeApiError", 1)[1].split("\n}", 1)[0]
        assert "detail.message" in body, "the function stopped reading the envelope"
        # It may be COMPARED against (to drop a duplicate hint), but it must not
        # be concatenated into the rendered hint.
        assert "serverMessage" not in body, (
            "the server's English message is being appended under the Korean "
            "copy again; copy.message has already replaced it"
        )
        assert re.search(r"extra\s*\?\s*`\$\{copy\.hint\}", body), (
            "the hint is no longer built as `our copy + optional server "
            "detail` - re-point this guard"
        )

    def test_only_the_codes_with_real_detail_keep_the_server_hint(self):
        """The server hint is kept exactly where it carries specifics the
        Korean sentence cannot know (which element was rejected, which field
        was missing, what the builder said). Everywhere else it is an English
        restatement of the line above it."""
        entries = error_copy_entries()
        keeps = {code for code, body in entries.items() if "keepServerDetail" in body}
        assert keeps == {
            "invalid_spec",
            "analysis_failed",
            "build_failed",
            "invalid_request",
            "bad_request",
        }, f"the set of codes that append server detail changed: {sorted(keeps)}"

    def test_every_entry_is_korean(self):
        for code, body in error_copy_entries().items():
            for field in ("message", "hint"):
                match = re.search(rf'{field}:\s*"([^"]*)"', body)
                assert match, f"{code} has no {field}"
                assert HANGUL.search(match.group(1)), (
                    f"ERROR_COPY.{code}.{field} is not Korean: {match.group(1)!r}"
                )
