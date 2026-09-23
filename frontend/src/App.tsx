import type { ReactNode } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  FileJson,
  Eye,
  KeyRound,
  LoaderCircle,
  Presentation,
  ServerCrash,
  Sparkles,
  TriangleAlert,
  WandSparkles,
  X,
  Zap,
} from "lucide-react";

import {
  buildFromJson,
  convert,
  downloadResult,
  getHealth,
  previewSpec,
  saveBlob,
  toApiError,
} from "./api/client";
import FileUploader from "./components/FileUploader";
import OptionPanel from "./components/OptionPanel";
import ResultView from "./components/ResultView";
import SpecEditor from "./components/SpecEditor";
import type {
  ApiError,
  BuiltDeck,
  ConvertResponse,
  ElementCounts,
  HealthResponse,
  SlidePreview,
} from "./types/slide";

/** Used until /api/health answers, and if it never does. */
const FALLBACK_EXTENSIONS = [".jpeg", ".jpg", ".png", ".webp"];

const PROGRESS = [
  "이미지를 업로드하는 중...",
  "Vision 모델이 레이아웃을 읽는 중...",
  "요소를 0..1000 좌표계로 옮기는 중...",
  "요소를 하나씩 검증하는 중...",
  "네이티브 도형·표·텍스트 상자를 그리는 중...",
  "미리보기를 만드는 중...",
];

const WORKING = "처리 중...";

type Tab = "result" | "spec";

/**
 * A preview refreshed from the edited Spec JSON via `/api/preview`.
 *
 * `payload` is kept alongside the numbers because it is what the download has
 * to be built from: once the shown preview comes from an edit, downloading the
 * originally converted deck would hand over a file that does not match what
 * the user is looking at.
 */
interface EditedOutcome {
  payload: unknown;
  counts: ElementCounts;
  total: number;
  dropped: string[];
  warnings: string[];
  preview: SlidePreview | null;
}

/** "파일명: 도형 3개, 표 1개, 텍스트 상자 6개 - 요소 2개 건너뜀" */
function describeDeck(deck: BuiltDeck): string {
  const counts = deck.counts;
  const tally = counts
    ? `도형 ${counts.shape}개, 표 ${counts.table}개, 텍스트 상자 ${counts.text}개`
    : "생성 완료";
  const droppedNote =
    deck.droppedCount && deck.droppedCount > 0
      ? ` - 요소 ${deck.droppedCount}개 건너뜀`
      : "";
  return `${deck.filename} 다운로드: ${tally}${droppedNote}`;
}

