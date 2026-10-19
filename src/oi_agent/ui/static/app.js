/* OI setup wizard + dashboard. Plain JS, no build step, no CDN. */
"use strict";

const $ = (id) => document.getElementById(id);
let state = {
  mode: "wizard",
  discordToken: "",        // kept in memory only, sent with the final save
  application: null,
  watches: [],             // {kind:"guild"|"channel", id, name, channels?, repos:[], post_hourly_cap}
  model: "",
  tokenFrom: "wizard",
};

async function api(path, body, method) {
  const opts = { method: method || (body !== undefined ? "POST" : "GET") };
  if (body !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch { /* non-JSON error page */ }
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `request failed (HTTP ${res.status})`);
  }
  return data;
}

function show(name) {
  document.querySelectorAll(".view").forEach((v) => { v.hidden = v.id !== `view-${name}`; });
}

function setError(id, message) {
  const el = $(id);
  el.textContent = message || "";
  el.hidden = !message;
}

/* ---------------- wizard: OpenCode ---------------- */

async function refreshOcStatus() {
  const panel = $("oc-status");
  $("oc-error").hidden = true;
  try {
    const st = await api("/api/opencode/status");
    if (!st.installed) {
      panel.textContent = "OpenCode is not installed yet.";
      $("btn-oc-install").hidden = false;
      return;
    }
    $("btn-oc-install").hidden = true;
    panel.innerHTML = st.authenticated
      ? `OpenCode <b>${st.version}</b> installed. Provider connected: <b>${st.integrations.join(", ")}</b>.`
      : `OpenCode <b>${st.version}</b> installed. No provider connected yet.`;
    $("oc-login").hidden = st.authenticated;
    $("btn-oc-next").disabled = !st.authenticated;
    if (st.authenticated) await loadModels();
  } catch (err) {
    setError("oc-error", String(err.message || err));
  }
}

async function installOc() {
  $("btn-oc-install").disabled = true;
  $("oc-status").textContent = "Installing OpenCode… (this can take a minute)";
  try {
    const out = await api("/api/opencode/install", {});
    $("oc-status").textContent = `Installed OpenCode ${out.version}.`;
  } catch (err) {
    setError("oc-error", String(err.message || err));
  }
  $("btn-oc-install").disabled = false;
  await refreshOcStatus();
}

async function startLogin() {
  $("btn-oc-login-start").disabled = true;
  try {
    await api("/api/opencode/login/start", {});
    const out = await api("/api/opencode/login/integrations");
    const host = $("oc-integrations");
    host.innerHTML = "";
    for (const integ of out.integrations || []) {
      const row = document.createElement("div");
      row.className = "panel";
      row.innerHTML = `<b>${integ.name || integ.id}</b> <span class="muted">${integ.id}</span><br>`;
      const keyInput = document.createElement("input");
      keyInput.type = "password";
      keyInput.placeholder = "API key";
      keyInput.autocomplete = "off";
      const keyBtn = document.createElement("button");
      keyBtn.textContent = "Connect with API key";
      keyBtn.onclick = async () => {
        try {
          await api("/api/opencode/login/key", { integration: integ.id, key: keyInput.value });
          row.append(" connected ✓");
          keyInput.value = "";
          await refreshOcStatus();
        } catch (err) { setError("oc-error", String(err.message || err)); }
      };
      const oauthBtn = document.createElement("button");
      oauthBtn.textContent = "Sign in with browser";
      oauthBtn.onclick = async () => {
        try {
          const out2 = await api("/api/opencode/login/oauth", { integration: integ.id, method_id: "oauth" });
          $("oc-oauth").hidden = false;
          $("oc-oauth-instructions").textContent = out2.instructions || "";
          $("oc-oauth-link").href = out2.url || "#";
          pollOauth(out2.attempt_id);
        } catch (err) { setError("oc-error", String(err.message || err)); }
      };
      row.append(keyInput, keyBtn, oauthBtn);
      host.append(row);
    }
  } catch (err) {
    setError("oc-error", String(err.message || err));
  }
  $("btn-oc-login-start").disabled = false;
}

