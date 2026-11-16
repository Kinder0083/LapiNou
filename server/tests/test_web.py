# SPDX-License-Identifier: MIT
"""Page de réglages du serveur : connexion, réglages sans secrets, mémoire, minuteurs…"""

import datetime as dt
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lapinou.app import create_app
from lapinou.brain.base import Usage
from lapinou.brain.budget import Budget
from lapinou.config import ProviderConfig, SettingsStore, load_settings, save_settings
from lapinou.conversation import ConversationLog
from lapinou.db import Database
from lapinou.rabbit.registry import RabbitRegistry
from lapinou.tools.memory import MemoryTools
from lapinou.tools.timers import TimerTools
from lapinou.updater import UpdateResult
from lapinou.web.routes import WebContext

H = {"X-LapiNou": "1"}  # en-tête exigé sur toute requête qui modifie (contre le CSRF)
PASSWORD = "Carotte-2026"
SECRETS = ["mqtt-SECRET", "ha-SECRET", "4321", "github_pat_SECRET", "sk-ant-SECRET"]
OPEN = {"/api/health", "/api/session", "/api/login", "/api/setup", "/api/logout"}


class FakeOta:
    def __init__(self, key: Path | None = None):
        self.key = key
        self.refreshed = 0

    def status(self):
        from lapinou.ota import OtaStatus

        return OtaStatus(version="v1.2.3" if self.key else "", repo="Kinder0083/LapiNou")

    async def refresh(self):
        self.refreshed += 1
        return self.status()

    def key_zip(self):
        return self.key

    def manifest_bytes(self):
        return None

    def file_path(self, name):
        return None


class FakeUpdater:
    def __init__(self):
        self.updates = 0
        self.restarts = 0

    def current(self) -> str:
        return "d4f3ce4"

    async def update(self) -> UpdateResult:
        self.updates += 1
        return UpdateResult(ok=True, before="d4f3ce4", after="abc1234", output="ok",
                            restarting=True, message="Mis à jour")

    async def restart(self) -> None:
        self.restarts += 1


@pytest.fixture
def env(tmp_path):
    settings = load_settings(tmp_path / "config.toml")
    settings.mqtt.password = "mqtt-SECRET"
    settings.ha.token = "ha-SECRET"
    settings.safety.pin = "4321"
    settings.ota.token = "github_pat_SECRET"
    settings.brain.providers["anthropic"] = ProviderConfig(api_key="sk-ant-SECRET")
    settings.server.public_url = "http://192.168.1.10:8080"
    save_settings(settings)
    db = Database(":memory:")
    db.migrate()
    now = [dt.datetime(2026, 11, 16, 12, 0, tzinfo=dt.UTC)]
    ctx = WebContext(
        store=SettingsStore(settings),
        db=db,
        memory=MemoryTools(db, clock=lambda: now[0]),
        timers=TimerTools(db, announcer=None, clock=lambda: now[0]),
        budget=Budget(db, settings.budget, clock=lambda: now[0]),
        log=ConversationLog(db, clock=lambda: now[0]),
        registry=RabbitRegistry(),
        ota=FakeOta(),
        updater=FakeUpdater(),
    )
    client = TestClient(create_app(settings, web=ctx))
    return client, ctx, tmp_path


def _login(client):
    r = client.post("/api/setup", json={"password": PASSWORD}, headers=H)
    assert r.status_code == 200, r.text
    return client


def test_every_api_route_requires_login(env):
    client, _, _ = env
    checked = 0
    for route in client.app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/") or path in OPEN:
            continue
        url = path.replace("{memory_id}", "1").replace("{timer_id}", "1")
        for method in route.methods - {"HEAD", "OPTIONS"}:
            r = client.request(method, url, headers=H, json={})
            assert r.status_code == 401, (method, url, r.status_code)
            checked += 1
    assert checked >= 15


