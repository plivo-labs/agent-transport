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

import { writeSync } from 'node:fs';
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

  private _session: any = null;
  private _callEnded: Promise<void>;
  private _resolveCallEnded: () => void;
  private _shutdownCallbacks: Array<() => void | Promise<void>> = [];

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

    this.room = new TransportRoom(opts.endpoint as any, opts.sessionId, {
      agentName: opts.agentName ?? 'audio-stream-agent',
      callerIdentity: opts.plivoCallUuid,
    });
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
      for (const cb of this._shutdownCallbacks) {
        try { await cb(); } catch {}
      }
      this._resolveCallEnded();
      try { (this.endpoint as any).hangup(this.sessionId); } catch {}
    });
  }

  addShutdownCallback(callback: () => void | Promise<void>): void {
    this._shutdownCallbacks.push(callback);
  }

  /** @internal */
  get callEnded(): Promise<void> {
    return this._callEnded;
  }
}
