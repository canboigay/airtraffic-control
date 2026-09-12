/**
 * AiRTraffic Control frontend — front face
 */
const SAMPLE_RATE = 16000;
const DEMO_IDS = new Set(["log-spam", "fake-build", "fake-research"]);
const CONFIRM_PHRASE = "confirm kill";
const LS_DEMO_ONLY = "atcDemoOnly";

function $(id) { return document.getElementById(id); }

const els = {};
function bindEls() {
  els.badge = $("connBadge");
  els.panel = $("listenPanel");
  els.label = $("listenLabel");
  els.partial = $("partial");
  els.final = $("final");
  els.btnListen = $("btnListen");
  els.btnRefresh = $("btnRefresh");
  els.btnPauseAll = $("btnPauseAll");
  els.btnResumeAll = $("btnResumeAll");
  els.demoOnlyToggle = $("demoOnlyToggle");
  els.workersList = $("workersList");
  els.auditList = $("auditList");
  els.textForm = $("textForm");
  els.textCmd = $("textCmd");
  els.cmdResult = $("cmdResult");
  els.inspectDrawer = $("inspectDrawer");
  els.inspectTitle = $("inspectTitle");
  els.inspectSummary = $("inspectSummary");
  els.inspectLog = $("inspectLog");
}

let mediaStream = null;
let audioCtx = null;
let processor = null;
let ws = null;
let listening = false;
let listenToggleLock = false;

let towerAudio = null;
let towerAudioUrl = null;
let speaking = false;
let speakGen = 0;
let lastSpokenText = "";
let lastSpokenAt = 0;
let finalDebounceTimer = null;
let finalDebounceBuf = "";
let bargeHoldUntil = 0;
const USE_NEURAL_TTS = true;

const TOWER_HISTORY_MAX = 20;
const towerHistory = []; // session-only: {heard, spoken_reply, action, ts}

/** worker_id -> armed for kill confirm */
const armedKill = new Set();
/** worker_id showing inline redirect input */
let redirectOpenId = null;
let demoOnly = true;
let liveLogPollId = null;
const liveLogScroll = {}; // id -> stickToBottom
const expandedWorkerIds = new Set(JSON.parse(localStorage.getItem("atcExpandedWorkers") || "[]"));
let lastWorkers = [];

function loadDemoOnly() {
  try {
    const v = localStorage.getItem(LS_DEMO_ONLY);
    if (v === null) return true; // default ON for product face
    return v === "1" || v === "true";
  } catch (_) {
    return true;
  }
}

function saveDemoOnly(on) {
  try {
    localStorage.setItem(LS_DEMO_ONLY, on ? "1" : "0");
  } catch (_) {}
}

function isDemoWorker(w) {
  if (!w) return false;
  if (w.source === "demo") return true;
  return DEMO_IDS.has(w.id);
}

function setTowerReply(text) {
  const el = document.getElementById("towerReply");
  if (el) {
    el.textContent = text || "";
    el.title = text ? "Click to interrupt tower" : "";
  }
}

function stopTowerSpeech() {
  speaking = false;
  try {
    if (towerAudio) {
      towerAudio.onended = null;
      towerAudio.onerror = null;
      towerAudio.pause();
      towerAudio.removeAttribute("src");
      towerAudio.load();
      towerAudio = null;
    }
  } catch (_) {}
  try {
    if (towerAudioUrl) {
      URL.revokeObjectURL(towerAudioUrl);
      towerAudioUrl = null;
    }
  } catch (_) {}
  try {
    if (window.speechSynthesis) window.speechSynthesis.cancel();
  } catch (_) {}
}

function interruptTower(reason) {
  console.log("[ATC] barge-in", reason || "");
  ++speakGen;
  stopTowerSpeech();
}

function rmsEnergy(float32Array) {
  let sum = 0;
  const n = float32Array.length;
  for (let i = 0; i < n; i++) {
    const v = float32Array[i];
    sum += v * v;
  }
  return Math.sqrt(sum / Math.max(1, n));
}

