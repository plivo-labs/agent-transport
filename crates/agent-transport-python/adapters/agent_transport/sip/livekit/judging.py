"""Post-session judge integration for the LiveKit transport facade.

The livekit-agents version range that this module's JudgeGroup integration
works against (`>=1.5.2,<1.6`) is pinned in agent-transport-python's
pyproject.toml `[livekit]` extra — pip / uv resolution enforces it, so no
runtime version check is needed here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EvaluationConfig:
    """Per-session post-conversation evaluation configuration."""

    judge_llm: Any
    judges: list[Any] | tuple[Any, ...] = field(default_factory=list)
    auto_outcome: bool = True


async def run_configured_judges(
    *,
    session: Any,
    job_context: Any,
    evaluation: EvaluationConfig | None,
    session_id: str,
    logger: Any,
) -> bool:
    """Run configured LiveKit judges and attach results to the session tagger.

    The LiveKit SDK's JudgeGroup already knows how to serialize evaluation
    results through Tagger. This helper only supplies the post-session
    ChatContext and makes sure the result is attached before observability
    upload runs.
    """
    if evaluation is None:
        return False

    judges = list(evaluation.judges or [])
    if not judges:
        return False
    if evaluation.judge_llm is None:
        logger.warning("Skipping post-session judges for %s: judge_llm is not configured", session_id)
        return False

    make_report = getattr(job_context, "make_session_report", None)
    if not callable(make_report):
        logger.warning("Skipping post-session judges for %s: no make_session_report on job context", session_id)
        return False

    try:
        from livekit.agents.evals import JudgeGroup

        report = make_report(session)
        tagger = getattr(job_context, "tagger", None)
        previous_eval_count = len(getattr(tagger, "evaluations", []) or []) if tagger else 0

        # Mark the session as evaluation-capable BEFORE judges run so the obs
        # UI can show the Evaluation button even when JudgeGroup fails or its
        # results haven't merged yet. Node sessions never reach this branch
        # (no JudgeGroup support), so the flag stays Python-only.
        add_tag = getattr(tagger, "add", None) if tagger else None
        if callable(add_tag):
            try:
                add_tag("evaluations:enabled", metadata=None)
            except Exception:
                pass

        group = JudgeGroup(llm=evaluation.judge_llm, judges=judges)
        result = await group.evaluate(report.chat_history)

        # JudgeGroup auto-tags when get_job_context() is active. In the
        # standalone transport facade, attach defensively if contextvars were
        # not inherited by the cleanup task for any reason.
        current_eval_count = len(getattr(tagger, "evaluations", []) or []) if tagger else 0
        attach = getattr(tagger, "_evaluation", None) if tagger else None
        if current_eval_count == previous_eval_count and callable(attach):
            attach(result)

        if evaluation.auto_outcome and tagger and getattr(tagger, "outcome", None) is None:
            if result.judgments:
                if result.all_passed:
                    tagger.success(reason="All configured post-session judges passed")
                else:
                    tagger.fail(reason="One or more configured post-session judges failed")
            else:
                logger.warning("Post-session judges for %s produced no judgments", session_id)

        logger.info(
            "Post-session judges completed for %s: score=%.2f judgments=%d",
            session_id,
            result.score,
            len(result.judgments),
        )
        return True
    except Exception:
        logger.warning("Failed to run post-session judges for %s", session_id, exc_info=True)
        return False