async function pollOauth(attemptId) {
  for (;;) {
    try {
      const out = await api(`/api/opencode/login/oauth/${encodeURIComponent(attemptId)}`);
      $("oc-oauth-state").textContent = out.done ? "Sign-in detected, finishing…" : "Waiting for you to finish…";
      if (out.done) {
        await api(`/api/opencode/login/oauth/${encodeURIComponent(attemptId)}/complete`, {});
        $("oc-oauth-state").textContent = "Connected ✓";
        await refreshOcStatus();
        return;
      }
    } catch { /* keep polling */ }
    await new Promise((r) => setTimeout(r, 2000));
  }
}

async function loadModels() {
  try {
    const out = await api("/api/opencode/models");
    const select = $("oc-model");
    select.innerHTML = "";
    for (const model of out.models || []) {
      const opt = document.createElement("option");
      opt.value = model; opt.textContent = model;
      select.append(opt);
    }
    state.model = select.value || state.model;
  } catch (err) {
    setError("oc-error", String(err.message || err));
  }
}

/* ---------------- wizard: Discord ---------------- */

async function checkDiscord() {
  setError("discord-error", "");
  const result = $("discord-result");
  result.hidden = true;
  try {
    const out = await api("/api/discord/validate", { token: $("discord-token").value });
    state.discordToken = $("discord-token").value;
    state.application = out.application;
    result.innerHTML = out.message_content_enabled
      ? `Token ok — application <b>${out.application.name}</b>.`
      : `Token ok — application <b>${out.application.name}</b>, but the bot cannot read messages yet.`;
    if (out.hint) {
      result.innerHTML += `<div class="error">${out.hint}</div>`;
    }
    result.innerHTML += `<p>Invite the bot: <a href="${out.invite_url}" target="_blank" rel="noreferrer">${out.invite_url}</a></p>`;
    result.hidden = false;
    $("btn-discord-next").disabled = false;
  } catch (err) {
    setError("discord-error", String(err.message || err));
    $("btn-discord-next").disabled = true;
  }
}

/* ---------------- wizard: channels + repos (shared with dashboard) ---------------- */

function watchFromGuild(guild, channelIds, mode) {
  const watch = {
    channel_id: 0, guild_id: 0, repos: [], ignored_channels: [],
    allowed_channels: [], bot_user_id: 0, post_hourly_cap: 3,
  };
  if (mode === "guild") {
    watch.guild_id = Number(guild.id);
  } else {
    watch.channel_id = Number(channelIds[0]);
  }
  return watch;
}

async function renderGuilds() {
  const host = $("guilds");
  host.innerHTML = "<p class=\"muted\">Loading servers…</p>";
  try {
    const out = await api("/api/discord/guilds", { token: state.discordToken });
    host.innerHTML = "";
    state.watches = [];
    for (const guild of out.guilds || []) {
      const box = document.createElement("div");
      box.className = "guild";
      const title = document.createElement("label");
      const tick = document.createElement("input");
      tick.type = "checkbox";
      tick.dataset.guild = guild.id;
      tick.onchange = () => updateWatchesFromGuilds();
      title.append(tick, document.createTextNode(` Whole server: ${guild.name}`));
      const pick = document.createElement("button");
      pick.textContent = "or pick single channels";
      const list = document.createElement("div");
      list.className = "channel-list";
      list.hidden = true;
      pick.onclick = async () => {
        list.hidden = !list.hidden;
        if (!list.dataset.loaded) {
          const chans = await api("/api/discord/channels", { token: state.discordToken, guild_id: guild.id });
          list.dataset.loaded = "1";
          for (const ch of chans.channels || []) {
            const l = document.createElement("label");
            const cb = document.createElement("input");
            cb.type = "checkbox";
            cb.value = ch.id;
            cb.dataset.guild = guild.id;
            cb.onchange = () => updateWatchesFromGuilds();
            l.append(cb, document.createTextNode(` #${ch.name}`));
            list.append(l);
          }
        }
      };
      box.append(title, pick, list);
      host.append(box);
    }
    if (!(out.guilds || []).length) host.innerHTML = "<p>No servers visible — invite the bot first.</p>";
  } catch (err) {
    host.innerHTML = `<p class="error">${err.message || err}</p>`;
  }
  updateWatchesFromGuilds();
}

