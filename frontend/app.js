/**
 * AiRTraffic Control frontend
 * Mic → PCM16 → Speechmatics Realtime WS (jwt from backend)
 * On final AddTranscript → POST /api/command
 */

const SAMPLE_RATE = 16000;

const els = {
  badge: document.getElementById("connBadge"),
  panel: document.getElementById("listenPanel"),
  label: document.getElementById("listenLabel"),
  partial: document.getElementById("partial"),
  final: document.getElementById("final"),
  btnListen: document.getElementById("btnListen"),
  btnStop: document.getElementById("btnStop"),
  btnRefresh: document.getElementById("btnRefresh"),
  workersBody: document.querySelector("#workersTable tbody"),
  auditList: document.getElementById("auditList"),
  textForm: document.getElementById("textForm"),
  textCmd: document.getElementById("textCmd"),
  cmdResult: document.getElementById("cmdResult"),
};

let mediaStream = null;
let audioCtx = null;
let processor = None;
let ws = None;
let listening = false;

function setBadge(text, cls) {
  els.badge.textContent = text;
  els.badge.className = `badge ${cls}`;
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

function startSpeechmaticsSession(jwt) {
  return new Promise((resolve, reject) => {
    const url = `wss://global.rt.speechmatics.com/v2?jwt=${encodeURIComponent(jwt)}`;
    ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";

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
            operating_point: "enhanced",
            enable_partials: true,
            max_delay: 1.5,
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
        els.label.textContent = "Listening…";
        els.panel.classList.add("active");
        resolve();
      } else if (msg.message === "AddPartialTranscript") {
        els.partial.textContent = msg.metadata?.transcript || extractTranscript(msg) || "";
      } else if (msg.message === "AddTranscript") {
        const text = (msg.metadata?.transcript || extractTranscript(msg) || "").trim();
        if (!text) return;
        els.partial.textContent = "";
        els.final.textContent = text;
        await handleFinalTranscript(text);
      } else if (msg.message === "Error") {
        setBadge("Error", "error");
        els.label.textContent = msg.reason || "Speechmatics error";
        reject(new Error(msg.reason || "Speechmatics error"));
      }
    };

    ws.onerror = () => reject(new Error("WebSocket error"));
    ws.onclose = () => {
      if (listening) stopListening(false);
    };
  });
}

function extractTranscript(msg) {
  if (!msg.results) return "";
  return msg.results
    .map((r) => (r.alternatives && r.alternatives[0] && r.alternatives[0].content) || "")
    .join(" ")
    .replace(/\s+([.,!?])/g, "$1")
    .trim();
}

async function handleFinalTranscript(text) {
  try {
    const res = await fetch("/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ transcript: text, source: "voice" }),
    });
    const data = await res.json();
    els.cmdResult.textContent = JSON.stringify(data, null, 2);
    await refreshAll();
  } catch (e) {
    els.cmdResult.textContent = String(e);
  }
}

async function startMic() {
  mediaStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
    },
  });
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  const source = audioCtx.createMediaStreamSource(mediaStream);
  processor = audioCtx.createScriptProcessor(4096, 1, 1);
  processor.onaudioprocess = (e) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const input = e.inputBuffer.getChannelData(0);
    const down = downsample(input, audioCtx.sampleRate, SAMPLE_RATE);
    const pcm = floatTo16BitPCM(down);
    ws.send(pcm);
  };
  source.connect(processor);
  processor.connect(audioCtx.destination);
}

async function startListening() {
  if (listening) return;
  listening = true;
  els.btnListen.disabled = true;
  els.btnStop.disabled = false;
  setBadge("Connecting…", "idle");
  els.label.textContent = "Minting Speechmatics JWT…";
  els.final.textContent = "";
  els.partial.textContent = "";
  try {
    const token = await fetchJwt();
    await startSpeechmaticsSession(token.jwt);
    await startMic();
  } catch (e) {
    setBadge("Error", "error");
    els.label.textContent = e.message || String(e);
    listening = false;
    els.btnListen.disabled = false;
    els.btnStop.disabled = true;
    els.panel.classList.remove("active");
  }
}

function stopListening(sendEnd = true) {
  listening = false;
  els.panel.classList.remove("active");
  els.label.textContent = "Ready to listen";
  setBadge("Idle", "idle");
  els.btnListen.disabled = false;
  els.btnStop.disabled = true;
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
        ws.send(JSON.stringify({ message: "EndOfStream" }));
      }
      ws.close();
      ws = null;
    }
  } catch (_) {
    /* ignore */
  }
}

async function refreshWorkers() {
  const res = await fetch("/api/workers");
  const data = await res.json();
  els.workersBody.innerHTML = "";
  for (const w of data.workers || []) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(w.name)}</td>
      <td><span class="status ${w.status}">${escapeHtml(w.status)}</span></td>
      <td><code>${w.pid ?? "—"}</code></td>
      <td>${escapeHtml(w.target || "—")}</td>`;
    els.workersBody.appendChild(tr);
  }
}

async function refreshAudit() {
  const res = await fetch("/api/audit?limit=40");
  const data = await res.json();
  els.auditList.innerHTML = "";
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

async function refreshAll() {
  await Promise.all([refreshWorkers(), refreshAudit()]);
}

els.btnListen.addEventListener("click", startListening);
els.btnStop.addEventListener("click", () => stopListening(true));
els.btnRefresh.addEventListener("click", refreshAll);

els.textForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const text = els.textCmd.value.trim();
  if (!text) return;
  const res = await fetch("/api/command/text", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, source: "text" }),
  });
  const data = await res.json();
  els.cmdResult.textContent = JSON.stringify(data, null, 2);
  els.final.textContent = text;
  els.textCmd.value = "";
  await refreshAll();
});

refreshAll();
setInterval(refreshAll, 3000);
