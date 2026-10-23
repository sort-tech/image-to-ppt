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


class TestTheAssistOnlyClaimsWhatHappened:
    """The status line under the instructions box describes a round trip the
    user cannot see, so every word of it has to be true of what the server
    actually did. Three of them were not.
    """

    @staticmethod
    def _context_note() -> str:
        """Just the ``CONTEXT_NOTE`` literal - the sentences that reach a user.

        Scoped deliberately: the comments around it quote the old wording in
        order to explain why it was wrong, and a whole-file scan would forbid
        recording that.
        """
        panel = source("components", "OptionPanel.tsx")
        head = "const CONTEXT_NOTE: Record<RefineContext, string> = {"
        assert head in panel, "CONTEXT_NOTE changed shape - re-point this guard"
        block = panel.split(head, 1)[1].split("};", 1)[0]
        assert "spec:" in block and "image:" in block and "none:" in block, block
        return block

    def test_the_spec_line_names_the_slide_on_screen_not_the_last_conversion(self):
        """``currentSpec`` is ``edited.payload`` whenever an edited preview is
        showing, i.e. the user's own hand-edited Spec JSON - so "직전 변환의"
        (the PREVIOUS conversion's) analysis named the one thing the assist did
        not use."""
        context_note = self._context_note()
        assert "직전 변환" not in context_note, (
            "the spec context line claims the previous conversion's analysis "
            f"again, which is false as soon as the user edits the spec: "
            f"{context_note}"
        )
        assert "현재 화면의 슬라이드 분석 결과" in context_note

    def test_the_no_context_line_does_not_claim_the_note_was_only_tidied(self):
        """With no context the model still WRITES an instruction - it rewrote
        "표가 이상해" into a full imperative sentence - so "메모 문장만
        다듬었습니다" described something that did not happen. That wording is
        only true of the fallback, which now has its own line."""
        context_note = self._context_note()
        assert "문장만 다듬었습니다" not in context_note, context_note
        assert "없어" not in context_note, (
            "a context line blames a missing context again; the fallback case "
            "is reported separately because there the context was usually there"
        )

    def test_the_fallback_has_its_own_line_and_offers_no_undo(self):
        """Server-side, ``fell_back`` is the flag that tells a model that gave
        nothing back from a genuine no-context run - both answer
        ``context_used == "none"``. The UI has to read it, or it states a cause
        (no spec, no image) that did not happen and offers a 되돌리기 that
        restores identical text."""
        app_source = source("App.tsx")
        panel = source("components", "OptionPanel.tsx")
        client = source("api", "client.ts")

        assert "fell_back" in client, "the client drops the fallback flag"
        assert "fellBack" in app_source, "App.tsx ignores the fallback flag"
        assert "fellBack" in panel, "the panel does not describe a fallback"
        assert "모델이 문장을 완성하지 못해" in panel

        # The undo is offered only when the box really changed.
        body = app_source.split("const handleRefine", 1)[1].split(
            "const changeExtraInstructions", 1
        )[0]
        assert re.search(r"changed\s*=\s*data\.instruction !== rough", body), (
            "the assist no longer checks whether it changed anything, so a "
            "fallback that returned the note verbatim still offers an undo"
        )
        assert re.search(r"setPreRefineNote\(changed \?", body), body


