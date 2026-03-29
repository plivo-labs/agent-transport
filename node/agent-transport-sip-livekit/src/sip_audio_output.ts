/**
 * SipAudioOutput — implements LiveKit's AudioOutput interface for SIP/AudioStream.
 *
 * Duck-types LiveKit's AudioOutput (not publicly exported from @livekit/agents).
 * Implements the exact same interface including:
 * - Segment counting (playbackSegmentsCount / playbackFinishedCount)
 * - captureFrame() with backpressure via sendAudioNotify
 * - flush() → waitForPlayout task racing completion vs interruption
 * - clearBuffer() → interrupt + onPlaybackFinished
 * - onPlaybackStarted / onPlaybackFinished events
 * - pause() / resume() / onAttached() / onDetached()
 * - nextInChain support for middleware stacking
 *
 * Playout timing replaces rtc.AudioSource (tracks queue depth for position).
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
  synchronizedTranscript?: string;
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
  readonly nextInChain?: SipAudioOutput;

  // Playout state (matches ParticipantAudioOutput)
  private pushedDuration = 0;
  private interruptedFuture = new Future();
  private firstFrameEmitted = false;
  private flushAbortController: AbortController | null = null;

  // Playout timing (replaces rtc.AudioSource internal tracking)
  private lastCapture = 0;
  private qSize = 0;
  private playoutTimer: ReturnType<typeof setTimeout> | null = null;
  private playoutFuture: Future | null = null;

  // Segment tracking (matches AudioOutput base class exactly)
  private _capturing = false;
  private playbackFinishedFuture = new Future();
  private playbackFinishedCount = 0;
  private playbackSegmentsCount = 0;
  private lastPlaybackEvent: PlaybackFinishedEvent = { playbackPosition: 0, interrupted: false };
  private _chainStartedCb: ((ev: PlaybackStartedEvent) => void) | null = null;
  private _chainFinishedCb: ((ev: PlaybackFinishedEvent) => void) | null = null;

  constructor(endpoint: SipEndpoint, callId: string, sampleRate?: number, nextInChain?: SipAudioOutput) {
    super();
    this.endpoint = endpoint;
    this.callId = callId;
    this.sampleRate = sampleRate ?? endpoint.outputSampleRate;
    this.nextInChain = nextInChain;

    // Chain event forwarding (matches AudioOutput base class)
    if (this.nextInChain) {
      this._chainStartedCb = (ev: PlaybackStartedEvent) => this.onPlaybackStarted(ev.createdAt);
      this._chainFinishedCb = (ev: PlaybackFinishedEvent) => this.onPlaybackFinished(ev);
      this.nextInChain.on(SipAudioOutput.EVENT_PLAYBACK_STARTED, this._chainStartedCb);
      this.nextInChain.on(SipAudioOutput.EVENT_PLAYBACK_FINISHED, this._chainFinishedCb);
    }
  }

  get canPause(): boolean {
    return this.capabilities.pause && (this.nextInChain?.canPause ?? true);
  }

  get queuedDuration(): number {
    return Math.max(this.qSize - (performance.now() / 1000 - this.lastCapture), 0);
  }

  /**
   * captureFrame — matches AudioOutput.captureFrame + ParticipantAudioOutput pattern.
   */
  async captureFrame(frame: AudioFrame): Promise<void> {
    // Segment tracking (same as AudioOutput base class)
    if (!this._capturing) {
      this._capturing = true;
      this.playbackSegmentsCount++;
    }

    // Timing math (replaces rtc.AudioSource internal tracking)
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
          frame.numChannels,
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
   * flush — matches AudioOutput.flush + ParticipantAudioOutput._waitForPlayout.
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
   * clearBuffer — matches AudioOutput.clearBuffer. Triggers interruption.
   */
  clearBuffer(): void {
    if (this.playoutTimer) {
      clearTimeout(this.playoutTimer);
      this.playoutTimer = null;
    }
    if (!this.pushedDuration) return;
    this.interruptedFuture.resolve();
  }

  /**
   * waitForPlayoutTask — races playout completion vs interruption.
   */
  private async waitForPlayoutTask(_abortController: AbortController): Promise<void> {
    const waitPlayout = async (): Promise<boolean> => {
      if (this.playoutFuture) await this.playoutFuture.await;
      return false;
    };

    const interrupted = await Promise.race([
      waitPlayout(),
      this.interruptedFuture.await.then(() => true),
    ]);

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

  private releaseWaiter(): void {
    if (!this.playoutFuture) return;
    this.playoutFuture.resolve();
    this.lastCapture = 0;
    this.qSize = 0;
    this.playoutFuture = null;
  }

  // ─── AudioOutput base class interface (segment tracking + events) ──────

  onPlaybackStarted(createdAt: number): void {
    this.emit(SipAudioOutput.EVENT_PLAYBACK_STARTED, { createdAt } satisfies PlaybackStartedEvent);
  }

  onPlaybackFinished(ev: PlaybackFinishedEvent): void {
    if (this.playbackFinishedCount >= this.playbackSegmentsCount) {
      console.warn('playback_finished called more times than playback segments were captured');
      return;
    }
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
    this.nextInChain?.pause();
  }

  resume(): void {
    this.endpoint.resume(this.callId);
    this.nextInChain?.resume();
  }

  onAttached(): void {
    this.nextInChain?.onAttached();
  }

  onDetached(): void {
    this.nextInChain?.onDetached();
  }

  async close(): Promise<void> {
    if (this.flushAbortController) this.flushAbortController.abort();
    if (this.playoutTimer) clearTimeout(this.playoutTimer);
    if (this.nextInChain) {
      if (this._chainStartedCb) this.nextInChain.off(SipAudioOutput.EVENT_PLAYBACK_STARTED, this._chainStartedCb);
      if (this._chainFinishedCb) this.nextInChain.off(SipAudioOutput.EVENT_PLAYBACK_FINISHED, this._chainFinishedCb);
    }
  }
}
