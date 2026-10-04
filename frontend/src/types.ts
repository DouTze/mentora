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

export interface Guide {
  chinese_text: string | null;
  guide: string;
  key_points: string[];
  check: string;
  latency_ms?: number;
  agent?: string;
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
  status: "active" | "completed";
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
}

export interface ModelConfig {
  backend: "mock" | "ollama" | "openvino";
  model: string;
  host: string;
  model_path: string;
  device: string;
  temperature: number;
  max_tokens: number;
  context_tokens: number;
}

export interface ActiveBackend {
  backend: string;
  model: string;
  simulated: boolean;
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
}

export interface MaterialDetail extends Material {
  units: string[];
}

export interface Library {
  categories: Category[];
  materials: Material[];
}
