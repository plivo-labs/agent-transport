# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "agent-transport[livekit]",
#     "livekit-agents>=1.5.2",
#     "python-dotenv",
#     "livekit-plugins-deepgram",
#     "livekit-plugins-openai",
#     "livekit-plugins-silero",
#     "livekit-plugins-turn-detector",
# ]
# ///
"""Audio streaming Shopify support voice agent with post-session LiveKit judges.

This is the audio stream transport version of sip_agent_with_judges.py.
Plivo connects to this WebSocket server and streams audio bidirectionally.
The JobContext is configured with a judge LLM and LiveKit SDK Judge objects.
After each stream ends, agent-transport builds the LiveKit SessionReport, runs
JudgeGroup.evaluate(...) for the configured judges, and lets the SDK Tagger
send those evaluation results through native LiveKit observability uploads.

Try calls like:
    "Where is order 1001?"
    "Can I return order 1001 because it is too small?"
    "Change the shipping address on order 1002."

Setup:
    Configure Plivo XML answer URL to return:
    <Response>
        <Stream bidirectional="true" keepCallAlive="true"
            contentType="audio/x-mulaw;rate=8000">
            wss://your-server:8765
        </Stream>
    </Response>

Required for calls:
    OPENAI_API_KEY
    DEEPGRAM_API_KEY

Optional for Plivo signature validation:
    PLIVO_AUTH_ID
    PLIVO_AUTH_TOKEN

Required for post-session observability uploads:
    AGENT_OBSERVABILITY_URL # for example, http://localhost:9090
    LIVEKIT_API_KEY
    LIVEKIT_API_SECRET

Optional for dashboard account correlation:
    AGENT_ACCOUNT_ID        # any account/customer/tenant id value

Usage:
    uv run examples/livekit/audio_stream_agent_with_judges.py start
    uv run examples/livekit/audio_stream_agent_with_judges.py dev

Companion pytest sims (same agent_id, so live + sim land on one agent
record in the obs dashboard):
    agent-observability/plugins/examples/pytest/pytest_shopify_agent.py
    agent-observability/plugins/examples/pytest/pytest_shopify_with_judges.py
"""

import logging
import os

from dotenv import load_dotenv

from agent_transport.audio_stream.livekit import AudioStreamServer, EvaluationConfig, JobContext, JobProcess

from livekit.agents import Agent, AgentSession, RunContext, TurnHandlingOptions
from livekit.agents.beta.tools import EndCallTool, send_dtmf_events
from livekit.agents.evals import (
    Judge,
    JudgmentResult,
    accuracy_judge,
    safety_judge,
    task_completion_judge,
    tool_use_judge,
)
from livekit.agents.job import get_job_context
from livekit.agents.llm import ChatContext, function_tool
from livekit.agents.voice.background_audio import BackgroundAudioPlayer, BuiltinAudioClip
from livekit.plugins import deepgram, openai, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

load_dotenv()

logger = logging.getLogger("audio-stream-agent-with-judges")


SHOPIFY_ORDERS = {
    "1001": {
        "account_id": "northstar-vip-account",
        "customer": "Maya Chen",
        "email": "maya@example.com",
        "item": "Trail Runner Jacket",
        "status": "delivered",
        "tracking": "1Z999AA10123456784",
        "carrier": "UPS",
        "delivered_on": "April 22, 2026",
        "return_window_until": "May 22, 2026",
        "total": "$128.40",
        "can_cancel": "no",
    },
    "1002": {
        "account_id": "northstar-demo-account",
        "customer": "Arjun Patel",
        "email": "arjun@example.com",
        "item": "Everyday Canvas Backpack",
        "status": "processing",
        "tracking": "not shipped yet",
        "carrier": "not assigned yet",
        "delivered_on": "",
        "return_window_until": "",
        "total": "$86.10",
        "can_cancel": "yes",
    },
    "1003": {
        "account_id": "northstar-demo-account",
        "customer": "Sam Rivera",
        "email": "sam@example.com",
        "item": "Insulated Coffee Tumbler",
        "status": "in transit",
        "tracking": "9400111206213957123456",
        "carrier": "USPS",
        "delivered_on": "",
        "return_window_until": "",
        "total": "$32.00",
        "can_cancel": "no",
    },
}

