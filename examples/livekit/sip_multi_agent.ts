/**
 * SIP multi-agent with handoff and tool calling.
 *
 * Demonstrates agent handoff using class inheritance (same pattern as LiveKit TS):
 * - GreeterAgent: greets the caller, gathers intent, hands off
 * - SalesAgent: handles product inquiries with tool calling
 * - SupportAgent: handles support issues with tool calling
 *
 * Usage:
 *   npx ts-node examples/livekit/sip_multi_agent.ts start
 *   npx ts-node examples/livekit/sip_multi_agent.ts dev
 */

import { AgentServer, JobProcess, type JobContext } from '@agent-transport/sip-livekit';
import { voice, llm, metrics, getJobContext } from '@livekit/agents';
import * as deepgram from '@livekit/agents-plugin-deepgram';
import * as openai from '@livekit/agents-plugin-openai';
import * as silero from '@livekit/agents-plugin-silero';
import * as livekit from '@livekit/agents-plugin-livekit';
import { z } from 'zod';

type CallData = {
  callerName?: string;
  intent?: string;
};

const server = new AgentServer({
  sipUsername: process.env.SIP_USERNAME!,
  sipPassword: process.env.SIP_PASSWORD!,
  sipServer: process.env.SIP_DOMAIN ?? 'phone.plivo.com',
});

server.setupFnc = (proc: JobProcess) => {
  proc.userData.vad = silero.VAD.load();
  proc.userData.turnDetector = new livekit.turnDetector.MultilingualModel();
};

// ─── Agents (class-based, matching LiveKit TS pattern) ───────────

class GreeterAgent extends voice.Agent<CallData> {
  private _dtmfHandler = (ev: any) => {
    console.log(`DTMF received: ${ev.digit}`);
    if (ev.digit === '1') {
      this.session.generateReply({
        instructions: 'The caller pressed 1 for sales. Ask for their name and route them.',
      });
    } else if (ev.digit === '2') {
      this.session.generateReply({
        instructions: 'The caller pressed 2 for support. Ask for their name and route them.',
      });
    }
  };

  async onEnter() {
    try {
      const jobCtx = getJobContext();
      jobCtx.room.on('sip_dtmf_received', this._dtmfHandler);
    } catch {}

    this.session.generateReply({
      instructions: 'Greet the caller and ask how you can help. Let them know they can press 1 for sales or 2 for support.',
    });
  }

  async onExit() {
    try {
      const jobCtx = getJobContext();
      jobCtx.room.off('sip_dtmf_received', this._dtmfHandler);
    } catch {}
  }

  static create() {
    return new GreeterAgent({
      instructions:
        'You are a friendly receptionist. Greet the caller, ' +
        'ask for their name, and determine if they need sales or support. ' +
        'Keep responses brief. Do not use emojis or markdown.',
      tools: {
        routeToSales: llm.tool({
          description: 'Route to sales when the caller is interested in purchasing.',
          parameters: z.object({
            callerName: z.string().describe("The caller's name"),
          }),
          execute: async ({ callerName }, { ctx }) => {
            ctx.userData.callerName = callerName;
            ctx.userData.intent = 'sales';
            console.log(`Routing ${callerName} to sales`);
            return llm.handoff({
              agent: SalesAgent.create(callerName),
              returns: `Transferring you to our sales team, ${callerName}.`,
            });
          },
        }),
        routeToSupport: llm.tool({
          description: 'Route to support when the caller has an issue.',
          parameters: z.object({
            callerName: z.string().describe("The caller's name"),
          }),
          execute: async ({ callerName }, { ctx }) => {
            ctx.userData.callerName = callerName;
            ctx.userData.intent = 'support';
            console.log(`Routing ${callerName} to support`);
            return llm.handoff({
              agent: SupportAgent.create(callerName),
              returns: `Transferring you to our support team, ${callerName}.`,
            });
          },
        }),
      },
    });
  }
}

class SalesAgent extends voice.Agent<CallData> {
  async onEnter() {
    this.session.generateReply();
  }

