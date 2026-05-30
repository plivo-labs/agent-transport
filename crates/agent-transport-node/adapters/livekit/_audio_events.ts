/**
 * AudioEventBroker — async-id ↔ event routing for the 0.2.0 audio protocol.
 *
 * The Rust core (see `crates/agent-transport-node/src/lib.rs`) exposes audio
 * sends as an async-id flow that mirrors LiveKit's `capture_audio_frame`:
 *
 *   const asyncId = ep.sendAudioAsync(sessionId, buf, rate, channels); // bigint
 *   // ...await the matching `audio_capture_complete` event by asyncId...
 *
 *   const asyncId = ep.waitForPlayoutAsync(sessionId); // bigint
 *   // ...await the matching `audio_playout_complete` event by asyncId...
 *
 * Every async-id produces exactly one terminal event:
 *   - `audio_capture_complete`  (asyncId)            → capture resolved
 *   - `audio_playout_complete`  (asyncId)            → playout resolved
 *   - `audio_capture_error`     (asyncId, error)     → either path rejected
 *     (emitted for every pending async-id on clearBuffer / flush / drop)
 *
 * Single-reader constraint
 * ------------------------
 * The napi endpoint exposes ONE destructive event channel
 * (`waitForEvent` / `pollEvent`). The LiveKit servers (`AgentServer`,
 * `AudioStreamServer`) own that single reader for call-routing. They feed
 * audio events into this broker via {@link AudioEventBroker.dispatch}.
 *
 * When `SipAudioOutput` is used standalone (no server pumping the channel),
 * the broker lazily starts its OWN pump so it still works — but a server,
 * once it has claimed the endpoint via {@link AudioEventBroker.claimFeeder},
 * suppresses the self-pump to preserve the single-reader invariant.
 *
 * Race freedom
 * ------------
 * The Python reference (`_audio_source.py`) subscribes a filtered FfiQueue
 * BEFORE issuing the FFI call so a synchronously-emitted completion is not
 * lost. The napi binding has no per-call subscribe primitive, so this broker
 * achieves the same guarantee differently: terminal events are keyed by
 * their (unique, monotonic) async-id and BUFFERED if no waiter is present
 * yet. A waiter registering after the event arrived finds the buffered
 * result. Ordering of "event dispatched" vs "waiter registered" therefore
 * does not matter — closing the dispatch-before-wait race.
 */

import type { SipEndpoint, AudioStreamEndpoint, EventInfo } from 'agent-transport';

type AnyEndpoint = SipEndpoint | AudioStreamEndpoint;

interface Waiter {
  resolve: () => void;
  reject: (err: Error) => void;
}

const AUDIO_EVENT_TYPES = new Set([
  'audio_capture_complete',
  'audio_playout_complete',
  'audio_buffer_drained',
  'audio_capture_error',
]);

/** True if `ev` is one of the async-id terminal audio events. */
export function isAudioEvent(ev: { eventType?: string } | null | undefined): boolean {
  return !!ev && AUDIO_EVENT_TYPES.has(ev.eventType as string);
}

export class AudioEventBroker {
  /** Pending waiters keyed by async-id. */
  private captureWaiters = new Map<bigint, Waiter>();
  private playoutWaiters = new Map<bigint, Waiter>();

  /**
   * Terminal events that arrived before a waiter registered. Keyed by
   * async-id. Bounded implicitly: each entry is consumed by exactly one
   * `waitFor*` call, and `clearBuffer`/`flush`/drop always emits an error
   * event for every outstanding async-id, so buffered entries cannot leak
   * across a session's lifetime.
   */
  private capturePending = new Map<bigint, EventInfo>();
  private playoutPending = new Map<bigint, EventInfo>();

  private endpoint: AnyEndpoint;
  private feederClaimed = false;
  private selfPumpRunning = false;
  private stopped = false;

  constructor(endpoint: AnyEndpoint) {
    this.endpoint = endpoint;
  }

  /**
   * Marks that an external reader (a server event loop) owns the endpoint
   * event channel and will call {@link dispatch}. Suppresses the self-pump.
   */
  claimFeeder(): void {
    this.feederClaimed = true;
  }