SHOPIFY_ACCOUNTS = {
    "northstar-demo-account": {
        "customer": "Sam Rivera",
        "email": "sam@example.com",
        "tier": "Trail Club",
        "status": "active",
        "preferred_channel": "email",
        "saved_address": "1420 Pine Street, Seattle, WA 98101",
        "recent_order_ids": ["1002", "1003"],
        "open_ticket": "NST-1003-0429",
    },
    "northstar-vip-account": {
        "customer": "Maya Chen",
        "email": "maya@example.com",
        "tier": "Summit",
        "status": "active",
        "preferred_channel": "sms",
        "saved_address": "88 Market Street, San Francisco, CA 94105",
        "recent_order_ids": ["1001"],
        "open_ticket": "",
    },
}

STORE_POLICIES = {
    "returns": (
        "Most items can be returned within 30 days of delivery if unused and in "
        "original condition. Final-sale items cannot be returned."
    ),
    "shipping": (
        "Standard shipping takes 3 to 5 business days after fulfillment. "
        "Expedited shipping takes 1 to 2 business days."
    ),
    "cancellations": (
        "Orders can be cancelled only while they are still processing. "
        "After shipment, the customer can start a return after delivery."
    ),
}


def _account_id() -> str:
    return os.environ.get("AGENT_ACCOUNT_ID", "northstar-demo-account")


def _message_texts(chat_ctx: ChatContext, *, role: str | None = None) -> list[str]:
    texts: list[str] = []
    for message in chat_ctx.messages():
        if role is not None and message.role != role:
            continue
        text = message.text_content
        if text:
            texts.append(text)
    return texts


class PlainSpeechJudge(Judge):
    """Checks that phone responses stay plain spoken, not markdown-formatted."""

    def __init__(self) -> None:
        super().__init__(name="plain_speech")

    async def evaluate(
        self,
        *,
        chat_ctx: ChatContext,
        reference: ChatContext | None = None,
        llm=None,
    ) -> JudgmentResult:
        assistant_text = "\n".join(_message_texts(chat_ctx, role="assistant"))
        markdown_markers = ["```", "**", "__", "# ", "- "]
        found = [marker for marker in markdown_markers if marker in assistant_text]

        return JudgmentResult(
            verdict="fail" if found else "pass",
            reasoning=(
                f"Found markdown markers: {', '.join(found)}"
                if found
                else "Assistant responses were plain speech."
            ),
            instructions="Assistant phone responses should not contain markdown formatting.",
        )


class ConciseTurnsJudge(Judge):
    """Checks that individual assistant turns stay concise for a phone call."""

    def __init__(self, *, max_words: int = 70) -> None:
        super().__init__(name="concise_turns")
        self._max_words = max_words

    async def evaluate(
        self,
        *,
        chat_ctx: ChatContext,
        reference: ChatContext | None = None,
        llm=None,
    ) -> JudgmentResult:
        assistant_turns = _message_texts(chat_ctx, role="assistant")
        if not assistant_turns:
            return JudgmentResult(
                verdict="fail",
                reasoning="No assistant messages were captured.",
                instructions="The assistant should respond during the call.",
            )

        longest = max(len(text.split()) for text in assistant_turns)
        return JudgmentResult(
            verdict="pass" if longest <= self._max_words else "fail",
            reasoning=f"Longest assistant turn was {longest} words.",
            instructions=(
                "Each assistant turn should stay under "
                f"{self._max_words} words for a phone conversation."
            ),
        )


class FinalUserAcknowledgedJudge(Judge):
    """Flags calls where the final user turn did not get an assistant response."""

    def __init__(self) -> None:
        super().__init__(name="final_user_acknowledged")

    async def evaluate(
        self,
        *,
        chat_ctx: ChatContext,
        reference: ChatContext | None = None,
        llm=None,
    ) -> JudgmentResult:
        messages = chat_ctx.messages()
        last_user_index = next(
            (index for index in range(len(messages) - 1, -1, -1) if messages[index].role == "user"),
            None,
        )
        if last_user_index is None:
            return JudgmentResult(
                verdict="maybe",
                reasoning="No user messages were captured.",
                instructions="Evaluate whether the caller had a chance to state their request.",
            )

        assistant_after_user = any(
            message.role == "assistant" for message in messages[last_user_index + 1 :]
        )
        return JudgmentResult(
            verdict="pass" if assistant_after_user else "maybe",
            reasoning=(
                "The assistant responded after the final user turn."
                if assistant_after_user
                else "The stream ended before the final user turn received an assistant response."
            ),
            instructions=(
                "The final substantive user request should be acknowledged before the call ends."
            ),
        )


