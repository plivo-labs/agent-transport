/**
 * Observability setup for agent-transport (Node.js).
 *
 * Delegates to livekit-agents' setupCloudTracer() and uploadSessionReport()
 * so we get the same traces, logs, and session reports as standard LiveKit agents.
 *
 * Set LIVEKIT_OBSERVABILITY_URL to enable.
 */

import { createSessionReport, type SessionReportOptions } from '@livekit/agents/dist/voice/report.js';
import { uploadSessionReport } from '@livekit/agents/dist/telemetry/traces.js';
// TODO: Re-enable OTel observability once we finalize the tracing backend.
// import { setupCloudTracer } from '@livekit/agents/dist/telemetry/traces.js';

export function getObservabilityUrl(): string | undefined {
  return process.env.LIVEKIT_OBSERVABILITY_URL;
}

// TODO: Re-enable OTel observability once we finalize the tracing backend.
// let _initialized = false;
//
// export async function setupObservability(callId: string = 'server'): Promise<boolean> {
//   if (_initialized) return true;
//   const obsUrl = getObservabilityUrl();
//   if (!obsUrl) return false;
//   try {
//     await setupCloudTracer({ roomId: callId, jobId: callId, cloudHostname: obsUrl });
//     _initialized = true;
//     console.log(`Observability configured → ${obsUrl}`);
//     return true;
//   } catch {
//     console.warn('Failed to set up cloud tracer — observability disabled.');
//     return false;
//   }
// }

export async function uploadReport(options: {
  agentName: string;
  session: any;
  callId: string;
  recordingPath?: string;
  recordingStartedAt?: number;
}): Promise<void> {
  const obsUrl = getObservabilityUrl();
  if (!obsUrl) return;

  const { agentName, session, callId, recordingPath, recordingStartedAt } = options;

  const reportOpts: SessionReportOptions = {
    jobId: callId,
    roomId: callId,
    room: callId,
    options: session.options ?? {},
    events: session._recordedEvents ?? [],
    chatHistory: session.history?.copy?.() ?? session.history ?? [],
    enableRecording: !!recordingPath,
    startedAt: session._startedAt ?? Date.now(),
    audioRecordingPath: recordingPath,
    audioRecordingStartedAt: recordingStartedAt,
    modelUsage: session.usage?.modelUsage,
  };

  const report = createSessionReport(reportOpts);

  await uploadSessionReport({
    agentName,
    cloudHostname: obsUrl,
    report,
  });
}