export default function App() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [healthError, setHealthError] = useState<ApiError | null>(null);
  const [healthLoading, setHealthLoading] = useState(true);

  const [file, setFile] = useState<File | null>(null);
  const [model, setModel] = useState("");
  const [extraInstructions, setExtraInstructions] = useState("");

  const [converting, setConverting] = useState(false);
  const [progress, setProgress] = useState<string>(PROGRESS[0] ?? WORKING);
  const [result, setResult] = useState<ConvertResponse | null>(null);
  const [edited, setEdited] = useState<EditedOutcome | null>(null);
  const [tab, setTab] = useState<Tab>("result");

  const [downloading, setDownloading] = useState(false);
  const [specDownloading, setSpecDownloading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  /** Belt-and-braces double-submit guard: state updates are async. */
  const inFlight = useRef(false);

  /* ----------------------------------------------------------------------- */
  /* Health                                                                  */
  /* ----------------------------------------------------------------------- */

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const data = await getHealth(controller.signal);
        setHealth(data);
        setHealthError(null);
      } catch (err) {
        if (controller.signal.aborted) return;
        setHealth(null);
        setHealthError(toApiError(err).toDetail());
      } finally {
        if (!controller.signal.aborted) setHealthLoading(false);
      }
    })();
    return () => controller.abort();
  }, []);

  const acceptedExtensions = useMemo(
    () =>
      health?.supported_extensions?.length
        ? health.supported_extensions
        : FALLBACK_EXTENSIONS,
    [health],
  );

  const keyMissing = health ? !health.proxy.api_key_configured : false;

  /* ----------------------------------------------------------------------- */
  /* Progress ticker                                                         */
  /* ----------------------------------------------------------------------- */

  useEffect(() => {
    if (!converting) return;
    let index = 0;
    setProgress(PROGRESS[0] ?? WORKING);
    const timer = window.setInterval(() => {
      index = Math.min(index + 1, PROGRESS.length - 1);
      setProgress(PROGRESS[index] ?? WORKING);
    }, 2200);
    return () => window.clearInterval(timer);
  }, [converting]);

  /* ----------------------------------------------------------------------- */
  /* What the result panel shows                                             */
  /* ----------------------------------------------------------------------- */

  /**
   * The conversion, with the edited preview's numbers laid over it when there
   * is one - so the tallies, the diagnostics and the slide on screen always
   * describe the same deck.
   */
  const view = useMemo<ConvertResponse | null>(() => {
    if (!result) return null;
    if (!edited) return result;
    return {
      ...result,
      counts: edited.counts,
      total: edited.total,
      dropped: edited.dropped,
      warnings: edited.warnings,
      preview: edited.preview,
    };
  }, [edited, result]);

  /* ----------------------------------------------------------------------- */
  /* Actions                                                                 */
  /* ----------------------------------------------------------------------- */

  const handleConvert = useCallback(async () => {
    if (inFlight.current || !file) return;
    inFlight.current = true;
    setConverting(true);
    setError(null);
    setNote(null);
    // Drop the previous run BEFORE this one starts. Keeping it meant a failed
    // conversion left "변환 완료", the old filename, the old tallies, the old
    // preview and a live download button bound to the old download_id sitting
    // next to the new image and an error banner - one click away from saving
    // the wrong deck - and left the Spec JSON tab holding the old spec, so a
    // download from there silently handed over the previous slide.
    setResult(null);
    setEdited(null);
    setTab("result");

    try {
      const data = await convert({
        file,
        model,
        extra_instructions: extraInstructions,
      });
      setResult(data);
      setTab("result");
    } catch (err) {
      setError(toApiError(err).toDetail());
    } finally {
      inFlight.current = false;
      setConverting(false);
    }
  }, [extraInstructions, file, model]);

  /**
   * The result panel's download.
   *
   * With an edited preview on screen it rebuilds from that edit rather than
   * fetching the stored deck, because the stored deck is the pre-edit one.
   */
  const handleDownload = useCallback(async () => {
    if (!result || downloading) return;
    setDownloading(true);
    setError(null);
    setNote(null);
    try {
      if (edited) {
        const deck = await buildFromJson(edited.payload, result.filename);
        saveBlob(deck.blob, deck.filename);
        setNote(describeDeck(deck));
      } else {
        await downloadResult(result.download_id, result.filename);
      }
    } catch (err) {
      setError(toApiError(err).toDetail());
    } finally {
      setDownloading(false);
    }
  }, [downloading, edited, result]);

  /** Spec JSON tab -> POST /api/build-from-json, straight to a saved file. */
  const handleSpecDownload = useCallback(
    async (payload: unknown) => {
      if (specDownloading) return;
      setSpecDownloading(true);
      setError(null);
      setNote(null);
      try {
        const deck = await buildFromJson(payload, result?.filename ?? "slide.pptx");
        saveBlob(deck.blob, deck.filename);
        setNote(describeDeck(deck));
      } catch (err) {
        setError(toApiError(err).toDetail());
      } finally {
        setSpecDownloading(false);
      }
    },
    [result, specDownloading],
  );

  /** Spec JSON tab -> POST /api/preview, which re-derives it from a real build. */
  const handleRefreshPreview = useCallback(
    async (payload: unknown) => {
      if (refreshing) return;
      setRefreshing(true);
      setError(null);
      setNote(null);
      try {
        const data = await previewSpec(payload);
        setEdited({
          payload,
          counts: data.counts,
          total: data.total,
          dropped: data.dropped,
          warnings: data.warnings,
          preview: data.preview,
        });
        // The user just asked to see the slide, so show it.
        setTab("result");
        setNote("수정한 Spec JSON으로 미리보기를 갱신했습니다.");
      } catch (err) {
        setError(toApiError(err).toDetail());
      } finally {
        setRefreshing(false);
      }
    },
    [refreshing],
  );

  /** Reverting the editor puts the originally converted slide back on screen. */
  const handleRevert = useCallback(() => {
    setEdited(null);
    setNote(null);
  }, []);

  /** A different image makes the shown result stale too, so it goes as well. */
  const chooseFile = useCallback((next: File | null) => {
    setFile(next);
    setResult(null);
    setEdited(null);
    setNote(null);
  }, []);

  const disabledReason = !file ? "먼저 이미지를 선택해 주세요" : null;
  const busy = converting || inFlight.current;

  /* ----------------------------------------------------------------------- */
  /* Render                                                                  */
  /* ----------------------------------------------------------------------- */

  return (
    <div className="min-h-full">
      <header className="border-b border-slate-800/80 bg-slate-950/60 backdrop-blur">
        <div className="mx-auto flex max-w-7xl flex-col gap-4 px-4 py-6 sm:px-6 lg:flex-row lg:items-center lg:justify-between lg:px-8">
          <div className="flex items-start gap-3">
            <span className="mt-0.5 rounded-xl border border-sky-500/30 bg-sky-500/10 p-2">
              <Presentation aria-hidden="true" className="h-6 w-6 text-sky-400" />
            </span>
            <div>
              <h1 className="text-xl font-semibold tracking-tight text-slate-50 sm:text-2xl">
                이미지 → 편집 가능한 PPTX 변환기
              </h1>
              <p className="mt-1 max-w-2xl text-sm text-slate-400">
                Vision 모델이 이미지를 읽어 슬라이드 구성을 0..1000 좌표계로 정리하고, 그 결과를
                실제 도형·표·텍스트 상자로 그려 넣습니다. 납작하게 박힌 이미지가 아니라
                PowerPoint에서 문구·색상·위치를 그대로 편집할 수 있는 슬라이드가 만들어집니다.
              </p>
            </div>
          </div>

          <StatusPill loading={healthLoading} health={health} error={healthError} />
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
        {/* min-w-0 on both columns: a grid track is sized from its content's
            min-content width, so without it one long unbreakable string in a
            diagnostic list widens the column and drags the whole page into a
            horizontal scroll. */}
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          {/* ---------------------------- LEFT ---------------------------- */}
          <div className="min-w-0 space-y-5">
            <FileUploader
              file={file}
              onSelect={chooseFile}
              onClear={() => chooseFile(null)}
              acceptedExtensions={acceptedExtensions}
              disabled={busy}
            />

            <OptionPanel
              model={model}
              onModelChange={setModel}
              extraInstructions={extraInstructions}
              onExtraInstructionsChange={setExtraInstructions}
              modelPlaceholder={health?.proxy.model ?? "gemini-flash"}
              keyMissing={keyMissing}
              disabled={busy}
            />

            <div>
              <button
                type="button"
                onClick={() => void handleConvert()}
                disabled={busy || !file}
                title={disabledReason ?? undefined}
                aria-describedby="convert-status"
                className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-sky-500 px-6 py-4 text-base font-semibold text-sky-950 shadow-lg shadow-sky-500/25 transition hover:bg-sky-400 disabled:cursor-not-allowed disabled:bg-slate-800 disabled:text-slate-500 disabled:shadow-none"
              >
                {busy ? (
                  <LoaderCircle aria-hidden="true" className="h-5 w-5 animate-spin" />
                ) : (
                  <WandSparkles aria-hidden="true" className="h-5 w-5" />
                )}
                {busy ? "변환 중..." : "PPTX로 변환"}
              </button>

              <p
                id="convert-status"
                role="status"
                aria-live="polite"
                className="min-h-[1.5rem] pt-2 text-center text-xs text-slate-400"
              >
                {busy ? progress : (disabledReason ?? "변환이 끝나면 다운로드 전에 미리보기를 보여드립니다.")}
              </p>
            </div>
          </div>

          {/* ---------------------------- RIGHT --------------------------- */}
          <div className="min-w-0 space-y-4">
            <div
              role="tablist"
              aria-label="변환 결과"
              className="flex gap-1 rounded-xl border border-slate-800 bg-slate-900/50 p-1"
            >
              <TabButton
                id="tab-result"
                panelId="panel-result"
                active={tab === "result"}
                onClick={() => setTab("result")}
                icon={<Eye aria-hidden="true" className="h-4 w-4" />}
                label="결과 미리보기"
                badge={view ? String(view.total) : null}
              />
              <TabButton
                id="tab-spec"
                panelId="panel-spec"
                active={tab === "spec"}
                onClick={() => setTab("spec")}
                icon={<FileJson aria-hidden="true" className="h-4 w-4" />}
                label="Spec JSON"
                badge={null}
              />
            </div>

            <div
              role="tabpanel"
              id="panel-result"
              aria-labelledby="tab-result"
              hidden={tab !== "result"}
            >
              <ResultView
                result={view}
                onDownload={() => void handleDownload()}
                downloading={downloading}
                previewNote={
                  edited
                    ? "수정한 Spec JSON을 반영한 미리보기입니다. 다운로드도 같은 내용으로 만들어집니다."
                    : null
                }
              />
            </div>

            <div
              role="tabpanel"
              id="panel-spec"
              aria-labelledby="tab-spec"
              hidden={tab !== "spec"}
            >
              <SpecEditor
                spec={result?.spec ?? null}
                onRefreshPreview={(payload) => void handleRefreshPreview(payload)}
                onDownload={(payload) => void handleSpecDownload(payload)}
                onRevert={handleRevert}
                refreshing={refreshing}
                downloading={specDownloading}
              />
            </div>

            <p
              role="status"
              aria-live="polite"
              className="min-h-[1.25rem] text-xs text-emerald-300"
            >
              {note ?? ""}
            </p>
          </div>
        </div>
      </main>

      <ErrorBanner error={error} onDismiss={() => setError(null)} />
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Header status pill                                                         */
/* -------------------------------------------------------------------------- */

function StatusPill({
  loading,
  health,
  error,
}: {
  loading: boolean;
  health: HealthResponse | null;
  error: ApiError | null;
}) {
  let tone = "border-slate-700 bg-slate-900/70 text-slate-300";
  let icon = <LoaderCircle aria-hidden="true" className="h-3.5 w-3.5 animate-spin" />;
  let headline = "백엔드 확인 중...";
  let detail: string | null = null;

  if (!loading && error) {
    tone = "border-rose-500/40 bg-rose-500/10 text-rose-200";
    icon = <ServerCrash aria-hidden="true" className="h-3.5 w-3.5" />;
    headline = "백엔드에 연결할 수 없음";
    detail = "8000 포트에서 API를 실행해 주세요";
  } else if (!loading && health) {
    if (health.proxy.api_key_configured) {
      tone = "border-emerald-500/40 bg-emerald-500/10 text-emerald-200";
      icon = <Zap aria-hidden="true" className="h-3.5 w-3.5" />;
      headline = "백엔드 준비 완료";
      detail = `${health.proxy.model} · 키 설정됨`;
    } else {
      // With mock mode gone, the honest message is that a key is required.
      tone = "border-amber-500/40 bg-amber-500/10 text-amber-200";
      icon = <KeyRound aria-hidden="true" className="h-3.5 w-3.5" />;
      headline = "API 키 필요";
      detail = `${health.proxy.model} · .env에 키를 설정해 주세요`;
    }
  }

  return (
    <div
      role="status"
      aria-live="polite"
      className={`inline-flex shrink-0 items-center gap-2.5 self-start rounded-full border px-3.5 py-2 text-xs ${tone}`}
    >
      {icon}
      <span className="font-semibold">{headline}</span>
      {detail ? (
        <>
          <span aria-hidden="true" className="opacity-40">
            |
          </span>
          <span className="font-mono opacity-90">{detail}</span>
        </>
      ) : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Tab button                                                                 */
/* -------------------------------------------------------------------------- */

function TabButton({
  id,
  panelId,
  active,
  onClick,
  icon,
  label,
  badge,
}: {
  id: string;
  panelId: string;
  active: boolean;
  onClick: () => void;
  icon: ReactNode;
  label: string;
  badge: string | null;
}) {
  return (
    <button
      type="button"
      id={id}
      role="tab"
      aria-selected={active}
      aria-controls={panelId}
      onClick={onClick}
      className={`inline-flex flex-1 items-center justify-center gap-2 rounded-lg px-3 py-2 text-sm font-medium transition ${
        active
          ? "bg-slate-800 text-slate-50 shadow"
          : "text-slate-400 hover:bg-slate-800/50 hover:text-slate-200"
      }`}
    >
      {icon}
      {label}
      {badge ? (
        <span className="rounded-md bg-sky-500/20 px-1.5 py-0.5 font-mono text-xs text-sky-300">
          {badge}
        </span>
      ) : null}
    </button>
  );
}

/* -------------------------------------------------------------------------- */
/* Error banner                                                               */
/* -------------------------------------------------------------------------- */

function ErrorBanner({
  error,
  onDismiss,
}: {
  error: ApiError | null;
  onDismiss: () => void;
}) {
  return (
    <div
      role="alert"
      aria-live="assertive"
      className="pointer-events-none fixed inset-x-0 bottom-0 z-40 flex justify-center px-4 pb-6"
    >
      {error ? (
        <div className="pointer-events-auto w-full max-w-xl animate-slide-up rounded-2xl border border-rose-500/50 bg-rose-950/95 p-4 shadow-2xl shadow-rose-950/50 backdrop-blur">
          <div className="flex items-start gap-3">
            <TriangleAlert aria-hidden="true" className="mt-0.5 h-5 w-5 shrink-0 text-rose-300" />
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <p className="text-sm font-semibold text-rose-100">요청 실패</p>
                <code className="rounded bg-rose-500/20 px-1.5 py-0.5 text-xs text-rose-200">
                  {error.code}
                </code>
              </div>
              {/* A hint can carry an unbreakable token - `invalid_spec` quotes
                  the rejected element's raw `type` back in full - and without a
                  break rule the text left the banner and the viewport with no
                  way to scroll to it. `min-w-0` is what lets the span shrink
                  inside its flex parent; the break rule is what makes it wrap.
                  `whitespace-pre-wrap` keeps the per-element drop list's own
                  line breaks readable. */}
              <p className="mt-1 break-words text-sm leading-relaxed text-rose-100/90 [overflow-wrap:anywhere]">
                {error.message}
              </p>
              {error.hint ? (
                <p className="mt-2 flex items-start gap-1.5 rounded-lg bg-rose-500/10 px-2.5 py-2 text-xs leading-relaxed text-rose-200/90">
                  <Sparkles aria-hidden="true" className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                  <span className="min-w-0 max-h-56 overflow-y-auto whitespace-pre-wrap break-words [overflow-wrap:anywhere]">
                    {error.hint}
                  </span>
                </p>
              ) : null}
            </div>
            <button
              type="button"
              onClick={onDismiss}
              aria-label="오류 알림 닫기"
              className="shrink-0 rounded-lg p-1 text-rose-300 transition hover:bg-rose-500/20 hover:text-rose-100"
            >
              <X aria-hidden="true" className="h-4 w-4" />
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