async function speakTower(text, opts = {}) {
  if (!text) return;
  const now = Date.now();
  if (text === lastSpokenText && now - lastSpokenAt < 1800) return;
  lastSpokenText = text;
  lastSpokenAt = now;

  const gen = ++speakGen;
  setTowerReply(text);
  stopTowerSpeech();
  speaking = true;
  bargeHoldUntil = Date.now() + 350;

  // Always prefer neural (smooth). Browser TTS is fallback only if neural fails
  // or USE_NEURAL_TTS is off — never force robotic voice for fast_path.
  const forceBrowser = !!(opts && opts.browser) && !USE_NEURAL_TTS;
  if (!USE_NEURAL_TTS || forceBrowser) {
    speakBrowser(text, gen);
    return;
  }

  try {
    const res = await fetch("/api/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (gen !== speakGen) return;
    if (res.ok) {
      const blob = await res.blob();
      if (gen !== speakGen) return;
      const url = URL.createObjectURL(blob);
      towerAudioUrl = url;
      const audio = new Audio(url);
      audio.playbackRate = 1.0;
      towerAudio = audio;
      audio.onended = () => {
        if (gen !== speakGen) return;
        speaking = false;
        if (towerAudioUrl === url) {
          URL.revokeObjectURL(url);
          towerAudioUrl = null;
        }
        if (towerAudio === audio) towerAudio = null;
      };
      audio.onerror = () => {
        if (gen !== speakGen) return;
        speaking = false;
        speakBrowser(text, gen);
      };
      try {
        await audio.play();
        return;
      } catch (playErr) {
        speakBrowser(text, gen);
        return;
      }
    }
  } catch (e) {
    console.warn("[ATC] tts fetch failed", e);
  }
  if (gen !== speakGen) return;
  speakBrowser(text, gen);
}

function pickVoice() {
  const voices = window.speechSynthesis ? window.speechSynthesis.getVoices() : [];
  if (!voices.length) return null;
  const rank = (v) => {
    const n = v.name || "";
    const lang = v.lang || "";
    let s = 0;
    if (/^en(-|_|$)/i.test(lang)) s += 10;
    if (/en-US/i.test(lang)) s += 5;
    if (/Premium|Enhanced|Neural|Siri|Natural/i.test(n)) s += 40;
    if (/Ava|Zoe|Nora|Samantha|Karen|Moira|Tessa|Nicky|Aaron|Evan|Allison/i.test(n)) s += 25;
    if (/Google US English|Microsoft (Aria|Jenny|Guy|Davis|Jane)/i.test(n)) s += 18;
    if (/Compact|Eloquence|Fred|Junior|Pipe|Bad News|Good News|Zarvox|Trinoids/i.test(n)) s -= 50;
    return s;
  };
  return voices.slice().sort((a, b) => rank(b) - rank(a))[0] || null;
}

function speakBrowser(text, gen) {
  if (gen != null && gen !== speakGen) return;
  if (!window.speechSynthesis) {
    speaking = false;
    return;
  }
  try { window.speechSynthesis.cancel(); } catch (_) {}
  const u = new SpeechSynthesisUtterance(text);
  u.rate = 1.14;
  u.pitch = 1.0;
  u.volume = 1.0;
  const prefer = pickVoice();
  if (prefer) u.voice = prefer;
  u.onend = () => { if (gen == null || gen === speakGen) speaking = false; };
  u.onerror = () => { if (gen == null || gen === speakGen) speaking = false; };
  speaking = true;
  setTimeout(() => {
    if (gen != null && gen !== speakGen) return;
    try { window.speechSynthesis.speak(u); } catch (_) { speaking = false; }
  }, 20);
}

function setBadge(text, cls) {
  if (!els.badge) return;
  els.badge.textContent = text;
  els.badge.className = `pill ${cls}`;
}

function showErr(msg) {
  setBadge("Error", "error");
  if (els.label) els.label.textContent = msg;
  if (els.cmdResult) els.cmdResult.textContent = msg;
  console.error("[ATC]", msg);
}

function floatTo16BitPCM(float32Array) {
  const buf = new ArrayBuffer(float32Array.length * 2);
  const view = new DataView(buf);
  for (let i = 0; i < float32Array.length; i++) {
    let s = Math.max(-1, Math.min(1, float32Array[i]));
    view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return buf;
}

function downsample(buffer, inRate, outRate) {
  if (inRate === outRate) return buffer;
  const ratio = inRate / outRate;
  const newLen = Math.round(buffer.length / ratio);
  const result = new Float32Array(newLen);
  for (let i = 0; i < newLen; i++) {
    const start = Math.floor(i * ratio);
    const end = Math.min(Math.floor((i + 1) * ratio), buffer.length);
    let sum = 0;
    for (let j = start; j < end; j++) sum += buffer[j];
    result[i] = sum / (end - start || 1);
  }
  return result;
}

async function fetchJwt() {
  const res = await fetch("/api/speechmatics/token", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ttl_seconds: 3600 }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `token mint failed: ${res.status}`);
  }
  return res.json();
}

function extractTranscript(msg) {
  if (msg.metadata && msg.metadata.transcript) return msg.metadata.transcript;
  if (!msg.results) return "";
  return msg.results
    .map((r) => (r.alternatives && r.alternatives[0] && r.alternatives[0].content) || "")
    .join(" ")
    .replace(/\s+([.,!?])/g, "$1")
    .trim();
}

function pushTowerHistory(heard, data) {
  if (!heard && !(data && data.spoken_reply)) return;
  towerHistory.unshift({
    heard: heard || "",
    spoken_reply: (data && data.spoken_reply) || "",
    action: (data && data.action) || "chat",
    ts: Date.now(),
  });
  if (towerHistory.length > TOWER_HISTORY_MAX) towerHistory.length = TOWER_HISTORY_MAX;
  renderTowerHistory();
}

function renderTowerHistory() {
  const list = document.getElementById("towerHistory");
  const empty = document.getElementById("towerHistoryEmpty");
  if (!list) return;
  list.innerHTML = "";
  for (const entry of towerHistory) {
    const li = document.createElement("li");
    const t = new Date(entry.ts).toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    li.innerHTML = `
      <div class="th-heard">Heard: ${escapeHtml(entry.heard)}</div>
      <div class="th-spoken">${escapeHtml(entry.spoken_reply || "—")}</div>
      <div class="th-meta">${escapeHtml(entry.action)} · ${escapeHtml(t)}</div>`;
    list.appendChild(li);
  }
  if (empty) empty.hidden = towerHistory.length > 0;
}

