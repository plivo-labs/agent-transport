#!/usr/bin/env node
/**
 * SIP e2e smoke for the Node SDK.
 *
 * This mirrors the Python SIP smoke while keeping CI policy local to e2e:
 * E2E_* environment names, bounded waits, strict exit codes, redacted logs,
 * and a received WAV artifact.
 */

import { Buffer } from "node:buffer";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as sleep } from "node:timers/promises";

const SAMPLE_RATE = 8000;
const CHANNELS = 1;
const FRAME_SAMPLES = Math.floor((SAMPLE_RATE * 20) / 1000);
const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const DEFAULT_CLIP = resolve(REPO_ROOT, "examples/audio/caller_greeting_8k.wav");
const DEFAULT_OUTPUT = "/tmp/received_audio_node.wav";
const DEFAULT_CALLER_OUTPUT = "/tmp/received_audio_node_caller.wav";

class E2EFailure extends Error {}

function parseArgs(argv) {
  const args = {
    ci: false,
    dryRun: false,
    direction: "outbound",
    clip: DEFAULT_CLIP,
    output: DEFAULT_OUTPUT,
    callerOutput: DEFAULT_CALLER_OUTPUT,
    maxRegisterSeconds: 10,
    maxAnswerSeconds: 30,
    maxCallSeconds: 90,
    maxHangupSeconds: 10,
    callAttempts: 2,
    callRetrySeconds: 3,
    minReceivedSeconds: 1.0,
    minSpeechPercent: 1.0,
    speechThreshold: 500,
    dtmfDigit: "1",
  };

  for (let idx = 0; idx < argv.length; idx += 1) {
    const arg = argv[idx];
    const next = () => {
      idx += 1;
      if (idx >= argv.length) throw new E2EFailure(`Missing value for ${arg}`);
      return argv[idx];
    };

    if (arg === "--ci") args.ci = true;
    else if (arg === "--direction") {
      args.direction = next();
      if (!["outbound", "inbound"].includes(args.direction)) {
        throw new E2EFailure(`Unsupported --direction: ${args.direction}`);
      }
    }
    else if (arg === "--dry-run") args.dryRun = true;
    else if (arg === "--clip") args.clip = next();
    else if (arg === "--output") args.output = next();
    else if (arg === "--caller-output") args.callerOutput = next();
    else if (arg === "--max-register-seconds") args.maxRegisterSeconds = Number(next());
    else if (arg === "--max-answer-seconds") args.maxAnswerSeconds = Number(next());
    else if (arg === "--max-call-seconds") args.maxCallSeconds = Number(next());
    else if (arg === "--max-hangup-seconds") args.maxHangupSeconds = Number(next());
    else if (arg === "--call-attempts") args.callAttempts = Number(next());
    else if (arg === "--call-retry-seconds") args.callRetrySeconds = Number(next());
    else if (arg === "--min-received-seconds") args.minReceivedSeconds = Number(next());
    else if (arg === "--min-speech-percent") args.minSpeechPercent = Number(next());
    else if (arg === "--speech-threshold") args.speechThreshold = Number(next());
    else if (arg === "--dtmf-digit") args.dtmfDigit = next();
    else throw new E2EFailure(`Unknown argument: ${arg}`);
  }

  return args;
}

