/**
 * The single place that talks to the backend.
 *
 * Requests go to a relative `/api` base so the browser makes same-origin calls
 * through the Vite dev proxy (see vite.config.ts). `VITE_API_BASE` can point it
 * straight at e.g. http://localhost:8000/api, which is what the backend's CORS
 * allow-list covers.
 */

import type {
  ApiError,
  ApiErrorCode,
  BuiltDeck,
  ConvertInput,
  ConvertResponse,
  ElementCounts,
  HealthResponse,
  PreviewResponse,
  RefineInput,
  RefineResponse,
} from "../types/slide";

const API_BASE: string = (import.meta.env.VITE_API_BASE ?? "/api").replace(/\/+$/, "");

export const PPTX_MIME =
  "application/vnd.openxmlformats-officedocument.presentationml.presentation";

/* -------------------------------------------------------------------------- */
/* Korean error copy                                                          */
/* -------------------------------------------------------------------------- */

interface ErrorCopy {
  /** Replaces the server's `message` outright. */
  message: string;
  /** Replaces the server's `hint` - unless `keepServerDetail` says otherwise. */
  hint: string;
  /**
   * Append the server's own `hint` under ours, separated by a blank line.
   *
   * Only for the codes whose hint carries specifics no Korean sentence written
   * here can know: which element was rejected and why, which field of the
   * request was missing, what the builder or the upstream model actually said.
   * For every other code the server's hint is an English restatement of the
   * line above it, and stacking the two made a Korean UI read as a
   * half-translated one.
   *
   * The server's `message` is never appended, for any code: `message` here has
   * already replaced it, so re-appending it printed the same sentence twice -
   * once in Korean, once in English (`invalid_spec` showed "Spec JSON이
   * 유효하지 않습니다." and then "The request body is not a usable SlideSpec.").
   */
  keepServerDetail?: true;
}

/**
 * Korean presentation text for the error codes the contract pins.
 *
 * The API's own payloads stay English - this is purely how the UI *presents*
 * them, keyed on the stable `code`. An unknown code falls back to the server's
 * own `message`/`hint`, so a new backend code still says something useful
 * instead of a generic shrug.
 */
