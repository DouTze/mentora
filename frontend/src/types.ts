export type Assessment = "correct" | "partial" | "incorrect" | "unclear";
export type Action = "advance" | "probe" | "hint" | "explain" | "pass";

export interface TutorMessage {
  action: Action;
  feedback: string;
  hint: string | null;
  probe: string | null;
  explanation: string | null;
  examples: string[];
  source_ids: string[];
}

export interface QuestionView {
  id: string;
  concept_id: string;
  concept_title: string;
  type: "diagnostic" | "transfer";
  prompt: string;
  hints_used: number;
  hints_total: number;
}

export interface Turn {
  idx: number;
  created_at: string;
  concept_id: string;
  question_id: string;
  phase: string;
  student_answer: string;
  model_raw?: string | null;
  judgement?: { assessment: Assessment; reasoning_ok: boolean; evidence: string; source_ids: string[] } | null;
  validation_notes?: string[] | null;
  decision?: { rule: string; action: Action } | null;
  tutor_message?: TutorMessage | null;
  latency_ms?: number | null;
  usage?: Record<string, unknown> | null;
  error?: string | null;
  backend?: string;
  model?: string;
}

export interface ConceptState {
  status: "not_started" | "in_progress" | "passed" | "passed_with_help" | "needs_review";
}

export interface Summary {
  practiced: { concept_id: string; title: string; status: string }[];
  stuck_at: { question_id: string; concept_id: string; prompt: string }[];
  review_next: { concept_id: string; title: string; explanation: string }[];
  turn_count: number;
  error_count: number;
  verdict: "passed" | "needs_review";
}

/** One 來源單元 of the chosen range: a paragraph-level thought, taught on its own before the quiz. */
export interface Quote {
  id: string;
  index: number;
  label: string;
  heading: string | null;
  excerpt: string;
  text: string;
  chars: number;
  part: number;
  /** LangGraph sessions: stable source-unit id and the source blocks it was rebuilt from */
  unit_id?: string;
  block_ids?: string[];
}

/** what a published guide tells the reader it could not settle: a passage whose wording could not be confirmed
 *  (older guides), or a note of the handout review (a point it may not teach, a possible mismatch …) */
export type GuideNote = { kind: "coverage"; source: string; guide: string } | { kind: "note"; text: string; code?: string };

export interface Guide {
  chinese_text?: string | null;
  guide: string;
  key_points: string[];
  check: string;
  latency_ms?: number;
  agent?: string;
  unverified?: GuideNote[];
  /** a 主題 written one 區塊 at a time: shown as source 區塊 and guide 區塊 alternately */
  mode?: "blocks";
  topic_summary?: string;
  blocks?: GuideBlock[];
  /** some 區塊 did not pass their checks: shown as source only, and can be written again */
  partial?: boolean;
}

export interface GuideBlock {
  index: number;
  source: string;
  heading?: string | null;
  status: "accepted" | "failed";
  guide: string;
  /** attempts this 區塊 has used (failed ones are retried automatically up to a limit) */
  attempt?: number;
  chinese_text?: string | null;
  unverified?: GuideNote[];
}

export interface ReadingQA {
  question: string;
  answer: string;
  latency_ms?: number;
}

/** How the quotes were decided: by the model (WaAi-ML source-unit rules) or by the rule-based fallback. */
export interface Segmentation {
  planner: "model" | "rules";
  candidates: number;
  notes: string[];
  status?: "pending" | "done";
  /** 區塊 left out as needing no guide (contents, a summary repeating the text, matter about the material / course / exam) */
  skipped?: { role: "toc" | "summary" | "meta" | "exercise"; label: string; topic?: string; text: string }[];
}

export interface ReadingView {
  index: number;
  total: number;
  done: boolean;
  quotes: Quote[];
  parts: { index: number; label: string; quote_from: number; quote_to: number; chars: number }[];
  guides: Record<string, Guide>;
  qa: Record<string, ReadingQA[]>;
  compacts: Record<string, { summary: string; key_terms: string[]; claims: string[]; markdown: string }>;
  quiz_source: "full" | "compact" | null;
  segmentation?: Segmentation | null;
  stopped_at?: number | null;
  /** background work on the server: poll the session until it is null */
  job?: ReadingJob | null;
}

export type JobStage = "segment" | "guide" | "compact" | "generate";
/** finer stages of LangGraph sessions (analysis, review, …); unknown ones fall back to `stage` */
export type DetailStage = JobStage | "analyze" | "context" | "draft" | "review" | "revise" | "ask" | "assess";
export interface ReadingJob {
  stage: JobStage;
  detail_stage?: DetailStage | string | null;
  error: ApiError | null;
}

/** One accepted operation of a LangGraph session (POST /events, GET /commands/{id}). */
export interface CommandView {
  command_id: string;
  type: "start" | "guide" | "ask" | "next" | "finish" | "answer" | "retry";
  status: "queued" | "running" | "completed" | "failed";
  stage: string | null;
  result: { answer?: ReadingQA; message?: TutorMessage; [k: string]: unknown } | null;
  error: ApiError | null;
  retryable: boolean;
  created_at: string;
  updated_at: string;
}

