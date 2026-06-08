/**
 * AudioStreamServer — Plivo audio streaming equivalent of AgentServer.
 *
 * Shutdown behavior: same force-exit model as AgentServer — hangup active
 * sessions, bounded cleanup, then `process.exit(0)`. Flush recordings /
 * observability per-session, not at server shutdown.
 *
 * No SIP credentials needed — Plivo connects to your WebSocket server.
 * Configure Plivo XML to return:
 *   <Response>
 *     <Stream bidirectional="true" keepCallAlive="true"
 *       contentType="audio/x-mulaw;rate=8000">
 *       wss://your-server:8765
 *     </Stream>
 *   </Response>
 *
 * Usage:
 *   const server = new AudioStreamServer({ listenAddr: '0.0.0.0:8765' });
 *   server.audioStreamSession(async (ctx) => {
 *     const session = new voice.AgentSession({ ... });
 *     ctx.session = session;
 *     await session.start({ agent, room: ctx.room });
 *   });
 *   server.run();
 */

import { createServer, type Server, type IncomingMessage } from 'node:http';
import { hostname, cpus } from 'node:os';
import { AudioStreamEndpoint } from 'agent-transport';
import { initializeLogger, InferenceRunner, runWithJobContext } from '@livekit/agents';
import { AudioStreamJobContext } from './audio_stream_context.js';
import { JobProcess } from './agent_server.js';
import { logObservabilityStatus } from './observability.js';
import { finalizeSession, startSessionRecording } from './_session_finalize.js';
import { runServerCleanup, forceShutdownAgentSession, installUnhandledRejectionHandler, registerSignalCleanup } from './_session_teardown.js';
import { brokerFor, isAudioEvent } from './_audio_events.js';

export interface AudioStreamServerOptions {
  listenAddr?: string;
  plivoAuthId?: string;
  plivoAuthToken?: string;
  sampleRate?: number;
  host?: string;
  port?: number;
  /** Stable developer-supplied identifier (typically UUID4). Mandatory:
   * obs's agents view keys on it, and agent_transport_sessions.agent_id
   * is NOT NULL after migration 013. Throws at construction if missing. */
  agentId?: string;
  agentName?: string;
  auth?: (req: IncomingMessage) => boolean | Promise<boolean>;
}

type EntrypointFn = (ctx: AudioStreamJobContext) => Promise<void>;
type SetupFn = () => Record<string, unknown>;

class LoadMonitor {
  private samples: number[] = [];
  private readonly windowSize = 5;
  private timer: ReturnType<typeof setInterval>;

  constructor() {
    this.timer = setInterval(() => this.sample(), 500);
    this.timer.unref();
  }

  private sample(): void {
    const cpuList = cpus();
    let idle = 0;
    let total = 0;
    for (const cpu of cpuList) {
      idle += cpu.times.idle;
      total += cpu.times.user + cpu.times.nice + cpu.times.sys + cpu.times.irq + cpu.times.idle;
    }
    const usage = 1 - idle / total;
    this.samples.push(usage);
    if (this.samples.length > this.windowSize) this.samples.shift();
  }

  getLoad(): number {
    if (this.samples.length === 0) return 0;
    return this.samples.reduce((a, b) => a + b, 0) / this.samples.length;
  }

  stop(): void {
    clearInterval(this.timer);
  }
}

export class AudioStreamServer {
  private listenAddr: string;
  private plivoAuthId: string;
  private plivoAuthToken: string;
  private sampleRate: number;
  private host: string;
  private port: number;
  private agentId: string;
  private agentName: string;
  private authFn?: (req: IncomingMessage) => boolean | Promise<boolean>;
  private entrypointFn?: EntrypointFn;
  private setupFn?: SetupFn;
  private userdata: Record<string, unknown> = {};
  private proc = new JobProcess();
  private ep?: AudioStreamEndpoint;
  private activeSessions = new Map<string, { promise: Promise<void>; resolveEnded: () => void; room?: any; ctx?: any }>();
  private httpServer?: Server;
  private loadMonitor = new LoadMonitor();
  private inferenceExecutor: any;
  private sessionCount = 0;
  private sessionDurations: number[] = [];
  // Cooperative shutdown flag for the WS event loop. The loop checks this
  // each iteration and breaks when set, so the run() promise can await the
  // loop's exit before tearing down. Without this, the infinite poll loop
  // would pin Node's libuv event loop forever.
  private shutdownRequested = false;

