/**
 * AgentServer — SIP equivalent of LiveKit's AgentServer.
 *
 * Handles SIP registration, call routing, HTTP server (health/worker/metrics/call),
 * CLI (start/dev/debug), and call lifecycle management.
 *
 * Usage:
 *   const server = new AgentServer({ sipUsername: '...', sipPassword: '...' });
 *
 *   server.sipSession(async (ctx) => {
 *     const session = new voice.AgentSession({ ... });
 *     await ctx.start(session, { agent });
 *   });
 *
 *   server.run();
 */

import { createServer, type Server, type IncomingMessage, type ServerResponse } from 'node:http';
import { cpus } from 'node:os';
import { hostname } from 'node:os';
import { mkdirSync } from 'node:fs';
import { SipEndpoint } from 'agent-transport';
import { initializeLogger, InferenceRunner, runWithJobContext, log as agentLog, voice } from '@livekit/agents';
import { JobContext } from './session_context.js';

export class JobProcess {
  userData: Record<string, unknown> = {};
}

export interface AgentServerOptions {
  sipServer?: string;
  sipPort?: number;
  sipUsername: string;
  sipPassword: string;
  host?: string;
  port?: number;
  agentName?: string;
  auth?: (req: IncomingMessage) => boolean | Promise<boolean>;
}

type EntrypointFn = (ctx: JobContext) => Promise<void>;
type SetupFn = () => Record<string, unknown>;

/**
 * CPU load monitor — matches LiveKit's _DefaultLoadCalc / Python _LoadMonitor.
 * Samples cpu usage every 500ms, averaged over a 5-sample window (2.5s).
 */
class LoadMonitor {
  private samples: number[] = [];
  private readonly windowSize = 5;
  private timer: ReturnType<typeof setInterval>;

  constructor() {
    this.timer = setInterval(() => this.sample(), 500);
    this.timer.unref(); // don't block process exit
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
    if (this.samples.length > this.windowSize) {
      this.samples.shift();
    }
  }

  getLoad(): number {
    if (this.samples.length === 0) return 0;
    return this.samples.reduce((a, b) => a + b, 0) / this.samples.length;
  }

  stop(): void {
    clearInterval(this.timer);
  }
}

function getNodename(): string {
  return hostname();
}

export class AgentServer {
  private sipServer: string;
  private sipPort: number;
  private sipUsername: string;
  private sipPassword: string;
  private host: string;
  private port: number;
  private agentName: string;
  private authFn?: (req: IncomingMessage) => boolean | Promise<boolean>;

  private entrypointFn?: EntrypointFn;
  private setupFn?: SetupFn;
  private userdata: Record<string, unknown> = {};
  private proc = new JobProcess();
  private ep?: SipEndpoint;
  private activeCalls = new Map<string, { promise: Promise<void>; resolveEnded: () => void; room?: any }>();
  private httpServer?: Server;
  private loadMonitor = new LoadMonitor();
  private inferenceExecutor: any = null;

  // Prometheus-compatible metrics (in-memory, served as text)
  private sipCallsTotal = { inbound: 0, outbound: 0 };
  private sipCallDurations: number[] = [];

  constructor(opts: AgentServerOptions) {
    this.sipServer = opts.sipServer ?? process.env.SIP_DOMAIN ?? 'phone.plivo.com';
    this.sipPort = opts.sipPort ?? parseInt(process.env.SIP_PORT ?? '5060', 10);
    this.sipUsername = opts.sipUsername ?? process.env.SIP_USERNAME ?? '';
    this.sipPassword = opts.sipPassword ?? process.env.SIP_PASSWORD ?? '';
    this.host = opts.host ?? '0.0.0.0';
    this.port = opts.port ?? parseInt(process.env.PORT ?? '8080', 10);
    this.agentName = opts.agentName ?? 'sip-agent';
    this.authFn = opts.auth;
  }

