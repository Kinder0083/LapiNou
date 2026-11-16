// SPDX-License-Identifier: MIT
// Page de réglages du serveur LapiNou : sans dépendance ni étape de construction. Les textes
// viennent de fr.json ; tout ce qui vient du serveur est posé en textContent (jamais en HTML).
"use strict";

(() => {
  const PAGES = ["brain", "budget", "voice", "persona", "wake", "tools", "connections",
    "memory", "timers", "rabbits", "journal", "updates", "usbkey"];
  let T = {};
  const state = { settings: null, secrets: {}, choices: {} };
  const main = document.getElementById("main");
  const nav = document.getElementById("nav");
  const menuButton = document.getElementById("menu-button");
  const logoutButton = document.getElementById("logout");

  // --- Outils ---------------------------------------------------------------------------

  const t = (path) => {
    const v = path.split(".").reduce((o, k) => (o == null ? undefined : o[k]), T);
    return v == null ? path : v;
  };
  const label = (key) => (T.fields && T.fields[key]) || [key, ""];

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "text") node.textContent = v;
      else if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (k === "value") node.value = v;
      else if (k === "style") node.style.cssText = v;  // CSSOM : permis par la CSP
      else if (k === "checked") node.checked = !!v;
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) {
      if (c == null || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }

  let toastTimer = null;
  function toast(message, isError = false) {
    const box = document.getElementById("toast");
    box.textContent = message;
    box.className = "toast" + (isError ? " error" : "");
    box.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { box.hidden = true; }, isError ? 8000 : 4500);
  }

  class ApiError extends Error {
    constructor(message, status) { super(message); this.status = status; }
  }

  function detailText(detail) {
    if (Array.isArray(detail)) return detail.map((d) => `${d.field} : ${d.message}`).join(" ; ");
    return typeof detail === "string" ? detail : JSON.stringify(detail);
  }

  async function api(method, path, body) {
    const opts = { method, headers: { "X-LapiNou": "1" }, credentials: "same-origin" };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let r;
    try {
      r = await fetch(path, opts);
    } catch (e) {
      throw new ApiError(t("app.offline"), 0);
    }
    const isJson = (r.headers.get("content-type") || "").includes("application/json");
    if (!r.ok) {
      let msg = `HTTP ${r.status}`;
      if (isJson) {
        const data = await r.json().catch(() => ({}));
        if (data.detail) msg = detailText(data.detail);
      }
      if (r.status === 401 && !["/api/login", "/api/password"].includes(path)) {
        start();
        throw new ApiError(t("app.session_lost"), 401);
      }
      throw new ApiError(msg, r.status);
    }
    return isJson ? r.json() : r;
  }

  // Bouton qui attend : désactivé pendant l'action, message d'erreur sinon.
  function action(button, fn) {
    return async (event) => {
      if (event) event.preventDefault();
      button.disabled = true;
      try {
        await fn();
      } catch (e) {
        if (!(e instanceof ApiError && e.status === 401)) toast(t("app.error") + e.message, true);
      } finally {
        button.disabled = false;
      }
    };
  }

  const getPath = (obj, path) => path.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj);
  function setPath(obj, path, value) {
    const keys = path.split(".");
    let o = obj;
    for (const k of keys.slice(0, -1)) o = o[k] = o[k] || {};
    o[keys[keys.length - 1]] = value;
  }

  const fmtDate = (iso, tz) => {
    try {
      return new Intl.DateTimeFormat("fr-FR", { dateStyle: "full", timeStyle: "short",
        timeZone: tz || undefined }).format(new Date(iso));
    } catch (e) {
      return iso;
    }
  };
  const fmtUsd = (x) => new Intl.NumberFormat("fr-FR", { style: "currency", currency: "USD" }).format(x || 0);
  const fmtInt = (x) => new Intl.NumberFormat("fr-FR").format(x || 0);

  // --- Formulaires de réglages ----------------------------------------------------------

  // Un formulaire : ses champs savent écrire leur valeur dans le corps de PUT /api/settings.
  class Form {
    constructor() { this.collectors = []; }

    wrap(key, control, id) {
      const [text, help] = label(key);
      return el("div", { class: "field" },
        el("label", { for: id, text }), control, help ? el("p", { class: "help", text: help }) : null);
    }

    text(key, path = key, type = "text", attrs = {}) {
      const id = "f-" + path.replace(/\W/g, "-");
      const input = el("input", { id, type, value: getPath(state.settings, path) ?? "", ...attrs });
      this.collectors.push((body) => setPath(body, path, input.value.trim()));
      return this.wrap(key, input, id);
    }

    number(key, path = key, attrs = {}) {
      const id = "f-" + path.replace(/\W/g, "-");
      const input = el("input", { id, type: "number", value: getPath(state.settings, path), ...attrs });
      this.collectors.push((body) => setPath(body, path, input.value === "" ? null : Number(input.value)));
      return this.wrap(key, input, id);
    }

    check(key, path = key) {
      const id = "f-" + path.replace(/\W/g, "-");
      const input = el("input", { id, type: "checkbox", checked: getPath(state.settings, path) });
      this.collectors.push((body) => setPath(body, path, input.checked));
      const [text, help] = label(key);
      return el("div", { class: "field" },
        el("label", { class: "check", for: id }, input, text),
        help ? el("p", { class: "help", text: help }) : null);
    }

    select(key, choices, path = key) {
      const id = "f-" + path.replace(/\W/g, "-");
      const current = getPath(state.settings, path);
      const input = el("select", { id },
        choices.map((c) => el("option", { value: c, text: c, selected: c === current })));
      this.collectors.push((body) => setPath(body, path, input.value));
      return this.wrap(key, input, id);
    }

    list(key, path = key) {
      const id = "f-" + path.replace(/\W/g, "-");
      const input = el("textarea", { id, rows: 4, spellcheck: "false" });
      input.value = (getPath(state.settings, path) || []).join("\n");
      this.collectors.push((body) => setPath(body, path,
        input.value.split("\n").map((s) => s.trim()).filter(Boolean)));
      return this.wrap(key, input, id);
    }

    textarea(key, path = key, attrs = {}) {
      const id = "f-" + path.replace(/\W/g, "-");
      const input = el("textarea", { id, rows: 5, ...attrs });
      input.value = getPath(state.settings, path) || "";
      this.collectors.push((body) => setPath(body, path, input.value));
      return this.wrap(key, input, id);
    }

    add(fn) { this.collectors.push(fn); }

    saveButton(after) {
      const button = el("button", { type: "submit", text: t("app.save") });
      button.addEventListener("click", action(button, async () => {
        const body = {};
        for (const c of this.collectors) c(body);
        const data = await api("PUT", "/api/settings", body);
        Object.assign(state, { settings: data.settings, secrets: data.secrets });
        toast(t("app.saved"));
        if (after) after();
      }));
      return el("div", { class: "actions" }, button);
    }
  }

  // Un secret : on ne voit que s'il est réglé ; on peut le changer ou l'effacer.
  function secret(key, name) {
    const id = "s-" + name.replace(/\W/g, "-");
    const status = el("span");
    const input = el("input", { id, type: "password", autocomplete: "new-password",
      placeholder: t("app.secret_placeholder") });
    const paint = () => {
      const set = !!state.secrets[name];
      status.className = "badge " + (set ? "ok" : "");
      status.textContent = set ? t("app.secret_set") : t("app.secret_unset");
      clear.hidden = !set;
    };
    const send = async (value) => {
      const data = await api("PUT", "/api/secrets", { name, value });
      state.secrets = data.secrets;
      input.value = "";
      paint();
      toast(value ? t("app.secret_changed") : t("app.secret_cleared"));
    };
    const save = el("button", { type: "button", class: "secondary", text: t("app.secret_save") });
    const clear = el("button", { type: "button", class: "danger", text: t("app.secret_clear") });
    save.addEventListener("click", action(save, () => (input.value ? send(input.value) : null)));
    clear.addEventListener("click", action(clear, () => (confirm(t("app.confirm")) ? send("") : null)));
    paint();
    const [text, help] = label(key);
    return el("div", { class: "field" },
      el("label", { for: id }, text, " ", status),
      el("div", { class: "row" }, el("div", { style: "flex:1 1 14rem" }, input), save, clear),
      help ? el("p", { class: "help", text: help }) : null);
  }

  const card = (...children) => el("section", { class: "card" }, ...children);
  const intro = (page) => el("p", { class: "intro", text: t("intro." + page) });
  const empty = () => el("p", { class: "meta", text: t("app.none") });

  // --- Pages ----------------------------------------------------------------------------

  const pages = {
    brain(root) {
      const f = new Form();
      root.append(card(intro("brain"),
        el("div", { class: "grid2" }, f.text("brain.primary"), f.text("brain.fast")),
        f.list("brain.fallback_chain"), f.check("brain.web_search")));

      const providers = JSON.parse(JSON.stringify(state.settings.brain.providers || {}));
      const list = el("ul", { class: "list" });
      const draw = () => {
        list.replaceChildren();
        const names = Object.keys(providers);
        if (!names.length) list.append(el("li", {}, empty()));
        for (const name of names) {
          const p = providers[name];
          const kind = el("select", { "aria-label": label("provider.kind")[0] },
            state.choices.provider_kind.map((c) => el("option", { value: c, text: c, selected: c === p.kind })));
          kind.addEventListener("change", () => { p.kind = kind.value; });
          const url = el("input", { type: "url", value: p.base_url || "", placeholder: label("provider.base_url")[0],
            "aria-label": label("provider.base_url")[0] });
          url.addEventListener("input", () => { p.base_url = url.value.trim(); });
          const remove = el("button", { type: "button", class: "danger", text: t("app.delete"),
            onclick: () => { delete providers[name]; draw(); } });
          list.append(el("li", {},
            el("div", { class: "grow" }, el("h3", { text: name }),
              el("div", { class: "grid2" },
                el("div", { class: "field" }, el("span", { class: "label", text: label("provider.kind")[0] }), kind),
                el("div", { class: "field" }, el("span", { class: "label", text: label("provider.base_url")[0] }), url)),
              state.secrets[`brain.providers.${name}.api_key`] !== undefined
                ? secret("provider.api_key", `brain.providers.${name}.api_key`) : null),
            remove));
        }
      };
      const newName = el("input", { type: "text", placeholder: label("provider.name")[0],
        "aria-label": label("provider.name")[0], pattern: "[a-z0-9_-]+" });
      const addButton = el("button", { type: "button", class: "secondary", text: label("provider.add")[0],
        onclick: () => {
          const name = newName.value.trim().toLowerCase();
          if (!name || providers[name]) return;
          providers[name] = { kind: "openai", base_url: "" };
          newName.value = "";
          draw();
        } });
      f.add((body) => setPath(body, "brain.providers", providers));
      draw();
      const [ptext, phelp] = label("providers");
      root.append(card(el("h3", { text: ptext }), el("p", { class: "help", text: phelp }), list,
        el("div", { class: "row" }, el("div", { style: "flex:1 1 12rem" }, newName), addButton),
        f.saveButton(() => show("brain"))));
    },

    async budget(root) {
      const f = new Form();
      root.append(card(intro("budget"),
        el("div", { class: "grid2" }, f.number("budget.monthly_usd", undefined, { min: 0, step: "0.5" }),
          f.number("budget.warn_pct", undefined, { min: 1, max: 100 }),
          f.number("budget.stop_pct", undefined, { min: 1, max: 1000 })),
        f.saveButton(() => show("budget"))));
      const b = await api("GET", "/api/budget");
      const pct = b.ratio == null ? 100 : Math.round(b.ratio * 1000) / 10;
      const level = pct >= b.stop_pct ? "bad" : pct >= b.warn_pct ? "warn" : "";
      const meter = el("div", { class: "meter " + level, role: "progressbar", "aria-valuemin": 0,
        "aria-valuemax": 100, "aria-valuenow": Math.min(pct, 100) },
      el("span", { style: `width:${Math.min(pct, 100)}%` }));
      const rows = b.rows.map((r) => el("tr", {},
        el("td", { text: r.provider }), el("td", { text: r.model }),
        el("td", { class: "num", text: fmtInt(r.input_tokens) }),
        el("td", { class: "num", text: fmtInt(r.output_tokens) }),
        el("td", { class: "num", text: fmtUsd(r.usd) })));
      root.append(card(el("h3", { text: `${t("budget.month")} (${b.month})` }),
        el("p", { class: "big" }, fmtUsd(b.spent), el("span", { class: "meta", text: ` ${t("budget.of")} ${fmtUsd(b.monthly_usd)} · ${pct} %` })),
        meter,
        level ? el("p", { class: "notice " + level, text: t(level === "bad" ? "budget.stop" : "budget.warn") }) : null,
        rows.length ? el("div", { class: "table-wrap" }, el("table", {},
          el("thead", {}, el("tr", {}, el("th", { text: t("budget.provider") }), el("th", { text: t("budget.model") }),
            el("th", { class: "num", text: t("budget.tokens_in") }), el("th", { class: "num", text: t("budget.tokens_out") }),
            el("th", { class: "num", text: t("budget.cost") }))),
          el("tbody", {}, rows))) : empty(),
        b.history.length > 1 ? el("div", {}, el("h3", { text: t("budget.history") }),
          el("ul", { class: "list" }, b.history.map((h) => el("li", {},
            el("span", { class: "grow", text: h.month }), el("strong", { text: fmtUsd(h.usd) }))))) : null));
    },

    voice(root) {
      const f = new Form();
      const sample = el("input", { type: "text", value: "Bonjour, je suis Lapinou ! On joue ?",
        "aria-label": label("voice.preview_text")[0] });
      const audio = el("audio", { controls: true, hidden: true });
      const play = el("button", { type: "button", class: "secondary", text: label("voice.preview")[0] });
      play.addEventListener("click", action(play, async () => {
        const r = await fetch("/api/voice/preview?text=" + encodeURIComponent(sample.value), { credentials: "same-origin" });
        if (!r.ok) {
          const data = await r.json().catch(() => ({}));
          throw new Error(detailText(data.detail || `HTTP ${r.status}`));
        }
        if (audio.src) URL.revokeObjectURL(audio.src);
        audio.src = URL.createObjectURL(await r.blob());
        audio.hidden = false;
        audio.play();
      }));
      root.append(card(intro("voice"),
        el("div", { class: "grid2" }, f.select("voice.tts_engine", state.choices.tts_engine), f.text("voice.voice")),
        f.saveButton()),
      card(el("div", { class: "field" }, el("label", { text: label("voice.preview_text")[0] }), sample),
        el("div", { class: "row" }, play, audio)));
    },

    async persona(root) {
      const f = new Form();
      const p = await api("GET", "/api/persona");
      root.append(card(intro("persona"),
        f.textarea("brain.persona_extra", undefined, { maxlength: 2000, rows: 6 }), f.saveButton()),
      card(el("details", {}, el("summary", { text: "Les règles de base de Lapinou" }),
        el("pre", { text: p.base }))));
    },

    wake(root) {
      const f = new Form();
      root.append(card(intro("wake"),
        f.select("voice.wake_method", state.choices.wake_method),
        el("div", { class: "grid2" },
          f.number("voice.wake_threshold", undefined, { min: 0, max: 1, step: "0.01" }),
          f.number("voice.followup_s", undefined, { min: 0, max: 30 }),
          f.number("voice.vad_silence_ms", undefined, { min: 200, max: 3000, step: 50 })),
        f.saveButton()));
    },

    tools(root) {
      const f = new Form();
      const range = state.settings.safety.climate_range || [16, 24];
      const cmin = el("input", { id: "climate-min", type: "number", step: "0.5", value: range[0] });
      const cmax = el("input", { id: "climate-max", type: "number", step: "0.5", value: range[1] });
      f.add((body) => setPath(body, "safety.climate_range", [Number(cmin.value), Number(cmax.value)]));
      root.append(
        el("section", { class: "card security", role: "note" },
          el("h3", { text: t("tools.security_title") }), el("p", { text: t("tools.security_note") })),
        card(intro("tools"),
          f.list("safety.allowed_domains"), f.list("safety.allowed_entities"),
          f.list("safety.sensitive_entities"),
          el("div", { class: "grid2" }, f.list("safety.sensitive_domains"), f.list("safety.sensitive_cover_classes")),
          el("div", { class: "grid2" }, f.wrap("safety.climate_min", cmin, "climate-min"),
            f.wrap("safety.climate_max", cmax, "climate-max")),
          el("div", { class: "grid2" }, f.list("safety.assist_blocked_stems"), f.list("safety.assist_blocked_words")),
          f.saveButton()),
        card(secret("safety.pin", "safety.pin")));
    },

    connections(root) {
      const f = new Form();
      root.append(card(intro("connections"),
        el("div", { class: "grid2" }, f.text("server.public_url", undefined, "url"), f.text("server.timezone")),
        el("h3", { text: "Home Assistant" }), f.text("ha.url", undefined, "url"),
        el("h3", { text: "Lyrion" }),
        el("div", { class: "grid2" }, f.text("lms.host"), f.number("lms.port", undefined, { min: 1, max: 65535 })),
        f.list("lms.allowed_url_hosts"),
        el("h3", { text: "MQTT" }),
        el("div", { class: "grid2" }, f.text("mqtt.host"), f.number("mqtt.port", undefined, { min: 1, max: 65535 })),
        f.text("mqtt.username"),
        f.saveButton()),
      card(secret("ha.token", "ha.token"), secret("mqtt.password", "mqtt.password")));
    },

    async memory(root) {
      const f = new Form();
      root.append(card(intro("memory"),
        el("div", { class: "grid2" }, f.check("memory.enabled"),
          f.number("memory.max_items", undefined, { min: 1, max: 500 })),
        f.saveButton()));
      const data = await api("GET", "/api/memories");
      const list = el("ul", { class: "list" });
      if (!data.memories.length) list.append(el("li", {}, empty()));
      for (const m of data.memories) {
        const text = el("div", { class: "grow" }, el("div", { text: m.text }),
          el("div", { class: "meta", text: `n°${m.id} · ${fmtDate(m.created_at)}` }));
        const edit = el("button", { type: "button", class: "secondary", text: t("app.edit") });
        const del = el("button", { type: "button", class: "danger", text: t("app.delete") });
        edit.addEventListener("click", () => {
          const area = el("textarea", { rows: 2, maxlength: 300 });
          area.value = m.text;
          const ok = el("button", { type: "button", text: t("app.save") });
          ok.addEventListener("click", action(ok, async () => {
            await api("PUT", `/api/memories/${m.id}`, { text: area.value });
            show("memory");
          }));
          const cancel = el("button", { type: "button", class: "secondary", text: t("app.cancel"),
            onclick: () => show("memory") });
          text.replaceChildren(area, el("div", { class: "actions" }, ok, cancel));
        });
        del.addEventListener("click", action(del, async () => {
          if (!confirm(t("app.confirm"))) return;
          await api("DELETE", `/api/memories/${m.id}`);
          show("memory");
        }));
        list.append(el("li", {}, text, edit, del));
      }
      const fresh = el("textarea", { id: "new-memory", rows: 2, maxlength: 300 });
      const add = el("button", { type: "button", text: t("app.add") });
      add.addEventListener("click", action(add, async () => {
        if (!fresh.value.trim()) return;
        await api("POST", "/api/memories", { text: fresh.value });
        show("memory");
      }));
      const clear = el("button", { type: "button", class: "danger", text: label("memory.clear")[0] });
      clear.addEventListener("click", action(clear, async () => {
        if (!confirm(t("app.confirm"))) return;
        await api("DELETE", "/api/memories");
        show("memory");
      }));
      root.append(card(el("h3", { text: `${data.memories.length} / ${data.max_items}` }), list),
        card(f.wrap("memory.new", fresh, "new-memory"),
          el("div", { class: "actions" }, add, data.memories.length ? clear : null)));
    },

    async timers(root) {
      const data = await api("GET", "/api/timers");
      const list = el("ul", { class: "list" });
      if (!data.timers.length) list.append(el("li", {}, empty()));
      for (const tm of data.timers) {
        const cancel = el("button", { type: "button", class: "danger", text: t("timers.cancel") });
        cancel.addEventListener("click", action(cancel, async () => {
          await api("DELETE", `/api/timers/${tm.id}`);
          show("timers");
        }));
        list.append(el("li", {},
          el("div", { class: "grow" }, el("div", { text: tm.text }),
            el("div", { class: "meta" }, `${fmtDate(tm.due_at, data.timezone)} · ${t("timers.rabbit")} ${tm.rabbit_id}`,
              tm.recurrence ? ` · ${t("timers.repeat")} : ${tm.recurrence.split("@")[0]}` : "")),
          cancel));
      }
      root.append(card(intro("timers"), list));
    },

    async rabbits(root) {
      const data = await api("GET", "/api/rabbits");
      const list = el("ul", { class: "list" });
      const edits = {};
      if (!data.rabbits.length) list.append(el("li", {}, empty()));
      for (const r of data.rabbits) {
        const name = el("input", { type: "text", value: r.name === r.id ? "" : r.name, placeholder: r.id,
          "aria-label": label("rabbit.name")[0] });
        const player = el("input", { type: "text", value: r.default_lms_player,
          "aria-label": label("rabbit.default_lms_player")[0] });
        edits[r.id] = () => ({ name: name.value.trim(), default_lms_player: player.value.trim() });
        const badge = r.connected ? ["ok", t("rabbits.connected")]
          : r.state === "inconnu" ? ["", t("rabbits.unknown")] : ["bad", t("rabbits.disconnected")];
        list.append(el("li", {}, el("div", { class: "grow" },
          el("h3", {}, r.name || r.id, " ", el("span", { class: "badge " + badge[0], text: badge[1] })),
          el("div", { class: "meta", text: `${t("rabbits.id")} : ${r.id}` }),
          el("div", { class: "grid2" },
            el("div", { class: "field" }, el("span", { class: "label", text: label("rabbit.name")[0] }), name),
            el("div", { class: "field" }, el("span", { class: "label", text: label("rabbit.default_lms_player")[0] }), player,
              el("p", { class: "help", text: label("rabbit.default_lms_player")[1] }))))));
      }
      const f = new Form();
      f.add((body) => {
        const rabbits = JSON.parse(JSON.stringify(state.settings.rabbits || {}));
        for (const [id, get] of Object.entries(edits)) rabbits[id] = get();
        body.rabbits = rabbits;
      });
      root.append(card(intro("rabbits"), list, data.rabbits.length ? f.saveButton(() => show("rabbits")) : null));
    },

    async journal(root) {
      const f = new Form();
      root.append(card(intro("journal"), f.number("logs.retention_days", undefined, { min: 1, max: 365 }),
        f.saveButton()));
      const data = await api("GET", "/api/journal");
      const list = el("ul", { class: "list" });
      if (!data.conversations.length) list.append(el("li", {}, empty()));
      for (const c of data.conversations) {
        list.append(el("li", {}, el("div", { class: "grow" },
          el("div", { class: "meta", text: `${t("journal.started")} ${fmtDate(c.started_at)} · ${c.rabbit_id}` }),
          c.turns.map((tu) => el("p", { class: "turn " + tu.role },
            el("span", { class: "who", text: tu.role === "user" ? t("journal.user") : t("journal.assistant") }),
            tu.text)))));
      }
      const clear = el("button", { type: "button", class: "danger", text: label("journal.clear")[0] });
      clear.addEventListener("click", action(clear, async () => {
        if (!confirm(t("app.confirm"))) return;
        await api("DELETE", "/api/journal");
        show("journal");
      }));
      root.append(card(list, data.conversations.length ? el("div", { class: "actions" }, clear) : null));
    },

    async updates(root) {
      const data = await api("GET", "/api/updates");
      const out = el("pre", { hidden: true });
      const update = el("button", { type: "button", text: t("updates.update") });
      const restart = el("button", { type: "button", class: "secondary", text: t("app.restart") });
      update.addEventListener("click", action(update, async () => {
        toast(t("updates.updating"));
        const r = await api("POST", "/api/updates/server");
        out.textContent = r.output;
        out.hidden = !r.output;
        toast(r.message, !r.ok);
      }));
      restart.addEventListener("click", action(restart, async () => {
        if (!confirm(t("app.confirm"))) return;
        await api("POST", "/api/restart");
        toast(t("app.restarting"));
      }));
      root.append(card(intro("updates"), el("h3", { text: t("updates.server") }),
        el("p", {}, `${t("updates.version")} ${data.server.version} · ${t("updates.commit")} `, el("code", { text: data.server.commit })),
        el("div", { class: "actions" }, update, restart),
        el("details", {}, el("summary", { text: t("updates.output") }), out)));

      const ota = data.ota;
      if (ota) {
        const f = new Form();
        const check = el("button", { type: "button", class: "secondary", text: t("updates.check") });
        check.addEventListener("click", action(check, async () => {
          toast(t("updates.checking"));
          await api("POST", "/api/updates/ota");
          show("updates");
        }));
        root.append(card(el("h3", { text: t("updates.rabbits") }),
          el("p", {}, `${t("updates.served")} : `, el("strong", { text: ota.version || "—" })),
          ota.latest ? el("p", { text: `${t("updates.latest")} : ${ota.latest}` }) : null,
          ota.checked_at ? el("p", { class: "meta", text: `${t("updates.checked")} : ${fmtDate(ota.checked_at)}` }) : null,
          !ota.version ? el("p", { class: "notice warn", text: t("updates.no_release") }) : null,
          ota.error ? el("p", { class: "notice error", text: ota.error }) : null,
          el("div", { class: "actions" }, check),
          f.text("ota.repo"), f.saveButton(), secret("ota.token", "ota.token")));
      }

      const old = el("input", { id: "pw-old", type: "password", autocomplete: "current-password" });
      const neu = el("input", { id: "pw-new", type: "password", autocomplete: "new-password", minlength: 8 });
      const change = el("button", { type: "button", text: t("updates.change_password") });
      change.addEventListener("click", action(change, async () => {
        await api("POST", "/api/password", { old: old.value, new: neu.value });
        old.value = neu.value = "";
        toast(t("updates.password_changed"));
      }));
      const f2 = new Form();
      root.append(card(el("h3", { text: t("updates.admin") }),
        el("div", { class: "grid2" }, f2.wrap("password.old", old, "pw-old"), f2.wrap("password.new", neu, "pw-new")),
        el("div", { class: "actions" }, change)));
    },

    usbkey(root) {
      const inputs = {};
      const f = new Form();
      const field = (name, type = "text", attrs = {}) => {
        const id = "usb-" + name;
        inputs[name] = el("input", { id, type, ...attrs });
        return f.wrap("usb." + name, inputs[name], id);
      };
      const build = el("button", { type: "submit", text: t("usbkey.build") });
      const form = el("form", { autocomplete: "off" },
        el("h3", { text: t("usbkey.network") }),
        el("div", { class: "grid2" }, field("ssid", "text", { required: true }),
          field("password", "password", { required: true, minlength: 8, maxlength: 63 })),
        el("div", { class: "grid2" }, field("ip", "text", { inputmode: "decimal" }),
          field("netmask", "text", { value: "255.255.255.0", inputmode: "decimal" }),
          field("gateway", "text", { inputmode: "decimal" }), field("dns", "text", { inputmode: "decimal" })),
        el("h3", { text: t("usbkey.access") }),
        el("div", { class: "grid2" }, field("hostname", "text", { placeholder: "lapin-salon" }),
          field("web_password", "password", { autocomplete: "new-password" })),
        field("ssh_pubkey"),
        el("p", { class: "help", text: t("usbkey.includes") }),
        el("div", { class: "actions" }, build));
      form.addEventListener("submit", action(build, async () => {
        const body = Object.fromEntries(Object.entries(inputs).map(([k, i]) => [k, i.value.trim()]));
        body.password = inputs.password.value;  // espaces gardés
        body.web_password = inputs.web_password.value;
        toast(t("usbkey.building"));
        const r = await api("POST", "/api/usbkey", body);
        const blob = await r.blob();
        const disposition = r.headers.get("content-disposition") || "";
        const m = /filename="?([^";]+)"?/.exec(disposition);
        const a = el("a", { href: URL.createObjectURL(blob), download: m ? m[1] : "lapinou-cle.zip" });
        document.body.append(a);
        a.click();
        setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 2000);
        toast(t("usbkey.done"));
      }));
      root.append(card(intro("usbkey"), form));
    },
  };

  // --- Navigation -----------------------------------------------------------------------

  function buildNav() {
    nav.replaceChildren(...PAGES.map((p) => el("a", { href: "#" + p, "data-page": p, text: t("pages." + p) })));
    nav.hidden = false;
    menuButton.hidden = false;
    menuButton.textContent = t("app.menu");
    logoutButton.hidden = false;
    logoutButton.textContent = t("app.logout");
  }

  async function show(page) {
    if (!PAGES.includes(page)) page = PAGES[0];
    for (const a of nav.querySelectorAll("a")) {
      if (a.dataset.page === page) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    }
    nav.classList.add("closed");
    menuButton.setAttribute("aria-expanded", "false");
    document.title = `LapiNou : ${t("pages." + page)}`;
    const root = el("div");
    main.replaceChildren(el("h2", { text: t("pages." + page) }), root);
    try {
      await pages[page](root);
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 401)) {
        root.append(el("p", { class: "notice error", text: t("app.error") + e.message }));
      }
    }
    main.focus({ preventScroll: true });
  }

  function loginScreen(passwordSet) {
    nav.hidden = menuButton.hidden = logoutButton.hidden = true;
    const pw = el("input", { id: "login-pw", type: "password", required: true, minlength: passwordSet ? null : 8,
      autocomplete: passwordSet ? "current-password" : "new-password" });
    const pw2 = el("input", { id: "login-pw2", type: "password", required: true, minlength: 8, autocomplete: "new-password" });
    const submit = el("button", { type: "submit", text: t(passwordSet ? "login.submit" : "login.setup_submit") });
    const message = el("p", { class: "notice error", hidden: true, role: "alert" });
    const form = el("form", {},
      el("div", { class: "field" }, el("label", { for: "login-pw", text: t("login.password") }), pw),
      passwordSet ? null : el("div", { class: "field" }, el("label", { for: "login-pw2", text: t("login.setup_confirm") }), pw2),
      message, el("div", { class: "actions" }, submit));
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      message.hidden = true;
      if (!passwordSet && pw.value !== pw2.value) {
        message.textContent = t("login.mismatch");
        message.hidden = false;
        return;
      }
      submit.disabled = true;
      try {
        await api("POST", passwordSet ? "/api/login" : "/api/setup", { password: pw.value });
        await start();
      } catch (e) {
        message.textContent = e.status === 401 ? t("login.wrong") : e.status === 429 ? t("login.locked") : e.message;
        message.hidden = false;
      } finally {
        submit.disabled = false;
      }
    });
    main.replaceChildren(el("section", { class: "card login" },
      el("h2", { text: t(passwordSet ? "login.title" : "login.setup_title") }),
      el("p", { class: "intro", text: t(passwordSet ? "login.intro" : "login.setup_intro") }), form));
    pw.focus();
  }

  async function start() {
    if (!T.app) T = await (await fetch("/static/fr.json")).json();
    document.getElementById("subtitle").textContent = t("app.subtitle");
    let session;
    try {
      session = await api("GET", "/api/session");
    } catch (e) {
      main.replaceChildren(el("p", { class: "notice error", text: e.message }));
      return;
    }
    if (!session.logged_in) {
      loginScreen(session.password_set);
      return;
    }
    const data = await api("GET", "/api/settings");
    Object.assign(state, data);
    buildNav();
    show(location.hash.slice(1));
  }

  window.addEventListener("hashchange", () => { if (state.settings) show(location.hash.slice(1)); });
  menuButton.addEventListener("click", () => {
    const open = nav.classList.toggle("closed") === false;
    menuButton.setAttribute("aria-expanded", String(open));
  });
  logoutButton.addEventListener("click", async () => {
    await api("POST", "/api/logout").catch(() => null);
    state.settings = null;
    start();
  });
  start();
})();
