/**
 * SipAudioInput — extends LiveKit's AudioInput base class for SIP/AudioStream.
 *
 * Architecture matches Python SipAudioInput and LiveKit's _ParticipantAudioInputStream:
 * - Extends AudioInput (from @livekit/agents internal io.js)
 * - A forwarding task reads from Rust and pushes frames into a ReadableStream
 * - That ReadableStream is added to the base class multiStream
 * - On stream end, pushes 0.5s silence to flush STT, then closes
 */

import { AudioFrame } from '@livekit/rtc-node';
import type { ReadableStream as NodeReadableStream } from 'node:stream/web';
import { createRequire } from 'node:module';
import type { SipEndpoint, AudioStreamEndpoint } from 'agent-transport';

// AudioInput is not publicly exported from @livekit/agents — resolve internal path
// (same pattern used for InferenceProcExecutor in agent_server.ts)
const _require = createRequire(import.meta.url);
const _agentsPath = _require.resolve('@livekit/agents');
const _ioPath = _agentsPath.replace(/dist\/index\.(c?)js$/, 'dist/voice/io.$1js');
const { AudioInput: _AudioInputBase } = _require(_ioPath);
// eslint-disable-next-line @typescript-eslint/no-unsafe-declaration-merging
export interface SipAudioInput {
  multiStream: {
    addInputStream(source: NodeReadableStream<AudioFrame>): string;
    removeInputStream(id: string): Promise<void>;
  };
}

export class SipAudioInput extends _AudioInputBase {
  private endpoint: SipEndpoint | AudioStreamEndpoint;
  private sessionId: string;
  private closed = false;
  private attached = true;
  private frameCount = 0;
  private inputStreamId: string | null = null;

  constructor(endpoint: SipEndpoint | AudioStreamEndpoint, sessionId: string) {
    super();
    this.endpoint = endpoint;
    this.sessionId = sessionId;

    // Start immediately — the ReadableStream is pull-based, so recvAudioBytesAsync
    // won't be called until the downstream pipeline actually reads from the stream.
    // This matches how _ParticipantAudioInputStream adds its stream in the constructor.
    console.log(`[SipAudioInput] constructor: sessionId=${sessionId} sr=${endpoint.inputSampleRate}`);
    this.start();
  }

  /**
   * Start the forwarding task that reads from Rust and pushes to multiStream.
   * Matches Python's start() / _forward_audio pattern.
   */
  start(): void {
    if (this.inputStreamId !== null) return;
    console.log('[SipAudioInput] start: creating audio stream and adding to multiStream');

    const self = this;
    const sampleRate = this.endpoint.inputSampleRate;

    // Create a ReadableStream that pulls audio from Rust
    const audioStream = new ReadableStream<AudioFrame>({
      start(controller) {
        console.log('[SipAudioInput] ReadableStream.start() called');
      },
      async pull(controller) {
        if (self.frameCount === 0) console.log('[SipAudioInput] pull() invoked — reading from Rust...');
        if (self.closed) {
          self.pushSilenceAndClose(controller, sampleRate);
          return;
        }

        try {
          const bytes: Buffer | null = await self.endpoint.recvAudioBytesAsync(
            self.sessionId,
            20,
          );

          if (self.closed) {
            self.pushSilenceAndClose(controller, sampleRate);
            return;
          }

          // null = timeout (no audio available) — produce silence frame matching
          // WebRTC's AudioStream which always produces frames (C++ sends silence when no audio)
          if (!bytes) {
            const silenceSamples = Math.floor(sampleRate * 0.02); // 20ms silence
            controller.enqueue(
              new AudioFrame(new Int16Array(silenceSamples), sampleRate, 1, silenceSamples),
            );
            return;
          }

          if (self.attached) {
            const samplesPerChannel = bytes.length / 2;
            const data = new Int16Array(
              bytes.buffer,
              bytes.byteOffset,
              samplesPerChannel,
            );
            controller.enqueue(
              new AudioFrame(data, sampleRate, 1, samplesPerChannel),
            );

            self.frameCount++;
            if (self.frameCount === 1) {
              console.log(
                `SipAudioInput: first frame received sr=${sampleRate} samples=${samplesPerChannel}`,
              );
            } else if (self.frameCount % 250 === 0) {
              console.log(
                `SipAudioInput: ${self.frameCount} frames forwarded (${(self.frameCount * 0.02).toFixed(1)}s)`,
              );
            }
          }
        } catch {
          // Call ended (BYE received / stream closed)
          self.pushSilenceAndClose(controller, sampleRate);
        }
      },
    });

    // Add the audio stream to the base class multiStream
    // This is exactly how _ParticipantAudioInputStream does it
    // Cast needed: global ReadableStream vs node:stream/web ReadableStream
    this.inputStreamId = this.multiStream.addInputStream(
      audioStream as unknown as NodeReadableStream<AudioFrame>,
    );
    console.log(`[SipAudioInput] start: added to multiStream, id=${this.inputStreamId}`);
  }

  /**
   * Push 0.5s silence to flush STT, then close the stream.
   * Matches Python's _forward_audio finally block.
   */
  private pushSilenceAndClose(
    controller: ReadableStreamDefaultController<AudioFrame>,
    sampleRate: number,
  ): void {
    try {
      const silentSamples = Math.floor(sampleRate * 0.5);
      controller.enqueue(
        new AudioFrame(
          new Int16Array(silentSamples),
          sampleRate,
          1,
          silentSamples,
        ),
      );
    } catch {
      /* stream already closed */
    }
    try {
      controller.close();
    } catch {
      /* already closed */
    }
  }

  onAttached(): void {
    this.attached = true;
  }

  onDetached(): void {
    this.attached = false;
  }

  async close(): Promise<void> {
    this.closed = true;
    if (this.inputStreamId !== null) {
      await this.multiStream.removeInputStream(this.inputStreamId);
      this.inputStreamId = null;
    }
    await super.close();
  }
}