export interface WorkflowView {
  waiting: "student" | "retry" | "answer" | "completed" | null;
  stage: string | null;
  active_command: CommandView | null;
  error: ApiError | null;
  failed_command_id: string | null;
  last_failed: string | null;
  failed_node: string | null;
  runner_active: boolean;
  workflow_version?: string | null;
  reading_workflow_version?: string | null;   // reading-graph-v1 (older sessions) | reading-graph-v2
  prompt_bundle_hash?: string | null;
  model_config_hash?: string | null;
  task_contract?: { depth: "close" | "skim"; goal: string; reader_background: string; defaults: string[] } | null;
  migrations?: { from: string | null; to: string; at: string; note: string }[];
}

export interface SessionView {
  id: string;
  /** closed: its material was deleted; kept as a record, takes no more commands */
  status: "active" | "completed" | "closed";
  created_at: string;
  updated_at: string;
  content_id: string;
  content_version: string;
  content_title: string;
  strategy_id: string;
  strategy_version: string;
  source: Source | null;
  reading: ReadingView | null;
  questions: { id: string; concept_id: string; type: "diagnostic" | "transfer"; prompt: string }[];
  concepts: { id: string; title: string; explanation: string; examples: string[] }[];
  model_config_snapshot: ModelConfig & { resolved_model?: string; simulated?: boolean };
  state: { concept_order: string[]; concept_index: number; phase: string; concepts: Record<string, ConceptState> };
  current_question: QuestionView | null;
  turns: Turn[];
  summary: Summary | null;
  /** "langgraph" sessions take every action through POST /events; older ones keep the original endpoints */
  workflow_runner?: "legacy" | "langgraph";
  revision?: number;
  workflow?: WorkflowView | null;
}

export interface SessionListItem {
  id: string;
  status: string;
  created_at: string;
  updated_at: string;
  content_title: string;
  content_version: string;
  strategy_id: string;
  source: Omit<Source, "text"> | null;
  backend: string;
  simulated: boolean;
  verdict: string | null;
  /** reading quote `read` of `total`, or past the reading and answering questions */
  progress: { stage: "reading" | "quiz"; read: number; total: number };
  /** the companion's whole-material study (導師全文學習): used by the reader, left out of the lists */
  study?: boolean;
}

export interface ModelConfig {
  backend: "mock" | "ollama" | "remote" | "openvino";
  model: string;
  host: string;
  model_path: string;
  device: string;
  temperature: number;
  max_tokens: number;
  context_tokens: number;
  /** requests the model server may run at once; above 1 the coming units' sources are verified ahead */
  parallel?: number;
}

/** The remote model server saved on the backend; the token itself never comes back. */
export interface RemoteEndpoint {
  url: string;
  has_token: boolean;
}

export interface ActiveBackend {
  backend: string;
  model: string;
  simulated: boolean;
  /** Ollama / remote: the server's address */
  host?: string;
}

export interface ApiError {
  code: string;
  detail: string;
  retryable?: boolean;
  turn?: Turn | null;
}

export interface Content {
  id: string;
  version: string;
  title: string;
  description: string;
  concepts: { id: string; title: string; explanation: string; examples: string[] }[];
  questions: { id: string; concept_id: string; type: string; prompt: string }[];
}

export interface Strategy {
  id: string;
  version: string;
  title: string;
  description: string;
  rules: { when: string; then: string; note: string }[];
}

/** Where generated questions came from: a range of an imported material (1-based, inclusive). */
export interface Source {
  material_id: string;
  material_title: string;
  unit: "page" | "line";
  start: number;
  end: number;
  label: string;
  text: string;
}

/** A page (a line, in a text) the reader marked, and the chapter it falls in as the reader named it. */
export interface Bookmark {
  unit_no: number;
  chapter: string | null;
  /** a name the person gave it, shown in the sidebar in place of the chapter */
  label?: string | null;
  created_at: string;
}
export type Bookmarks = Record<string, Bookmark[]>;

export interface Category {
  id: string;
  name: string;
  position: number;
}

export interface Material {
  id: string;
  category_id: string | null;
  title: string;
  filename: string;
  kind: "pdf" | "md" | "txt";
  unit: "page" | "line";
  unit_count: number;
  created_at: string;
  /** a short name the connected model gave the book (null until it has) */
  alias?: string | null;
  /** "extracting": a PDF just imported whose text is still being read in the background (it opens and shows its
   * pages; questions and 導讀 wait); "failed": its text could not be read (`status_detail` says why) */
  status?: "ready" | "extracting" | "failed";
  status_detail?: string | null;
  /** guides, questions and quizzes in 繁體中文; otherwise in the material's own language */
  translate_zh: boolean;
}

/** The book's copy in the person's book folder (~/.mentora) and what it stands for (backend app/library.py). */
export interface LibraryFile {
  stored_name: string;
  original_name: string;
  /** where the file came from; null for a browser upload, which gives only the name */
  original_path: string | null;
  sha256: string;
  size: number;
  alias: string | null;
  description: string | null;
  described_by: string | null;
  described_at: string | null;
}

