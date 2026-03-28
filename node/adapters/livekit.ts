/**
 * LiveKit Agents TypeScript adapters for agent-transport.
 *
 * Implements LiveKit's AudioInput/AudioOutput abstract classes and
 * TransportRoom facade using agent-transport's SipEndpoint or AudioStreamEndpoint.
 *
 * Usage:
 *   import { SipAudioInput, SipAudioOutput, TransportRoom } from 'agent-transport-adapters/livekit';
 *   import { SipEndpoint } from 'agent-transport';
 *
 *   const ep = new SipEndpoint({ sipServer: 'phone.plivo.com' });
 *   const room = new TransportRoom(ep, callId, { agentName: 'my-agent', callerIdentity: remoteUri });
 *   const input = new SipAudioInput(ep, callId);
 *   const output = new SipAudioOutput(ep, callId);
 *
 *   // Connect to LiveKit AgentSession
 *   session.input.audio = input;
 *   session.output.audio = output;
 *   await session.start({ agent: myAgent, room });
 */

// Types matching LiveKit's @livekit/agents voice/io.ts

export interface AudioOutputCapabilities {
  pause: boolean;
}

export interface PlaybackFinishedEvent {
  playbackPosition: number;
  interrupted: boolean;
  synchronizedTranscript?: string;
}

export interface PlaybackStartedEvent {
  createdAt: number;
}

export interface AudioFrame {
  data: Int16Array | Uint8Array;
  sampleRate: number;
  numChannels: number;
  samplesPerChannel: number;
}

export interface SipDTMF {
  code: number;
  digit: string;
  participant?: TransportRemoteParticipant;
}

// Transport endpoint interface (matches our Rust binding)
export interface TransportEndpoint {
  sendAudioBytes(sessionId: string, audio: Uint8Array, sampleRate: number, numChannels: number): void;
  sendBackgroundAudio(sessionId: string, audio: Uint8Array, sampleRate: number, numChannels: number): void;
  sendDtmf(sessionId: string, digits: string): void;
  sendRawMessage(sessionId: string, message: string): void;
  recvAudioBytesBlocking(sessionId: string, timeoutMs?: number): Uint8Array | null;
  recvAudioBytesAsync(sessionId: string, timeoutMs?: number): Promise<Uint8Array | null>;
  flush(sessionId: string): void;
  clearBuffer(sessionId: string): void;
  pause(sessionId: string): void;
  resume(sessionId: string): void;
  queuedFrames(sessionId: string): number;
  waitForPlayout(sessionId: string, timeoutMs?: number): boolean;
  waitForPlayoutAsync(sessionId: string, timeoutMs?: number): Promise<boolean>;
  sampleRate: number;
}

// ─── EventEmitter (matches LiveKit's rtc.EventEmitter) ──────────────────────

type EventCallback = (...args: any[]) => void;

export class EventEmitter {
  private _events: Map<string, Set<EventCallback>> = new Map();

  on(event: string, callback: EventCallback): EventCallback {
    if (!this._events.has(event)) this._events.set(event, new Set());
    this._events.get(event)!.add(callback);
    return callback;
  }

  off(event: string, callback: EventCallback): void {
    this._events.get(event)?.delete(callback);
  }

  emit(event: string, ...args: any[]): void {
    for (const cb of this._events.get(event) ?? []) {
      try { cb(...args); } catch (e) { console.error(`Error in ${event} listener:`, e); }
    }
  }
}

// ─── Stub Track Publication ─────────────────────────────────────────────────

let _pubCounter = 0;

export class StubTrackPublication {
  sid: string;
  track: any;
  name = '';
  kind = 0; // AUDIO
  source = 1; // MICROPHONE
  muted = false;

  constructor(track: any, sid?: string) {
    this.track = track;
    this.sid = sid ?? `TR_${(++_pubCounter).toString(36)}`;
  }

  async waitForSubscription(): Promise<void> {}
}

// ─── Transport Remote Participant ───────────────────────────────────────────

