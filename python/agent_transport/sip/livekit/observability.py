"""Observability setup for agent-transport.

Delegates to livekit-agents' _setup_cloud_tracer() so we get the same
traces, logs, and recording support as standard LiveKit agents.

Set LIVEKIT_OBSERVABILITY_URL to enable.
"""

import logging
import os

logger = logging.getLogger("agent_transport.observability")

_initialized = False


def _get_observability_url() -> str | None:
    return os.environ.get("LIVEKIT_OBSERVABILITY_URL")


def setup_observability(*, call_id: int | str = "server") -> bool:
    """Set up OTel trace and log export via LiveKit's built-in telemetry.

    Returns True if observability was configured, False otherwise.
    """
    global _initialized
    if _initialized:
        return True

    obs_url = _get_observability_url()
    if not obs_url:
        return False

    try:
        from livekit.agents.telemetry.traces import _setup_cloud_tracer
    except ImportError:
        logger.warning(
            "livekit-agents telemetry not available — observability disabled."
        )
        return False

    _setup_cloud_tracer(
        room_id=str(call_id),
        job_id=str(call_id),
        observability_url=obs_url,
    )

    _initialized = True
    logger.info("Observability configured → %s", obs_url)
    return True


def shutdown_observability() -> None:
    """Flush and shut down OTel exporters."""
    global _initialized
    if not _initialized:
        return

    try:
        from livekit.agents.telemetry.traces import _shutdown_telemetry
        _shutdown_telemetry()
    except Exception:
        logger.debug("telemetry shutdown error", exc_info=True)

    _initialized = False