const ERROR_COPY: Record<string, ErrorCopy> = {
  unsupported_extension: {
    message: "지원하지 않는 이미지 형식입니다.",
    hint: "JPG, JPEG, PNG, WEBP 파일만 변환할 수 있습니다. 파일을 변환해서 다시 올려주세요.",
  },
  empty_file: {
    message: "파일이 비어 있습니다.",
    hint: "0바이트 파일입니다. 이미지가 온전히 저장됐는지 확인한 뒤 다시 올려주세요.",
  },
  not_an_image: {
    message: "이미지로 읽을 수 없는 파일입니다.",
    hint: "확장자만 이미지이거나 파일이 손상됐을 수 있습니다. 이미지 편집기에서 열어 다시 저장해 보세요.",
  },
  file_too_large: {
    message: "파일 용량이 너무 큽니다.",
    hint: "20MB 이하로 줄여서 다시 시도해 주세요. 해상도를 낮추거나 JPG로 저장하면 크게 줄어듭니다.",
  },
  missing_api_key: {
    message: "API 키가 설정되지 않았습니다.",
    hint: "프로젝트 루트의 .env 파일에 LiteLLM 프록시 키를 넣고 백엔드를 다시 시작해 주세요.",
  },
  proxy_unreachable: {
    message: "Vision 모델 프록시에 연결할 수 없습니다.",
    hint: "LiteLLM 프록시(기본 http://localhost:4000)가 실행 중인지 확인한 뒤 다시 시도해 주세요.",
  },
  // One code, several causes: the model replied with something unusable, the
  // key was rejected, or - the one this copy used to get wrong - the configured
  // model does not exist on the proxy, in which case nothing replied at all.
  // Telling that user to try a sharper image asserted a cause that did not
  // happen and offered two remedies that cannot help, and since the UI no
  // longer has a model field, the only remaining remedy is VISION_MODEL. So the
  // message no longer claims the model answered, and the hint names the fix and
  // points at the server's own detail below it, which says which case it was.
  analysis_failed: {
    message: "Vision 모델 분석에 실패했습니다.",
    hint: "모델을 호출하지 못했거나, 돌아온 응답을 슬라이드 정보로 읽을 수 없었습니다. 아래 상세 내용을 확인해 주세요. 모델 이름이 잘못된 경우에는 .env의 VISION_MODEL을 프록시가 제공하는 이름으로 바꾼 뒤 백엔드를 다시 시작해 주세요. 이미지 문제로 보이면 더 선명한 이미지로 바꾸거나 추가 지시사항에 원하는 요소를 적어 다시 시도해 보세요.",
    keepServerDetail: true,
  },
  empty_note: {
    message: "완성할 메모가 비어 있습니다.",
    hint: "무엇을 바꾸고 싶은지 한 줄이라도 적어 주세요. 예: 표 글씨를 더 크게",
  },
  refine_failed: {
    message: "지시사항 문장을 완성하지 못했습니다.",
    hint: "모델이 쓸 수 있는 문장을 돌려주지 않았습니다. 적어 두신 메모는 그대로 남아 있으니, 조금 더 구체적으로 적은 뒤 다시 시도하거나 메모 그대로 변환해 주세요.",
    keepServerDetail: true,
  },
  build_failed: {
    message: "PPTX 파일을 만드는 중 오류가 발생했습니다.",
    hint: "Spec JSON 탭에서 좌표나 색상 값을 확인한 뒤 다시 시도해 주세요.",
    keepServerDetail: true,
  },
  invalid_spec: {
    message: "Spec JSON이 유효하지 않습니다.",
    hint: "아래 상세 내용에 어떤 요소가 왜 거부됐는지 나와 있습니다. 해당 부분을 고친 뒤 다시 시도해 주세요.",
    keepServerDetail: true,
  },
  unknown_download: {
    message: "다운로드 링크가 만료되었습니다.",
    hint: "서버에 보관된 변환 결과가 사라졌습니다. 이미지를 다시 변환하거나, Spec JSON 탭에서 바로 내려받아 주세요.",
  },
  internal_error: {
    message: "서버에서 예상치 못한 오류가 발생했습니다.",
    hint: "잠시 후 다시 시도해 주세요. 계속되면 백엔드 로그를 확인해 주세요.",
  },
  invalid_request: {
    message: "요청 형식이 올바르지 않습니다.",
    hint: "요청 본문이 이 엔드포인트의 형식과 맞지 않습니다. 어떤 항목이 문제인지 아래에 나와 있습니다.",
    keepServerDetail: true,
  },
  // Starlette's own failures, raised before any of our routes run. Their
  // `message` is an English string like "Not Found" and their `hint` is empty,
  // so without copy here the banner showed nothing but "Not Found" - the least
  // useful message the UI can render, and the one a misconfigured
  // `VITE_API_BASE` or dev proxy produces.
  not_found: {
    message: "요청한 주소를 찾을 수 없습니다.",
    hint: "백엔드 주소 설정(VITE_API_BASE)이나 개발 서버 프록시 설정에서 `/api` 경로가 빠졌는지 확인해 주세요.",
  },
  method_not_allowed: {
    message: "허용되지 않은 요청 방식입니다.",
    hint: "백엔드와 프런트엔드 버전이 서로 다를 수 있습니다. 백엔드를 다시 시작한 뒤 페이지를 새로고침해 주세요.",
  },
  bad_request: {
    message: "요청을 읽을 수 없습니다.",
    hint: "업로드가 중간에 끊겼을 수 있습니다. 파일을 다시 선택해 변환해 주세요.",
    keepServerDetail: true,
  },
};

/**
 * Turns an error envelope into the Korean copy the UI shows.
 *
 * Unknown codes pass the server's own text through untouched: a wrong
 * translation is worse than an English sentence that is at least accurate.
 *
 * A known code shows Korean only, with one exception: the codes flagged
 * `keepServerDetail` append the server's `hint`, because that is where the
 * specifics live (which element was rejected and why, which field was
 * missing). The server's `message` is never appended - `copy.message` has
 * already replaced it, so appending it printed the same sentence twice, in two
 * languages.
 */
