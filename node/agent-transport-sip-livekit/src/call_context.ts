/**
 * CallContext — equivalent of LiveKit's JobContext for SIP/AudioStream calls.
 *
 * Creates a TransportRoom facade so get_job_context().room works,
 * enabling DTMF (GetDtmfTask), background audio, transcription, etc.
 *
 * Matches LiveKit WebRTC pattern:
 *   await session.start({ agent, room: ctx.room })
 */

import type { SipEndpoint } from 'agent-transport';
import { SipAudioInput } from './sip_audio_input.js';
import { SipAudioOutput } from './sip_audio_output.js';
import { TransportRoom } from '../../adapters/livekit.js';

export interface CallContextOptions {
  callId: string;
  remoteUri: string;
  direction: 'inbound' | 'outbound';
  endpoint: SipEndpoint;
  userdata: Record<string, unknown>;
  agentName?: string;
  callEnded: Promise<void>;
  resolveCallEnded: () => void;
}

export class CallContext {
  readonly callId: string;
  readonly remoteUri: string;
  readonly direction: 'inbound' | 'outbound';
  readonly endpoint: SipEndpoint;
  readonly userdata: Record<string, unknown>;
  readonly room: TransportRoom;

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

    // Create Room facade — matches Python's ctx._room
    this.room = new TransportRoom(opts.endpoint as any, opts.callId, {
      agentName: opts.agentName ?? 'sip-agent',
      callerIdentity: opts.remoteUri,
    });
  }

  get session(): any {
    return this._session;
  }

  /**
   * Wire SIP audio I/O, start the agent session, and wait for call to end.
   *
   * Creates Room facade so LiveKit features (DTMF, background audio) work.
   * Matches LiveKit WebRTC: await session.start({ agent, room: ctx.room })
   */
  async start(session: any, opts: { agent: any }): Promise<void> {
    const input = new SipAudioInput(this.endpoint, this.callId);
    const output = new SipAudioOutput(this.endpoint, this.callId);

    session.input.audio = input;
    session.output.audio = output;
    this._session = session;

    // Listen to session close event — handles agent-initiated shutdown
    // (matches Python's @session.on("close") pattern and LiveKit WebRTC's
    // RoomIO._on_agent_session_close)
    session.on('close', () => {
      console.log(`Call ${this.callId} session closed`);
      this._resolveCallEnded();
      // Send BYE to remote if agent initiated hangup
      try { this.endpoint.hangup(this.callId); } catch {}
    });

    try {
      // Pass room= so AgentSession creates RoomIO (audio disabled since I/O already set)
      await session.start({ agent: opts.agent, room: this.room });

      // Wait for call to end (resolved by either call_terminated event or session close)
      await this._callEnded;
    } finally {
      // Room cleanup — only emit disconnected, not participant_disconnected
      // (participant_disconnected already emitted by server event loop on call_terminated)
      this.room._onSessionEnded();
    }
  }
}
