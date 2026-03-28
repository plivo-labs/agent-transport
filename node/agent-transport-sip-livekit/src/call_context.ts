/**
 * CallContext — equivalent of LiveKit's JobContext for SIP calls.
 */

import type { SipEndpoint } from 'agent-transport';
import { SipAudioInput } from './sip_audio_input.js';
import { SipAudioOutput } from './sip_audio_output.js';

export interface CallContextOptions {
  callId: string;
  remoteUri: string;
  direction: 'inbound' | 'outbound';
  endpoint: SipEndpoint;
  userdata: Record<string, unknown>;
  callEnded: Promise<void>;
  resolveCallEnded: () => void;
}

export class CallContext {
  readonly callId: string;
  readonly remoteUri: string;
  readonly direction: 'inbound' | 'outbound';
  readonly endpoint: SipEndpoint;
  readonly userdata: Record<string, unknown>;

  private _session: any = null;
  private _callEnded: Promise<void>;
  private _resolveCallEnded: () => void;

  constructor(opts: CallContextOptions) {
    this.callId = opts.callId;
    this.remoteUri = opts.remoteUri;
    this.direction = opts.direction;
    this.endpoint = opts.endpoint;
    this.userdata = opts.userdata;
    this._callEnded = opts.callEnded;
    this._resolveCallEnded = opts.resolveCallEnded;
  }

  get session(): any {
    return this._session;
  }

  /**
   * Wire SIP audio I/O, start the agent session, and wait for call to end.
   * Equivalent of: await session.start({ agent, room: ctx.room })
   */
  async start(session: any, opts: { agent: any }): Promise<void> {
    const input = new SipAudioInput(this.endpoint, this.callId);
    const output = new SipAudioOutput(this.endpoint, this.callId);

    session.input.audio = input;
    session.output.audio = output;
    this._session = session;

    await session.start({ agent: opts.agent });

    // Wait for SIP call to end
    await this._callEnded;
  }
}
