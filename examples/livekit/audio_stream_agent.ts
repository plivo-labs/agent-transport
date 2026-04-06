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
 *   npx ts-node examples/livekit/audio_stream_agent.ts
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
      description: 'End the call when the user is done.',
      parameters: z.object({}),
      execute: async (_, ctx) => {
        console.log('End call requested');
        (ctx as any).session.shutdown();
        return 'Say goodbye to the user.';
      },
    }),
  },
});

server.audioStreamSession(async (ctx: AudioStreamJobContext) => {
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
