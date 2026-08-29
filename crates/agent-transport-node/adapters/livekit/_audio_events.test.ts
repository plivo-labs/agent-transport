/**
 * Unit tests for _audio_events.ts (AudioEventBroker).
 *
 * Run with: npx tsx --test adapters/livekit/_audio_events.test.ts
 *
 * The async-id audio protocol keys every pending waiter by the async-id from
 * `sendAudioAsync` / `waitForPlayoutAsync` (a napi `BigInt` → JS bigint) and
 * resolves it when the matching terminal event arrives. The event's `async_id`
 * is also a napi `BigInt`, so both sides are bigint — see EventInfo.async_id
 * in src/lib.rs, which MUST stay `BigInt`. (An `i64` there maps to a JS
 * `number`, and `7n !== 7` would leave the waiter hanging forever —
 * captureFrame stalls and TTS goes silent.)
 *
 * These cover the broker's matching, race-buffering, and error paths.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import type { EventInfo } from 'agent-transport';
import { AudioEventBroker } from './_audio_events.js';

// Minimal stand-in endpoint. `claimFeeder()` suppresses the self-pump, so the
// broker never reads from this object — it is only held, never called.
function makeBroker(): AudioEventBroker {
  const broker = new AudioEventBroker({} as never);
  broker.claimFeeder();
  return broker;
}

// A terminal audio event as the napi binding delivers it — async-id is a
// bigint (EventInfo.async_id is napi `BigInt`).
function audioEvent(eventType: string, asyncId: bigint, error?: string): EventInfo {
  return { eventType, asyncId, error } as EventInfo;
}

// 'resolved'/'rejected' if the waiter settles, 'pending' if it never does. A
// correct broker settles on the microtask queue; a key mismatch would leave
// the waiter pending forever, so a short real-timer grace separates them.
async function settledWithin(
  p: Promise<void>,
  ms: number,
): Promise<'resolved' | 'rejected' | 'pending'> {
  return await Promise.race([
    p.then(
      () => 'resolved' as const,
      () => 'rejected' as const,
    ),
    new Promise<'pending'>((r) => setTimeout(() => r('pending'), ms)),
  ]);
}

test('capture completion resolves the matching waiter', async () => {
  const broker = makeBroker();
  const p = broker.waitForCapture(7n);
  broker.dispatch(audioEvent('audio_capture_complete', 7n));
  assert.equal(await settledWithin(p, 250), 'resolved');
});

test('completion buffered before the waiter registers is still matched', async () => {
  // Rust immediate-emit race: the event arrives before waitForCapture.
  const broker = makeBroker();
  broker.dispatch(audioEvent('audio_capture_complete', 9n));
  const p = broker.waitForCapture(9n);
  assert.equal(await settledWithin(p, 250), 'resolved');
});

test('playout completion resolves the matching waiter', async () => {
  const broker = makeBroker();
  const p = broker.waitForPlayout(11n);
  broker.dispatch(audioEvent('audio_playout_complete', 11n));
  assert.equal(await settledWithin(p, 250), 'resolved');
});

test('capture error rejects the matching waiter', async () => {
  // clearBuffer / flush / drop emits audio_capture_error per pending async-id.
  const broker = makeBroker();
  const p = broker.waitForCapture(13n);
  broker.dispatch(audioEvent('audio_capture_error', 13n, 'cleared'));
  assert.equal(await settledWithin(p, 250), 'rejected');
});
