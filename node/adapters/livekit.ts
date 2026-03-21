/**
 * LiveKit Agents TypeScript adapters for agent-transport.
 *
 * Implements LiveKit's AudioInput/AudioOutput abstract classes
 * using agent-transport's SipEndpoint or AudioStreamEndpoint.
 *
 * Usage:
 *   import { SipAudioInput, SipAudioOutput } from 'agent-transport-adapters/livekit';
 *   import { SipEndpoint } from 'agent-transport';
 *
 *   const ep = new SipEndpoint({ sipServer: 'phone.plivo.com' });
 *   const input = new SipAudioInput(ep, callId);
 *   const output = new SipAudioOutput(ep, callId);
 *
 *   // Connect to LiveKit AgentSession
 *   session.input.audio = input;
 *   session.output.audio = output;
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

// Transport endpoint interface (matches our Rust binding)
interface TransportEndpoint {
  sendAudioBytes(sessionId: number, audio: Uint8Array, sampleRate: number, numChannels: number): void;
  recvAudioBytesBlocking(sessionId: number, timeoutMs?: number): Uint8Array | null;
  flush(sessionId: number): void;
  clearBuffer(sessionId: number): void;
  pause(sessionId: number): void;
  resume(sessionId: number): void;
  queuedFrames(sessionId: number): number;
  waitForPlayout(sessionId: number, timeoutMs?: number): boolean;
  waitForPlayoutAsync(sessionId: number, timeoutMs?: number): Promise<boolean>;
  recvAudioBytesAsync(sessionId: number, timeoutMs?: number): Promise<Uint8Array | null>;
  sendRawMessage(sessionId: number, message: string): void;
  sampleRate: number;
}

/**
 * AudioInput — reads audio frames from SIP or audio streaming transport.
 * Matches LiveKit's AudioInput abstract class.
 */
export class SipAudioInput {
  private _endpoint: TransportEndpoint;
  private _sessionId: number;
  private _closed = false;
  readonly label: string;

  constructor(endpoint: TransportEndpoint, sessionId: number, label = 'sip-audio-input') {
    this._endpoint = endpoint;
    this._sessionId = sessionId;
    this.label = label;
  }

  /**
   * Read next audio frame (blocking in worker thread recommended).
   * Returns null when closed.
   */
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

/**
 * AudioOutput — sends audio frames to SIP or audio streaming transport.
 * Matches LiveKit's AudioOutput abstract class with:
 *   - Segment counting
 *   - Playback events (started/finished)
 *   - flush/clear_buffer with async playout wait
 *   - pause/resume
 */
export class SipAudioOutput {
  private _endpoint: TransportEndpoint;
  private _sessionId: number;
  private _capturing = false;
  private _segmentCount = 0;
  private _finishedCount = 0;
  private _pushedDuration = 0;
  private _firstFrameSent = false;
  private _interrupted = false;
  private _lastEvent: PlaybackFinishedEvent = { playbackPosition: 0, interrupted: false };
  private _listeners: { [event: string]: Array<(ev: any) => void> } = {};
  readonly label: string;
  readonly capabilities: AudioOutputCapabilities;
  readonly nextInChain?: SipAudioOutput;

  constructor(
    endpoint: TransportEndpoint,
    sessionId: number,
    options?: {
      label?: string;
      capabilities?: AudioOutputCapabilities;
      sampleRate?: number;
      nextInChain?: SipAudioOutput;
    },
  ) {
    this._endpoint = endpoint;
    this._sessionId = sessionId;
    this.label = options?.label ?? 'sip-audio-output';
    this.capabilities = options?.capabilities ?? { pause: true };
    this.nextInChain = options?.nextInChain;
  }

  get sampleRate(): number {
    return this._endpoint.sampleRate;
  }

  get canPause(): boolean {
    if (!this.capabilities.pause) return false;
    if (this.nextInChain && !this.nextInChain.canPause) return false;
    return true;
  }

  captureFrame(frame: AudioFrame): void {
    const first = !this._capturing;
    if (first) {
      this._capturing = true;
      this._segmentCount++;
      this._interrupted = false;
    }

    // Send to transport first (matches LiveKit: emit after frame queued)
    this._endpoint.sendAudioBytes(
      this._sessionId,
      frame.data instanceof Uint8Array ? frame.data : new Uint8Array(frame.data.buffer),
      frame.sampleRate,
      frame.numChannels,
    );

    if (frame.sampleRate > 0) {
      this._pushedDuration += frame.samplesPerChannel / frame.sampleRate;
    }

    // Emit playback_started AFTER send
    if (!this._firstFrameSent) {
      this._firstFrameSent = true;
      this.onPlaybackStarted(Date.now() / 1000);
    }
  }

  flush(): void {
    this._capturing = false;
    this._endpoint.flush(this._sessionId);

    if (!this._pushedDuration) return;

    // Wait for playout asynchronously
    const pushed = this._pushedDuration;
    this._pushedDuration = 0;
    this._firstFrameSent = false;

    // Use async napi task — runs on libuv thread pool, never blocks event loop.
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
      this.onPlaybackFinished({
        playbackPosition: played,
        interrupted: true,
      });
    }
  }

  onPlaybackStarted(createdAt: number): void {
    this._emit('playbackStarted', { createdAt });
  }

  onPlaybackFinished(event: PlaybackFinishedEvent): void {
    this._finishedCount++;
    this._lastEvent = event;
    this._emit('playbackFinished', event);
  }

  async waitForPlayout(): Promise<PlaybackFinishedEvent> {
    while (this._finishedCount < this._segmentCount) {
      await new Promise((resolve) => setTimeout(resolve, 10));
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

  // Simple EventEmitter
  on(event: string, listener: (ev: any) => void): void {
    if (!this._listeners[event]) this._listeners[event] = [];
    this._listeners[event].push(listener);
  }

  off(event: string, listener: (ev: any) => void): void {
    const l = this._listeners[event];
    if (l) this._listeners[event] = l.filter((f) => f !== listener);
  }

  private _emit(event: string, data: any): void {
    for (const listener of this._listeners[event] ?? []) {
      try { listener(data); } catch {}
    }
  }
}

// Aliases for audio streaming (same interface, different default labels)
export class AudioStreamInput extends SipAudioInput {
  constructor(endpoint: TransportEndpoint, sessionId: number, label = 'audio-stream-input') {
    super(endpoint, sessionId, label);
  }
}

export class AudioStreamOutput extends SipAudioOutput {
  constructor(
    endpoint: TransportEndpoint,
    sessionId: number,
    options?: {
      label?: string;
      capabilities?: AudioOutputCapabilities;
      sampleRate?: number;
      nextInChain?: SipAudioOutput;
    },
  ) {
    super(endpoint, sessionId, {
      label: options?.label ?? 'audio-stream-output',
      ...options,
    });
  }
}
