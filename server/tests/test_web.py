# SPDX-License-Identifier: MIT
"""Page de réglages du serveur : connexion, réglages sans secrets, mémoire, minuteurs…"""

import datetime as dt
import json
import re
import stat
import threading
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
        setup_token_path=tmp_path / "data" / "setup-token",
    )
    ctx.auth.prepare_setup()  # comme Services.start
    client = TestClient(create_app(settings, web=ctx), base_url=BASE)
    return client, ctx, tmp_path


BASE = "http://localhost"


def _token(tmp_path) -> str:
    return (tmp_path / "data" / "setup-token").read_text(encoding="utf-8").strip()


def _login(client):
    tmp_path = Path(client.app.state.web.setup_token_path).parents[1]
    r = client.post("/api/setup", json={"password": PASSWORD, "token": _token(tmp_path)},
                    headers=H)
    assert r.status_code == 200, r.text
    return client


def test_every_api_route_requires_login(env):
    client, _, _ = env
    checked = 0
    for path, methods in client.app.openapi()["paths"].items():
        if not path.startswith("/api/") or path in OPEN:
            continue
        url = path.replace("{memory_id}", "1").replace("{timer_id}", "1")
        for method in methods:
            r = client.request(method.upper(), url, headers=H, json={})
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
    r = client.get("/static/app.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    assert client.get("/static/style.css").status_code == 200


STATIC = Path(__file__).resolve().parents[1] / "lapinou" / "web" / "static"
RABBIT_WWW = Path(__file__).resolve().parents[2] / "image" / "core" / "usr" / "www" / "lapinou"


@pytest.mark.parametrize("name", ["tokens.css", "lapin.svg"])
def test_design_tokens_are_the_rabbit_copy(name):
    """R45 : jetons et lapin partagés avec la page du lapin ; la copie du serveur doit suivre."""
    # Le firmware est fusionné dans ce dépôt : une page du lapin absente est une erreur.
    assert (RABBIT_WWW / name).exists(), f"page du lapin absente : {RABBIT_WWW / name}"
    assert (STATIC / name).read_bytes() == (RABBIT_WWW / name).read_bytes(), f"{name} : copie à suivre"


def test_page_uses_the_tokens_without_inline_code(env):
    client, _, _ = env
    html = client.get("/").text
    assert html.index("/static/tokens.css") < html.index("/static/style.css")
    # La CSP (default-src 'self') interdit le code en ligne : rien de tel dans la page.
    assert "<style" not in html
    assert not re.search(r"\s(style|on[a-z]+)\s*=", html)
    scripts = re.findall(r"<script([^>]*)>(.*?)</script>", html, re.S)
    assert scripts and all("src=" in attrs and not body.strip() for attrs, body in scripts)
    r = client.get("/static/tokens.css")
    assert r.status_code == 200 and "--ln-ink" in r.text and "prefers-color-scheme: dark" in r.text
    r = client.get("/static/lapin.svg")
    assert r.status_code == 200 and "image/svg+xml" in r.headers["content-type"]
    css = client.get("/static/style.css").text
    assert "var(--ln-" in css and "prefers-reduced-motion" in css
    js = client.get("/static/app.js").text
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert banned not in js, banned


def test_setup_then_login(env):
    client, _, tmp_path = env
    token_file = tmp_path / "data" / "setup-token"
    assert client.get("/api/session").json() == {
        "password_set": False, "logged_in": False, "setup_file": str(token_file)}
    assert client.post("/api/setup", json={"password": "court", "token": _token(tmp_path)},
                       headers=H).status_code == 422
    _login(client)
    assert client.get("/api/session").json() == {
        "password_set": True, "logged_in": True, "setup_file": None}
    assert not token_file.exists()  # usage unique : effacé dès le mot de passe créé
    # Une seule fois : ensuite il faut le mot de passe.
    assert client.post("/api/setup", json={"password": "Autre-mot-2", "token": "x"},
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
    other = TestClient(create_app(settings, web=WebContext(store=SettingsStore(settings))),
                       base_url=BASE)
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
    """Destinations inchangées : les secrets restent ; le corps ne peut pas les écraser par
    des valeurs vides, ni toucher au PIN, au haché admin ni aux commandes."""
    client, ctx, tmp_path = env
    _login(client)
    r = client.put("/api/settings", headers=H, json={
        "budget": {"monthly_usd": 12.5},
        "mqtt": {"username": "lapin", "password": ""},
        "ha": {"token": ""},
        "brain": {"persona_extra": "La famille a un chat, Moustache.",
                  "providers": {"anthropic": {"kind": "anthropic", "api_key": ""},
                                "mistral": {"kind": "openai",
                                            "base_url": "https://api.mistral.ai/v1"}}},
        "admin": {"password_hash": "x"},
        "update": {"restart_command": ["rm", "-rf", "/"]},
        "safety": {"allowed_entities": ["lock.entree"], "pin": "0000"},
    })
    assert r.status_code == 200, r.text
    assert r.json()["restart_needed"] is True and r.json()["cleared"] == []
    saved = load_settings(tmp_path / "config.toml")
    assert saved.budget.monthly_usd == 12.5 and saved.mqtt.username == "lapin"
    assert saved.mqtt.password == "mqtt-SECRET" and saved.safety.pin == "4321"
    assert saved.ha.token == "ha-SECRET"
    assert saved.update.restart_command == ["systemctl", "--no-block", "restart", "lapinou"]
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


# Chacune empêcherait le service de redémarrer : 422, rien d'écrit (le service garde l'ancien).
INVALID = [
    {"server": {"timezone": "Mars/Olympus"}},
    {"server": {"timezone": "../../etc/passwd"}},
    {"server": {"timezone": "Europe"}},
    {"budget": {"warn_pct": 150}},
    {"budget": {"warn_pct": 90, "stop_pct": 80}},
    {"server": {"http_port": 70000}},
    {"server": {"tts_port": 0}},
    {"mqtt": {"port": -1}},
    {"lms": {"port": 65536}},
    {"server": {"public_url": "ftp://192.168.1.10"}},
    {"server": {"public_url": "192.168.1.10:8080"}},
    {"server": {"public_url": "http://x:99999"}},
    {"ha": {"url": "javascript:alert(1)"}},
    {"brain": {"providers": {"local": {"kind": "openai", "base_url": "file:///etc/passwd"}}}},
    {"brain": {"providers": {"local": {"kind": "autre"}}}},
    {"budget": {"monthly_usd": -1}},
    {"budget": {"warn_pct": 0}},
    {"budget": {"prices": {"x": {"input": -2}}}},
    {"voice": {"wake_threshold": 1.5}},
    {"voice": {"wake_threshold": -0.1}},
    {"voice": {"vad_silence_ms": 0}},
    {"voice": {"followup_s": 999}},
    {"voice": {"tts_engine": "espeak"}},
    {"safety": {"climate_range": [30, 10]}},
    {"safety": {"climate_range": [16]}},
    {"memory": {"max_items": 0}},
    {"logs": {"retention_days": 0}},
    {"ota": {"check_hours": -1}},
]


@pytest.mark.parametrize("body", INVALID, ids=lambda b: json.dumps(b)[:60])
def test_values_that_would_break_the_service_are_refused(env, body):
    client, ctx, tmp_path = env
    _login(client)
    before = (tmp_path / "config.toml").read_bytes()
    r = client.put("/api/settings", headers=H, json=body)
    assert r.status_code == 422, r.text
    assert (tmp_path / "config.toml").read_bytes() == before
    assert load_settings(tmp_path / "config.toml").model_dump() == ctx.store.get().model_dump()


def test_valid_urls_and_timezone_are_accepted(env):
    client, _, tmp_path = env
    _login(client)
    r = client.put("/api/settings", headers=H, json={
        "server": {"public_url": "https://lapinou.maison:8443", "timezone": "America/Montreal"},
        "ha": {"url": "http://192.168.1.20:8123"}, "voice": {"wake_threshold": 0.5}})
    assert r.status_code == 200, r.text
    saved = load_settings(tmp_path / "config.toml")
    assert saved.server.timezone == "America/Montreal"


# --- Destination changée : son secret est effacé (sinon il partirait chez un autre) --------


def test_changing_ha_url_without_token_clears_the_token(env):
    client, _, tmp_path = env
    _login(client)
    r = client.put("/api/settings", headers=H, json={"ha": {"url": "http://192.168.66.6:8123"}})
    assert r.status_code == 200, r.text
    assert r.json()["cleared"] == ["ha.token"]
    assert r.json()["secrets"]["ha.token"] is False
    saved = load_settings(tmp_path / "config.toml")
    assert saved.ha.url == "http://192.168.66.6:8123" and saved.ha.token == ""
    assert "ha-SECRET" not in (tmp_path / "secrets.toml").read_text(encoding="utf-8")


def test_changing_a_destination_with_its_new_secret_keeps_the_new_one(env):
    client, _, tmp_path = env
    _login(client)
    r = client.put("/api/settings", headers=H, json={
        "ha": {"url": "http://ha.maison:8123", "token": "nouveau-jeton"},
        "mqtt": {"host": "192.168.1.51", "password": "nouveau-mqtt"},
        "brain": {"providers": {"anthropic": {"kind": "openai",
                                              "base_url": "https://autre.example/v1",
                                              "api_key": "nouvelle-cle"}}},
    })
    assert r.status_code == 200 and r.json()["cleared"] == []
    assert "nouveau" not in r.text and "nouvelle" not in r.text
    saved = load_settings(tmp_path / "config.toml")
    assert (saved.ha.token, saved.mqtt.password) == ("nouveau-jeton", "nouveau-mqtt")
    assert saved.brain.providers["anthropic"].api_key == "nouvelle-cle"


def test_changing_mqtt_host_or_provider_destination_clears_their_secrets(env):
    client, _, tmp_path = env
    _login(client)
    r = client.put("/api/settings", headers=H, json={
        "mqtt": {"host": "attaquant.example"},
        "brain": {"providers": {"anthropic": {"kind": "openai",
                                              "base_url": "https://attaquant.example/v1"}}},
    })
    assert r.status_code == 200, r.text
    assert sorted(r.json()["cleared"]) == ["brain.providers.anthropic.api_key", "mqtt.password"]
    saved = load_settings(tmp_path / "config.toml")
    assert saved.mqtt.password == "" and saved.brain.providers["anthropic"].api_key == ""
    secrets = (tmp_path / "secrets.toml").read_text(encoding="utf-8")
    assert "mqtt-SECRET" not in secrets and "sk-ant-SECRET" not in secrets
    # Le type seul compte aussi (la clé partirait vers un autre service).
    client.put("/api/secrets", headers=H, json={"name": "brain.providers.anthropic.api_key",
                                                "value": "k2"})
    r = client.put("/api/settings", headers=H, json={
        "brain": {"providers": {"anthropic": {"kind": "anthropic",
                                              "base_url": "https://attaquant.example/v1"}}}})
    assert r.json()["cleared"] == ["brain.providers.anthropic.api_key"]


# --- Host, code d'installation (R40) ------------------------------------------------------


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8080", "192.168.66.6",
                                  "lapinou.local"])
