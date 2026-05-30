// Best-effort cleanup of user-supplied STT/TTS/LLM at session end.
//
// Upstream @livekit/agents runs every job in a forked child process (see
// `ipc/job_proc_executor` — `import { fork } from 'node:child_process'`);
// when the child exits, the OS reclaims the WebSocket FDs that STT/TTS/LLM
// plugins keep open against their vendors. Agent-transport's LiveKit
// adapter instead handles every call as an in-process async function on
// a long-lived server (see `runCall()` in agent_server.ts and
// `runSession()` in audio_stream_server.ts), so those FDs leak per call
// unless we close each service explicitly. AgentSession.close() does not
// cascade into the user-supplied services — they're treated as caller-
// owned in upstream and freed by the process exit.
//
// Ownership model: any STT/TTS/LLM attached to an AgentSession is
// considered session-owned and is closed when that session ends. This
// matches upstream's per-subprocess lifecycle, where each call effectively
// gets its own services anyway. Process-scoped resources that must
// survive across calls (canonically the VAD, via proc.userdata['vad'])
// are *not* touched — only session.stt/tts/llm is walked. Plugging a
// single STT/TTS/LLM into many concurrent sessions is not supported:
// most plugins carry per-stream state (transcripts, history, audio
// buffers) and would corrupt regardless of any cleanup behavior.

export interface CloseSessionServicesOptions {
  /** Per-service timeout in milliseconds. Default 2000ms. */
  perServiceTimeoutMs?: number;
  /** Optional logger for visibility into closes and failures. */
  logger?: (msg: string, err?: unknown) => void;
}

type AnyFn = (this: unknown) => unknown | Promise<unknown>;

const SERVICE_KINDS = ['stt', 'tts', 'llm'] as const;
type ServiceKind = (typeof SERVICE_KINDS)[number];

// The Node @livekit/agents SDK is inconsistent: STT, TTS and RealtimeModel
// expose `close()`, but the standard chat LLM base class exposes `aclose()`
// (cf. node_modules/@livekit/agents/dist/llm/llm.js vs llm/realtime.js).
// Prefer the canonical name for the field, fall back to the other so plugin
// overrides that diverge from the base still get cleaned up.
const PRIMARY: Record<ServiceKind, 'close' | 'aclose'> = {
  stt: 'close',
  tts: 'close',
  llm: 'aclose',
};

function pickCloser(svc: object): AnyFn | null {
  const candidates = ['close', 'aclose'] as const;
  for (const name of candidates) {
    const fn = (svc as Record<string, unknown>)[name];
    if (typeof fn === 'function') return fn as AnyFn;
  }
  return null;
}

/**
 * Walk session.stt / tts / llm and close each independently with a per-
 * service timeout. Never throws; one failing service does not skip the
 * others. Safe to call with a missing or partially populated session.
 */
export async function closeSessionServices(
  session: unknown,
  opts: CloseSessionServicesOptions = {},
): Promise<void> {
  if (session == null) return;

  const timeoutMs = opts.perServiceTimeoutMs ?? 2000;
  const log = opts.logger;

  for (const kind of SERVICE_KINDS) {
    const svc = (session as Record<string, unknown>)[kind];
    if (svc == null || typeof svc !== 'object') continue;

    const preferred = (svc as Record<string, unknown>)[PRIMARY[kind]];
    const closer: AnyFn | null =
      typeof preferred === 'function' ? (preferred as AnyFn) : pickCloser(svc);
    if (closer === null) continue;

    let timer: NodeJS.Timeout | undefined;
    const startedAt = Date.now();
    try {
      // Promise.race against a per-service timeout so a hung peer cannot
      // block call teardown. The work promise keeps running in the
      // background if it loses the race — that's fine: the FD leak is the
      // worst case we're already in, and we logged it.
      await Promise.race([
        Promise.resolve(closer.call(svc)),
        new Promise<never>((_, reject) => {
          timer = setTimeout(() => reject(new Error('timeout')), timeoutMs);
        }),
      ]);
      log?.(`closed ${kind} in ${Date.now() - startedAt}ms`);
    } catch (err) {
      const msg = (err as Error | undefined)?.message === 'timeout'
        ? `timed out closing ${kind} after ${timeoutMs}ms`
        : `error closing ${kind}`;
      log?.(msg, err);
    } finally {
      if (timer) clearTimeout(timer);
    }
  }
}
