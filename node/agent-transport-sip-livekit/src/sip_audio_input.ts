/**
 * SipAudioInput — drop-in replacement for LiveKit's ParticipantAudioInputStream.
 *
 * Implements the AudioInput interface (duck-typed — base class is not publicly exported).
 * Creates a ReadableStream of AudioFrame from SIP/RTP that AgentSession reads from.
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

    this._stream = new ReadableStream<AudioFrame>({
      pull: async (controller) => {
        if (this.closed) {
          this.pushSilenceAndClose(controller);
          return;
        }

        try {
          const bytes: Buffer | null = await this.endpoint.recvAudioBytesAsync(this.callId, 20);
          if (bytes && this.attached) {
            const samplesPerChannel = bytes.length / 2;
            const data = new Int16Array(bytes.buffer, bytes.byteOffset, samplesPerChannel);
            controller.enqueue(new AudioFrame(data, 16000, 1, samplesPerChannel));

            this.frameCount++;
            if (this.frameCount === 1) {
              console.log(`SipAudioInput: first frame received sr=16000 samples=${samplesPerChannel}`);
            } else if (this.frameCount % 250 === 0) {
              console.log(`SipAudioInput: ${this.frameCount} frames forwarded (${(this.frameCount * 0.02).toFixed(1)}s)`);
            }
          }
        } catch {
          this.pushSilenceAndClose(controller);
        }
      },
    });
  }

  private pushSilenceAndClose(controller: ReadableStreamDefaultController<AudioFrame>) {
    try {
      const silentSamples = 8000; // 0.5s at 16kHz — flushes STT
      controller.enqueue(new AudioFrame(new Int16Array(silentSamples), 16000, 1, silentSamples));
    } catch { /* stream already closed */ }
    try { controller.close(); } catch { /* already closed */ }
  }

  /** ReadableStream consumed by AgentSession pipeline. */
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
