// SPDX-License-Identifier: MIT
// LapiNou's page on the rabbit (Task 12, R45): the home (status and quick controls), the settings'
// tabs and the updates, over the rabbit's own programs (cgi-bin/lapinou/*, and LapinLibre's leds,
// ears, tts, status and lapinlibre/status). No dependency and nothing from outside: the texts come
// from fr.json, the rabbit from lapin.svg, put inline. Every value goes into the page as text
// (textContent, attributes), never as HTML. Reads are GET, changes POST (form-encoded).
"use strict";

(function () {
  let T = {};
  const $ = (id) => document.getElementById(id);
  const state = { status: null, info: null, api: null, settings: null, tab: "etat", otaTimer: 0 };

  // --- texts ---
  function t(key, vars) {
    let s = Object.prototype.hasOwnProperty.call(T, key) ? T[key] : key;
    for (const k in vars || {}) s = s.split("{" + k + "}").join(String(vars[k]));
    return s;
  }
  function applyTexts(root) {
    root.querySelectorAll("[data-t]").forEach((el) => { el.textContent = t(el.dataset.t); });
    root.querySelectorAll("[data-t-label]").forEach((el) => { el.setAttribute("aria-label", t(el.dataset.tLabel)); });
  }

  // --- a small element builder: el("p", {class: "x"}, "text", child...) ---
  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const k in attrs || {}) {
      const v = attrs[k];
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") n.className = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (k === "value") n.value = v;
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids) if (kid !== null && kid !== undefined) n.append(kid);
    return n;
  }

  // --- the rabbit's API ---
  class ApiError extends Error {
    constructor(message, status) { super(message); this.status = status; }
  }
  async function call(path, fields, method) {
    const opts = { method: method || (fields ? "POST" : "GET"), credentials: "same-origin", cache: "no-store" };
    let url = "cgi-bin/" + path;
    if (fields && opts.method === "POST") {
      opts.body = new URLSearchParams(fields);
    } else if (fields) {
      url += "?" + new URLSearchParams(fields);
    }
    let res;
    try {
      res = await fetch(url, opts);
    } catch (e) {
      throw new ApiError(t("error.network"), 0);
    }
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (res.status === 401) {
      showPasswordBanner();
      throw new ApiError(t("error.auth"), 401);
    }
    if (!data || data.return !== "0") {
      throw new ApiError((data && data.msg) || t("error.generic"), res.status);
    }
    return data;
  }

  function say(node, text, ok) {
    node.textContent = text || "";
    node.className = "result" + (text ? (ok ? " ok" : " bad") : "");
  }
  // run(button, result, fn): the button busy while fn runs; fn's text or error in result.
  async function run(button, result, fn) {
    if (button) button.disabled = true;
    try {
      const text = await fn();
      if (result) say(result, text, true);
    } catch (e) {
      if (result) say(result, e.message, false);
    } finally {
      if (button) button.disabled = false;
    }
  }

  // --- the banner ---
  function banner(text, action) {
    const b = $("banner");
    b.replaceChildren(text);
    if (action) b.append(action);
    b.hidden = !text;
  }
  function showPasswordBanner() {
    banner(t("settings.need_password"),
      el("a", { class: "button", href: "#reglages/securite" }, t("settings.create_password")));
  }

  // --- the rabbit drawing ---
  async function loadRabbit() {
    try {
      const res = await fetch("lapinou/lapin.svg", { cache: "force-cache" });
      const doc = new DOMParser().parseFromString(await res.text(), "image/svg+xml");
      const svg = doc.documentElement;
      if (svg.nodeName !== "svg") return;
      const box = $("rabbit");
      box.replaceChildren(document.importNode(svg, true));
      box.classList.add("hello-wiggle");
    } catch (e) { /* the page works without its drawing */ }
  }
  function earAngle(pos, side) {
    // 17 steps a turn; 0 is up. Shown as a tilt outwards, at most about half a turn.
    const p = ((pos % 17) + 17) % 17;
    const deg = (p <= 8 ? p : p - 17) * 11;
    return side === "left" ? -deg : deg;
  }
  function showEars(left, right) {
    const box = $("rabbit");
    const l = box.querySelector(".ln-ear-left");
    const r = box.querySelector(".ln-ear-right");
    box.classList.remove("hello-wiggle");
    if (l) l.style.transform = "rotate(" + earAngle(left, "left") + "deg)";
    if (r) r.style.transform = "rotate(" + earAngle(right, "right") + "deg)";
  }
  function showLed(color) {
    const off = !color || color === "000000";
    document.documentElement.style.setProperty("--ln-led", off ? "#46597d" : "#" + color);
    $("rabbit").classList.toggle("off", off);
  }

  // --- the home page ---
  const LED_COLORS = ["00FF00", "00A2FF", "FF00FF", "FF0000", "FFAA00", "FFFFFF"];
  const EAR_PRESETS = { debout: [0, 0], endormies: [4, 4], tombantes: [8, 8], perplexe: [0, 8], curieuse: [2, 15] };

  function signalWord(q) {
    const n = Number(q);
    if (!q || isNaN(n)) return "";
    return n >= 50 ? t("signal.good") : n >= 30 ? t("signal.fair") : t("signal.weak");
  }

  function renderHome() {
    const s = state.status || {};
    const i = state.info || {};
    const name = i.hostname || "";
    $("hello").textContent = name ? t("home.hello", { name: name }) : t("home.hello_anon");
    let mood = t("home.mood_none");
    if (state.api && state.api.sleep === "1") mood = t("home.mood_asleep");
    else if (s.mode === "server") mood = s.server_link === "connected" ? t("home.mood_server") : t("home.mood_server_off");
    else if (s.mode === "cloud") mood = t("home.mood_cloud");
    $("mood").textContent = mood;

    const items = [
      [t("sticker.version"), s.version],
      [t("sticker.wifi"), i.wifi_ssid],
      [t("sticker.signal"), signalWord(i.wifi_signal)],
      [t("sticker.ip"), i.ip],
      [t("sticker.assistant"), s.mode ? t("mode." + s.mode) : ""],
      [t("sticker.server"), s.mode === "server" ? t("link." + s.server_link) : ""],
      [t("sticker.ha"), s.mqtt && s.mqtt !== "absent" ? t("link." + s.mqtt) : ""],
      [t("sticker.update"), updateWord(s.last_check)],
    ];
    $("stickers").replaceChildren(...items.filter((x) => x[1]).map((x) => el("li", null, x[0] + " ", el("b", null, x[1]))));
    if (s.volume) { $("volume").value = s.volume; $("volume-value").textContent = s.volume; }
    if (state.api) showLed(state.api.led_color);
    if (s.password_needed === "1") showPasswordBanner();
  }
  function updateWord(c) {
    if (!c) return "";
    return c.available && c.available !== c.installed ? t("update.new", { v: c.available }) : t("update.none");
  }

  async function loadStatus() {
    const [s, i, a] = await Promise.allSettled([call("lapinou/status"), call("lapinlibre/status"), call("status")]);
    if (s.status === "rejected" && i.status === "rejected") {
      banner(t("load.fail"));
      return;
    }
    state.status = s.value || null;
    state.info = i.value || null;
    state.api = a.value || null;
    renderHome();
    if (state.tab === "etat" && !$("view-settings").hidden) renderTab();
  }

  function buildToys() {
    const sw = $("led-swatches");
    const ledResult = sw.parentNode.querySelector(".result");
    const swatch = (color, label) => el("button", {
      type: "button", class: "swatch" + (color === "000000" ? " off" : ""), style: "--sw:#" + color,
      "aria-label": label, title: label, "aria-pressed": "false",
      onclick: (ev) => run(ev.currentTarget, ledResult, async () => {
        await call("leds", { color: color });
        sw.querySelectorAll(".swatch").forEach((b) => b.setAttribute("aria-pressed", String(b === ev.target)));
        showLed(color);
        return t("led.done");
      }),
    });
    sw.replaceChildren(...LED_COLORS.map((c) => swatch(c, t("led.c" + c))), swatch("000000", t("led.off")));

    const chips = $("ears-presets");
    const earsResult = chips.parentNode.querySelector(".result");
    chips.replaceChildren(...Object.keys(EAR_PRESETS).map((name) => el("button", {
      type: "button", class: "chip", "aria-pressed": "false",
      onclick: (ev) => run(ev.currentTarget, earsResult, async () => {
        const [l, r] = EAR_PRESETS[name];
        await call("ears", { left: l, right: r });
        chips.querySelectorAll(".chip").forEach((b) => b.setAttribute("aria-pressed", String(b === ev.target)));
        showEars(l, r);
        return t("ears.done");
      }),
    }, t("ears." + name))));

    const form = $("say-form");
    const input = $("say-text");
    input.placeholder = t("say.placeholder");
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const text = input.value.trim();
      const result = form.parentNode.querySelector(".result");
      if (!text) { say(result, t("say.empty"), false); return; }
      run(form.querySelector("button"), result, async () => {
        await call("tts", { text: text });
        return t("say.done");
      });
    });

    const vol = $("volume");
    const volResult = vol.closest(".toy").querySelector(".result");
    vol.addEventListener("input", () => { $("volume-value").textContent = vol.value; });
    vol.addEventListener("change", () => run(null, volResult, async () => {
      await call("lapinou/volume", { value: vol.value });
      return t("volume.done", { v: vol.value });
    }));
    $("refresh").addEventListener("click", () => loadStatus());
  }

  // --- the settings ---
  // A field: [kind, key, label] with kind text, number, switch, secret, select:<a|b>, readonly.
  // Keys are "section.key" (conf_set's section__key) or a secret's name.
  const TABS = {
    reseau: {
      intro: "reseau.intro",
      groups: [[null, [
        ["text", "network.hostname", "reseau.hostname"],
        ["text", "network.wifi_ssid", "reseau.wifi_ssid"],
        ["secret", "wifi_password", "reseau.wifi_password", "reseau.wifi_rule"],
        ["text", "network.ip", "reseau.ip"],
        ["text", "network.netmask", "reseau.netmask"],
        ["text", "network.gateway", "reseau.gateway"],
        ["text", "network.dns", "reseau.dns"],
      ]]],
      reboot: true,
    },
    mqtt: {
      intro: "mqtt.intro", restart: "mqtt", tests: ["mqtt"],
      groups: [[null, [
        ["switch", "mqtt.enabled", "mqtt.enabled"],
        ["text", "mqtt.host", "mqtt.host", "settings.address_clears"],
        ["number", "mqtt.port", "mqtt.port"],
        ["text", "mqtt.user", "mqtt.user"],
        ["secret", "mqtt_password", "mqtt.password"],
        ["text", "mqtt.prefix", "mqtt.prefix"],
        ["text", "mqtt.discovery_prefix", "mqtt.discovery_prefix"],
      ]]],
    },
    serveur: {
      intro: "serveur.intro", restart: "all", tests: ["server"],
      groups: [[null, [
        ["switch", "server.enabled", "serveur.enabled"],
        ["text", "server.host", "serveur.host"],
      ]]],
    },
    cloud: {
      intro: "cloud.intro", restart: "cloud", tests: ["stt", "llm", "tts"],
      groups: [
        [null, [["switch", "cloud.enabled", "cloud.enabled"]]],
        ["cloud.stt", [["text", "cloud.stt_url", "cloud.url", "settings.address_clears"], ["text", "cloud.stt_model", "cloud.model"],
          ["secret", "cloud_stt_key", "cloud.key"]]],
        ["cloud.llm", [["select:anthropic|openai", "cloud.llm_kind", "cloud.kind"],
          ["text", "cloud.llm_url", "cloud.url", "settings.address_clears"],
          ["text", "cloud.llm_model", "cloud.model"], ["select:low|medium|high|none", "cloud.llm_effort", "cloud.effort"],
          ["secret", "cloud_llm_key", "cloud.key"]]],
        ["cloud.tts", [["text", "cloud.tts_url", "cloud.url", "settings.address_clears"], ["text", "cloud.tts_model", "cloud.model"],
          ["text", "cloud.tts_voice", "cloud.voice"], ["secret", "cloud_tts_key", "cloud.key"]]],
      ],
    },
    voix: {
      intro: "voix.intro", restart: "mqtt",
      groups: [[null, [
        ["number", "audio.volume", "voix.volume"],
        ["number", "audio.mic_gain_db", "voix.mic_gain_db"],
        ["text", "assistant.language", "voix.language"],
        ["text", "time.tz", "voix.tz"],
      ]]],
    },
  };
  const TEST_LABELS = { mqtt: "tab.mqtt", server: "tab.serveur", stt: "cloud.stt", llm: "cloud.llm", tts: "cloud.tts" };

  function fieldNode(f, settings) {
    const [kind, key, label, note] = f;
    const id = "f-" + key.replace(/[^a-z0-9]/g, "-");
    const value = settings[key];
    const noteNode = note ? el("p", { class: "note" }, t(note)) : null;
    if (kind === "switch") {
      return el("div", { class: "field wide" },
        el("label", { class: "switch" }, el("input", { type: "checkbox", id: id, "data-key": key, checked: value === "1" }), t(label)));
    }
    let input;
    if (kind.startsWith("select:")) {
      input = el("select", { id: id, "data-key": key },
        el("option", { value: "" }, "—"), ...kind.slice(7).split("|").map((o) => el("option", { value: o }, o)));
      input.value = value || "";
    } else if (kind === "secret") {
      const isSet = value && value.set;
      input = el("input", { type: "password", id: id, "data-secret": key, autocomplete: "new-password",
        placeholder: isSet ? t("settings.secret_set") : t("settings.secret_unset") });
    } else {
      input = el("input", { type: kind === "number" ? "number" : "text", id: id, "data-key": key, value: value || "",
        inputmode: kind === "number" ? "numeric" : null, spellcheck: "false", autocapitalize: "off" });
    }
    return el("div", { class: "field" }, el("label", { for: id }, t(label)), input, noteNode);
  }

  async function loadSettings() {
    const data = await call("lapinou/conf_get");
    state.settings = data.settings;
    return data.settings;
  }

  async function renderForm(panel, def) {
    const settings = state.settings || await loadSettings();
    const form = el("form", { novalidate: true });
    form.append(el("p", { class: "intro" }, t(def.intro)));
    for (const [title, fields] of def.groups) {
      const box = el("fieldset", { class: "group" });
      if (title) box.append(el("legend", null, t(title)));
      box.append(el("div", { class: "fields" }, ...fields.map((f) => fieldNode(f, settings))));
      form.append(box);
    }
    const result = el("p", { class: "result", "aria-live": "polite" });
    const save = el("button", { type: "submit", class: "button" }, t("settings.save"));
    const actions = el("div", { class: "actions" }, save);
    for (const what of def.tests || []) {
      actions.append(el("button", {
        type: "button", class: "button quiet",
        onclick: (ev) => run(ev.currentTarget, result, async () => {
          say(result, t("settings.testing"), true);
          const r = await call("lapinou/test", { what: what });
          return t(TEST_LABELS[what]) + " : " + (r.detail || "OK");
        }),
      }, t("settings.test") + " " + t(TEST_LABELS[what]).toLowerCase()));
    }
    if (def.reboot) {
      actions.append(el("button", {
        type: "button", class: "button danger",
        onclick: (ev) => {
          if (!window.confirm(t("reseau.reboot_confirm"))) return;
          run(ev.currentTarget, result, async () => { await call("reboot", {}); return t("reseau.rebooting"); });
        },
      }, t("reseau.reboot")));
    }
    form.append(actions, result);
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      run(save, result, () => saveForm(form, def, settings));
    });
    panel.replaceChildren(form);
  }

  async function saveForm(form, def, settings) {
    // The Wi-Fi password goes on its own (wpa.conf); the other secrets with the settings, so a
    // changed address keeps the key given with it (R46).
    let wifi = "";
    const fields = {};
    for (const input of form.querySelectorAll("[data-secret]")) {
      if (!input.value) continue;
      if (input.dataset.secret === "wifi_password") {
        if (/["\\]/.test(input.value)) throw new Error(t("reseau.wifi_bad"));
        wifi = input.value;
      } else {
        fields["secret__" + input.dataset.secret] = input.value;
      }
    }
    for (const input of form.querySelectorAll("[data-key]")) {
      const key = input.dataset.key;
      const value = input.type === "checkbox" ? (input.checked ? "1" : "0") : input.value.trim();
      const before = settings[key] || (input.type === "checkbox" ? "0" : "");
      if (value !== before) fields[key.replace(".", "__")] = value;
    }
    if (!wifi && !Object.keys(fields).length) return t("settings.nothing");
    if (wifi) await call("lapinou/secret_set", { name: "wifi_password", value: wifi });
    let cleared = [];
    if (Object.keys(fields).length) cleared = (await call("lapinou/conf_set", fields)).cleared || [];
    form.querySelectorAll("[data-secret]").forEach((i) => { i.value = ""; });
    await loadSettings();
    for (const input of form.querySelectorAll("[data-secret]")) {
      const s = state.settings[input.dataset.secret];
      input.placeholder = s && s.set ? t("settings.secret_set") : t("settings.secret_unset");
    }
    const note = cleared.length ? " " + t("settings.cleared", { n: cleared.map((c) => t("secret." + c)).join(", ") }) : "";
    if (def.restart) {
      await call("lapinou/restart", { what: def.restart });
      return t("settings.saved_restart") + note;
    }
    return t("settings.saved") + note;
  }

  function renderEtat(panel) {
    const s = state.status || {};
    const i = state.info || {};
    const up = Number(i.uptime) || 0;
    const rows = [
      ["etat.version", s.version], ["etat.build", s.build_id], ["etat.hostname", i.hostname], ["etat.ip", i.ip],
      ["etat.wifi", i.wifi_ssid], ["etat.signal", signalWord(i.wifi_signal)],
      ["etat.uptime", up ? t("etat.hours", { h: Math.floor(up / 3600), m: Math.floor(up / 60) % 60 }) : ""],
      ["etat.assistant", s.mode ? t("mode." + s.mode) : ""],
      ["etat.server", s.mode === "server" ? (s.server_host || "") + " (" + t("link." + s.server_link) + ")" : ""],
      ["etat.mqtt", s.mqtt ? t("link." + s.mqtt) : ""],
      ["etat.password", s.password === "1" ? t("etat.yes") : t("etat.no")],
    ];
    panel.replaceChildren(el("p", { class: "intro" }, t("etat.intro")),
      el("dl", { class: "facts" }, ...rows.filter((r) => r[1]).map((r) => el("div", null, el("dt", null, t(r[0])), el("dd", null, r[1])))));
  }

  // waitForPress(result): the LED blinks; resolves with the press's token once the rabbit's head
  // is pressed, else throws after the wait.
  async function waitForPress(result) {
    const start = await call("lapinou/press", { action: "start" });
    const wait = Number(start.wait) || 60;
    const box = $("rabbit");
    box.classList.add("waiting");
    try {
      for (let left = wait; left > 0; left--) {
        say(result, t("securite.press", { s: left, c: start.color || "" }), true);
        await new Promise((r) => setTimeout(r, 1000));
        const st = await call("lapinou/press", { action: "status", token: start.token });
        if (st.pressed) return start.token;
        if (!st.waiting && left < wait - 2) break;
      }
      throw new Error(t("securite.press_late"));
    } finally {
      box.classList.remove("waiting");
    }
  }

  function renderSecurite(panel) {
    const s = state.status || {};
    const result = el("p", { class: "result", "aria-live": "polite" });
    const a = el("input", { type: "password", id: "pw1", autocomplete: "new-password", minlength: "8", maxlength: "64" });
    const b = el("input", { type: "password", id: "pw2", autocomplete: "new-password", minlength: "8", maxlength: "64" });
    const save = el("button", { type: "submit", class: "button" }, t("securite.save"));
    const form = el("form", { novalidate: true },
      el("p", { class: "intro" }, t("securite.intro")),
      el("p", { class: "result " + (s.password === "1" ? "ok" : "bad") }, s.password === "1" ? t("securite.state_on") : t("securite.state_off")),
      el("div", { class: "fields" },
        el("div", { class: "field" }, el("label", { for: "pw1" }, t("securite.new")), a, el("p", { class: "note" }, t("securite.rule"))),
        el("div", { class: "field" }, el("label", { for: "pw2" }, t("securite.again")), b)),
      el("div", { class: "actions" }, save), result);
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      run(save, result, async () => {
        if (a.value !== b.value) throw new Error(t("securite.mismatch"));
        // The first password: a press on the rabbit's head within 60 s (R46).
        const token = s.password !== "1" ? await waitForPress(result) : "";
        await call("lapinou/secret_set", { name: "web_password", value: a.value, token: token });
        a.value = b.value = "";
        banner("");
        if (state.status) { state.status.password = "1"; state.status.password_needed = "0"; }
        return t("securite.saved");
      });
    });
    panel.replaceChildren(form);
  }

  async function renderMaj(panel) {
    const s = state.status || {};
    const settings = state.settings || await loadSettings();
    const result = el("p", { class: "result", "aria-live": "polite" });
    const installed = el("strong", null, s.version || "?");
    const available = el("strong", null, "?");
    const checked = el("strong", null, t("maj.never"));
    const log = el("pre", { class: "log" }, t("maj.nolog"));
    const install = el("button", { type: "button", class: "button danger", hidden: true }, t("maj.install"));
    const check = el("button", { type: "button", class: "button" }, t("maj.check"));

    function showCheck(c) {
      if (!c) return;
      installed.textContent = c.installed || s.version || "?";
      available.textContent = c.available || "?";
      if (c.checked) checked.textContent = new Date(c.checked * 1000).toLocaleString("fr-FR");
      install.hidden = !(c.available && c.available !== c.installed);
    }
    async function poll() {
      clearTimeout(state.otaTimer);
      try {
        const st = await call("lapinou/ota", { action: "status" }, "GET");
        log.textContent = st.log || t("maj.nolog");
        showCheck(st.last_check);
        check.disabled = install.disabled = st.running;
        if (st.running) {
          say(result, t("maj.running"), true);
          state.otaTimer = setTimeout(poll, 3000);
        }
      } catch (e) {
        say(result, e.message, false);
      }
    }
    check.addEventListener("click", () => run(check, result, async () => {
      say(result, t("maj.checking"), true);
      const c = await call("lapinou/ota", { action: "check" });
      showCheck(c);
      return c.available === c.installed ? t("maj.uptodate") : t("maj.found", { v: c.available });
    }));
    install.addEventListener("click", () => {
      if (!window.confirm(t("maj.install_confirm", { v: available.textContent }))) return;
      run(install, result, async () => {
        await call("lapinou/ota", { action: "install" });
        state.otaTimer = setTimeout(poll, 2000);
        return t("maj.running");
      });
    });

    const source = el("form", { novalidate: true });
    const def = { intro: "maj.daily", groups: [["maj.source", [
      ["text", "ota.url", "maj.url"], ["readonly", "ota.repo", "maj.repo"], ["secret", "ota_token", "maj.token"]]]] };
    panel.replaceChildren(
      el("p", { class: "intro" }, t("maj.intro")),
      el("div", { class: "versions" },
        el("div", { class: "version-card" }, el("span", null, t("maj.installed")), installed),
        el("div", { class: "version-card" }, el("span", null, t("maj.available")), available),
        el("div", { class: "version-card" }, el("span", null, t("maj.checked")), checked)),
      el("div", { class: "actions" }, check, install), result,
      el("h3", null, t("maj.log")), log, source);
    await renderForm(source, def);
    const repo = source.querySelector("[data-key='ota.repo']");
    if (repo) {
      repo.readOnly = true;
      repo.removeAttribute("data-key");
      repo.value = settings["ota.repo"] || "Kinder0083/LapiNou";
      repo.after(el("p", { class: "note" }, t("settings.readonly")));
    }
    showCheck(s.last_check);
    poll();
  }

  async function renderTab() {
    const panel = $("panel");
    document.querySelectorAll(".tabs [data-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === state.tab)));
    clearTimeout(state.otaTimer);
    try {
      if (state.tab === "etat") renderEtat(panel);
      else if (state.tab === "securite") renderSecurite(panel);
      else if (state.tab === "maj") await renderMaj(panel);
      else await renderForm(panel, TABS[state.tab]);
    } catch (e) {
      panel.replaceChildren(el("p", { class: "result bad" }, e.message));
    }
  }

  // --- views: #accueil, #reglages[/tab], #maj ---
  function route() {
    const hash = location.hash.replace(/^#/, "");
    const [view, tab] = hash.split("/");
    const settings = view === "reglages" || view === "maj";
    $("view-home").hidden = settings;
    $("view-settings").hidden = !settings;
    const current = view === "maj" ? "updates" : settings ? "settings" : "home";
    document.querySelectorAll("[data-nav]").forEach((a) => {
      if (a.dataset.nav === current) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
    if (settings) {
      state.tab = view === "maj" ? "maj" : (tab && (TABS[tab] || ["etat", "securite", "maj"].includes(tab)) ? tab : "etat");
      renderTab();
    }
  }

  async function start() {
    try {
      const res = await fetch("lapinou/fr.json", { cache: "no-store" });
      T = await res.json();
    } catch (e) { T = {}; }
    applyTexts(document);
    document.querySelectorAll(".tabs [data-tab]").forEach((b) => b.addEventListener("click", () => {
      location.hash = b.dataset.tab === "maj" ? "#maj" : "#reglages/" + b.dataset.tab;
    }));
    window.addEventListener("hashchange", route);
    buildToys();
    await loadRabbit();
    route();
    await loadStatus();
  }

  start();
})();
