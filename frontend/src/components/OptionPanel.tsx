import { useId } from "react";
import {
  KeyRound,
  LoaderCircle,
  RotateCcw,
  Settings2,
  Sparkles,
  TriangleAlert,
} from "lucide-react";

import type { RefineContext, RefineOutcome } from "../types/slide";

interface OptionPanelProps {
  extraInstructions: string;
  onExtraInstructionsChange: (value: string) => void;
  /** Sends the note to `POST /api/refine-instructions`. */
  onRefine: () => void;
  /** A refine call is in flight. */
  refining?: boolean;
  /**
   * What the last completed assist did, or null when none has run (or when the
   * box has been edited since, so it no longer describes what is in there).
   */
  refineOutcome?: RefineOutcome | null;
  /** Puts back the exact text the user had typed before the last assist. */
  onUndoRefine?: () => void;
  /** True while that undo is still meaningful. */
  canUndoRefine?: boolean;
  /** True when the backend reports no API key: a conversion cannot succeed. */
  keyMissing?: boolean;
  disabled?: boolean;
}

/**
 * What each `context_used` value means, in the user's words.
 *
 * Deliberately keyed on the server's answer rather than on what the UI sent:
 * the request may carry a spec the server could not read, in which case it
 * falls back on its own and the honest line is the one for what it really used.
 * A value not in this map renders no line at all - saying nothing beats
 * describing the wrong context.
 *
 * Each line states only what happened, which took two corrections:
 *
 * - `spec` said "직전 변환의" (the PREVIOUS conversion's) analysis, but the
 *   context sent is the spec of the slide currently on screen - which is the
 *   hand-edited one whenever the user has edited it and pressed 미리보기 갱신.
 *   So it names the slide on screen, which is true in both cases.
 * - `none` claimed a cause ("참고할 분석 결과나 이미지가 없어") *and* an effect
 *   ("메모 문장만 다듬었습니다"), and on the common first use - no conversion
 *   yet - the effect was simply false: the model writes a full instruction from
 *   the note. The effect now describes what it did. The other state that used
 *   to land here, a model that returned nothing usable, is no longer described
 *   by this map at all (see {@link FALLBACK_NOTE}): its cause is the model, not
 *   a missing context, and the context was usually right there.
 */
const CONTEXT_NOTE: Record<RefineContext, string> = {
  spec: "현재 화면의 슬라이드 분석 결과를 참고해 완성했습니다.",
  image: "선택한 이미지를 참고해 완성했습니다.",
  none: "참고할 분석 결과나 이미지 없이 메모 내용만으로 문장을 만들었습니다.",
};

/**
 * The model gave nothing usable, so what came back is the user's own note.
 *
 * Two wordings, because the two cases differ in what the user can see: the note
 * came back untouched, or it came back collapsed to one line (the server tidies
 * it). Only the second is something 되돌리기 can undo, and the panel does not
 * offer that button for the first - there is nothing to restore.
 */
const FALLBACK_NOTE: Record<"unchanged" | "tidied", string> = {
  unchanged: "모델이 문장을 완성하지 못해, 적어 두신 메모를 그대로 두었습니다.",
  tidied: "모델이 문장을 완성하지 못해, 적어 두신 메모를 한 줄로 정리만 했습니다.",
};

/** The one sentence that is true about {@link RefineOutcome}, or null. */
function describeOutcome(outcome: RefineOutcome): string | null {
  if (outcome.fellBack) {
    return FALLBACK_NOTE[outcome.changed ? "tidied" : "unchanged"];
  }
  return CONTEXT_NOTE[outcome.context] ?? null;
}

/**
 * Conversion options: the extra instructions, plus the assist that writes them.
 *
 * There is no model control here by design. The Vision model in use is the one
 * the backend is configured with, and the header status pill reports which one
 * that is - so the fact is still on screen even though the choice is not.
 *
 * The instructions field sends nothing when left blank, so the server keeps its
 * own defaults rather than receiving an empty string.
 *
 * The assist REPLACES what is in the textarea, which is why 되돌리기 sits next
 * to it and why the panel never clears the field on its own: the note is the
 * user's own writing, and a feature meant to improve it must not be able to
 * lose it. 되돌리기 stays clickable while an assist is running, because there it
 * means "cancel this and give me my sentence back" - the parent aborts the
 * request so its reply cannot land on the restored text.
 */