export function localizeApiError(detail: ApiError): ApiError {
  const serverHint = detail.hint?.trim() ?? "";
  const copy = ERROR_COPY[detail.code];
  // `""` is what Starlette's envelopes carry; normalize it so the banner does
  // not render an empty hint block.
  if (!copy) return { ...detail, hint: serverHint || null };

  const extra =
    copy.keepServerDetail && serverHint && serverHint !== detail.message.trim()
      ? serverHint
      : "";

  return {
    code: detail.code,
    message: copy.message,
    hint: extra ? `${copy.hint}\n\n${extra}` : copy.hint,
  };
}

/* -------------------------------------------------------------------------- */
/* Typed error                                                                */
/* -------------------------------------------------------------------------- */

/**
 * A failed call, carrying the parsed `{detail:{code,message,hint}}` envelope.
 *
 * Every rejection from this module is one of these - including a transport
 * failure and an unreadable body - so callers can render `message`/`hint`
 * without type-testing what they caught.
 */
export class ApiRequestError extends Error implements ApiError {
  readonly code: ApiErrorCode;
  readonly hint: string | null;
  /** HTTP status, or 0 when the request never got a response. */
  readonly status: number;

  constructor(detail: ApiError, status: number) {
    super(detail.message);
    this.name = "ApiRequestError";
    this.code = detail.code;
    this.hint = detail.hint ?? null;
    this.status = status;
  }

  /**
   * The plain envelope payload, for state that should stay serializable.
   *
   * Localized on the way out: this is the single choke point every caller uses
   * to put an error into React state, so translating here means no call site
   * can forget to. `this.message` stays as the server wrote it, which is what
   * a stack trace or a console log wants.
   */
  toDetail(): ApiError {
    return localizeApiError({ code: this.code, message: this.message, hint: this.hint });
  }
}

export function isApiRequestError(value: unknown): value is ApiRequestError {
  return value instanceof ApiRequestError;
}

/** Narrows anything thrown (including `AbortError`) into a renderable error. */
export function toApiError(value: unknown): ApiRequestError {
  if (isApiRequestError(value)) return value;
  if (value instanceof DOMException && value.name === "AbortError") {
    return new ApiRequestError(
      { code: "aborted", message: "요청이 취소되었습니다.", hint: null },
      0,
    );
  }
  return new ApiRequestError(
    {
      code: "network_error",
      message: value instanceof Error ? value.message : "예상치 못한 오류가 발생했습니다.",
      hint: null,
    },
    0,
  );
}

/* -------------------------------------------------------------------------- */
/* Response plumbing                                                          */
/* -------------------------------------------------------------------------- */

const NETWORK_HINT =
  "백엔드가 응답하지 않았습니다. backend 디렉터리에서 " +
  // A Korean particle binds to the word before it with no space - including
  // after a closing backtick.
  "`uvicorn app.main:app --reload --port 8000`으로 실행한 뒤 다시 시도해 주세요.";

/** Wraps fetch so a transport failure arrives as an ApiRequestError too. */
async function request(path: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(`${API_BASE}${path}`, init);
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiRequestError(
      {
        code: "network_error",
        message: `API 서버(${API_BASE}${path})에 연결할 수 없습니다.`,
        hint: NETWORK_HINT,
      },
      0,
    );
  }
}

/**
 * Builds the typed error for a non-2xx response.
 *
 * Reads the body as text first and only then tries JSON, so a non-JSON body
 * (an HTML 502 page from a proxy, an empty body, a truncated stream) surfaces
 * as the real HTTP failure instead of a misleading JSON parse error thrown over
 * the top of it.
 */
async function errorFromResponse(res: Response): Promise<ApiRequestError> {
  let raw = "";
  try {
    raw = await res.text();
  } catch {
    raw = "";
  }

  const fallbackCode: ApiErrorCode = `http_${res.status}`;
  const trimmed = raw.trim();

  if (trimmed) {
    try {
      const parsed: unknown = JSON.parse(trimmed);
      const detail = readEnvelope(parsed, res.status);
      if (detail) return new ApiRequestError(detail, res.status);
    } catch {
      // Body is not JSON; fall through to the text fallback below.
    }
  }

  return new ApiRequestError(
    {
      code: fallbackCode,
      message:
        `서버가 ${res.status}` +
        (res.statusText ? ` ${res.statusText}` : "") +
        " 응답을 반환했습니다.",
      hint: snippet(trimmed),
    },
    res.status,
  );
}

