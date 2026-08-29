#!/usr/bin/env node
/**
 * Node SDK parity smoke for the local audio-stream e2e.
 *
 * The Python e2e owns the broad codec/control matrix. This script keeps a
 * smaller Node-specific signal that proves the built local Node package can use
 * the same raw AudioStreamEndpoint contract end to end.
 */

import { Buffer } from "node:buffer";
import { createServer } from "node:net";
import { setTimeout as sleep } from "node:timers/promises";

class E2EFailure extends Error {}

function parseArgs(argv) {
  const args = {
    ci: false,
    dryRun: false,
    direction: "both",
    listenHost: "127.0.0.1",
    listenPort: 0,
    minInboundSeconds: 0.25,
    minOutboundSeconds: 0.25,
    speechThreshold: 500,
    timeoutSeconds: 10,
  };

  for (let idx = 0; idx < argv.length; idx += 1) {
    const arg = argv[idx];
    const next = () => {
      idx += 1;
      if (idx >= argv.length) throw new E2EFailure(`Missing value for ${arg}`);
      return argv[idx];
    };

    if (arg === "--ci") args.ci = true;
    else if (arg === "--dry-run") args.dryRun = true;
    else if (arg === "--direction") {
      args.direction = next();
      if (!["both", "inbound", "outbound"].includes(args.direction)) {
        throw new E2EFailure(`Unsupported --direction: ${args.direction}`);
      }
    }
    else if (arg === "--listen-host") args.listenHost = next();
    else if (arg === "--listen-port") args.listenPort = Number(next());
    else if (arg === "--min-inbound-seconds") args.minInboundSeconds = Number(next());
    else if (arg === "--min-outbound-seconds") args.minOutboundSeconds = Number(next());
    else if (arg === "--speech-threshold") args.speechThreshold = Number(next());
    else if (arg === "--timeout-seconds") args.timeoutSeconds = Number(next());
    else throw new E2EFailure(`Unknown argument: ${arg}`);
  }

  return args;
}

async function loadAgentTransport() {
  try {
    return await import("../crates/agent-transport-node/index.js");
  } catch (error) {
    throw new E2EFailure(
      `Missing built Node SDK. Run from repo root after building crates/agent-transport-node: ${error.message}`,
    );
  }
}

function choosePort(host) {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.once("error", reject);
    server.listen(0, host, () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : null;
      server.close(() => {
        if (!port) reject(new E2EFailure("Could not choose free TCP port"));
        else resolve(port);
      });
    });
  });
}

function sineSamples(sampleRate, seconds, freq = 440, amplitude = 8000) {
  const count = Math.floor(sampleRate * seconds);
  const samples = new Array(count);
  for (let idx = 0; idx < count; idx += 1) {
    samples[idx] = Math.trunc(amplitude * Math.sin((2 * Math.PI * freq * idx) / sampleRate));
  }
  return samples;
}

function pcm16le(samples) {
  const out = Buffer.alloc(samples.length * 2);
  for (let idx = 0; idx < samples.length; idx += 1) {
    out.writeInt16LE(samples[idx], idx * 2);
  }
  return out;
}

function decodePcm16le(raw) {
  const buffer = Buffer.from(raw);
  const samples = [];
  for (let idx = 0; idx + 1 < buffer.length; idx += 2) {
    samples.push(buffer.readInt16LE(idx));
  }
  return samples;
}

function audioStats(samples, threshold) {
  const speechSamples = samples.filter((sample) => Math.abs(sample) >= threshold).length;
  return {
    samples: samples.length,
    speechPercent: samples.length ? (100 * speechSamples) / samples.length : 0,
  };
}

function startMessage(callId, streamId) {
  return {
    event: "start",
    start: {
      callId,
      streamId,
      mediaFormat: {
        encoding: "audio/x-l16",
        sampleRate: 8000,
      },
    },
    extra_headers: '{"X-PH-e2e": "audio-stream-node"}',
  };
}

function mediaMessage(samples) {
  return {
    event: "media",
    media: {
      payload: pcm16le(samples).toString("base64"),
    },
  };
}

