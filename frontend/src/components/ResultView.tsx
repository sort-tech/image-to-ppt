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
import type { ConvertResponse, ElementType } from "../types/slide";

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

/**
 * The finished conversion: the rendered preview, the download button, element
 * tallies, and the two diagnostic panels.
 *
 * The preview comes FIRST on purpose - the user should see the slide before
 * deciding to download it.
 *
 * `dropped` and `warnings` are kept visually distinct on purpose - a dropped
 * element means something is MISSING from the deck (amber), while a warning
 * means everything is there but something was adjusted (neutral).
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
          <h3 className="text-sm font-semibold text-slate-100">네이티브 요소</h3>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <div className="rounded-xl border border-slate-700 bg-slate-950/60 px-4 py-3">
            <p className="text-2xl font-semibold tabular-nums text-slate-50">{total}</p>
            <p className="text-xs text-slate-400">전체</p>
          </div>
          <div className="flex flex-wrap gap-2">
            {BADGES.map(({ key, label, icon: Icon, tone }) => (
              <span
                key={key}
                className={`inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs font-medium ${tone}`}
              >
                <Icon aria-hidden="true" className="h-3.5 w-3.5" />
                <span className="tabular-nums">{counts[key] ?? 0}</span>
                <span className="text-slate-400">{label}</span>
              </span>
            ))}
          </div>
        </div>
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
              렌더링 조정 {warnings.length}건
            </h3>
          </div>
          <p className="mb-3 text-xs leading-relaxed text-slate-400">
            해당 요소는 슬라이드에 들어 있지만, 슬라이드 안에 들어가도록 크기나 위치가
            조정되었습니다. 위 미리보기에 조정된 결과가 그대로 반영되어 있습니다.
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