class TestAnAssistInFlightIsCancelledNotIgnored:
    """A reply is only meaningful for the note AND the context it was sent
    with. Every path that changes either has to cancel the request, or the
    reply lands on top of what replaced it.

    Measured before the fix: 되돌리기 restored the user's sentence, the earlier
    run's reply landed 2 seconds later, and that sentence was gone from state -
    a second 되돌리기 then restored the machine text instead of it. Swapping the
    image left "선택한 이미지를 참고해 완성했습니다" beside a different image, and
    clearing it left the same line beside no image at all.
    """

    #: Every handler that changes the note or the assist's context.
    CANCELLING_HANDLERS = (
        ("const changeExtraInstructions", "typing in the box"),
        ("const handleUndoRefine", "되돌리기 - itself an edit of the box"),
        ("const chooseFile", "choosing or clearing the image"),
        ("const handleRefreshPreview", "refreshing the preview from edited JSON"),
        ("const handleRevert", "reverting to the converted spec"),
        ("const handleConvert", "converting, which replaces the spec"),
    )

    def test_every_path_that_changes_the_context_cancels_the_request(self):
        app_source = source("App.tsx")
        assert "const cancelRefine" in app_source, (
            "the shared cancel is gone - re-point this guard"
        )
        missing = []
        for marker, what in self.CANCELLING_HANDLERS:
            assert marker in app_source, f"{marker} is gone - re-point this guard"
            body = app_source.split(marker, 1)[1].split("\n  }, [", 1)[0]
            if "cancelRefine()" not in body and "refineAbort.current?.abort()" not in body:
                missing.append(what)
        assert missing == [], (
            "these paths change what the assist was asked about without "
            f"cancelling it, so a stale reply can still land: {missing}"
        )

    def test_a_reply_is_also_dropped_when_the_box_no_longer_holds_its_note(self):
        """The second half: an abort that is ever missed must not be the only
        thing standing between a stale reply and the user's text."""
        app_source = source("App.tsx")
        body = app_source.split("const handleRefine", 1)[1].split(
            "const changeExtraInstructions", 1
        )[0]
        assert "noteRef.current !== rough" in body, (
            "the landing path trusts the abort alone; a reply whose rough note "
            "is no longer in the box must be dropped"
        )
        landing = body.find("noteRef.current !== rough")
        assert landing < body.find("setExtraInstructions("), (
            "the guard runs after the textarea has already been overwritten"
        )


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
# The Vision model picker is hidden, and the configured model still shows
# --------------------------------------------------------------------------- #
class TestTheModelPickerStaysOffTheScreen:
    """The model is the SERVER's choice (``VISION_MODEL`` ->
    ``config.DEFAULT_MODEL``) and the UI no longer offers to change it.

    The backend's ``model`` form field is deliberately KEPT - API clients and
    the CLI's ``-m`` use it, and ``test_api.py`` asserts it stays in the schema
    - so nothing on the server side would fail if a control reappeared here.
    That makes these the only checks standing between a re-added input and a
    user who sets a model that silently disagrees with the one the status pill
    reports.
    """

    def test_no_model_control_is_rendered(self):
        """The removed control, by every name it had: the id that tied its
        label to its input, its two props, and its state setter.

        ``modelId`` rather than the visible "Vision 모델" label text: the label
        is on its own line in JSX, so no single literal spans it, and the id is
        what a re-added ``<label htmlFor=...>``/``<input id=...>`` pair needs in
        order to be an accessible field at all.
        """
        offenders = []
        for path in copy_files():
            text = path.read_text(encoding="utf-8")
            for needle in (
                "modelId",
                "onModelChange",
                "modelPlaceholder",
                "setModel(",
            ):
                if needle in text:
                    offenders.append(f"{path.relative_to(FRONTEND)}: {needle}")
        assert offenders == [], (
            f"a Vision model control is back on the screen: {offenders}"
        )

    def test_the_convert_call_sends_no_model_field(self):
        """The other half of it: a control is useless without a ``model`` part
        on the request, and a ``model`` part is what would override the
        configured value. Neither may exist."""
        client = source("api", "client.ts")
        assert 'append("model"' not in client, (
            "client.ts appends a `model` form field again, so the UI - not the "
            "server's VISION_MODEL - decides the model"
        )
        # `spec`, `note` and `file` are the parts the client legitimately sends.
        appended = set(re.findall(r'append\(\s*"([a-z_]+)"', client))
        assert "model" not in appended, sorted(appended)

    def test_the_convert_input_type_has_no_model_field(self):
        """A `model?: string` on ``ConvertInput`` would type-check at the call
        site and then be dropped on the floor by ``convert()``. The field is
        gone so the compiler catches that instead."""
        types = source("types", "slide.ts")
        match = re.search(
            r"export interface ConvertInput \{(.*?)\n\}", types, re.DOTALL
        )
        assert match, "ConvertInput is no longer declared - re-point this guard"
        assert "model" not in match.group(1), (
            "ConvertInput declares a `model` field again, but convert() does "
            "not send one - it would be a knob that silently does nothing"
        )

    def test_the_configured_model_is_still_shown(self):
        """Hiding the CONTROL must not hide the FACT. The header pill is the
        only place left that names the model, so it has to keep reading it from
        /api/health."""
        app_source = source("App.tsx")
        assert re.search(r"health\??\.proxy\.model", app_source), (
            "the header status pill no longer shows the configured model, so "
            "nothing on screen says which Vision model ran"
        )

    def test_a_blank_model_is_not_rendered_as_a_dangling_separator(self):
        """The pill's text was ```${health.proxy.model} · 키 설정됨```, so a
        blank model rendered " · 키 설정됨" - a separator with nothing in front
        of it, in the one place on screen that is supposed to name the model,
        and with no field left to type one into. ``VISION_MODEL=`` in a
        hand-edited ``.env`` produced exactly that (``config`` now falls back
        before it can, and this is the other half of the guard).
        """
        app_source = source("App.tsx")
        assert not re.search(r"\$\{health\??\.proxy\.model\}", app_source), (
            "the pill interpolates /api/health's model straight into its text "
            "again, so a blank value renders as a dangling separator"
        )
        fallback = re.search(r"const model(?:Text)? = [^;]*\|\|[^;]*;", app_source)
        assert fallback, "the blank-model fallback is gone - re-point this guard"
        assert HANGUL.search(fallback.group(0)), (
            "a blank model must fall back to Korean copy that says so, not to "
            "another blank: "
            f"{fallback.group(0)}"
        )


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
            # ``refine_failed`` belongs with ``analysis_failed``: its hint quotes
            # the (scrubbed) upstream text, which is the one thing a fixed
            # Korean sentence cannot know.
            "refine_failed",
        }, f"the set of codes that append server detail changed: {sorted(keeps)}"

    def test_analysis_failed_does_not_assert_that_the_model_replied(self):
        """``analysis_failed`` is also the code for "the configured model does
        not exist on the proxy" (README documents it, and the proxy answers
        ``Invalid model name passed in model=...``). In that case nothing
        replied at all, so "모델 응답을 슬라이드 정보로 읽을 수 없었습니다" stated a
        cause that did not happen - and both remedies it offered (a sharper
        image, extra instructions) cannot help. Typing another model name used
        to be the workaround; the UI has no field for that any more, so the copy
        has to name the one remedy that is left.
        """
        body = error_copy_entries()["analysis_failed"]

        assert "VISION_MODEL" in body, (
            "the only remaining way to change the model is .env's "
            "VISION_MODEL, and the copy never mentions it"
        )
        # It may offer the image advice - it is one of the real causes - but it
        # must not present "the model answered" as established fact.
        assert "모델 응답을" not in body, body
        assert "이미지를 분석하지 못했습니다" not in body, (
            "the message still says the model failed to ANALYZE the image, "
            "which is wrong when the request never reached a model: " + body
        )

    def test_every_entry_is_korean(self):
        for code, body in error_copy_entries().items():
            for field in ("message", "hint"):
                match = re.search(rf'{field}:\s*"([^"]*)"', body)
                assert match, f"{code} has no {field}"
                assert HANGUL.search(match.group(1)), (
                    f"ERROR_COPY.{code}.{field} is not Korean: {match.group(1)!r}"
                )