def test_pages_are_public_and_french(env):
    client, _, _ = env
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert 'lang="fr"' in r.text and "app.js" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    fr = client.get("/static/fr.json").json()
    for page in ("brain", "budget", "voice", "persona", "wake", "tools", "memory", "timers",
                 "rabbits", "journal", "updates", "usbkey"):
        assert fr["pages"][page], page
    note = fr["tools"]["security_note"]
    assert "input_" in note and "allowed_entities" in note and "groupe" in note
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_setup_then_login(env):
    client, _, tmp_path = env
    assert client.get("/api/session").json() == {"password_set": False, "logged_in": False}
    assert client.post("/api/setup", json={"password": "court"}, headers=H).status_code == 422
    _login(client)
    assert client.get("/api/session").json() == {"password_set": True, "logged_in": True}
    # Une seule fois : ensuite il faut le mot de passe.
    assert client.post("/api/setup", json={"password": "Autre-mot-2"},
                       headers=H).status_code == 409
    secrets = (tmp_path / "secrets.toml").read_text(encoding="utf-8")
    assert "$argon2id$" in secrets and PASSWORD not in secrets
    assert "argon2" not in (tmp_path / "config.toml").read_text(encoding="utf-8")

    client.post("/api/logout", headers=H)
    assert client.get("/api/settings").status_code == 401
    assert client.post("/api/login", json={"password": "mauvais"}, headers=H).status_code == 401
    r = client.post("/api/login", json={"password": PASSWORD}, headers=H)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/settings").status_code == 200


def test_password_survives_restart(env):
    client, ctx, tmp_path = env
    _login(client)
    settings = load_settings(tmp_path / "config.toml")
    other = TestClient(create_app(settings, web=WebContext(store=SettingsStore(settings))))
    assert other.get("/api/session").json()["password_set"] is True
    assert other.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200


