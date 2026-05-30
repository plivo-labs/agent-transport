/**
 * Race a promise against a timeout. Returns void when the promise settles OR
 * the timeout fires (whichever is first). Used in shutdown paths so a wedged
 * resource (unresponsive inference child, keep-alive HTTP connection) can't
 * hang the process indefinitely.
 *
 * Swallows any rejection from `p` so callers don't need to wrap with
 * `.catch(() => {})` — handy in shutdown paths where the only thing we care
 * about is whether it settled in time.
 */
export function withTimeout(p: Promise<unknown>, ms: number, label: string): Promise<void> {
  return new Promise<void>((resolve) => {
    const timer = setTimeout(() => {
      console.warn(`[shutdown] ${label} timed out after ${ms}ms — continuing`);
      resolve();
    }, ms);
    Promise.resolve(p)
      .catch(() => {})
      .finally(() => {
        clearTimeout(timer);
        resolve();
      });
  });
}

/**
 * Things the cleanup body needs from the server. Modelled as a small
 * interface (rather than passing the server instance) so unit tests can
 * supply a stub without loading the native `agent-transport` binding.
 */
export interface CleanupContext {
  /** Iterator over active session ids (calls or streams). */
  activeSessionIds(): Iterable<string>;
  /** Hang up a single session. May throw — caller swallows. */
  hangup(sessionId: string): void;
  /** Stop the CPU load monitor. */
  stopLoadMonitor(): void;
  /**
   * Optional inference executor. ``close()`` is awaited under a 2s timeout
   * (via {@link withTimeout}); leave undefined if not attached.
   */
  inferenceExecutor?: { close(): Promise<unknown> } | null;
  /** Optional HTTP server close hook. May throw — caller swallows. */
  closeHttpServer?: () => void;
  /** Optional Rust endpoint shutdown. May throw — caller swallows. */
  shutdownEndpoint?: () => void;
}

/**
 * Hang up active sessions, then drain ancillary resources with bounded
 * timeouts. Intended to be called from a SIGINT/SIGTERM handler; the caller
 * is responsible for ``process.exit(0)`` once this returns.
 *
 * Split out of the server classes so the signal-handler path stays a thin
 * wrapper and tests can drive the cleanup logic with a stub context — no
 * native binding required.
 */
export async function runServerCleanup(ctx: CleanupContext): Promise<void> {
  console.log('Shutting down...');
  for (const sessionId of ctx.activeSessionIds()) {
    try {
      ctx.hangup(sessionId);
    } catch {
      // Don't let one failed hangup short-circuit cleanup of others.
    }
  }
  try {
    ctx.stopLoadMonitor();
  } catch {}
  if (ctx.inferenceExecutor) {
    await withTimeout(
      Promise.resolve(ctx.inferenceExecutor.close()).catch(() => {}),
      2000,
      'inferenceExecutor.close()',
    );
  }
  if (ctx.closeHttpServer) {
    try {
      ctx.closeHttpServer();
    } catch {}
  }
  if (ctx.shutdownEndpoint) {
    try {
      ctx.shutdownEndpoint();
    } catch {}
  }
}

const ACTIVITY_INPUT_GUARD_INSTALLED = Symbol('agentTransportActivityInputGuardInstalled');

function guardActivityInput(activity: any): void {
  if (!activity || activity[ACTIVITY_INPUT_GUARD_INSTALLED]) return;

  try {
    Object.defineProperty(activity, ACTIVITY_INPUT_GUARD_INSTALLED, { value: true });
  } catch {
    activity[ACTIVITY_INPUT_GUARD_INSTALLED] = true;
  }

  if (typeof activity.onEndOfTurn === 'function') {
    activity.onEndOfTurn = async function onEndOfTurnAfterTransportClose(_info: unknown) {
      try {
        if (typeof this.cancelPreemptiveGeneration === 'function') {
          this.cancelPreemptiveGeneration();
        }
      } catch {}
      return true;
    };
  }

  if (typeof activity.onPreemptiveGeneration === 'function') {
    activity.onPreemptiveGeneration = function onPreemptiveGenerationAfterTransportClose() {};
  }
}

/**
 * Synchronously begin tearing down a LiveKit AgentSession on call termination.
 *
 * Guards end-of-turn and preemptive-generation callbacks, closes the audio
 * input (stops feeding STT), clears pending playout, and calls
 * `shutdown({ drain: false })`. Guarding happens before the next I/O tick, so
 * any STT transcript buffered at Deepgram is dropped before it can trigger
 * LLM/TTS on a dead call.
 *
 * Verified against `@livekit/agents` 1.2.x. `session.shutdown()` is public;
 * `activity.onEndOfTurn` / `activity.onPreemptiveGeneration` are private
 * LiveKit hooks; if upstream renames them, issue #83's race can return.
 *
 * Do not set `activity._schedulingPaused` directly on Node. LiveKit's
 * `activity.drain()` treats that flag as "already drained" and skips
 * `agent.onExit()`.
 */
export function forceShutdownAgentSession(session: any): void {
  if (!session) return;

  try {
    const activities = new Set([session.activity, session.nextActivity].filter(Boolean));
    for (const activity of activities) {
      guardActivityInput(activity);
    }
  } catch (e) {
    console.debug('[force-shutdown] activity input guard failed:', e);
  }

  try {
    const audioIn = session.input?.audio;
    if (audioIn && typeof audioIn.close === 'function') {
      Promise.resolve(audioIn.close()).catch(() => {});
    }
  } catch (e) {
    console.debug('[force-shutdown] audio input close failed:', e);
  }

  try {
    const audioOut = session.output?.audio;
    if (audioOut && typeof audioOut.clearBuffer === 'function') {
      audioOut.clearBuffer();
    }
  } catch (e) {
    console.debug('[force-shutdown] audio output clearBuffer failed:', e);
  }

  try {
    if (typeof session.shutdown === 'function') {
      session.shutdown({ drain: false });
    }
  } catch (e) {
    console.debug('[force-shutdown] session.shutdown failed:', e);
  }
}