async function decodeWsData(data) {
  if (typeof data === "string") return data;
  if (data instanceof ArrayBuffer) return Buffer.from(data).toString("utf8");
  if (ArrayBuffer.isView(data)) return Buffer.from(data.buffer, data.byteOffset, data.byteLength).toString("utf8");
  if (data && typeof data.arrayBuffer === "function") {
    return Buffer.from(await data.arrayBuffer()).toString("utf8");
  }
  return String(data);
}

class JsonWebSocket {
  constructor(ws) {
    this.ws = ws;
    this.queue = [];
    this.waiters = [];
    this.errors = [];

    ws.addEventListener("message", async (event) => {
      try {
        const msg = JSON.parse(await decodeWsData(event.data));
        const waiter = this.waiters.shift();
        if (waiter) waiter.resolve(msg);
        else this.queue.push(msg);
      } catch (error) {
        const waiter = this.waiters.shift();
        if (waiter) waiter.reject(error);
        else this.errors.push(error);
      }
    });

    ws.addEventListener("error", (event) => {
      const error = new E2EFailure(`WebSocket error: ${event.message || "unknown"}`);
      const waiter = this.waiters.shift();
      if (waiter) waiter.reject(error);
      else this.errors.push(error);
    });
  }

  sendJson(value) {
    this.ws.send(JSON.stringify(value));
  }

  readJson(timeoutMs) {
    if (this.errors.length) return Promise.reject(this.errors.shift());
    if (this.queue.length) return Promise.resolve(this.queue.shift());
    return new Promise((resolve, reject) => {
      const waiter = {
        resolve: (value) => {
          clearTimeout(timer);
          resolve(value);
        },
        reject: (error) => {
          clearTimeout(timer);
          reject(error);
        },
      };
      const timer = setTimeout(() => {
        this.waiters = this.waiters.filter((item) => item !== waiter);
        reject(new E2EFailure("Timed out waiting for websocket message"));
      }, timeoutMs);
      this.waiters.push(waiter);
    });
  }

  async readUntil(predicate, timeoutSeconds) {
    const deadline = Date.now() + timeoutSeconds * 1000;
    while (Date.now() < deadline) {
      const msg = await this.readJson(Math.max(100, deadline - Date.now()));
      console.log(`ws_event=${msg.event}`);
      if (predicate(msg)) return msg;
    }
    throw new E2EFailure("Timed out waiting for expected websocket message");
  }

  async close() {
    if (this.ws.readyState === WebSocket.CLOSED) return;
    await new Promise((resolve) => {
      const timer = setTimeout(resolve, 500);
      this.ws.addEventListener("close", () => {
        clearTimeout(timer);
        resolve();
      }, { once: true });
      this.ws.close();
    });
  }
}

async function connectWebSocket(uri, timeoutSeconds) {
  const deadline = Date.now() + timeoutSeconds * 1000;
  let lastError = null;

  while (Date.now() < deadline) {
    try {
      const ws = new WebSocket(uri);
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new E2EFailure("websocket open timeout")), 2000);
        ws.addEventListener("open", () => {
          clearTimeout(timer);
          resolve();
        }, { once: true });
        ws.addEventListener("error", (event) => {
          clearTimeout(timer);
          reject(new E2EFailure(`websocket connect failed: ${event.message || "unknown"}`));
        }, { once: true });
      });
      return new JsonWebSocket(ws);
    } catch (error) {
      lastError = error;
      await sleep(50);
    }
  }

  throw new E2EFailure(`Timed out connecting to ${uri}: ${lastError?.message || lastError}`);
}

async function waitEvent(endpoint, eventType, timeoutSeconds, predicate = null) {
  const deadline = Date.now() + timeoutSeconds * 1000;
  while (Date.now() < deadline) {
    const remainingMs = Math.max(1, deadline - Date.now());
    const event = await endpoint.waitForEvent(Math.min(remainingMs, 250));
    if (!event) continue;
    console.log(`event=${event.eventType}`);
    if (event.eventType === eventType && (!predicate || predicate(event))) return event;
    if (["call_terminated", "shutdown"].includes(event.eventType) && event.eventType !== eventType) {
      throw new E2EFailure(`Unexpected terminal event while waiting for ${eventType}: ${JSON.stringify(event)}`);
    }
  }
  throw new E2EFailure(`Timed out waiting for event: ${eventType}`);
}