def test_unknown_host_names_are_refused(env, host):
    client, _, _ = env
    for path in ("/", "/static/app.js", "/api/session", "/api/settings"):
        r = client.get(path, headers={"Host": host})
        assert r.status_code == 400, (path, host)
    r = client.post("/api/login", json={"password": "x"}, headers={**H, "Host": host})
    assert r.status_code == 400


@pytest.mark.parametrize("host", ["localhost", "localhost:8080", "127.0.0.1:8080", "[::1]:8080",
                                  "192.168.1.10:8080", "LAPINOU.local"])
def test_known_host_names_are_accepted(env, host):
    client, ctx, _ = env
    s = ctx.store.get().model_dump()
    s["server"]["allowed_hosts"] = ["lapinou.local"]
    ctx.store.replace(s)
    assert client.get("/api/session", headers={"Host": host}).status_code == 200


def test_own_lan_address_is_accepted(env, monkeypatch):
    from lapinou.web import routes

    client, _, _ = env
    monkeypatch.setattr(routes, "own_addresses", lambda: frozenset({"10.1.2.3"}))
    assert client.get("/", headers={"Host": "10.1.2.3:8080"}).status_code == 200


def test_rabbit_paths_ignore_the_host_check(env):
    client, _, _ = env
    assert client.get("/api/health", headers={"Host": "lapin"}).status_code == 200
    r = client.get("/health", headers={"Host": "lapin"})
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert client.get("/ota/manifest.json", headers={"Host": "lapin"}).status_code == 404
    assert client.get("/tts/x.mp3", headers={"Host": "lapin"}).status_code == 404