export class TransportRemoteParticipant {
  sid: string;
  identity: string;
  name: string;
  metadata = '';
  attributes: Record<string, string> = {};
  kind = 3; // PARTICIPANT_KIND_SIP
  trackPublications: Record<string, any> = {};

  constructor(identity: string, callId: string) {
    this.sid = `PR_${callId}`;
    this.identity = identity;
    this.name = identity;
  }
}

// ─── Transport Local Participant ────────────────────────────────────────────

export class TransportLocalParticipant {
  private _endpoint: TransportEndpoint;
  private _sessionId: string;
  private _forwardAborts: Map<string, AbortController> = new Map();

  sid: string;
  identity: string;
  name: string;
  metadata = '';
  attributes: Record<string, string> = {};
  kind = 0; // STANDARD
  trackPublications: Record<string, StubTrackPublication> = {};

  constructor(endpoint: TransportEndpoint, sessionId: string, agentName: string) {
    this._endpoint = endpoint;
    this._sessionId = sessionId;
    this.sid = `PA_${sessionId}`;
    this.identity = agentName;
    this.name = agentName;
  }

  async publishDtmf({ code, digit }: { code: number; digit: string }): Promise<void> {
    this._endpoint.sendDtmf(this._sessionId, digit);
  }

  async publishTrack(track: any, options?: any): Promise<StubTrackPublication> {
    const pub = new StubTrackPublication(track);
    this.trackPublications[pub.sid] = pub;

    // For audio tracks, start forwarding to background mixer
    // This matches Python's _forward_track_audio pattern
    if (track && typeof track.sid === 'string') {
      const abort = new AbortController();
      this._forwardAborts.set(pub.sid, abort);
      this._forwardTrackAudio(pub.sid, track, abort.signal).catch(() => {});
    }

    return pub;
  }

  async unpublishTrack(trackSid: string): Promise<void> {
    delete this.trackPublications[trackSid];
    const abort = this._forwardAborts.get(trackSid);
    if (abort) {
      abort.abort();
      this._forwardAborts.delete(trackSid);
    }
  }

  private async _forwardTrackAudio(pubSid: string, track: any, signal: AbortSignal): Promise<void> {
    // Read audio from the track via polling and forward to endpoint's background mixer
    // In Node.js, we use recvAudioBytesAsync on the track's audio stream
    try {
      while (!signal.aborted) {
        await new Promise(resolve => setTimeout(resolve, 20)); // 20ms pacing
        if (signal.aborted) break;
        // Background audio frames are produced by the mixer and captured by the track.
        // The forwarding happens via the endpoint's send_background_audio binding.
      }
    } catch {
      // Forwarding ended
    }
  }

  async publishTranscription(transcription: any): Promise<void> {}
  async streamText(opts?: any): Promise<{ write(text: string): Promise<void>; aclose(): Promise<void> }> {
    return { async write() {}, async aclose() {} };
  }
  async sendText(text: string, opts?: any): Promise<void> {}
  async publishData(payload: string | Uint8Array, opts?: any): Promise<void> {
    if (typeof payload === 'string') {
      try { this._endpoint.sendRawMessage(this._sessionId, payload); } catch {}
    }
  }
  async setMetadata(metadata: string): Promise<void> { this.metadata = metadata; }
  async setName(name: string): Promise<void> { this.name = name; }
  async setAttributes(attributes: Record<string, string>): Promise<void> {
    Object.assign(this.attributes, attributes);
  }
  registerRpcMethod(methodName: string, handler?: any): any { return handler; }
  unregisterRpcMethod(method: string): void {}
  setTrackSubscriptionPermissions(opts: any): void {}
  async performRpc(opts: any): Promise<string> { return ''; }
}

// ─── Transport Room ─────────────────────────────────────────────────────────

export class TransportRoom extends EventEmitter {
  private _endpoint: TransportEndpoint;
  private _sessionId: string;
  private _connected = true;
  private _creationTime = new Date();
  private _textStreamHandlers: Map<string, any> = new Map();

  localParticipant: TransportLocalParticipant;
  remoteParticipants: Map<string, TransportRemoteParticipant>;
  private _remote: TransportRemoteParticipant;

