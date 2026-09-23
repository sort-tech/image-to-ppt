import { useCallback, useEffect, useId, useMemo, useState } from "react";
import {
  Check,
  CircleAlert,
  CircleCheck,
  Clipboard,
  Download,
  LoaderCircle,
  RefreshCw,
  RotateCcw,
} from "lucide-react";

import type { SlideSpec } from "../types/slide";

interface SpecEditorProps {
  /** The spec from the last conversion, or null before the first run. */
  spec: SlideSpec | null;
  /** Hands the parsed payload to POST /api/preview. */
  onRefreshPreview: (payload: unknown) => void;
  /** Hands the parsed payload to POST /api/build-from-json and saves the file. */
  onDownload: (payload: unknown) => void;
  /** Called when the buffer is reset, so the shown preview can go back too. */
  onRevert?: () => void;
  /** A /api/preview call is in flight. */
  refreshing?: boolean;
  /** A /api/build-from-json call is in flight. */
  downloading?: boolean;
}

interface ParseState {
  ok: boolean;
  /**
   * Why the buffer is not usable yet.
   *
   * An empty editor is not a JSON error, and labelling it as one produced the
   * run-on "JSON 오류 편집기가 비어 있습니다.". The status line branches on this
   * instead of gluing one fixed label onto both cases.
   */
  kind: "ok" | "empty" | "syntax";
  error: string | null;
  value: unknown;
}

function parse(text: string): ParseState {
  const trimmed = text.trim();
  if (!trimmed) {
    return { ok: false, kind: "empty", error: null, value: null };
  }
  try {
    return { ok: true, kind: "ok", error: null, value: JSON.parse(trimmed) as unknown };
  } catch (err) {
    return {
      ok: false,
      kind: "syntax",
      // The engine's own parse message names the offending position, which is
      // the only genuinely useful part - so it is kept verbatim as the DETAIL,
      // introduced by a Korean sentence rather than standing in for one.
      error: err instanceof Error ? err.message : null,
      value: null,
    };
  }
}

/**
 * An editable view of the returned SlideSpec, with its own preview and download.
 *
 * The text is sent to the server as typed (once it parses): the backend reuses
 * `normalize_spec_payload`, so a bare spec, a `{"spec": {...}}` wrapper and a
 * bare element array are all legitimate here and clamping the input to one
 * shape would only take working options away.
 *
 * "미리보기 갱신" goes through `/api/preview`, which builds the deck on the
 * server and reports its real geometry back, so the preview reflects the actual
 * builder - not a second geometry implementation living in the browser.
 */