def test_setup_needs_the_one_time_code(env):
    client, ctx, tmp_path = env
    token_file = tmp_path / "data" / "setup-token"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    for token in ("", "faux", _token(tmp_path) + "x"):
        r = client.post("/api/setup", json={"password": PASSWORD, "token": token}, headers=H)
        assert r.status_code == 403, token
    assert not ctx.auth.password_set()
    r = client.post("/api/setup", json={"password": PASSWORD, "token": _token(tmp_path)},
                    headers=H)
    assert r.status_code == 200
    assert not token_file.exists()


def test_setup_code_is_logged_by_location_only(tmp_path, caplog):
    from lapinou.web.auth import Auth

    settings = load_settings(tmp_path / "config.toml")
    auth = Auth(SettingsStore(settings), tmp_path / "data" / "setup-token")
    with caplog.at_level("INFO"):
        auth.prepare_setup()
    token = (tmp_path / "data" / "setup-token").read_text().strip()
    assert len(token) >= 24 and token not in caplog.text
    assert str(tmp_path / "data" / "setup-token") in caplog.text


def test_no_setup_code_when_a_password_exists(tmp_path):
    from lapinou.web.auth import Auth, SetupRefused, hash_password

    settings = load_settings(tmp_path / "config.toml")
    path = tmp_path / "setup-token"
    path.write_text("ancien\n")
    settings.admin.password_hash = hash_password(PASSWORD)
    auth = Auth(SettingsStore(settings), path)
    auth.prepare_setup()
    assert not path.exists()
    # Sans fichier de code (ou sans code préparé) : création par la page impossible.
    bare = Auth(SettingsStore(load_settings(tmp_path / "autre" / "config.toml")))
    with pytest.raises(SetupRefused):
        bare.setup("", PASSWORD, "c")