server = AudioStreamServer(
    listen_addr=os.environ.get("AUDIO_STREAM_ADDR", "0.0.0.0:8765"),
    plivo_auth_id=os.environ.get("PLIVO_AUTH_ID", ""),
    plivo_auth_token=os.environ.get("PLIVO_AUTH_TOKEN", ""),
    # Stable opaque UUID4 — uploaded with every session so the obs
    # dashboard groups this agent's calls together. Matches the id used
    # by the pytest simulation file
    # `agent-observability/plugins/examples/pytest/pytest_shopify_agent.py`
    # so live conversation evals and simulation evals land under one
    # agent in the dashboard. AGENT_ID env wins when set.
    agent_id=os.environ.get("AGENT_ID", "da3d4071-34ce-41b2-8c9e-05eef23a43bb"),
    agent_name=os.environ.get("AGENT_NAME", "northstar-shopify-bot"),
)


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()
    proc.userdata["turn_detector"] = MultilingualModel()


server.setup_fnc = prewarm


class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You are a Shopify support phone agent for Northstar Outfitters. "
                "Help callers with account profile questions, order status, "
                "returns, shipping policies, address changes, and IVR touch-tone "
                "menus when needed. Keep responses concise and conversational. "
                f"The current account id is {_account_id()}. "
                "Use lookup_account for account profile questions. "
                "For order-specific questions, ask for the order ID if missing. Always use "
                "lookup_order before stating order status, delivery, cancellation, "
                "or tracking details. Use check_return_eligibility before creating "
                "a return label. Use get_store_policy for policy questions. "
                "Do not invent order data; if a tool cannot verify it, say you "
                "need the caller to confirm the details. Do not use emojis, "
                "asterisks, markdown, or special formatting."
            ),
            tools=[send_dtmf_events, EndCallTool()],
        )

    async def on_enter(self) -> None:
        job_ctx = get_job_context()
        job_ctx.room.on("sip_dtmf_received", self._on_dtmf)
        self.session.say("Thanks for calling Northstar Outfitters. How can I help?")

    def _on_dtmf(self, ev) -> None:
        logger.info("DTMF received: digit=%s code=%d", ev.digit, ev.code)

    @function_tool
    async def lookup_account(self, context: RunContext, account_id: str = "") -> str:
        """Look up a Shopify customer account.

        Args:
            account_id: The account ID to look up. Use the current account ID if omitted.
        """
        resolved_account_id = account_id or _account_id()
        logger.info("Looking up account %s", resolved_account_id)
        account = SHOPIFY_ACCOUNTS.get(resolved_account_id)
        if not account:
            return f"No account found for {resolved_account_id}. Ask the caller to confirm it."

        return (
            f"Account {resolved_account_id}: customer={account['customer']}, "
            f"email={account['email']}, tier={account['tier']}, status={account['status']}, "
            f"preferred_channel={account['preferred_channel']}, "
            f"saved_address={account['saved_address']}, "
            f"recent_order_ids={', '.join(account['recent_order_ids'])}, "
            f"open_ticket={account['open_ticket'] or 'none'}."
        )

    @function_tool
    async def lookup_order(self, context: RunContext, order_id: str) -> str:
        """Look up a Shopify order by order ID.

        Args:
            order_id: The order ID the caller is asking about, for example 1001
        """
        logger.info("Looking up order %s", order_id)
        order = SHOPIFY_ORDERS.get(order_id.strip().lstrip("#"))
        if not order:
            return f"No order found for {order_id}. Ask the caller to confirm the order ID."

        return (
            f"Order {order_id}: account_id={order['account_id']}, "
            f"customer={order['customer']}, item={order['item']}, "
            f"status={order['status']}, carrier={order['carrier']}, "
            f"tracking={order['tracking']}, delivered_on={order['delivered_on'] or 'not delivered'}, "
            f"return_window_until={order['return_window_until'] or 'not available'}, "
            f"can_cancel={order['can_cancel']}, total={order['total']}."
        )

    @function_tool
    async def check_return_eligibility(self, context: RunContext, order_id: str) -> str:
        """Check whether an order is eligible for a return.

        Args:
            order_id: The order ID to check for return eligibility
        """
        logger.info("Checking return eligibility for order %s", order_id)
        normalized = order_id.strip().lstrip("#")
        order = SHOPIFY_ORDERS.get(normalized)
        if not order:
            return f"No order found for {order_id}. Ask the caller to confirm the order ID."
        if order["status"] != "delivered":
            return (
                f"Order {normalized} is not return-eligible yet because its status is "
                f"{order['status']}. Returns can start after delivery."
            )
        return (
            f"Order {normalized} is eligible for return until "
            f"{order['return_window_until']} if the item is unused and in original condition."
        )

    @function_tool
    async def create_return_label(self, context: RunContext, order_id: str, reason: str) -> str:
        """Create a return label for an eligible order.

        Args:
            order_id: The order ID that should be returned
            reason: The customer's reason for the return
        """
        logger.info("Creating return label for order %s", order_id)
        normalized = order_id.strip().lstrip("#")
        order = SHOPIFY_ORDERS.get(normalized)
        if not order:
            return f"No order found for {order_id}. Return label was not created."
        if order["status"] != "delivered":
            return (
                f"Return label was not created for order {normalized}. "
                f"The order status is {order['status']}, so it is not return-eligible yet."
            )
        return (
            f"Return label created for order {normalized}. RMA=RMA-{normalized}-0429, "
            f"reason={reason}, label_url=https://returns.example.com/RMA-{normalized}-0429."
        )

    @function_tool
    async def update_shipping_address(self, context: RunContext, order_id: str, new_address: str) -> str:
        """Update shipping address when an order has not shipped.

        Args:
            order_id: The order ID to update
            new_address: The corrected shipping address
        """
        logger.info("Updating address for order %s", order_id)
        normalized = order_id.strip().lstrip("#")
        order = SHOPIFY_ORDERS.get(normalized)
        if not order:
            return f"No order found for {order_id}. Shipping address was not updated."
        if order["status"] != "processing":
            return (
                f"Shipping address was not updated for order {normalized}. "
                f"The order is {order['status']} and can no longer be edited."
            )
        return f"Shipping address updated for order {normalized} to: {new_address}."

    @function_tool
    async def get_store_policy(self, context: RunContext, topic: str) -> str:
        """Look up a store policy.

        Args:
            topic: Policy topic, such as returns, shipping, or cancellations
        """
        logger.info("Looking up policy topic %s", topic)
        normalized = topic.lower().strip()
        for key, policy in STORE_POLICIES.items():
            if key in normalized:
                return f"{key.title()} policy: {policy}"
        return "Available policies are returns, shipping, and cancellations."


