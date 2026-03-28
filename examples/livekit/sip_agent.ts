/**
 * SIP voice agent with tool calling — handles inbound and outbound calls.
 *
 * Usage:
 *   npx ts-node examples/livekit/sip_agent.ts start     # production
 *   npx ts-node examples/livekit/sip_agent.ts dev       # dev mode
 *   npx ts-node examples/livekit/sip_agent.ts debug     # full debug
 */

import { AgentServer, type CallContext } from '@agent-transport/sip-livekit';
import { voice, llm, metrics } from '@livekit/agents';
import * as deepgram from '@livekit/agents-plugin-deepgram';
import * as openai from '@livekit/agents-plugin-openai';
import * as silero from '@livekit/agents-plugin-silero';
import * as livekit from '@livekit/agents-plugin-livekit';
import { z } from 'zod';

const server = new AgentServer({
  sipUsername: process.env.SIP_USERNAME!,
  sipPassword: process.env.SIP_PASSWORD!,
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
    transferCall: llm.tool({
      description: 'Transfer the call to another phone number or agent.',
      parameters: z.object({
        destination: z.string().describe('The phone number or SIP URI to transfer to'),
      }),
      execute: async ({ destination }) => {
        console.log(`Transfer requested to ${destination}`);
        // TODO: implement SIP REFER or blind transfer
        return `I'm transferring you to ${destination} now. Please hold.`;
      },
    }),
  },
});

server.sipSession(async (ctx: CallContext) => {
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

  // Log metrics as they are emitted
  session.on(voice.AgentSessionEventTypes.MetricsCollected, (ev) => {
    metrics.logMetrics(ev.metrics);
  });

  await ctx.start(session, { agent });

  // Greet the user once the session is started
  session.say('Hello, how can I help you today?');
});

server.run();
