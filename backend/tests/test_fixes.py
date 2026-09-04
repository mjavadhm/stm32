"""Regression tests for the orchestrator/LLM hardening pass."""

import asyncio
import time
from pathlib import Path

import app.core.llm as llm_module
from app.agents import router as router_module
from app.db.models import RequestType

WORKER_SOURCE = (
    Path(__file__).resolve().parents[1] / "app" / "workers" / "celery_app.py"
).read_text()


# --- shared provider client ------------------------------------------------


def test_llm_client_is_shared_between_calls():
    assert llm_module.get_llm_client() is llm_module.get_llm_client()
    assert llm_module.get_embedding_client() is llm_module.get_embedding_client()


def test_llm_client_has_a_bounded_timeout():
    client = llm_module.get_llm_client()
    assert client.timeout == llm_module.settings.llm_timeout_seconds
    assert client.timeout < 600, "must not inherit the SDK's 10-minute default"


# --- agent_settings cache --------------------------------------------------


def test_agent_settings_are_read_once_then_cached(monkeypatch):
    calls: list[int] = []

    def fake_load():
        calls.append(1)
        return {"firmware": ("qwen2.5-coder:14b", True), "debug": (None, False)}

    monkeypatch.setattr(llm_module, "_load_agent_settings", fake_load)
    llm_module.invalidate_agent_settings_cache()

    assert llm_module.resolve_agent_model("firmware") == "qwen2.5-coder:14b"
    assert llm_module.is_agent_enabled("debug") is False
    assert llm_module.is_agent_enabled("firmware") is True
    assert len(calls) == 1, "a pipeline step must not requery per LLM call"

    llm_module.invalidate_agent_settings_cache()
    llm_module.resolve_agent_model("firmware")
    assert len(calls) == 2, "a settings write must be visible immediately"

    llm_module.invalidate_agent_settings_cache()


def test_unknown_agent_falls_back_to_the_default_model(monkeypatch):
    monkeypatch.setattr(llm_module, "_load_agent_settings", dict)
    llm_module.invalidate_agent_settings_cache()
    try:
        assert llm_module.resolve_agent_model("nope") == llm_module.settings.llm_model
        assert llm_module.is_agent_enabled("nope") is True
    finally:
        llm_module.invalidate_agent_settings_cache()


def test_database_failure_does_not_break_model_resolution(monkeypatch):
    def boom():
        raise RuntimeError("database is down")

    monkeypatch.setattr(llm_module, "_load_agent_settings", boom)
    llm_module.invalidate_agent_settings_cache()
    try:
        assert llm_module.resolve_agent_model("firmware") == llm_module.settings.llm_model
        assert llm_module.is_agent_enabled("firmware") is True
    finally:
        llm_module.invalidate_agent_settings_cache()


# --- router timeout --------------------------------------------------------


def test_router_falls_back_to_rules_when_the_provider_hangs(monkeypatch):
    async def hang(_text: str):
        await asyncio.sleep(30)
        raise AssertionError("should have been cancelled")

    monkeypatch.setattr(router_module, "is_agent_enabled", lambda _name: True)
    monkeypatch.setattr(router_module, "_classify_with_llm", hang)
    monkeypatch.setattr(router_module.settings, "router_timeout_seconds", 0.05)

    started = time.monotonic()
    result, method = asyncio.run(
        router_module.classify_request("این HardFault چرا رخ می‌دهد؟")
    )
    elapsed = time.monotonic() - started

    assert result is RequestType.debug
    assert method == "rules:timeout"
    assert elapsed < 5, f"classification blocked for {elapsed:.1f}s"


# --- worker structure ------------------------------------------------------


def test_pipeline_drives_the_graph_asynchronously():
    """LangGraph only awaits coroutine nodes on the astream/ainvoke path.

    Reverting to `graph.stream()` would silently return unawaited coroutines
    once the mock agents become real `async def` agents in M3.
    """
    assert "graph.astream(" in WORKER_SOURCE
    assert "graph.stream(" not in WORKER_SOURCE
    assert "asyncio.run(" in WORKER_SOURCE


def test_pipeline_marks_progress_and_attributes_failures():
    assert "RunStatus.running" in WORKER_SOURCE, "live progress requires a running marker"
    assert "started_at" in WORKER_SOURCE
    assert "_close_unfinished" in WORKER_SOURCE, "abandoned tasks must not stay pending"
    assert "current_agent" in WORKER_SOURCE, "failures must name the agent that raised"


def test_the_stream_ceiling_stands_on_its_own():
    """LLM_STREAM_MAX_TOKENS used to need LLM_MAX_TOKENS set too.

    It defaults to 0, so setting only the stream ceiling did nothing at all
    -- the opposite of what the setting says.
    """
    import asyncio

    from app.core.config import settings

    sent: dict = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            sent.update(kwargs)

            async def empty():
                return
                yield  # pragma: no cover

            return empty()

    class FakeClient:
        chat = type("C", (), {"completions": FakeCompletions()})()

    agent = llm_module.AgentLLM(agent_name="chat", client=FakeClient(), model="glm-5")

    async def drain(**kwargs):
        sent.clear()
        async for _ in agent.stream([{"role": "user", "content": "hi"}], **kwargs):
            pass  # pragma: no cover

    original = (settings.llm_max_tokens, settings.llm_stream_max_tokens)
    try:
        settings.llm_max_tokens = 0
        settings.llm_stream_max_tokens = 1234
        asyncio.run(drain())
        assert sent["max_tokens"] == 1234

        # And the non-streaming ceiling never leaks into a stream.
        settings.llm_stream_max_tokens = 0
        settings.llm_max_tokens = 99
        asyncio.run(drain())
        assert "max_tokens" not in sent

        # A caller-supplied ceiling is dropped too: it is the clipping bug.
        asyncio.run(drain(max_tokens=5))
        assert "max_tokens" not in sent
    finally:
        settings.llm_max_tokens, settings.llm_stream_max_tokens = original


def test_a_junk_reasoning_pattern_does_not_match_every_model():
    """An empty regex matches everything, which would slow every model down."""
    from app.core.config import settings

    original = settings.llm_reasoning_model_pattern
    try:
        for pattern in ("", "none", "[unclosed"):
            settings.llm_reasoning_model_pattern = pattern
            agent = llm_module.AgentLLM(
                agent_name="chat", client=object(), model="glm-5.3"
            )
            assert agent._is_reasoning_model() is False, pattern

        settings.llm_reasoning_model_pattern = "glm|o3"
        agent = llm_module.AgentLLM(agent_name="chat", client=object(), model="GLM-5.3")
        assert agent._is_reasoning_model() is True
        agent = llm_module.AgentLLM(
            agent_name="chat", client=object(), model="gpt-4o-mini"
        )
        assert agent._is_reasoning_model() is False
    finally:
        settings.llm_reasoning_model_pattern = original