@server.audio_stream_session()
async def entrypoint(ctx: JobContext):
    # In production, set this from your routing or customer metadata.
    ctx.set_metadata({"account_id": _account_id()})
    ctx.evaluation = EvaluationConfig(
        judge_llm=openai.LLM(model=os.environ.get("JUDGE_LLM_MODEL", "gpt-4.1-mini")),
        judges=[
            # Built-in LiveKit LLM judges. These use judge_llm through
            # JudgeGroup.evaluate(...).
            task_completion_judge(),
            accuracy_judge(),  # grounding / hallucination check against tool outputs
            tool_use_judge(),
            safety_judge(),
            # Small deterministic checks can still live next to LLM judges.
            PlainSpeechJudge(),
            ConciseTurnsJudge(max_words=int(os.environ.get("JUDGE_MAX_WORDS", "70"))),
            FinalUserAcknowledgedJudge(),
        ],
    )

    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=deepgram.STT(model="nova-3"),
        llm=openai.LLM(model="gpt-4.1-mini"),
        tts=openai.TTS(voice="alloy"),
        turn_handling=TurnHandlingOptions(
            turn_detection=ctx.proc.userdata["turn_detector"],
        ),
        preemptive_generation=True,
        aec_warmup_duration=3.0,
        tts_text_transforms=["filter_emoji", "filter_markdown"],
    )
    ctx.session = session

    bg_audio = BackgroundAudioPlayer(
        ambient_sound=BuiltinAudioClip.OFFICE_AMBIENCE,
        thinking_sound=BuiltinAudioClip.KEYBOARD_TYPING,
    )
    await bg_audio.start(room=ctx.room, agent_session=session)

    await session.start(agent=Assistant(), room=ctx.room)


if __name__ == "__main__":
    server.run()