/** Pulls `{detail:{...}}` out of a parsed body, tolerating FastAPI's variants. */
function readEnvelope(parsed: unknown, status: number): ApiError | null {
  if (!parsed || typeof parsed !== "object") return null;
  const detail = (parsed as { detail?: unknown }).detail;

  // The pinned envelope.
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const obj = detail as Record<string, unknown>;
    if (typeof obj.message === "string") {
      return {
        code: typeof obj.code === "string" ? obj.code : `http_${status}`,
        message: obj.message,
        hint: typeof obj.hint === "string" ? obj.hint : null,
      };
    }
  }

  // `HTTPException(detail="text")`.
  if (typeof detail === "string") {
    return { code: `http_${status}`, message: detail, hint: null };
  }

  // FastAPI request-validation errors (422): detail is a list of issues.
  if (Array.isArray(detail)) {
    const parts = detail.map((item) => {
      if (!item || typeof item !== "object") return String(item);
      const issue = item as { loc?: unknown; msg?: unknown };
      const where = Array.isArray(issue.loc) ? issue.loc.join(".") : "";
      const msg = typeof issue.msg === "string" ? issue.msg : "invalid";
      return where ? `${where}: ${msg}` : msg;
    });
    // One issue per line: the hint is rendered with `whitespace-pre-wrap`, and
    // `ERROR_COPY.invalid_request` supplies the sentence around it, so this
    // carries only the per-field detail. `message` is the fallback for a build
    // where that entry has been removed - normally it is replaced, not shown,
    // which is why it must not be a second phrasing of the same sentence.
    return {
      code: "invalid_request",
      message: "요청 형식이 올바르지 않습니다.",
      hint: parts.join("\n") || null,
    };
  }

  return null;
}

function snippet(text: string): string | null {
  if (!text) return null;
  const oneLine = text.replace(/\s+/g, " ").trim();
  if (!oneLine) return null;
  return oneLine.length > 240 ? `${oneLine.slice(0, 240)}...` : oneLine;
}

/** Parses a 2xx JSON body, reporting an unreadable one honestly. */
async function readJson<T>(res: Response): Promise<T> {
  const raw = await res.text();
  try {
    return JSON.parse(raw) as T;
  } catch {
    throw new ApiRequestError(
      {
        code: "bad_response",
        message: "서버 응답이 올바른 JSON이 아닙니다.",
        hint: snippet(raw),
      },
      res.status,
    );
  }
}

/* -------------------------------------------------------------------------- */
/* Endpoints                                                                  */
/* -------------------------------------------------------------------------- */

/** `GET /api/health`. Configuration only - it does not probe the proxy. */
export async function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  const res = await request("/health", {
    method: "GET",
    headers: { Accept: "application/json" },
    ...(signal ? { signal } : {}),
  });
  if (!res.ok) throw await errorFromResponse(res);
  return readJson<HealthResponse>(res);
}

/**
 * `POST /api/convert` (multipart/form-data).
 *
 * Optional fields are omitted when blank rather than sent empty, so the server
 * applies its own defaults.
 *
 * No `model` is sent, and {@link ConvertInput} has no field for one. The
 * endpoint's `model` field is optional and falls back to the server's
 * configured `DEFAULT_MODEL`, so omitting it is exactly what makes the
 * configured model authoritative now that the UI has no picker for it.
 */
export async function convert(
  input: ConvertInput,
  signal?: AbortSignal,
): Promise<ConvertResponse> {
  const body = new FormData();
  body.append("file", input.file, input.file.name);

  const extra = input.extra_instructions?.trim();
  if (extra) body.append("extra_instructions", extra);

  // No explicit Content-Type: the browser must set the multipart boundary.
  const res = await request("/convert", {
    method: "POST",
    headers: { Accept: "application/json" },
    body,
    ...(signal ? { signal } : {}),
  });
  if (!res.ok) throw await errorFromResponse(res);
  return readJson<ConvertResponse>(res);
}

