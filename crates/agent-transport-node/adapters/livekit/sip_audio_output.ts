/**
 * SipAudioOutput — extends LiveKit's AudioOutput base class for SIP/AudioStream.
 *
 * Matches WebRTC's ParticipantAudioOutput exactly:
 * - captureFrame sends to Rust with backpressure via the 0.2.0 async-id flow:
 *   `sendAudioAsync` returns an async-id, then we await the matching
 *   `audio_capture_complete` event (mirrors LiveKit's `capture_audio_frame`
 *   and the Python `_audio_source.py` reference).
 * - waitForPlayout uses `waitForPlayoutAsync` + `audio_playout_complete`
 *   (fires when buffer drains to empty).
 * - queuedDuration reads real Rust buffer state
 * - clearBuffer signals interruption (Rust emits `audio_capture_error` for
 *   every pending async-id, surfaced as a rejection on the awaited event).
 * - pause/resume controls Rust RTP output directly
 *
 * No timer heuristics — all playout tracking comes from Rust. Audio events
 * are delivered via {@link AudioEventBroker}, which the server event loop
 * feeds (single-reader) or which self-pumps for standalone use.
 */

import { AudioFrame } from '@livekit/rtc-node';
import { createRequire } from 'node:module';
import { Future, Task } from '@livekit/agents';
import type { SipEndpoint, AudioStreamEndpoint } from 'agent-transport';
import { AudioEventBroker, brokerFor } from './_audio_events.js';

// AudioOutput is not publicly exported from @livekit/agents — resolve internal path
const _require = createRequire(import.meta.url);
const _agentsPath = _require.resolve('@livekit/agents');
const _ioPath = _agentsPath.replace(/dist\/index\.(c?)js$/, 'dist/voice/io.$1js');
const { AudioOutput: _AudioOutputBase } = _require(_ioPath);

export class SipAudioOutput extends _AudioOutputBase {
  private endpoint: SipEndpoint | AudioStreamEndpoint;
  private sessionId: string;
  private broker: AudioEventBroker;

  private flushTask: Task<void> | null = null;
  private interruptedFuture = new Future<void>();
  private firstFrameEmitted = false;
  private pushedDuration = 0;
  private rustPaused = false;

  constructor(
    endpoint: SipEndpoint | AudioStreamEndpoint,
    sessionId: string,
    sampleRate?: number,
    nextInChain?: any,
  ) {
    const _sampleRate = sampleRate ?? endpoint.outputSampleRate;
    super(_sampleRate, nextInChain, { pause: true });
    this.endpoint = endpoint;
    this.sessionId = sessionId;
    this.broker = brokerFor(endpoint);
  }

  // -- captureFrame: matches WebRTC's ParticipantAudioOutput.captureFrame --

  async captureFrame(frame: AudioFrame): Promise<void> {
    // Segment tracking (WebRTC's super.captureFrame is sync void)
    super.captureFrame(frame);

    // Track pushed duration before the await so a sync caller's
    // pushedDuration accounting matches what the upstream sync
    // super.captureFrame would have done.
    this.pushedDuration += frame.samplesPerChannel / frame.sampleRate;

    // Push to Rust with backpressure via the 0.2.0 async-id flow. Mirrors
    // `_audio_source.py.capture_frame`: issue `sendAudioAsync` to get the
    // async-id, then await the matching `audio_capture_complete` event
    // (delivered through the broker). Matches WebRTC's
    // `await audioSource.captureFrame(frame)`.
    //
    // The broker buffers terminal events by async-id, so a completion that
    // the Rust immediate-emit path fires before `sendAudioAsync` returns is
    // not lost — closing the dispatch-before-wait race the napi note in
    // lib.rs warns about, without a pre-call subscribe primitive.
    const frameData = Buffer.from(frame.data.buffer, frame.data.byteOffset, frame.data.byteLength);
    const isFirstFrame = !this.firstFrameEmitted;
    try {
      const asyncId = this.endpoint.sendAudioAsync(
        this.sessionId,
        frameData,
        frame.sampleRate,
        frame.channels,
      );
      await this.broker.waitForCapture(asyncId);
    } catch {
      // Buffer full / session gone / cleared-while-in-flight — drop frame
      // silently (matches WebRTC behavior where captureFrame returns false
      // on buffer full without throwing, and matches `_audio_source.py`
      // surfacing cleared/flushed as a benign drop in the adapter).
    }

    // Emit playback-started AFTER the first frame has actually been
    // accepted by Rust (the napi callback fired). Upstream LiveKit fires
    // it after `await audioSource.captureFrame(frame)` returns, so the
    // TTFB metric reflects "first frame queued for playback" rather than
    // "first frame received from TTS". Firing it before the await would
    // overreport TTFB by ~50-100 ms.
    if (isFirstFrame) {
      this.firstFrameEmitted = true;
      this.onPlaybackStarted(Date.now());
    }
  }

