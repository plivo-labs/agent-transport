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
import { SipAudioInput } from './sip_audio_input.js';
import { SipAudioOutput } from './sip_audio_output.js';
import { TransportRoom } from './livekit_adapters.js';
import { JobProcess } from './agent_server.js';

export interface JobContextOptions {
  callId: string;
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
  readonly callId: string;
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
    this.callId = opts.callId;
    this.remoteUri = opts.remoteUri;
    this.direction = opts.direction;
    this.endpoint = opts.endpoint;
    this.proc = opts.proc ?? new JobProcess();
    this.userdata = opts.userdata;
    this._callEnded = opts.callEnded;
    this._resolveCallEnded = opts.resolveCallEnded;

    // Create Room facade
    this.room = new TransportRoom(opts.endpoint as any, opts.callId, {
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
    session.input.audio = new SipAudioInput(this.endpoint, this.callId);
    session.output.audio = new SipAudioOutput(this.endpoint, this.callId);

    // Listen to session close event — handles agent-initiated shutdown
    session.on('close', () => {
      console.log(`Call ${this.callId} session closed`);
      this._resolveCallEnded();
      try { this.endpoint.hangup(this.callId); } catch {}
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