def test_login_attempts_are_limited(env):
    client, _, _ = env
    _login(client)
    client.post("/api/logout", headers=H)
    codes = [client.post("/api/login", json={"password": f"faux-{i}"}, headers=H).status_code
             for i in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 429
    # Même le bon mot de passe attend la fin du blocage.
    assert client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 429


def test_change_password(env):
    client, _, _ = env
    _login(client)
    assert client.post("/api/password", json={"old": "faux", "new": "Nouveau-mot-1"},
                       headers=H).status_code == 401
    assert client.post("/api/password", json={"old": PASSWORD, "new": "Nouveau-mot-1"},
                       headers=H).status_code == 200
    client.post("/api/logout", headers=H)
    assert client.post("/api/login", json={"password": "Nouveau-mot-1"},
                       headers=H).status_code == 200


def test_changes_need_the_csrf_header(env):
    client, _, _ = env
    _login(client)
    r = client.put("/api/settings", json={"budget": {"monthly_usd": 5}})
    assert r.status_code == 403
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 403


def test_settings_never_contain_secrets(env):
    client, _, _ = env
    _login(client)
    r = client.get("/api/settings")
    assert r.status_code == 200
    text = r.text
    for secret in SECRETS:
        assert secret not in text
    assert "argon2" not in text and "password_hash" not in text
    body = r.json()
    assert body["secrets"] == {
        "mqtt.password": True, "ha.token": True, "safety.pin": True, "ota.token": True,
        "brain.providers.anthropic.api_key": True,
    }
    assert body["settings"]["server"]["public_url"] == "http://192.168.1.10:8080"
    assert "admin" not in body["settings"]
    assert "password" not in body["settings"]["mqtt"]
    assert "api_key" not in body["settings"]["brain"]["providers"]["anthropic"]


def test_put_settings_saves_and_keeps_secrets(env):
    client, ctx, tmp_path = env
    _login(client)
    r = client.put("/api/settings", headers=H, json={
        "budget": {"monthly_usd": 12.5},
        "mqtt": {"host": "192.168.1.50", "password": "ÉCRASÉ"},
        "brain": {"persona_extra": "La famille a un chat, Moustache.",
                  "providers": {"anthropic": {"kind": "anthropic", "api_key": "ÉCRASÉ"},
                                "mistral": {"kind": "openai",
                                            "base_url": "https://api.mistral.ai/v1"}}},
        "admin": {"password_hash": "x"},
        "safety": {"allowed_entities": ["lock.entree"], "pin": "0000"},
    })
    assert r.status_code == 200, r.text
    assert r.json()["restart_needed"] is True
    saved = load_settings(tmp_path / "config.toml")
    assert saved.budget.monthly_usd == 12.5 and saved.mqtt.host == "192.168.1.50"
    assert saved.mqtt.password == "mqtt-SECRET" and saved.safety.pin == "4321"
    assert saved.brain.providers["anthropic"].api_key == "sk-ant-SECRET"
    assert saved.brain.providers["mistral"].base_url == "https://api.mistral.ai/v1"
    assert saved.brain.persona_extra.startswith("La famille")
    assert saved.safety.allowed_entities == ["lock.entree"]
    assert saved.admin.password_hash.startswith("$argon2")
    assert ctx.store.get().budget.monthly_usd == 12.5
    # Un fournisseur retiré de la liste disparaît (avec sa clé).
    r = client.put("/api/settings", headers=H, json={"brain": {"providers": {
        "anthropic": {"kind": "anthropic"}}}})
    assert r.status_code == 200
    assert list(load_settings(tmp_path / "config.toml").brain.providers) == ["anthropic"]


def test_put_settings_rejects_invalid_values(env):
    client, _, tmp_path = env
    _login(client)
    before = (tmp_path / "config.toml").read_bytes()
    r = client.put("/api/settings", headers=H, json={"budget": {"monthly_usd": "beaucoup"}})
    assert r.status_code == 422
    assert "monthly_usd" in r.text
    r = client.put("/api/settings", headers=H, json={"voice": {"wake_method": "rien"}})
    assert r.status_code == 422
    assert (tmp_path / "config.toml").read_bytes() == before


def test_set_and_clear_secret(env):
    client, _, tmp_path = env
    _login(client)
    r = client.put("/api/secrets", headers=H, json={"name": "ha.token", "value": "nouveau"})
    assert r.status_code == 200 and "nouveau" not in r.text
    assert load_settings(tmp_path / "config.toml").ha.token == "nouveau"
    r = client.put("/api/secrets", headers=H, json={"name": "mqtt.password", "value": ""})
    assert r.json()["secrets"]["mqtt.password"] is False
    r = client.put("/api/secrets", headers=H,
                   json={"name": "brain.providers.mistral.api_key", "value": "k"})
    assert r.status_code == 200
    assert load_settings(tmp_path / "config.toml").brain.providers["mistral"].api_key == "k"
    for name in ("admin.password_hash", "budget.monthly_usd", "brain.providers..api_key"):
        r = client.put("/api/secrets", headers=H, json={"name": name, "value": "x"})
        assert r.status_code == 422, name


def test_memories_view_edit_delete(env):
    client, ctx, _ = env
    _login(client)
    ctx.memory.remember("Léa adore les dinosaures")
    r = client.post("/api/memories", headers=H, json={"text": "Le chat s'appelle Moustache"})
    assert r.status_code == 200
    mems = client.get("/api/memories").json()["memories"]
    assert [m["text"] for m in mems] == ["Léa adore les dinosaures", "Le chat s'appelle Moustache"]
    first = mems[0]["id"]
    r = client.put(f"/api/memories/{first}", headers=H, json={"text": "Léa adore les tigres"})
    assert r.status_code == 200
    assert ctx.memory.all()[0].text == "Léa adore les tigres"
    assert client.put(f"/api/memories/{first}", headers=H,
                      json={"text": "le code de la porte est 1234"}).status_code == 422
    assert client.put("/api/memories/999", headers=H, json={"text": "x y z"}).status_code == 404
    assert client.delete(f"/api/memories/{first}", headers=H).status_code == 200
    assert [m.text for m in ctx.memory.all()] == ["Le chat s'appelle Moustache"]
    assert client.delete("/api/memories", headers=H).json() == {"deleted": 1}
    assert ctx.memory.all() == []


async def test_timers_list_and_cancel(env):
    client, ctx, _ = env
    _login(client)
    tid = await ctx.timers.set_timer("salon", 600, "les pâtes")
    timers = client.get("/api/timers").json()["timers"]
    assert len(timers) == 1 and timers[0]["rabbit_id"] == "salon"
    assert "pâtes" in timers[0]["text"] and timers[0]["due_at"].startswith("2026-11-16T12:10")
    assert client.delete(f"/api/timers/{tid}", headers=H).status_code == 200
    assert client.get("/api/timers").json()["timers"] == []
    assert client.delete(f"/api/timers/{tid}", headers=H).status_code == 404


def test_journal(env):
    client, ctx, tmp_path = env
    _login(client)
    cid = ctx.log.start("salon")
    ctx.log.turn(cid, "user", "allume le salon")
    ctx.log.turn(cid, "assistant", "C'est fait !")
    ctx.log.end(cid)
    body = client.get("/api/journal").json()
    assert body["retention_days"] == 7
    [conv] = body["conversations"]
    assert conv["rabbit_id"] == "salon"
    assert [(t["role"], t["text"]) for t in conv["turns"]] == [
        ("user", "allume le salon"), ("assistant", "C'est fait !")]
    assert client.delete("/api/journal", headers=H).json() == {"deleted": 1}
    assert client.get("/api/journal").json()["conversations"] == []


def test_budget(env):
    client, ctx, _ = env
    _login(client)
    ctx.budget.record(Usage(provider="anthropic", model="claude-haiku-5-5",
                            input_tokens=1_000_000, output_tokens=0))
    body = client.get("/api/budget").json()
    assert body["month"] == "2026-11" and body["monthly_usd"] == 20.0
    assert body["spent"] == pytest.approx(0.10) and body["ratio"] == pytest.approx(0.005)
    assert body["warn_pct"] == 80 and body["stop_pct"] == 100
    assert body["rows"] == [{"provider": "anthropic", "model": "claude-haiku-5-5",
                             "input_tokens": 1_000_000, "output_tokens": 0,
                             "usd": pytest.approx(0.10)}]


def test_rabbits(env):
    client, ctx, _ = env
    _login(client)
    ctx.registry.get("salon")
    client.put("/api/settings", headers=H, json={"rabbits": {"salon": {"name": "Pompon"},
                                                             "cuisine": {"name": "Caramel"}}})
    rabbits = {r["id"]: r for r in client.get("/api/rabbits").json()["rabbits"]}
    assert rabbits["salon"]["connected"] is False and rabbits["salon"]["name"] == "Pompon"
    assert rabbits["cuisine"]["name"] == "Caramel" and rabbits["cuisine"]["state"] == "inconnu"


def test_persona(env):
    client, _, _ = env
    _login(client)
    body = client.get("/api/persona").json()
    assert "Lapinou" in body["base"] and body["extra"] == ""


def test_updates(env):
    client, ctx, _ = env
    _login(client)
    body = client.get("/api/updates").json()
    assert body["server"]["commit"] == "d4f3ce4" and body["ota"]["repo"] == "Kinder0083/LapiNou"
    r = client.post("/api/updates/server", headers=H)
    assert r.status_code == 200 and r.json()["after"] == "abc1234"
    assert ctx.updater.updates == 1
    assert client.post("/api/updates/ota", headers=H).status_code == 200
    assert ctx.ota.refreshed == 1
    assert client.post("/api/restart", headers=H).status_code == 200
    assert ctx.updater.restarts == 1


def test_usb_key(env):
    client, ctx, tmp_path = env
    _login(client)
    form = {"ssid": "Maison", "password": "motdepasse", "web_password": "Lapin-2026!"}
    r = client.post("/api/usbkey", headers=H, json=form)
    assert r.status_code == 409  # aucune version téléchargée
    key = tmp_path / "LapinLibre-v1.2.3.zip"
    with zipfile.ZipFile(key, "w") as zf:
        zf.write(Path(__file__).parent / "data/usbkey/lapinlibre.txt", "lapinlibre.txt")
        zf.writestr("autorun", "x")
    ctx.ota = FakeOta(key)
    r = client.post("/api/usbkey", headers=H, json=form)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    assert "lapinou-cle-v1.2.3.zip" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"
    out = tmp_path / "got.zip"
    out.write_bytes(r.content)
    with zipfile.ZipFile(out) as zf:
        text = zf.read("lapinlibre.txt").decode()
    assert 'WIFI_SSID="Maison"' in text and 'LAPINOU_SERVER="192.168.1.10"' in text
    assert 'MQTT_PASSWORD="mqtt-SECRET"' not in text  # pas de broker réglé : pas de MQTT
    r = client.post("/api/usbkey", headers=H, json={**form, "password": 'a"b"c"d"e'})
    assert r.status_code == 422 and "Wi-Fi" in r.json()["detail"]


def test_api_responses_are_not_cached(env):
    client, _, _ = env
    _login(client)
    assert client.get("/api/settings").headers["cache-control"] == "no-store"


def test_settings_json_is_valid_for_the_page(env):
    client, _, _ = env
    _login(client)
    body = client.get("/api/settings").json()
    json.dumps(body)
    assert body["choices"]["wake_method"] == ["asr", "both"]
    assert body["choices"]["tts_engine"] == ["kokoro", "piper"]