async function recvEndpointAudio(endpoint, sessionId, minSeconds, threshold, timeoutSeconds) {
  const deadline = Date.now() + timeoutSeconds * 1000;
  const samples = [];
  const required = Math.floor(8000 * minSeconds);

  while (samples.length < required && Date.now() < deadline) {
    const frame = endpoint.recvAudioBytesBlocking(sessionId, 250);
    if (!frame) continue;
    samples.push(...decodePcm16le(frame));
  }

  const stats = audioStats(samples, threshold);
  if (samples.length < required) {
    throw new E2EFailure(`Insufficient inbound audio: got ${samples.length} samples, need ${required}`);
  }
  if (stats.speechPercent < 1) {
    throw new E2EFailure(`Insufficient inbound speech: ${stats.speechPercent.toFixed(2)}%`);
  }
  return stats;
}

async function sendInboundAudio(client, seconds) {
  const inbound = sineSamples(8000, seconds, 440);
  const frameSize = Math.floor(8000 * 0.02);
  for (let idx = 0; idx < inbound.length; idx += frameSize) {
    client.sendJson(mediaMessage(inbound.slice(idx, idx + frameSize)));
    await sleep(5);
  }
}

async function sendOutboundAudio(endpoint, sessionId, seconds) {
  const outbound = sineSamples(8000, seconds, 660);
  const frameSize = Math.floor(8000 * 0.02);
  for (let idx = 0; idx < outbound.length; idx += frameSize) {
    endpoint.sendAudioBytes(sessionId, pcm16le(outbound.slice(idx, idx + frameSize)), 8000, 1);
    await sleep(20);
  }
}

async function verifyOutboundAudio(client, endpoint, sessionId, minSeconds, threshold, timeoutSeconds) {
  endpoint.flush(sessionId);
  const requiredBytes = Math.floor(8000 * minSeconds * 2);
  const chunks = [];
  let bytes = 0;
  let checkpointName = null;
  const deadline = Date.now() + timeoutSeconds * 1000;

  while ((bytes < requiredBytes || checkpointName === null) && Date.now() < deadline) {
    const msg = await client.readJson(Math.max(100, deadline - Date.now()));
    console.log(`ws_event=${msg.event}`);
    if (msg.event === "playAudio") {
      const media = msg.media || {};
      if (media.contentType !== "audio/x-l16") {
        throw new E2EFailure(`Unexpected outbound contentType: ${media.contentType}`);
      }
      if (media.sampleRate !== 8000) {
        throw new E2EFailure(`Unexpected outbound sampleRate: ${media.sampleRate}`);
      }
      const chunk = Buffer.from(media.payload || "", "base64");
      chunks.push(chunk);
      bytes += chunk.length;
    } else if (msg.event === "checkpoint") {
      checkpointName = msg.name;
      client.sendJson({ event: "playedStream", name: checkpointName });
    }
  }

  if (bytes < requiredBytes) {
    throw new E2EFailure(`Insufficient outbound audio: got ${bytes} bytes, need ${requiredBytes}`);
  }
  if (!checkpointName) throw new E2EFailure("Did not receive outbound checkpoint");
  if (!(await endpoint.waitForPlayoutAsync(sessionId, Math.floor(timeoutSeconds * 1000)))) {
    throw new E2EFailure("Timed out waiting for playout acknowledgement");
  }

  const outboundSamples = decodePcm16le(Buffer.concat(chunks));
  const stats = audioStats(outboundSamples, threshold);
  if (stats.speechPercent < 1) {
    throw new E2EFailure(`Insufficient outbound speech: ${stats.speechPercent.toFixed(2)}%`);
  }
  console.log(`outbound_audio=${(outboundSamples.length / 8000).toFixed(3)}s speech=${stats.speechPercent.toFixed(2)}%`);
}