def test_concurrent_setups_only_one_wins(tmp_path):
    from lapinou.web.auth import Auth

    settings = load_settings(tmp_path / "config.toml")
    auth = Auth(SettingsStore(settings), tmp_path / "setup-token")
    auth.prepare_setup()
    token = (tmp_path / "setup-token").read_text().strip()
    results: list[str] = []
    barrier = threading.Barrier(4)

    def attempt(i):
        barrier.wait()
        try:
            auth.setup(token, f"Mot-de-passe-{i}", f"client-{i}")
            results.append("ok")
        except Exception as e:  # noqa: BLE001
            results.append(type(e).__name__)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 1, results


def test_cli_sets_the_password_hash(tmp_path, monkeypatch, capsys):
    import io

    from lapinou.web import auth

    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    auth.main(["--config", str(tmp_path / "config.toml")])
    saved = load_settings(tmp_path / "config.toml")
    assert saved.admin.password_hash.startswith("$argon2id$")
    assert PASSWORD not in (tmp_path / "secrets.toml").read_text()
    assert stat.S_IMODE((tmp_path / "secrets.toml").stat().st_mode) == 0o600


# --- Essais limités -----------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _auth_with_password(tmp_path, clock):
    from lapinou.web.auth import Auth, hash_password

    settings = load_settings(tmp_path / "config.toml")
    settings.admin.password_hash = hash_password(PASSWORD)
    return Auth(SettingsStore(settings), clock=clock)


