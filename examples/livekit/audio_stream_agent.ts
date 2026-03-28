/**
 * Audio streaming voice agent with tool calling and DTMF support.
 *
 * Plivo connects to your WebSocket server and streams audio bidirectionally.
 * No SIP credentials needed — configure Plivo XML to point to your server.
 *
 * Uses the same LiveKit Agents patterns as WebRTC — getJobContext().room works,
 * DTMF events come through room.on("sip_dtmf_received"), built-in tools work.
 *
 * Same Agent code works with both SIP and audio streaming.
 * TS uses AgentServer for both transports; Python has separate
 * AgentServer (SIP) and AudioStreamServer (Plivo WebSocket).
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
 *   npx ts-node examples/livekit/audio_stream_agent.ts start
 *   npx ts-node examples/livekit/audio_stream_agent.ts dev
 */

import { AgentServer, type JobContext } from '@agent-transport/sip-livekit';
import { voice, llm, metrics } from '@livekit/agents';
import { getJobContext } from '@livekit/agents';
import * as deepgram from '@livekit/agents-plugin-deepgram';
import * as openai from '@livekit/agents-plugin-openai';
import * as silero from '@livekit/agents-plugin-silero';
import * as livekit from '@livekit/agents-plugin-livekit';
import { z } from 'zod';

const server = new AgentServer({
  sipUsername: process.env.SIP_USERNAME ?? '',
  sipPassword: process.env.SIP_PASSWORD ?? '',
  sipServer: process.env.SIP_DOMAIN ?? 'phone.plivo.com',
});

server.setup(() => ({
  vad: silero.VAD.load(),
  turnDetector: new livekit.turnDetector.MultilingualModel(),
}));

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
      execute: async (_, ctx) => {
        console.log('End call requested');
        (ctx as any).session.shutdown();
        return 'Say goodbye to the user.';
      },
    }),
  },
});

server.sipSession(async (ctx: JobContext) => {
  const session = new voice.AgentSession({
    vad: ctx.userdata.vad as silero.VAD,
    stt: new deepgram.STT({ model: 'nova-3' }),
    llm: new openai.LLM({ model: 'gpt-4.1-mini' }),
    tts: new openai.TTS({ voice: 'alloy' }),
    turnHandling: {
      turnDetection: ctx.userdata.turnDetector as livekit.turnDetector.MultilingualModel,
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
  await session.start({ agent, room: ctx.room });

  session.say('Hello, how can I help you today?');
});

server.run();
