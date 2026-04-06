/**
 * SIP voice agent with tool calling — handles inbound and outbound calls.
 *
 * Usage:
 *   npx ts-node examples/livekit/sip_agent.ts start     # production
 *   npx ts-node examples/livekit/sip_agent.ts dev       # dev mode
 *   npx ts-node examples/livekit/sip_agent.ts debug     # full debug
 */

import { AgentServer, JobProcess, type JobContext } from 'agent-transport/livekit';
import { voice, llm, metrics, getJobContext } from '@livekit/agents';
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

server.setupFnc = async (proc: JobProcess) => {
  proc.userData.vad = await silero.VAD.load();
  proc.userData.turnDetector = new livekit.turnDetector.MultilingualModel();
};

server.sipSession(async (ctx: JobContext) => {
  // Create a fresh Agent per call — agent._agentActivity persists across calls
  // and prevents reuse of the same Agent instance for multiple sessions.
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
        execute: async (_, runCtx) => {
          console.log('End call requested');
          try { (runCtx as any).session?.shutdown(); } catch {}
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
          return `I'm transferring you to ${destination} now. Please hold.`;
        },
      }),
    },
  });
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


  // Log metrics as they are emitted
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

  // Greet the user — session.say() generates a reply using the agent's instructions
  session.say('Hello, how can I help you today?');
});

server.run();
