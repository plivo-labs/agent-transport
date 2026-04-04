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

import { writeSync } from 'node:fs';
import type { SipEndpoint } from 'agent-transport';
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
}

export class JobContext {
  readonly sessionId: string;
  readonly remoteUri: string;
  readonly direction: 'inbound' | 'outbound';
  readonly endpoint: SipEndpoint;
  readonly userdata: Record<string, unknown>;
  readonly room: TransportRoom;
  readonly proc: JobProcess;

  private _session: any = null;
  private _callEnded: Promise<void>;
  private _resolveCallEnded: () => void;
  private _shutdownCallbacks: Array<() => void | Promise<void>> = [];

  constructor(opts: JobContextOptions) {
    this.sessionId = opts.sessionId;
    this.remoteUri = opts.remoteUri;
    this.direction = opts.direction;
    this.endpoint = opts.endpoint;
    this.proc = opts.proc ?? new JobProcess();
    this.userdata = opts.userdata;
    this._callEnded = opts.callEnded;
    this._resolveCallEnded = opts.resolveCallEnded;

    // Create Room facade
    this.room = new TransportRoom(opts.endpoint as any, opts.sessionId, {
      agentName: opts.agentName ?? 'sip-agent',
      callerIdentity: opts.remoteUri,
    });
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
    this._session = session;

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
          writeSync(2, `[SessionContext] TTS maxListeners set to 100 on ${tts?.constructor?.name}\n`);
        } else {
          writeSync(2, `[SessionContext] WARNING: TTS not found on session (tts=${tts})\n`);
        }
      } catch (e: any) {
        writeSync(2, `[SessionContext] TTS maxListeners failed: ${e?.message}\n`);
      }
      // Verify audio I/O survived session.start()
      console.log(`[SessionContext] post-start input.audio:`, session.input.audio?.constructor?.name);
      console.log(`[SessionContext] post-start output.audio:`, session.output.audio?.constructor?.name);
      console.log(`[SessionContext] post-start input.audioEnabled:`, session.input.audioEnabled);
      console.log(`[SessionContext] post-start started:`, session.started);
    };

    // Listen to session close event — handles agent-initiated shutdown
    session.on('close', async () => {
      console.log(`Call ${this.sessionId} session closed`);
      for (const cb of this._shutdownCallbacks) {
        try { await cb(); } catch {}
      }
      this._resolveCallEnded();
      try { this.endpoint.hangup(this.sessionId); } catch {}
    });
  }

  addShutdownCallback(callback: () => void | Promise<void>): void {
    this._shutdownCallbacks.push(callback);
  }

  /** @internal Wait for call to end — called by server after entrypoint returns */
  get callEnded(): Promise<void> {
    return this._callEnded;
  }
}