# --------------------------------------------------------------------------- #
# The preview must draw the route the deck draws, not an approximation of it
# --------------------------------------------------------------------------- #
class TestTheElbowPreviewMatchesTheDecksOwnRoute:
    """``bentConnector3`` routes in THREE legs: dx/2, the whole dy, then dx/2.

    So it both LEAVES and ARRIVES horizontally, and both of its arrowheads are
    horizontal whichever way the line runs. The preview drew ONE bend and chose
    the leg order from ``dx >= dy``, which left the first or the last segment
    vertical - and an SVG ``<marker>`` orients to its segment, so at least one
    arrowhead pointed 90 degrees away from what the .pptx shows. The route is
    fully determined by the two endpoints (the builder never overrides the
    preset's ``adj1``), so there is nothing to approximate.
    """

    def source(self) -> str:
        return source("components", "SlidePreview.tsx")

    def test_the_route_has_three_legs_bending_at_the_midpoint(self):
        text = self.source()
        assert "const mx = (x1 + x2) / 2;" in text, (
            "the bend is no longer taken at the midpoint, which is where the "
            "preset's default adj1 (50000) puts it"
        )
        assert "`M ${x1} ${y1} L ${mx} ${y1} L ${mx} ${y2} L ${x2} ${y2}`" in text, (
            "the elbow is not drawn as the deck's three-segment route, so an "
            "arrowhead can point the wrong way again"
        )

    def test_the_leg_order_is_no_longer_guessed_from_the_aspect(self):
        text = self.source()
        assert "dx >= dy" not in text, (
            "the two-segment branch is back: it makes marker-start or "
            "marker-end vertical, which the deck never draws"
        )
        assert "near-zero" not in text, (
            "the comment justifying the old branch claimed the preset's final "
            "horizontal leg is near-zero; it is always dx/2"
        )

    def test_the_caption_no_longer_calls_the_route_an_approximation(self):
        text = self.source()
        assert "두 구간으로 근사해" not in text, (
            "the caption still describes a two-segment approximation"
        )
        assert "세 구간" in text, "the caption should name the real route"