/**
 * `POST /api/refine-instructions` (multipart/form-data).
 *
 * Turns a rough note into one instruction written for the slide analyzer, in
 * the note's own language. Context is optional and at most one is sent: the
 * spec of the conversion on screen when there is one (text-only and cheap),
 * otherwise the selected image. The server reports which it used in
 * `context_used` and whether the model completed anything at all in
 * `fell_back`, and those two together are what the UI describes - not what we
 * hoped it would use.
 *
 * The note is sent as typed, not trimmed: the server trims it itself to decide
 * whether it is blank, and echoes back what it received.
 *
 * This never resolves with a blank `instruction`. The caller drops the result
 * straight into the user's textarea, so a 200 carrying nothing usable is a
 * failure (`refine_failed`) and is raised as one - overwriting what the user
 * typed with an empty string would destroy the very text this feature exists
 * to improve.
 */
export async function refineInstructions(
  input: RefineInput,
  signal?: AbortSignal,
): Promise<RefineResponse> {
  const body = new FormData();
  body.append("note", input.note);

  // Only context, so a spec that cannot be serialized (a hand-edited buffer
  // holding something exotic) is left out rather than failing the request.
  if (input.spec !== undefined && input.spec !== null) {
    try {
      body.append("spec", JSON.stringify(input.spec));
    } catch {
      // Unserializable: fall through with no spec. The server then uses the
      // file, or tidies the note, and says so in `context_used`.
    }
  } else if (input.file) {
    body.append("file", input.file, input.file.name);
  }

  const res = await request("/refine-instructions", {
    method: "POST",
    headers: { Accept: "application/json" },
    body,
    ...(signal ? { signal } : {}),
  });
  if (!res.ok) throw await errorFromResponse(res);

  const data = await readJson<RefineResponse>(res);
  const instruction = typeof data.instruction === "string" ? data.instruction.trim() : "";
  if (!instruction) {
    throw new ApiRequestError(
      {
        code: "refine_failed",
        message: "서버가 완성된 지시사항을 돌려주지 않았습니다.",
        hint: null,
      },
      res.status,
    );
  }

  return {
    status: "success",
    instruction,
    note: typeof data.note === "string" ? data.note : input.note,
    // Passed through as sent. An unrecognized value is not mapped onto one of
    // the three - the UI has copy per known value and simply says nothing for
    // anything else, which is better than describing the wrong context.
    context_used: data.context_used,
    // Only a real `true` counts as a fallback. An older backend that does not
    // send this field must not make the UI announce one: the honest default is
    // "the model completed it", which is what `context_used` already describes.
    fell_back: data.fell_back === true,
  };
}

/**
 * `POST /api/preview` (application/json) -> the rendered preview, no file.
 *
 * The server builds the deck to read its real geometry back and throws the
 * bytes away, so this is the endpoint for "show me what my edit looks like"
 * while {@link buildFromJson} stays the one that hands over a file. `spec` is
 * passed through as-is for the same reason as there: the server reuses
 * `normalize_spec_payload`, so all three accepted shapes are legitimate.
 */
export async function previewSpec(
  spec: unknown,
  signal?: AbortSignal,
): Promise<PreviewResponse> {
  const res = await request("/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(spec),
    ...(signal ? { signal } : {}),
  });
  if (!res.ok) throw await errorFromResponse(res);
  return readJson<PreviewResponse>(res);
}

/**
 * `POST /api/build-from-json` -> the .pptx binary plus its sidecar headers.
 *
 * `spec` is passed through as-is: the server reuses `normalize_spec_payload`,
 * so a bare `SlideSpec`, a `{"spec": {...}}` wrapper and a bare element array
 * are all valid and it is not this layer's job to second-guess which one the
 * user typed.
 */
