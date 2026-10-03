// Commands of LangGraph sessions (POST /api/sessions/{id}/events). Every action gets a command_id once, before it
// is sent; network retries and page reloads reuse that id, so the server applies it at most once. The pending
// command (type, payload, id) is kept in sessionStorage until the server reports it finished, so a reload first
// asks for its status and only resends when the server never received it.
import { useCallback, useEffect, useRef, useState } from "react";
import { api, RequestError } from "./api";
import type { ApiError, CommandView, SessionView } from "./types";

export const timing = { pollMs: 2000, resendMs: 1500, resends: 3 };

export interface PendingCommand {
  sid: string;
  command_id: string;
  type: string;
  payload: Record<string, unknown>;
  unit_id?: string;
  question_id?: string;
  expected_revision?: number;
}

const key = (sid: string) => `mentor.pending.${sid}`;

function load(sid: string): PendingCommand | null {
  try {
    const v = sessionStorage.getItem(key(sid));
    return v ? (JSON.parse(v) as PendingCommand) : null;
  } catch {
    return null;
  }
}

function store(p: PendingCommand | null, sid: string) {
  try {
    if (p) sessionStorage.setItem(key(sid), JSON.stringify(p));
    else sessionStorage.removeItem(key(sid));
  } catch {
    // storage unavailable (private mode): the command still has its id for in-page resends
  }
}

export function pendingCommand(sid: string): PendingCommand | null {
  return load(sid);
}

function newId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `c-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const finished = (c: CommandView) => c.status === "completed" || c.status === "failed";

/** Thrown when the server refused the command (409 stale / busy, 400 …); `session` is the latest view when sent. */
export class CommandRefused extends Error {
  constructor(public body: ApiError & { session?: SessionView | null }) {
    super(body.detail);
  }
}

async function deliver(p: PendingCommand, onTick: () => void): Promise<CommandView> {
  let cmd: CommandView | null = null;
  for (let i = 0; ; i++) {
    try {
      const r = await api.event(p.sid, { command_id: p.command_id, type: p.type, payload: p.payload, unit_id: p.unit_id,
        question_id: p.question_id, expected_revision: p.expected_revision });
      cmd = r;
      break;
    } catch (e) {
      const err = e as RequestError;
      if (err.status === 0 && i < timing.resends) {   // no answer at all: resend the same id
        await sleep(timing.resendMs);
        continue;
      }
      store(null, p.sid);
      throw err.status === 0 ? err : new CommandRefused(err.body);
    }
  }
  while (!finished(cmd)) {
    await sleep(timing.pollMs);
    onTick();
    try {
      cmd = await api.command(p.sid, p.command_id);
    } catch {
      // keep polling through a network hiccup; the command is safe on the server
    }
  }
  store(null, p.sid);
  return cmd;
}

/** send(type, payload, extras) → the finished command; `running` while one is in flight. `types` limits which
 *  pending command this component resumes after a reload (the reading panel and the quiz each own theirs). */
export function useCommands(session: SessionView | null, onSession: (s: SessionView) => void, types: string[]) {
  const sid = session?.id ?? "";
  const [running, setRunning] = useState<PendingCommand | null>(null);
  const alive = useRef(true);
  const refresh = useCallback(() => {
    if (sid) api.session(sid).then((s) => alive.current && onSession(s)).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sid]);

  const run = useCallback(async (p: PendingCommand): Promise<CommandView> => {
    setRunning(p);
    try {
      const cmd = await deliver(p, refresh);
      refresh();
      return cmd;
    } finally {
      if (alive.current) setRunning(null);
    }
  }, [refresh]);

  const send = useCallback((type: string, payload: Record<string, unknown> = {}, extra: Partial<PendingCommand> = {}) => {
    const p: PendingCommand = { sid, command_id: newId(), type, payload, ...extra };
    store(p, sid);
    return run(p);
  }, [sid, run]);

  // after a reload: ask about the unconfirmed command first; resend it (same id) only if the server never got it
  const [resumed, setResumed] = useState<CommandView | null>(null);
  useEffect(() => {
    alive.current = true;
    const p = sid && session && isGraph(session) ? load(sid) : null;
    if (p && types.includes(p.type)) {
      api.command(sid, p.command_id)
        .then((c) => (finished(c) ? (store(null, sid), refresh(), c) : run(p)))
        .catch((e: RequestError) => (e.status === 404 ? run(p) : null))
        .then((c) => c && alive.current && setResumed(c))
        .catch(() => {});
    }
    return () => {
      alive.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sid]);

  return { send, running, resumed };
}

export function isGraph(s: SessionView): boolean {
  return s.workflow_runner === "langgraph";
}
