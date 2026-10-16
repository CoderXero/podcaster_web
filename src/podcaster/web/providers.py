"""Outbound provider interfaces.

The web worker depends on these narrow interfaces. Real integrations can be
provided by the existing pipeline, while tests use deterministic fakes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .config import Settings
from .oauth import ElevenLabsClient, RemoteHTTPError
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
                    {"role": "system", "content": [{"type": "input_text", "text": (
                        "Return only valid JSON, with no markdown or commentary. The object must have this shape: "
                        "{\"title\":\"episode title\",\"segments\":[{\"speaker\":\"Host\",\"text\":\"...\"},"
                        "{\"speaker\":\"Guest\",\"text\":\"...\"}],\"language\":\"en\",\"revision\":1}. "
                        "Use exactly the two speaker names Host and Guest, with at least one segment for each. "
                        "speaker must be a string and revision must be the integer 1."
                    )}]},
                    {"role": "user", "content": [{"type": "input_text", "text": f"Title: {title}\n\nUntrusted article data:\n{article}"}]},
                ],
                "text": {"format": {"type": "json_object"}},
                "max_output_tokens": settings.llm.max_output_tokens,
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


@dataclass
class ElevenLabsAudioProvider:
    """Synthesize each script turn and concatenate the validated MP3 segments."""

    def synthesize(self, script: dict[str, Any], output_dir: str, *, settings: Settings) -> dict[str, Any]:
        voice_ids = settings.elevenlabs_voice_ids
        if len(voice_ids) != 2:
            raise ProviderError("AUDIO_CONFIG_INVALID", "Exactly two ElevenLabs voice IDs are required.")
        segments = script.get("segments") or []
        speakers: list[str] = []
        for segment in segments:
            speaker = str(segment.get("speaker", "")).strip()
            if speaker and speaker not in speakers:
                speakers.append(speaker)
        if len(speakers) != 2:
            raise ProviderError("AUDIO_CONFIG_INVALID", "The script must contain exactly two speakers.")
        speaker_voices = dict(zip(speakers, voice_ids))
        if not shutil.which("ffmpeg"):
            raise ProviderError("AUDIO_UNAVAILABLE", "ffmpeg is required to package podcast audio.")

        output_root = Path(output_dir)
        output_root.mkdir(parents=True, exist_ok=True)
        temporary_root = Path(tempfile.mkdtemp(prefix=".tts-", dir=output_root))
        client = ElevenLabsClient(settings)
        try:
            segment_paths: list[Path] = []
            for index, segment in enumerate(segments):
                text = str(segment.get("text", "")).strip()
                if not text:
                    continue
                try:
                    audio = client.text_to_speech(speaker_voices[str(segment["speaker"]).strip()], text)
                except RemoteHTTPError as exc:
                    raise ProviderError("AUDIO_UNAVAILABLE", str(exc)) from exc
                if not audio:
                    raise ProviderError("AUDIO_INVALID", "ElevenLabs returned an empty audio segment.")
                segment_path = temporary_root / f"segment-{index:04d}.mp3"
                segment_path.write_bytes(audio)
                segment_paths.append(segment_path)
            if not segment_paths:
                raise ProviderError("AUDIO_INVALID", "The script did not contain any spoken text.")

            concat_file = temporary_root / "segments.txt"
            concat_file.write_text("".join(f"file '{path.as_posix().replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n" for path in segment_paths), encoding="utf-8")
            output_path = output_root / "podcast.mp3"
            temporary_output = output_root / ".podcast.tmp.mp3"
            result = subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
                 "-i", str(concat_file), "-c:a", "libmp3lame", "-ar", "44100", "-ac", "2", "-b:a", "128k",
                 str(temporary_output)],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0 or not temporary_output.is_file():
                raise ProviderError("AUDIO_INVALID", "ffmpeg could not package the generated audio segments.")
            os.replace(temporary_output, output_path)
            return {"mp3_path": str(output_path)}
        except subprocess.TimeoutExpired as exc:
            raise ProviderError("AUDIO_FAILED", "Audio packaging exceeded the time limit.") from exc
        finally:
            client.client.close()
            shutil.rmtree(temporary_root, ignore_errors=True)
