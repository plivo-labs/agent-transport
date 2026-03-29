/**
 * SipAudioInput — implements LiveKit's AudioInput interface for SIP/AudioStream.
 *
 * Duck-types LiveKit's AudioInput (not publicly exported from @livekit/agents).
 * Provides the same interface:
 * - stream: ReadableStream<AudioFrame> (consumed by AgentSession pipeline)
 * - close(): Promise<void>
 * - onAttached() / onDetached()
 *
 * Creates a ReadableStream from the Rust endpoint's recv_audio_bytes_async.
 * On stream end, pushes 0.5s silence to flush STT (matches LiveKit pattern).
 */

import { AudioFrame } from '@livekit/rtc-node';
import type { SipEndpoint } from 'agent-transport';

export class SipAudioInput {
  private endpoint: SipEndpoint;
  private callId: string;
  private closed = false;
  private attached = true;
  private frameCount = 0;
  private _stream: ReadableStream<AudioFrame>;

  constructor(endpoint: SipEndpoint, callId: string) {
    this.endpoint = endpoint;
    this.callId = callId;

    // Create ReadableStream that AgentSession will consume
    const self = this;
    this._stream = new ReadableStream<AudioFrame>({
      async pull(controller) {
        if (self.closed) {
          self.pushSilenceAndClose(controller);
          return;
        }

        try {
          const bytes: Buffer | null = await self.endpoint.recvAudioBytesAsync(self.callId, 20);
          if (!bytes || self.closed) {
            self.pushSilenceAndClose(controller);
            return;
          }

          if (self.attached) {
            const sr = self.endpoint.sampleRate;
            const samplesPerChannel = bytes.length / 2;
            const data = new Int16Array(bytes.buffer, bytes.byteOffset, samplesPerChannel);
            controller.enqueue(new AudioFrame(data, sr, 1, samplesPerChannel));

            self.frameCount++;
            if (self.frameCount === 1) {
              console.log(`SipAudioInput: first frame received sr=${sr} samples=${samplesPerChannel}`);
            } else if (self.frameCount % 250 === 0) {
              console.log(`SipAudioInput: ${self.frameCount} frames forwarded (${(self.frameCount * 0.02).toFixed(1)}s)`);
            }
          }
        } catch {
          self.pushSilenceAndClose(controller);
        }
      },
    });
  }

  private pushSilenceAndClose(controller: ReadableStreamDefaultController<AudioFrame>) {
    try {
      // Push 0.5s silence to flush STT (matches LiveKit _ParticipantAudioInputStream)
      const sr = this.endpoint.sampleRate;
      const silentSamples = sr / 2; // 0.5s at pipeline rate
      controller.enqueue(new AudioFrame(new Int16Array(silentSamples), sr, 1, silentSamples));
    } catch { /* stream already closed */ }
    try { controller.close(); } catch { /* already closed */ }
  }

  /** ReadableStream consumed by AgentSession pipeline. Matches AudioInput.stream */
  get stream(): ReadableStream<AudioFrame> {
    return this._stream;
  }

  onAttached(): void {
    this.attached = true;
  }

  onDetached(): void {
    this.attached = false;
  }

  async close(): Promise<void> {
    this.closed = true;
  }
}