export default function OptionPanel({
  extraInstructions,
  onExtraInstructionsChange,
  onRefine,
  refining = false,
  refineOutcome = null,
  onUndoRefine,
  canUndoRefine = false,
  keyMissing = false,
  disabled = false,
}: OptionPanelProps) {
  const baseId = useId();
  const extraId = `${baseId}-extra`;
  const helpId = `${baseId}-extra-help`;
  const statusId = `${baseId}-extra-status`;

  const blankNote = extraInstructions.trim().length === 0;
  const canRefine = !disabled && !refining && !blankNote;
  const outcomeNote = refineOutcome ? describeOutcome(refineOutcome) : null;
  // A fallback is not an error - the note is intact and convertible - but it is
  // not the completion the user asked for either, so it does not get the tone
  // that says "done".
  const outcomeTone = refineOutcome?.fellBack ? "text-amber-200" : "text-sky-300";

  return (
    <section
      aria-labelledby={`${baseId}-heading`}
      className="rounded-2xl border border-slate-800 bg-slate-900/50 p-5 shadow-lg shadow-slate-950/40 backdrop-blur"
    >
      <div className="mb-4 flex items-center gap-2">
        <Settings2 aria-hidden="true" className="h-4 w-4 text-sky-400" />
        <h2
          id={`${baseId}-heading`}
          className="text-sm font-semibold tracking-wide text-slate-100"
        >
          변환 옵션
        </h2>
      </div>

      <div className="space-y-4">
        <div>
          <div className="mb-1.5 flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5">
            <label htmlFor={extraId} className="text-xs font-medium text-slate-300">
              추가 지시사항 <span className="font-normal text-slate-500">(선택)</span>
            </label>

            <div className="flex items-center gap-2">
              {canUndoRefine && onUndoRefine ? (
                <button
                  type="button"
                  onClick={onUndoRefine}
                  disabled={disabled}
                  title={
                    refining
                      ? "진행 중인 문장 완성을 취소하고, 직접 적은 문장으로 되돌립니다"
                      : "완성 전에 직접 적은 문장으로 되돌립니다"
                  }
                  className="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 px-2.5 py-1.5 text-xs font-medium text-slate-200 transition hover:border-sky-500 hover:text-sky-300 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <RotateCcw aria-hidden="true" className="h-3.5 w-3.5" />
                  되돌리기
                </button>
              ) : null}

              <button
                type="button"
                onClick={onRefine}
                disabled={!canRefine}
                title={
                  blankNote
                    ? "먼저 바꾸고 싶은 내용을 한 줄이라도 적어 주세요"
                    : "적어 둔 메모를 분석 모델이 이해하기 쉬운 문장으로 완성합니다"
                }
                className="inline-flex items-center gap-1.5 rounded-lg border border-sky-500/40 bg-sky-500/10 px-2.5 py-1.5 text-xs font-medium text-sky-200 transition hover:border-sky-400 hover:bg-sky-500/20 hover:text-sky-100 disabled:cursor-not-allowed disabled:border-slate-700 disabled:bg-transparent disabled:text-slate-500"
              >
                {refining ? (
                  <LoaderCircle aria-hidden="true" className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Sparkles aria-hidden="true" className="h-3.5 w-3.5" />
                )}
                {refining ? "완성 중..." : "AI로 문장 완성"}
              </button>
            </div>
          </div>

          <textarea
            id={extraId}
            value={extraInstructions}
            onChange={(event) => onExtraInstructionsChange(event.target.value)}
            placeholder="예: 표는 그대로 살리고 하단 로고는 무시해 주세요. 글꼴은 좀 더 크게."
            rows={3}
            disabled={disabled}
            aria-describedby={`${helpId} ${statusId}`}
            className="w-full resize-y rounded-lg border border-slate-700 bg-slate-950/60 px-3 py-2 text-sm leading-relaxed text-slate-100 placeholder:text-slate-500 transition focus:border-sky-500 disabled:opacity-50 scrollbar-slim"
          />

          <p id={helpId} className="mt-1 text-xs leading-relaxed text-slate-500">
            어떤 요소를 살리고 무엇을 무시할지 모델에게 한국어로 적어 주면 됩니다. 거칠게
            적어도 괜찮습니다. <span className="text-slate-400">AI로 문장 완성</span>을 누르면
            변환 결과나 이미지를 참고해 분석 모델이 알아듣기 쉬운 문장으로 완성해 드립니다.
          </p>

          {/* Fixed minimum height so the panel does not jump as this line
              appears and goes. */}
          <p
            id={statusId}
            role="status"
            aria-live="polite"
            className={`mt-1 min-h-[1.25rem] text-xs leading-relaxed ${
              refining ? "text-sky-300" : outcomeTone
            }`}
          >
            {refining ? (
              "메모를 바탕으로 문장을 완성하는 중..."
            ) : outcomeNote ? (
              <span className="inline-flex items-start gap-1.5">
                {refineOutcome?.fellBack ? (
                  <TriangleAlert
                    aria-hidden="true"
                    className="mt-0.5 h-3.5 w-3.5 shrink-0"
                  />
                ) : (
                  <Sparkles aria-hidden="true" className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                )}
                <span className="min-w-0">{outcomeNote}</span>
              </span>
            ) : (
              ""
            )}
          </p>
        </div>

        {keyMissing ? (
          <p className="flex items-start gap-2 rounded-xl border border-amber-500/40 bg-amber-500/5 px-3 py-2.5 text-xs leading-relaxed text-amber-200">
            <KeyRound aria-hidden="true" className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span className="min-w-0">
              백엔드에 API 키가 설정되어 있지 않아 변환이{" "}
              <span className="font-mono">missing_api_key</span> 오류로 실패합니다. 프로젝트 루트의{" "}
              <span className="font-mono">.env</span> 파일에 LiteLLM 프록시 키를 넣고 백엔드를 다시
              시작해 주세요.
            </span>
          </p>
        ) : null}
      </div>
    </section>
  );
}
