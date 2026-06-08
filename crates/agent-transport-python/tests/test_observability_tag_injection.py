"""Tests for the chat_history tag injection helper.

The obs server's ``extractAgentId(rawReport)`` reads ``agent_id:<uuid>`` from
``rawReport.tags[]`` (the multipart's chat_history JSON), not from the OTLP
records that arrive later. LiveKit's ``ChatContext.to_dict`` doesn't emit
tagger tags by default, so the adapter merges ``tagger.tags`` into the
chat-history dict before upload. If this helper drifts, sessions return
``400 missing_agent_id`` on every recording POST.
"""

from agent_transport.sip.livekit.observability import merge_tagger_tags_into_dict


def test_injects_tags_into_empty_dict():
    """No existing tags → tagger tags become the full `tags[]` list."""
    result = merge_tagger_tags_into_dict(
        {"items": [{"role": "user", "content": "hi"}]},
        [
            "agent_id:agent-uuid-1",
            "agent_name:support-agent",
            "account_id:acct-1",
        ],
    )

    assert result["items"] == [{"role": "user", "content": "hi"}]
    # Tagger tags are sorted (stable round-trip) and present verbatim.
    assert result["tags"] == sorted(
        ["agent_id:agent-uuid-1", "agent_name:support-agent", "account_id:acct-1"]
    )


def test_preserves_and_dedupes_existing_tags():
    """If LiveKit ever serializes its own tags[], merge instead of replace."""
    result = merge_tagger_tags_into_dict(
        {"items": [], "tags": ["agent_id:agent-uuid-1", "custom_tag"]},
        ["agent_id:agent-uuid-1", "transport:audio_stream"],
    )

    tags = result["tags"]
    assert tags.count("agent_id:agent-uuid-1") == 1, "duplicate not added twice"
    assert "custom_tag" in tags, "pre-existing tag preserved"
    assert "transport:audio_stream" in tags


def test_no_tags_no_mutation():
    """An empty / None tagger should leave the dict unchanged."""
    original = {"items": [{"role": "user", "content": "hi"}]}
    result = merge_tagger_tags_into_dict(original, [])
    assert "tags" not in result, "no tags added when tagger is empty"

    result_none = merge_tagger_tags_into_dict({"items": []}, None)
    assert "tags" not in result_none, "no tags added when tagger is None"


def test_non_dict_input_passes_through():
    """Defensive: if LiveKit ever returns a non-dict, we don't crash."""
    assert merge_tagger_tags_into_dict([1, 2, 3], ["agent_id:x"]) == [1, 2, 3]
    assert merge_tagger_tags_into_dict("string", ["agent_id:x"]) == "string"
    assert merge_tagger_tags_into_dict(None, ["agent_id:x"]) is None


def test_obs_server_extractor_format():
    """End-to-end contract pin: the agent_id prefix tag obs's
    `extractAgentId` slices on (`agent_id:`) must round-trip through the
    helper exactly. If this breaks, the obs server returns 400 on every
    recording multipart from the Python SDK."""
    tags = [
        "agent.session",
        "agent_id:da3d4071-34ce-41b2-8c9e-05eef23a43bb",
        "agent_name:northstar-shopify-bot",
        "account_id:acct-1",
        "transport:audio_stream",
        "evaluations:enabled",
    ]
    result = merge_tagger_tags_into_dict({"items": []}, tags)

    # Mirror obs's extraction logic.
    agent_id = None
    for tag in result["tags"]:
        if isinstance(tag, str) and tag.startswith("agent_id:"):
            agent_id = tag.split(":", 1)[1]
            break
    assert agent_id == "da3d4071-34ce-41b2-8c9e-05eef23a43bb"
