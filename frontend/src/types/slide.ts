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

/**
 * A single point on the same normalized 0..1000 grid, as the API returns it.
 *
 * Same object/tuple split as {@link BBox}, and for the same reason: the
 * backend's `Point` is a pydantic model, so a *response* always carries the
 * object form `{"x":120,"y":300}`, while `[x, y]` is an accepted *input* that
 * hand-edited JSON in the spec editor may legitimately use.
 */
export interface Point {
  x: number;
  y: number;
}

/** The positional input form the backend also accepts: `[x, y]`. */
export type PointTuple = [number, number];

/** Either accepted point form. Use {@link pointToTuple} to read one uniformly. */
export type PointLike = Point | PointTuple;

/** Reads either point form into `[x, y]`, with 0 for anything unreadable. */
export function pointToTuple(point: PointLike | null | undefined): PointTuple {
  if (Array.isArray(point)) {
    return [num(point[0]), num(point[1])];
  }
  if (point && typeof point === "object") {
    return [num(point.x), num(point.y)];
  }
  return [0, 0];
}

/* -------------------------------------------------------------------------- */
/* Elements                                                                   */
/* -------------------------------------------------------------------------- */

/**
 * The autoshape geometries the builder can draw.
 *
 * The four `arrow_*` kinds are BLOCK arrows - filled autoshapes with a bbox,
 * not connectors. A line between two boxes is a {@link ConnectorElement}.
 */
export type ShapeKind =
  | "rectangle"
  | "rounded_rectangle"
  | "circle"
  | "arrow_right"
  | "arrow_left"
  | "arrow_up"
  | "arrow_down";
export type Align = "left" | "center" | "right";

/** Straight or one-bend orthogonal, matching `MSO_CONNECTOR.STRAIGHT/.ELBOW`. */
export type ConnectorKind = "straight" | "elbow";

/** The two line dash presets the builder maps to `MSO_LINE_DASH_STYLE`. */
export type DashStyle = "solid" | "dashed";

/** The `type` discriminant values, which are also the `counts` keys. */
export type ElementType = "shape" | "table" | "text" | "connector" | "image";

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

/**
 * A diagram connector: a line between two points, with optional arrowheads.
 *
 * It deliberately does **not** extend `BaseElement`, mirroring the backend:
 * a connector has no bbox at all, its geometry is `start_point`/`end_point`.
 * Anything walking `SlideSpec.elements` therefore has to narrow on `type`
 * before reaching for `bbox` - which is why `BaseElement` is not on the union
 * as a common supertype.
 */
export interface ConnectorElement {
  type: "connector";
  kind?: ConnectorKind;
  start_point: PointLike;
  end_point: PointLike;
  /** Arrowhead at `start_point`. Defaults to false server-side. */
  arrow_start?: boolean;
  /** Arrowhead at `end_point`. Defaults to **true** server-side. */
  arrow_end?: boolean;
  dash_style?: DashStyle;
  color?: string | null;
  width_pt?: number | null;
}

/**
 * A region of the SOURCE image, cropped out and placed as a real picture.
 *
 * This is the one element that is not vector-native, and it exists for the
 * regions that genuinely cannot be one: a photo, a rendered chart, a logo. The
 * builder crops `bbox` out of the uploaded image, so a build with no source
 * image to crop from (`/api/preview`, `/api/build-from-json`) cannot render it
 * and says so in `warnings`.
 */
export interface ImageElement extends BaseElement {
  type: "image";
  /** What the analyzer saw there, e.g. "team photo". Model-supplied text. */
  description?: string | null;
}

/** Discriminated on `type`, matching the backend's `Element` union. */
export type SlideElement =
  | ShapeElement
  | TableElement
  | TextElement
  | ConnectorElement
  | ImageElement;

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

/**
 * The autoshape geometries the builder can emit, plus a catch-all.
 *
 * NOTE: the block-arrow `ShapeKind`s are NOT listed here, because the preview
 * payload does not name them - `MSO_SHAPE.RIGHT_ARROW` and friends come back
 * as `"other"`, which the renderer draws as the shape's bounding box. The
 * arrow is in the deck; only the preview shows it square.
 */
export type PreviewAutoShape = "rectangle" | "rounded_rectangle" | "oval" | "other";

