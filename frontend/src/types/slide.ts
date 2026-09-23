/**
 * Hand-written mirrors of the pinned HTTP API contract.
 *
 * Every name here matches the backend wire format exactly (snake_case, no
 * renaming). These are deliberately NOT generated: the contract is pinned, so
 * a drift between this file and the server is a bug worth seeing in review.
 */

/* -------------------------------------------------------------------------- */
/* Geometry                                                                   */
/* -------------------------------------------------------------------------- */

/**
 * A bounding box on the normalized 0..1000 integer grid, as the API returns it.
 *
 * NOTE on shape: the backend's `BBox` is a pydantic model, so a *response*
 * always carries the object form `{"x":0,"y":0,"w":1000,"h":150}` - verified
 * against `SlideSpec.model_dump(mode="json")`. The 4-tuple form is an accepted
 * *input* (the backend's `BBox._coerce` takes `[x, y, w, h]` too), which is why
 * {@link BBoxTuple} and {@link BBoxLike} exist: hand-edited JSON in the spec
 * editor may legitimately use either, but nothing the server sends us does.
 */
export interface BBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** The positional input form the backend also accepts: `[x, y, w, h]`. */
export type BBoxTuple = [number, number, number, number];

/** Either accepted bbox form. Use {@link bboxToTuple} to read one uniformly. */
export type BBoxLike = BBox | BBoxTuple;

/** Reads either bbox form into `[x, y, w, h]`, with 0 for anything unreadable. */
export function bboxToTuple(bbox: BBoxLike | null | undefined): BBoxTuple {
  if (Array.isArray(bbox)) {
    return [num(bbox[0]), num(bbox[1]), num(bbox[2]), num(bbox[3])];
  }
  if (bbox && typeof bbox === "object") {
    return [num(bbox.x), num(bbox.y), num(bbox.w), num(bbox.h)];
  }
  return [0, 0, 0, 0];
}