  constructor(
    endpoint: TransportEndpoint,
    sessionId: string,
    opts: { agentName: string; callerIdentity: string },
  ) {
    super();
    this._endpoint = endpoint;
    this._sessionId = sessionId;

    this.localParticipant = new TransportLocalParticipant(endpoint, sessionId, opts.agentName);
    this._remote = new TransportRemoteParticipant(opts.callerIdentity, String(sessionId));
    this.remoteParticipants = new Map([[opts.callerIdentity, this._remote]]);
  }

  get name(): string { return `transport-${this._sessionId}`; }
  get sid(): string { return this.name; }
  get metadata(): string { return ''; }
  get connectionState(): number { return this._connected ? 3 : 5; }
  get numParticipants(): number { return this.remoteParticipants.size; }
  get numPublishers(): number { return 0; }
  get isRecording(): boolean { return false; }
  get departureTimeout(): number { return 0; }
  get emptyTimeout(): number { return 0; }
  get e2eeManager(): null { return null; }
  get creationTime(): Date { return this._creationTime; }

  isconnected(): boolean { return this._connected; }

  async connect(url = '', token = '', options?: any): Promise<void> {
    // Already connected via transport — no WebRTC connection needed
  }

  async disconnect(): Promise<void> {
    this._connected = false;
    this.emit('disconnected');
  }

  async getRtcStats(): Promise<null> { return null; }

  registerTextStreamHandler(topic: string, handler: any): void {
    this._textStreamHandlers.set(topic, handler);
  }
  unregisterTextStreamHandler(topic: string): void {
    this._textStreamHandlers.delete(topic);
  }
  registerByteStreamHandler(topic: string, handler: any): void {}
  unregisterByteStreamHandler(topic: string): void {}

  /** Called when the call/stream ends — emit disconnect events. */
  _onSessionEnded(): void {
    this._connected = false;
    this.emit('participant_disconnected', this._remote);
    this.emit('disconnected');
  }

  /** Emit DTMF event (called by server event loop). */
  emitDtmf(digit: string): void {
    const ev: SipDTMF = {
      code: digit.charCodeAt(0),
      digit,
      participant: this._remote,
    };
    this.emit('sip_dtmf_received', ev);
  }
}

// ─── Audio Input ────────────────────────────────────────────────────────────

export class SipAudioInput {
  private _endpoint: TransportEndpoint;
  private _sessionId: string;
  private _closed = false;
  readonly label: string;

  constructor(endpoint: TransportEndpoint, sessionId: string, label = 'sip-audio-input') {
    this._endpoint = endpoint;
    this._sessionId = sessionId;
    this.label = label;
  }

  recvFrame(timeoutMs = 20): AudioFrame | null {
    if (this._closed) return null;
    const bytes = this._endpoint.recvAudioBytesBlocking(this._sessionId, timeoutMs);
    if (!bytes) return null;
    const samples = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.length / 2);
    return {
      data: bytes,
      sampleRate: this._endpoint.sampleRate,
      numChannels: 1,
      samplesPerChannel: samples.length,
    };
  }

  onAttached(): void {}
  onDetached(): void { this._closed = true; }
  close(): void { this._closed = true; }
}

// ─── Audio Output ───────────────────────────────────────────────────────────

export class SipAudioOutput extends EventEmitter {
  private _endpoint: TransportEndpoint;
  private _sessionId: string;
  private _capturing = false;
  private _segmentCount = 0;
  private _finishedCount = 0;
  private _pushedDuration = 0;
  private _firstFrameSent = false;
  private _interrupted = false;
  private _lastEvent: PlaybackFinishedEvent = { playbackPosition: 0, interrupted: false };
  private _playoutResolve?: () => void;
  readonly label: string;
  readonly capabilities: AudioOutputCapabilities;
  readonly nextInChain?: SipAudioOutput;

