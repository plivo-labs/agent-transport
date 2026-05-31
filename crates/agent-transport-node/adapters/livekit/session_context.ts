/**
 * JobContext — equivalent of LiveKit's JobContext for SIP/AudioStream calls.
 *
 * Matches LiveKit's standard pattern exactly:
 *   server.sipSession(async (ctx) => {
 *     const session = new voice.AgentSession({ ... });
 *     ctx.session = session;
 *     await session.start({ agent, room: ctx.room });
 *   });
 *
 * Setting ctx.session automatically wires SIP audio I/O and registers
 * the close handler. Then session.start({ room: ctx.room }) works exactly
 * like LiveKit WebRTC.
 */

import type { SipEndpoint } from 'agent-transport';
import { mkdirSync } from 'node:fs';
import { SipAudioInput } from './sip_audio_input.js';
import { SipAudioOutput } from './sip_audio_output.js';
import { TransportRoom } from './livekit_adapters.js';
import { JobProcess } from './agent_server.js';

export interface JobContextOptions {
  sessionId: string;
  remoteUri: string;
  direction: 'inbound' | 'outbound';
  endpoint: SipEndpoint;
  userdata: Record<string, unknown>;
  agentName?: string;
  callEnded: Promise<void>;
  resolveCallEnded: () => void;
  proc?: JobProcess;
  inferenceExecutor?: unknown;
  enableRecording?: boolean;
  sessionDirectory?: string;
}

export class JobContext {
  readonly sessionId: string;
  readonly remoteUri: string;
  readonly direction: 'inbound' | 'outbound';
  readonly endpoint: SipEndpoint;
  readonly userdata: Record<string, unknown>;
  readonly room: TransportRoom;
  readonly proc: JobProcess;
  readonly job: { id: string; agentName: string; enableRecording: boolean; room: TransportRoom };
  readonly workerId = 'local';
  readonly worker_id = 'local';
  readonly sessionDirectory: string;
  readonly inferenceExecutor: unknown;

  /** @internal Primary AgentSession used by LiveKit job-context helpers. */
  _primaryAgentSession: any = undefined;

  private _session: any = null;
  private _callEnded: Promise<void>;
  private _resolveCallEnded: () => void;
  private _shutdownCallbacks: Array<(reason?: string) => void | Promise<void>> = [];
  private _shutdownCallbacksFired = false;

  constructor(opts: JobContextOptions) {
    this.sessionId = opts.sessionId;
    this.remoteUri = opts.remoteUri;
    this.direction = opts.direction;
    this.endpoint = opts.endpoint;
    this.proc = opts.proc ?? new JobProcess();
    this.userdata = opts.userdata;
    this._callEnded = opts.callEnded;
    this._resolveCallEnded = opts.resolveCallEnded;
    this.inferenceExecutor = opts.inferenceExecutor;
    this.sessionDirectory = opts.sessionDirectory ?? '/tmp/agent-sessions';
    try { mkdirSync(this.sessionDirectory, { recursive: true }); } catch {}

    // Create Room facade
    this.room = new TransportRoom(opts.endpoint as any, opts.sessionId, {
      agentName: opts.agentName ?? 'sip-agent',
      callerIdentity: opts.remoteUri,
    });
    this.job = {
      id: `job-${opts.sessionId}`,
      agentName: opts.agentName ?? 'sip-agent',
      enableRecording: opts.enableRecording ?? false,
      room: this.room,
    };
  }

  get session(): any {
    return this._session;
  }