  constructor(opts: AudioStreamServerOptions) {
    this.listenAddr = opts.listenAddr ?? process.env.AUDIO_STREAM_ADDR ?? '0.0.0.0:8765';
    this.plivoAuthId = opts.plivoAuthId ?? process.env.PLIVO_AUTH_ID ?? '';
    this.plivoAuthToken = opts.plivoAuthToken ?? process.env.PLIVO_AUTH_TOKEN ?? '';
    this.sampleRate = opts.sampleRate ?? 8000;
    this.host = opts.host ?? '0.0.0.0';
    this.port = opts.port ?? parseInt(process.env.PORT ?? '8080');
    // agent_id (opt or AGENT_ID env) is OPTIONAL — the server runs fine
    // without it. It's only required to upload observability (obs keys on it;
    // the sessions table is NOT NULL), so when it's unset while
    // AGENT_OBSERVABILITY_URL is configured we warn at boot and skip the upload
    // (see uploadReport) rather than hard-break servers that don't use obs.
    this.agentId = opts.agentId ?? process.env.AGENT_ID ?? '';
    this.agentName = opts.agentName ?? 'audio-stream-agent';
    this.authFn = opts.auth;
  }

  setup(fn: SetupFn): void {
    this.setupFn = fn;
  }

  /**
   * LiveKit-compatible setup_fnc setter — accepts a function that receives a JobProcess.
   */
  set setupFnc(fn: (proc: JobProcess) => void | Record<string, unknown> | Promise<void | Record<string, unknown>>) {
    this.setupFn = fn as any;
  }

  audioStreamSession(fn: EntrypointFn): void {
    this.entrypointFn = fn;
  }