function num(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

/* -------------------------------------------------------------------------- */
/* Elements                                                                   */
/* -------------------------------------------------------------------------- */

export type ShapeKind = "rectangle" | "rounded_rectangle" | "circle";
export type Align = "left" | "center" | "right";

/** The `type` discriminant values, which are also the `counts` keys. */
export type ElementType = "shape" | "table" | "text";

interface BaseElement {
  bbox: BBox;
}

/** A native autoshape. */
export interface ShapeElement extends BaseElement {
  type: "shape";
  kind?: ShapeKind;
  fill_color?: string | null;
  border_color?: string | null;
  border_width?: number | null;
}

/** A native table. `data` is row-major; row 0 is the header. */
export interface TableElement extends BaseElement {
  type: "table";
  data: string[][];
  header_bg_color?: string | null;
  header_font_bold?: boolean;
  font_size?: number | null;
}

/** A native text box. `content` may contain `\n` for multiple lines. */
export interface TextElement extends BaseElement {
  type: "text";
  content: string;
  font_size?: number | null;
  font_color?: string | null;
  bold?: boolean;
  align?: Align;
}

/** Discriminated on `type`, matching the backend's `Element` union. */
export type SlideElement = ShapeElement | TableElement | TextElement;

/* -------------------------------------------------------------------------- */
/* Spec                                                                       */
/* -------------------------------------------------------------------------- */

/**
 * A validated slide specification.
 *
 * There is intentionally no `dropped` field: it is tool-generated,
 * `exclude=True` on the server and stripped from any incoming payload, so it
 * never appears in `spec` and must never be sent back in one. Drop reasons
 * travel in {@link ConvertResponse.dropped} instead.
 */
export interface SlideSpec {
  background_color?: string | null;
  elements: SlideElement[];
}

/** Per-type element tallies. Keys are always present, zero when none. */
export type ElementCounts = Record<ElementType, number>;

/* -------------------------------------------------------------------------- */
/* Preview                                                                    */
/* -------------------------------------------------------------------------- */

/**
 * The rendered-slide preview, derived by the backend from the *built*
 * `Presentation` object rather than from the SlideSpec.
 *
 * That is the whole point of the payload: it reports what is genuinely in the
 * .pptx, so the min-dimension floor, off-slide clipping, cleaned text and
 * z-order are all already applied. Geometry arrives as percentages of the
 * slide (trivially responsive) and font sizes in points - and since the slide
 * is 960x540pt, an SVG with `viewBox="0 0 960 540"` maps one point to one user
 * unit, which is what {@link ../components/SlidePreview} relies on.
 */
export interface PreviewSlide {
  width_in: number;
  height_in: number;
  width_pt: number;
  height_pt: number;
}

/**
 * The slide-relative box every preview shape carries, in percent.
 *
 * NOT clamped: a shape may legitimately sit partly (or wholly) off-slide, and
 * clipping it is the renderer's job.
 */
interface PreviewBox {
  left_pct: number;
  top_pct: number;
  width_pct: number;
  height_pct: number;
}

/** The autoshape geometries the builder can emit, plus a catch-all. */
export type PreviewAutoShape = "rectangle" | "rounded_rectangle" | "oval" | "other";

/** A native autoshape. A null colour means "none", not "black". */
export interface PreviewShape extends PreviewBox {
  kind: "shape";
  autoshape: PreviewAutoShape;
  fill_color: string | null;
  line_color: string | null;
  line_width_pt: number | null;
}

/** One paragraph of a text box. `font_size_pt` is never null (see the docs). */
export interface PreviewParagraph {
  text: string;
  font_size_pt: number;
  font_color: string | null;
  bold: boolean;
  align: Align;
}

/** A native text box, already split into paragraphs. */
export interface PreviewText extends PreviewBox {
  kind: "text";
  align: Align;
  paragraphs: PreviewParagraph[];
}

/** One table cell. */
export interface PreviewTableCell {
  text: string;
  fill_color: string | null;
  font_size_pt: number;
  font_color: string | null;
  bold: boolean;
  align: Align;
}

/**
 * A native table.
 *
 * `col_widths_pct` / `row_heights_pct` are percentages of the TABLE's own box
 * (each summing to ~100), not of the slide.
 */
export interface PreviewTable extends PreviewBox {
  kind: "table";
  col_widths_pct: number[];
  row_heights_pct: number[];
  rows: PreviewTableCell[][];
}

/**
 * A shape the backend could not describe.
 *
 * The builder never emits pictures, so in practice this never appears; it
 * exists so an unexpected shape degrades into a placeholder instead of
 * failing the conversion.
 */
export interface PreviewUnsupported extends PreviewBox {
  kind: "unsupported";
}

/** Discriminated on `kind`. */
export type PreviewShapeItem =
  | PreviewShape
  | PreviewText
  | PreviewTable
  | PreviewUnsupported;

/** The whole preview. `shapes` is already in z-order, back to front. */
export interface SlidePreview {
  slide: PreviewSlide;
  background_color: string | null;
  shapes: PreviewShapeItem[];
}

/* -------------------------------------------------------------------------- */
/* Responses                                                                  */
/* -------------------------------------------------------------------------- */

/** `POST /api/convert` -> 200 */
export interface ConvertResponse {
  status: "success";
  counts: ElementCounts;
  total: number;
  /** Per-element drop reasons. Always an array; empty when nothing dropped. */
  dropped: string[];
  /** Render warnings (clipping, min-dimension floor, ...). Always an array. */
  warnings: string[];
  spec: SlideSpec;
  /**
   * The rendered preview of the built deck, or null when deriving it failed.
   *
   * Deriving the preview never fails a conversion: a failure leaves this null
   * and adds a line to {@link warnings}, so the deck is still downloadable.
   */
  preview: SlidePreview | null;
  /** uuid4 hex, for `GET /api/download/{file_id}`. */
  download_id: string;
  filename: string;
}

/**
 * `POST /api/preview` -> 200.
 *
 * The convert response WITHOUT `download_id` / `filename`: the endpoint builds
 * the deck only to read its real geometry back, then throws the bytes away, so
 * there is nothing stored to download. The deck itself comes from
 * `POST /api/build-from-json`.
 */
export interface PreviewResponse {
  status: "success";
  counts: ElementCounts;
  total: number;
  dropped: string[];
  warnings: string[];
  spec: SlideSpec;
  preview: SlidePreview | null;
}

/**
 * Which context the instruction assist actually had to work with.
 *
 * `"spec"` is the summarized SlideSpec of the last conversion, `"image"` the
 * source image when no conversion has happened yet, `"none"` neither - the note
 * was only tidied. The UI reports this verbatim rather than guessing from what
 * it sent, so it cannot claim the slide was taken into account when it was not.
 */
export type RefineContext = "spec" | "image" | "none";

/** `POST /api/refine-instructions` -> 200 */
export interface RefineResponse {
  status: "success";
  /** The completed instruction, ready to drop into `extra_instructions`. */
  instruction: string;
  /** The note exactly as it was received. */
  note: string;
  context_used: RefineContext;
  /**
   * The model returned nothing usable, so `instruction` is the user's own note,
   * tidied - not a completion.
   *
   * It sits beside `context_used` because that field alone cannot tell this
   * apart from a genuine no-context run: both answer `"none"`, and the two need
   * different sentences on screen. A fallback happens *after* a digest has been
   * built and sent, so blaming a missing context for it would state a reason
   * that did not happen.
   */
  fell_back: boolean;
}

/**
 * What the last completed assist did, as the panel has to describe it.
 *
 * Three independent facts, because one sentence has to be true about all of
 * them: which context the SERVER reported using, whether the model actually
 * completed anything, and whether the text in the box really changed (a
 * fallback that only collapsed whitespace did change it; one that returned the
 * note verbatim did not, and then there is nothing for 되돌리기 to restore).
 */
export interface RefineOutcome {
  context: RefineContext;
  fellBack: boolean;
  changed: boolean;
}

/** `GET /api/health` -> 200. Reports configuration only; it never fails. */
export interface HealthResponse {
  status: string;
  proxy: {
    base_url: string;
    model: string;
    /** A bool. It never echoes the key itself, nor any prefix of it. */
    api_key_configured: boolean;
  };
  supported_extensions: string[];
  slide: {
    width_in: number;
    height_in: number;
    coord_scale: number;
  };
}

/* -------------------------------------------------------------------------- */
/* Errors                                                                     */
/* -------------------------------------------------------------------------- */

/** The error codes the contract pins, plus client-side additions. */
export type ApiErrorCode =
  | "unsupported_extension"
  | "empty_file"
  | "not_an_image"
  | "file_too_large"
  | "missing_api_key"
  | "proxy_unreachable"
  | "analysis_failed"
  | "build_failed"
  /** `POST /api/refine-instructions`: the note was blank after trimming. */
  | "empty_note"
  /** `POST /api/refine-instructions`: the model gave nothing usable back. */
  | "refine_failed"
  | "invalid_spec"
  | "unknown_download"
  | "internal_error"
  /** `RequestValidationError` - the body did not match the endpoint's shape. */
  | "invalid_request"
  /**
   * Raised by the app-wide `HTTPException` handler for a failure Starlette
   * itself produced, i.e. before any route of ours ran: a wrong URL or a wrong
   * method (a stale `VITE_API_BASE` or dev-proxy target), or a body the
   * multipart parser could not read. Their `message` is Starlette's English
   * string and their `hint` is empty, so the UI has to supply all the copy.
   */
  | "not_found"
  | "method_not_allowed"
  | "bad_request"
  /** Raised client-side when the request never reached the server. */
  | "network_error"
  /** Raised client-side when the caller aborted the request. */
  | "aborted"
  /** Raised client-side for a response we could not interpret. */
  | "bad_response"
  | (string & {});

/** The `detail` payload of the single error envelope every endpoint uses. */
export interface ApiError {
  code: ApiErrorCode;
  message: string;
  hint?: string | null;
}

/** The envelope itself: `{"detail": {"code": ..., "message": ..., "hint": ...}}`. */
export interface ApiErrorEnvelope {
  detail: ApiError;
}

/* -------------------------------------------------------------------------- */
/* Request inputs                                                             */
/* -------------------------------------------------------------------------- */

/**
 * Multipart fields for `POST /api/convert`.
 *
 * There is deliberately **no `model`** here.
 *
 * The endpoint still accepts a `model` field - API clients and the CLI's `-m`
 * need it - but this UI has no picker and never sends one, so the model in use
 * is whatever the server is configured with (`VISION_MODEL`), reported in the
 * header status pill. Carrying an optional `model` on this type anyway would
 * make it a knob that type-checks, reads as supported, and silently does
 * nothing; leaving it out means the compiler rejects the mistake at the call
 * site instead. Restore it only together with a `body.append("model", ...)` in
 * {@link ../api/client.convert}.
 */
export interface ConvertInput {
  file: File;
  extra_instructions?: string;
}

/**
 * Multipart fields for `POST /api/refine-instructions`.
 *
 * `spec` and `file` are both context, and the server prefers `spec` when it is
 * usable, so the caller sends whichever one it has - the spec of the last
 * conversion, or the selected image before there is one. Neither is required:
 * with no context the note is only tidied.
 *
 * `spec` is `unknown` rather than {@link SlideSpec} for the same reason the
 * spec editor passes its buffer through untouched - what is on screen may be
 * hand-edited JSON - and because the server parses it leniently and ignores
 * anything it cannot read.
 */
export interface RefineInput {
  /** The user's rough note, in any language. Required. */
  note: string;
  /** The spec the current preview was built from, when there is one. */
  spec?: unknown;
  /** The source image, used when no conversion has happened yet. */
  file?: File | null;
}

/** What `buildFromJson` hands back: the deck plus its sidecar headers. */
export interface BuiltDeck {
  blob: Blob;
  filename: string;
  /** From `X-Element-Counts`; null when the header was absent or unparseable. */
  counts: ElementCounts | null;
  /** From `X-Dropped-Count`; null when the header was absent or unparseable. */
  droppedCount: number | null;
}