export interface MaterialDetail extends Material {
  units: string[];
  library?: LibraryFile | null;
}

/** Rectangles of a PDF page in view space: points, origin at the top-left of the page as shown, y down. */
export type PdfRect = [number, number, number, number];

/** A bookmark of the PDF's outline; `page` is 1-based, `y` where on that page it points (view space). */
export interface PdfOutlineItem {
  title: string;
  page: number | null;
  y?: number;
  children: PdfOutlineItem[];
}

/** What a PDF holds besides its pages (backend app/pdfdoc/inspect.py). */
export interface PdfInfo {
  title: string | null;
  author: string | null;
  subject: string | null;
  keywords: string | null;
  creator: string | null;
  producer: string | null;
  created: string | null;
  modified: string | null;
  pdf_version: string | null;
  encrypted: boolean;
  tagged: boolean;
  has_javascript: boolean;
  page_count: number;
  pages: [number, number][];
  /** the document's own page labels (i, ii, 1, 2…); null when they are just the page numbers */
  labels: string[] | null;
  outline: PdfOutlineItem[];
  attachments: { name: string; description: string | null; size: number | null }[];
  fields: { name: string; kind: string; value: string | null; page: number }[];
  layers: { name: string; visible: boolean }[];
  fonts: { name: string; kind: string; embedded: boolean; subset: boolean }[];
  counts: { links: number; images: number; annotations: number; attachments: number; fields: number };
  warnings: string[];
}

/** A link on a page: to a page of the same file, a web address, or something the reader does not follow. */
export type PdfLink = { rect: PdfRect } & (
  | { kind: "page"; page: number; y?: number }
  | { kind: "uri"; uri: string }
  | { kind: "file"; file: string; page: number }
  | { kind: "launch"; file: string }
  | { kind: "other"; name: string }
);

/** One page laid over its image: text runs in reading order, links, images and comments (app/pdfdoc/layer.py). */
export interface PdfPageLayer {
  page: number;
  label: string;
  size: [number, number];
  /** the text runs this many quarter turns clockwise from left-to-right (1: downwards) */
  quarter: number;
  /** a run with `math` holds that formula's LaTeX ($…$ / $$…$$) in place of the glyphs it covers */
  lines: { block: number; runs: { t: string; rect: PdfRect; sp?: boolean; math?: number }[] }[];
  /** formulas read from the glyphs and their layout (backend app/pdfdoc/math.py) */
  math?: { rect: PdfRect; latex: string; display: boolean; tag: string | null }[];
  links: PdfLink[];
  images: { rect: PdfRect; xref: number; width: number | null; height: number | null }[];
  annotations: { index: number; type: string; rect: PdfRect; quads: PdfRect[]; content: string | null; author: string | null; color: string | null }[];
}

/** 導師全文學習: the whole material read once through the learning flow; its parts locate the reader's page. */
export interface StudyView {
  /** off: not started (or cancelled) — the companion reads only while the book is open */
  status: "reading" | "ready" | "failed" | "off";
  session_id: string | null;
  /** waiting: paused by the person (`by_user`) or because another book is being read */
  paused?: boolean;
  by_user?: boolean;
  planner: string | null;
  /** 0–1, share of the text segmented so far */
  progress: number;
  /** where the segmentation is: preparing the text, the outline pass (exact count), the topic pass or the buffers
   * (counts estimated: `approx`) */
  step: { phase: "extract" | "outline" | "topic" | "window"; current?: number; total?: number | null; approx?: boolean } | null;
  error: string | null;
  /** first and last page (line) of every reading part */
  parts: { index: number; from: number; to: number; heading: string | null }[];
}

/** A question typed in the reader, answered from the units on that page (and their guide when one exists). */
export interface ReaderQuestion {
  id: string;
  unit_no: number;
  question: string;
  answer: string;
  label: string | null;
  unit_ids: string[];
  used_guide: boolean;
  created_at: string;
}

/** A readable file the shelf scout found on this computer (not imported yet). */
export interface ShelfBook {
  id: string;
  title: string;
  author: string;
  kind: "pdf" | "md" | "txt";
  pages: number | null;
  /** a serial: number of chapter files read as one book */
  parts: number | null;
  folder: string;
  /** cover width / height (PDF first page); text files get a drawn cover */
  aspect: number;
  /** iCloud / OneDrive placeholder: no cover is rendered (that would download it) */
  cloud: boolean;
  reason: string;
  tendency: string | null;
}

export interface ShelfView {
  status: "preparing" | "ready";
  query: string | null;
  tendencies: string[];
  books: ShelfBook[];
  interests: string[];
  simulated: boolean;
  /** who chose this shelf: the model, or keywords and the topic lexicon (no model, or the model failed) */
  mode: "model" | "keywords";
  indexed: number;
  /** false when no folder may be searched */
  scoped: boolean;
}

export interface ShelfScope {
  /** `library`: the Mentora library folder (~/.mentora), where every import is copied */
  roots: { path: string; label: string; enabled: boolean; cloud: boolean; exists: boolean; library: boolean }[];
  exclude: { path: string; label: string }[];
}

export interface Library {
  categories: Category[];
  materials: Material[];
}
