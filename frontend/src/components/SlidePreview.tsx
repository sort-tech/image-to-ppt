import type { CSSProperties } from "react";
import { useEffect, useId, useMemo, useState } from "react";
import { EyeOff, Maximize2, MonitorPlay, X } from "lucide-react";

import type {
  PreviewShape,
  PreviewShapeItem,
  PreviewTable,
  PreviewText,
  SlidePreview as SlidePreviewPayload,
} from "../types/slide";

interface SlidePreviewProps {
  /** The payload from `/api/convert` or `/api/preview`; null when it failed. */
  preview: SlidePreviewPayload | null;
  /** Shown under the caption, e.g. "편집한 Spec JSON을 반영했습니다". */
  note?: string | null;
}

/* -------------------------------------------------------------------------- */
/* Fidelity constants                                                         */
/* -------------------------------------------------------------------------- */

/** Fallback slide size in points (13.333in x 7.5in), if the payload omits it. */
const FALLBACK_W_PT = 960;
const FALLBACK_H_PT = 540;

/**
 * PowerPoint's default `roundRect` corner adjustment: 16.667% of the shorter
 * side. The builder never overrides the preset's adjustment, so this is what
 * the real shape looks like.
 */
const ROUNDED_ADJUSTMENT = 0.16667;

/**
 * Table cell padding, mirroring `pptx_builder._CELL_MARGIN_H/_CELL_MARGIN_V`
 * (0.04in / 0.01in) converted to points, since 1 user unit == 1pt here.
 */
const CELL_PAD_H_PT = 0.04 * 72;
const CELL_PAD_V_PT = 0.01 * 72;

/**
 * PowerPoint's single line spacing is ~1.2x the font size, and the builder
 * never sets an explicit `line_spacing`.
 */
const LINE_HEIGHT = 1.2;

/**
 * The deck carries no explicit typeface, so PowerPoint renders it in the
 * theme's Calibri. Naming Calibri first keeps Latin text close to the real
 * output; the Korean faces behind it are what actually renders Hangul, which
 * Calibri has no glyphs for.
 */
const PREVIEW_FONT =
  'Calibri, "Apple SD Gothic Neo", "Malgun Gothic", "맑은 고딕", "Noto Sans KR", sans-serif';

/** Text with no colour is black in PowerPoint, not "inherit". */
const DEFAULT_INK = "#000000";

/**
 * The builder sets "No Style, Table Grid", which draws 1pt dark rules on every
 * cell edge - so the preview draws them too rather than showing a borderless
 * grid the deck does not have.
 */
const TABLE_RULE = "1px solid #000000";

/* -------------------------------------------------------------------------- */
/* Number plumbing                                                            */
/* -------------------------------------------------------------------------- */