  async run(): Promise<void> {
    // Handle unhandled rejections from LiveKit SDK TTS abort paths gracefully.
    // Idempotent — safe if both servers run in one process.
    installUnhandledRejectionHandler();

    // Strip tsx/ts-node loader hooks from execArgv before any child process forks
    const cleanArgv: string[] = [];
    for (let i = 0; i < process.execArgv.length; i++) {
      const arg = process.execArgv[i];
      const next = process.execArgv[i + 1] ?? '';
      if ((arg === '--require' || arg === '--import') && (next.includes('tsx') || next.includes('ts-node'))) {
        i++;
      } else {
        cleanArgv.push(arg);
      }
    }
    process.execArgv = cleanArgv;

    const mode = process.argv[2] ?? 'start';

    // Handle download-files command (downloads model files for turn detection etc.)
    if (mode === 'download-files') {
      initializeLogger({ pretty: true, level: 'info' });
      const { Plugin, log: agentLog } = await import('@livekit/agents');
      const logger = agentLog();
      for (const plugin of Plugin.registeredPlugins) {
        logger.info(`Downloading files for ${plugin.title}`);
        await plugin.downloadFiles();
        logger.info(`Finished: ${plugin.title}`);
      }
      process.exit(0);
    }

    if (!this.entrypointFn) {
      console.error(
        'No audio stream session entrypoint registered.\n' +
        'Define one using server.audioStreamSession(async (ctx) => { ... })'
      );
      process.exit(1);
    }

    // Initialize inference executor (for turn detection)
    if (this.setupFn) {
      try {
        initializeLogger({ pretty: true, level: 'info' });
        const runners = InferenceRunner?.registeredRunners;

        if (runners && Object.keys(runners).length > 0) {
          let InferenceProcExecutor: any = null;
          try {
            const { createRequire } = await import('node:module');
            const require = createRequire(import.meta.url);
            const agentsPath = require.resolve('@livekit/agents');
            const execPath = agentsPath.replace(/dist\/index\.(c?)js$/, 'dist/ipc/inference_proc_executor.$1js');
            const mod = require(execPath);
            InferenceProcExecutor = mod?.InferenceProcExecutor ?? null;
          } catch { /* not available */ }

          if (InferenceProcExecutor) {
            this.inferenceExecutor = new InferenceProcExecutor({
              runners,
              initializeTimeout: 5 * 60_000,
              closeTimeout: 5000,
              memoryWarnMb: 2000,
              memoryLimitMb: 0,
              pingInterval: 5000,
              pingTimeout: 60_000,
              highPingThreshold: 2500,
            });
            await this.inferenceExecutor.start();
            await this.inferenceExecutor.initialize();
            console.log('Inference executor ready (turn detection models available)');
          }
        }

        // Run setup with job context stub for inference executor
        if (this.inferenceExecutor) {
          const stub = { inferenceExecutor: this.inferenceExecutor } as any;
          await runWithJobContext(stub, () => this.callSetupFn());
        } else {
          await this.callSetupFn();
        }
      } catch (e) {
        console.warn('Setup failed:', (e as Error)?.message || e);
        await this.callSetupFn();
      }
      console.log(`Setup complete: ${Object.keys(this.userdata).join(', ')}`);
    }

    // Create AudioStreamEndpoint (starts WS server)
    this.ep = new AudioStreamEndpoint({
      listenAddr: this.listenAddr,
      plivoAuthId: this.plivoAuthId,
      plivoAuthToken: this.plivoAuthToken,
      inputSampleRate: this.sampleRate,
      outputSampleRate: this.sampleRate,
    });
    // This server's eventLoop is the single reader of the endpoint event
    // channel — claim the audio-event broker so SipAudioOutput async-id
    // completions are dispatched to us (suppressing the broker self-pump).
    brokerFor(this.ep).claimFeeder();
    console.log(`Audio stream WebSocket server on ws://${this.listenAddr}`);

    // Start HTTP server
    this.startHttpServer();
    console.log(`HTTP server on http://${this.host}:${this.port}`);

    logObservabilityStatus(this.agentId);

    // Start event loop. Track the promise so we can await its exit during
    // shutdown — without this the infinite while loop pins libuv forever.
    const eventLoopDone = this.eventLoop();

    // On signal: hang up everything, run critical cleanup with short
    // timeouts, then process.exit. The Rust endpoint owns a background
    // thread that pins libuv, so natural exit isn't reliable — we force it.
    // Idempotent registration: signal listeners install once per process and
    // run every registered server's cleanup, so two servers (or a second
    // run()) don't double-register listeners.
    registerSignalCleanup(() => this.runCleanup());
    await eventLoopDone;
  }

  /**
   * Hang up active sessions, drain ancillary resources with short timeouts.
   *
   * Thin wrapper around {@link runServerCleanup} so the signal-handler path
   * stays a one-liner and tests can drive the shared cleanup helper without
   * loading the native `agent-transport` binding.
   */
  async runCleanup(): Promise<void> {
    this.shutdownRequested = true;
    await runServerCleanup({
      activeSessionIds: () => this.activeSessions.keys(),
      hangup: (id) => this.ep?.hangup(id),
      stopLoadMonitor: () => this.loadMonitor.stop(),
      inferenceExecutor: this.inferenceExecutor ?? null,
      closeHttpServer: () => {
        if (this.httpServer) {
          try { (this.httpServer as any).closeAllConnections?.(); } catch {}
          this.httpServer.close();
        }
      },
      shutdownEndpoint: () => this.ep?.shutdown(),
    });
  }

  /**
   * Call the setup function, supporting both LiveKit proc pattern and plain pattern.
   */
  private async callSetupFn(): Promise<void> {
    if (!this.setupFn) return;
    const result = await (this.setupFn as any)(this.proc);
    if (result && typeof result === 'object' && !(result instanceof Promise)) {
      Object.assign(this.proc.userData, result);
    }
    this.userdata = this.proc.userData;
  }

  // ─── Event loop ─────────────────────────────────────────────────────