async function runSmoke(args) {
  const { AudioStreamEndpoint, initLogging } = await loadAgentTransport();
  initLogging(process.env.E2E_RUST_LOG || "info");

  const port = args.listenPort || await choosePort(args.listenHost);
  const listenAddr = `${args.listenHost}:${port}`;
  const uri = `ws://${listenAddr}`;
  console.log(`listen_addr=${listenAddr}`);

  const endpoint = new AudioStreamEndpoint({
    listenAddr,
    plivoAuthId: "",
    plivoAuthToken: "",
    inputSampleRate: 8000,
    outputSampleRate: 8000,
    autoHangup: false,
  });

  let client = null;
  try {
    client = await connectWebSocket(uri, args.timeoutSeconds);
    const callId = "audio-stream-node-e2e-call";
    const streamId = "audio-stream-node-e2e-stream";
    client.sendJson(startMessage(callId, streamId));

    const answered = await waitEvent(
      endpoint,
      "call_answered",
      args.timeoutSeconds,
      (event) => event.session?.callUuid === callId,
    );
    const sessionId = answered.session.sessionId;
    console.log(`session_id=${sessionId}`);

    if (args.direction === "both" || args.direction === "inbound") {
      await sendInboundAudio(client, args.minInboundSeconds + 0.1);
      const inbound = await recvEndpointAudio(
        endpoint,
        sessionId,
        args.minInboundSeconds,
        args.speechThreshold,
        args.timeoutSeconds,
      );
      console.log(`inbound_audio=${(inbound.samples / 8000).toFixed(3)}s speech=${inbound.speechPercent.toFixed(2)}%`);

      client.sendJson({ event: "dtmf", dtmf: { digit: "7" } });
      const dtmf = await waitEvent(
        endpoint,
        "dtmf_received",
        args.timeoutSeconds,
        (event) => event.sessionId === sessionId,
      );
      if (dtmf.digit !== "7" || dtmf.method !== "audio_stream") {
        throw new E2EFailure(`Unexpected DTMF event: ${JSON.stringify(dtmf)}`);
      }
      console.log("dtmf_received=7");
    }

    if (args.direction === "both" || args.direction === "outbound") {
      await sendOutboundAudio(endpoint, sessionId, args.minOutboundSeconds + 0.25);
      await verifyOutboundAudio(
        client,
        endpoint,
        sessionId,
        args.minOutboundSeconds,
        args.speechThreshold,
        args.timeoutSeconds,
      );

      endpoint.sendDtmf(sessionId, "42");
      const outboundDtmf = await client.readUntil((msg) => msg.event === "sendDTMF", args.timeoutSeconds);
      if (outboundDtmf.dtmf !== "42") {
        throw new E2EFailure(`Unexpected outbound DTMF payload: ${JSON.stringify(outboundDtmf)}`);
      }
      console.log("outbound_dtmf=42");
    }

    if (args.direction === "both") {
      endpoint.pause(sessionId);
      const clearOnPause = await client.readUntil((msg) => msg.event === "clearAudio", args.timeoutSeconds);
      if (clearOnPause.streamId !== streamId) {
        throw new E2EFailure(`Unexpected clearAudio streamId: ${JSON.stringify(clearOnPause)}`);
      }
      client.sendJson({ event: "clearedAudio" });
      endpoint.resume(sessionId);

      endpoint.clearBuffer(sessionId);
      await client.readUntil((msg) => msg.event === "clearAudio", args.timeoutSeconds);
    }

    client.sendJson({ event: "stop" });
    await waitEvent(
      endpoint,
      "call_terminated",
      args.timeoutSeconds,
      (event) => event.session?.sessionId === sessionId,
    );
  } finally {
    if (client) await client.close();
    endpoint.shutdown();
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  console.log(`dry_run=${args.dryRun}`);
  console.log("sdk=node");
  console.log(`direction=${args.direction}`);
  console.log("scenario=l16-8k-parity");

  if (args.dryRun) {
    if (typeof WebSocket !== "function") {
      throw new E2EFailure("Node runtime does not expose global WebSocket");
    }
    console.log("dry run passed");
    return;
  }

  await runSmoke(args);
  const label = args.direction === "both" ? "smoke" : args.direction;
  console.log(`node audio stream ${label} passed`);
}

main().catch((error) => {
  const message = error instanceof Error ? error.message : String(error);
  console.error(`E2E FAILED: ${message}`);
  process.exit(1);
});
