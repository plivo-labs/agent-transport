/**
 * SipAudioOutput — extends LiveKit's AudioOutput base class for SIP/AudioStream.
 *
 * Matches WebRTC's ParticipantAudioOutput exactly:
 * - captureFrame sends to Rust with backpressure (sendAudioNotify callback)
 * - waitForPlayout uses Rust callback (fires when buffer drains to empty)
 * - queuedDuration reads real Rust buffer state
 * - clearBuffer signals interruption
 * - pause/resume controls Rust RTP output directly
 *
 * No timer heuristics — all playout tracking comes from Rust.
 */

import { AudioFrame } from '@livekit/rtc-node';
import { createRequire } from 'node:module';
import { Future, Task } from '@livekit/agents';
import type { SipEndpoint, AudioStreamEndpoint } from 'agent-transport';

// AudioOutput is not publicly exported from @livekit/agents — resolve internal path
const _require = createRequire(import.meta.url);
const _agentsPath = _require.resolve('@livekit/agents');
const _ioPath = _agentsPath.replace(/dist\/index\.(c?)js$/, 'dist/voice/io.$1js');
const { AudioOutput: _AudioOutputBase } = _require(_ioPath);

// Log via fd 2 (stderr) — pino-pretty takes over stdout/fd 1 completely
import { writeSync } from 'node:fs';
const _log = (msg: string) => { try { writeSync(2, msg + '\n'); } catch {} };

export class SipAudioOutput extends _AudioOutputBase {
  private endpoint: SipEndpoint | AudioStreamEndpoint;
  private sessionId: string;

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
    _log(`SipAudioOutput constructor: sr=${_sampleRate} session=${sessionId}`);
    this.endpoint = endpoint;
    this.sessionId = sessionId;
  }

  // -- captureFrame: matches WebRTC's ParticipantAudioOutput.captureFrame --

  async captureFrame(frame: AudioFrame): Promise<void> {
    // Segment tracking (WebRTC doesn't await this — it's sync internally)
    super.captureFrame(frame);

    // Emit playback started on first frame
    if (!this.firstFrameEmitted) {
      this.firstFrameEmitted = true;
      this.onPlaybackStarted(Date.now());
    }

    // Track pushed duration
    this.pushedDuration += frame.samplesPerChannel / frame.sampleRate;

    // Push to Rust with backpressure — callback fires when buffer has space.
    // Matches WebRTC's await audioSource.captureFrame(frame).
    const frameData = Buffer.from(frame.data.buffer, frame.data.byteOffset, frame.data.byteLength);
    await new Promise<void>((resolve) => {
      try {
        this.endpoint.sendAudioNotify(
          this.sessionId,
          frameData,
          frame.sampleRate,
          frame.channels,
          () => resolve(),
        );
      } catch {
        // Buffer full or session gone — drop frame silently (matches WebRTC behavior
        // where captureFrame returns false on buffer full without throwing)
        resolve();
      }
    });
  }

  // -- flush: matches WebRTC's ParticipantAudioOutput.flush --

  flush(): void {
    super.flush();

    if (!this.pushedDuration) {
      _log('SipAudioOutput.flush: no pushed_duration, skipping');
      return;
    }

    if (this.flushTask && !this.flushTask.done) {
      _log('SipAudioOutput.flush: called while playback in progress');
      this.flushTask.cancel();
    }

    _log(`SipAudioOutput.flush: pushed_dur=${this.pushedDuration.toFixed(3)}s, creating waitForPlayout task`);
    this.flushTask = Task.from((controller: any) => this.waitForPlayoutTask(controller));
  }

  // -- clearBuffer: matches WebRTC's ParticipantAudioOutput.clearBuffer --

  clearBuffer(): void {
    if (!this.pushedDuration) {
      _log('SipAudioOutput.clearBuffer: no pushed_duration, skipping');
      return;
    }
    _log(`SipAudioOutput.clearBuffer: setting interrupted, pushed_dur=${this.pushedDuration.toFixed(3)}s`);
    this.interruptedFuture.resolve();
  }

  // -- pause/resume: call Rust endpoint directly for immediate RTP effect --

  pause(): void {
    super.pause();
    if (!this.rustPaused) {
      this.rustPaused = true;
      try {
        this.endpoint.pause(this.sessionId);
        _log('SipAudioOutput.pause: Rust paused');
      } catch { /* ignore */ }
    }
  }

  resume(): void {
    super.resume();
    if (this.rustPaused) {
      this.rustPaused = false;
      try {
        this.endpoint.resume(this.sessionId);
        _log('SipAudioOutput.resume: Rust resumed');
      } catch { /* ignore */ }
    }
  }

  // -- waitForPlayoutTask: matches WebRTC's waitForPlayoutTask exactly --

  private async waitForPlayoutTask(abortController?: any): Promise<void> {
    _log(`SipAudioOutput._waitForPlayout: starting (pushed=${this.pushedDuration.toFixed(3)}s)`);
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
      // Real Rust buffer state — matches WebRTC's audioSource.queuedDuration
      const queuedMs = (this.endpoint as any).queuedDurationMs?.(this.sessionId) ?? 0;
      pushedDuration = Math.max(this.pushedDuration - queuedMs / 1000, 0);
      this.clearSourceQueue();
      _log(`SipAudioOutput._waitForPlayout: interrupted, played=${pushedDuration.toFixed(3)}s`);
    } else {
      _log(`SipAudioOutput._waitForPlayout: completed, played=${pushedDuration.toFixed(3)}s`);
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

  /** Wait for playout via Rust callback — no timer, no thread pool. */
  private waitForSourcePlayout(): Promise<void> {
    return new Promise<void>((resolve) => {
      try {
        (this.endpoint as any).waitForPlayoutNotify(this.sessionId, () => resolve());
      } catch {
        resolve(); // Session gone
      }
    });
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
