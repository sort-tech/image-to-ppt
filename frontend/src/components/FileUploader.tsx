import type { DragEvent } from "react";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Image as ImageIcon, RefreshCw, Trash2, Upload } from "lucide-react";

interface FileUploaderProps {
  file: File | null;
  onSelect: (file: File) => void;
  onClear: () => void;
  /** Extensions the backend accepts, e.g. `[".jpeg", ".jpg", ".png", ".webp"]`. */
  acceptedExtensions: string[];
  disabled?: boolean;
}

function extensionOf(name: string): string {
  const dot = name.lastIndexOf(".");
  // `dot <= 0` is deliberate: a leading-dot name like ".png" has NO extension.
  // That is how POSIX reads it and how the backend reads it
  // (`Path(".png").suffix` is `''`), so accepting it here only bought the user
  // a 400 `unsupported_extension` from the server. The two layers have to agree
  // on what an extension is.
  return dot <= 0 ? "" : name.slice(dot).toLowerCase();
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "크기 알 수 없음";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit] ?? "GB"}`;
}

/**
 * Drag-and-drop / click-to-browse image picker with a live preview.
 *
 * The drop zone is a `<label>` wrapping a visually hidden but still focusable
 * `<input type="file">`, so the whole zone is clickable AND the control is
 * reachable by Tab and operated with Enter/Space by the browser itself - no
 * synthetic key handling, and the ring follows the input's focus.
 *
 * "Follows the input's focus" is done with Tailwind's `peer-focus-visible:*`,
 * which compiles to a GENERAL SIBLING selector, so the element that wears the
 * ring has to be a sibling of the input - in both branches it is the input's
 * next sibling, and nothing nested inside it can stand in for it.
 */
export default function FileUploader({
  file,
  onSelect,
  onClear,
  acceptedExtensions,
  disabled = false,
}: FileUploaderProps) {
  const inputId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [rejection, setRejection] = useState<string | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  /** Nested dragenter/dragleave pairs would otherwise flicker the highlight. */
  const dragDepth = useRef(0);

  // One object URL per file, revoked as soon as it is replaced or unmounted.
  useEffect(() => {
    if (!file) {
      setPreviewUrl(null);
      return;
    }
    const url = URL.createObjectURL(file);
    setPreviewUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  const accept = acceptedExtensions.join(",");
  const prettyExtensions = acceptedExtensions
    .map((ext) => ext.replace(/^\./, "").toUpperCase())
    .join(" · ");

  const take = useCallback(
    (candidate: File | null | undefined) => {
      if (!candidate) return;
      const ext = extensionOf(candidate.name);
      if (!acceptedExtensions.includes(ext)) {
        setRejection(
          ext
            ? `"${candidate.name}" 은(는) 지원하지 않는 이미지 형식입니다. 지원 형식: ${acceptedExtensions.join(", ")}`
            : `"${candidate.name}" 에는 확장자가 없습니다. ${acceptedExtensions.join(", ")} 중 하나로 끝나도록 이름을 바꿔 주세요 - ".png"가 아니라 "slide.png" 처럼요.`,
        );
        return;
      }
      if (candidate.size === 0) {
        setRejection(`"${candidate.name}" 은(는) 비어 있습니다(0바이트). 실제 이미지를 선택해 주세요.`);
        return;
      }
      setRejection(null);
      onSelect(candidate);
    },
    [acceptedExtensions, onSelect],
  );

  const resetDrag = () => {
    dragDepth.current = 0;
    setDragging(false);
  };

  const handleDrop = (event: DragEvent<HTMLElement>) => {
    event.preventDefault();
    resetDrag();
    if (disabled) return;
    take(event.dataTransfer.files?.[0]);
  };

  const handleDragEnter = (event: DragEvent<HTMLElement>) => {
    event.preventDefault();
    if (disabled) return;
    dragDepth.current += 1;
    setDragging(true);
  };

  const handleDragOver = (event: DragEvent<HTMLElement>) => {
    // Without this the browser opens the file instead of firing `drop`.
    event.preventDefault();
    if (!disabled) event.dataTransfer.dropEffect = "copy";
  };

  const handleDragLeave = (event: DragEvent<HTMLElement>) => {
    event.preventDefault();
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setDragging(false);
  };

  const clear = () => {
    setRejection(null);
    // Let the same file be picked again after a remove.
    if (inputRef.current) inputRef.current.value = "";
    onClear();
  };

  const zoneTone = dragging
    ? "border-sky-400 bg-sky-500/10"
    : rejection
      ? "border-rose-500/60 bg-rose-500/5"
      : "border-slate-700 bg-slate-900/40 hover:border-slate-500 hover:bg-slate-900/70";

  return (
    <section
      aria-labelledby={`${inputId}-heading`}
      className="rounded-2xl border border-slate-800 bg-slate-900/50 p-5 shadow-lg shadow-slate-950/40 backdrop-blur"
    >
      <div className="mb-4 flex items-center gap-2">
        <ImageIcon aria-hidden="true" className="h-4 w-4 text-sky-400" />
        <h2 id={`${inputId}-heading`} className="text-sm font-semibold tracking-wide text-slate-100">
          원본 이미지
        </h2>
      </div>

      <input
        ref={inputRef}
        id={inputId}
        type="file"
        accept={accept}
        disabled={disabled}
        className="peer sr-only"
        onChange={(event) => take(event.target.files?.[0])}
      />

      {/* The ring lives on THIS div because it is the input's next element
          sibling, which is what Tailwind's `peer-*` variants compile to
          (`.peer:focus-visible ~ .peer-focus-visible\:ring-2`). The same
          classes on the nested "Replace" chip below could never match - it is
          a grandchild, not a sibling - and the input's own focus ring is drawn
          on an `sr-only` 1x1 clipped box, so tabbing to the file picker with a
          file selected showed NO focus indicator anywhere (WCAG 2.4.7). The
          empty-state drop zone rings the whole zone for the same reason, so
          ringing the whole card here is also the consistent behaviour. */}
      {file && previewUrl ? (
        <div className="space-y-3 rounded-2xl peer-focus-visible:ring-2 peer-focus-visible:ring-sky-400 peer-focus-visible:ring-offset-2 peer-focus-visible:ring-offset-slate-950">
          <div className="overflow-hidden rounded-xl border border-slate-700 bg-slate-950/60">
            <img
              src={previewUrl}
              alt={`${file.name} 미리보기`}
              className="mx-auto max-h-64 w-full object-contain"
            />
          </div>
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-800 bg-slate-950/40 px-3 py-2">
            <div className="min-w-0">
              <p className="truncate text-sm font-medium text-slate-100" title={file.name}>
                {file.name}
              </p>
              <p className="text-xs text-slate-400">
                {formatBytes(file.size)} ·{" "}
                {extensionOf(file.name).replace(".", "").toUpperCase() || "IMAGE"}
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <label
                htmlFor={inputId}
                className={`inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-slate-700 px-2.5 py-1.5 text-xs font-medium text-slate-200 transition hover:border-sky-500 hover:text-sky-300 ${
                  disabled ? "pointer-events-none opacity-50" : ""
                }`}
              >
                <RefreshCw aria-hidden="true" className="h-3.5 w-3.5" />
                교체
              </label>
              <button
                type="button"
                onClick={clear}
                disabled={disabled}
                className="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 px-2.5 py-1.5 text-xs font-medium text-slate-200 transition hover:border-rose-500 hover:text-rose-300 disabled:opacity-50"
              >
                <Trash2 aria-hidden="true" className="h-3.5 w-3.5" />
                삭제
              </button>
            </div>
          </div>
        </div>
      ) : (
        <label
          htmlFor={inputId}
          onDrop={handleDrop}
          onDragEnter={handleDragEnter}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          className={`flex cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed px-6 py-12 text-center transition peer-focus-visible:ring-2 peer-focus-visible:ring-sky-400 peer-focus-visible:ring-offset-2 peer-focus-visible:ring-offset-slate-950 ${zoneTone} ${
            disabled ? "pointer-events-none opacity-50" : ""
          }`}
        >
          <Upload
            aria-hidden="true"
            className={`h-7 w-7 ${dragging ? "text-sky-300" : "text-slate-500"}`}
          />
          <span className="text-sm font-medium text-slate-100">
            {dragging ? "여기에 놓으면 불러옵니다" : "이미지를 끌어다 놓거나 클릭해서 선택"}
          </span>
          <span className="text-xs text-slate-400">{prettyExtensions} · 최대 20MB</span>
        </label>
      )}

      <p role="alert" aria-live="polite" className="min-h-[1.25rem] pt-2 text-xs text-rose-300">
        {rejection ?? ""}
      </p>
    </section>
  );
}
