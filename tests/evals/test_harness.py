# -*- coding: utf-8 -*-
"""
tests/evals/test_harness.py — Tests for evaluation harness behavior.

Specifically validates:
1. When simulator emits [END], the [END] token is stripped before delivering to the assistant.
2. The assistant receives the message and is allowed to respond and execute tool calls (e.g. end_my_session).
3. The loop stops immediately after the assistant completes that turn without re-invoking the simulator.
"""

from unittest.mock import MagicMock, patch
import pytest

from evals.run import run_persona


def test_simulator_end_marker_lifecycle():
    """Verify [END] is stripped, assistant responds with tool call, and then loop terminates."""
    fake_persona = {
        "id": "C99",
        "role": "citizen",
        "name": "Test Citizen",
        "mobile": "9990000099",
        "goal": "Test lifecycle",
        "how_they_talk": "Polite",
        "turn_budget": 5,
        "max_citizen_messages": 5,
        "non_interference": False,
        "must_be_true": {},
    }

    # Setup simulated conversation:
    # Turn 1: Simulator speaks -> Assistant speaks
    # Turn 2: Simulator speaks with [END] -> Assistant calls end_my_session -> Assistant says Goodbye -> Loop ends
    sim_responses = [
        "Namaste, I want to apply for pension.",
        "Aapka bahut dhanyawad beta! [END]",
    ]
    sim_call_idx = 0

    def mock_sim_turn(*args, **kwargs):
        nonlocal sim_call_idx
        res = sim_responses[sim_call_idx]
        sim_call_idx += 1
        return res

    # Assistant responses:
    # 1. First response: text reply asking for details
    msg1 = MagicMock()
    msg1.tool_calls = None
    msg1.content = "Namaste. Please tell me your age."
    resp1 = MagicMock()
    resp1.choices = [MagicMock(message=msg1)]

    # 2. In response to Turn 2 ("Aapka bahut dhanyawad beta!"), assistant calls end_my_session tool
    tc = MagicMock()
    tc.id = "call_end_123"
    tc.function.name = "end_my_session"
    tc.function.arguments = "{}"
    msg2 = MagicMock()
    msg2.tool_calls = [tc]
    msg2.content = None
    resp2 = MagicMock()
    resp2.choices = [MagicMock(message=msg2)]

    # 3. After tool response, assistant emits final goodbye
    msg3 = MagicMock()
    msg3.tool_calls = None
    msg3.content = "Aapka swagat hai. Session samapt ho gaya. Namaste."
    resp3 = MagicMock()
    resp3.choices = [MagicMock(message=msg3)]

    asst_responses = [resp1, resp2, resp3]
    asst_call_idx = 0
    passed_messages = []

    def mock_asst_call(*args, **kwargs):
        nonlocal asst_call_idx
        # Record what messages were delivered to the assistant
        msgs = kwargs.get("messages", [])
        passed_messages.append([dict(m) for m in msgs])
        res = asst_responses[asst_call_idx]
        asst_call_idx += 1
        return res

    mock_client = MagicMock()
    mock_token = MagicMock(access_token="fake-token")
    mock_client.obtain_citizen_token.return_value = mock_token

    executed_tools = []
    def mock_tool_exec(client, url, token, name, args):
        executed_tools.append((name, args))
        return {"result": {"content": [{"text": "Session ended successfully"}]}}

    with patch("evals.run.cleanup_persona"), \
         patch("evals.run.create_persona_state"), \
         patch("evals.run.fetch_mcp_tools", return_value=[{"type": "function", "function": {"name": "end_my_session", "parameters": {}}}]), \
         patch("evals.run.run_simulator_turn", side_effect=mock_sim_turn), \
         patch("evals.run.call_openai_with_backoff", side_effect=mock_asst_call), \
         patch("evals.run.execute_tool_call", side_effect=mock_tool_exec), \
         patch("evals.run.run_checks", return_value=({"all": True}, {})):

        result = run_persona(
            persona=fake_persona,
            rep=1,
            client=mock_client,
            asst_client=MagicMock(),
            asst_config={"model": "test-asst-model"},
            sim_client=MagicMock(),
            sim_config={"model": "test-sim-model"},
            pace_seconds=0.0,
            max_calls=10,
        )

    # 1. Verify simulator was called exactly 2 times (not a 3rd time after [END])
    assert sim_call_idx == 2, f"Expected 2 simulator calls, got {sim_call_idx}"

    # 2. Verify that the [END] token was stripped from the delivered message
    # In passed_messages for the final assistant turns, find the user message
    last_user_msg = None
    for m in passed_messages[-1]:
        if m["role"] == "user":
            last_user_msg = m["content"]
    assert last_user_msg is not None
    assert "[END]" not in last_user_msg
    assert last_user_msg == "Aapka bahut dhanyawad beta!"

    # 3. Verify assistant executed end_my_session tool call
    assert any(name == "end_my_session" for name, _ in executed_tools), "Assistant failed to execute end_my_session"

    # 4. Verify history contains clean user message and assistant closing
    history = result["history"]
    user_msgs = [h["content"] for h in history if h["role"] == "citizen"]
    asst_msgs = [h["content"] for h in history if h["role"] == "assistant"]

    assert "Aapka bahut dhanyawad beta!" in user_msgs
    assert not any("[END]" in m for m in user_msgs)
    assert any("Session samapt" in m for m in asst_msgs)


def test_quota_exhausted_classified_as_error():
    """Verify that quota exhaustion raises QuotaExhaustedError, sets status ERROR, and cleans up."""
    fake_persona = {
        "id": "C99",
        "role": "citizen",
        "name": "Test Citizen",
        "mobile": "9990000099",
        "turn_budget": 5,
        "max_citizen_messages": 5,
        "non_interference": False,
        "must_be_true": {},
    }

    mock_client = MagicMock()
    mock_token = MagicMock(access_token="fake-token")
    mock_client.obtain_citizen_token.return_value = mock_token

    from evals.run import QuotaExhaustedError

    with patch("evals.run.cleanup_persona") as mock_cleanup, \
         patch("evals.run.create_persona_state"), \
         patch("evals.run.fetch_mcp_tools", return_value=[]), \
         patch("evals.run.run_simulator_turn", return_value="Hello, apply please"), \
         patch("evals.run.call_openai_with_backoff", side_effect=QuotaExhaustedError("Rate limit / token quota exhausted after 5 attempts")):

        result = run_persona(
            persona=fake_persona,
            rep=1,
            client=mock_client,
            asst_client=MagicMock(),
            asst_config={"model": "test-asst"},
            sim_client=MagicMock(),
            sim_config={"model": "test-sim"},
            pace_seconds=0.0,
            max_calls=10,
        )

    # Must be classified as ERROR (not FAIL)
    assert result["status"] == "ERROR"
    assert result["passed"] is False
    assert result["is_error"] is True
    assert result["error_type"] == "quota_exhaustion"
    # Verification checks should NOT run on abort
    assert result["checks"] == {}
    # Guaranteed cleanup must have run
    assert mock_cleanup.called

