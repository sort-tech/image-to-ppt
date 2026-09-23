import {
  TriangleAlert,
  Download,
  FileText,
  ImageIcon,
  Info,
  Layers,
  LoaderCircle,
  MoveRight,
  Shapes,
  Sparkles,
  Table2,
  Type,
} from "lucide-react";

import SlidePreview from "./SlidePreview";
import { renderedCounts } from "../types/slide";
import type { ConvertResponse, ElementCounts, ElementType } from "../types/slide";

interface ResultViewProps {
  result: ConvertResponse | null;
  onDownload: () => void;
  downloading?: boolean;
  /** Shown under the preview caption when it came from the edited Spec JSON. */
  previewNote?: string | null;
}

/**
 * Korean needs no plural agreement, so one noun per element type is enough -
 * the English version carried a singular/plural pair here.
 */
const BADGES: ReadonlyArray<{
  key: ElementType;
  label: string;
  icon: typeof Shapes;
  tone: string;
}> = [
  { key: "shape", label: "도형", icon: Shapes, tone: "text-sky-300 border-sky-500/30 bg-sky-500/10" },
  { key: "table", label: "표", icon: Table2, tone: "text-violet-300 border-violet-500/30 bg-violet-500/10" },
  { key: "text", label: "텍스트 상자", icon: Type, tone: "text-emerald-300 border-emerald-500/30 bg-emerald-500/10" },
  // 연결선 = a diagram connector (an arrow between boxes), 이미지 = a region
  // cropped out of the source image. Neither borrows the amber of the
  // 건너뛴 요소 panel or the rose of an error, so a count cannot read as a
  // warning about itself.
  { key: "connector", label: "연결선", icon: MoveRight, tone: "text-cyan-300 border-cyan-500/30 bg-cyan-500/10" },
  { key: "image", label: "이미지", icon: ImageIcon, tone: "text-indigo-300 border-indigo-500/30 bg-indigo-500/10" },
];

/** The types a user can be told about, in the order the badges show them. */
const BADGE_KEYS: ReadonlyArray<ElementType> = BADGES.map(({ key }) => key);

/**
 * The types whose requested count exceeds what the deck holds, with the gap.
 *
 * Only an `image` element can realistically land here (the builder skips it
 * with no source image to crop from, or when the anti-flattening rule refuses
 * the crop), but the comparison is made for every type rather than special-
 * casing one: render isolation can skip anything, and a badge that overstates
 * the file is the same defect whichever type it is.
 */
function skippedByType(
  requested: ElementCounts,
  rendered: ElementCounts,
): Array<{ label: string; missing: number }> {
  const gaps: Array<{ label: string; missing: number }> = [];
  for (const { key, label } of BADGES) {
    const missing = (requested[key] ?? 0) - (rendered[key] ?? 0);
    if (missing > 0) gaps.push({ label, missing });
  }
  return gaps;
}

/**
 * The finished conversion: the rendered preview, the download button, element
 * tallies, and the two diagnostic panels.
 *
 * The preview comes FIRST on purpose - the user should see the slide before
 * deciding to download it.
 *
 * `dropped` and `warnings` are kept visually distinct on purpose - a dropped
 * element means something is MISSING from the deck because it failed
 * validation (amber), while a warning is a render-time adjustment or skip
 * (neutral). Note that "warning" no longer implies "in the deck": an `image`
 * element with nothing to crop from is reported here too, and the panel's copy
 * has to cover both rather than promising everything is on the slide.
 *
 * The tallies are read off the PREVIEW, which is derived from the built deck -
 * never from `counts`, which is what the spec asked for. The two differ exactly
 * when the builder skipped something, and that is the moment a badge must not
 * claim the file holds it.
 */