  private async eventLoop(): Promise<void> {
    // With the post-answer event refactor, Plivo's WebSocket `start` maps
    // directly to `call_answered` — the session is created immediately.
    // No pending map, no wait-for-first-media gate.

    while (!this.shutdownRequested) {
      const ev = await this.waitForEvent(1000);
      if (!ev) continue;

      // Sentinel pushed by ep.shutdown() — wake immediately and exit cleanly.
      if (ev.eventType === 'shutdown') {
        break;
      }

      // Route async-id audio completion events to the broker (resolves the
      // pending SipAudioOutput captureFrame / waitForPlayout awaits).
      if (isAudioEvent(ev)) {
        brokerFor(this.ep!).dispatch(ev);
        continue;
      }

      if (ev.eventType === 'call_answered' && ev.session) {
        // Plivo WebSocket start → Rust fired CallAnswered → create agent.
        const session = ev.session;
        const sessionId = session.sessionId;
        const plivoCallUuid = session.remoteUri;
        const streamId = session.localUri ?? '';
        const extraHeaders = session.extraHeaders ?? {};
        if (this.activeSessions.has(sessionId)) {
          // Defensive: duplicate event or retry.
          continue;
        }
        console.log(
          `Audio stream session ${sessionId} connected (plivo_call_uuid=${plivoCallUuid}, stream_id=${streamId})`,
        );
        this.startSession(sessionId, plivoCallUuid, streamId, extraHeaders).catch((err) => {
          console.error(`Session ${sessionId} startup failed:`, err);
          try { this.ep!.hangup(sessionId); } catch {}
        });

      } else if (ev.eventType === 'call_terminated' && ev.session) {
        const sessionId = ev.session.sessionId;
        const reason = ev.reason ?? 'unknown';
        console.log(`Session ${sessionId} terminated (reason=${reason})`);

        const active = this.activeSessions.get(sessionId);
        // Synchronously begin tearing down the AgentSession so a buffered STT
        // transcript delivered after disconnect can't trigger a wasted LLM +
        // TTS turn on a dead session (issue #83). Must run before the Room
        // facade emits participant_disconnected (which schedules the close).
        forceShutdownAgentSession(active?.ctx?.session);

        // Emit participant_disconnected on Room facade
        if (active?.room) {
          active.room.emitParticipantDisconnected();
        }

        if (active) {
          active.resolveEnded();
        }

      } else if (ev.eventType === 'dtmf_received' && ev.sessionId) {
        const active = this.activeSessions.get(ev.sessionId);
        if (active?.room) {
          active.room.emitDtmf(ev.digit ?? '');
        }

      } else if (ev.eventType === 'beep_detected' && ev.sessionId) {
        const active = this.activeSessions.get(ev.sessionId);
        if (active?.room) {
          active.room.emit('beep_detected', { frequencyHz: ev.frequencyHz ?? 0, durationMs: ev.durationMs ?? 0 });
        }

      } else if (ev.eventType === 'beep_timeout' && ev.sessionId) {
        const active = this.activeSessions.get(ev.sessionId);
        if (active?.room) {
          active.room.emit('beep_timeout', {});
        }
      }
    }
  }