function updateWatchesFromGuilds() {
  const guildWatches = new Map();
  document.querySelectorAll("#guilds input[type=checkbox]:checked").forEach((cb) => {
    const gid = cb.dataset.guild;
    if (!guildWatches.has(gid)) {
      guildWatches.set(gid, { channel_id: 0, guild_id: Number(gid), repos: [], ignored_channels: [], allowed_channels: [], bot_user_id: 0, post_hourly_cap: 3 });
    }
  });
  const channelPicks = new Map();
  document.querySelectorAll("#guilds .channel-list input:checked").forEach((cb) => {
    const gid = cb.dataset.guild;
    if (guildWatches.has(gid)) return; // whole server already selected
    if (!channelPicks.has(gid)) channelPicks.set(gid, []);
    channelPicks.get(gid).push(Number(cb.value));
  });
  const next = [...guildWatches.values()];
  for (const [gid, channels] of channelPicks) {
    for (const cid of channels) {
      next.push({ channel_id: cid, guild_id: 0, repos: [], ignored_channels: [], allowed_channels: [], bot_user_id: 0, post_hourly_cap: 3 });
    }
  }
  state.watches = next;
  $("btn-channels-next").disabled = !state.watches.length;
  renderRepoPickers();
  renderCaps();
}

async function renderRepoPickers() {
  const host = $("watch-repos");
  host.innerHTML = "";
  state.watches.forEach((watch, index) => {
    const name = watch.guild_id ? `server ${watch.guild_id}` : `channel ${watch.channel_id}`;
    const box = document.createElement("div");
    box.className = "watch";
    box.innerHTML = `<b>${name}</b>`;
    const found = document.createElement("div");
    found.innerHTML = "<span class=\"muted\">run a scan to list clones</span>";
    const add = document.createElement("input");
    add.placeholder = "/absolute/path/to/repo";
    const addBtn = document.createElement("button");
    addBtn.textContent = "Add path";
    addBtn.onclick = async () => {
      try {
        const out = await api("/api/repos/validate", { path: add.value });
        if (!watch.repos.includes(out.path)) watch.repos.push(out.path);
        add.value = "";
        renderRepoPickers();
      } catch (err) { setError("settings-error", String(err.message || err)); }
    };
    const list = document.createElement("ul");
    for (const repo of watch.repos) {
      const li = document.createElement("li");
      li.textContent = repo + " ";
      const rm = document.createElement("button");
      rm.textContent = "remove";
      rm.onclick = () => { watch.repos = watch.repos.filter((r) => r !== repo); renderRepoPickers(); };
      li.append(rm);
      list.append(li);
    }
    box.append(found, add, addBtn, list);
    box.dataset.scanIndex = index;
    host.append(box);
  });
}

async function scanRepos() {
  try {
    const out = await api("/api/repos/scan", {});
    document.querySelectorAll("#watch-repos .watch").forEach((box, index) => {
      const watch = state.watches[index];
      if (!watch) return;
      const found = box.querySelector("div");
      found.innerHTML = "";
      for (const repo of out.repos || []) {
        const l = document.createElement("label");
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = watch.repos.includes(repo);
        cb.onchange = () => {
          watch.repos = cb.checked
            ? [...new Set([...watch.repos, repo])]
            : watch.repos.filter((r) => r !== repo);
        };
        l.append(cb, document.createTextNode(` ${repo}`));
        found.append(l);
      }
      if (!(out.repos || []).length) found.innerHTML = "<span class=\"muted\">no git clones found</span>";
    });
  } catch (err) {
    setError("settings-error", String(err.message || err));
  }
}

function renderCaps() {
  const host = $("watch-caps");
  host.innerHTML = "<h3>Posts per hour cap, per watch</h3>";
  state.watches.forEach((watch, index) => {
    const name = watch.guild_id ? `server ${watch.guild_id}` : `channel ${watch.channel_id}`;
    const l = document.createElement("label");
    l.textContent = `${name} `;
    const input = document.createElement("input");
    input.type = "number";
    input.min = "1";
    input.value = watch.post_hourly_cap;
    input.oninput = () => { watch.post_hourly_cap = Number(input.value) || 3; };
    l.append(input);
    host.append(l);
  });
}

/* ---------------- wizard: review + save + service ---------------- */

function renderReview() {
  const host = $("review");
  const rows = [
    ["Model", state.model],
    ["Personality", $("f-personality").value],
    ["Environment", $("f-environment_mode").value],
    ["Token env var", $("f-discord_token_env").value],
    ["Bot token", state.discordToken ? "saved (hidden)" : "NOT SET"],
  ];
  for (const watch of state.watches) {
    rows.push([
      "Watch",
      watch.guild_id ? `server ${watch.guild_id}` : `channel ${watch.channel_id}`,
      `repos: ${watch.repos.length ? watch.repos.join(", ") : "none"}`,
      `cap: ${watch.post_hourly_cap}/h`,
    ]);
  }
  host.innerHTML = "<table class=\"review-table\">" + rows.map(
    (r) => `<tr><td><b>${r[0]}</b></td><td>${r.slice(1).join(" — ")}</td></tr>`
  ).join("") + "</table>";
}

