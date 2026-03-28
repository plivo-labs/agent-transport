/**
 * SipAudioOutput — drop-in replacement for LiveKit's ParticipantAudioOutput.
 *
 * Implements the AudioOutput interface (duck-typed — base class is not publicly exported).
 * Matches ParticipantAudioOutput's behavior exactly:
 * - captureFrame() with segment tracking and backpressure
 * - flush() creates waitForPlayout task that races completion vs interruption
 * - clearBuffer() resolves interruptedFuture
 */

import { EventEmitter } from 'node:events';
import type { AudioFrame } from '@livekit/rtc-node';
import type { SipEndpoint } from 'agent-transport';

export interface PlaybackStartedEvent {
  createdAt: number;
}

export interface PlaybackFinishedEvent {
  playbackPosition: number;
  interrupted: boolean;
}

class Future<T = void> {
  private _resolve!: (value: T) => void;
  private _promise: Promise<T>;
  private _done = false;

  constructor() {
    this._promise = new Promise<T>((resolve) => { this._resolve = resolve; });
  }

  get done(): boolean { return this._done; }
  get await(): Promise<T> { return this._promise; }

  resolve(value?: T): void {
    if (!this._done) {
      this._done = true;
      this._resolve(value as T);
    }
  }
}

export class SipAudioOutput extends EventEmitter {
  static readonly EVENT_PLAYBACK_STARTED = 'playbackStarted';
  static readonly EVENT_PLAYBACK_FINISHED = 'playbackFinished';

  private endpoint: SipEndpoint;
  private callId: string;
  readonly sampleRate: number;
  readonly capabilities = { pause: true };

  // Matches ParticipantAudioOutput fields
  private pushedDuration = 0;
  private interruptedFuture = new Future();
  private firstFrameEmitted = false;
  private flushAbortController: AbortController | null = null;

  // Playout timing (replaces audioSource internal tracking)
  private lastCapture = 0;
  private qSize = 0;
  private playoutTimer: ReturnType<typeof setTimeout> | null = null;
  private playoutFuture: Future | null = null;

  // Segment tracking (reimplements AudioOutput base class)
  private _capturing = false;
  private playbackFinishedFuture = new Future();
  private playbackFinishedCount = 0;
  private playbackSegmentsCount = 0;
  private lastPlaybackEvent: PlaybackFinishedEvent = { playbackPosition: 0, interrupted: false };

  constructor(endpoint: SipEndpoint, callId: string, sampleRate?: number) {
    super();
    this.endpoint = endpoint;
    this.callId = callId;
    this.sampleRate = sampleRate ?? endpoint.sampleRate;
  }

  get canPause(): boolean {
    return true;
  }

  get queuedDuration(): number {
    return Math.max(this.qSize - (performance.now() / 1000 - this.lastCapture), 0);
  }

  /**
   * Capture an audio frame — matches ParticipantAudioOutput.captureFrame.
   */
  async captureFrame(frame: AudioFrame): Promise<void> {
    // Segment tracking (same as base class)
    if (!this._capturing) {
      this._capturing = true;
      this.playbackSegmentsCount++;
    }

    // Timing math (replaces audioSource internal tracking)
    const now = performance.now() / 1000;
    const elapsed = this.lastCapture === 0 ? 0 : now - this.lastCapture;
    this.qSize += frame.samplesPerChannel / this.sampleRate - elapsed;
    this.lastCapture = now;

    if (this.playoutTimer) clearTimeout(this.playoutTimer);
    if (!this.playoutFuture) this.playoutFuture = new Future();
    this.playoutTimer = setTimeout(() => this.releaseWaiter(), Math.max(this.qSize * 1000, 0));

    // Push to Rust with backpressure callback
    const frameData = Buffer.from(frame.data.buffer, frame.data.byteOffset, frame.data.byteLength);
    await new Promise<void>((resolve, reject) => {
      try {
        this.endpoint.sendAudioNotify(
          this.callId,
          frameData,
          frame.sampleRate,
          1, // mono
          () => resolve(),
        );
      } catch (e) {
        this.qSize -= frame.samplesPerChannel / this.sampleRate;
        if (this.qSize <= 0) this.releaseWaiter();
        reject(e);
      }
    });

    if (!this.firstFrameEmitted) {
      this.firstFrameEmitted = true;
      this.onPlaybackStarted(Date.now());
    }

    this.pushedDuration += frame.samplesPerChannel / frame.sampleRate;
  }

  /**
   * Flush — matches ParticipantAudioOutput.flush.
   */
  flush(): void {
    this._capturing = false;

    if (!this.pushedDuration) return;

    if (this.flushAbortController) {
      this.flushAbortController.abort();
    }

    this.flushAbortController = new AbortController();
    this.waitForPlayoutTask(this.flushAbortController);
  }

  /**
   * waitForPlayoutTask — matches ParticipantAudioOutput.waitForPlayoutTask.
   */
  private async waitForPlayoutTask(abortController: AbortController): Promise<void> {
    const abortFuture = new Future<boolean>();
    const resolveAbort = () => { if (!abortFuture.done) abortFuture.resolve(true); };
    abortController.signal.addEventListener('abort', resolveAbort);

    const waitPlayout = async (): Promise<boolean> => {
      if (this.playoutFuture) await this.playoutFuture.await;
      return false;
    };

    const interrupted = await Promise.race([
      waitPlayout(),
      this.interruptedFuture.await.then(() => true),
    ]);

    abortController.signal.removeEventListener('abort', resolveAbort);

    let pushedDuration = this.pushedDuration;
    if (interrupted) {
      pushedDuration = Math.max(this.pushedDuration - this.queuedDuration, 0);
      this.endpoint.clearBuffer(this.callId);
      this.releaseWaiter();
    }

    this.pushedDuration = 0;
    this.interruptedFuture = new Future();
    this.firstFrameEmitted = false;

    this.onPlaybackFinished({ playbackPosition: pushedDuration, interrupted });
  }

  /**
   * clearBuffer — matches ParticipantAudioOutput.clearBuffer.
   */
  clearBuffer(): void {
    if (!this.pushedDuration) return;
    this.interruptedFuture.resolve();
  }

  private releaseWaiter(): void {
    if (!this.playoutFuture) return;
    this.playoutFuture.resolve();
    this.lastCapture = 0;
    this.qSize = 0;
    this.playoutFuture = null;
  }

  // ─── Base class methods (reimplemented since AudioOutput is not exported) ──

  onPlaybackStarted(createdAt: number): void {
    this.emit(SipAudioOutput.EVENT_PLAYBACK_STARTED, { createdAt } satisfies PlaybackStartedEvent);
  }

  onPlaybackFinished(ev: PlaybackFinishedEvent): void {
    if (this.playbackFinishedCount >= this.playbackSegmentsCount) return;
    this.lastPlaybackEvent = ev;
    this.playbackFinishedCount++;
    this.playbackFinishedFuture.resolve();
    this.playbackFinishedFuture = new Future();
    this.emit(SipAudioOutput.EVENT_PLAYBACK_FINISHED, ev);
  }

  async waitForPlayout(): Promise<PlaybackFinishedEvent> {
    const target = this.playbackSegmentsCount;
    while (this.playbackFinishedCount < target) {
      await this.playbackFinishedFuture.await;
    }
    return this.lastPlaybackEvent;
  }

  pause(): void {
    this.endpoint.pause(this.callId);
  }

  resume(): void {
    this.endpoint.resume(this.callId);
  }

  onAttached(): void {}
  onDetached(): void {}

  async close(): Promise<void> {
    if (this.flushAbortController) this.flushAbortController.abort();
  }
}
