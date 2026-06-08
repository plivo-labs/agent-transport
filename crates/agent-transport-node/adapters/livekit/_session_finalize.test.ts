/**
 * Unit tests for startSessionRecording in _session_finalize.ts.
 *
 * Run with: npx tsx --test adapters/livekit/_session_finalize.test.ts
 *
 * Covers the "record iff we'll upload" gate: recording only starts when
 * observability (AGENT_OBSERVABILITY_URL) is configured — the symmetric
 * counterpart to finalizeSession, which uploads then deletes the file.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { rmSync } from 'node:fs';

import { startSessionRecording } from './_session_finalize.js';

const OBS = 'AGENT_OBSERVABILITY_URL';

function fakeEndpoint() {
  const started: Array<[string, string, boolean]> = [];
  return {
    started,
    startRecording(id: string, path: string, stereo: boolean) {
      started.push([id, path, stereo]);
    },
  };
}

function withObs<T>(value: string | undefined, fn: () => T): T {
  const prev = process.env[OBS];
  if (value === undefined) delete process.env[OBS];
  else process.env[OBS] = value;
  try {
    return fn();
  } finally {
    if (prev === undefined) delete process.env[OBS];
    else process.env[OBS] = prev;
  }
}

test('startSessionRecording records when observability is configured', () => {
  const dir = join(tmpdir(), `obs-rec-${process.pid}-${Date.now()}`);
  try {
    const ep = fakeEndpoint();
    const { recordingPath, recordingStartedAt } = withObs('https://obs.example/v0', () =>
      startSessionRecording(ep, 'sess-1', dir),
    );

    assert.equal(recordingPath, `${dir}/recording_sess-1.ogg`);
    assert.ok(typeof recordingStartedAt === 'number');
    assert.deepEqual(ep.started, [['sess-1', recordingPath, true]]);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('startSessionRecording is a no-op when observability is unset', () => {
  const ep = fakeEndpoint();
  const result = withObs(undefined, () => startSessionRecording(ep, 'sess-1', join(tmpdir(), 'should-not-exist')));

  assert.deepEqual(result, {});
  assert.deepEqual(ep.started, [], 'must not record when there is nowhere to upload');
});

test('startSessionRecording swallows start failures and reports nothing', () => {
  const ep = {
    startRecording() {
      throw new Error('rust recorder unavailable');
    },
  };
  const dir = join(tmpdir(), `obs-rec-fail-${process.pid}-${Date.now()}`);
  try {
    const result = withObs('https://obs.example/v0', () => startSessionRecording(ep, 'sess-1', dir));
    assert.deepEqual(result, {}, 'a failed start must not leave a dangling path for finalize');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
