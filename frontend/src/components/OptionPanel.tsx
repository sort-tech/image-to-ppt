import { useId } from "react";
import { KeyRound, Settings2 } from "lucide-react";

interface OptionPanelProps {
  model: string;
  onModelChange: (value: string) => void;
  extraInstructions: string;
  onExtraInstructionsChange: (value: string) => void;
  /** Shown as the model placeholder - the server's default, from /api/health. */
  modelPlaceholder: string;
  /** True when the backend reports no API key: a conversion cannot succeed. */
  keyMissing?: boolean;
  disabled?: boolean;
}

/**
 * Conversion options: model override and extra instructions.
 *
 * Both text fields send nothing when left blank, so the server keeps its own
 * defaults rather than receiving an empty string.
 */
export default function OptionPanel({
  model,
  onModelChange,
  extraInstructions,
  onExtraInstructionsChange,
  modelPlaceholder,
  keyMissing = false,
  disabled = false,
}: OptionPanelProps) {
  const baseId = useId();
  const modelId = `${baseId}-model`;
  const extraId = `${baseId}-extra`;

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
          <label htmlFor={modelId} className="mb-1.5 block text-xs font-medium text-slate-300">
            Vision 모델
          </label>
          <input
            id={modelId}
            type="text"
            value={model}
            onChange={(event) => onModelChange(event.target.value)}
            placeholder={modelPlaceholder}
            disabled={disabled}
            autoComplete="off"
            spellCheck={false}
            className="w-full rounded-lg border border-slate-700 bg-slate-950/60 px-3 py-2 font-mono text-sm text-slate-100 placeholder:text-slate-500 transition focus:border-sky-500 disabled:opacity-50"
          />
          <p className="mt-1 text-xs text-slate-500">
            비워 두면 프록시의 기본 모델을 사용합니다.
          </p>
        </div>

        <div>
          <label htmlFor={extraId} className="mb-1.5 block text-xs font-medium text-slate-300">
            추가 지시사항 <span className="font-normal text-slate-500">(선택)</span>
          </label>
          <textarea
            id={extraId}
            value={extraInstructions}
            onChange={(event) => onExtraInstructionsChange(event.target.value)}
            placeholder="예: 표는 그대로 살리고 하단 로고는 무시해 주세요. 글꼴은 좀 더 크게."
            rows={3}
            disabled={disabled}
            className="w-full resize-y rounded-lg border border-slate-700 bg-slate-950/60 px-3 py-2 text-sm leading-relaxed text-slate-100 placeholder:text-slate-500 transition focus:border-sky-500 disabled:opacity-50 scrollbar-slim"
          />
          <p className="mt-1 text-xs text-slate-500">
            어떤 요소를 살리고 무엇을 무시할지 모델에게 한국어로 적어 주면 됩니다.
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