/**
 * A native autoshape. A null colour means "none", not "black".
 *
 * `line_dash` matters for one shape in particular: the builder marks the bbox
 * of an `image` element it could not render with a DASHED grey rectangle, and
 * previewing that solid would show a box the deck does not contain.
 */
export interface PreviewShape extends PreviewBox {
  kind: "shape";
  autoshape: PreviewAutoShape;
  fill_color: string | null;
  line_color: string | null;
  line_width_pt: number | null;
  line_dash: DashStyle;
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

/** A point on the slide, in percent of slide width / height. */
export interface PreviewPoint {
  x: number;
  y: number;
}

/**
 * A connector, read back off the built deck's `MSO_SHAPE_TYPE.LINE` shape.
 *
 * Unlike every other preview item this carries NO {@link PreviewBox}: a
 * connector is two endpoints, and its `begin_x/begin_y/end_x/end_y` are what
 * the server reports (as percentages). Code that walks `shapes` must narrow on
 * `kind` before reading `left_pct`.
 *
 * `elbow` only says the deck used `bentConnector3`; the actual bend offsets are
 * the preset's, so the exact route is not in the payload.
 */
export interface PreviewConnector {
  kind: "connector";
  start_pct: PreviewPoint;
  end_pct: PreviewPoint;
  elbow: boolean;
  arrow_start: boolean;
  arrow_end: boolean;
  dash: DashStyle;
  /** null when the deck left the line colour to the theme. */
  color: string | null;
  /** null when the deck left the line width to the theme. */
  width_pt: number | null;
}

/**
 * A picture, read back off the built deck's `MSO_SHAPE_TYPE.PICTURE` shape.
 *
 * `data_uri` is the image bytes that are genuinely embedded in the .pptx
 * (`shape.image.blob`), downscaled and size-capped for transport - not the
 * source image, and not a re-crop. It is null when the server could not inline
 * the crop, and then it also adds a line to `warnings`; the picture itself is
 * still in the deck, so the renderer shows a labelled placeholder rather than
 * claiming the image is missing.
 */
export interface PreviewImage extends PreviewBox {
  kind: "image";
  data_uri: string | null;
  /** The analyzer's own words for the region. Model-supplied text. */
  description: string | null;
}

/**
 * A shape the backend could not describe.
 *
 * Pictures and connectors now have their own kinds, so this stays what it
 * always was: the degrade path, so an unexpected shape becomes a placeholder
 * instead of failing the conversion.
 */
export interface PreviewUnsupported extends PreviewBox {
  kind: "unsupported";
}

/** Discriminated on `kind`. */
export type PreviewShapeItem =
  | PreviewShape
  | PreviewText
  | PreviewTable
  | PreviewConnector
  | PreviewImage
  | PreviewUnsupported;

/** The whole preview. `shapes` is already in z-order, back to front. */
export interface SlidePreview {
  slide: PreviewSlide;
  background_color: string | null;
  shapes: PreviewShapeItem[];
}

/**
 * Per-type tallies of what the BUILT deck really holds.
 *
 * Read off the preview payload, which the backend derives from the built
 * `Presentation` - so this is a statement about the .pptx.
 *
 * `counts` in the response is a different number: it is derived from the
 * validated `SlideSpec`, i.e. what was REQUESTED. For every type but one the
 * two agree, because a validated element reaches the deck. An `image` element
 * does not: the builder legitimately skips it when there is no source image to
 * crop from (`/api/preview`, `/api/build-from-json`) or when the
 * anti-flattening rule refuses the crop, and in the second case not even a
 * placeholder outline is left. Showing the request as though it were the file
 * is the overclaim this helper exists to avoid.
 *
 * Returns null when there is no preview to read. A caller must then say whose
 * numbers it is showing rather than passing the request off as the file.
 */
export function renderedCounts(
  preview: SlidePreview | null | undefined,
): ElementCounts | null {
  const shapes = preview?.shapes;
  if (!Array.isArray(shapes)) return null;
  const counts: ElementCounts = { shape: 0, table: 0, text: 0, connector: 0, image: 0 };
  for (const item of shapes) {
    switch (item?.kind) {
      case "shape":
      case "table":
      case "text":
      case "connector":
      case "image":
        counts[item.kind] += 1;
        break;
      default:
        // `unsupported` is the degrade path and belongs to no element type;
        // counting it under one would be a guess.
        break;
    }
  }
  return counts;
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
