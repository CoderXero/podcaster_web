from __future__ import annotations

from pathlib import Path

from podcaster.web.config import Settings


def test_settings_load_dotenv(tmp_path: Path, monkeypatch):
    source = tmp_path / "articles"
    source.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PODCASTER_WEB_TOKENS=dotenv-token=publisher:dotenv-user\n"
        "PODCASTER_WEB_SOURCE_ROOTS=articles\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("PODCASTER_WEB_TOKENS", raising=False)
    monkeypatch.delenv("PODCASTER_WEB_SOURCE_ROOTS", raising=False)
    settings = Settings.load(tmp_path / "missing.yaml")
    assert settings.web.source_roots == ["articles"]
    assert settings.source_root_map()["articles"] == source.resolve()
    assert settings.web.admin_tokens_from_env == "PODCASTER_WEB_TOKENS"


def test_settings_loads_two_voice_ids_from_dotenv(tmp_path: Path, monkeypatch):
    source = tmp_path / "articles"
    source.mkdir()
    (tmp_path / ".env").write_text(
        'PODCASTER_WEB_SOURCE_ROOTS=articles\n'
        'ELEVENLABS_VOICE_IDS=["host-id","guest-id"]\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("PODCASTER_WEB_SOURCE_ROOTS", raising=False)
    monkeypatch.delenv("ELEVENLABS_VOICE_IDS", raising=False)
    settings = Settings.load(tmp_path / "missing.yaml")
    assert settings.elevenlabs_voice_ids == ["host-id", "guest-id"]
