import { existsSync, mkdirSync, unlinkSync } from 'node:fs';
import { uploadReport, getObservabilityUrl } from './observability.js';
import { closeSessionServices } from './_session_cleanup.js';

/**
 * Start a transport-layer recording for this session, if appropriate.
 *
 * Gated on observability being configured: the recording's only purpose is to
 * be uploaded as part of the session report, and {@link finalizeSession}
 * uploads then deletes it. Without observability there's nowhere to send it and
 * nothing would clean up the file, so we skip it. This is the single source of
 * the "record iff we'll upload" policy — the symmetric counterpart to
 * {@link finalizeSession}.
 *
 * Returns the recording path + start timestamp, or an empty object when no
 * recording was started.
 */
export function startSessionRecording(
  endpoint: any,
  sessionId: string,
  sessionDir: string,
): { recordingPath?: string; recordingStartedAt?: number } {
  if (!getObservabilityUrl()) return {};
  try {
    mkdirSync(sessionDir, { recursive: true });
    const recordingPath = `${sessionDir}/recording_${sessionId}.ogg`;
    endpoint.startRecording(sessionId, recordingPath, true);
    return { recordingPath, recordingStartedAt: Date.now() };
  } catch {
    return {};
  }
}

export interface FinalizeSessionOptions {
  session: any;
  endpoint: any;
  sessionId: string;
  transport: 'sip' | 'audio_stream';
  agentId: string;
  agentName: string;
  accountId?: string;
  metadata?: Record<string, unknown>;
  direction: string;
  recordingPath?: string;
  recordingStartedAt?: number;
}

/**
 * Shared end-of-session finalize for the SIP and audio_stream Node servers.
 *
 * Ordering is load-bearing:
 *   1. Wait for the session to close so in-flight LLM/TTS responses finalize
 *      into history BEFORE the recorder stops — otherwise the final agent turn
 *      is truncated from both the transcript and the OGG recording.
 *   2. Stop recording and wait for the Rust recorder to flush the file.
 *   3. Upload the report BEFORE closing vendor services.
 *   4. Close vendor STT/TTS/LLM sockets (`close()` does not cascade to them).
 *
 * No-op when no AgentSession was created. Each server keeps its own
 * transport-specific orchestration (hangup, room teardown, bookkeeping).
 */
export async function finalizeSession(opts: FinalizeSessionOptions): Promise<void> {
  const {
    session, endpoint, sessionId, transport, agentId, agentName,
    accountId, metadata, direction, recordingPath, recordingStartedAt,
  } = opts;
  if (!session) return;

  const noun = transport === 'sip' ? 'Call' : 'Session';

  try {
    const usage = (session as any).usage;
    if (usage) {
      console.log(`${noun} ${sessionId} usage:`, JSON.stringify(usage));
    }
  } catch {}

  // Wait for natural session close (preserves in-flight LLM/TTS responses in
  // history). The participant_disconnected event triggers _close_soon(), which
  // does a graceful close. If 'close' never fires within the window, force an
  // explicit close so we don't proceed (and stop recording) mid-drain.
  await new Promise<void>((resolve) => {
    const timer = setTimeout(() => {
      Promise.resolve((session as any).close?.()).catch(() => {}).finally(() => resolve());
    }, 5000);
    session.on('close', () => { clearTimeout(timer); resolve(); });
  });

  // Stop recording and wait for the file to be finalized. Runs AFTER the close
  // wait so the final drained turn is captured.
  try { endpoint.stopRecording(sessionId); } catch {}
  if (recordingPath) {
    for (let i = 0; i < 20; i++) {
      if (existsSync(recordingPath)) break;
      await new Promise((r) => setTimeout(r, 100));
    }
  }

  // Upload session report (transcript, audio, metrics) after close so history
  // is complete. agentId is required to upload — obs keys sessions on it and the
  // sessions table is NOT NULL — so when it's unset we skip (and keep the local
  // recording) rather than write an unparented session. Single gate here,
  // mirroring the Python finalize_session.
  const obsUrl = getObservabilityUrl();
  if (obsUrl && !agentId) {
    console.warn(
      `Skipping session report upload for ${sessionId} — observability is configured ` +
        `but agentId is unset (local recording, if any, is kept).`,
    );
  } else if (obsUrl) {
    try {
      await uploadReport({
        agentId,
        agentName,
        session,
        callId: sessionId,
        accountId,
        metadata,
        direction,
        recordingPath,
        recordingStartedAt,
        transport,
      });
    } catch (e) {
      console.warn(`Failed to upload session report for ${noun.toLowerCase()} ${sessionId}:`, e);
    }

    // Clean up the local recording after the upload attempt — co-located with
    // the upload under the same gate (kept on disk on the skip path above).
    if (recordingPath) {
      try { unlinkSync(recordingPath); } catch (e) {
        console.warn(`Failed to clean up recording ${recordingPath}:`, e);
      }
    }
  }

  // Close vendor STT/TTS/LLM sockets — close() does not cascade to them, so
  // they would leak per session on our long-lived in-process server.
  await closeSessionServices(session, {
    logger: (msg, err) => console.warn(`[${noun.toLowerCase()} ${sessionId}] ${msg}`, err ?? ''),
  });
}