export default function SpecEditor({
  spec,
  onRefreshPreview,
  onDownload,
  onRevert,
  refreshing = false,
  downloading = false,
}: SpecEditorProps) {
  const textareaId = useId();
  const statusId = `${textareaId}-status`;

  const original = useMemo(
    () => (spec ? `${JSON.stringify(spec, null, 2)}\n` : ""),
    [spec],
  );

  const [text, setText] = useState(original);
  const [copied, setCopied] = useState(false);

  // A new conversion replaces the buffer; edits within one run are kept.
  useEffect(() => {
    setText(original);
  }, [original]);

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1600);
    return () => window.clearTimeout(timer);
  }, [copied]);

  const state = useMemo(() => parse(text), [text]);
  const dirty = text !== original;
  const busy = refreshing || downloading;

  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      // Clipboard permission denied / insecure context: fall back to selecting
      // the text so the user can copy it by hand instead of silently failing.
      const node = document.getElementById(textareaId);
      if (node instanceof HTMLTextAreaElement) {
        node.focus();
        node.select();
      }
    }
  }, [text, textareaId]);

  const revert = useCallback(() => {
    setText(original);
    onRevert?.();
  }, [onRevert, original]);

  if (!spec) {
    return (
      <div className="rounded-2xl border border-dashed border-slate-800 bg-slate-900/30 px-6 py-16 text-center">
        <p className="text-sm font-medium text-slate-300">아직 편집할 내용이 없습니다</p>
        <p className="mx-auto mt-2 max-w-sm text-xs leading-relaxed text-slate-500">
          변환이 끝나면 검증된 <span className="font-mono">SlideSpec</span>이 여기에 표시됩니다.
          좌표·색상·문구를 직접 수정하고 미리보기를 갱신한 뒤, 모델을 다시 호출하지 않고 바로
          PPTX로 내려받을 수 있습니다.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-3 animate-fade-in">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <label htmlFor={textareaId} className="text-xs font-medium text-slate-300">
          Spec JSON
          {dirty ? <span className="ml-2 text-amber-300">수정됨</span> : null}
        </label>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => void copy()}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 px-2.5 py-1.5 text-xs font-medium text-slate-200 transition hover:border-sky-500 hover:text-sky-300"
          >
            {copied ? (
              <Check aria-hidden="true" className="h-3.5 w-3.5 text-emerald-400" />
            ) : (
              <Clipboard aria-hidden="true" className="h-3.5 w-3.5" />
            )}
            {copied ? "복사됨" : "복사"}
          </button>
          <button
            type="button"
            onClick={revert}
            disabled={!dirty}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 px-2.5 py-1.5 text-xs font-medium text-slate-200 transition hover:border-sky-500 hover:text-sky-300 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <RotateCcw aria-hidden="true" className="h-3.5 w-3.5" />
            되돌리기
          </button>
        </div>
      </div>

      <textarea
        id={textareaId}
        value={text}
        onChange={(event) => setText(event.target.value)}
        spellCheck={false}
        aria-invalid={!state.ok}
        aria-describedby={statusId}
        className={`h-[26rem] w-full resize-y rounded-xl border bg-slate-950/70 px-3 py-3 font-mono text-xs leading-relaxed text-slate-200 transition scrollbar-slim ${
          state.ok
            ? "border-slate-700 focus:border-sky-500"
            : "border-rose-500/60 focus:border-rose-400"
        }`}
      />

      <p
        id={statusId}
        role="status"
        aria-live="polite"
        className={`flex items-start gap-1.5 text-xs ${
          state.ok ? "text-emerald-300" : "text-rose-300"
        }`}
      >
        {state.ok ? (
          <>
            <CircleCheck aria-hidden="true" className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            올바른 JSON입니다
          </>
        ) : state.kind === "empty" ? (
          <>
            <CircleAlert aria-hidden="true" className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span className="min-w-0">편집기가 비어 있습니다. Spec JSON을 붙여 넣어 주세요.</span>
          </>
        ) : (
          <>
            <CircleAlert aria-hidden="true" className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span className="min-w-0">
              JSON 형식 오류입니다.
              {state.error ? (
                <>
                  {" 브라우저 메시지: "}
                  <span className="font-mono [overflow-wrap:anywhere]">{state.error}</span>
                </>
              ) : null}
            </span>
          </>
        )}
      </p>

      <div className="grid gap-2 sm:grid-cols-2">
        <button
          type="button"
          onClick={() => {
            if (!state.ok || busy) return;
            onRefreshPreview(state.value);
          }}
          disabled={!state.ok || busy}
          className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-sky-500 px-5 py-3 text-sm font-semibold text-sky-950 shadow-lg shadow-sky-500/20 transition hover:bg-sky-400 disabled:cursor-not-allowed disabled:bg-slate-700 disabled:text-slate-400 disabled:shadow-none"
        >
          {refreshing ? (
            <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin" />
          ) : (
            <RefreshCw aria-hidden="true" className="h-4 w-4" />
          )}
          {refreshing ? "갱신 중..." : "미리보기 갱신"}
        </button>

        <button
          type="button"
          onClick={() => {
            if (!state.ok || busy) return;
            onDownload(state.value);
          }}
          disabled={!state.ok || busy}
          className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-emerald-500 px-5 py-3 text-sm font-semibold text-emerald-950 shadow-lg shadow-emerald-500/20 transition hover:bg-emerald-400 disabled:cursor-not-allowed disabled:bg-slate-700 disabled:text-slate-400 disabled:shadow-none"
        >
          {downloading ? (
            <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin" />
          ) : (
            <Download aria-hidden="true" className="h-4 w-4" />
          )}
          {downloading ? "준비 중..." : "수정한 JSON으로 다운로드"}
        </button>
      </div>

      <p className="text-center text-xs text-slate-500">
        {state.ok
          ? "미리보기를 갱신하면 결과 탭의 슬라이드가 수정한 JSON 기준으로 다시 그려집니다."
          : "위의 JSON을 고치면 두 버튼이 다시 활성화됩니다."}
      </p>
    </div>
  );
}