export default function ResultView({
  result,
  onDownload,
  downloading = false,
  previewNote = null,
}: ResultViewProps) {
  if (!result) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 rounded-2xl border border-dashed border-slate-800 bg-slate-900/30 px-6 py-16 text-center">
        <Sparkles aria-hidden="true" className="h-8 w-8 text-slate-600" />
        <p className="text-sm font-medium text-slate-300">아직 만들어진 슬라이드가 없습니다</p>
        <p className="max-w-sm text-xs leading-relaxed text-slate-500">
          왼쪽에서 이미지를 고르고 <span className="text-slate-300">PPTX로 변환</span>을 누르면
          결과가 여기에 표시됩니다. 납작한 스크린샷이 아니라 실제 도형·표·텍스트 상자로 이루어진,
          그대로 편집할 수 있는 슬라이드입니다.
        </p>
      </div>
    );
  }

  const { counts, total, dropped, warnings, filename, preview } = result;

  // What the deck really holds, read off the deck-derived preview. Null only
  // when the preview could not be derived at all - and then the panel says
  // whose numbers it is showing instead of quietly passing off the request.
  const rendered = renderedCounts(preview);
  const shown = rendered ?? counts;
  const shownTotal = rendered
    ? BADGE_KEYS.reduce((sum, key) => sum + (rendered[key] ?? 0), 0)
    : total;
  const skipped = rendered ? skippedByType(counts, rendered) : [];

  return (
    <div className="space-y-4 animate-fade-in">
      <SlidePreview preview={preview} note={previewNote} />

      <div className="rounded-2xl border border-emerald-500/30 bg-gradient-to-br from-emerald-500/10 to-slate-900/40 p-5">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="min-w-0">
            <p className="text-xs uppercase tracking-wider text-emerald-300/90">변환 완료</p>
            <p className="mt-1 truncate font-mono text-sm text-slate-100" title={filename}>
              {filename}
            </p>
          </div>
          <button
            type="button"
            onClick={onDownload}
            disabled={downloading}
            className="inline-flex items-center gap-2 rounded-xl bg-emerald-500 px-5 py-3 text-sm font-semibold text-emerald-950 shadow-lg shadow-emerald-500/20 transition hover:bg-emerald-400 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {downloading ? (
              <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin" />
            ) : (
              <Download aria-hidden="true" className="h-4 w-4" />
            )}
            {downloading ? "준비 중..." : "PPTX 다운로드"}
          </button>
        </div>
      </div>

      <div className="rounded-2xl border border-slate-800 bg-slate-900/50 p-5">
        <div className="mb-4 flex items-center gap-2">
          <Layers aria-hidden="true" className="h-4 w-4 text-sky-400" />
          <h3 className="text-sm font-semibold text-slate-100">슬라이드 구성 요소</h3>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <div className="rounded-xl border border-slate-700 bg-slate-950/60 px-4 py-3">
            <p className="text-2xl font-semibold tabular-nums text-slate-50">{shownTotal}</p>
            <p className="text-xs text-slate-400">전체</p>
          </div>
          <div className="flex flex-wrap gap-2">
            {BADGES.map(({ key, label, icon: Icon, tone }) => (
              <span
                key={key}
                className={`inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs font-medium ${tone}`}
              >
                <Icon aria-hidden="true" className="h-3.5 w-3.5" />
                <span className="tabular-nums">{shown[key] ?? 0}</span>
                <span className="text-slate-400">{label}</span>
              </span>
            ))}
          </div>
        </div>
        {/* The numbers above describe the FILE. When the spec asked for more
            than the file holds, the gap is named here rather than folded into
            a badge - otherwise a skipped image just looks like a smaller
            number and the user has no way to tell it was ever requested. */}
        {skipped.length > 0 ? (
          <p className="mt-3 text-xs leading-relaxed text-amber-200/90 [word-break:keep-all]">
            위 숫자는 실제로 만들어진 파일의 내용입니다. 사양에는 있었지만 파일에 들어가지 못한
            요소가 있습니다:{" "}
            {skipped.map(({ label, missing }) => `${label} ${missing}개`).join(", ")}. 아래
            경고 목록에서 이유를 확인하세요.
          </p>
        ) : null}
        {rendered === null ? (
          <p className="mt-3 text-xs leading-relaxed text-slate-400 [word-break:keep-all]">
            미리보기를 만들지 못해 위 숫자는 파일이 아니라 요청한 사양을 기준으로 표시했습니다.
            건너뛴 요소가 있으면 아래 경고 목록에 남아 있습니다.
          </p>
        ) : null}
      </div>

      {dropped.length > 0 ? (
        <div className="rounded-2xl border border-amber-500/40 bg-amber-500/5 p-5">
          <div className="mb-2 flex items-center gap-2">
            <TriangleAlert aria-hidden="true" className="h-4 w-4 text-amber-300" />
            <h3 className="text-sm font-semibold text-amber-200">
              건너뛴 요소 {dropped.length}개
            </h3>
          </div>
          <p className="mb-3 text-xs leading-relaxed text-amber-100/80">
            아래 요소는 값을 제대로 읽지 못해 검증을 통과하지 못했고,{" "}
            <strong>슬라이드에 포함되지 않았습니다</strong>. 나머지 요소는 모두 그대로
            그려졌습니다. Spec JSON 탭에서 직접 고쳐 다시 만들 수 있습니다.
          </p>
          {/* A drop reason is backend text with no length or whitespace
              guarantee: pydantic's discriminated-union error quotes the
              element's raw `type` verbatim, so one junk value from the model
              puts a 300-character unbreakable token here. Without a break rule
              that token set the list's min-content width, widened the grid
              column and dragged the whole page into a horizontal scroll (~42
              characters is enough on a phone). `[overflow-wrap:anywhere]` -
              not just `break-words` - is what shrinks the min-content width. */}
          <ul className="min-w-0 space-y-1.5">
            {dropped.map((reason, index) => (
              <li
                key={`${index}-${reason}`}
                className="min-w-0 rounded-lg bg-amber-950/30 px-3 py-2 font-mono text-xs leading-relaxed text-amber-100 [overflow-wrap:anywhere]"
              >
                {reason}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {warnings.length > 0 ? (
        <div className="rounded-2xl border border-slate-700 bg-slate-900/60 p-5">
          <div className="mb-2 flex items-center gap-2">
            <Info aria-hidden="true" className="h-4 w-4 text-slate-400" />
            <h3 className="text-sm font-semibold text-slate-200">
              렌더링 알림 {warnings.length}건
            </h3>
          </div>
          {/* This copy used to promise that every warned element was on the
              slide, merely resized or moved. An `image` element with no source
              image to crop from, or one the anti-flattening rule refused, is
              reported in the same array and is NOT on the slide - the warning
              body itself says "was NOT rendered". So the sentence covers both
              outcomes and points at the preview, which is read back from the
              file either way. */}
          <p className="mb-3 text-xs leading-relaxed text-slate-400 [word-break:keep-all]">
            아래 요소는 슬라이드에 들어가도록 크기나 위치가 조정되었거나, 그리지 못해
            건너뛰어졌습니다. 어느 쪽이든 위 미리보기는 실제로 만들어진 파일을 그대로 보여 줍니다.
            각 줄의 내용에서 어떤 경우인지 확인할 수 있습니다.
          </p>
          {/* Same containment as the dropped list above: a warning is
              forwarded stderr, so its length and content are the core's, not
              this component's, to decide. */}
          <ul className="min-w-0 space-y-1.5">
            {warnings.map((warning, index) => (
              <li
                key={`${index}-${warning}`}
                className="min-w-0 rounded-lg bg-slate-950/60 px-3 py-2 font-mono text-xs leading-relaxed text-slate-300 [overflow-wrap:anywhere]"
              >
                {warning}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {dropped.length === 0 && warnings.length === 0 ? (
        <p className="flex items-center gap-2 px-1 text-xs text-slate-500">
          <FileText aria-hidden="true" className="h-3.5 w-3.5" />
          모든 요소가 그대로 변환되었습니다. 건너뛴 요소도, 잘린 요소도 없습니다.
        </p>
      ) : null}
    </div>
  );
}
