/** Type declarations for the agent-transport native module (napi-rs).
 *
 * This shadows the d.ts shipped in the napi crate so the TS adapter can be
 * type-checked from the source tree without depending on the published wheel.
 * Keep in sync with crates/agent-transport-node/index.d.ts.
 */
declare module 'agent-transport' {
  export function initLogging(filter?: string): void;

  export interface AudioFrame {
    data: number[];
    sampleRate: number;
    numChannels: number;
    samplesPerChannel: number;
  }

  export interface CallSession {
    sessionId: string;
    callUuid?: string;
    direction: string;
    state: string;
    remoteUri: string;
    localUri: string;
    extraHeaders: Record<string, string>;
  }

  export interface EndpointConfig {
    sipServer?: string;
    stunServer?: string;
    codecs?: string[];
    logLevel?: number;
    inputSampleRate?: number;
    outputSampleRate?: number;
    jitterBuffer?: boolean;
    plc?: boolean;
    comfortNoise?: boolean;
  }

  export interface EventInfo {
    eventType: string;
    sessionId?: string;
    session?: CallSession;
    error?: string;
    reason?: string;
    digit?: string;
    method?: string;
    frequencyHz?: number;
    durationMs?: number;
    /**
     * async_id for audioCaptureComplete / audioPlayoutComplete /
     * audioBufferDrained / audioCaptureError events. Received as a bigint
     * because u64 may exceed JS's safe-integer range.
     */
    asyncId?: bigint;
    /**
     * Set on audioCaptureComplete only: `true` if the completion was
     * synthesized by `clearBuffer()` (or buffer drop on session teardown),
     * `false` for a real send completion.
     */
    cancelled?: boolean;
  }

  export interface AudioStreamConfigJs {
    listenAddr?: string;
    plivoAuthId?: string;
    plivoAuthToken?: string;
    inputSampleRate?: number;
    outputSampleRate?: number;
    autoHangup?: boolean;
  }

  export class SipEndpoint {
    constructor(config?: EndpointConfig);
    register(username: string, password: string): void;
    unregister(): void;
    isRegistered(): boolean;
    call(destUri: string, fromUri?: string, headers?: Record<string, string>, sessionId?: string): string;
    answer(sessionId: string, code?: number): void;
    reject(sessionId: string, code?: number): void;
    hangup(sessionId: string): void;
    sendAudio(sessionId: string, frame: AudioFrame): void;
    sendAudioBytes(sessionId: string, audio: Uint8Array, sampleRate: number, numChannels: number): void;
    sendBackgroundAudio(sessionId: string, audio: Uint8Array, sampleRate: number, numChannels: number): void;
    /** Send audio without backpressure — push directly, drop if buffer full. */
    sendAudioNoBackpressure(sessionId: string, audio: Buffer, sampleRate: number, numChannels: number): void;
    /**
     * Push an audio frame and return the `async_id` (bigint) to await on the
     * event channel. Every call produces exactly one completion event:
     * `audio_capture_complete` (match on `asyncId`) on success, or
     * `audio_capture_error` on cancel/flush/drop. Callers MUST subscribe to
     * the event channel BEFORE calling.
     */
    sendAudioAsync(sessionId: string, audio: Buffer, sampleRate: number, numChannels: number): bigint;
    recvAudio(sessionId: string): AudioFrame | null;
    recvAudioBytes(sessionId: string): Uint8Array | null;
    recvAudioBlocking(sessionId: string, timeoutMs?: number): AudioFrame | null;
    recvAudioBytesBlocking(sessionId: string, timeoutMs?: number): Uint8Array | null;
    recvAudioBytesAsync(sessionId: string, timeoutMs?: number): Promise<Buffer | null>;
    /**
     * Timeout-bounded "wait for buffer to empty" Promise (Condvar-backed).
     * Resolves `true` if drained, `false` on timeout. Renamed in 0.2.0 from
     * `waitForPlayoutAsync`.
     */
    waitForPlayoutBlocking(sessionId: string, timeoutMs?: number): Promise<boolean>;
    /**
     * Register an `async_id` (bigint) for "buffer drained to empty" and await
     * the matching `audio_playout_complete` event (or `audio_capture_error`
     * on cancel/flush/drop). The completion event always fires.
     */
    waitForPlayoutAsync(sessionId: string): bigint;
    mute(sessionId: string): void;
    unmute(sessionId: string): void;
    pause(sessionId: string): void;
    resume(sessionId: string): void;
    hold(sessionId: string): void;
    unhold(sessionId: string): void;
    clearBuffer(sessionId: string): void;
    flush(sessionId: string): void;
    waitForPlayout(sessionId: string, timeoutMs?: number): boolean;
    checkpoint(sessionId: string, name?: string): string;
    sendDtmf(sessionId: string, digits: string, method?: string): void;
    sendDtmfAsync(sessionId: string, digits: string, method?: string): Promise<void>;
    sendInfo(sessionId: string, contentType: string, body: string): void;
    transfer(sessionId: string, destUri: string): void;
    transferAttended(sessionId: string, targetSessionId: string): void;
    sendRawMessage(sessionId: string, message: string): void;
    queuedFrames(sessionId: string): number;
    /**
     * Number of milliseconds of audio currently queued for outbound playback.
     * Mirrors WebRTC `audioSource.queuedDuration`.
     */
    queuedDurationMs(sessionId: string): number;
    pollEvent(): EventInfo | null;
    /**
     * Block waiting for the next event up to `timeoutMs`. Resolves to `null`
     * on timeout. Mirrors Python's `wait_for_event()`. Runs on napi's thread
     * pool — does not block the JS event loop.
     */
    waitForEvent(timeoutMs: number): Promise<EventInfo | null>;
    detectBeep(sessionId: string, timeoutMs?: number, minDurationMs?: number, maxDurationMs?: number): void;
    cancelBeepDetection(sessionId: string): void;
    startRecording(sessionId: string, path: string, stereo?: boolean): void;
    stopRecording(sessionId: string): void;
    get inputSampleRate(): number;
    get outputSampleRate(): number;
    get numChannels(): number;
    shutdown(): void;
  }