/** Every geometry field is a JSON number, but a bad one must not blank the SVG. */
function num(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

/** A percentage of the slide, in SVG user units (= points). */
function fromPct(pct: unknown, span: number): number {
  return (num(pct) / 100) * span;
}

/** SVG renders nothing for a negative width/height, so floor sizes at 0. */
function size(pct: unknown, span: number): number {
  return Math.max(0, fromPct(pct, span));
}

/* -------------------------------------------------------------------------- */
/* Shape nodes                                                               */
/* -------------------------------------------------------------------------- */

function AutoShapeNode({
  shape,
  slideW,
  slideH,
}: {
  shape: PreviewShape;
  slideW: number;
  slideH: number;
}) {
  const x = fromPct(shape.left_pct, slideW);
  const y = fromPct(shape.top_pct, slideH);
  const w = size(shape.width_pct, slideW);
  const h = size(shape.height_pct, slideH);

  // null is "no fill / no outline", which is see-through - never black.
  const fill = shape.fill_color ?? "none";
  const hasLine = Boolean(shape.line_color);
  const stroke = shape.line_color ?? "none";
  const strokeWidth = hasLine ? Math.max(0, num(shape.line_width_pt, 1)) : 0;

  if (shape.autoshape === "oval") {
    return (
      <ellipse
        cx={x + w / 2}
        cy={y + h / 2}
        rx={w / 2}
        ry={h / 2}
        fill={fill}
        stroke={stroke}
        strokeWidth={strokeWidth}
      />
    );
  }

  const radius =
    shape.autoshape === "rounded_rectangle" ? Math.min(w, h) * ROUNDED_ADJUSTMENT : 0;

  return (
    <rect
      x={x}
      y={y}
      width={w}
      height={h}
      {...(radius > 0 ? { rx: radius, ry: radius } : {})}
      fill={fill}
      stroke={stroke}
      strokeWidth={strokeWidth}
    />
  );
}

/**
 * A text box, as HTML inside `<foreignObject>`.
 *
 * HTML is the point: `word_wrap` is on in the deck, so the preview needs real
 * word wrapping, which SVG `<text>` does not do. Inside a `viewBox` measured
 * in points, 1 CSS pixel is 1 point, so `font_size_pt` goes straight into
 * `font-size` with no conversion.
 *
 * The builder zeroes all four text-frame margins and anchors the frame to the
 * top, so there is no padding here and the column starts at the top edge.
 */
function TextNode({
  shape,
  slideW,
  slideH,
}: {
  shape: PreviewText;
  slideW: number;
  slideH: number;
}) {
  const x = fromPct(shape.left_pct, slideW);
  const y = fromPct(shape.top_pct, slideH);
  const w = size(shape.width_pct, slideW);
  const h = size(shape.height_pct, slideH);
  const paragraphs = Array.isArray(shape.paragraphs) ? shape.paragraphs : [];

  return (
    <foreignObject
      x={x}
      y={y}
      width={w}
      height={h}
      // PowerPoint lets text spill out of an undersized box rather than
      // truncating it, so the box does not clip - the slide does (see the
      // <clipPath> the whole layer sits in).
      style={{ overflow: "visible" }}
    >
      <div
        style={{
          width: "100%",
          height: "100%",
          fontFamily: PREVIEW_FONT,
          lineHeight: LINE_HEIGHT,
        }}
      >
        {paragraphs.map((paragraph, index) => (
          <div
            key={index}
            style={{
              fontSize: `${Math.max(1, num(paragraph.font_size_pt, 12))}px`,
              fontWeight: paragraph.bold ? 700 : 400,
              color: paragraph.font_color ?? DEFAULT_INK,
              textAlign: paragraph.align ?? shape.align ?? "left",
              whiteSpace: "pre-wrap",
              overflowWrap: "anywhere",
            }}
          >
            {/* An empty paragraph still occupies a line in the deck. */}
            {paragraph.text === "" ? " " : paragraph.text}
          </div>
        ))}
      </div>
    </foreignObject>
  );
}

/**
 * A table, as a real HTML `<table>` inside `<foreignObject>`.
 *
 * `table-layout: fixed` plus a `<colgroup>` is what makes the server's
 * `col_widths_pct` authoritative instead of letting the browser re-measure
 * columns from their content - the deck's columns do not do that.
 */
function TableNode({
  shape,
  slideW,
  slideH,
}: {
  shape: PreviewTable;
  slideW: number;
  slideH: number;
}) {
  const x = fromPct(shape.left_pct, slideW);
  const y = fromPct(shape.top_pct, slideH);
  const w = size(shape.width_pct, slideW);
  const h = size(shape.height_pct, slideH);

  const rows = Array.isArray(shape.rows) ? shape.rows : [];
  const colWidths = Array.isArray(shape.col_widths_pct) ? shape.col_widths_pct : [];
  const rowHeights = Array.isArray(shape.row_heights_pct) ? shape.row_heights_pct : [];
  const colCount = Math.max(colWidths.length, ...rows.map((row) => row.length), 1);

  return (
    <foreignObject x={x} y={y} width={w} height={h} style={{ overflow: "visible" }}>
      <table
        style={{
          width: "100%",
          height: "100%",
          tableLayout: "fixed",
          borderCollapse: "collapse",
          fontFamily: PREVIEW_FONT,
          lineHeight: LINE_HEIGHT,
        }}
      >
        <colgroup>
          {Array.from({ length: colCount }, (_, index) => (
            <col
              key={index}
              style={{ width: `${num(colWidths[index], 100 / colCount)}%` }}
            />
          ))}
        </colgroup>
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr
              key={rowIndex}
              style={{ height: `${num(rowHeights[rowIndex], 100 / (rows.length || 1))}%` }}
            >
              {Array.from({ length: colCount }, (_, colIndex) => {
                const cell = row[colIndex];
                return (
                  <td
                    key={colIndex}
                    style={{
                      border: TABLE_RULE,
                      // A cell with no fill shows the slide through it.
                      backgroundColor: cell?.fill_color ?? "transparent",
                      color: cell?.font_color ?? DEFAULT_INK,
                      fontSize: `${Math.max(1, num(cell?.font_size_pt, 10))}px`,
                      fontWeight: cell?.bold ? 700 : 400,
                      textAlign: cell?.align ?? "left",
                      // The builder anchors every cell to the middle.
                      verticalAlign: "middle",
                      padding: `${CELL_PAD_V_PT}px ${CELL_PAD_H_PT}px`,
                      overflow: "hidden",
                      overflowWrap: "anywhere",
                    }}
                  >
                    {cell?.text ?? ""}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </foreignObject>
  );
}

/** A shape the backend could not describe: an outline, so it is not invisible. */
function UnsupportedNode({
  shape,
  slideW,
  slideH,
}: {
  shape: PreviewShapeItem;
  slideW: number;
  slideH: number;
}) {
  return (
    <rect
      x={fromPct(shape.left_pct, slideW)}
      y={fromPct(shape.top_pct, slideH)}
      width={size(shape.width_pct, slideW)}
      height={size(shape.height_pct, slideH)}
      fill="none"
      stroke="#94A3B8"
      strokeWidth={1}
      strokeDasharray="4 3"
    />
  );
}

/* -------------------------------------------------------------------------- */
/* The slide itself                                                           */
/* -------------------------------------------------------------------------- */

function SlideSvg({
  preview,
  className,
  style,
}: {
  preview: SlidePreviewPayload;
  className?: string;
  style?: CSSProperties;
}) {
  // useId can contain ":" - legal in an id, but it keeps url(#...) readable to
  // strip it, and some tooling chokes on it.
  const clipId = `slide-clip-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;

  const slideW = Math.max(1, num(preview.slide?.width_pt, FALLBACK_W_PT));
  const slideH = Math.max(1, num(preview.slide?.height_pt, FALLBACK_H_PT));
  const shapes = Array.isArray(preview.shapes) ? preview.shapes : [];

  const label = useMemo(() => {
    const tally = { shape: 0, text: 0, table: 0, other: 0 };
    for (const item of shapes) {
      if (item.kind === "shape") tally.shape += 1;
      else if (item.kind === "text") tally.text += 1;
      else if (item.kind === "table") tally.table += 1;
      else tally.other += 1;
    }
    return `슬라이드 미리보기 - 도형 ${tally.shape}개, 표 ${tally.table}개, 텍스트 상자 ${tally.text}개`;
  }, [shapes]);

  return (
    <svg
      viewBox={`0 0 ${slideW} ${slideH}`}
      preserveAspectRatio="xMidYMid meet"
      role="img"
      aria-label={label}
      className={className}
      style={style}
    >
      <defs>
        <clipPath id={clipId}>
          <rect x={0} y={0} width={slideW} height={slideH} />
        </clipPath>
      </defs>

      {/* No background colour means a white slide, which is what PowerPoint shows. */}
      <rect
        x={0}
        y={0}
        width={slideW}
        height={slideH}
        fill={preview.background_color ?? "#FFFFFF"}
      />

      {/* Everything is clipped to the slide, so an element the builder placed
          partly off-slide is cut at the edge here exactly as PowerPoint cuts
          it - it cannot escape into the surrounding UI. */}
      <g clipPath={`url(#${clipId})`}>
        {shapes.map((shape, index) => {
          // Already in z-order, back to front: paint order is document order.
          switch (shape.kind) {
            case "shape":
              return (
                <AutoShapeNode key={index} shape={shape} slideW={slideW} slideH={slideH} />
              );
            case "text":
              return <TextNode key={index} shape={shape} slideW={slideW} slideH={slideH} />;
            case "table":
              return <TableNode key={index} shape={shape} slideW={slideW} slideH={slideH} />;
            default:
              return (
                <UnsupportedNode key={index} shape={shape} slideW={slideW} slideH={slideH} />
              );
          }
        })}
      </g>
    </svg>
  );
}

/* -------------------------------------------------------------------------- */
/* Public component                                                           */
/* -------------------------------------------------------------------------- */

/**
 * The rendered slide, shown BEFORE the download.
 *
 * The payload is derived on the server by walking the built `Presentation`'s
 * shapes, so what this draws is what is genuinely in the .pptx - including the
 * min-dimension floor, off-slide clipping, cleaned text and z-order. It is
 * still an approximation of PowerPoint's own text layout (font metrics and
 * line breaking are the renderer's), which the caption says out loud.
 *
 * Every string is rendered as a React text node, so slide text can never be
 * interpreted as markup.
 */
export default function SlidePreview({ preview, note = null }: SlidePreviewProps) {
  const [zoomed, setZoomed] = useState(false);

  useEffect(() => {
    if (!zoomed) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setZoomed(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [zoomed]);

  // A refresh that came back without a preview must not leave the overlay up.
  useEffect(() => {
    if (!preview) setZoomed(false);
  }, [preview]);

  if (!preview) {
    return (
      <div className="rounded-2xl border border-slate-800 bg-slate-900/50 p-5">
        <div className="mb-3 flex items-center gap-2">
          <MonitorPlay aria-hidden="true" className="h-4 w-4 text-sky-400" />
          <h3 className="text-sm font-semibold text-slate-100">슬라이드 미리보기</h3>
        </div>
        <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-slate-700 bg-slate-950/40 px-6 py-10 text-center">
          <EyeOff aria-hidden="true" className="h-6 w-6 text-slate-600" />
          <p className="text-sm font-medium text-slate-300">미리보기를 만들지 못했습니다</p>
          <p className="max-w-sm text-xs leading-relaxed text-slate-500">
            PPTX 파일 자체는 정상적으로 만들어졌으니 그대로 내려받아 PowerPoint에서 확인할 수
            있습니다. 자세한 사유는 아래 경고 목록에 기록되어 있습니다.
          </p>
        </div>
      </div>
    );
  }

  const aspect = `${Math.max(1, num(preview.slide?.width_pt, FALLBACK_W_PT))} / ${Math.max(
    1,
    num(preview.slide?.height_pt, FALLBACK_H_PT),
  )}`;

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/50 p-5">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <MonitorPlay aria-hidden="true" className="h-4 w-4 text-sky-400" />
          <h3 className="text-sm font-semibold text-slate-100">슬라이드 미리보기</h3>
        </div>
        <button
          type="button"
          onClick={() => setZoomed(true)}
          className="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 px-2.5 py-1.5 text-xs font-medium text-slate-200 transition hover:border-sky-500 hover:text-sky-300"
        >
          <Maximize2 aria-hidden="true" className="h-3.5 w-3.5" />
          크게 보기
        </button>
      </div>

      <div
        className="w-full overflow-hidden rounded-xl border border-slate-700 bg-white shadow-inner"
        style={{ aspectRatio: aspect }}
      >
        <SlideSvg preview={preview} className="h-full w-full" />
      </div>

      <p className="mt-3 text-xs leading-relaxed text-slate-500">
        실제로 만들어진 PPTX의 도형·표·텍스트 상자를 그대로 읽어 그린 미리보기입니다. 다만 글꼴
        치수와 줄바꿈은 브라우저가 계산하므로 PowerPoint에서 열었을 때와 조금 다를 수 있습니다.
      </p>
      {note ? <p className="mt-1 text-xs text-amber-300">{note}</p> : null}

      {zoomed ? (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="슬라이드 미리보기 크게 보기"
          onClick={() => setZoomed(false)}
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/90 p-4 backdrop-blur-sm sm:p-8"
        >
          <div
            onClick={(event) => event.stopPropagation()}
            className="w-full max-w-6xl"
          >
            <div className="mb-3 flex items-center justify-between gap-3">
              <p className="text-sm font-medium text-slate-200">슬라이드 미리보기</p>
              <button
                type="button"
                onClick={() => setZoomed(false)}
                autoFocus
                className="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 bg-slate-900 px-2.5 py-1.5 text-xs font-medium text-slate-200 transition hover:border-sky-500 hover:text-sky-300"
              >
                <X aria-hidden="true" className="h-3.5 w-3.5" />
                닫기
              </button>
            </div>
            <div
              className="w-full overflow-hidden rounded-xl border border-slate-700 bg-white"
              style={{ aspectRatio: aspect }}
            >
              <SlideSvg preview={preview} className="h-full w-full" />
            </div>
            <p className="mt-3 text-center text-xs text-slate-400">
              Esc 키를 누르거나 바깥쪽을 클릭하면 닫힙니다.
            </p>
          </div>
        </div>
      ) : null}
    </div>
  );
}