  /**
   * Register setup function — runs once at startup.
   * Returns a dict of shared resources (VAD, turn detector, etc.)
   * available as ctx.userdata in each call.
   */
  setup(fn: SetupFn): void {
    this.setupFn = fn;
  }

  /**
   * LiveKit-compatible setup_fnc setter — accepts a function that receives a JobProcess.
   */
  set setupFnc(fn: (proc: JobProcess) => void | Record<string, unknown> | Promise<void | Record<string, unknown>>) {
    this.setupFn = fn as any;
  }

  /**
   * Register the call entrypoint — equivalent of @server.sip_session().
   */
  sipSession(fn: EntrypointFn): void {
    this.entrypointFn = fn;
  }

  /**
   * Run the server — registers SIP, starts HTTP, handles calls.
   */
  async run(): Promise<void> {
    // Handle unhandled rejections from LiveKit SDK TTS abort paths gracefully
    // (StreamAdapter rejects with undefined when TTS is cancelled during interruption)
    process.on('unhandledRejection', (reason) => {
      if (reason === undefined || reason === null) return; // TTS abort — benign
      console.error('Unhandled rejection:', reason);
    });

    // Strip tsx/ts-node loader hooks from execArgv before any child process forks
    // (pino-pretty worker, inference subprocess). These hooks corrupt IPC channels.
    // Must filter flag+value pairs: ['--require', '/path/tsx/...', '--import', 'file:///path/tsx/...']
    const origLen = process.execArgv.length;
    const cleanArgv: string[] = [];
    for (let i = 0; i < process.execArgv.length; i++) {
      const arg = process.execArgv[i];
      const next = process.execArgv[i + 1] ?? '';
      if ((arg === '--require' || arg === '--import') && (next.includes('tsx') || next.includes('ts-node'))) {
        i++; // skip the value too
      } else {
        cleanArgv.push(arg);
      }
    }
    process.execArgv = cleanArgv;
    console.log(`[init] execArgv: ${origLen} -> ${cleanArgv.length} (stripped ${origLen - cleanArgv.length} tsx hooks)`);

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

    this.configureLogging(mode);

    if (!this.sipUsername || !this.sipPassword) {
      console.error('Set SIP_USERNAME and SIP_PASSWORD environment variables');
      process.exit(1);
    }

    if (!this.entrypointFn) {
      console.error(
        'No SIP session entrypoint registered.\n' +
        'Register one using server.sipSession(fn), for example:\n' +
        '    server.sipSession(async (ctx) => {\n' +
        '        ...\n' +
        '    });'
      );
      process.exit(1);
    }

    // Initialize LiveKit logger and inference executor before setup.
    if (this.setupFn) {
      try {
        // Initialize logger (required by LiveKit SDK before creating any agents/models)
        initializeLogger({ pretty: true, level: 'info' });
        const runners = InferenceRunner?.registeredRunners;
        console.log('[init] Inference runners:', runners ? Object.keys(runners) : 'none');

        if (runners && Object.keys(runners).length > 0) {
          // InferenceProcExecutor is not publicly exported — resolve via absolute path
          let InferenceProcExecutor: any = null;
          try {
            const { createRequire } = await import('node:module');
            const require = createRequire(import.meta.url);
            const agentsPath = require.resolve('@livekit/agents');
            const execPath = agentsPath.replace(/dist\/index\.(c?)js$/, 'dist/ipc/inference_proc_executor.$1js');
            const mod = require(execPath);
            InferenceProcExecutor = mod?.InferenceProcExecutor ?? null;
          } catch { /* not available in this SDK version */ }

          if (InferenceProcExecutor) {
            this.inferenceExecutor = new InferenceProcExecutor({
              runners,
              initializeTimeout: 5 * 60 * 1000,
              closeTimeout: 5000,
              memoryWarnMB: 2000,
              memoryLimitMB: 0,
              pingInterval: 5000,
              pingTimeout: 60000,
              highPingThreshold: 2500,
            });
            await this.inferenceExecutor.start();
            await this.inferenceExecutor.initialize();
            console.log('Inference executor ready (turn detection models available)');
          }
        }

        // Run setup within job context stub so MultilingualModel() works
        if (this.inferenceExecutor) {
          const stub = { inferenceExecutor: this.inferenceExecutor } as any;
          await runWithJobContext(stub as any, () => this.callSetupFn());
        } else {
          await this.callSetupFn();
        }
      } catch (e) {
        console.warn('Setup failed:', (e as Error)?.stack || e);
        await this.callSetupFn();
      }
      console.log(`Setup complete: ${Object.keys(this.userdata).join(', ')}`);
    }

    // Create SIP endpoint and register
    this.ep = new SipEndpoint({ sipServer: this.sipServer });
    this.ep.register(this.sipUsername, this.sipPassword);

    // Wait for registration
    const regEvent = await this.waitForEvent(10000);
    if (!regEvent || regEvent.eventType !== 'registered') {
      console.error('SIP registration failed:', regEvent);
      process.exit(1);
    }
    console.log(`Registered as ${this.sipUsername}@${this.sipServer}:${this.sipPort}`);

    // Start HTTP server
    this.startHttpServer();
    console.log(`HTTP server on http://${this.host}:${this.port}`);

    // Start SIP event loop
    this.sipEventLoop();

    // Wait for shutdown signal
    await new Promise<void>((resolve) => {
      process.on('SIGINT', () => resolve());
      process.on('SIGTERM', () => resolve());
    });

    console.log('Shutting down...');

    // Drain active calls with 10-second timeout
    if (this.activeCalls.size > 0) {
      console.log(`Draining ${this.activeCalls.size} active call(s)...`);
      await Promise.race([
        Promise.allSettled([...this.activeCalls.values()].map((c) => c.promise)),
        new Promise<void>((resolve) => setTimeout(() => {
          console.warn('Shutdown timeout reached (10s), forcing exit');
          resolve();
        }, 10000)),
      ]);
    }

    this.loadMonitor.stop();
    if (this.inferenceExecutor) {
      try { await this.inferenceExecutor.close(); } catch {}
    }
    this.httpServer?.close();
    this.ep?.shutdown();
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

  // ─── Event dispatcher (single reader, no race conditions) ──────

  private pendingInbound = new Map<string, string>(); // sessionId → remoteUri
  // pendingOutbound removed — outbound calls start session directly after ep.call()

  private async sipEventLoop(): Promise<void> {
    while (true) {
      const ev = await this.waitForEvent(1000);
      if (!ev) continue;

      if (ev.eventType === 'incoming_call' && ev.session) {
        const sessionId = ev.session.sessionId;
        const remoteUri = ev.session.remoteUri;
        console.log(`Incoming call ${sessionId} from ${remoteUri}`);
        this.ep!.answer(sessionId);
        this.pendingInbound.set(sessionId, remoteUri);

      } else if (ev.eventType === 'call_media_active' && ev.sessionId !== undefined) {
        const sessionId = ev.sessionId;

        if (this.pendingInbound.has(sessionId)) {
          const remoteUri = this.pendingInbound.get(sessionId)!;
          this.pendingInbound.delete(sessionId);
          this.startCall(sessionId, remoteUri, 'inbound').catch((err) => {
            console.error(`Inbound call ${sessionId} failed:`, err);
            try { this.ep!.hangup(sessionId); } catch {}
          });
        }

      } else if (ev.eventType === 'call_terminated' && ev.session) {
        const sessionId = ev.session.sessionId;
        const reason = ev.reason ?? 'unknown';
        console.log(`Call ${sessionId} terminated (reason=${reason})`);

        // Emit participant_disconnected on Room facade (matches LiveKit WebRTC)
        // RoomIO._on_participant_disconnected will call _close_soon() → session closes
        const active = this.activeCalls.get(sessionId);
        if (active?.room) {
          active.room.emitParticipantDisconnected();
        }

        // Clean up pending
        this.pendingInbound.delete(sessionId);

        // Signal active call to end
        if (active) {
          active.resolveEnded();
        }

      } else if (ev.eventType === 'dtmf_received' && ev.sessionId) {
        // Route DTMF to Room facade (matches Python server pattern)
        const active = this.activeCalls.get(ev.sessionId);
        if (active?.room) {
          active.room.emitDtmf(ev.digit ?? '');
        }

      } else if (ev.eventType === 'beep_detected' && ev.sessionId) {
        const active = this.activeCalls.get(ev.sessionId);
        if (active?.room) {
          active.room.emit('beep_detected', { frequencyHz: ev.frequencyHz ?? 0, durationMs: ev.durationMs ?? 0 });
        }

      } else if (ev.eventType === 'beep_timeout' && ev.sessionId) {
        const active = this.activeCalls.get(ev.sessionId);
        if (active?.room) {
          active.room.emit('beep_timeout', {});
        }
      }
    }
  }

  private async startCall(sessionId: string, remoteUri: string, direction: 'inbound' | 'outbound'): Promise<void> {
    let resolveEnded!: () => void;
    const callEnded = new Promise<void>((r) => { resolveEnded = r; });

    const ctx = new JobContext({
      sessionId,
      remoteUri,
      direction,
      endpoint: this.ep!,
      userdata: this.userdata,
      agentName: this.agentName,
      callEnded,
      resolveCallEnded: resolveEnded,
      proc: this.proc,
    });

    const runCall = async () => {
      this.sipCallsTotal[direction]++;
      const callStart = performance.now();

      try {
        // Wrap in runWithJobContext so getJobContext().room works inside handler
        // (matches LiveKit WebRTC where entrypoint runs inside job context)
        const sessionDir = `/tmp/agent-sessions`;
        const stub = {
          room: ctx.room,
          job: { id: `job-${sessionId}`, agentName: this.agentName, enableRecording: false, room: { sid: ctx.room.sid, name: ctx.room.name } },
          _primaryAgentSession: undefined as any,
          sessionDirectory: sessionDir,
          proc: { executorType: null },
          inferenceExecutor: this.inferenceExecutor,
          initRecording: () => {},
          connect: async () => {},
          addShutdownCallback: () => {},
          shutdown: () => {},
          is_fake_job: () => false,
          isFakeJob: () => false,
          worker_id: 'local',
          workerId: 'local',
        };

        if (runWithJobContext) {
          await runWithJobContext(stub as any, () => this.entrypointFn!(ctx));
        } else {
          await this.entrypointFn!(ctx);
        }

        // Hook user state changes for debug logging (matches Python server behavior)
        if (ctx.session) {
          const { writeSync } = await import('node:fs');
          ctx.session.on('user_state_changed', (ev: any) => {
            try { writeSync(2, `Call ${sessionId} user: ${ev.oldState} -> ${ev.newState}\n`); } catch {}
          });
        }

        // Start Rust recording (stereo OGG/Opus at transport layer)
        // Captures full mix: agent voice + background audio + user audio
        try {
          mkdirSync(sessionDir, { recursive: true });
          this.ep!.startRecording(sessionId, `${sessionDir}/recording_${sessionId}.ogg`, true);
        } catch {}

        // Entrypoint returned — session.start() is non-blocking,
        // so wait for call to actually end (BYE or agent shutdown)
        await ctx.callEnded;
      } catch (e) {
        console.error(`Call ${sessionId} handler failed:`, e);
      } finally {
        const durationSec = (performance.now() - callStart) / 1000;
        this.sipCallDurations.push(durationSec);

        // Stop Rust recording if active
        try { this.ep!.stopRecording(sessionId); } catch {}

        // Log usage
        if (ctx.session) {
          try {
            const usage = (ctx.session as any).usage;
            if (usage) {
              console.log(`Call ${sessionId} usage:`, JSON.stringify(usage));
            }
          } catch {}
        }

        // Close session
        if (ctx.session) {
          try { await (ctx.session as any).close(); } catch {}
        }

        // Hangup
        try { this.ep!.hangup(sessionId); } catch {}

        this.activeCalls.delete(sessionId);
        console.log(`Call ${sessionId} ended (${direction}) duration=${durationSec.toFixed(1)}s`);
      }
    };

    const callPromise = runCall();
    this.activeCalls.set(sessionId, { promise: callPromise, resolveEnded, room: ctx.room });
  }

  // ─── HTTP server ────────────────────────────────────────────────

  private startHttpServer(): void {
    this.httpServer = createServer(async (req, res) => {
      const url = new URL(req.url ?? '/', `http://${req.headers.host}`);

      // Health — always open (no auth)
      if (url.pathname === '/') {
        if (!this.ep) {
          res.writeHead(503);
          res.end('SIP endpoint not initialized');
        } else {
          res.writeHead(200);
          res.end('OK');
        }
        return;
      }

      // Auth check for all other routes
      if (this.authFn) {
        let authed: boolean;
        try {
          authed = await this.authFn(req);
        } catch {
          authed = false;
        }
        if (!authed) {
          res.writeHead(401, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'unauthorized' }));
          return;
        }
      }

      if (url.pathname === '/worker') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          agent_name: this.agentName,
          worker_type: 'JT_SIP',
          worker_load: this.loadMonitor.getLoad(),
          active_jobs: this.activeCalls.size,
          sdk_version: this.getSdkVersion(),
          project_type: 'typescript',
          sip_server: this.sipServer,
          sip_port: this.sipPort,
        }));

      } else if (url.pathname === '/metrics') {
        res.writeHead(200, { 'Content-Type': 'text/plain; version=0.0.4' });
        res.end(this.generateMetrics());

      } else if (url.pathname === '/call' && req.method === 'POST') {
        let body = '';
        const MAX_BODY = 64 * 1024; // 64KB limit
        req.on('data', (chunk: Buffer) => {
          body += chunk;
          if (body.length > MAX_BODY) {
            res.writeHead(413, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: 'Request body too large' }));
            req.destroy();
          }
        });
        req.on('end', async () => {
          try {
            const data = JSON.parse(body);
            const rawTo = data.to;
            if (!rawTo) {
              res.writeHead(400, { 'Content-Type': 'application/json' });
              res.end(JSON.stringify({ error: "missing 'to' field" }));
              return;
            }

            // Normalize destination for SIP: add sip: prefix and @domain if missing
            let destination = rawTo;
            if (!destination.startsWith('sip:')) destination = 'sip:' + destination;
            if (!destination.split(':')[1]?.includes('@')) destination = destination + '@' + this.sipServer;

            const fromUri: string | undefined = data.from;
            const rawFrom = fromUri ?? '';
            const headers: Record<string, string> | undefined = data.headers;
            const wait: boolean = data.wait_until_answered ?? false;

            if (wait) {
              // Blocking mode: wait for call to connect
              const sessionId = this.ep!.call(destination, fromUri, headers);
              console.log(`Outbound call ${sessionId} to ${destination} connected (from=${fromUri ?? 'default'})`);
              this.startCall(sessionId, destination, 'outbound');
              res.writeHead(200, { 'Content-Type': 'application/json' });
              res.end(JSON.stringify({ session_id: sessionId, status: 'connected', to: rawTo, from: rawFrom }));
            } else {
              // Non-blocking (default): generate session_id upfront, dial in background
              const sessionId = 'c' + crypto.randomUUID().replace(/-/g, '').slice(0, 16);
              res.writeHead(200, { 'Content-Type': 'application/json' });
              res.end(JSON.stringify({ session_id: sessionId, status: 'dialing', to: rawTo, from: rawFrom }));

              setImmediate(async () => {
                try {
                  const returnedId = this.ep!.call(destination, fromUri, headers, sessionId);
                  console.log(`Outbound call ${returnedId} to ${destination} connected (from=${fromUri ?? 'default'})`);
                  this.startCall(returnedId, destination, 'outbound');
                } catch (err) {
                  console.warn('Outbound call %s to %s failed:', sessionId, destination, err);
                }
              });
            }
          } catch {
            res.writeHead(400, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: 'invalid JSON' }));
          }
        });

      } else {
        res.writeHead(404);
        res.end('Not Found');
      }
    });

    this.httpServer.listen(this.port, this.host);
  }

  // ─── Metrics ──────────────────────────────────────────────────

  private generateMetrics(): string {
    const node = getNodename();
    const lines: string[] = [];

    lines.push('# HELP lk_agents_active_job_count Active calls');
    lines.push('# TYPE lk_agents_active_job_count gauge');
    lines.push(`lk_agents_active_job_count{nodename="${node}"} ${this.activeCalls.size}`);

    lines.push('# HELP lk_agents_worker_load Worker load percentage');
    lines.push('# TYPE lk_agents_worker_load gauge');
    lines.push(`lk_agents_worker_load{nodename="${node}"} ${this.loadMonitor.getLoad()}`);

    lines.push('# HELP lk_agents_sip_calls_total Total SIP calls handled');
    lines.push('# TYPE lk_agents_sip_calls_total counter');
    lines.push(`lk_agents_sip_calls_total{nodename="${node}",direction="inbound"} ${this.sipCallsTotal.inbound}`);
    lines.push(`lk_agents_sip_calls_total{nodename="${node}",direction="outbound"} ${this.sipCallsTotal.outbound}`);

    lines.push('# HELP lk_agents_sip_call_duration_seconds SIP call duration in seconds');
    lines.push('# TYPE lk_agents_sip_call_duration_seconds histogram');
    const buckets = [1, 5, 10, 30, 60, 120, 300, 600];
    for (const b of buckets) {
      const count = this.sipCallDurations.filter((d) => d <= b).length;
      lines.push(`lk_agents_sip_call_duration_seconds_bucket{nodename="${node}",le="${b}"} ${count}`);
    }
    lines.push(`lk_agents_sip_call_duration_seconds_bucket{nodename="${node}",le="+Inf"} ${this.sipCallDurations.length}`);
    const sum = this.sipCallDurations.reduce((a, b) => a + b, 0);
    lines.push(`lk_agents_sip_call_duration_seconds_sum{nodename="${node}"} ${sum}`);
    lines.push(`lk_agents_sip_call_duration_seconds_count{nodename="${node}"} ${this.sipCallDurations.length}`);

    return lines.join('\n') + '\n';
  }

  private getSdkVersion(): string {
    try {
      // Try to get @livekit/agents version
      const pkg = require('@livekit/agents/package.json');
      return pkg.version ?? 'unknown';
    } catch {
      return 'unknown';
    }
  }

  // ─── Helpers ────────────────────────────────────────────────────

  private waitForEvent(timeoutMs: number): Promise<ReturnType<SipEndpoint['pollEvent']> | null> {
    return new Promise((resolve) => {
      const start = Date.now();
      const poll = () => {
        const ev = this.ep?.pollEvent();
        if (ev) {
          resolve(ev);
        } else if (Date.now() - start >= timeoutMs) {
          resolve(null);
        } else {
          setTimeout(poll, 50);
        }
      };
      poll();
    });
  }

  private configureLogging(mode: string): void {
    if (mode === 'debug') {
      process.env.RUST_LOG = process.env.RUST_LOG ?? 'debug';
    } else if (mode === 'dev') {
      process.env.RUST_LOG = process.env.RUST_LOG ?? 'info';
    }
  }
}
