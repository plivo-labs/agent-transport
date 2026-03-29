/** Type declarations for the agent-transport native module (napi-rs). */
declare module 'agent-transport' {
  export interface AudioFrame {
    data: number[];
    sampleRate: number;
    numChannels: number;
    samplesPerChannel: number;
  }

  export interface CallSession {
    callId: string;
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
    sampleRate?: number;
    jitterBuffer?: boolean;
    plc?: boolean;
    comfortNoise?: boolean;
  }

  export interface EventInfo {
    eventType: string;
    callId?: string;
    session?: CallSession;
    error?: string;
    reason?: string;
    digit?: string;
    method?: string;
    frequencyHz?: number;
    durationMs?: number;
  }

  export interface AudioStreamConfigJs {
    listenAddr?: string;
    plivoAuthId?: string;
    plivoAuthToken?: string;
    sampleRate?: number;
    autoHangup?: boolean;
  }

  export class SipEndpoint {
    constructor(config?: EndpointConfig);
    on(eventName: string, callback: (event: EventInfo) => void): void;
    register(username: string, password: string): void;
    unregister(): void;
    call(destUri: string, fromUri?: string, headers?: Record<string, string>): string;
    answer(callId: string): void;
    hangup(callId: string): void;
    sendAudio(callId: string, frame: AudioFrame): void;
    sendAudioBytes(callId: string, audio: Uint8Array, sampleRate: number, numChannels: number): void;
    sendBackgroundAudio(callId: string, audio: Uint8Array, sampleRate: number, numChannels: number): void;
    sendAudioNotify(callId: string, audio: Buffer, sampleRate: number, numChannels: number, notifyFn: () => void): void;
    recvAudio(callId: string): AudioFrame | null;
    recvAudioBytes(callId: string): Uint8Array | null;
    recvAudioBlocking(callId: string, timeoutMs?: number): AudioFrame | null;
    recvAudioBytesBlocking(callId: string, timeoutMs?: number): Uint8Array | null;
    recvAudioBytesAsync(callId: string, timeoutMs?: number): Promise<Buffer | null>;
    waitForPlayoutAsync(callId: string, timeoutMs?: number): Promise<boolean>;
    mute(callId: string): void;
    unmute(callId: string): void;
    pause(callId: string): void;
    resume(callId: string): void;
    clearBuffer(callId: string): void;
    flush(callId: string): void;
    waitForPlayout(callId: string, timeoutMs?: number): boolean;
    checkpoint(callId: string, name?: string): string;
    sendDtmf(callId: string, digits: string): void;
    sendRawMessage(callId: string, message: string): void;
    queuedFrames(callId: string): number;
    pollEvent(): EventInfo | null;
    detectBeep(callId: string, timeoutMs?: number, minDurationMs?: number, maxDurationMs?: number): void;
    startRecording(callId: string, path: string, stereo?: boolean): void;
    stopRecording(callId: string): void;
    get sampleRate(): number;
    get numChannels(): number;
    shutdown(): void;
  }

  export class AudioStreamEndpoint {
    constructor(config?: AudioStreamConfigJs);
    sendAudio(sessionId: string, frame: AudioFrame): void;
    sendAudioBytes(sessionId: string, audio: Uint8Array, sampleRate: number, numChannels: number): void;
    sendBackgroundAudio(sessionId: string, audio: Uint8Array, sampleRate: number, numChannels: number): void;
    sendAudioNotify(sessionId: string, audio: Buffer, sampleRate: number, numChannels: number, notifyFn: () => void): void;
    recvAudio(sessionId: string): AudioFrame | null;
    recvAudioBytes(sessionId: string): Uint8Array | null;
    recvAudioBlocking(sessionId: string, timeoutMs?: number): AudioFrame | null;
    recvAudioBytesBlocking(sessionId: string, timeoutMs?: number): Uint8Array | null;
    recvAudioBytesAsync(sessionId: string, timeoutMs?: number): Promise<Buffer | null>;
    waitForPlayoutAsync(sessionId: string, timeoutMs?: number): Promise<boolean>;
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
    hangup(sessionId: string): void;
    detectBeep(sessionId: string, timeoutMs?: number, minDurationMs?: number, maxDurationMs?: number): void;
    cancelBeepDetection(sessionId: string): void;
    pollEvent(): EventInfo | null;
    startRecording(sessionId: string, path: string, stereo?: boolean): void;
    stopRecording(sessionId: string): void;
    get sampleRate(): number;
    get numChannels(): number;
    shutdown(): void;
  }
}