function e2eEnv(args) {
  const env = {
    a: {
      username: process.env.E2E_SIP_USERNAME_A || "",
      password: process.env.E2E_SIP_PASSWORD_A || "",
      destUri: process.env.E2E_SIP_DEST_URI_A || "",
    },
    b: {
      username: process.env.E2E_SIP_USERNAME_B || "",
      password: process.env.E2E_SIP_PASSWORD_B || "",
      destUri: process.env.E2E_SIP_DEST_URI_B || "",
    },
  };
  const sipDomain = process.env.E2E_SIP_DOMAIN || "phone.plivo.com";
  const rustLog = process.env.E2E_RUST_LOG || "info";

  const missing = [
    ["E2E_SIP_USERNAME_A", env.a.username],
    ["E2E_SIP_PASSWORD_A", env.a.password],
    ...(args.direction === "outbound"
      ? [["E2E_SIP_DEST_URI_A", env.a.destUri]]
      : [
          ["E2E_SIP_USERNAME_B", env.b.username],
          ["E2E_SIP_PASSWORD_B", env.b.password],
          ["E2E_SIP_DEST_URI_B", env.b.destUri],
        ]),
  ].filter(([, value]) => !value).map(([name]) => name);

  if (missing.length && !args.dryRun) {
    throw new E2EFailure(`Missing required env vars: ${missing.join(", ")}`);
  }

  return { ...env, sipDomain, rustLog };
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

function readChunk(buffer, offset) {
  if (offset + 8 > buffer.length) return null;
  const id = buffer.toString("ascii", offset, offset + 4);
  const size = buffer.readUInt32LE(offset + 4);
  return { id, size, dataOffset: offset + 8, nextOffset: offset + 8 + size + (size % 2) };
}

function loadWav(path) {
  const buffer = readFileSync(path);
  if (buffer.toString("ascii", 0, 4) !== "RIFF" || buffer.toString("ascii", 8, 12) !== "WAVE") {
    throw new E2EFailure(`Unsupported WAV file: ${path}`);
  }

  let fmt = null;
  let data = null;
  for (let offset = 12; offset < buffer.length;) {
    const chunk = readChunk(buffer, offset);
    if (!chunk) break;
    if (chunk.id === "fmt ") fmt = chunk;
    if (chunk.id === "data") data = chunk;
    offset = chunk.nextOffset;
  }

  if (!fmt || !data) throw new E2EFailure(`WAV missing fmt/data chunk: ${path}`);

  const format = buffer.readUInt16LE(fmt.dataOffset);
  const channels = buffer.readUInt16LE(fmt.dataOffset + 2);
  const sampleRate = buffer.readUInt32LE(fmt.dataOffset + 4);
  const bitsPerSample = buffer.readUInt16LE(fmt.dataOffset + 14);
  if (format !== 1 || bitsPerSample !== 16 || channels < 1) {
    throw new E2EFailure(`Unsupported WAV format in ${path}: format=${format} channels=${channels} bits=${bitsPerSample}`);
  }

  const samples = [];
  const frameBytes = channels * 2;
  const end = data.dataOffset + data.size;
  for (let offset = data.dataOffset; offset + frameBytes <= end; offset += frameBytes) {
    samples.push(buffer.readInt16LE(offset));
  }

  if (sampleRate === SAMPLE_RATE) return samples;

  const outCount = Math.floor((samples.length / sampleRate) * SAMPLE_RATE);
  const resampled = [];
  for (let idx = 0; idx < outCount; idx += 1) {
    const src = Math.floor((idx * (samples.length - 1)) / Math.max(1, outCount - 1));
    resampled.push(samples[src]);
  }
  return resampled;
}

function pcm16le(samples) {
  const out = Buffer.alloc(samples.length * 2);
  for (let idx = 0; idx < samples.length; idx += 1) {
    const sample = Math.max(-32768, Math.min(32767, samples[idx]));
    out.writeInt16LE(sample, idx * 2);
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

function saveWav(path, samples) {
  mkdirSync(dirname(path), { recursive: true });
  const data = pcm16le(samples);
  const header = Buffer.alloc(44);
  header.write("RIFF", 0, "ascii");
  header.writeUInt32LE(36 + data.length, 4);
  header.write("WAVE", 8, "ascii");
  header.write("fmt ", 12, "ascii");
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(CHANNELS, 22);
  header.writeUInt32LE(SAMPLE_RATE, 24);
  header.writeUInt32LE(SAMPLE_RATE * CHANNELS * 2, 28);
  header.writeUInt16LE(CHANNELS * 2, 32);
  header.writeUInt16LE(16, 34);
  header.write("data", 36, "ascii");
  header.writeUInt32LE(data.length, 40);
  writeFileSync(path, Buffer.concat([header, data]));
}

function audioStats(samples, threshold = 500) {
  if (!samples.length) return { peak: 0, rms: 0, speechPercent: 0 };
  let peak = 0;
  let sumSquares = 0;
  let speechSamples = 0;
  for (const sample of samples) {
    const abs = Math.abs(sample);
    peak = Math.max(peak, abs);
    sumSquares += sample * sample;
    if (abs > threshold) speechSamples += 1;
  }
  return {
    peak,
    rms: Math.sqrt(sumSquares / samples.length),
    speechPercent: (100 * speechSamples) / samples.length,
  };
}

function sessionIdFromEvent(event) {
  return event.sessionId || event.session?.sessionId || event.session?.session_id || "";
}

async function waitForEventType(endpoint, eventType, timeoutSeconds, label = "event", expectedSessionId = "") {
  const deadline = Date.now() + timeoutSeconds * 1000;
  while (Date.now() < deadline) {
    const remainingMs = Math.max(1, deadline - Date.now());
    const event = await endpoint.waitForEvent(Math.min(remainingMs, 500));
    if (!event) continue;
    const eventSessionId = sessionIdFromEvent(event);
    console.log(`[${label}] event=${event.eventType}`);
    if (event.eventType === eventType) {
      if (expectedSessionId && eventSessionId !== expectedSessionId) {
        console.log(`[${label}] ignoring ${eventType} for session_id=${eventSessionId}`);
        continue;
      }
      return event;
    }
    if (event.eventType === "registration_failed") {
      throw new E2EFailure(`Registration failed: ${event.error || JSON.stringify(event)}`);
    }
    if (event.eventType === "call_terminated") {
      if (expectedSessionId && eventSessionId !== expectedSessionId) {
        console.log(`[${label}] ignoring call_terminated for session_id=${eventSessionId}`);
        continue;
      }
      throw new E2EFailure(`Call terminated before ${eventType}: ${event.reason || ""}`);
    }
  }
  throw new E2EFailure(`Timed out after ${timeoutSeconds.toFixed(0)}s waiting for ${eventType}`);
}

async function sendFramesRealtime(endpoint, sessionId, samples, label, callState) {
  const stats = audioStats(samples);
  const duration = samples.length / SAMPLE_RATE;
  console.log(`[${label}] sending ${duration.toFixed(1)}s audio (peak=${stats.peak}, rms=${stats.rms.toFixed(0)})`);

  let framesSent = 0;
  for (let idx = 0; idx < samples.length; idx += FRAME_SAMPLES) {
    if (callState.ended) throw new E2EFailure(`Call ended while sending ${label}`);
    const chunk = samples.slice(idx, idx + FRAME_SAMPLES);
    while (chunk.length < FRAME_SAMPLES) chunk.push(0);
    try {
      endpoint.sendAudioBytes(sessionId, pcm16le(chunk), SAMPLE_RATE, CHANNELS);
      framesSent += 1;
    } catch (error) {
      throw new E2EFailure(`${label} send failed at frame ${framesSent}: ${error.message}`);
    }
    await sleep(20);
  }

  if (framesSent === 0) throw new E2EFailure(`${label} sent no frames`);
  console.log(`[${label}] sent ${framesSent} frames`);
}

async function sendSilence(endpoint, sessionId, seconds, callState) {
  const silence = pcm16le(new Array(FRAME_SAMPLES).fill(0));
  for (let frameIdx = 0; frameIdx < Math.floor(seconds / 0.02); frameIdx += 1) {
    if (callState.ended) break;
    try {
      endpoint.sendAudioBytes(sessionId, silence, SAMPLE_RATE, CHANNELS);
    } catch (error) {
      throw new E2EFailure(`silence send failed at frame ${frameIdx}: ${error.message}`);
    }
    await sleep(20);
  }
}

function startReceivers(endpoint, sessionId, label = "recv") {
  const state = {
    running: true,
    ended: false,
    errors: [],
    receivedSamples: [],
    receivedDtmf: [],
  };

  const recvTask = (async () => {
    let frameCount = 0;
    while (state.running && !state.ended) {
      try {
        const result = await endpoint.recvAudioBytesAsync(sessionId, 100);
        if (!result) continue;
        const samples = decodePcm16le(result);
        if (!samples.length) continue;
        state.receivedSamples.push(...samples);
        frameCount += 1;
        if (frameCount === 1) {
          console.log(`[${label}/recv] first frame: ${samples.length} samples at ${SAMPLE_RATE}Hz`);
        } else if (frameCount % 500 === 0) {
          const duration = state.receivedSamples.length / SAMPLE_RATE;
          const stats = audioStats(samples);
          console.log(`[${label}/recv] ${frameCount} frames (${duration.toFixed(1)}s total, rms=${stats.rms.toFixed(0)})`);
        }
      } catch (error) {
        if (String(error.message).toLowerCase().includes("call not active")) {
          state.ended = true;
        } else if (state.running && !state.ended) {
          state.errors.push(`recvAudioBytesAsync failed: ${error.message}`);
        }
        break;
      }
    }
  })();

  const eventTask = (async () => {
    while (state.running && !state.ended) {
      try {
        const event = await endpoint.waitForEvent(200);
        if (!event) continue;
        if (event.eventType === "call_terminated") {
          const eventSessionId = sessionIdFromEvent(event);
          if (eventSessionId && eventSessionId !== sessionId) {
            console.log(`[${label}/event] ignoring call end for session_id=${eventSessionId}`);
            continue;
          }
          console.log(`[${label}/event] call ended: ${event.reason || ""}`);
          state.ended = true;
          break;
        }
        if (event.eventType === "dtmf_received") {
          const eventSessionId = sessionIdFromEvent(event);
          if (!eventSessionId || eventSessionId === sessionId) {
            console.log(`[${label}/event] DTMF received: ${event.digit}`);
            state.receivedDtmf.push(event.digit);
          }
        }
      } catch (error) {
        if (state.running && !state.ended) state.errors.push(`waitForEvent failed: ${error.message}`);
        break;
      }
    }
  })();

  return { state, recvTask, eventTask };
}

async function waitForReceivedAudio(state, minSeconds, timeoutSeconds, label = "recv") {
  if (timeoutSeconds <= 0) {
    throw new E2EFailure(`No remaining call budget to wait for ${minSeconds.toFixed(1)}s of audio on ${label}`);
  }
  const deadline = Date.now() + timeoutSeconds * 1000;
  while (Date.now() < deadline) {
    if (state.ended) {
      throw new E2EFailure(`Call ended before receiving ${minSeconds.toFixed(1)}s of audio on ${label}`);
    }
    const duration = state.receivedSamples.length / SAMPLE_RATE;
    if (duration >= minSeconds) return;
    await sleep(100);
  }
  throw new E2EFailure(`Timed out waiting for ${minSeconds.toFixed(1)}s of received audio on ${label}`);
}

async function waitForDtmfReceived(state, expectedDigit, timeoutSeconds, label = "recv") {
  if (timeoutSeconds <= 0) {
    throw new E2EFailure(`No remaining call budget to wait for DTMF ${expectedDigit} on ${label}`);
  }
  const deadline = Date.now() + timeoutSeconds * 1000;
  while (Date.now() < deadline) {
    if (state.ended) {
      throw new E2EFailure(`Call ended before DTMF ${expectedDigit} arrived on ${label}`);
    }
    if (state.receivedDtmf.some((d) => String(d) === String(expectedDigit))) return;
    await sleep(50);
  }
  throw new E2EFailure(
    `Timed out waiting for DTMF ${expectedDigit} on ${label}; received=${JSON.stringify(state.receivedDtmf)}`,
  );
}

async function waitForCallEnded(state, timeoutSeconds) {
  const deadline = Date.now() + timeoutSeconds * 1000;
  while (Date.now() < deadline) {
    if (state.ended) return;
    await sleep(100);
  }
  throw new E2EFailure("Timed out waiting for call termination after hangup");
}

function analyze(samples, outputPath, speechThreshold, label = "received") {
  saveWav(outputPath, samples);
  const stats = audioStats(samples, speechThreshold);
  const duration = samples.length / SAMPLE_RATE;
  console.log(`[${label}] received=${duration.toFixed(2)}s peak=${stats.peak} rms=${stats.rms.toFixed(0)} speech=${stats.speechPercent.toFixed(2)}%`);
  console.log(`[${label}] artifact=${outputPath}`);
  return { duration, speechPercent: stats.speechPercent };
}

function assertAnalyzedAudio(samples, outputPath, args, label) {
  const { duration, speechPercent } = analyze(samples, outputPath, args.speechThreshold, label);
  if (duration < args.minReceivedSeconds) {
    throw new E2EFailure(`${label} audio duration ${duration.toFixed(2)}s is below ${args.minReceivedSeconds.toFixed(2)}s`);
  }
  if (speechPercent < args.minSpeechPercent) {
    throw new E2EFailure(`${label} speech percent ${speechPercent.toFixed(2)}% is below ${args.minSpeechPercent.toFixed(2)}%`);
  }
}

async function callWithRetries(endpoint, destUri, args, label) {
  const attempts = Math.max(1, args.callAttempts);
  let lastError = null;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      return endpoint.call(destUri);
    } catch (error) {
      lastError = error;
      if (attempt >= attempts) break;
      console.log(`[${label}] call attempt ${attempt} failed: ${error.message}; retrying`);
      await sleep(args.callRetrySeconds * 1000);
    }
  }
  throw new E2EFailure(`${label} call initiation failed: ${lastError?.message || lastError}`);
}

async function stopReceivers(receivers) {
  if (!receivers) return;
  receivers.state.running = false;
  await Promise.race([
    Promise.allSettled([receivers.recvTask, receivers.eventTask]),
    sleep(500),
  ]);
}

async function runOutbound(args, SipEndpoint, env, clip) {
  const endpoint = new SipEndpoint({ sipServer: env.sipDomain, logLevel: 3 });
  let sessionId = null;
  let receivers = null;

  try {
    console.log("registering SIP account A");
    endpoint.register(env.a.username, env.a.password);
    await waitForEventType(endpoint, "registered", args.maxRegisterSeconds, "account-a");
    console.log("registered account A");

    console.log("account A calling E2E_SIP_DEST_URI_A");
    sessionId = await callWithRetries(endpoint, env.a.destUri, args, "account-a");

    await waitForEventType(endpoint, "call_answered", args.maxAnswerSeconds, "account-a", sessionId);
    console.log(`connected session_id=${sessionId}`);

    receivers = startReceivers(endpoint, sessionId, "account-a");
    const deadline = Date.now() + args.maxCallSeconds * 1000;

    await sendSilence(endpoint, sessionId, 0.5, receivers.state);
    await sendFramesRealtime(endpoint, sessionId, clip, "a-outbound-clip", receivers.state);

    console.log(`sending DTMF ${args.dtmfDigit}`);
    try {
      endpoint.sendDtmf(sessionId, args.dtmfDigit);
    } catch (error) {
      throw new E2EFailure(`DTMF send failed: ${error.message}`);
    }

    await waitForReceivedAudio(
      receivers.state,
      args.minReceivedSeconds,
      Math.min(15.0, Math.max(0.0, (deadline - Date.now()) / 1000)),
      "account-a",
    );
    await sendSilence(endpoint, sessionId, 1.0, receivers.state);

    if (receivers.state.ended) throw new E2EFailure("Call ended before scripted hangup");
    console.log("hanging up");
    try {
      endpoint.hangup(sessionId);
    } catch (error) {
      throw new E2EFailure(`Hangup failed: ${error.message}`);
    }
    await waitForCallEnded(receivers.state, args.maxHangupSeconds);
  } finally {
    await stopReceivers(receivers);
    try {
      endpoint.shutdown();
    } catch (error) {
      console.log(`warning: shutdown failed: ${error.message}`);
    }
  }

  if (receivers?.state.errors.length) {
    throw new E2EFailure(receivers.state.errors.join("; "));
  }
  assertAnalyzedAudio(receivers ? [...receivers.state.receivedSamples] : [], args.output, args, "account-a");
}

async function runInbound(args, SipEndpoint, env, clip) {
  let receiverEndpoint = null;
  let callerEndpoint = null;
  let receiverMedia = null;
  let callerMedia = null;

  try {
    receiverEndpoint = new SipEndpoint({ sipServer: env.sipDomain, logLevel: 3 });
    callerEndpoint = new SipEndpoint({ sipServer: env.sipDomain, logLevel: 3 });

    console.log("registering receiver account A");
    receiverEndpoint.register(env.a.username, env.a.password);
    await waitForEventType(receiverEndpoint, "registered", args.maxRegisterSeconds, "receiver-a");
    console.log("registered receiver account A");

    console.log("registering caller account B");
    callerEndpoint.register(env.b.username, env.b.password);
    await waitForEventType(callerEndpoint, "registered", args.maxRegisterSeconds, "caller-b");
    console.log("registered caller account B");

    console.log("caller B calling E2E_SIP_DEST_URI_B");
    let callerSessionId = await callWithRetries(callerEndpoint, env.b.destUri, args, "caller-b");

    const ringing = await waitForEventType(receiverEndpoint, "call_ringing", args.maxAnswerSeconds, "receiver-a");
    let receiverSessionId = sessionIdFromEvent(ringing);
    const receiverAnswered = await waitForEventType(
      receiverEndpoint,
      "call_answered",
      args.maxAnswerSeconds,
      "receiver-a",
      receiverSessionId,
    );
    receiverSessionId = sessionIdFromEvent(receiverAnswered) || receiverSessionId;
    const callerAnswered = await waitForEventType(
      callerEndpoint,
      "call_answered",
      args.maxAnswerSeconds,
      "caller-b",
      callerSessionId,
    );
    callerSessionId = sessionIdFromEvent(callerAnswered) || callerSessionId;

    if (!receiverSessionId) throw new E2EFailure("Inbound receiver did not expose a session id");
    if (!callerSessionId) throw new E2EFailure("Inbound caller did not expose a session id");
    console.log(`connected receiver_session_id=${receiverSessionId} caller_session_id=${callerSessionId}`);

    receiverMedia = startReceivers(receiverEndpoint, receiverSessionId, "receiver-a");
    callerMedia = startReceivers(callerEndpoint, callerSessionId, "caller-b");
    const deadline = Date.now() + args.maxCallSeconds * 1000;

    await sendSilence(callerEndpoint, callerSessionId, 0.5, callerMedia.state);
    await sendFramesRealtime(callerEndpoint, callerSessionId, clip, "b-to-a-clip", callerMedia.state);
    await waitForReceivedAudio(
      receiverMedia.state,
      args.minReceivedSeconds,
      Math.min(15.0, Math.max(0.0, (deadline - Date.now()) / 1000)),
      "receiver-a",
    );

    await sendSilence(receiverEndpoint, receiverSessionId, 0.5, receiverMedia.state);
    await sendFramesRealtime(receiverEndpoint, receiverSessionId, clip, "a-to-b-clip", receiverMedia.state);
    await waitForReceivedAudio(
      callerMedia.state,
      args.minReceivedSeconds,
      Math.min(15.0, Math.max(0.0, (deadline - Date.now()) / 1000)),
      "caller-b",
    );

    console.log(`caller B sending DTMF ${args.dtmfDigit}`);
    try {
      callerEndpoint.sendDtmf(callerSessionId, args.dtmfDigit);
    } catch (error) {
      throw new E2EFailure(`Inbound DTMF send failed: ${error.message}`);
    }

    await sendSilence(callerEndpoint, callerSessionId, 0.5, callerMedia.state);
    await sendSilence(receiverEndpoint, receiverSessionId, 0.5, receiverMedia.state);

    await waitForDtmfReceived(
      receiverMedia.state,
      args.dtmfDigit,
      Math.min(5.0, Math.max(0.0, (deadline - Date.now()) / 1000)),
      "receiver-a",
    );

    if (callerMedia.state.ended || receiverMedia.state.ended) {
      throw new E2EFailure("Inbound call ended before scripted hangup");
    }
    console.log("caller B hanging up");
    try {
      callerEndpoint.hangup(callerSessionId);
    } catch (error) {
      throw new E2EFailure(`Inbound hangup failed: ${error.message}`);
    }
    const hangupDeadline = Date.now() + args.maxHangupSeconds * 1000;
    for (const [waiterLabel, waiterState] of [
      ["receiver-a", receiverMedia.state],
      ["caller-b", callerMedia.state],
    ]) {
      const remainingSeconds = Math.max(0.0, (hangupDeadline - Date.now()) / 1000);
      try {
        await waitForCallEnded(waiterState, remainingSeconds);
      } catch {
        throw new E2EFailure(
          `Timed out waiting for ${waiterLabel} call termination after hangup`,
        );
      }
    }
  } finally {
    await stopReceivers(callerMedia);
    await stopReceivers(receiverMedia);
    for (const [label, endpoint] of [["caller", callerEndpoint], ["receiver", receiverEndpoint]]) {
      if (!endpoint) continue;
      try {
        endpoint.shutdown();
      } catch (error) {
        console.log(`warning: ${label} shutdown failed: ${error.message}`);
      }
    }
  }

  const errors = [
    ...(receiverMedia?.state.errors || []),
    ...(callerMedia?.state.errors || []),
  ];
  if (errors.length) {
    throw new E2EFailure(errors.join("; "));
  }
  assertAnalyzedAudio(receiverMedia ? [...receiverMedia.state.receivedSamples] : [], args.output, args, "receiver-a");
  assertAnalyzedAudio(callerMedia ? [...callerMedia.state.receivedSamples] : [], args.callerOutput, args, "caller-b");
}

async function run(args) {
  const env = e2eEnv(args);
  console.log(`dry_run=${args.dryRun}`);
  console.log("sdk=node");
  console.log(`direction=${args.direction}`);
  console.log("sip env loaded");
  console.log(`clip=${args.clip}`);
  console.log(`output=${args.output}`);
  if (args.direction === "inbound") {
    console.log(`caller_output=${args.callerOutput}`);
  }

  if (args.dryRun) {
    console.log("dry run passed");
    return;
  }

  const { SipEndpoint, initLogging } = await loadAgentTransport();
  initLogging(env.rustLog);

  const clip = loadWav(args.clip);
  if (args.direction === "outbound") {
    await runOutbound(args, SipEndpoint, env, clip);
  } else {
    await runInbound(args, SipEndpoint, env, clip);
  }

  console.log("node SIP e2e smoke passed");
}

run(parseArgs(process.argv.slice(2))).catch((error) => {
  const message = error instanceof Error ? error.message : String(error);
  console.error(`E2E FAILED: ${message}`);
  process.exit(1);
});
