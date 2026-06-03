/**
 * AudioStreamJobContext — equivalent of LiveKit's JobContext for Plivo audio streaming.
 *
 * Matches the SIP JobContext pattern:
 *   server.audioStreamSession(async (ctx) => {
 *     const session = new voice.AgentSession({ ... });
 *     ctx.session = session;
 *     await session.start({ agent, room: ctx.room });
 *   });
 */

import { mkdirSync, writeSync } from 'node:fs';
import type { AudioStreamEndpoint } from 'agent-transport';
import { SipAudioInput } from './sip_audio_input.js';
import { SipAudioOutput } from './sip_audio_output.js';
import { TransportRoom } from './livekit_adapters.js';
import { JobProcess } from './agent_server.js';

export interface AudioStreamJobContextOptions {
  sessionId: string;
  plivoCallUuid: string;  // Plivo Call UUID
  streamId: string;       // Plivo Stream UUID
  direction: 'inbound';
  extraHeaders: Record<string, string>;
  endpoint: AudioStreamEndpoint;
  userdata: Record<string, unknown>;
  agentName?: string;
  callEnded: Promise<void>;
  resolveCallEnded: () => void;
  proc?: JobProcess;
  inferenceExecutor?: unknown;
  enableRecording?: boolean;
  sessionDirectory?: string;
}

export class AudioStreamJobContext {
  readonly sessionId: string;
  readonly plivoCallUuid: string;
  readonly streamId: string;
  readonly direction: 'inbound';
  readonly extraHeaders: Record<string, string>;
  readonly endpoint: AudioStreamEndpoint;
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

  constructor(opts: AudioStreamJobContextOptions) {
    this.sessionId = opts.sessionId;
    this.plivoCallUuid = opts.plivoCallUuid;
    this.streamId = opts.streamId;
    this.direction = opts.direction;
    this.extraHeaders = opts.extraHeaders;
    this.endpoint = opts.endpoint;
    this.proc = opts.proc ?? new JobProcess();
    this.userdata = opts.userdata;
    this._callEnded = opts.callEnded;
    this._resolveCallEnded = opts.resolveCallEnded;
    this.inferenceExecutor = opts.inferenceExecutor;
    this.sessionDirectory = opts.sessionDirectory ?? '/tmp/agent-sessions';
    try { mkdirSync(this.sessionDirectory, { recursive: true }); } catch {}

    this.room = new TransportRoom(opts.endpoint as any, opts.sessionId, {
      agentName: opts.agentName ?? 'audio-stream-agent',
      callerIdentity: opts.plivoCallUuid,
    });
    this.job = {
      id: `job-${opts.sessionId}`,
      agentName: opts.agentName ?? 'audio-stream-agent',
      enableRecording: opts.enableRecording ?? false,
      room: this.room,
    };
  }

  get session(): any {
    return this._session;
  }

  /**
   * Set the agent session — automatically wires audio stream I/O.
   * After setting, call session.start({ agent, room: ctx.room }).
   */
  set session(session: any) {
    this._session = session;
    this._primaryAgentSession = session;

    // Wire audio stream I/O (uses same SipAudioInput/Output — they work with AudioStreamEndpoint too)
    session.input.audio = new SipAudioInput(this.endpoint as any, this.sessionId);
    session.output.audio = new SipAudioOutput(this.endpoint as any, this.sessionId);

    // Wrap session.start() to disable RoomIO audio replacement (matches SIP JobContext)
    const originalStart = session.start.bind(session);
    session.start = async (opts: any) => {
      opts = { ...opts };
      opts.inputOptions = { ...opts.inputOptions, audioEnabled: false };
      opts.outputOptions = { ...opts.outputOptions, audioEnabled: false };
      await originalStart(opts);
      // Work around LiveKit SDK bug: StreamAdapter adds anonymous listeners per speech
      try {
        const tts = (session as any).tts;
        if (tts?.setMaxListeners) {
          tts.setMaxListeners(100);
          writeSync(2, `[AudioStreamContext] TTS maxListeners set to 100\n`);
        }
      } catch {}
    };

    session.on('close', async () => {
      console.log(`Session ${this.sessionId} closed`);
      await this.runShutdownCallbacks('session closed');
      this._resolveCallEnded();
      try { (this.endpoint as any).hangup(this.sessionId); } catch {}
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
   * Terminate the agent job and drop the underlying audio_stream call.
   *
   * Matches the Python `JobContext.shutdown(reason)` contract and
   * mirrors `JobContext.shutdown` in `session_context.ts` (SIP variant).
   * Idempotent — `endpoint.hangup` is a no-op once the session is gone.
   *
   * Behavior:
   *   1. Fires all registered shutdown callbacks (fire-and-forget on
   *      the event loop; exceptions from user callbacks are swallowed).
   *   2. Calls `endpoint.hangup(sessionId)` to drop the Plivo WS session.
   *   3. Resolves the internal `callEnded` promise so the server's
   *      entrypoint runner proceeds to cleanup.
   */
  shutdown(reason: string = ''): void {
    console.log(`Session ${this.sessionId} JobContext.shutdown(reason=${JSON.stringify(reason)})`);
    this.runShutdownCallbacks(reason).catch(() => {});
    try {
      (this.endpoint as any).hangup(this.sessionId);
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
   * to call before or after `shutdown()`. This is the method
   * `EndCallTool` calls via its shutdown callback when `deleteRoom: true`.
   */
  async deleteRoom(_roomName: string = ''): Promise<void> {
    try {
      (this.endpoint as any).hangup(this.sessionId);
    } catch {
      /* already gone */
    }
  }

  /** @internal */
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
    // The audio stream transport is already connected by the time the agent starts.
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

  addParticipantEntrypoint(callback: (job: AudioStreamJobContext, participant: any) => unknown): void {
    this.waitForParticipant()
      .then((participant) => callback(this, participant))
      .catch(() => {});
  }
}
