import assert from 'node:assert/strict';
import test from 'node:test';
import { existsSync, mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { finalizeSession } from '../../livekit/_session_finalize.js';

// agentId is optional; the observability upload is gated on it (obs keys
// sessions on agent_id, and the sessions table is NOT NULL). This is the Node
// parity for the Python test_observability_skip_without_agent_id:
//
//   - agentId unset + obs URL set  -> SKIP upload, KEEP the local recording
//   - agentId set   + obs URL set  -> ATTEMPT upload, then clean up the recording
//
// No module mocking: uploadReport throws fast without LIVEKIT_API_KEY/SECRET
// (buildBearerAuthHeaders runs before any fetch), and the recording-cleanup
// sits under the same gate as the upload — so the file's fate is an exact proxy
// for which branch finalizeSession took.

function fakeSession() {
  return {
    usage: undefined,
    stt: null,
    tts: null,
    llm: null,
    // Fire 'close' synchronously so finalize's close-wait resolves instantly
    // rather than burning the 5s fallback timer.
    on(event, cb) {
      if (event === 'close') cb();
    },
    async close() {},
  };
}

const fakeEndpoint = { stopRecording() {} };

function makeRecording() {
  const dir = mkdtempSync(join(tmpdir(), 'at-finalize-'));
  const path = join(dir, 'recording_x.ogg');
  writeFileSync(path, 'ogg');
  return path;
}

async function withEnv(fn) {
  // Snapshot + restore the env keys this test mutates so file-level test
  // ordering can't leak observability config between cases.
  const saved = {
    AGENT_OBSERVABILITY_URL: process.env.AGENT_OBSERVABILITY_URL,
    LIVEKIT_API_KEY: process.env.LIVEKIT_API_KEY,
    LIVEKIT_API_SECRET: process.env.LIVEKIT_API_SECRET,
  };
  process.env.AGENT_OBSERVABILITY_URL = 'https://obs.example/v0';
  delete process.env.LIVEKIT_API_KEY;
  delete process.env.LIVEKIT_API_SECRET;
  const warnings = [];
  const origWarn = console.warn;
  console.warn = (...a) => warnings.push(a.map(String).join(' '));
  try {
    await fn(warnings);
  } finally {
    console.warn = origWarn;
    for (const [k, v] of Object.entries(saved)) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
  }
}

function runFinalize(agentId, recordingPath) {
  return finalizeSession({
    session: fakeSession(),
    endpoint: fakeEndpoint,
    sessionId: 'sess-x',
    transport: 'sip',
    agentId,
    agentName: 'sip-agent',
    recordingPath,
    recordingStartedAt: undefined,
  });
}

test('skips upload and keeps the recording when agentId is unset', async () => {
  await withEnv(async (warnings) => {
    const rec = makeRecording();
    await runFinalize('', rec);
    assert.ok(existsSync(rec), 'recording must be kept when the upload is skipped');
    assert.ok(
      warnings.some((w) => w.includes('agentId is unset')),
      'a warning must explain why the upload was skipped',
    );
  });
});

test('attempts upload and cleans up the recording when agentId is set', async () => {
  await withEnv(async () => {
    const rec = makeRecording();
    await runFinalize('agent-9', rec);
    assert.ok(!existsSync(rec), 'recording must be cleaned up after an upload attempt');
  });
});
