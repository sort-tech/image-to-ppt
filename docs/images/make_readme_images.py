# Regenerates docs/images/before-after.png and docs/images/editable-objects.gif.
#
# macOS only (Quick Look renders the deck; Helvetica Neue draws the captions):
#
#   cd backend && python cli.py -i sample.png -o /tmp/s.pptx --save-json /tmp/s.json
#   qlmanage -t -s 1600 -o /tmp /tmp/s.pptx            # -> /tmp/s.pptx.png
#   python ../docs/images/make_readme_images.py sample.png /tmp/s.pptx.png /tmp/s.json ../docs/images
#
# The images in the repository come from a real conversion of backend/sample.png
# (gemini-flash, 2026-10-24). Quick Look draws the deck in a serif face; in
# PowerPoint the same file renders in the theme font.
"""README visuals from a REAL conversion: input screenshot -> rendered .pptx.

Highlight boxes come from the converter's own spec (0..1000 coordinates), so
each one marks an object that really is a separate native object in the deck.
"""
import json
import sys
from PIL import Image, ImageDraw, ImageFont

src, render, spec_path, out_dir = sys.argv[1:5]
F = "/System/Library/Fonts/HelveticaNeue.ttc"
def font(n, bold=False):
    return ImageFont.truetype(F, n, index=1 if bold else 0)

BG = (15, 23, 42)          # slate-900
INK = (226, 232, 240)      # slate-200
MUTED = (148, 163, 184)    # slate-400
ACCENT = (56, 189, 248)    # sky-400

inp = Image.open(src).convert("RGB")
outp = Image.open(render).convert("RGB").resize(inp.size)
spec = json.load(open(spec_path))

# ---------------------------------------------------------------- before/after
PW, PH = 740, 416
canvas = Image.new("RGB", (1600, 590), BG)
d = ImageDraw.Draw(canvas)
for i, (img, title, sub) in enumerate([
    (inp, "Input", "a screenshot (PNG)"),
    (outp, "Output", "a native PowerPoint slide (.pptx)"),
]):
    x = 30 + i * (PW + 60)
    d.text((x, 28), title, font=font(34, True), fill=INK)
    d.text((x + d.textlength(title, font=font(34, True)) + 14, 40), sub, font=font(22), fill=MUTED)
    canvas.paste(img.resize((PW, PH)), (x, 92))
    d.rectangle([x - 1, 91, x + PW, 92 + PH], outline=(51, 65, 85), width=2)
ax = 30 + PW + 16
d.polygon([(ax, 284), (ax + 28, 300), (ax, 316)], fill=ACCENT)
d.text((30, 540), "Every box, table, line and text box on the right can be clicked and edited in PowerPoint.",
       font=font(20), fill=MUTED)
canvas.save(f"{out_dir}/before-after.png", optimize=True)

# ---------------------------------------------------------------- highlight GIF
GW, GH = 960, 540
sx, sy = GW / 1000, GH / 1000
base = outp.resize((GW, GH))

def frame(img, caption, boxes=()):
    f = Image.new("RGB", (GW, GH + 64), BG)
    f.paste(img, (0, 0))
    dr = ImageDraw.Draw(f)
    for b in boxes:
        x0, y0 = b["x"] * sx - 5, b["y"] * sy - 5
        x1, y1 = b["x"] * sx + max(b["w"] * sx, 6) + 5, b["y"] * sy + max(b["h"] * sy, 6) + 5
        for k in range(int(x0), int(x1), 12):                       # dashed edges
            dr.line([k, y0, min(k + 6, x1), y0], fill=ACCENT, width=2)
            dr.line([k, y1, min(k + 6, x1), y1], fill=ACCENT, width=2)
        for k in range(int(y0), int(y1), 12):
            dr.line([x0, k, x0, min(k + 6, y1)], fill=ACCENT, width=2)
            dr.line([x1, k, x1, min(k + 6, y1)], fill=ACCENT, width=2)
        for hx, hy in [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]:
            dr.rectangle([hx - 4, hy - 4, hx + 4, hy + 4], fill="white", outline=ACCENT, width=2)
    dr.text((24, GH + 16), caption, font=font(26, True), fill=INK)
    return f

els = spec["elements"]
of = lambda t: [e["bbox"] for e in els if e["type"] == t and e.get("bbox")]
frames = [
    (frame(inp.resize((GW, GH)), "1. Start from a screenshot of a slide"), 1800),
    (frame(base, "2. Get a real PowerPoint slide back"), 1800),
    (frame(base, "Tables stay tables - retype any cell", of("table")), 1600),
    (frame(base, "Boxes, cards and badges are shapes", of("shape")), 1600),
    (frame(base, "Text is text boxes - edit the wording", of("text")), 1600),
]
imgs = [f.convert("P", palette=Image.ADAPTIVE, colors=128) for f, _ in frames]
imgs[0].save(f"{out_dir}/editable-objects.gif", save_all=True, append_images=imgs[1:],
             duration=[ms for _, ms in frames], loop=0, optimize=True)
print("ok")