def test_lockout_doubles_up_to_15_minutes(tmp_path):
    from lapinou.web.auth import Locked

    clock = Clock()
    auth = _auth_with_password(tmp_path, clock)
    waits = []
    for _ in range(6):
        for _ in range(5):
            assert auth.login("faux", "c") is None
        with pytest.raises(Locked) as e:
            auth.login(PASSWORD, "c")
        waits.append(e.value.retry_after)
        clock.t += e.value.retry_after
    assert waits == [61, 121, 241, 481, 901, 901]
    assert auth.login(PASSWORD, "c") is not None  # réussite : la série repart de zéro
    for _ in range(5):
        auth.login("faux", "c")
    with pytest.raises(Locked) as e:
        auth.login("faux", "c")
    assert e.value.retry_after == 61


def test_attempts_are_counted_before_argon2(tmp_path, monkeypatch):
    """Des essais en parallèle ne passent pas tous : le compte se fait sous verrou, avant le
    calcul (lent) du haché."""
    from lapinou.web import auth as auth_mod

    clock = Clock()
    auth = _auth_with_password(tmp_path, clock)
    started = threading.Event()

    def slow_verify(password):
        started.wait(2)
        return False

    monkeypatch.setattr(auth, "_verify", slow_verify)
    results = []

    def attempt():
        try:
            results.append(auth.login("faux", "c"))
        except auth_mod.Locked:
            results.append("locked")

    threads = [threading.Thread(target=attempt) for _ in range(20)]
    for t in threads:
        t.start()
    started.set()
    for t in threads:
        t.join()
    assert results.count(None) == 5 and results.count("locked") == 15


def test_password_change_failures_count_too(tmp_path):
    from lapinou.web.auth import Locked

    auth = _auth_with_password(tmp_path, Clock())
    for _ in range(5):
        assert auth.change("faux", "Nouveau-mot-1", "c") is False
    with pytest.raises(Locked):
        auth.login(PASSWORD, "c")


def test_failures_table_is_bounded(tmp_path):
    from lapinou.web import auth as auth_mod

    clock = Clock()
    auth = _auth_with_password(tmp_path, clock)
    for i in range(auth_mod.MAX_CLIENTS + 200):
        clock.t += 2.1  # sous le plafond global (30 essais par minute)
        auth._attempt(f"10.0.{i // 256}.{i % 256}")
    assert len(auth._clients) <= auth_mod.MAX_CLIENTS
    clock.t += auth_mod.FORGET_S + 1
    auth._attempt("autre")
    assert list(auth._clients) == ["autre"]


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
    assert body["choices"]["wake_method"] == ["asr"]  # « both » : seulement avec wake_model
    assert body["choices"]["tts_engine"] == ["kokoro", "piper"]


def test_blocked_addresses_are_never_evicted(tmp_path):
    """Inonder la table d'adresses nouvelles n'efface pas le blocage d'une adresse ; table
    pleine d'adresses bloquées : une adresse nouvelle est refusée."""
    from lapinou.web import auth as auth_mod

    clock = Clock()
    auth = _auth_with_password(tmp_path, clock)
    for _ in range(5):
        auth.login("faux", "attaquant")
    clock.t += 1
    # Table remplie d'adresses libres plus récentes que lui : c'est elles qui partent.
    for i in range(auth_mod.MAX_CLIENTS):
        auth._clients[f"c{i}"] = auth_mod._Client(last=clock.t)
    clock.t += 1
    auth._attempt("nouvelle")
    assert len(auth._clients) <= auth_mod.MAX_CLIENTS and "nouvelle" in auth._clients
    assert auth.locked_for("attaquant") > 0
    with pytest.raises(auth_mod.Locked):
        auth.login(PASSWORD, "attaquant")
    # Table pleine de blocages : on refuse plutôt que d'oublier.
    full = _auth_with_password(tmp_path, clock)
    for i in range(auth_mod.MAX_CLIENTS):
        full._clients[f"c{i}"] = auth_mod._Client(locked_until=clock.t + 600, last=clock.t)
    with pytest.raises(auth_mod.Locked):
        full._attempt("nouvelle")
    assert "c0" in full._clients and "nouvelle" not in full._clients