function showCommandResult(data, heard) {
  if (!els.cmdResult) return;
  if (data && data.spoken_reply) {
    els.cmdResult.textContent = data.spoken_reply;
    return;
  }
  if (data && typeof data.detail === "string") {
    els.cmdResult.textContent = `Heard: "${heard}"
${data.detail}`;
    return;
  }
  if (data && (data.action === "unknown" || (data.parsed && data.parsed.action === "unknown"))) {
    els.cmdResult.textContent =
      `Heard: "${heard}"
Not matched. Try: status | pause log spam | kill build | confirm kill | resume research`;
    return;
  }
  if (data && data.ok === false && data.error) {
    els.cmdResult.textContent = `Heard: "${heard}"
${data.error}`;
    return;
  }
  els.cmdResult.textContent = JSON.stringify(data, null, 2);
}

let finalInFlight = false;
let lastFinalHeard = "";
let lastFinalAt = 0;

async function handleFinalTranscript(text) {
  text = (text || "").trim();
  if (!text) return;
  const now = Date.now();
  if (text === lastFinalHeard && now - lastFinalAt < 2000) return;
  if (finalInFlight) return;
  lastFinalHeard = text;
  lastFinalAt = now;
  finalInFlight = true;
  try {
    const res = await fetch("/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ transcript: text, source: "voice" }),
    });
    const data = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
    if (!res.ok && !data.spoken_reply) {
      data.detail = data.detail || data.error || `Command failed (${res.status})`;
    }
    showCommandResult(data, text);
    pushTowerHistory(text, data);
    if (els.final) els.final.textContent = text;
    if (data && data.spoken_reply) {
      speakTower(data.spoken_reply, { fastPath: !!data.fast_path });
    }
    await refreshAll();
  } catch (e) {
    if (els.cmdResult) els.cmdResult.textContent = String(e);
    showErr(e.message || String(e));
  } finally {
    finalInFlight = false;
  }
}

function startSpeechmaticsSession(jwt, wsUrl) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const done = (fn, arg) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      fn(arg);
    };

    const url = wsUrl || `wss://us.rt.speechmatics.com/v2?jwt=${encodeURIComponent(jwt)}`;
    ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";

    const timer = setTimeout(() => {
      done(reject, new Error("Speechmatics connection timed out"));
      try { ws && ws.close(); } catch (_) {}
    }, 12000);

    ws.onopen = () => {
      ws.send(
        JSON.stringify({
          message: "StartRecognition",
          audio_format: {
            type: "raw",
            encoding: "pcm_s16le",
            sample_rate: SAMPLE_RATE,
          },
          transcription_config: {
            language: "en",
            operating_point: "standard",
            enable_partials: true,
            max_delay: 0.8,
          },
        })
      );
    };

    ws.onmessage = async (ev) => {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (msg.message === "RecognitionStarted") {
        setBadge("Listening", "listening");
        if (els.label) els.label.textContent = "Listening…";
        const ch1 = $("kpiChannel");
        if (ch1) ch1.textContent = "Live";
        if (els.panel) els.panel.classList.add("active");
        done(resolve);
      } else if (msg.message === "AddPartialTranscript") {
        const p = extractTranscript(msg) || "";
        if (els.partial) els.partial.textContent = p;
        if (speaking && p.trim().length >= 3 && Date.now() > bargeHoldUntil) {
          interruptTower("partial");
        }
      } else if (msg.message === "AddTranscript") {
        const chunk = (extractTranscript(msg) || "").trim();
        if (!chunk) return;
        if (els.partial) els.partial.textContent = "";
        finalDebounceBuf = (finalDebounceBuf ? finalDebounceBuf + " " : "") + chunk;
        finalDebounceBuf = finalDebounceBuf.replace(/\s+/g, " ").trim();
        if (els.final) els.final.textContent = finalDebounceBuf;
        if (finalDebounceTimer) clearTimeout(finalDebounceTimer);
        const snapshot = finalDebounceBuf;
        finalDebounceTimer = setTimeout(() => {
          finalDebounceTimer = null;
          finalDebounceBuf = "";
          handleFinalTranscript(snapshot);
        }, 250);
      } else if (msg.message === "Error") {
        done(reject, new Error(msg.reason || "Speechmatics error"));
      }
    };

    ws.onerror = () => done(reject, new Error("WebSocket error"));
    ws.onclose = () => {
      if (!settled && listening) {
        done(reject, new Error("Speechmatics socket closed"));
      } else if (settled && listening) {
        stopListening(false);
      }
    };
  });
}

async function attachMic(stream) {
  mediaStream = stream;
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  if (audioCtx.state === "suspended") await audioCtx.resume();
  const source = audioCtx.createMediaStreamSource(mediaStream);
  processor = audioCtx.createScriptProcessor(4096, 1, 1);
  processor.onaudioprocess = (e) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const input = e.inputBuffer.getChannelData(0);
    if (speaking && Date.now() > bargeHoldUntil) {
      if (rmsEnergy(input) > 0.045) interruptTower("mic-energy");
    }
    const down = downsample(input, audioCtx.sampleRate, SAMPLE_RATE);
    ws.send(floatTo16BitPCM(down));
  };
  source.connect(processor);
  const mute = audioCtx.createGain();
  mute.gain.value = 0;
  processor.connect(mute);
  mute.connect(audioCtx.destination);
}

