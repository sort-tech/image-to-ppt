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