  private async startSession(sessionId: string, plivoCallUuid: string, streamId: string, extraHeaders: Record<string, string>): Promise<void> {
    let resolveEnded!: () => void;
    const callEnded = new Promise<void>((r) => { resolveEnded = r; });

    const ctx = new AudioStreamJobContext({
      sessionId,
      plivoCallUuid,
      streamId,
      direction: 'inbound',
      extraHeaders,
      endpoint: this.ep!,
      userdata: this.userdata,
      agentId: this.agentId,
      agentName: this.agentName,
      callEnded,
      resolveCallEnded: resolveEnded,
      proc: this.proc,
      inferenceExecutor: this.inferenceExecutor,
      enableRecording: false,
    });

    const runSession = async () => {
      this.sessionCount++;
      const sessionStart = performance.now();

      const sessionDir = ctx.sessionDirectory;
      let recPath: string | undefined;
      let recordingStartedAt: number | undefined;
      try {
        if (runWithJobContext) {
          await runWithJobContext(ctx as any, () => this.entrypointFn!(ctx));
        } else {
          await this.entrypointFn!(ctx);
        }

        ({ recordingPath: recPath, recordingStartedAt } = startSessionRecording(this.ep!, sessionId, sessionDir));

        // Hook user state changes for debug logging
        if (ctx.session) {
          const { writeSync } = await import('node:fs');
          ctx.session.on('user_state_changed', (ev: any) => {
            try { writeSync(2, `Session ${sessionId} user: ${ev.oldState} -> ${ev.newState}\n`); } catch {}
          });
        }

        // Wait for stream to end
        await ctx.callEnded;
      } catch (e) {
        console.error(`Session ${sessionId} handler failed:`, e);
      } finally {
        const durationSec = (performance.now() - sessionStart) / 1000;
        this.sessionDurations.push(durationSec);

        await finalizeSession({
          session: ctx.session,
          endpoint: this.ep!,
          sessionId,
          transport: 'audio_stream',
          agentId: this.agentId,
          agentName: this.agentName,
          accountId: ctx.accountId,
          metadata: ctx.metadata,
          direction: ctx.direction,
          recordingPath: recPath,
          recordingStartedAt,
        });

        try { this.ep!.hangup(sessionId); } catch {}

        ctx.room._onSessionEnded();
        this.activeSessions.delete(sessionId);
        console.log(`Session ${sessionId} ended, duration=${durationSec.toFixed(1)}s`);
      }
    };

    const sessionPromise = runSession();
    this.activeSessions.set(sessionId, { promise: sessionPromise, resolveEnded, room: ctx.room, ctx });
  }

  // ─── HTTP server ────────────────────────────────────────────────────

  private startHttpServer(): void {
    this.httpServer = createServer(async (req, res) => {
      if (this.authFn && req.url !== '/') {
        const ok = await this.authFn(req);
        if (!ok) { res.writeHead(401); res.end('Unauthorized'); return; }
      }

      if (req.url === '/') {
        res.writeHead(200); res.end('OK');
      } else if (req.url === '/worker') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          agent_name: this.agentName,
          worker_type: 'JT_AUDIO_STREAM',
          worker_load: this.loadMonitor.getLoad(),
          active_jobs: this.activeSessions.size,
          listen_addr: this.listenAddr,
        }));
      } else if (req.url === '/metrics') {
        res.writeHead(200, { 'Content-Type': 'text/plain' });
        res.end(this.generateMetrics());
      } else {
        res.writeHead(404); res.end('Not Found');
      }
    });

    this.httpServer.listen(this.port, this.host);
  }

  private generateMetrics(): string {
    const node = hostname();
    const lines: string[] = [];
    lines.push(`# HELP lk_agents_audio_stream_sessions_total Total audio stream sessions`);
    lines.push(`# TYPE lk_agents_audio_stream_sessions_total counter`);
    lines.push(`lk_agents_audio_stream_sessions_total{nodename="${node}"} ${this.sessionCount}`);
    lines.push(`# HELP lk_agents_active_job_count Active sessions`);
    lines.push(`# TYPE lk_agents_active_job_count gauge`);
    lines.push(`lk_agents_active_job_count{nodename="${node}"} ${this.activeSessions.size}`);
    lines.push(`# HELP lk_agents_cpu_load CPU load`);
    lines.push(`# TYPE lk_agents_cpu_load gauge`);
    lines.push(`lk_agents_cpu_load{nodename="${node}"} ${this.loadMonitor.getLoad().toFixed(4)}`);
    return lines.join('\n') + '\n';
  }

  private async waitForEvent(timeoutMs: number): Promise<any> {
    // Use the napi blocking waitForEvent (runs on the napi thread pool, so
    // the JS event loop is not blocked). Wakes immediately when
    // ep.shutdown() pushes the Shutdown sentinel — much faster than the
    // old setTimeout-based polling and no CPU spin between polls.
    if (!this.ep) return null;
    return await this.ep.waitForEvent(timeoutMs);
  }
}