async function saveSetup() {
  setError("save-error", "");
  $("btn-save").disabled = true;
  try {
    const payload = {
      config: {
        discord_token_env: $("f-discord_token_env").value || "OI_DISCORD_TOKEN",
        personality: $("f-personality").value,
        environment_mode: $("f-environment_mode").value,
        opencode_model: state.model,
        watches: state.watches,
      },
      discord_token: state.discordToken,
    };
    await api("/api/save", payload);
    show("service");
  } catch (err) {
    setError("save-error", String(err.message || err));
  }
  $("btn-save").disabled = false;
}

async function installService() {
  $("btn-install-service").disabled = true;
  try {
    await api("/api/service/install", {});
    $("service-result").textContent = "Started. OI now runs in the background.";
    $("service-result").hidden = false;
    $("btn-open-dashboard").hidden = false;
  } catch (err) {
    $("service-result").textContent = `Could not install the service: ${err.message || err}`;
    $("service-result").hidden = false;
  }
  $("btn-install-service").disabled = false;
}

/* ---------------- dashboard ---------------- */

async function refreshStatus() {
  try {
    const out = await api("/api/status");
    const running = out.service_running === null ? "unknown" : (out.service_running ? "running" : "stopped");
    const last = out.last_activity ? new Date(out.last_activity * 1000).toLocaleString() : "never";
    $("dash-status").innerHTML =
      `Service: <b>${running}</b> — ${out.paused ? "<b>paused</b>" : "posting"} — last activity: ${last}`;
    $("btn-pause").disabled = out.paused;
    $("btn-resume").disabled = !out.paused;
  } catch (err) {
    $("dash-status").textContent = `Could not read status: ${err.message || err}`;
  }
}

async function refreshLogs() {
  try {
    const out = await api("/api/logs?lines=200");
    $("dash-logs").textContent = (out.lines || []).join("\n") || "(no log lines)";
  } catch (err) {
    $("dash-logs").textContent = `Could not read logs: ${err.message || err}`;
  }
}

async function pauseResume(path) {
  try { await api(path, {}); } catch (err) { setError("settings-error", String(err.message || err)); }
  await refreshStatus();
}

async function restartService() {
  $("btn-restart").disabled = true;
  try { await api("/api/restart", {}); $("btn-restart").disabled = false; await refreshStatus(); }
  catch (err) {
    setError("settings-error", String(err.message || err));
    $("btn-restart").disabled = false;
  }
}

const SETTINGS_FIELDS = [
  ["personality", "text"], ["environment_mode", "select:workstation,server"],
  ["discord_token_env", "text"], ["db_path", "text"], ["max_reply_chars", "number"],
  ["reply_delivery", "select:chunked,single_message"], ["opencode_binary", "text"],
  ["opencode_model", "text"], ["opencode_steps", "number"],
  ["opencode_timeout_seconds", "number"], ["write_mode", "select:read-only,docs-only"],
  ["max_session_turns", "number"], ["session_ttl_days", "number"],
  ["max_context_messages", "number"], ["max_context_age_days", "number"],
  ["reminder_channel_id", "number"], ["external_inputs", "select:disabled,read-only"],
  ["max_external_evidence_items", "number"], ["max_attachments", "number"],
  ["max_attachment_bytes", "number"], ["image_inputs", "select:off,on"],
];

async function loadSettings() {
  try {
    const out = await api("/api/settings");
    const cfg = out.config;
    const form = $("settings-form");
    form.innerHTML = "";
    for (const [key, kind] of SETTINGS_FIELDS) {
      const l = document.createElement("label");
      l.textContent = `${key} `;
      let input;
      if (kind.startsWith("select:")) {
        input = document.createElement("select");
        for (const choice of kind.slice(7).split(",")) {
          const o = document.createElement("option");
          o.value = choice; o.textContent = choice;
          input.append(o);
        }
        input.value = String(cfg[key]);
      } else {
        input = document.createElement("input");
        input.type = kind === "number" ? "number" : "text";
        input.value = kind === "number" ? Number(cfg[key]) : String(cfg[key]);
      }
      input.dataset.field = key;
      l.append(input);
      form.append(l);
    }
    renderDashWatches(cfg.watches || []);
  } catch (err) {
    setError("settings-error", String(err.message || err));
  }
}