  // -- flush: matches WebRTC's ParticipantAudioOutput.flush --

  flush(): void {
    super.flush();

    if (!this.pushedDuration) return;

    if (this.flushTask && !this.flushTask.done) {
      this.flushTask.cancel();
    }

    this.flushTask = Task.from((controller: any) => this.waitForPlayoutTask(controller));
  }

  // -- clearBuffer: matches WebRTC's ParticipantAudioOutput.clearBuffer --

  clearBuffer(): void {
    if (!this.pushedDuration) return;
    this.interruptedFuture.resolve();
  }

  // -- pause/resume: call Rust endpoint directly for immediate RTP effect --

  pause(): void {
    super.pause();
    if (!this.rustPaused) {
      // Update the flag AFTER the FFI call succeeds. If endpoint.pause()
      // throws (e.g., session already closed), the flag must stay false
      // so a subsequent pause() retries instead of being short-circuited
      // by the guard — otherwise Rust keeps sending audio while the TS
      // layer thinks it's paused.
      try {
        this.endpoint.pause(this.sessionId);
        this.rustPaused = true;
      } catch { /* session gone */ }
    }
  }

  resume(): void {
    super.resume();
    if (this.rustPaused) {
      try {
        this.endpoint.resume(this.sessionId);
        this.rustPaused = false;
      } catch { /* session gone */ }
    }
  }

  // -- waitForPlayoutTask: matches WebRTC's waitForPlayoutTask exactly --

  private async waitForPlayoutTask(abortController?: any): Promise<void> {
    const abortFuture = new Future<boolean>();
    const resolveAbort = () => {
      if (!abortFuture.done) abortFuture.resolve(true);
    };
    if (abortController?.signal) {
      abortController.signal.addEventListener('abort', resolveAbort);
    }

    // Wait for Rust playout — callback fires when buffer drains to empty.
    // Pause-aware: won't fire while paused (RTP loop doesn't drain).
    // Matches WebRTC's audioSource.waitForPlayout().
    this.waitForSourcePlayout().finally(() => {
      if (abortController?.signal) {
        abortController.signal.removeEventListener('abort', resolveAbort);
      }
      if (!abortFuture.done) abortFuture.resolve(false);
    });

    const interrupted = await Promise.race([
      abortFuture.await,
      this.interruptedFuture.await.then(() => true),
    ]);

    let pushedDuration = this.pushedDuration;

    if (interrupted) {
      // Real Rust buffer state — matches WebRTC's audioSource.queuedDuration.
      // Always exposed by both SipEndpoint and AudioStreamEndpoint via napi.
      const queuedMs = this.endpoint.queuedDurationMs(this.sessionId);
      pushedDuration = Math.max(this.pushedDuration - queuedMs / 1000, 0);
      this.clearSourceQueue();
    }

    this.pushedDuration = 0;
    this.interruptedFuture = new Future();
    this.firstFrameEmitted = false;
    this.onPlaybackFinished({
      playbackPosition: pushedDuration,
      interrupted,
    });
  }

  // -- Source helpers (using Rust APIs, matching WebRTC's audioSource) --

  /**
   * Wait for playout via the 0.2.0 async-id flow — no timer, no thread pool.
   * Mirrors `_audio_source.py.wait_for_playout`: `waitForPlayoutAsync`
   * registers an async-id (the completion always fires — immediately if the
   * buffer is already empty, deferred otherwise), then we await the matching
   * `audio_playout_complete` event through the broker.
   */
  private async waitForSourcePlayout(): Promise<void> {
    try {
      const asyncId = this.endpoint.waitForPlayoutAsync(this.sessionId);
      await this.broker.waitForPlayout(asyncId);
    } catch {
      // Session gone, or cleared/flushed while waiting (surfaced as an
      // audio_capture_error rejection) — treat as "playout done".
    }
  }

  /** Clear Rust buffer immediately. */
  private clearSourceQueue(): void {
    try {
      this.endpoint.clearBuffer(this.sessionId);
    } catch { /* ignore */ }
  }

  // -- lifecycle --

  async close(): Promise<void> {
    if (this.flushTask) this.flushTask.cancel();
  }

  onAttached(): void {
    if (this.nextInChain) this.nextInChain.onAttached();
  }

  onDetached(): void {
    if (this.nextInChain) this.nextInChain.onDetached();
  }
}
