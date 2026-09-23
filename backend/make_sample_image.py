"""Generate a synthetic 16:9 "slide" PNG with Pillow, for end-to-end demos.

Why this exists: to try the converter you need an input image, and shipping a
binary screenshot in the repo is both heavy and legally awkward.  This module
draws one from scratch instead.

Only Pillow's *built-in* font is used - no system font files are read - so the
script works on a bare CI container.  ``ImageFont.load_default(size=...)`` was
added in Pillow 10.1; older versions only accept a no-argument call and return
a small bitmap font.  :func:`_font` handles both.

Run it directly to write ``./sample.png``::

    python make_sample_image.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

__all__ = ["make_sample_image"]

# ---------------------------------------------------------------------------
# palette (kept in sync with mock_data.py so the demo looks coherent)
# ---------------------------------------------------------------------------
WHITE = "#FFFFFF"
NAVY = "#1F3B73"
DEEP_NAVY = "#10233F"
AMBER = "#FFB000"
CARD_BG = "#F2F5FA"
GRID = "#9AA9C2"
MUTED = "#5A6B85"
HEADER_TEXT = "#DCE3F0"

_TABLE_DATA: list[list[str]] = [
    ["Region", "Users", "Growth"],
    ["EMEA", "18,400", "+12%"],
    ["APAC", "9,750", "+27%"],
]

_BULLETS: list[str] = [
    "p95 latency down 38%",
    "Two new regions live",
    "Churn flat at 1.2%",
]

_NEXT_STEPS: list[str] = [
    "Ship the EMEA migration",
    "Close the APAC capacity gap",
    "Re-run the pricing experiment",
]


def _font(size: int) -> ImageFont.ImageFont:
    """Return the default PIL font at ``size``, tolerating old Pillow.

    Pillow >= 10.1 scales its bundled TrueType face when ``size`` is passed.
    Anything older raises ``TypeError``, and we fall back to the fixed-size
    bitmap font (legible, just smaller).
    """
    size = max(1, int(size))
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _text_box(
    draw: ImageDraw.ImageDraw, text: str, font
) -> tuple[float, float, float, float]:
    """Ink box of ``text`` when drawn at (0, 0): (left, top, right, bottom).

    ``top`` is usually > 0 (the ascender gap), so subtracting it is what makes
    manual centering look right.  ``anchor="mm"`` is deliberately not used: it
    only works for TrueType fonts, and old Pillow's default font is a bitmap.
    """
    try:
        return draw.textbbox((0, 0), text, font=font)
    except AttributeError:  # pragma: no cover - very old Pillow
        w, h = draw.textsize(text, font=font)
        return 0.0, 0.0, float(w), float(h)


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    """Width/height of ``text`` in pixels."""
    left, top, right, bottom = _text_box(draw, text, font)
    return int(right - left), int(bottom - top)


def _centered(
    draw: ImageDraw.ImageDraw,
    text: str,
    box: tuple[float, float, float, float],
    font,
    fill: str,
) -> None:
    """Draw ``text`` optically centered inside ``box`` = (x0, y0, x1, y1)."""
    x0, y0, x1, y1 = box
    left, top, right, bottom = _text_box(draw, text, font)
    x = x0 + (x1 - x0 - (right - left)) / 2 - left
    y = y0 + (y1 - y0 - (bottom - top)) / 2 - top
    draw.text((x, y), text, font=font, fill=fill)


def _right_aligned(
    draw: ImageDraw.ImageDraw, text: str, right: float, top: float, font, fill: str
) -> None:
    tw, _ = _text_size(draw, text, font)
    draw.text((right - tw, top), text, font=font, fill=fill)


def _bullet_block(
    draw: ImageDraw.ImageDraw,
    items: list[str],
    x: float,
    y: float,
    font,
    line_step: float,
    dot_r: float,
    fill: str,
) -> float:
    """Draw a bullet list; returns the y just below the last line.

    The marker is a drawn dot rather than a "*" glyph, so the output does not
    depend on the bundled font containing that codepoint.
    """
    for item in items:
        _, top, _, bottom = _text_box(draw, item, font)
        cy = y + (top + bottom) / 2
        draw.ellipse((x, cy - dot_r, x + 2 * dot_r, cy + dot_r), fill=fill)
        draw.text((x + 3.2 * dot_r, y), item, font=font, fill=fill)
        y += line_step
    return y


def make_sample_image(
    output_path: str | Path = "sample.png", size: tuple[int, int] = (1600, 900)
) -> Path:
    """Draw a legible synthetic 16:9 slide and save it as PNG.

    Parameters
    ----------
    output_path:
        Where to write the image.  Parent directories are created.
    size:
        ``(width, height)`` in pixels; every coordinate below is expressed as a
        fraction of these, so non-default sizes still render correctly.

    Returns
    -------
    Path
        The resolved path of the written file.
    """
    width, height = int(size[0]), int(size[1])
    if width < 1 or height < 1:
        raise ValueError(f"size must be positive, got {size!r}")

    # Font sizes are tuned for 1600x900 and scaled from there.
    k = min(width / 1600, height / 900)
    f_title = _font(round(46 * k))
    f_tag = _font(round(20 * k))
    f_h2 = _font(round(28 * k))
    f_body = _font(round(22 * k))
    f_cell = _font(round(22 * k))
    f_small = _font(round(17 * k))

    img = Image.new("RGB", (width, height), WHITE)
    draw = ImageDraw.Draw(img)

    # ---- header band + title -------------------------------------------
    band_h = 0.155 * height
    draw.rectangle((0, 0, width, band_h), fill=NAVY)
    draw.text((0.028 * width, 0.042 * height), "Quarterly Platform Review", font=f_title, fill=WHITE)
    _right_aligned(draw, "FY24 - Confidential", 0.845 * width, 0.062 * height, f_tag, HEADER_TEXT)

    # ---- circle badge in the band --------------------------------------
    c_d = 0.09 * height
    c_x = 0.885 * width
    c_y = 0.033 * height
    draw.ellipse((c_x, c_y, c_x + c_d, c_y + c_d), fill=AMBER, outline=WHITE, width=max(1, round(3 * k)))
    _centered(draw, "Q4", (c_x, c_y, c_x + c_d, c_y + c_d), f_tag, DEEP_NAVY)

    # ---- bordered rounded card (left) ----------------------------------
    card = (0.035 * width, 0.205 * height, 0.455 * width, 0.53 * height)
    draw.rounded_rectangle(
        card,
        radius=max(4, round(22 * k)),
        fill=CARD_BG,
        outline=NAVY,
        width=max(1, round(3 * k)),
    )
    draw.text((card[0] + 0.022 * width, card[1] + 0.028 * height), "Highlights", font=f_h2, fill=NAVY)
    _bullet_block(
        draw,
        _BULLETS,
        x=card[0] + 0.024 * width,
        y=card[1] + 0.095 * height,
        font=f_body,
        line_step=0.062 * height,
        dot_r=max(2.0, 5 * k),
        fill=DEEP_NAVY,
    )

    # ---- ruled 3x3 table (right) ---------------------------------------
    tx0, ty0 = 0.505 * width, 0.205 * height
    tx1, ty1 = 0.965 * width, 0.53 * height
    rows, cols = len(_TABLE_DATA), max(len(r) for r in _TABLE_DATA)
    col_w = (tx1 - tx0) / cols
    row_h = (ty1 - ty0) / rows
    lw = max(1, round(2 * k))

    # shaded header row, drawn before the rules so the lines stay crisp
    draw.rectangle((tx0, ty0, tx1, ty0 + row_h), fill=NAVY)
    draw.rectangle((tx0, ty0, tx1, ty1), outline=GRID, width=lw)
    for r in range(1, rows):
        y = ty0 + r * row_h
        draw.line((tx0, y, tx1, y), fill=GRID, width=lw)
    for c in range(1, cols):
        x = tx0 + c * col_w
        draw.line((x, ty0, x, ty1), fill=GRID, width=lw)

    for r, row in enumerate(_TABLE_DATA):
        for c, cell in enumerate(row):
            box = (tx0 + c * col_w, ty0 + r * row_h, tx0 + (c + 1) * col_w, ty0 + (r + 1) * row_h)
            _centered(draw, cell, box, f_cell, WHITE if r == 0 else DEEP_NAVY)

    # ---- second bullet list (bottom left) ------------------------------
    draw.text((0.035 * width, 0.585 * height), "Next steps", font=f_h2, fill=NAVY)
    _bullet_block(
        draw,
        _NEXT_STEPS,
        x=0.038 * width,
        y=0.655 * height,
        font=f_body,
        line_step=0.062 * height,
        dot_r=max(2.0, 5 * k),
        fill=DEEP_NAVY,
    )

    # ---- accent rule + footer ------------------------------------------
    draw.rectangle(
        (0.505 * width, 0.60 * height, 0.965 * width, 0.60 * height + max(2, 5 * k)), fill=AMBER
    )
    draw.text((0.505 * width, 0.635 * height), "Growth is concentrated in APAC.", font=f_body, fill=MUTED)
    _centered(
        draw,
        "Source: internal telemetry, 2024-Q4",
        (0.0, 0.93 * height, float(width), float(height)),
        f_small,
        MUTED,
    )

    out = Path(output_path).expanduser()
    if out.parent and not out.parent.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, format="PNG")
    return out.resolve()


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    path = make_sample_image()
    print(f"wrote {path}")