function renderDashWatches(watches) {
  const host = $("dash-watches");
  host.innerHTML = "";
  watches.forEach((watch) => {
    const name = watch.guild_id ? `server ${watch.guild_id}` : `channel ${watch.channel_id}`;
    const box = document.createElement("div");
    box.className = "watch";
    box.innerHTML = `<b>${name}</b> cap ${watch.post_hourly_cap}/h — repos: ` +
      (watch.repos.length ? watch.repos.join(", ") : "none");
    const rm = document.createElement("button");
    rm.textContent = "remove watch";
    rm.onclick = () => {
      const cfg = collectSettings(watches.filter((_, i) => i !== watches.indexOf(watch)));
      saveSettings(cfg);
    };
    box.append(rm);
    host.append(box);
  });
}

function collectSettings(watches) {
  const cfg = {};
  document.querySelectorAll("#settings-form [data-field]").forEach((input) => {
    cfg[input.dataset.field] = input.type === "number" ? Number(input.value) : input.value;
  });
  cfg.write_dirs = Array.isArray(state.dashWriteDirs) ? state.dashWriteDirs : cfg.write_dirs || [];
  cfg.watches = watches;
  return cfg;
}

async function saveSettings(cfgOverride) {
  setError("settings-error", "");
  $("settings-ok").hidden = true;
  try {
    const current = cfgOverride || collectSettings(state.dashWatches || []);
    await api("/api/settings", { config: current });
    $("settings-ok").hidden = false;
    state.dashWatches = current.watches;
    state.dashWriteDirs = current.write_dirs;
    await loadSettings();
  } catch (err) {
    setError("settings-error", String(err.message || err));
  }
}

async function saveToken() {
  setError("settings-error", "");
  try {
    await api("/api/secrets/token", { token: $("new-token").value });
    $("new-token").value = "";
    $("settings-ok").textContent = "Token saved (mode 600). Use “Restart service” to apply.";
    $("settings-ok").hidden = false;
  } catch (err) {
    setError("settings-error", String(err.message || err));
  }
}

/* ---------------- boot ---------------- */

function openDashboard() { show("dashboard"); refreshStatus(); refreshLogs(); loadSettings(); }

async function boot() {
  const st = await api("/api/state");
  state.mode = st.mode;
  if (st.mode === "wizard") {
    show("welcome");
    await refreshOcStatus();
  } else {
    openDashboard();
  }
}

window.addEventListener("DOMContentLoaded", () => {
  $("btn-start").onclick = () => show("opencode");
  document.querySelectorAll(".back").forEach((b) => { b.onclick = () => show(b.dataset.goto); });
  $("btn-oc-install").onclick = installOc;
  $("btn-oc-login-start").onclick = startLogin;
  $("oc-model").onchange = (e) => { state.model = e.target.value; };
  $("btn-oc-next").onclick = async () => {
    try { await api("/api/opencode/login/stop", {}); } catch { /* already stopped */ }
    show("discord");
  };
  $("btn-discord-check").onclick = checkDiscord;
  $("btn-discord-next").onclick = () => { show("channels"); renderGuilds(); };
  $("btn-channels-next").onclick = () => { show("repos"); renderRepoPickers(); };
  $("btn-repos-scan").onclick = scanRepos;
  $("btn-repos-next").onclick = () => { show("behavior"); renderCaps(); };
  $("btn-behavior-next").onclick = () => { show("review"); renderReview(); };
  $("btn-save").onclick = saveSetup;
  $("btn-install-service").onclick = installService;
  $("btn-open-dashboard").onclick = openDashboard;
  $("btn-pause").onclick = () => pauseResume("/api/pause");
  $("btn-resume").onclick = () => pauseResume("/api/resume");
  $("btn-restart").onclick = restartService;
  $("btn-dash-logs").onclick = refreshLogs;
  $("btn-token").onclick = saveToken;
  $("settings-form").addEventListener("submit", (e) => { e.preventDefault(); saveSettings(); });
  boot().catch((err) => {
    document.body.insertAdjacentHTML("afterbegin", `<div class="error">${err.message || err}</div>`);
  });
});
