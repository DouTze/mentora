import type { CompanionSettings } from "./companions";
import type { ActiveBackend, ApiError, CommandView, Category, Content, Guide, Library, Material, MaterialDetail, ModelConfig, ReadingQA, SessionListItem, SessionView, Strategy, TutorMessage, Turn } from "./types";

export class RequestError extends Error {
  constructor(public status: number, public body: ApiError & { session?: SessionView | null }) {
    super(body.detail);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    // FormData sets its own multipart content-type (with boundary)
    const headers = init?.body instanceof FormData ? undefined : { "content-type": "application/json" };
    res = await fetch(`/api${path}`, { headers, ...init });
  } catch (e) {
    throw new RequestError(0, { code: "network", detail: `無法連線後端：${e}`, retryable: true });
  }
  if (!res.ok) {
    let body: ApiError = { code: "http_" + res.status, detail: res.statusText, retryable: false };
    try {
      body = await res.json();
    } catch {}
    throw new RequestError(res.status, body);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => call<{ ok: boolean; backend: ActiveBackend }>("/health"),
  contents: () => call<Pick<Content, "id" | "version" | "title" | "description">[]>("/content"),
  content: (id: string) => call<Content>(`/content/${id}`),
  strategies: () => call<Strategy[]>("/strategies"),
  settings: () => call<{ config: ModelConfig; active: ActiveBackend }>("/settings"),
  saveSettings: (cfg: ModelConfig) =>
    call<{ config: ModelConfig; active: ActiveBackend }>("/settings", { method: "PUT", body: JSON.stringify(cfg) }),
  companions: () => call<CompanionSettings | null>("/companions"),
  saveCompanions: (body: CompanionSettings) =>
    call<{ companions: CompanionSettings; config: ModelConfig; active: ActiveBackend }>("/companions", { method: "PUT", body: JSON.stringify(body) }),
  checkSettings: () => call<{ active: ActiveBackend; health: { ok: boolean; detail: string; [k: string]: unknown } }>("/settings/check", { method: "POST" }),
  startSession: (content_id: string, strategy_id: string) =>
    call<SessionView>("/sessions", { method: "POST", body: JSON.stringify({ content_id, strategy_id }) }),
  startFromMaterial: (material_id: string, start: number, end: number) =>
    call<SessionView>("/sessions", { method: "POST", body: JSON.stringify({ material_id, start, end, strategy_id: "rules-v1" }) }),
  library: () => call<Library>("/library"),
  createCategory: (name: string) => call<Category>("/categories", { method: "POST", body: JSON.stringify({ name }) }),
  renameCategory: (id: string, name: string) => call<Category>(`/categories/${id}`, { method: "PATCH", body: JSON.stringify({ name }) }),
  deleteCategory: (id: string) => call<{ ok: boolean }>(`/categories/${id}`, { method: "DELETE" }),
  importMaterial: (file: File, categoryId: string | null) => {
    const form = new FormData();
    form.append("file", file);
    if (categoryId) form.append("category_id", categoryId);
    return call<Material>("/materials", { method: "POST", body: form });
  },
  material: (id: string) => call<MaterialDetail>(`/materials/${id}`),
  updateMaterial: (id: string, patch: { title?: string; category_id?: string | null }) =>
    call<Material>(`/materials/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  deleteMaterial: (id: string) => call<{ ok: boolean }>(`/materials/${id}`, { method: "DELETE" }),
  materialFileUrl: (id: string) => `/api/materials/${id}/file`,
  sessions: () => call<SessionListItem[]>("/sessions"),
  session: (id: string) => call<SessionView>(`/sessions/${id}`),
  answer: (id: string, answer: string) =>
    call<{ turn: Turn; message: TutorMessage; session: SessionView }>(`/sessions/${id}/answer`, { method: "POST", body: JSON.stringify({ answer }) }),
  /** LangGraph sessions: 202 accepted (queued/running) or 200 already finished (a resend of a known command_id) */
  event: (id: string, body: { command_id: string; type: string; payload?: Record<string, unknown>; unit_id?: string; question_id?: string; expected_revision?: number }) =>
    call<CommandView & { session: SessionView }>(`/sessions/${id}/events`, { method: "POST", body: JSON.stringify(body) }),
  command: (id: string, commandId: string) => call<CommandView>(`/sessions/${id}/commands/${encodeURIComponent(commandId)}`),
  replay: (id: string) =>
    call<{ session: SessionView; replayed_answers: number; total_answers: number; error: ApiError | null }>(`/sessions/${id}/replay`, { method: "POST" }),
  exportUrl: (id: string, format: "json" | "md") => `/api/sessions/${id}/export?format=${format}`,
};