  static create(callerName: string) {
    return new SalesAgent({
      instructions:
        `You are a sales agent speaking with ${callerName}. ` +
        'Help with products and pricing. Be enthusiastic but not pushy. ' +
        'Keep responses concise. Do not use emojis or markdown.',
      tools: {
        checkPricing: llm.tool({
          description: 'Look up pricing for a product.',
          parameters: z.object({
            product: z.string().describe('The product name'),
          }),
          execute: async ({ product }) => {
            console.log(`Checking pricing for ${product}`);
            return `${product} starts at $49/month for basic, $99/month for premium.`;
          },
        }),
        checkAvailability: llm.tool({
          description: 'Check if a product is available.',
          parameters: z.object({
            product: z.string().describe('The product to check'),
          }),
          execute: async ({ product }) => {
            return `${product} is available and can be set up within 24 hours.`;
          },
        }),
        transferToSupport: llm.tool({
          description: 'Transfer to support if the caller has an issue.',
          parameters: z.object({}),
          execute: async (_, { ctx }) => {
            const name = ctx.userData.callerName ?? 'caller';
            console.log(`Sales transferring ${name} to support`);
            return llm.handoff({ agent: SupportAgent.create(name) });
          },
        }),
        endCall: llm.tool({
          description: 'End the call when the user is done.',
          parameters: z.object({}),
          execute: async (_, { ctx }) => {
            (ctx as any).session?.shutdown();
            return 'Say goodbye to the user.';
          },
        }),
      },
    });
  }
}

class SupportAgent extends voice.Agent<CallData> {
  async onEnter() {
    this.session.generateReply();
  }

  static create(callerName: string) {
    return new SupportAgent({
      instructions:
        `You are a support agent speaking with ${callerName}. ` +
        'Help resolve their issue. Be empathetic. ' +
        'Keep responses concise. Do not use emojis or markdown.',
      tools: {
        lookupAccount: llm.tool({
          description: "Look up a customer's account.",
          parameters: z.object({
            accountId: z.string().describe('Account ID or email'),
          }),
          execute: async ({ accountId }) => {
            console.log(`Looking up account ${accountId}`);
            return `Account ${accountId} is active, premium plan, last payment 15 days ago.`;
          },
        }),
        createTicket: llm.tool({
          description: 'Create a support ticket.',
          parameters: z.object({
            issueDescription: z.string().describe('Description of the issue'),
          }),
          execute: async ({ issueDescription }) => {
            console.log(`Creating ticket: ${issueDescription}`);
            return 'Support ticket #12345 created. Team will follow up within 2 hours.';
          },
        }),
        transferToSales: llm.tool({
          description: 'Transfer to sales if the caller wants to purchase.',
          parameters: z.object({}),
          execute: async (_, { ctx }) => {
            const name = ctx.userData.callerName ?? 'caller';
            console.log(`Support transferring ${name} to sales`);
            return llm.handoff({ agent: SalesAgent.create(name) });
          },
        }),
        endCall: llm.tool({
          description: 'End the call when the issue is resolved.',
          parameters: z.object({}),
          execute: async (_, { ctx }) => {
            (ctx as any).session?.shutdown();
            return 'Say goodbye to the user.';
          },
        }),
      },
    });
  }
}

// ─── Server ──────────────────────────────────────────────────────

server.sipSession(async (ctx: JobContext) => {
  const session = new voice.AgentSession<CallData>({
    vad: ctx.proc.userData.vad as silero.VAD,
    stt: new deepgram.STT({ model: 'nova-3' }),
    llm: new openai.LLM({ model: 'gpt-4.1-mini' }),
    tts: new openai.TTS({ voice: 'alloy' }),
    userData: {} as CallData,
    turnHandling: {
      turnDetection: ctx.proc.userData.turnDetector as livekit.turnDetector.MultilingualModel,
    },
    preemptiveGeneration: true,
    aecWarmupDuration: 3000,
    ttsTextTransforms: ['filterEmoji', 'filterMarkdown'],
  });

  session.on(voice.AgentSessionEventTypes.MetricsCollected, (ev) => {
    metrics.logMetrics(ev.metrics);
  });

  ctx.session = session;
  await session.start({ agent: GreeterAgent.create(), room: ctx.room });
});

server.run();
