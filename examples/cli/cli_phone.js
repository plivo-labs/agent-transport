#!/usr/bin/env node
/**
 * CLI Example Phone (Node.js) — make a real SIP call from your terminal.
 *
 * Uses agent-transport Node.js binding for SIP.
 * Audio I/O is not implemented (Node lacks native mic/speaker access).
 * This example demonstrates SIP signaling, DTMF, pause/resume, flush/clear.
 *
 * Prerequisites:
 *     cd crates/agent-transport-node && npm run build
 *
 * Usage:
 *     SIP_USERNAME=xxx SIP_PASSWORD=yyy \
 *       node examples/cli_phone.js sip:+15551234567@phone.plivo.com
 *
 * Keyboard controls during call:
 *     0-9, *, #   Send DTMF digit
 *     p           Pause audio playback
 *     r           Resume audio playback
 *     m           Mute
 *     u           Unmute
 *     f           Flush
 *     c           Clear buffer
 *     w           Wait for playout
 *     q           Hang up
 */

const path = require('path');

// Load the native binding
let SipEndpoint;
try {
  ({ SipEndpoint } = require(path.join(__dirname, '..', 'crates', 'agent-transport-node')));
} catch (e) {
  console.error('Build the Node binding first: cd crates/agent-transport-node && npm run build');
  process.exit(1);
}

const username = process.env.SIP_USERNAME;
const password = process.env.SIP_PASSWORD;
const sipDomain = process.env.SIP_DOMAIN || 'phone.plivo.com';
const destUri = process.argv[2];

if (!username || !password) {
  console.error('Set SIP_USERNAME and SIP_PASSWORD environment variables.');
  process.exit(1);
}

console.log('Initializing SIP endpoint...');
const ep = new SipEndpoint({ sipServer: sipDomain, logLevel: 3 });

// Event handling
ep.on('registered', () => console.log('Registered.'));
ep.on('call_media_active', (ev) => console.log(`Call ${ev.callId} media active.`));
ep.on('dtmf_received', (ev) => console.log(`  DTMF recv: ${ev.digit}`));
ep.on('call_terminated', (ev) => {
  console.log(`  Call ended: ${ev.reason || ''}`);
  cleanup();
});

console.log(`Registering as ${username}@${sipDomain}...`);
ep.register(username, password);

// Wait a bit for registration, then make call
setTimeout(() => {
  if (!destUri) {
    console.log('No destination URI provided. Waiting for incoming call...');
    ep.on('incoming_call', (ev) => {
      console.log(`Incoming call from ${ev.session.remoteUri}`);
      ep.answer(ev.callId);
      startCall(ev.callId);
    });
    return;
  }

  console.log(`Calling ${destUri}...`);
  try {
    const callId = ep.call(destUri);
    console.log(`Call initiated (callId=${callId}). Waiting for answer...`);
    startCall(callId);
  } catch (e) {
    console.error(`Call failed: ${e.message}`);
    ep.shutdown();
    process.exit(1);
  }
}, 2000);

let activeCallId = null;

function startCall(callId) {
  activeCallId = callId;

  // Wait for media active
  setTimeout(() => {
    console.log('');
    console.log('=== CONNECTED ===');
    console.log('  0-9,*,#  DTMF     p Pause   r Resume   m Mute   u Unmute');
    console.log('  f Flush   c Clear buffer   w Wait for playout   q Hang up');
    console.log('');
    console.log('  (No audio I/O in Node — use Python CLI for full audio)');
    console.log('');

    // Start keyboard input
    enableKeyboard(callId);

    // Periodic stats
    const statsInterval = setInterval(() => {
      if (activeCallId === null) {
        clearInterval(statsInterval);
        return;
      }
      try {
        const queued = ep.queuedFrames(callId);
        process.stdout.write(`\r  [STATS] queued=${queued} (${(queued * 0.02).toFixed(1)}s)\n`);
      } catch {
        clearInterval(statsInterval);
      }
    }, 5000);
  }, 1000);
}

function enableKeyboard(callId) {
  if (!process.stdin.isTTY) return;

  process.stdin.setRawMode(true);
  process.stdin.resume();
  process.stdin.setEncoding('utf8');

  process.stdin.on('data', (key) => {
    if (activeCallId === null) return;

    const DTMF = '0123456789*#';
    if (DTMF.includes(key)) {
      ep.sendDtmf(callId, key);
      process.stdout.write(`\r  DTMF sent: ${key}\n`);
    } else if (key === 'p') {
      ep.pause(callId);
      const q = ep.queuedFrames(callId);
      process.stdout.write(`\r  PAUSED — queued=${q} (${(q * 0.02).toFixed(1)}s)\n`);
    } else if (key === 'r') {
      const q = ep.queuedFrames(callId);
      ep.resume(callId);
      process.stdout.write(`\r  RESUMED — draining ${q} frames (${(q * 0.02).toFixed(1)}s)\n`);
    } else if (key === 'm') {
      ep.mute(callId);
      process.stdout.write('\r  MUTED\n');
    } else if (key === 'u') {
      ep.unmute(callId);
      process.stdout.write('\r  UNMUTED\n');
    } else if (key === 'f') {
      ep.flush(callId);
      const q = ep.queuedFrames(callId);
      process.stdout.write(`\r  FLUSH — queued=${q}\n`);
    } else if (key === 'c') {
      const qBefore = ep.queuedFrames(callId);
      ep.clearBuffer(callId);
      process.stdout.write(`\r  CLEAR BUFFER — discarded ${qBefore} frames (${(qBefore * 0.02).toFixed(1)}s)\n`);
    } else if (key === 'w') {
      const q = ep.queuedFrames(callId);
      process.stdout.write(`\r  WAITING for playout (queued=${q})...\n`);
      const t0 = Date.now();
      const completed = ep.waitForPlayout(callId, 10000);
      const elapsed = ((Date.now() - t0) / 1000).toFixed(2);
      process.stdout.write(`\r  PLAYOUT ${completed ? 'completed' : 'timed out'} in ${elapsed}s\n`);
    } else if (key === 'q' || key === '\r' || key === '\x03') {
      process.stdout.write('\r  Hanging up...\n');
      ep.hangup(callId);
      cleanup();
    }
  });
}

function cleanup() {
  activeCallId = null;
  if (process.stdin.isTTY) {
    process.stdin.setRawMode(false);
  }
  ep.shutdown();
  console.log('Done.');
  process.exit(0);
}
