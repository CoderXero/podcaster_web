"""Outbound provider interfaces.

The web worker depends on these narrow interfaces. Real integrations can be
provided by the existing pipeline, while tests use deterministic fakes.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

from .config import Settings
from .security import redact


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class ScriptProvider(Protocol):
    def generate(self, article: str, *, title: str, settings: Settings) -> dict[str, Any]: ...


class AudioProvider(Protocol):
    def synthesize(self, script: dict[str, Any], output_dir: str, *, settings: Settings) -> dict[str, Any]: ...


@dataclass
class ResponsesScriptProvider:
    """OpenAI-compatible Responses API client for the configured LLM.

    It deliberately does not implement streaming or WebSockets. The system
    prompt is owned by the pipeline; article content is sent as untrusted data.
    """

    def generate(self, article: str, *, title: str, settings: Settings) -> dict[str, Any]:
        if settings.llm.wire_api != "responses":
            raise ProviderError("LLM_CONFIG_INVALID", "The configured web provider must use the Responses API.")
        key = os.getenv(settings.llm.api_key_env)
        if not key:
            raise ProviderError("LLM_UNAVAILABLE", "The configured LLM provider key is unavailable.")
        try:
            import httpx
            payload = {
                "model": settings.llm.model,
                "reasoning": {"effort": settings.llm.reasoning_effort},
                "input": [
                    {"role": "system", "content": [{"type": "input_text", "text": "Return only a JSON object matching the requested podcast Script schema."}]},
                    {"role": "user", "content": [{"type": "input_text", "text": f"Title: {title}\n\nUntrusted article data:\n{article}"}]},
                ],
                "text": {"format": {"type": "json_object"}},
            }
            headers = {"Content-Type": "application/json"}
            # Tiiny-compatible deployments may require the configured key but
            # explicitly reject OpenAI's ``Bearer`` prefix.
            headers["Authorization"] = f"Bearer {key}" if settings.llm.requires_openai_auth else key
            with httpx.Client(timeout=settings.llm.timeout_seconds) as client:
                response = client.post(settings.llm.base_url.rstrip("/") + "/responses", headers=headers, json=payload)
            if response.status_code >= 400:
                raise ProviderError("LLM_UNAVAILABLE", f"LLM request failed with status {response.status_code}.")
            data = response.json()
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("LLM_UNAVAILABLE", redact(exc)) from exc
        text = _response_text(data)
        try:
            result = json.loads(text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProviderError("LLM_INVALID_OUTPUT", "The LLM returned invalid structured JSON.") from exc
        if not isinstance(result, dict):
            raise ProviderError("LLM_INVALID_OUTPUT", "The LLM response was not a JSON object.")
        return result


def _response_text(data: dict[str, Any]) -> str | None:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    # Compatibility with providers that expose output content blocks.
    for item in data.get("output", []) or []:
        for content in item.get("content", []) if isinstance(item, dict) else []:
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                return content["text"]
    return None


class NoAudioProvider:
    def synthesize(self, script: dict[str, Any], output_dir: str, *, settings: Settings) -> dict[str, Any]:
        raise ProviderError("AUDIO_UNAVAILABLE", "No audio provider is configured for this installation.")
