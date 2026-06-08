import assert from 'node:assert/strict';
import test from 'node:test';

import { voice } from '@livekit/agents';
import {
  buildOtlpLogRecords,
  buildRoomTags,
  injectRoomTagsIntoChatHistory,
} from '../../livekit/observability.js';

// Round-trip test: the obs server has a hand-crafted fixture
// (`accepts raw session.report logs from Agent Transport Node` in
// agent-observability/tests/routes.test.ts) that asserts what shape it
// expects from this builder. If we change the field names or the body
// string here without updating that fixture (or vice-versa), uploads
// will silently fail to populate raw_report — the obs side branches on
// `body === "session report"` and reads specific attribute keys. This
// test pins the contract on the Node side.
test('buildOtlpLogRecords emits the envelope obs server expects', () => {
  const report = voice.createSessionReport({
    jobId: 'job-room-node-raw-report',
    roomId: 'room-node-raw-report',
    room: 'room-node-raw-report',
    options: { max_tool_steps: 2 },
    events: [],
    enableRecording: false,
    chatHistory: { items: [], toJSON: () => ({ items: [] }) },
    startedAt: 1700000000,
    modelUsage: null,
  });

  const records = buildOtlpLogRecords(
    report,
    'node-support-agent-id',
    'node-support-agent',
    {
      agent_id: 'node-support-agent-id',
      agent_name: 'node-support-agent',
      account_id: 'acct-node',
      transport: 'audio_stream',
    },
  );

  assert.equal(records.length, 1, 'one OTLP record per session');
  const [record] = records;

  // Body string is the discriminator the obs handler branches on.
  // Changing it breaks `persistLiveKitOtlpLogs`'s "session report" branch.
  assert.equal(record.body, 'session report');

  // Every attribute key obs reads from the record. Keep in sync with the
  // fixture in agent-observability/tests/routes.test.ts:665.
  assert.equal(record.attributes.room_id, 'room-node-raw-report');
  assert.equal(record.attributes.job_id, 'job-room-node-raw-report');
  assert.equal(record.attributes['logger.name'], 'chat_history');
  assert.equal(record.attributes.agent_id, 'node-support-agent-id');
  assert.equal(record.attributes.agent_name, 'node-support-agent');
  assert.equal(typeof record.attributes.sdk_version, 'string');
  assert.deepEqual(record.attributes.room_tags, {
    agent_id: 'node-support-agent-id',
    agent_name: 'node-support-agent',
    account_id: 'acct-node',
    transport: 'audio_stream',
  });

  // session.report is the SDK's own JSON projection — assert it's an
  // object and carries the room id (sanity check that we passed the
  // right report through, not that we replicate the SDK's exact shape).
  assert.equal(typeof record.attributes['session.report'], 'object');
  // sessionReportToJSON projects to snake_case (matches the obs fixture).
  assert.equal(record.attributes['session.report']?.room_id, 'room-node-raw-report');
});

test('injectRoomTagsIntoChatHistory adds prefix tags obs extractor needs', () => {
  // Pin the chat_history.tags[] contract obs's extractAgentId reads.
  // If this drifts the obs server returns 400 missing_agent_id and the
  // session row never gets created — see agent-observability/src/index.ts
  // `extractAgentId` for the consumer side.
  const merged = injectRoomTagsIntoChatHistory(
    { items: [{ role: 'user', content: 'hello' }] },
    {
      agent_id: 'agent-uuid-1',
      agent_name: 'support-agent',
      account_id: 'acct-1',
      transport: 'audio_stream',
    },
  );

  assert.deepEqual(merged.items, [{ role: 'user', content: 'hello' }]);
  assert.ok(Array.isArray(merged.tags));
  const tags = merged.tags;
  assert.ok(tags.includes('agent_id:agent-uuid-1'), 'agent_id: prefix tag emitted');
  assert.ok(tags.includes('agent_name:support-agent'));
  assert.ok(tags.includes('account_id:acct-1'));
  assert.ok(tags.includes('transport:audio_stream'));
});

test('injectRoomTagsIntoChatHistory dedupes and preserves existing tags', () => {
  // Defensive: if LiveKit ever starts serializing tags itself, don't
  // duplicate the prefix tag we add.
  const merged = injectRoomTagsIntoChatHistory(
    { items: [], tags: ['agent_id:agent-uuid-1', 'custom_tag'] },
    { agent_id: 'agent-uuid-1', agent_name: 'support-agent' },
  );

  const tags = merged.tags;
  assert.equal(
    tags.filter((t) => t === 'agent_id:agent-uuid-1').length,
    1,
    'agent_id: prefix tag deduplicated',
  );
  assert.ok(tags.includes('custom_tag'), 'pre-existing tag preserved');
  assert.ok(tags.includes('agent_name:support-agent'));
});

test('injectRoomTagsIntoChatHistory ignores non-string existing tags', () => {
  // Be lenient: if a future LiveKit version returns a non-string entry,
  // it's filtered out rather than poisoning the array.
  const merged = injectRoomTagsIntoChatHistory(
    { items: [], tags: ['valid', 123, null, { object: true }] },
    { agent_id: 'a1' },
  );

  assert.ok(merged.tags.includes('valid'));
  assert.ok(merged.tags.includes('agent_id:a1'));
  assert.ok(!merged.tags.includes(123));
});

test('buildRoomTags combines session metadata with transport tags', () => {
  assert.deepEqual(
    buildRoomTags({
      agentId: 'support-agent-id',
      agentName: 'support-agent',
      accountId: 'acct-1',
      metadata: {
        account_id: 'acct-1',
        customer_tier: 'gold',
        skipped: { nested: true },
      },
      transport: 'audio_stream',
      direction: 'inbound',
    }),
    {
      account_id: 'acct-1',
      customer_tier: 'gold',
      agent_id: 'support-agent-id',
      agent_name: 'support-agent',
      transport: 'audio_stream',
      direction: 'inbound',
    },
  );
});
