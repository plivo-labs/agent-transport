// Smoke tests for the Node closeSessionServices helper.
//
// Run with `node --test tests/closeSessionServices.test.mjs` from
// crates/agent-transport-node/. Requires the livekit adapter to be
// built first (`npm run build:livekit`).
//
// The Python equivalent is exercised by
// crates/agent-transport-python/tests/test_livekit_close_session_services.py
// with a wider matrix of cases; this file is a thin smoke check for the
// TS port — the helper logic is intentionally identical across bindings.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { closeSessionServices } from '../livekit/_session_cleanup.js';

function makeFake({ method = 'close', throws = null, sleepMs = 0 } = {}) {
  const state = { closeCalls: 0, closed: false };
  const fn = async function () {
    state.closeCalls++;
    if (sleepMs) {
      // Unref so the dangling timer (when our caller bails on a timeout)
      // does not keep the event loop alive past test end.
      await new Promise((r) => setTimeout(r, sleepMs).unref());
    }
    if (throws) throw throws;
    state.closed = true;
  };
  const svc = { [method]: fn };
  Object.defineProperty(svc, 'state', { value: state, enumerable: false });
  return svc;
}

class FakeService {
  constructor({ throws = null, sleepMs = 0 } = {}) {
    this.closeCalls = 0;
    this.closed = false;
    this._throws = throws;
    this._sleepMs = sleepMs;
  }

  async close() {
    this.closeCalls++;
    if (this._sleepMs) {
      await new Promise((r) => setTimeout(r, this._sleepMs).unref());
    }
    if (this._throws) throw this._throws;
    this.closed = true;
  }
}

test('closes all three', async () => {
  const stt = new FakeService();
  const tts = new FakeService();
  const llm = new FakeService();
  await closeSessionServices({ stt, tts, llm });
  assert.equal(stt.closeCalls, 1);
  assert.equal(tts.closeCalls, 1);
  assert.equal(llm.closeCalls, 1);
  assert.ok(stt.closed && tts.closed && llm.closed);
});

test('one failure does not block others', async () => {
  const stt = new FakeService();
  const tts = new FakeService({ throws: new Error('vendor 500') });
  const llm = new FakeService();
  const logs = [];
  await closeSessionServices(
    { stt, tts, llm },
    { logger: (m) => logs.push(m) },
  );
  assert.ok(stt.closed);
  assert.equal(tts.closeCalls, 1);
  assert.ok(!tts.closed);
  assert.ok(llm.closed);
  assert.ok(logs.some((l) => l.includes('tts')));
});

test('per-service timeout', async () => {
  const fast = new FakeService();
  const slow = new FakeService({ sleepMs: 5000 });
  const other = new FakeService();
  const started = Date.now();
  await closeSessionServices(
    { stt: fast, tts: slow, llm: other },
    { perServiceTimeoutMs: 50 },
  );
  const elapsed = Date.now() - started;
  assert.ok(fast.closed);
  assert.ok(!slow.closed);
  assert.ok(other.closed);
  assert.ok(elapsed < 1000, `elapsed=${elapsed}ms`);
});

test('handles null and missing close', async () => {
  const stt = null;
  const tts = {}; // no close method
  const llm = new FakeService();
  await closeSessionServices({ stt, tts, llm });
  assert.ok(llm.closed);
});

test('no session is noop', async () => {
  await closeSessionServices(null);
  await closeSessionServices(undefined);
});

// The @livekit/agents Node SDK is inconsistent: STT, TTS and RealtimeModel
// expose close(), but the standard chat LLM base class exposes aclose()
// (cf. node_modules/@livekit/agents/dist/llm/llm.js vs llm/realtime.js).
// The helper must close whichever exists per-service.

test('closes chat LLM via aclose', async () => {
  const llm = makeFake({ method: 'aclose' });
  await closeSessionServices({ llm });
  assert.equal(llm.state.closeCalls, 1);
  assert.ok(llm.state.closed);
});

test('closes realtime LLM via close', async () => {
  const llm = makeFake({ method: 'close' });
  await closeSessionServices({ llm });
  assert.equal(llm.state.closeCalls, 1);
  assert.ok(llm.state.closed);
});

test('closes STT/TTS via close', async () => {
  const stt = makeFake({ method: 'close' });
  const tts = makeFake({ method: 'close' });
  await closeSessionServices({ stt, tts });
  assert.ok(stt.state.closed && tts.state.closed);
});

test('mixed methods all get closed', async () => {
  const stt = makeFake({ method: 'close' });
  const tts = makeFake({ method: 'close' });
  const llm = makeFake({ method: 'aclose' });
  await closeSessionServices({ stt, tts, llm });
  assert.ok(stt.state.closed);
  assert.ok(tts.state.closed);
  assert.ok(llm.state.closed);
});