  export class AudioStreamEndpoint {
    constructor(config?: AudioStreamConfigJs);
    sendAudio(sessionId: string, frame: AudioFrame): void;
    sendAudioBytes(sessionId: string, audio: Uint8Array, sampleRate: number, numChannels: number): void;
    sendBackgroundAudio(sessionId: string, audio: Uint8Array, sampleRate: number, numChannels: number): void;
    /**
     * Push an audio frame and return the `async_id` (bigint) to await on the
     * event channel. See `SipEndpoint.sendAudioAsync` for the full contract.
     */
    sendAudioAsync(sessionId: string, audio: Buffer, sampleRate: number, numChannels: number): bigint;
    recvAudio(sessionId: string): AudioFrame | null;
    recvAudioBytes(sessionId: string): Uint8Array | null;
    recvAudioBlocking(sessionId: string, timeoutMs?: number): AudioFrame | null;
    recvAudioBytesBlocking(sessionId: string, timeoutMs?: number): Uint8Array | null;
    recvAudioBytesAsync(sessionId: string, timeoutMs?: number): Promise<Buffer | null>;
    /**
     * Timeout-bounded "wait for playout" Promise (Plivo checkpoint Condvar).
     * Resolves `true` if confirmed, `false` on timeout. Renamed in 0.2.0 from
     * `waitForPlayoutAsync`.
     */
    waitForPlayoutBlocking(sessionId: string, timeoutMs?: number): Promise<boolean>;
    /**
     * Register an `async_id` (bigint) for "buffer drained to empty" and await
     * the matching `audio_playout_complete` event. See
     * `SipEndpoint.waitForPlayoutAsync` for the full contract.
     */
    waitForPlayoutAsync(sessionId: string): bigint;
    mute(sessionId: string): void;
    unmute(sessionId: string): void;
    pause(sessionId: string): void;
    resume(sessionId: string): void;
    clearBuffer(sessionId: string): void;
    flush(sessionId: string): void;
    waitForPlayout(sessionId: string, timeoutMs?: number): boolean;
    checkpoint(sessionId: string, name?: string): string;
    sendDtmf(sessionId: string, digits: string): void;
    sendRawMessage(sessionId: string, message: string): void;
    queuedFrames(sessionId: string): number;
    /**
     * Number of milliseconds of audio currently queued for outbound playback.
     * Mirrors WebRTC `audioSource.queuedDuration`.
     */
    queuedDurationMs(sessionId: string): number;
    hangup(sessionId: string, authId?: string, authToken?: string): void;
    detectBeep(sessionId: string, timeoutMs?: number, minDurationMs?: number, maxDurationMs?: number): void;
    cancelBeepDetection(sessionId: string): void;
    pollEvent(): EventInfo | null;
    /**
     * Block waiting for the next event up to `timeoutMs`. Resolves to `null`
     * on timeout. Mirrors Python's `wait_for_event()`.
     */
    waitForEvent(timeoutMs: number): Promise<EventInfo | null>;
    startRecording(sessionId: string, path: string, stereo?: boolean): void;
    stopRecording(sessionId: string): void;
    get inputSampleRate(): number;
    get outputSampleRate(): number;
    get numChannels(): number;
    shutdown(): void;
  }
}