def test_global_attempt_ceiling(tmp_path):
    """Beaucoup d'adresses, un essai chacune : au-delà de 30 par minute, tout attend."""
    from lapinou.web import auth as auth_mod

    clock = Clock()
    auth = _auth_with_password(tmp_path, clock)
    for i in range(auth_mod.GLOBAL_MAX_ATTEMPTS):
        auth._attempt(f"10.2.0.{i}")
    with pytest.raises(auth_mod.Locked):
        auth._attempt("10.2.1.1")
    clock.t += auth_mod.GLOBAL_WINDOW_S + 1
    auth._attempt("10.2.1.1")


# --- Contrôle de Host : tout est gardé sauf les chemins des lapins ---------------------------


@pytest.mark.parametrize("host", ["[::1]junk", "[::1]:", "[::1]:80:80", "localhost:abc",
                                  "::1", "localhost:8080:1", "[evil.example]"])
def test_malformed_host_headers_are_refused(env, host):
    client, _, _ = env
    assert client.get("/api/session", headers={"Host": host}).status_code == 400


def test_absolute_form_request_does_not_reach_guarded_handlers(env):
    client, _, _ = env
    r = client.get("http://evil.example/api/session", headers={"Host": "localhost"})
    assert r.status_code == 400
    r = client.get("http://evil.example/", headers={"Host": "localhost"})
    assert r.status_code == 400


def test_everything_but_rabbit_paths_is_guarded(env):
    client, _, _ = env
    for path in ("/docs", "/redoc", "/openapi.json", "/autre", "/static/fr.json", "/api/x"):
        assert client.get(path, headers={"Host": "evil.example"}).status_code == 400, path
    # Sans documentation d'API sur 8080, même pour un nom connu.
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path


def test_ota_settings_are_read_only_from_the_page(env):
    client, _, tmp_path = env
    _login(client)
    r = client.put("/api/settings", headers=H, json={
        "ota": {"repo": "attaquant/faux", "pubkey_path": "/tmp/cle.pub", "check_hours": 6}})
    assert r.status_code == 200, r.text
    saved = load_settings(tmp_path / "config.toml")
    assert saved.ota.repo == "Kinder0083/LapiNou" and saved.ota.pubkey_path == ""
    assert saved.ota.check_hours == 6


def test_settings_give_the_effective_base_url(env):
    client, _, _ = env
    _login(client)
    assert client.get("/api/settings").json()["base_url"] == "http://192.168.1.10:8080"


def test_usb_key_refused_without_a_valid_ota_public_key(env):
    from lapinou.ota import OtaError

    client, ctx, _ = env
    _login(client)

    class Placeholder(FakeOta):
        def key_zip(self):
            raise OtaError("clé publique OTA provisoire (PLACEHOLDER) : mises à jour refusées")

    ctx.ota = Placeholder()
    r = client.post("/api/usbkey", headers=H, json={"ssid": "Maison", "password": "motdepasse"})
    assert r.status_code == 409 and "PLACEHOLDER" in r.json()["detail"]


# --- Rapport de correction 3 --------------------------------------------------------------


def test_blocked_address_cannot_lock_out_everyone(tmp_path):
    """Une adresse bloquée qui insiste n'est pas comptée : le plafond global ne se remplit
    pas, et l'admin peut se connecter depuis une autre adresse."""
    from lapinou.web import auth as auth_mod

    clock = Clock()
    auth = _auth_with_password(tmp_path, clock)
    for _ in range(5):
        auth.login("faux", "attaquant")
    for _ in range(100):
        with pytest.raises(auth_mod.Locked):
            auth.login("faux", "attaquant")
    assert len(auth._recent) == 5
    assert auth.login(PASSWORD, "admin") is not None


@pytest.mark.parametrize("host", ["localhost:0", "localhost:65536", "localhost:99999",
                                  "[::1]:0", "[::1]:70000", "localhost:+80", "localhost: 80"])
def test_host_ports_out_of_range_are_refused(env, host):
    client, _, _ = env
    assert client.get("/api/session", headers={"Host": host}).status_code == 400


def test_host_port_65535_is_fine(env):
    client, _, _ = env
    assert client.get("/api/session", headers={"Host": "localhost:65535"}).status_code == 200
