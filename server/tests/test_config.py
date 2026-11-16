# SPDX-License-Identifier: MIT
import stat
import sys

import pytest

from lapinou.config import ProviderConfig, Settings, load_settings, save_settings


def test_defaults(tmp_path):
    s = load_settings(tmp_path / "config.toml")  # fichier absent
    assert (s.server.platform_port, s.server.mic_port, s.server.http_port) == (9123, 9300, 8080)
    assert (s.server.tts_port, s.server.asr_port) == (8081, 8443)
    assert s.lms.port == 9000
    assert s.brain.primary == "claude-opus-5-5"
    assert s.brain.fast == "claude-haiku-5-5"
    assert (s.budget.warn_pct, s.budget.stop_pct) == (80, 100)
    assert s.voice.vad_silence_ms == 700
    assert s.voice.followup_s == 5
    assert s.logs.retention_days == 7
    assert {"lock", "alarm_control_panel", "cover"} <= set(s.safety.sensitive_domains)
    assert s.rabbits == {}


def test_load_merges_secrets(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[ha]\nurl = "http://ha:8123"\n\n[rabbits.r1]\nname = "Salon"\n'
        'default_lms_player = "cuisine"\n',
        encoding="utf-8",
    )
    (tmp_path / "secrets.toml").write_text('[ha]\ntoken = "abc"\n', encoding="utf-8")
    s = load_settings(tmp_path / "config.toml")
    assert s.ha.url == "http://ha:8123" and s.ha.token == "abc"
    assert s.rabbits["r1"].default_lms_player == "cuisine"


def test_secrets_not_in_public_file(tmp_path):
    p = tmp_path / "config.toml"
    s = load_settings(p)
    s.ha.token = "tok-SECRET"
    s.mqtt.password = 'p"a$s;s é'
    s.safety.pin = "1234"
    s.brain.providers["anthropic"] = ProviderConfig(api_key="sk-SECRET")
    s.ha.url = "http://ha:8123"
    save_settings(s)

    public = p.read_text(encoding="utf-8")
    for secret in ("tok-SECRET", "sk-SECRET", "1234", "a$s;s"):
        assert secret not in public
    assert "http://ha:8123" in public

    secrets_file = tmp_path / "secrets.toml"
    assert "tok-SECRET" in secrets_file.read_text(encoding="utf-8")
    if sys.platform != "win32":
        assert stat.S_IMODE(secrets_file.stat().st_mode) == 0o600

    again = load_settings(p)
    assert again.ha.token == "tok-SECRET"
    assert again.mqtt.password == 'p"a$s;s é'
    assert again.safety.pin == "1234"
    assert again.brain.providers["anthropic"].api_key == "sk-SECRET"
    assert again.model_dump() == s.model_dump()


def test_save_leaves_no_temp_files(tmp_path):
    s = load_settings(tmp_path / "config.toml")
    save_settings(s)
    assert sorted(f.name for f in tmp_path.iterdir()) == ["config.toml", "secrets.toml"]


def test_save_requires_path():
    with pytest.raises(ValueError):
        save_settings(Settings())