export async function buildFromJson(
  spec: unknown,
  filename = "slide.pptx",
  signal?: AbortSignal,
): Promise<BuiltDeck> {
  const name = ensurePptxName(filename);
  const query = `?filename=${encodeURIComponent(name)}`;

  const res = await request(`/build-from-json${query}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: PPTX_MIME },
    body: JSON.stringify(spec),
    ...(signal ? { signal } : {}),
  });
  if (!res.ok) throw await errorFromResponse(res);

  return {
    blob: await res.blob(),
    filename: filenameFromResponse(res) ?? name,
    counts: countsFromHeader(res.headers.get("X-Element-Counts")),
    droppedCount: intFromHeader(res.headers.get("X-Dropped-Count")),
  };
}

/** The URL of a finished conversion's deck: `GET /api/download/{file_id}`. */
export function downloadUrl(fileId: string): string {
  return `${API_BASE}/download/${encodeURIComponent(fileId)}`;
}

/**
 * Fetches a previously converted deck by id.
 *
 * Deliberately a fetch rather than a bare anchor to `downloadUrl`: a 404
 * `unknown_download` (expired TTL, evicted entry) has to reach the error banner
 * instead of navigating the tab to a JSON error page. Re-downloading the same
 * id works - a download does not consume the entry.
 */
export async function fetchDownload(
  fileId: string,
  fallbackName = "slide.pptx",
  signal?: AbortSignal,
): Promise<BuiltDeck> {
  const res = await request(`/download/${encodeURIComponent(fileId)}`, {
    method: "GET",
    headers: { Accept: PPTX_MIME },
    ...(signal ? { signal } : {}),
  });
  if (!res.ok) throw await errorFromResponse(res);

  return {
    blob: await res.blob(),
    filename: filenameFromResponse(res) ?? ensurePptxName(fallbackName),
    counts: countsFromHeader(res.headers.get("X-Element-Counts")),
    droppedCount: intFromHeader(res.headers.get("X-Dropped-Count")),
  };
}

/* -------------------------------------------------------------------------- */
/* Header helpers                                                             */
/* -------------------------------------------------------------------------- */

function countsFromHeader(value: string | null): ElementCounts | null {
  if (!value) return null;
  try {
    const parsed: unknown = JSON.parse(value);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    const src = parsed as Record<string, unknown>;
    const read = (key: string): number => {
      const raw = src[key];
      const n = typeof raw === "number" ? raw : Number(raw);
      return Number.isFinite(n) ? n : 0;
    };
    return { shape: read("shape"), table: read("table"), text: read("text") };
  } catch {
    return null;
  }
}

function intFromHeader(value: string | null): number | null {
  if (value === null || value.trim() === "") return null;
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) ? parsed : null;
}

/** Reads the filename out of `Content-Disposition`, if the server sent one. */
function filenameFromResponse(res: Response): string | null {
  const header = res.headers.get("Content-Disposition");
  if (!header) return null;

  const encoded = /filename\*=(?:UTF-8'')?([^;]+)/i.exec(header);
  if (encoded?.[1]) {
    try {
      return sanitizeName(decodeURIComponent(encoded[1].trim().replace(/^"|"$/g, "")));
    } catch {
      // Malformed percent-encoding: fall through to the plain form.
    }
  }

  const plain = /filename="?([^";]+)"?/i.exec(header);
  if (plain?.[1]) return sanitizeName(plain[1]);
  return null;
}

/** Strips any path parts a header might carry before it reaches `a.download`. */
function sanitizeName(name: string): string {
  const base = name.trim().split(/[\\/]/).pop() ?? "";
  return base || "slide.pptx";
}

function ensurePptxName(name: string): string {
  const base = sanitizeName(name).replace(/\.pptx$/i, "");
  const safe = base.trim() || "slide";
  return `${safe}.pptx`;
}

/* -------------------------------------------------------------------------- */
/* Saving a blob                                                              */
/* -------------------------------------------------------------------------- */

/**
 * Hands a fetched deck to the browser's downloader via an object URL.
 *
 * The URL is revoked once the download has been handed off. Revoking in the
 * same tick can cancel the save in some browsers, so it happens on a short
 * timer - the blob is already fully in memory, so nothing is still streaming.
 */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = ensurePptxName(filename);
    anchor.rel = "noopener";
    anchor.style.display = "none";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
  }
}

/** Fetch a converted deck by id and save it. */
export async function downloadResult(
  fileId: string,
  fallbackName?: string,
): Promise<BuiltDeck> {
  const deck = await fetchDownload(fileId, fallbackName);
  saveBlob(deck.blob, deck.filename);
  return deck;
}