# --------------------------------------------------------------------------- #
# A dashed outline in the deck has to be dashed in the preview
# --------------------------------------------------------------------------- #
class TestThePlaceholderOutlineIsPreviewedDashed:
    """The builder's skipped-image marker is a DASHED grey rectangle, chosen so
    the gap does not read as slide content. The autoshape node drew every rect
    solid, so the one cue that tells the marker from a box the analyzer asked
    for was lost on the way to the screen.
    """

    def test_the_autoshape_node_passes_a_dash_array(self):
        text = source("components", "SlidePreview.tsx")
        shape_node = text.split("function AutoShapeNode", 1)[1].split(
            "function ConnectorNode", 1
        )[0]
        assert 'shape.line_dash === "dashed"' in shape_node, (
            "the autoshape branch ignores the payload's dash again"
        )
        assert shape_node.count("strokeDasharray={dashArray}") == 3, (
            "the ellipse, the diamond and the rect all have to carry the dash"
        )

    def test_the_payload_type_carries_the_field(self):
        assert "line_dash: DashStyle;" in source("types", "slide.ts"), (
            "PreviewShape lost line_dash, so the renderer cannot see it"
        )


# --------------------------------------------------------------------------- #
# The result panel may not describe elements the file does not contain
# --------------------------------------------------------------------------- #
class TestTheResultPanelDescribesTheFileAndNotTheRequest:
    """``counts`` is derived from the validated spec - what was REQUESTED.

    An ``image`` element is the one type that does not always reach the deck
    (no source image to crop from, or the anti-flattening rule refused the
    crop), so the badges were reporting "1 이미지" for a file holding none, and
    the warnings panel's fixed copy told the user the element WAS on the slide
    while the warning under it said "was NOT rendered".
    """

    def result_view(self) -> str:
        return source("components", "ResultView.tsx")

    def test_the_badges_read_the_deck_derived_tally(self):
        text = self.result_view()
        assert "renderedCounts(preview)" in text, (
            "the panel is back to reporting the requested counts as the file"
        )
        assert "{shown[key] ?? 0}" in text, "a badge still reads counts[key]"
        assert "counts[key] ?? 0" not in text

    def test_a_requested_element_missing_from_the_deck_is_named(self):
        text = self.result_view()
        assert "skippedByType" in text, (
            "nothing compares what was asked for with what the deck holds, so "
            "a skipped image is silently just a smaller number"
        )
        assert "파일에 들어가지 못한" in text

    def test_the_numbers_say_whose_they_are_when_there_is_no_preview(self):
        text = self.result_view()
        assert "rendered === null" in text
        assert "요청한 사양을 기준으로" in text, (
            "with no preview the panel shows the spec's numbers, and it has to "
            "say so rather than presenting them as the file"
        )

    def test_the_warning_panel_no_longer_promises_the_element_is_on_the_slide(self):
        text = self.result_view()
        assert "슬라이드에 들어 있지만" not in text, (
            "the panel again claims every warned element is in the slide; an "
            "image element with no source image is not"
        )
        assert "건너뛰어졌습니다" in text, (
            "the copy has to cover the skip case as well as the adjust case"
        )

    def test_the_download_announcement_says_whose_numbers_it_reads(self):
        app = source("App.tsx")
        assert "describeDeck(deck, renderedCounts(edited.preview))" in app, (
            "the download note is back to announcing the spec's counts as the "
            "contents of the file"
        )
        assert "사양 기준" in app, (
            "when only the spec's counts are available the line has to say so"
        )