  /**
   * Route a terminal audio event to its waiter, or buffer it by async-id if
   * the waiter has not registered yet. Called by a server event loop (the
   * single reader) for events where {@link isAudioEvent} is true.
   */
  dispatch(ev: EventInfo): void {
    if (ev.asyncId === undefined || ev.asyncId === null) return;
    const id = ev.asyncId;

    switch (ev.eventType) {
      case 'audio_capture_complete': {
        const w = this.captureWaiters.get(id);
        if (w) {
          this.captureWaiters.delete(id);
          w.resolve();
        } else {
          this.capturePending.set(id, ev);
        }
        return;
      }
      case 'audio_playout_complete': {
        const w = this.playoutWaiters.get(id);
        if (w) {
          this.playoutWaiters.delete(id);
          w.resolve();
        } else {
          this.playoutPending.set(id, ev);
        }
        return;
      }
      case 'audio_capture_error': {
        // An error terminates whichever path is awaiting this async-id.
        const err = new Error(ev.error ?? 'audio_capture_error');
        const cw = this.captureWaiters.get(id);
        if (cw) {
          this.captureWaiters.delete(id);
          cw.reject(err);
        }
        const pw = this.playoutWaiters.get(id);
        if (pw) {
          this.playoutWaiters.delete(id);
          pw.reject(err);
        }
        if (!cw && !pw) {
          // Buffer under both so whichever path registers first sees it.
          this.capturePending.set(id, ev);
          this.playoutPending.set(id, ev);
        }
        return;
      }
      // audio_buffer_drained is informational; the LiveKit adapters await
      // playout/capture completion, not drain. Ignore.
      default:
        return;
    }
  }

  /**
   * Await the `audio_capture_complete` matching `asyncId`. Rejects if an
   * `audio_capture_error` for that id arrives (clear / flush / drop).
   */
  waitForCapture(asyncId: bigint): Promise<void> {
    this.ensureSelfPump();
    const buffered = this.capturePending.get(asyncId);
    if (buffered) {
      this.capturePending.delete(asyncId);
      if (buffered.eventType === 'audio_capture_error') {
        return Promise.reject(new Error(buffered.error ?? 'audio_capture_error'));
      }
      return Promise.resolve();
    }
    return new Promise<void>((resolve, reject) => {
      this.captureWaiters.set(asyncId, { resolve, reject });
    });
  }

  /**
   * Await the `audio_playout_complete` matching `asyncId`. Rejects if an
   * `audio_capture_error` for that id arrives (clear / flush / drop).
   */
  waitForPlayout(asyncId: bigint): Promise<void> {
    this.ensureSelfPump();
    const buffered = this.playoutPending.get(asyncId);
    if (buffered) {
      this.playoutPending.delete(asyncId);
      if (buffered.eventType === 'audio_capture_error') {
        return Promise.reject(new Error(buffered.error ?? 'audio_capture_error'));
      }
      return Promise.resolve();
    }
    return new Promise<void>((resolve, reject) => {
      this.playoutWaiters.set(asyncId, { resolve, reject });
    });
  }

  /** Stop the self-pump (if running) and reject all pending waiters. */
  stop(): void {
    this.stopped = true;
    const gone = new Error('audio event broker stopped');
    for (const [, w] of this.captureWaiters) w.reject(gone);
    for (const [, w] of this.playoutWaiters) w.reject(gone);
    this.captureWaiters.clear();
    this.playoutWaiters.clear();
  }

  /**
   * Fallback pump for standalone use. Drains the endpoint event channel and
   * dispatches audio events. No-op once a server has claimed the feeder.
   * Non-audio events drained here are discarded (standalone adapters don't
   * route call lifecycle events) — acceptable because a server would have
   * claimed the feeder if call routing mattered.
   */
  private ensureSelfPump(): void {
    if (this.feederClaimed || this.selfPumpRunning || this.stopped) return;
    this.selfPumpRunning = true;
    const loop = async () => {
      while (!this.stopped && !this.feederClaimed) {
        let ev: EventInfo | null = null;
        try {
          ev = await (this.endpoint as SipEndpoint).waitForEvent(1000);
        } catch {
          // Endpoint shut down.
          break;
        }
        if (!ev) continue;
        if (isAudioEvent(ev)) this.dispatch(ev);
      }
      this.selfPumpRunning = false;
    };
    void loop();
  }
}

/** One broker per endpoint instance. */
const brokers = new WeakMap<object, AudioEventBroker>();

/** Get (or lazily create) the {@link AudioEventBroker} for an endpoint. */
export function brokerFor(endpoint: AnyEndpoint): AudioEventBroker {
  let b = brokers.get(endpoint as object);
  if (!b) {
    b = new AudioEventBroker(endpoint);
    brokers.set(endpoint as object, b);
  }
  return b;
}