async function toggleListen(ev) {
  if (ev) {
    ev.preventDefault();
    ev.stopPropagation();
  }
  if (listenToggleLock) return;
  listenToggleLock = true;
  try {
    interruptTower("listen-toggle");
    if (listening) {
      stopListening(true);
      if (els.label) els.label.textContent = "Muted";
      setBadge("Muted", "idle");
      const ch = $("kpiChannel");
      if (ch) ch.textContent = "Standby";
      return;
    }
    await startListening(ev);
  } finally {
    listenToggleLock = false;
  }
}

async function startListening(ev) {
  if (ev) ev.preventDefault();
  if (listening) return;
  interruptTower("listen");
  try { if (window.speechSynthesis) { window.speechSynthesis.cancel(); window.speechSynthesis.getVoices(); } } catch (_) {}
  listening = true;
  if (els.btnListen) { els.btnListen.textContent = "Mute"; els.btnListen.classList.add("listening-btn"); els.btnListen.setAttribute("aria-pressed", "true"); }
  setBadge("Connecting…", "idle");
  const chc = $("kpiChannel");
  if (chc) chc.textContent = "Linking";
  if (els.label) els.label.textContent = "Requesting microphone…";
  if (els.final) els.final.textContent = "";
  if (els.partial) els.partial.textContent = "";

  let localStream;
  try {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error("Mic API unavailable — use http://127.0.0.1:8765 (not a file:// page)");
    }
    localStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
  } catch (e) {
    showErr(
      e && e.name === "NotAllowedError"
        ? "Mic blocked — allow microphone for localhost and retry"
        : e.message || String(e)
    );
    listening = false;
    if (els.btnListen) {
      els.btnListen.disabled = false;
      els.btnListen.textContent = "Listen";
      els.btnListen.classList.remove("listening-btn");
      els.btnListen.setAttribute("aria-pressed", "false");
    }
    return;
  }

  if (els.label) els.label.textContent = "Connecting Speechmatics…";
  try {
    const token = await fetchJwt();
    await startSpeechmaticsSession(token.jwt, token.ws_url);
    await attachMic(localStream);
  } catch (e) {
    showErr(e.message || String(e));
    listening = false;
    if (els.btnListen) {
      els.btnListen.disabled = false;
      els.btnListen.textContent = "Listen";
      els.btnListen.classList.remove("listening-btn");
      els.btnListen.setAttribute("aria-pressed", "false");
    }
    if (els.panel) els.panel.classList.remove("active");
    try { localStream.getTracks().forEach((tr) => tr.stop()); } catch (_) {}
  }
}

function stopListening(sendEnd = true) {
  if (finalDebounceTimer) { clearTimeout(finalDebounceTimer); finalDebounceTimer = null; }
  finalDebounceBuf = "";
  listening = false;
  if (els.panel) els.panel.classList.remove("active");
  if (els.label) els.label.textContent = "Ready";
  setBadge("Voice idle", "idle");
  const ch0 = $("kpiChannel");
  if (ch0) ch0.textContent = "Standby";
  if (els.btnListen) {
    els.btnListen.disabled = false;
    els.btnListen.textContent = "Listen";
    els.btnListen.classList.remove("listening-btn");
    els.btnListen.setAttribute("aria-pressed", "false");
  }
  try {
    if (processor) {
      processor.disconnect();
      processor.onaudioprocess = null;
      processor = null;
    }
    if (audioCtx) {
      audioCtx.close();
      audioCtx = null;
    }
    if (mediaStream) {
      mediaStream.getTracks().forEach((t) => t.stop());
      mediaStream = null;
    }
    if (ws) {
      if (sendEnd && ws.readyState === WebSocket.OPEN) {
        try { ws.send(JSON.stringify({ message: "EndOfStream" })); } catch (_) {}
      }
      ws.close();
      ws = null;
    }
  } catch (_) {}
}