  /**
   * Set the agent session — automatically wires SIP audio I/O.
   *
   * After setting ctx.session, call session.start({ agent, room: ctx.room })
   * directly — matches LiveKit WebRTC pattern exactly.
   */
  set session(session: any) {
    if (session == null) {
      throw new TypeError(
        "JobContext.session cannot be set to null/undefined. Assign a "
        + "valid voice.AgentSession instance (or use ctx.session to read)."
      );
    }
    this._session = session;
    this._primaryAgentSession = session;

    // Wire SIP audio I/O before session.start() is called
    session.input.audio = new SipAudioInput(this.endpoint, this.sessionId);
    session.output.audio = new SipAudioOutput(this.endpoint, this.sessionId);

    // Wrap session.start() to disable RoomIO audio replacement.
    // The Python SDK does this internally (sets room_options.audio_input = False when
    // input.audio is already set). The TS SDK doesn't — it creates RoomIO with audio
    // enabled and overwrites our I/O. We fix this by injecting the disable options,
    // so the user can call session.start({ agent, room: ctx.room }) normally.
    const originalStart = session.start.bind(session);
    session.start = async (opts: any) => {
      opts = { ...opts };
      opts.inputOptions = { ...opts.inputOptions, audioEnabled: false };
      opts.outputOptions = { ...opts.outputOptions, audioEnabled: false };
      await originalStart(opts);
      // Work around LiveKit SDK bug: StreamAdapter (tts/stream_adapter.ts) adds anonymous
      // metrics_collected + error listeners to the TTS emitter on every speech generation
      // but never removes them. With preemptive generation, 11+ speech handles exceed the
      // default maxListeners=10, causing ERR_UNHANDLED_ERROR crash on TTS abort.
      // Ref: StreamAdapter constructor at stream_adapter.ts:24-27
      try {
        const tts = (session as any).tts;
        if (tts?.setMaxListeners) {
          tts.setMaxListeners(100);
        }
      } catch {
        /* best-effort */
      }
    };

    // Listen to session close event — handles agent-initiated shutdown
    session.on('close', async () => {
      console.log(`Call ${this.sessionId} session closed`);
      await this.runShutdownCallbacks('session closed');
      this._resolveCallEnded();
      try { this.endpoint.hangup(this.sessionId); } catch {}
    });
  }

  addShutdownCallback(callback: (reason?: string) => void | Promise<void>): void {
    this._shutdownCallbacks.push(callback);
  }

  private async runShutdownCallbacks(reason: string): Promise<void> {
    if (this._shutdownCallbacksFired) return;
    this._shutdownCallbacksFired = true;
    for (const cb of this._shutdownCallbacks) {
      try { await cb(reason); } catch {}
    }
  }

  /**
   * Terminate the agent job and drop the underlying SIP/audio_stream call.
   *
   * Matches the Python `JobContext.shutdown(reason)` contract and the
   * `AgentSession.shutdown()` flow in TS LiveKit Agents. Idempotent — Rust
   * `endpoint.hangup` is a no-op once the session is gone.
   *
   * User-visible behavior:
   *   1. Fires all registered shutdown callbacks (fire-and-forget on the
   *      event loop; exceptions from user callbacks are swallowed).
   *   2. Calls `endpoint.hangup(sessionId)` to drop the SIP call.
   *   3. Resolves the internal `callEnded` promise so the server's
   *      entrypoint runner proceeds to cleanup.
   */
  shutdown(reason: string = ''): void {
    console.log(`Call ${this.sessionId} JobContext.shutdown(reason=${JSON.stringify(reason)})`);
    // Fire user callbacks — fire-and-forget (shutdown() is sync for
    // API parity with LiveKit's JobContext).
    this.runShutdownCallbacks(reason).catch(() => {});
    try {
      this.endpoint.hangup(this.sessionId);
    } catch {
      /* session already gone */
    }
    try {
      this._resolveCallEnded();
    } catch {
      /* already resolved */
    }
  }

  /**
   * Async equivalent of shutdown — matches Python `JobContext.delete_room`.
   *
   * LiveKit's real `delete_room` returns a Future/Promise; our stub maps
   * it directly to `endpoint.hangup`. Safe to call multiple times; safe
   * to call before or after `shutdown()`.
   */
  async deleteRoom(_roomName: string = ''): Promise<void> {
    try {
      this.endpoint.hangup(this.sessionId);
    } catch {
      /* already gone */
    }
  }

  /** @internal Wait for call to end — called by server after entrypoint returns */
  get callEnded(): Promise<void> {
    return this._callEnded;
  }

  isFakeJob(): boolean {
    return false;
  }

  is_fake_job(): boolean {
    return false;
  }

  async connect(): Promise<void> {
    // The SIP transport is already connected by the time the agent starts.
  }

  initRecording(): void {
    // agent-transport owns mixed transport recording; LiveKit RecorderIO is disabled.
  }

  get agent(): any {
    return this.room.localParticipant;
  }

  async waitForParticipant(identity?: string): Promise<any> {
    const participants = Array.from(this.room.remoteParticipants.values());
    return participants.find((p: any) => !identity || p.identity === identity) ?? participants[0];
  }

  addParticipantEntrypoint(callback: (job: JobContext, participant: any) => unknown): void {
    this.waitForParticipant()
      .then((participant) => callback(this, participant))
      .catch(() => {});
  }
}
