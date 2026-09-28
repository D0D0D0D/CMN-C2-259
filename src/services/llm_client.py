"""AgentCore Platform v1.0"""

# Injection seam for the LLM client.
# Nodes call build_llm(cfg, api_key) to obtain a BaseLLM-compatible object
# exposing .complete(messages: list) -> dict.
# Provider clients are imported lazily so that unit tests (which inject a
# fake BaseLLM) never require optional provider extras (langchain_*).

from __future__ import annotations

from typing import Any


def build_llm(llm_cfg: dict, api_key: str | None) -> Any:
    """Construct a shared.services.llm client from config.

    Provider clients take a single config dict; api_key is threaded in via
    config["api_key"] (never read from os.environ — S-3 secret isolation).
    """
    provider = str(llm_cfg.get("provider", "anthropic")).lower()
    client_config = {
        "model": llm_cfg.get("model"),
        "temperature": llm_cfg.get("temperature", 0.0),
        "api_key": api_key,
    }

    if provider == "anthropic":
        from shared.services.llm.anthropic_client import AnthropicClient
        return AnthropicClient(client_config)
    if provider == "openai":
        from shared.services.llm.openai_client import OpenAIClient
        return OpenAIClient(client_config)
    if provider == "bedrock":
        from shared.services.llm.bedrock_client import BedrockClient
        return BedrockClient(client_config)
    raise ValueError(f"Unsupported LLM provider: {provider!r}")