function formatUptime(sec) {
  if (sec == null || Number.isNaN(sec)) return null;
  const s = Math.max(0, Math.floor(sec));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function visibleWorkers(workers) {
  if (!demoOnly) return workers;
  return workers.filter(isDemoWorker);
}

function workerMetaHtml(w) {
  const up = formatUptime(w.uptime_sec);
  const target = w.target || "—";
  const bits = [
    `<code>pid ${w.pid ?? "—"}</code>`,
    `<span>→ ${escapeHtml(String(target))}</span>`,
  ];
  if (up) bits.push(`<span>up ${escapeHtml(up)}</span>`);
  if (w.source) bits.push(`<span>${escapeHtml(w.source)}</span>`);
  if (w.protected) bits.push(`<span class="protected-badge" title="Read-only unless you name this id/PID">protected</span>`);
  if (w.session_hint) {
    bits.push(`<span class="session-hint" title="Terminal / session">${escapeHtml(w.session_hint)}</span>`);
  } else if (w.session_tty || w.session_app) {
    const parts = [w.session_tty, w.session_app].filter(Boolean);
    bits.push(`<span class="session-hint" title="Terminal / session">${escapeHtml(parts.join(" · "))}</span>`);
  }
  return bits.join("");
}

function workerActionsHtml(w) {
  const armed = armedKill.has(w.id);
  const paused = w.status === "paused";
  const killed = w.status === "killed";
  let actions = "";
  if (!killed) {
    if (paused) {
      actions += `<button type="button" class="btn sm" data-act="resume" data-id="${escapeHtml(w.id)}">Resume</button>`;
    } else {
      actions += `<button type="button" class="btn sm" data-act="pause" data-id="${escapeHtml(w.id)}">Pause</button>`;
    }
    actions += `<button type="button" class="btn sm" data-act="inspect" data-id="${escapeHtml(w.id)}">Inspect</button>`;
    if (armed) {
      actions += `<button type="button" class="btn sm confirm-kill" data-act="kill-confirm" data-id="${escapeHtml(w.id)}">Confirm Kill</button>`;
    } else {
      actions += `<button type="button" class="btn sm danger" data-act="kill" data-id="${escapeHtml(w.id)}">Kill</button>`;
    }
    actions += `<button type="button" class="btn sm" data-act="redirect-toggle" data-id="${escapeHtml(w.id)}">Redirect</button>`;
  } else {
    actions += `<button type="button" class="btn sm" data-act="inspect" data-id="${escapeHtml(w.id)}">Inspect</button>`;
    actions += `<button type="button" class="btn sm" data-act="restart" data-id="${escapeHtml(w.id)}">Restart</button>`;
  }
  return actions;
}

function actionsSig(w) {
  return [
    w.status,
    armedKill.has(w.id) ? "1" : "0",
    redirectOpenId === w.id ? "1" : "0",
  ].join("|");
}

function ensureWorkerLivePane(row, w) {
  let logWrap = row.querySelector(".worker-live");
  if (logWrap) return logWrap;
  logWrap = document.createElement("div");
  logWrap.className = "worker-live";
  logWrap.innerHTML = `
    <div class="worker-live-head">
      <span class="live-dot" aria-hidden="true"></span>
      <span>Live log</span>
      <code class="wid">${escapeHtml(w.id)}</code>
    </div>
    <pre class="worker-live-log" data-live-log="${escapeHtml(w.id)}">Connecting…</pre>`;
  row.appendChild(logWrap);
  // one-shot fill so expand isn't blank until the 1s poller
  queueMicrotask(() => { tickLiveLogs(); });
  return logWrap;
}

function syncRedirectInline(row, w) {
  const killed = w.status === "killed";
  const want = redirectOpenId === w.id && !killed;
  let inline = row.querySelector(".redirect-inline");
  if (!want) {
    if (inline) inline.remove();
    return;
  }
  if (inline) return; // keep focus / typed text
  inline = document.createElement("div");
  inline.className = "redirect-inline";
  inline.innerHTML = `
    <input type="text" placeholder="New target…" data-redirect-input="${escapeHtml(w.id)}" autocomplete="off" />
    <button type="button" class="btn sm primary" data-act="redirect-go" data-id="${escapeHtml(w.id)}">Go</button>
    <button type="button" class="btn sm" data-act="redirect-cancel" data-id="${escapeHtml(w.id)}">Cancel</button>`;
  const actions = row.querySelector(".worker-actions");
  if (actions && actions.nextSibling) row.insertBefore(inline, actions.nextSibling);
  else row.appendChild(inline);
}

function createWorkerRow(w) {
  const row = document.createElement("div");
  row.className = "worker-row";
  row.setAttribute("role", "listitem");
  row.dataset.id = w.id;
  row.innerHTML = `
    <div class="worker-main worker-main-toggle">
      <div class="worker-name-row">
        <span class="worker-chevron" aria-hidden="true">▸</span>
        <span class="worker-name">${escapeHtml(w.name || w.id)}</span>
        <span class="status ${escapeHtml(w.status)}">${escapeHtml(w.status)}</span>
        ${w.protected ? '<span class="status protected">protected</span>' : ''}
      </div>
      <div class="worker-meta">${workerMetaHtml(w)}</div>
    </div>
    <div></div>
    <div class="worker-actions">${workerActionsHtml(w)}</div>`;
  row.dataset.actionsSig = actionsSig(w);
  const main = row.querySelector(".worker-main");
  if (main) {
    main.addEventListener("click", (ev) => {
      ev.preventDefault();
      toggleWorkerExpanded(w.id);
    });
  }
  return row;
}

function updateWorkerRow(row, w) {
  const expanded = expandedWorkerIds.has(w.id);
  row.classList.toggle("expanded", expanded);

  const nameEl = row.querySelector(".worker-name");
  if (nameEl) nameEl.textContent = w.name || w.id;

  const statusEl = row.querySelector(".status:not(.protected)");
  if (statusEl) {
    statusEl.className = `status ${w.status}`;
    statusEl.textContent = w.status;
  }
  let protEl = row.querySelector(".status.protected");
  if (w.protected && !protEl) {
    const nameRow = row.querySelector(".worker-name-row");
    if (nameRow) {
      protEl = document.createElement("span");
      protEl.className = "status protected";
      protEl.textContent = "protected";
      nameRow.appendChild(protEl);
    }
  } else if (!w.protected && protEl) {
    protEl.remove();
  }

  const metaEl = row.querySelector(".worker-meta");
  if (metaEl) metaEl.innerHTML = workerMetaHtml(w);

  const sig = actionsSig(w);
  if (row.dataset.actionsSig !== sig) {
    const actionsEl = row.querySelector(".worker-actions");
    if (actionsEl) actionsEl.innerHTML = workerActionsHtml(w);
    row.dataset.actionsSig = sig;
  }

  const chev = row.querySelector(".worker-chevron");
  if (chev) chev.textContent = expanded ? "▾" : "▸";

  const main = row.querySelector(".worker-main");
  if (main) {
    main.title = expanded ? "Click to collapse live log" : "Click to expand live log";
  }

  syncRedirectInline(row, w);

  if (expanded) {
    ensureWorkerLivePane(row, w);
  } else {
    const live = row.querySelector(".worker-live");
    if (live) live.remove();
  }
}

function renderWorkers(workers) {
  if (!els.workersList) return;
  lastWorkers = workers || [];
  const list = visibleWorkers(lastWorkers);
  const wanted = new Set(list.map((w) => w.id));

  // Drop rows that left the visible set (do not wipe the whole list)
  for (const row of [...els.workersList.querySelectorAll(".worker-row")]) {
    if (!wanted.has(row.dataset.id)) row.remove();
  }

  let prev = null;
  for (const w of list) {
    let row = els.workersList.querySelector(`.worker-row[data-id="${CSS.escape(w.id)}"]`);
    if (!row) {
      row = createWorkerRow(w);
      if (prev && prev.nextSibling) {
        els.workersList.insertBefore(row, prev.nextSibling);
      } else if (!prev && els.workersList.firstChild) {
        els.workersList.insertBefore(row, els.workersList.firstChild);
      } else {
        els.workersList.appendChild(row);
      }
    } else if (prev && row.previousSibling !== prev) {
      // keep list order stable with data order
      if (prev.nextSibling) els.workersList.insertBefore(row, prev.nextSibling);
      else els.workersList.appendChild(row);
    }
    updateWorkerRow(row, w);
    prev = row;
  }

  const empty = $("workersEmpty");
  if (empty) empty.hidden = list.length > 0;

  // KPIs from visible set (product face)
  const count = (s) => list.filter((w) => w.status === s).length;
  const set = (id, v) => { const el = $(id); if (el) el.textContent = String(v); };
  set("kpiRunning", count("running") + count("redirected"));
  set("kpiPaused", count("paused"));
  set("kpiKilled", count("killed"));
  startLiveLogPolling();
  // Do NOT call tickLiveLogs() here — poller owns smooth updates; full re-fetch
  // on every render was racing the DOM remount and looking like open/close.
}

async function refreshWorkers() {
  const res = await fetch("/api/workers");
  const data = await res.json();
  renderWorkers(data.workers || []);
}

async function postJson(url, body) {
  const opts = { method: "POST", headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(url, opts);
  const data = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
  if (!res.ok) {
    const msg = typeof data.detail === "string" ? data.detail : data.error || `HTTP ${res.status}`;
    const err = new Error(msg);
    err.data = data;
    err.status = res.status;
    throw err;
  }
  return data;
}

function afterUiAction(label, data, opts = {}) {
  const spoken =
    (data && data.spoken_reply) ||
    (data && data.message) ||
    opts.spoken ||
    "";
  const action = opts.action || (data && data.action) || label;
  pushTowerHistory(opts.heard || `[UI] ${label}`, {
    spoken_reply: spoken || label,
    action,
  });
  if (spoken) speakTower(spoken, { fastPath: true });
  if (els.cmdResult) els.cmdResult.textContent = spoken || JSON.stringify(data, null, 2);
}

async function workerAction(act, id) {
  try {
    if (act === "pause") {
      const data = await postJson(`/api/workers/${encodeURIComponent(id)}/pause`);
      afterUiAction(`pause ${id}`, data, {
        spoken: `Paused ${data.after?.name || id}.`,
        action: "pause",
      });
    } else if (act === "resume") {
      const data = await postJson(`/api/workers/${encodeURIComponent(id)}/resume`);
      afterUiAction(`resume ${id}`, data, {
        spoken: `Resumed ${data.after?.name || id}.`,
        action: "resume",
      });
    } else if (act === "kill") {
      const data = await postJson(`/api/workers/${encodeURIComponent(id)}/kill`);
      armedKill.add(id);
      afterUiAction(`arm kill ${id}`, data, {
        spoken: data.message || `Armed kill on ${id}. Confirm to proceed.`,
        action: "kill.arm",
      });
    } else if (act === "kill-confirm") {
      const data = await postJson(`/api/workers/${encodeURIComponent(id)}/kill/confirm`, {
        worker_id: id,
        confirm: CONFIRM_PHRASE,
      });
      armedKill.delete(id);
      afterUiAction(`confirm kill ${id}`, data, {
        spoken: `Killed ${data.after?.name || id}.`,
        action: "kill",
      });
    } else if (act === "restart") {
      const data = await postJson(`/api/workers/${encodeURIComponent(id)}/restart`);
      armedKill.delete(id);
      afterUiAction(`restart ${id}`, data, {
        spoken: `Restarted ${data.after?.name || id}.`,
        action: "restart",
      });
    } else if (act === "inspect") {
      await openInspect(id);
      return;
    } else if (act === "redirect-toggle") {
      redirectOpenId = redirectOpenId === id ? null : id;
      renderWorkers(lastWorkers);
      return;
    } else if (act === "redirect-cancel") {
      redirectOpenId = null;
      renderWorkers(lastWorkers);
      return;
    } else if (act === "redirect-go") {
      const input = document.querySelector(`[data-redirect-input="${CSS.escape(id)}"]`);
      const target = (input && input.value || "").trim();
      if (!target) {
        if (els.cmdResult) els.cmdResult.textContent = "Enter a redirect target.";
        return;
      }
      const data = await postJson(`/api/workers/${encodeURIComponent(id)}/redirect`, { target });
      redirectOpenId = null;
      afterUiAction(`redirect ${id} → ${target}`, data, {
        spoken: `Redirected ${data.after?.name || id} to ${target}.`,
        action: "redirect",
      });
    }
    await refreshAll();
  } catch (e) {
    showErr(e.message || String(e));
    if (els.cmdResult) els.cmdResult.textContent = e.message || String(e);
    await refreshAll().catch(() => {});
  }
}


async function fetchWorkerLog(id) {
  const res = await fetch(`/api/workers/${encodeURIComponent(id)}/inspect?lines=40`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    return { text: data.detail || data.error || `inspect failed (${res.status})`, summary: "" };
  }
  const lines = data.lines || data.log_lines || data.tail || [];
  const arr = Array.isArray(lines) ? lines : String(lines).split("\n");
  const text = arr.length ? arr.join("\n") : (data.detail || data.summary || "(no log output yet)");
  return { text, summary: data.summary || data.spoken_reply || "", raw: data };
}

function paintLiveLog(id, text) {
  const pre = document.querySelector(`[data-live-log="${CSS.escape(id)}"]`);
  if (!pre) return;
  if (pre.textContent === text) return; // avoid scroll/layout thrash on unchanged polls
  const stick = liveLogScroll[id] !== false;
  const nearBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  pre.textContent = text;
  if (stick || nearBottom) {
    pre.scrollTop = pre.scrollHeight;
    liveLogScroll[id] = true;
  }
}

function wireLiveLogScrollHandlers() {
  document.querySelectorAll("[data-live-log]").forEach((pre) => {
    if (pre.dataset.scrollWired) return;
    pre.dataset.scrollWired = "1";
    pre.addEventListener("scroll", () => {
      const id = pre.getAttribute("data-live-log");
      const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
      liveLogScroll[id] = atBottom;
    });
  });
}

async function tickLiveLogs() {
  const nodes = document.querySelectorAll("[data-live-log]");
  if (!nodes.length) return;
  wireLiveLogScrollHandlers();
  await Promise.all(
    Array.from(nodes).map(async (pre) => {
      const id = pre.getAttribute("data-live-log");
      if (!id) return;
      try {
        const { text } = await fetchWorkerLog(id);
        paintLiveLog(id, text);
        // keep drawer in sync if open for this id
        if (els.inspectDrawer && !els.inspectDrawer.hidden && els.inspectTitle && (els.inspectTitle.textContent || "").includes(id)) {
          if (els.inspectLog) {
            const stick = els.inspectLog.scrollHeight - els.inspectLog.scrollTop - els.inspectLog.clientHeight < 40;
            els.inspectLog.textContent = text;
            if (stick) els.inspectLog.scrollTop = els.inspectLog.scrollHeight;
          }
        }
      } catch (e) {
        paintLiveLog(id, String(e.message || e));
      }
    })
  );
}

function persistExpandedWorkers() {
  try {
    localStorage.setItem("atcExpandedWorkers", JSON.stringify([...expandedWorkerIds]));
  } catch (_) {}
}

function toggleWorkerExpanded(id) {
  if (!id) return;
  if (expandedWorkerIds.has(id)) expandedWorkerIds.delete(id);
  else expandedWorkerIds.add(id);
  persistExpandedWorkers();
  renderWorkers(lastWorkers || []);
}

function startLiveLogPolling() {
  if (liveLogPollId) return;
  tickLiveLogs();
  liveLogPollId = setInterval(() => { tickLiveLogs(); }, 1000);
}

function stopLiveLogPolling() {
  if (liveLogPollId) {
    clearInterval(liveLogPollId);
    liveLogPollId = null;
  }
}

async function openInspect(id) {
  try {
    if (id && !expandedWorkerIds.has(id)) {
      expandedWorkerIds.add(id);
      persistExpandedWorkers();
      renderWorkers(lastWorkers || []);
    }
    const { text, summary, raw } = await fetchWorkerLog(id);
    if (els.inspectDrawer) els.inspectDrawer.hidden = false;
    if (els.inspectTitle) {
      const w = (lastWorkers || []).find((x) => x.id === id);
      els.inspectTitle.textContent = w ? `${w.name} · ${id}` : id;
      els.inspectTitle.dataset.workerId = id;
    }
    if (els.inspectSummary) els.inspectSummary.textContent = summary || "Live tail";
    if (els.inspectLog) {
      els.inspectLog.textContent = text;
      els.inspectLog.scrollTop = els.inspectLog.scrollHeight;
    }
    // scroll worker live pane into view
    const pre = document.querySelector(`[data-live-log="${CSS.escape(id)}"]`);
    if (pre) pre.closest(".worker-row")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    afterUiAction(`inspect ${id}`, raw || {}, {
      spoken: summary || `Tailing ${id}.`,
      action: "inspect",
    });
  } catch (e) {
    showErr(e.message || String(e));
  }
}

function closeInspect() {
  if (els.inspectDrawer) els.inspectDrawer.hidden = true;
}

async function fleetPauseAll() {
  try {
    const data = await postJson("/api/fleet/pause_all");
    const n = data.paused_count != null ? data.paused_count : "?";
    afterUiAction("pause all", data, {
      spoken: `Paused ${n} workers.`,
      action: "pause_all",
    });
    await refreshAll();
  } catch (e) {
    showErr(e.message || String(e));
  }
}

async function fleetResumeAll() {
  try {
    const data = await postJson("/api/fleet/resume_all");
    const n = data.resumed_count != null ? data.resumed_count : "?";
    afterUiAction("resume all", data, {
      spoken: `Resumed ${n} workers.`,
      action: "resume_all",
    });
    await refreshAll();
  } catch (e) {
    showErr(e.message || String(e));
  }
}

async function refreshAudit() {
  if (!els.auditList) return;
  const res = await fetch("/api/audit?limit=40");
  const data = await res.json();
  els.auditList.innerHTML = "";
  const auditEmpty = $("auditEmpty");
  for (const e of data.entries || []) {
    const li = document.createElement("li");
    const beforePid = e.before?.pid != null ? `pid ${e.before.pid}` : "";
    const afterPid = e.after?.pid != null ? `pid ${e.after.pid}` : e.after?.status || "";
    const receipt =
      e.before || e.after
        ? ` · ${beforePid}${beforePid && afterPid ? " → " : ""}${afterPid}`
        : "";
    li.innerHTML = `
      <div class="ts">${escapeHtml(formatTs(e.ts))}</div>
      <div><span class="act">${escapeHtml(e.action)}</span>
        ${e.worker_id ? " · " + escapeHtml(e.worker_id) : ""}
        ${escapeHtml(receipt)}</div>`;
    els.auditList.appendChild(li);
  }
  if (auditEmpty) auditEmpty.hidden = (data.entries || []).length > 0;
}

function formatTs(iso) {
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return iso;
  }
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function refreshMeta() {
  try {
    const res = await fetch("/api/health");
    const data = await res.json();
    const el = document.getElementById("adapterMeta");
    if (!el) return;
    const mode = data.adapter_mode || data.adapter || "local";
    el.textContent = `Local · ${mode}`;
  } catch (_) {}
}

async function refreshAll() {
  try {
    await Promise.all([refreshWorkers(), refreshAudit(), refreshMeta()]);
  } catch (e) {
    console.error("[ATC] refresh failed", e);
  }
}

async function submitText(ev) {
  if (ev) ev.preventDefault();
  const text = (els.textCmd && els.textCmd.value || "").trim();
  if (!text) return;
  try {
    const res = await fetch("/api/command/text", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, source: "text" }),
    });
    const data = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
    if (!res.ok && !data.spoken_reply) {
      data.detail = data.detail || data.error || `Command failed (${res.status})`;
    }
    showCommandResult(data, text);
    pushTowerHistory(text, data);
    if (els.final) els.final.textContent = text;
    if (data && data.spoken_reply) speakTower(data.spoken_reply, { fastPath: !!data.fast_path });
    if (els.textCmd) els.textCmd.value = "";
    await refreshAll();
  } catch (e) {
    showErr(e.message || String(e));
  }
}

