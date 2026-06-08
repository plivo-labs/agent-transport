/**
 * Audio streaming voice agent with tool calling and DTMF support.
 *
 * Plivo connects to your WebSocket server and streams audio bidirectionally.
 * No SIP credentials needed — configure Plivo XML to point to your server.
 *
 * Setup:
 *   Configure Plivo XML answer URL to return:
 *   <Response>
 *     <Stream bidirectional="true" keepCallAlive="true"
 *       contentType="audio/x-mulaw;rate=8000">
 *       wss://your-server:8765
 *     </Stream>
 *   </Response>
 *
 * Usage:
 *   cd examples/livekit && npm install
 *   npx tsx audio_stream_agent.ts
 */

import { AudioStreamServer, JobProcess, type AudioStreamJobContext } from 'agent-transport/livekit';
import { voice, llm, metrics, getJobContext } from '@livekit/agents';
import * as deepgram from '@livekit/agents-plugin-deepgram';
import * as openai from '@livekit/agents-plugin-openai';
import * as silero from '@livekit/agents-plugin-silero';
import * as livekit from '@livekit/agents-plugin-livekit';
import { z } from 'zod';

const server = new AudioStreamServer({
  listenAddr: process.env.AUDIO_STREAM_ADDR ?? '0.0.0.0:8765',
  plivoAuthId: process.env.PLIVO_AUTH_ID ?? '',
  plivoAuthToken: process.env.PLIVO_AUTH_TOKEN ?? '',
  // Shared with sip_agent.{py,ts} so both transports of this demo
  // land on one agent record in obs. Override with AGENT_ID env.
  agentId: process.env.AGENT_ID ?? 'f90d7c10-bf35-4f26-bf3e-29d20ec857cb',
  agentName: process.env.AGENT_NAME ?? 'demo-phone-assistant',
});

server.setupFnc = async (proc: JobProcess) => {
  proc.userData.vad = await silero.VAD.load();
  proc.userData.turnDetector = new livekit.turnDetector.MultilingualModel();
};

const agent = new voice.Agent({
  instructions:
    'You are a helpful phone assistant. ' +
    'Keep responses concise and conversational. ' +
    'Do not use emojis, asterisks, markdown, or special formatting.',
  tools: {
    lookupWeather: llm.tool({
      description: 'Look up weather for a location.',
      parameters: z.object({
        location: z.string().describe('The location to look up weather for'),
      }),
      execute: async ({ location }) => {
        console.log(`Looking up weather for ${location}`);
        return `The weather in ${location} is sunny with a temperature of 72 degrees.`;
      },
    }),
    endCall: llm.tool({
      description:
        'End the call when the user is done. ' +
        'Call when the user says goodbye or indicates they are finished.',
      parameters: z.object({}),
      // The second arg is ToolOptions = { ctx, toolCallId, abortSignal },
      // NOT the RunContext directly. The AgentSession lives on opts.ctx.session.
      execute: async (_args, opts) => {
        console.log('End call requested');
        const ctx = (opts as any).ctx;
        const speechHandle = ctx?.speechHandle;
        const session = ctx?.session;
        // Defer shutdown until after the goodbye has played. Mirrors
        // the Python EndCallTool pattern: speech_handle.add_done_callback
        // so the goodbye finishes naturally before we tear down the call.
        if (speechHandle && session) {
          speechHandle.addDoneCallback(() => {
            try { session.shutdown(); } catch (e) { console.error('session.shutdown failed:', e); }
          });
        } else if (session) {
          try { session.shutdown(); } catch (e) { console.error('session.shutdown failed:', e); }
        }
        return 'Say goodbye to the user.';
      },
    }),
    sendDtmf: llm.tool({
      description:
        'Send DTMF touch-tone digits into the active call. ' +
        'Use this to navigate IVR menus, enter PINs, or trigger touch-tone actions.',
      parameters: z.object({
        digits: z.string().describe('Digits to send (0-9, *, #, A-D)'),
      }),
      // LiveKit JS SDK has no built-in `send_dtmf_events` tool (Python does),
      // so we define the outbound DTMF tool here. Routes through our
      // TransportLocalParticipant.publishDtmf → endpoint.sendDtmfAsync →
      // Plivo `{"event":"sendDTMF","dtmf":"..."}` WS frame.
      execute: async ({ digits }, _opts) => {
        console.log(`Sending DTMF digits: ${digits}`);
        try {
          const jobCtx = getJobContext();
          const lp = (jobCtx.room as any).localParticipant;
          // Send digits one at a time to match upstream send_dtmf_events timing.
          for (const digit of digits) {
            await lp.publishDtmf({ code: digit.charCodeAt(0), digit });
            await new Promise((r) => setTimeout(r, 300));
          }
          return `Successfully sent DTMF digits: ${digits}`;
        } catch (e: any) {
          return `Failed to send DTMF: ${e?.message ?? String(e)}`;
        }
      },
    }),
  },
});

server.audioStreamSession(async (ctx: AudioStreamJobContext) => {
  ctx.setMetadata({ account_id: process.env.AGENT_ACCOUNT_ID ?? 'demo-account' });

  const session = new voice.AgentSession({
    vad: ctx.proc.userData.vad as silero.VAD,
    stt: new deepgram.STT({ model: 'nova-3' }),
    llm: new openai.LLM({ model: 'gpt-4.1-mini' }),
    tts: new openai.TTS({ voice: 'alloy' }),
    turnHandling: {
      turnDetection: ctx.proc.userData.turnDetector as livekit.turnDetector.MultilingualModel,
    },
    preemptiveGeneration: true,
    aecWarmupDuration: 3000,
    ttsTextTransforms: ['filterEmoji', 'filterMarkdown'],
  });

  try {
    const jobCtx = getJobContext();
    jobCtx.room.on('sip_dtmf_received', (ev: any) => {
      console.log(`DTMF received: digit=${ev.digit} code=${ev.code}`);
    });
  } catch {}

  session.on(voice.AgentSessionEventTypes.MetricsCollected, (ev) => {
    metrics.logMetrics(ev.metrics);
  });

  ctx.session = session;

  // Background audio — ambient plays continuously, thinking plays while agent processes
  const bgAudio = new voice.BackgroundAudioPlayer({
    ambientSound: voice.BuiltinAudioClip.OFFICE_AMBIENCE,
    thinkingSound: voice.BuiltinAudioClip.KEYBOARD_TYPING,
  });
  await bgAudio.start({ room: ctx.room, agentSession: session });

  await session.start({ agent, room: ctx.room });

  session.say('Hello, how can I help you today?');
});

server.run();
