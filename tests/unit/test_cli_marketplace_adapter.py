"""Tests for the CMN-C2-259 AGENTIC STAR Marketplace adapter."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

import cli
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import NullProvider
from framework.secrets.context import bound_secrets
from shared.services.llm.base_llm import BaseLLM
from src.graph.graph import DealScoringPipelinePriorityGraph
from src.services import deal_repository_factory, llm_client

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


def _runtime_config() -> dict[str, Any]:
    config = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
    assert isinstance(config, dict)
    return config


@pytest.fixture(autouse=True)
def _restore_llm_seam():
    original = llm_client.build_llm
    try:
        yield
    finally:
        llm_client.build_llm = original


def _capture_base_invoke(monkeypatch):
    captured: dict[str, Any] = {}

    def capture(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del self
        captured.update(
            user_input=user_input,
            session_id=session_id,
            ctx=ctx,
            input_context=input_context,
        )
        return {"status": "success", "output": "captured"}

    monkeypatch.setattr(DealScoringPipelinePriorityGraph, "invoke", capture)
    return captured


def test_invoke_rebuilds_input_context_from_json_allowlist(monkeypatch):
    captured = _capture_base_invoke(monkeypatch)
    agent = cli.MarketplaceGraph(config=_runtime_config())
    user_input = json.dumps(
        {
            "owner": "sales-a",
            "phase": "proposal",
            "today": "2020-01-01",
            "_config": {"database": {"table": "evil"}},
            "unknown": "drop-me",
        }
    )

    agent.invoke(
        user_input,
        input_context={
            "conversation_history": [{"role": "user", "content": "CANARY"}],
            "today": "2020-01-01",
            "_config": {"database": {"table": "evil"}},
        },
    )

    assert captured["user_input"] == user_input
    assert captured["input_context"] == {"owner": "sales-a", "phase": "proposal"}


def test_input_context_ignores_non_string_filters(monkeypatch):
    captured = _capture_base_invoke(monkeypatch)
    agent = cli.MarketplaceGraph(config=_runtime_config())

    agent.invoke(json.dumps({"owner": 42, "phase": "", "today": "2020-01-01"}))

    assert captured["input_context"] == {}


def test_plain_text_input_is_forwarded_without_context(monkeypatch):
    captured = _capture_base_invoke(monkeypatch)
    agent = cli.MarketplaceGraph(config=_runtime_config())

    result = agent.invoke("優先案件を確認してください")

    assert result["status"] == "success"
    assert captured["user_input"] == "優先案件を確認してください"
    assert captured["input_context"] == {}


def test_constructor_sanitizes_llm_config_without_dropping_settings():
    agent = cli.MarketplaceGraph(config=_runtime_config())

    llm_config = agent.config["llm"]
    assert "api_key_secret" not in llm_config
    assert llm_config["max_deals"] == 20
    assert llm_config["temperature"] == 0.0
    assert llm_config["model"] == "claude-sonnet-5"


def test_main_passes_nonempty_file_config_and_marketplace_identity(monkeypatch):
    captured: dict[str, Any] = {}

    def record(agent_class, **kwargs):
        captured["agent_class"] = agent_class
        captured.update(kwargs)

    monkeypatch.setattr(cli, "load_agent_config", lambda agent_dir: {})
    monkeypatch.setattr(cli, "run_agent_marketplace", record)

    cli.main()

    assert captured["agent_class"] is cli.MarketplaceGraph
    assert captured["agent_name"] == "CMN-C2-259"
    assert captured["namespace"] == "cmn"
    assert captured["config"]["database"]["path_secret_key"] == "DEALS_DB_PATH"


def test_constructor_installs_lazy_azure_llm_seam():
    cli.MarketplaceGraph(config=_runtime_config())

    llm = llm_client.build_llm({"provider": "anthropic"}, None)

    assert isinstance(llm, cli._AzureMarketplaceLLM)
    assert vars(llm) == {}


@pytest.mark.parametrize("missing_key", cli._REQUIRED_SECRET_KEYS)
@pytest.mark.parametrize("bad_value", [None, " value with whitespace "])
def test_provision_secrets_fails_closed(monkeypatch, missing_key, bad_value):
    values = {
        "AZURE_OPENAI_API_KEY": "azure-key",
        "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
        "AZURE_OPENAI_DEPLOYMENT": "deployment",
        "DEALS_DB_PATH": "/tmp/deals.sqlite3",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    if bad_value is None:
        monkeypatch.delenv(missing_key)
    else:
        monkeypatch.setenv(missing_key, bad_value)
    agent = cli.MarketplaceGraph(config=_runtime_config())

    with pytest.raises((KeyError, ValueError)):
        agent.provision_secrets(NullProvider())


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(monkeypatch, endpoint):
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    values = {
        "AZURE_OPENAI_API_KEY": "azure-key",
        "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
        "AZURE_OPENAI_DEPLOYMENT": "deployment",
        "DEALS_DB_PATH": "/tmp/deals.sqlite3",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    agent = cli.MarketplaceGraph(config=_runtime_config())
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)

    with pytest.raises(ValueError, match="bare"):
        agent.provision_secrets(NullProvider())


class _CountingLLM(BaseLLM):
    def __init__(self) -> None:
        self.complete_calls = 0

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        self.complete_calls += 1
        return {
            "content": json.dumps(
                {
                    "items": [
                        {
                            "deal_id": "D-MARKET",
                            "risks": ["予算承認待ち"],
                            "actions": ["決裁者との確認会を設定"],
                        }
                    ]
                },
                ensure_ascii=False,
            )
        }

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield self.complete(messages)["content"]

    def bind_tools(self, tools: list[Any]) -> BaseLLM:
        del tools
        return self


def test_runner_order_invokes_fake_llm_once_and_discards_history(monkeypatch, tmp_path):
    database_path = tmp_path / "deals.sqlite3"
    repository = deal_repository_factory.create_deal_repository(str(database_path))
    try:
        repository.add_deal(
            {
                "deal_id": "D-MARKET",
                "deal_name": "Marketplace Deal",
                "account_name": "Example Account",
                "owner": "sales-a",
                "phase": "proposal",
                "amount": 10_000_000,
                "last_updated": "2026-08-31",
                "bant_budget": True,
                "bant_authority": True,
                "bant_need": True,
                "bant_timeline": False,
                "notes": "予算承認の最終確認が必要",
            }
        )
    finally:
        repository.close()

    env = {
        "AZURE_OPENAI_API_KEY": "azure-key",
        "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
        "AZURE_OPENAI_DEPLOYMENT": "deployment",
        "DEALS_DB_PATH": str(database_path),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    fake_llm = _CountingLLM()
    agent = cli.MarketplaceGraph(config=_runtime_config())
    monkeypatch.setattr(llm_client, "build_llm", lambda llm_cfg, api_key: fake_llm)
    agent.compile()
    agent.provision_secrets(NullProvider())
    ctx = InvocationContext(
        caller_id="marketplace-test",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
    )

    with bound_secrets(agent._secrets_provider):
        result = agent.invoke(
            json.dumps({"owner": "sales-a"}),
            ctx=ctx,
            input_context={
                "conversation_history": [{"role": "user", "content": "CANARY"}],
                "today": "2020-01-01",
                "_config": {"database": {"table": "evil"}},
            },
        )

    assert result["status"] == "success"
    assert result["output"]
    assert "予算承認待ち" in result["output"]
    assert "CANARY" not in json.dumps(result, ensure_ascii=False)
    assert fake_llm.complete_calls == 1