function onWorkersClick(ev) {
  const btn = ev.target.closest("[data-act]");
  if (!btn || !els.workersList || !els.workersList.contains(btn)) return;
  const act = btn.getAttribute("data-act");
  const id = btn.getAttribute("data-id");
  if (!act || !id) return;
  ev.preventDefault();
  workerAction(act, id);
}

function wire() {
  bindEls();
  demoOnly = loadDemoOnly();
  if (els.demoOnlyToggle) {
    els.demoOnlyToggle.checked = demoOnly;
    els.demoOnlyToggle.addEventListener("change", () => {
      demoOnly = !!els.demoOnlyToggle.checked;
      saveDemoOnly(demoOnly);
      renderWorkers(lastWorkers);
    });
  }

  window.atcListen = toggleListen;
  window.atcToggleListen = toggleListen;
  window.atcStop = () => stopListening(true);
  window.atcInterrupt = () => interruptTower("manual");

  if (els.btnListen) els.btnListen.addEventListener("click", toggleListen);
  if (els.btnRefresh) els.btnRefresh.addEventListener("click", refreshAll);
  if (els.btnPauseAll) els.btnPauseAll.addEventListener("click", fleetPauseAll);
  if (els.btnResumeAll) els.btnResumeAll.addEventListener("click", fleetResumeAll);
  if (els.workersList) els.workersList.addEventListener("click", onWorkersClick);

  const btnCloseInspect = $("btnCloseInspect");
  if (btnCloseInspect) btnCloseInspect.addEventListener("click", closeInspect);

  const btnClearTower = document.getElementById("btnClearTower");
  if (btnClearTower) {
    btnClearTower.addEventListener("click", () => {
      towerHistory.length = 0;
      renderTowerHistory();
    });
  }
  renderTowerHistory();
  if (els.textForm) els.textForm.addEventListener("submit", submitText);
  const towerEl = document.getElementById("towerReply");
  if (towerEl) towerEl.addEventListener("click", () => interruptTower("click"));
  window.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") interruptTower("escape");
  });
  try {
    if (window.speechSynthesis) {
      window.speechSynthesis.getVoices();
      window.speechSynthesis.onvoiceschanged = () => { window.speechSynthesis.getVoices(); };
    }
  } catch (_) {}

  refreshAll();
  setInterval(refreshAll, 3000);
  console.info("[ATC] UI wired (frontface1)");
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", wire);
} else {
  wire();
}