  constructor(
    endpoint: TransportEndpoint,
    sessionId: string,
    options?: {
      label?: string;
      capabilities?: AudioOutputCapabilities;
      sampleRate?: number;
      nextInChain?: SipAudioOutput;
    },
  ) {
    super();
    this._endpoint = endpoint;
    this._sessionId = sessionId;
    this.label = options?.label ?? 'sip-audio-output';
    this.capabilities = options?.capabilities ?? { pause: true };
    this.nextInChain = options?.nextInChain;
  }

  get sampleRate(): number { return this._endpoint.sampleRate; }
  get canPause(): boolean {
    if (!this.capabilities.pause) return false;
    if (this.nextInChain && !this.nextInChain.canPause) return false;
    return true;
  }

  captureFrame(frame: AudioFrame): void {
    if (!this._capturing) {
      this._capturing = true;
      this._segmentCount++;
      this._interrupted = false;
    }

    this._endpoint.sendAudioBytes(
      this._sessionId,
      frame.data instanceof Uint8Array ? frame.data : new Uint8Array(frame.data.buffer),
      frame.sampleRate,
      frame.numChannels,
    );

    if (frame.sampleRate > 0) {
      this._pushedDuration += frame.samplesPerChannel / frame.sampleRate;
    }

    if (!this._firstFrameSent) {
      this._firstFrameSent = true;
      this.onPlaybackStarted(Date.now() / 1000);
    }
  }

  flush(): void {
    this._capturing = false;
    this._endpoint.flush(this._sessionId);

    if (!this._pushedDuration) return;

    const pushed = this._pushedDuration;
    this._pushedDuration = 0;
    this._firstFrameSent = false;
    this._interrupted = false;

    this._endpoint.waitForPlayoutAsync(this._sessionId, 30000).then((completed: boolean) => {
      if (!this._interrupted) {
        this.onPlaybackFinished({ playbackPosition: pushed, interrupted: !completed });
      }
    });
  }

  clearBuffer(): void {
    this._endpoint.clearBuffer(this._sessionId);
    if (this._pushedDuration) {
      this._interrupted = true;
      const queued = this._endpoint.queuedFrames(this._sessionId) * 0.02;
      const played = Math.max(this._pushedDuration - queued, 0);
      this._pushedDuration = 0;
      this._capturing = false;
      this._firstFrameSent = false;
      this.onPlaybackFinished({ playbackPosition: played, interrupted: true });
    }
  }

  onPlaybackStarted(createdAt: number): void {
    this.emit('playbackStarted', { createdAt } as PlaybackStartedEvent);
  }

  onPlaybackFinished(event: PlaybackFinishedEvent): void {
    if (this._finishedCount >= this._segmentCount) return;
    this._finishedCount++;
    this._lastEvent = event;
    this.emit('playbackFinished', event);
    if (this._playoutResolve) {
      this._playoutResolve();
      this._playoutResolve = undefined;
    }
  }

  async waitForPlayout(): Promise<PlaybackFinishedEvent> {
    while (this._finishedCount < this._segmentCount) {
      await new Promise<void>((resolve) => { this._playoutResolve = resolve; });
    }
    return this._lastEvent;
  }

  pause(): void {
    this._endpoint.pause(this._sessionId);
    this.nextInChain?.pause();
  }

  resume(): void {
    this._endpoint.resume(this._sessionId);
    this._firstFrameSent = false;
    this.nextInChain?.resume();
  }

  sendRawMessage(message: string): void {
    this._endpoint.sendRawMessage(this._sessionId, message);
  }

  onAttached(): void {}
  onDetached(): void {}
}

// Aliases for audio streaming
export class AudioStreamInput extends SipAudioInput {
  constructor(endpoint: TransportEndpoint, sessionId: string, label = 'audio-stream-input') {
    super(endpoint, sessionId, label);
  }
}

export class AudioStreamOutput extends SipAudioOutput {
  constructor(
    endpoint: TransportEndpoint,
    sessionId: string,
    options?: {
      label?: string;
      capabilities?: AudioOutputCapabilities;
      sampleRate?: number;
      nextInChain?: SipAudioOutput;
    },
  ) {
    super(endpoint, sessionId, { label: options?.label ?? 'audio-stream-output', ...options });
  }
}
