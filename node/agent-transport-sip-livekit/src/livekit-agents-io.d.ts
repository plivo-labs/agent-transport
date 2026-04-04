/**
 * Type declarations for @livekit/agents internal voice/io module.
 *
 * AudioInput and AudioOutput are not publicly exported from @livekit/agents,
 * but we need to extend them (same as LiveKit's own _ParticipantAudioInputStream
 * and _ParticipantAudioOutput). This declaration file provides TypeScript types
 * for the deep import.
 */
declare module '@livekit/agents/dist/voice/io.js' {
  import type { AudioFrame } from '@livekit/rtc-node';
  import type { ReadableStream } from 'node:stream/web';
  import { EventEmitter } from 'node:events';

  export interface MultiInputStream<T> {
    get stream(): ReadableStream<T>;
    get inputCount(): number;
    get isClosed(): boolean;
    addInputStream(source: ReadableStream<T>): string;
    removeInputStream(id: string): Promise<void>;
    close(): Promise<void>;
  }

  export abstract class AudioInput {
    protected multiStream: MultiInputStream<AudioFrame>;
    get stream(): ReadableStream<AudioFrame>;
    close(): Promise<void>;
    onAttached(): void;
    onDetached(): void;
  }

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

  export abstract class AudioOutput extends EventEmitter {
    sampleRate?: number | undefined;
    protected readonly nextInChain?: AudioOutput | undefined;
    static readonly EVENT_PLAYBACK_STARTED: string;
    static readonly EVENT_PLAYBACK_FINISHED: string;
    protected readonly capabilities: AudioOutputCapabilities;

    constructor(
      sampleRate?: number | undefined,
      nextInChain?: AudioOutput | undefined,
      capabilities?: AudioOutputCapabilities,
    );

    get canPause(): boolean;
    captureFrame(_frame: AudioFrame): Promise<void>;
    waitForPlayout(): Promise<PlaybackFinishedEvent>;
    onPlaybackStarted(createdAt: number): void;
    onPlaybackFinished(options: PlaybackFinishedEvent): void;
    flush(): void;
    abstract clearBuffer(): void;
    onAttached(): void;
    onDetached(): void;
    pause(): void;
    resume(): void;
  }
}
