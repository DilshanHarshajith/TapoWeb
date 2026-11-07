const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const state = {
  status: null,
  hls: null,
  speed: 0.6,
  digitalZoom: 1,
  lightBusy: false,
  spotlightOn: false,
  talkActive: false,
  talkStarting: false,
  talkCancel: false,
  talkBuffer: new Uint8Array(0),
  talkQueue: [],
  talkSending: false,
  talkNodes: null,
  presets: [],
  recordingDates: [],
  recordings: [],
  recordingLoaded: false,
  recordingJob: null,
  recordingTimer: null,
  recordingStream: null,
  recordingHls: null,
  recordingPollToken: 0,
  activeRecording: null,
  liveStopped: false,
  view: "live",
  keysHeld: new Set(),
  activeMoves: new Set(),
  keyDebug: new URLSearchParams(window.location.search).has("keys"),
};

let bufferingTimer = null;

let videoRestartTimer = null;
let toastTimer = null;
let ptzQueue = Promise.resolve();

const KEY_VECTORS = {
  ArrowUp: [0, 1],
  ArrowDown: [0, -1],
  ArrowLeft: [-1, 0],
  ArrowRight: [1, 0],
};

// Resolve shortcuts from event.key first and fall back to the physical
// event.code so non-Latin layouts and remote keyboards still work.
function shortcutKey(event) {
  const key = event.key || "";
  const code = event.code || "";
  if (key === " " || key === "Spacebar" || code === "Space") return "space";
  if (key.length === 1 && /^[a-z0-9]$/i.test(key)) return key.toLowerCase();
  const digit = /^(?:Digit|Numpad)([0-9])$/.exec(code);
  if (digit) return digit[1];
  const letter = /^Key([A-Z])$/.exec(code);
  if (letter) return letter[1].toLowerCase();
  // Last resort for very old browsers or unusual input devices.
  const keyCode = event.keyCode || event.which || 0;
  if (keyCode === 32) return "space";
  if (keyCode >= 48 && keyCode <= 57) return String.fromCharCode(keyCode);
  if (keyCode >= 65 && keyCode <= 90) return String.fromCharCode(keyCode).toLowerCase();
  if (keyCode >= 96 && keyCode <= 105) return String(keyCode - 96);
  return "";
}

function arrowKey(event) {
  if (KEY_VECTORS[event.key]) return event.key;
  if (KEY_VECTORS[event.code]) return event.code;
  return "";
}

const ZOOM_MIN = 1;
const ZOOM_MAX = 4;
const ZOOM_STEP = 0.5;
const TALK_HOLD_DELAY = 300;

const talkKey = {
  timer: null,
  pressed: false,
  holding: false,
  startedByHold: false,
  released: true,
};

async function api(path, { method = "GET", body } = {}) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data = null;
  if ((response.headers.get("content-type") || "").includes("application/json")) {
    data = await response.json().catch(() => null);
  }
  if (!response.ok) {
    throw new Error((data && data.error) || `Request failed (${response.status})`);
  }
  return data;
}

function toast(message, kind = "info") {
  const element = $("#toast");
  element.textContent = message;
  element.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    element.className = "toast";
  }, 3500);
}

function showError(error) {
  toast(error && error.message ? error.message : String(error), "error");
}

function setVideoStatus(text, kind = "loading") {
  const overlay = $("#video-overlay");
  $("#video-status").textContent = text;
  overlay.hidden = kind === "hidden";
  overlay.classList.toggle("error", kind === "error");
}

function applyStatus(status) {
  state.status = status;
  const pill = $("#stream-pill");
  const pillText = $("#stream-text");
  const live = Boolean(status.authenticated && status.stream_running);
  pill.classList.toggle("live", live);
  if (live) {
    pillText.textContent = "live";
  } else if (status.authenticated) {
    pillText.textContent = "starting";
  } else {
    pillText.textContent = "offline";
  }

  if (status.authenticated && status.device && status.device.model) {
    const device = status.device;
    const name = [device.manufacturer, device.model].filter(Boolean).join(" ");
    const firmware = device.firmware ? ` · ${device.firmware}` : "";
    $("#device-line").textContent = `${name}${firmware} · ${status.host}`;
  } else if (status.authenticated) {
    $("#device-line").textContent = `${status.host}:${status.port}`;
  } else {
    $("#device-line").textContent = "Not connected";
  }
  updateLightAvailability(status);
  updateRecordingsAvailability(status);
  if (!status.authenticated && state.talkActive) {
    stopTalk({ silent: true });
  }
  updateTalkAvailability(status);
}

function applyDigitalZoom() {
  const video = $("#video");
  const value = state.digitalZoom;
  video.style.transform = value === 1 ? "" : `scale(${value})`;
  $("#zoom-value").textContent = `${value.toFixed(1)}x`;
  $("#zoom-out").disabled = value <= ZOOM_MIN;
  $("#zoom-in").disabled = value >= ZOOM_MAX;
}

function zoomBy(delta) {
  const next = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, state.digitalZoom + delta));
  if (next === state.digitalZoom) return;
  state.digitalZoom = next;
  applyDigitalZoom();
}

function lightAvailable(status = state.status) {
  return Boolean(status && status.authenticated && status.has_cloud_password);
}

function updateLightAvailability(status = state.status) {
  const available = lightAvailable(status);
  $$("#night-modes button, #spotlight-toggle, #light-refresh").forEach(
    (element) => {
      element.disabled = !available || state.lightBusy;
    }
  );
  const hint = $("#light-hint");
  if (!status || !status.authenticated) {
    hint.textContent = "Connect to the camera first.";
  } else if (!status.has_cloud_password) {
    hint.textContent =
      "Add your Tapo account password in Settings to control the spotlight and night mode.";
  } else {
    hint.textContent =
      "Spotlight and night mode use the Tapo local API. Click Refresh to read the current state.";
  }
  if (!available) {
    $$("#night-modes button").forEach((button) => button.classList.remove("active"));
  }
}

function markNightMode(mode) {
  $$("#night-modes button").forEach((button) => {
    button.classList.toggle("active", button.dataset.mode === mode);
  });
}

function markSpotlight(on) {
  state.spotlightOn = on === true;
  const button = $("#spotlight-toggle");
  button.classList.toggle("active", state.spotlightOn);
  button.setAttribute("aria-pressed", String(state.spotlightOn));
  button.textContent = state.spotlightOn ? "Spotlight on" : "Spotlight off";
}

function setLightBusy(busy) {
  state.lightBusy = busy;
  $$("#night-modes button, #spotlight-toggle, #light-refresh").forEach(
    (element) => {
      element.disabled = busy;
    }
  );
}

async function refreshLight() {
  if (!lightAvailable()) return;
  setLightBusy(true);
  try {
    const data = await api("/api/light");
    if (typeof data.on === "boolean") markSpotlight(data.on);
    if (data.mode) markNightMode(data.mode);
  } catch (error) {
    showError(error);
  } finally {
    state.lightBusy = false;
    updateLightAvailability();
  }
}

async function setSpotlight(on) {
  if (!lightAvailable()) {
    toast("Add your Tapo account password in Settings to use the spotlight.", "error");
    return;
  }
  if (state.lightBusy) {
    toast("Wait for the current light command to finish");
    return;
  }
  setLightBusy(true);
  try {
    await api("/api/light", { method: "POST", body: { on } });
    markSpotlight(on);
    toast(on ? "Spotlight on" : "Spotlight off");
  } catch (error) {
    showError(error);
  } finally {
    state.lightBusy = false;
    updateLightAvailability();
  }
}

async function setNightMode(mode) {
  if (!lightAvailable()) {
    toast("Add your Tapo account password in Settings to change night mode.", "error");
    return;
  }
  if (state.lightBusy) {
    toast("Wait for the current light command to finish");
    return;
  }
  setLightBusy(true);
  try {
    await api("/api/night", { method: "POST", body: { mode } });
    markNightMode(mode);
    const label = mode === "on" ? "Night" : mode === "off" ? "Day" : "Auto";
    toast(`Night mode: ${label}`);
  } catch (error) {
    showError(error);
  } finally {
    state.lightBusy = false;
    updateLightAvailability();
  }
}

function talkAvailable(status = state.status) {
  return Boolean(status && status.authenticated && status.has_cloud_password);
}

function updateTalkAvailability(status = state.status) {
  const button = $("#call-btn");
  if (!button) return;
  const available = talkAvailable(status);
  if (!state.talkActive) {
    button.disabled = !available;
    button.textContent = "Call";
    button.classList.remove("active");
    button.title = available
      ? "Talk through the camera (requires HTTPS or localhost) · Tap Space to toggle, hold Space to talk"
      : "Add your Tapo account password in Settings to call the camera";
  } else {
    button.disabled = false;
    button.textContent = "End call";
    button.classList.add("active");
    button.title = "End the call";
  }
}

function linearToAlaw(sample) {
  const pcm = Math.max(-32768, Math.min(32767, Math.round(sample * 32767)));
  let value = pcm >> 3;
  let mask;
  if (value >= 0) {
    mask = 0xd5;
  } else {
    mask = 0x55;
    value = -value - 1;
  }
  const segments = [0xff, 0x1ff, 0x3ff, 0x7ff, 0xfff, 0x1fff, 0x3fff, 0x7fff];
  let segment = 0;
  while (segment < 8 && value > segments[segment]) segment += 1;
  if (segment >= 8) return 0x7f ^ mask;
  let aval = segment << 4;
  if (segment < 2) {
    aval |= (value >> 1) & 0x0f;
  } else {
    aval |= (value >> segment) & 0x0f;
  }
  return (aval ^ mask) & 0xff;
}

class TalkDownsampler {
  constructor(inputRate, outputRate) {
    this.ratio = inputRate / outputRate;
    this.carry = new Float32Array(0);
    this.pos = 0;
  }
  process(input) {
    const data =
      this.carry.length > 0 ? new Float32Array(this.carry.length + input.length) : input;
    if (this.carry.length > 0) {
      data.set(this.carry, 0);
      data.set(input, this.carry.length);
    }
    const output = [];
    let pos = this.pos;
    while (pos < data.length - 1) {
      const i = Math.floor(pos);
      const frac = pos - i;
      const sample = data[i] + (data[i + 1] - data[i]) * frac;
      output.push(sample);
      pos += this.ratio;
    }
    const keepFrom = Math.max(0, Math.floor(pos));
    this.carry = data.slice(keepFrom);
    this.pos = pos - keepFrom;
    return output;
  }
}

function concatBytes(a, b) {
  const out = new Uint8Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}

async function postTalkAudio(bytes) {
  const response = await fetch("/api/talk/audio", {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: bytes,
  });
  let data = null;
  if ((response.headers.get("content-type") || "").includes("application/json")) {
    data = await response.json().catch(() => null);
  }
  if (!response.ok) {
    throw new Error((data && data.error) || `Request failed (${response.status})`);
  }
  return data;
}

function cleanupTalkAudio() {
  if (!state.talkNodes) return;
  const nodes = state.talkNodes;
  try {
    if (nodes.processor) nodes.processor.onaudioprocess = null;
    nodes.processor && nodes.processor.disconnect();
    nodes.source && nodes.source.disconnect();
    nodes.mute && nodes.mute.disconnect();
    nodes.stream.getTracks().forEach((track) => track.stop());
    nodes.ctx && nodes.ctx.close().catch(() => {});
  } catch (error) {}
  state.talkNodes = null;
}

function enqueueTalkAudio(bytes) {
  if (!state.talkActive) return;
  state.talkBuffer = concatBytes(state.talkBuffer, bytes);
  while (state.talkBuffer.length >= 800) {
    const chunk = state.talkBuffer.slice(0, 800);
    state.talkBuffer = state.talkBuffer.slice(800);
    state.talkQueue.push(chunk);
    if (state.talkQueue.length > 20) {
      state.talkQueue.splice(0, state.talkQueue.length - 20);
    }
  }
  pumpTalkAudio();
}

async function pumpTalkAudio() {
  if (state.talkSending) return;
  state.talkSending = true;
  while (state.talkActive && state.talkQueue.length > 0) {
    const chunk = state.talkQueue.shift();
    try {
      await postTalkAudio(chunk);
    } catch (error) {
      if (state.talkActive) {
        showError(error);
        await stopTalk({ silent: true });
      }
      break;
    }
  }
  state.talkSending = false;
}

async function startTalk(options = {}) {
  if (state.talkActive || state.talkStarting) return;
  if (!talkAvailable()) {
    openSetup();
    return;
  }
  if (!window.isSecureContext) {
    toast("Microphone access requires HTTPS or localhost", "error");
    return;
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    toast("This browser cannot access the microphone", "error");
    return;
  }
  const button = $("#call-btn");
  state.talkStarting = true;
  state.talkCancel = false;
  button.disabled = true;
  button.textContent = "Calling...";
  if (!options.silent) toast("Starting call...");
  let stream = null;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    await api("/api/talk/start", { method: "POST" });
    if (state.talkCancel) {
      state.talkCancel = false;
      stream.getTracks().forEach((track) => track.stop());
      try {
        await api("/api/talk/stop", { method: "POST" });
      } catch (inner) {}
      cleanupTalkAudio();
      updateTalkAvailability();
      return;
    }
    state.talkActive = true;
    state.talkBuffer = new Uint8Array(0);
    state.talkQueue = [];
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    const ctx = new AudioContext();
    await ctx.resume();
    const source = ctx.createMediaStreamSource(stream);
    const processor = ctx.createScriptProcessor(4096, 1, 1);
    const mute = ctx.createGain();
    mute.gain.value = 0;
    source.connect(processor);
    processor.connect(mute);
    mute.connect(ctx.destination);
    const downsampler = new TalkDownsampler(ctx.sampleRate, 8000);
    processor.onaudioprocess = (event) => {
      if (!state.talkActive) return;
      const input = event.inputBuffer.getChannelData(0);
      const resampled = downsampler.process(input);
      const alaw = new Uint8Array(resampled.length);
      for (let i = 0; i < resampled.length; i++) {
        alaw[i] = linearToAlaw(resampled[i]);
      }
      enqueueTalkAudio(alaw);
    };
    state.talkNodes = { ctx, source, processor, mute, stream };
    updateTalkAvailability();
    const video = $("#video");
    if (video.muted) {
      video.muted = false;
      $("#sound-btn").textContent = "Sound on";
      video.play().catch(() => {});
    }
    if (!options.silent) toast("Call started");
  } catch (error) {
    if (stream) stream.getTracks().forEach((track) => track.stop());
    try {
      await api("/api/talk/stop", { method: "POST" });
    } catch (inner) {}
    cleanupTalkAudio();
    state.talkActive = false;
    updateTalkAvailability();
    showError(error);
  } finally {
    state.talkStarting = false;
  }
}

async function stopTalk(options = {}) {
  if (state.talkStarting) state.talkCancel = true;
  const wasActive = state.talkActive;
  state.talkActive = false;
  cleanupTalkAudio();
  state.talkQueue = [];
  state.talkBuffer = new Uint8Array(0);
  updateTalkAvailability();
  if (!wasActive && !options.force) return;
  if (options.keepalive) {
    try {
      navigator.sendBeacon("/api/talk/stop");
    } catch (error) {}
    return;
  }
  try {
    await api("/api/talk/stop", { method: "POST" });
  } catch (error) {
    if (!options.silent) showError(error);
  }
  if (!options.silent) toast("Call ended");
}

function clearTalkKeyTimer() {
  clearTimeout(talkKey.timer);
  talkKey.timer = null;
}

async function startPushToTalk() {
  if (state.talkActive) return;
  talkKey.startedByHold = true;
  await startTalk({ silent: true });
  if (talkKey.released && talkKey.startedByHold) {
    talkKey.startedByHold = false;
    if (state.talkActive) stopTalk({ silent: true });
  }
}

function handleTalkKeyDown(event) {
  if (talkKey.pressed) return;
  event.preventDefault();
  talkKey.pressed = true;
  talkKey.holding = false;
  talkKey.startedByHold = false;
  talkKey.released = false;
  clearTalkKeyTimer();
  talkKey.timer = setTimeout(() => {
    talkKey.timer = null;
    talkKey.holding = true;
    if (!state.talkActive) startPushToTalk();
  }, TALK_HOLD_DELAY);
}

function handleTalkKeyUp(event) {
  if (!talkKey.pressed) return;
  event.preventDefault();
  clearTalkKeyTimer();
  const wasHolding = talkKey.holding;
  talkKey.pressed = false;
  talkKey.holding = false;
  talkKey.released = true;

  if (wasHolding) {
    // Push-to-talk: stop as soon as the key is released. If the call was
    // already active before the hold, leave it running.
    if (state.talkActive && talkKey.startedByHold) {
      talkKey.startedByHold = false;
      stopTalk();
    }
    return;
  }

  // A quick tap toggles the call on or off. A tap while the call is still
  // starting cancels it.
  if (state.talkActive || state.talkStarting) {
    stopTalk();
  } else {
    startTalk();
  }
}

function cancelTalkKeyHold() {
  clearTalkKeyTimer();
  const wasHolding = talkKey.holding;
  talkKey.pressed = false;
  talkKey.holding = false;
  talkKey.released = true;
  if (wasHolding && talkKey.startedByHold && state.talkActive) {
    talkKey.startedByHold = false;
    stopTalk({ silent: true });
  }
}

function recordingsAvailable(status = state.status) {
  return Boolean(status && status.authenticated && status.has_cloud_password);
}

function resetRecordingState() {
  clearTimeout(state.recordingTimer);
  state.recordingTimer = null;
  state.recordingJob = null;
  state.recordingLoaded = false;
  state.recordingDates = [];
  state.recordings = [];
  const select = $("#recordings-date");
  select.innerHTML = "";
  select.append(new Option("No recordings", ""));
  select.disabled = true;
  updateRecordingProgress(null);
  stopRecordingPlayback();
  $("#recordings-list").innerHTML =
    '<p class="hint">Choose a day to see its recordings.</p>';
}

function updateRecordingsAvailability(status = state.status) {
  const available = recordingsAvailable(status);
  $("#recordings-refresh").disabled = !available;
  if (!available) {
    resetRecordingState();
    $("#recordings-storage").textContent =
      status && status.authenticated
        ? "Add your Tapo account password in Settings to browse recordings."
        : "Connect to the camera first.";
  } else if (state.view === "recordings" && !state.recordingLoaded) {
    loadRecordingDates();
  }
}

async function switchView(view) {
  if (view !== "live" && view !== "recordings") return;
  if (state.view === view) return;
  state.view = view;
  $$(".view-tab").forEach((tab) => {
    const active = tab.dataset.view === view;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  });
  $("#live-view").hidden = view !== "live";
  $("#recordings-view").hidden = view !== "recordings";
  $("#sound-btn").hidden = view !== "live";
  $("#snapshot-btn").hidden = view !== "live";
  $("#call-btn").hidden = view !== "live";
  if (view !== "live") {
    cancelTalkKeyHold();
    if (state.talkActive) stopTalk({ silent: true });
  }

  if (view === "live") {
    await stopRecordingPlayback();
    if (state.view !== "live") return;
    if (state.status && state.status.authenticated) {
      await ensureLiveStream();
      if (state.view !== "live") return;
      startVideo();
    }
  } else {
    stopAll();
    state.keysHeld.clear();
    stopHls();
    $("#video").pause();
    if (recordingsAvailable() && !state.recordingLoaded) {
      loadRecordingDates();
    }
  }
}

async function ensureLiveStream() {
  if (!state.status || !state.status.authenticated) return;
  if (state.status.stream_running && !state.liveStopped) return;
  try {
    const status = await api("/api/stream/ensure", { method: "POST" });
    applyStatus(status);
    state.liveStopped = false;
  } catch (error) {
    showError(error);
  }
}

function formatRecordingDate(value) {
  const text = String(value || "");
  if (/^\d{8}$/.test(text)) {
    return `${text.slice(0, 4)}-${text.slice(4, 6)}-${text.slice(6, 8)}`;
  }
  return text;
}

function formatRecordingClock(timestamp) {
  const date = new Date(Number(timestamp) * 1000);
  if (Number.isNaN(date.getTime())) return "--:--";
  return date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function formatRecordingDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m ${secs}s`;
  return `${secs}s`;
}

async function loadRecordingDates() {
  if (!recordingsAvailable()) return;
  state.recordingLoaded = true;
  const button = $("#recordings-refresh");
  button.disabled = true;
  $("#recordings-storage").textContent = "Reading SD card...";
  $("#recordings-list").innerHTML =
    '<p class="hint">Looking for days with recordings...</p>';
  try {
    const data = await api("/api/recordings/dates");
    const storage = (data.storage || [])[0];
    $("#recordings-storage").textContent = storage
      ? `${storage.name}${storage.space ? " · " + storage.space : ""}`
      : "No SD card detected.";
    state.recordingDates = data.dates || [];
    const select = $("#recordings-date");
    const previous = select.value;
    select.innerHTML = "";
    if (!state.recordingDates.length) {
      select.append(new Option("No recordings", ""));
      select.disabled = true;
      state.recordings = [];
      $("#recordings-list").innerHTML =
        '<p class="hint">No recordings were found on the SD card.</p>';
      return;
    }
    for (const date of state.recordingDates) {
      select.append(new Option(formatRecordingDate(date), date));
    }
    select.disabled = false;
    select.value = state.recordingDates.includes(previous)
      ? previous
      : state.recordingDates[0];
    await loadRecordings();
  } catch (error) {
    $("#recordings-storage").textContent = "Could not read the SD card.";
    $("#recordings-list").innerHTML =
      '<p class="hint">Refresh to try loading recordings again.</p>';
    showError(error);
  } finally {
    button.disabled = !recordingsAvailable();
  }
}

async function loadRecordings() {
  const date = $("#recordings-date").value;
  if (!date) return;
  const list = $("#recordings-list");
  list.innerHTML = '<p class="hint">Loading recordings...</p>';
  try {
    const data = await api(`/api/recordings?date=${encodeURIComponent(date)}`);
    state.recordings = data.recordings || [];
    renderRecordings();
  } catch (error) {
    state.recordings = [];
    list.innerHTML =
      '<p class="hint">Could not load recordings for this day.</p>';
    showError(error);
  }
}

function renderRecordings() {
  const list = $("#recordings-list");
  list.innerHTML = "";
  if (!state.recordings.length) {
    list.innerHTML = '<p class="hint">No recordings for this day.</p>';
    return;
  }
  state.recordings.forEach((recording, index) => {
    const row = document.createElement("div");
    row.className = "recording-item";

    const info = document.createElement("div");
    info.className = "recording-info";
    const time = document.createElement("span");
    time.className = "recording-time";
    time.textContent = `${formatRecordingClock(
      recording.startTime
    )} – ${formatRecordingClock(recording.endTime)}`;
    const meta = document.createElement("span");
    meta.className = "recording-meta";
    const kind =
      recording.type && String(recording.type) !== "1"
        ? "Motion"
        : "Continuous";
    meta.textContent = `${formatRecordingDuration(
      recording.duration
    )} · ${kind}`;
    info.append(time, meta);

    const actions = document.createElement("div");
    actions.className = "recording-actions";
    for (const [action, label] of [
      ["play", "Play"],
      ["download", "Download"],
    ]) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "ghost small";
      button.dataset.action = action;
      button.dataset.index = String(index);
      button.textContent = label;
      actions.append(button);
    }

    row.append(info, actions);
    list.append(row);
  });
}

function setRecordingButtonsDisabled(disabled) {
  $$("#recordings-list button").forEach((button) => {
    button.disabled = disabled;
  });
}

function updateRecordingProgress(job) {
  const container = $("#recording-progress");
  const fill = $("#recording-progress-fill");
  const text = $("#recording-progress-text");
  if (!job) {
    container.hidden = true;
    fill.style.width = "0%";
    text.textContent = "";
    return;
  }
  container.hidden = false;
  const total = Number(job.total) || 0;
  const progress = Number(job.progress) || 0;
  const percent = total > 0 ? Math.min(100, (progress / total) * 100) : 0;
  fill.style.width = `${percent}%`;
  const suffix = percent >= 1 ? ` · ${Math.round(percent)}%` : "";
  text.textContent = `${job.action || "Downloading"}${suffix}`;
}

async function startRecordingJob(recording) {
  if (state.recordingJob) {
    toast("Wait for the current recording download to finish.");
    return;
  }
  setRecordingButtonsDisabled(true);
  updateRecordingProgress({ action: "Starting", progress: 0, total: 0 });
  try {
    const job = await api("/api/recordings/download", {
      method: "POST",
      body: { start: recording.startTime, end: recording.endTime },
    });
    state.recordingJob = {
      id: job.id,
      startTime: recording.startTime,
      endTime: recording.endTime,
    };
    updateRecordingProgress(job);
    pollRecordingJob();
  } catch (error) {
    setRecordingButtonsDisabled(false);
    updateRecordingProgress(null);
    showError(error);
  }
}

function pollRecordingJob() {
  clearTimeout(state.recordingTimer);
  const active = state.recordingJob;
  if (!active) return;
  state.recordingTimer = setTimeout(async () => {
    if (state.recordingJob !== active) return;
    try {
      const job = await api(`/api/recordings/download/${active.id}`);
      if (state.recordingJob !== active) return;
      updateRecordingProgress(job);
      if (job.state === "ready") {
        finishRecordingJob();
      } else if (job.state === "error") {
        failRecordingJob(job.error || "Recording download failed");
      } else {
        pollRecordingJob();
      }
    } catch (error) {
      if (state.recordingJob === active) failRecordingJob(error.message);
    }
  }, 800);
}

function finishRecordingJob() {
  const active = state.recordingJob;
  if (!active) return;
  state.recordingJob = null;
  clearTimeout(state.recordingTimer);
  state.recordingTimer = null;
  setRecordingButtonsDisabled(false);
  updateRecordingProgress(null);
  const url = `/api/recordings/file/${active.id}`;
  triggerDownload(`${url}?download=1`);
  toast("Recording downloaded");
}

function failRecordingJob(message) {
  state.recordingJob = null;
  clearTimeout(state.recordingTimer);
  state.recordingTimer = null;
  setRecordingButtonsDisabled(false);
  updateRecordingProgress(null);
  toast(message || "Recording download failed", "error");
}

function triggerDownload(url) {
  const link = document.createElement("a");
  link.href = url;
  link.download = "";
  document.body.append(link);
  link.click();
  link.remove();
}

function setRecordingStatus(text, kind = "loading") {
  const overlay = $("#recording-overlay");
  if (!overlay) return;
  $("#recording-status").textContent = text;
  overlay.hidden = kind === "hidden";
  overlay.classList.toggle("error", kind === "error");
}

function stopRecordingHls() {
  if (state.recordingHls) {
    try {
      state.recordingHls.destroy();
    } catch (error) {}
    state.recordingHls = null;
  }
}

function resetRecordingPlayer() {
  stopRecordingHls();
  state.recordingPollToken += 1;
  state.activeRecording = null;
  const video = $("#recording-video");
  video.pause();
  video.removeAttribute("src");
  video.load();
  $("#recording-placeholder").hidden = false;
  $("#recording-title").textContent = "No recording selected";
  $("#recording-subtitle").textContent =
    "Recordings stream directly from the camera's SD card.";
  $("#recording-download-btn").hidden = true;
  setRecordingStatus("", "hidden");
}

async function releaseRecordingStream() {
  const stream = state.recordingStream;
  state.recordingStream = null;
  if (!stream) return;
  try {
    await api(`/api/recordings/stream/${stream.id}`, { method: "DELETE" });
  } catch (error) {}
}

async function stopRecordingPlayback() {
  resetRecordingPlayer();
  await releaseRecordingStream();
}

function updateRecordingPlayerHeader(recording) {
  $("#recording-title").textContent = `${formatRecordingClock(
    recording.startTime
  )} – ${formatRecordingClock(recording.endTime)}`;
  $("#recording-subtitle").textContent = `${formatRecordingDuration(
    recording.endTime - recording.startTime
  )} clip · streaming from the SD card.`;
}

async function waitForRecordingStream(id, token) {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    if (token !== state.recordingPollToken) throw new Error("Playback cancelled");
    const data = await api(`/api/recordings/stream/${id}`);
    if (token !== state.recordingPollToken) throw new Error("Playback cancelled");
    if (data.state === "error") {
      throw new Error(data.error || "Could not start playback");
    }
    if (data.state === "stopped") throw new Error("Playback cancelled");
    if (data.ready) return data;
    if (data.state === "ended") {
      throw new Error("The camera did not send any recording data");
    }
    await new Promise((resolve) => setTimeout(resolve, 400));
  }
  throw new Error("Timed out starting recording playback");
}

function attachRecordingHls(id, token) {
  const video = $("#recording-video");
  const source = `/api/recordings/stream/${id}/index.m3u8`;
  stopRecordingHls();
  setRecordingStatus("Buffering...");
  if (window.Hls && window.Hls.isSupported()) {
    const hls = new window.Hls({
      lowLatencyMode: false,
      startPosition: 0,
      maxBufferLength: 30,
      backBufferLength: 30,
      enableWorker: true,
    });
    state.recordingHls = hls;
    hls.attachMedia(video);
    hls.on(window.Hls.Events.MEDIA_ATTACHED, () => {
      hls.loadSource(`${source}?t=${Date.now()}`);
    });
    hls.on(window.Hls.Events.MANIFEST_PARSED, () => {
      if (token !== state.recordingPollToken) return;
      setRecordingStatus("", "hidden");
      video.play().catch(() => {});
    });
    hls.on(window.Hls.Events.ERROR, (_event, data) => {
      if (!data.fatal) return;
      if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR) {
        try {
          hls.recoverMediaError();
          return;
        } catch (error) {}
      }
      setRecordingStatus("Recording playback stopped", "error");
    });
  } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = source;
    video.addEventListener(
      "playing",
      () => setRecordingStatus("", "hidden"),
      { once: true }
    );
    video.addEventListener(
      "error",
      () => setRecordingStatus("Recording playback stopped", "error"),
      { once: true }
    );
    video.play().catch(() => {});
  } else {
    setRecordingStatus("HLS playback is not supported in this browser", "error");
  }
}

async function startRecordingPlayback(recording) {
  if (!recording) return;
  if (state.recordingJob) {
    toast("Wait for the current recording download to finish.");
    return;
  }
  await stopRecordingPlayback();
  state.activeRecording = recording;
  state.liveStopped = true;
  const token = state.recordingPollToken;
  updateRecordingPlayerHeader(recording);
  $("#recording-placeholder").hidden = true;
  $("#recording-download-btn").hidden = false;
  setRecordingStatus("Starting playback...");
  setRecordingButtonsDisabled(true);
  try {
    const stream = await api("/api/recordings/stream", {
      method: "POST",
      body: { start: recording.startTime, end: recording.endTime },
    });
    if (token !== state.recordingPollToken) {
      try {
        await api(`/api/recordings/stream/${stream.id}`, { method: "DELETE" });
      } catch (error) {}
      if (state.view === "live") {
        state.liveStopped = true;
        await ensureLiveStream();
        startVideo();
      }
      return;
    }
    state.recordingStream = {
      id: stream.id,
      startTime: recording.startTime,
      endTime: recording.endTime,
    };
    await waitForRecordingStream(stream.id, token);
    if (token !== state.recordingPollToken) return;
    attachRecordingHls(stream.id, token);
  } catch (error) {
    if (token !== state.recordingPollToken) return;
    await releaseRecordingStream();
    resetRecordingPlayer();
    showError(error);
  } finally {
    setRecordingButtonsDisabled(false);
  }
}

async function downloadActiveRecording() {
  const recording = state.activeRecording;
  if (!recording) return;
  await stopRecordingPlayback();
  await startRecordingJob(recording);
}

function fillSetupForm(status) {
  const form = $("#setup-form");
  const authenticated = Boolean(status && status.authenticated);
  if (authenticated) {
    form.elements.host.value = status.host;
    form.elements.port.value = status.port;
    form.elements.user.value = status.username;
    form.elements.stream.value = status.stream || "stream1";
  }
  form.elements.password.value = "";
  form.elements.password.placeholder = authenticated
    ? "Leave blank to keep current"
    : "Camera account password";
  form.elements.cloud_password.value = "";
  form.elements.cloud_password.placeholder =
    authenticated && status.has_cloud_password
      ? "Leave blank to keep current"
      : "Optional · for spotlight, night mode and recordings";
  $("#disconnect-btn").hidden = !authenticated;
  $("#setup-error").hidden = true;
}

function openSetup() {
  cancelTalkKeyHold();
  state.keysHeld.clear();
  if (state.talkActive) stopTalk({ silent: true });
  stopAll();
  fillSetupForm(state.status);
  $("#setup-modal").hidden = false;
  setTimeout(() => {
    const form = $("#setup-form");
    if (!state.status || !state.status.authenticated) {
      form.elements.user.focus();
    }
  }, 40);
}

function closeSetup() {
  $("#setup-modal").hidden = true;
}

function stopHls() {
  if (state.hls) {
    try {
      state.hls.destroy();
    } catch (error) {}
    state.hls = null;
  }
  clearTimeout(videoRestartTimer);
  videoRestartTimer = null;
}

function scheduleVideoRestart(delay = 3000) {
  if (videoRestartTimer) return;
  videoRestartTimer = setTimeout(() => {
    videoRestartTimer = null;
    startVideo();
  }, delay);
}

function startVideo() {
  const video = $("#video");
  const source = "/hls/index.m3u8";
  stopHls();
  clearTimeout(bufferingTimer);
  bufferingTimer = null;
  setVideoStatus("Connecting to stream...");

  if (window.Hls && window.Hls.isSupported()) {
    // Keep a couple of segments of slack. The camera occasionally pauses for a
    // fraction of a second while it handles a control command; with a one
    // segment buffer that was enough to make the player show "Buffering...".
    const hls = new window.Hls({
      lowLatencyMode: false,
      liveSyncDurationCount: 2,
      liveMaxLatencyDurationCount: 5,
      backBufferLength: 15,
      maxBufferLength: 20,
      maxMaxBufferLength: 40,
      maxLiveSyncPlaybackRate: 1.5,
      enableWorker: true,
    });
    state.hls = hls;
    hls.attachMedia(video);
    hls.on(window.Hls.Events.MEDIA_ATTACHED, () => {
      hls.loadSource(`${source}?t=${Date.now()}`);
    });
    hls.on(window.Hls.Events.MANIFEST_PARSED, () => {
      video.play().catch(() => {});
    });
    hls.on(window.Hls.Events.ERROR, (_event, data) => {
      if (!data.fatal) return;
      if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR) {
        try {
          hls.recoverMediaError();
          return;
        } catch (error) {}
      }
      setVideoStatus("Reconnecting to stream...");
      scheduleVideoRestart(3000);
    });
  } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = source;
    video.play().catch(() => {});
    video.addEventListener("error", () => scheduleVideoRestart(3000), { once: true });
  } else {
    setVideoStatus("HLS playback is not supported in this browser", "error");
  }
}

function queuePtz(path, body) {
  const run = () => api(path, { method: "POST", body });
  const next = ptzQueue.then(run, run);
  ptzQueue = next.catch(() => {});
  return next;
}

function sendStop() {
  queuePtz("/api/ptz/stop").catch(() => {});
}

function stopAll() {
  for (const end of Array.from(state.activeMoves)) {
    end();
  }
}

function bindHold(element, pan, tilt, zoom) {
  let active = false;

  const end = () => {
    if (!active) return;
    active = false;
    state.activeMoves.delete(end);
    element.classList.remove("active");
    sendStop();
  };

  const begin = async (event) => {
    event.preventDefault();
    if (active) return;
    active = true;
    state.activeMoves.add(end);
    element.classList.add("active");
    try {
      await queuePtz("/api/ptz/move", { pan, tilt, zoom, speed: state.speed });
    } catch (error) {
      showError(error);
      end();
    }
  };

  element.addEventListener("pointerdown", begin);
  element.addEventListener("pointerup", end);
  element.addEventListener("pointercancel", end);
  element.addEventListener("pointerleave", () => {
    if (active) end();
  });
  element.addEventListener("contextmenu", (event) => event.preventDefault());
}

function keyButton(key) {
  const vector = KEY_VECTORS[key];
  if (!vector) return null;
  return [...$$(".dir")].find(
    (button) =>
      Number(button.dataset.pan) === vector[0] &&
      Number(button.dataset.tilt) === vector[1]
  );
}

function keyVector() {
  let pan = 0;
  let tilt = 0;
  for (const key of state.keysHeld) {
    const vector = KEY_VECTORS[key];
    if (!vector) continue;
    pan += vector[0];
    tilt += vector[1];
  }
  return {
    pan: Math.sign(pan),
    tilt: Math.sign(tilt),
  };
}

async function sendKeyMove() {
  const vector = keyVector();
  if (!vector.pan && !vector.tilt) {
    sendStop();
    return;
  }
  try {
    await queuePtz("/api/ptz/move", { ...vector, speed: state.speed });
  } catch (error) {
    showError(error);
  }
}

async function goHome() {
  stopAll();
  sendStop();
  try {
    await queuePtz("/api/ptz/home", { speed: state.speed });
    toast("Moving to home position");
  } catch (error) {
    showError(error);
  }
}

async function loadPresets() {
  if (!state.status || !state.status.authenticated) return;
  try {
    const presets = await api("/api/presets");
    state.presets = presets;
    const select = $("#preset-select");
    select.innerHTML = "";
    if (!presets.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "No presets saved";
      select.append(option);
      return;
    }
    for (const preset of presets) {
      const option = document.createElement("option");
      option.value = preset.token;
      option.textContent = preset.name;
      select.append(option);
    }
  } catch (error) {
    showError(error);
  }
}

async function goToPreset(token) {
  if (!token) return;
  try {
    await queuePtz("/api/ptz/preset/goto", { token, speed: state.speed });
    toast("Moving to preset");
  } catch (error) {
    showError(error);
  }
}

function takeSnapshot() {
  if (!state.status || !state.status.authenticated) {
    openSetup();
    return;
  }
  window.open(`/api/snapshot?t=${Date.now()}`, "_blank", "noopener");
}

function bindControls() {
  $$(".view-tab").forEach((tab) => {
    tab.addEventListener("click", () => switchView(tab.dataset.view));
  });

  $$(".dir").forEach((element) => {
    bindHold(
      element,
      Number(element.dataset.pan) || 0,
      Number(element.dataset.tilt) || 0,
      0
    );
  });

  $("#zoom-in").addEventListener("click", () => zoomBy(ZOOM_STEP));
  $("#zoom-out").addEventListener("click", () => zoomBy(-ZOOM_STEP));
  applyDigitalZoom();

  $("#spotlight-toggle").addEventListener("click", () =>
    setSpotlight(!state.spotlightOn)
  );
  $("#light-refresh").addEventListener("click", refreshLight);
  $$("#night-modes button").forEach((button) => {
    button.addEventListener("click", () => setNightMode(button.dataset.mode));
  });

  $("#stop-btn").addEventListener("click", () => {
    stopAll();
    sendStop();
  });

  $("#home-btn").addEventListener("click", goHome);

  $("#speed").addEventListener("input", (event) => {
    const percent = Number(event.target.value);
    state.speed = percent / 100;
    $("#speed-value").textContent = `${percent}%`;
  });
  $("#speed").addEventListener("change", (event) => event.target.blur());

  $("#settings-btn").addEventListener("click", openSetup);

  $("#call-btn").addEventListener("click", () => {
    if (state.talkActive) {
      stopTalk();
    } else {
      startTalk();
    }
  });

  $("#setup-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector('button[type="submit"]');
    const body = {
      host: form.elements.host.value.trim(),
      port: Number(form.elements.port.value),
      user: form.elements.user.value.trim(),
      password: form.elements.password.value,
      cloud_password: form.elements.cloud_password.value,
      stream: form.elements.stream.value,
    };
    button.disabled = true;
    button.textContent = "Connecting...";
    $("#setup-error").hidden = true;
    try {
      const status = await api("/api/session", { method: "POST", body });
      applyStatus(status);
      state.liveStopped = false;
      closeSetup();
      form.elements.password.value = "";
      form.elements.cloud_password.value = "";
      startVideo();
      loadPresets();
      resetRecordingState();
      if (state.view === "recordings" && status.has_cloud_password) {
        loadRecordingDates();
      }
      toast("Connected to camera");
    } catch (error) {
      const errorElement = $("#setup-error");
      errorElement.textContent = error.message;
      errorElement.hidden = false;
    } finally {
      button.disabled = false;
      button.textContent = "Connect";
    }
  });

  $("#disconnect-btn").addEventListener("click", async () => {
    if (!window.confirm("Disconnect from the camera and stop the stream?")) return;
    try {
      await stopTalk({ silent: true });
      const status = await api("/api/session", { method: "DELETE" });
      stopHls();
      const video = $("#video");
      video.removeAttribute("src");
      video.load();
      state.digitalZoom = 1;
      state.keysHeld.clear();
      applyDigitalZoom();
      state.status = status;
      applyStatus(status);
      await stopRecordingPlayback();
      openSetup();
      toast("Disconnected");
    } catch (error) {
      showError(error);
    }
  });

  $("#preset-refresh").addEventListener("click", loadPresets);
  $("#preset-select").addEventListener("change", (event) =>
    event.target.blur()
  );

  $("#recordings-refresh").addEventListener("click", loadRecordingDates);
  $("#recordings-date").addEventListener("change", loadRecordings);
  $("#recordings-list").addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const recording = state.recordings[Number(button.dataset.index)];
    if (!recording) return;
    if (button.dataset.action === "play") {
      await startRecordingPlayback(recording);
    } else {
      await stopRecordingPlayback();
      await startRecordingJob(recording);
    }
  });
  $("#recording-download-btn").addEventListener("click", downloadActiveRecording);
  $("#preset-goto").addEventListener("click", () =>
    goToPreset($("#preset-select").value)
  );

  $("#preset-save").addEventListener("click", async () => {
    const name = $("#preset-name").value.trim();
    try {
      await api("/api/ptz/preset/set", { method: "POST", body: { name } });
      $("#preset-name").value = "";
      $("#preset-name").blur();
      await loadPresets();
      toast("Preset saved");
    } catch (error) {
      showError(error);
    }
  });

  $("#preset-delete").addEventListener("click", async () => {
    const token = $("#preset-select").value;
    if (!token) return;
    if (!window.confirm("Delete this preset?")) return;
    try {
      await api("/api/ptz/preset/remove", { method: "POST", body: { token } });
      await loadPresets();
      toast("Preset deleted");
    } catch (error) {
      showError(error);
    }
  });

  $("#snapshot-btn").addEventListener("click", takeSnapshot);

  $("#sound-btn").addEventListener("click", () => {
    const video = $("#video");
    video.muted = !video.muted;
    $("#sound-btn").textContent = video.muted ? "Sound off" : "Sound on";
    if (!video.muted) video.play().catch(() => {});
  });

  $("#fullscreen-btn").addEventListener("click", () => {
    const shell =
      state.view === "recordings" ? $(".recording-stage") : $("#video-shell");
    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else if (shell.requestFullscreen) {
      shell.requestFullscreen().catch(() => {});
    }
  });

  $("#keys-btn").addEventListener("click", () => {
    state.keyDebug = true;
    $("#keys-modal").hidden = false;
  });
  $("#keys-close").addEventListener("click", () => {
    $("#keys-modal").hidden = true;
  });
  $("#keys-modal").addEventListener("click", (event) => {
    if (event.target === $("#keys-modal")) $("#keys-modal").hidden = true;
  });

  const video = $("#video");
  const clearBuffering = () => {
    clearTimeout(bufferingTimer);
    bufferingTimer = null;
    setVideoStatus("", "hidden");
  };
  // Only show the overlay for real stalls; a momentary gap should not flash
  // "Buffering..." every time the camera handles a command.
  const showBufferingSoon = () => {
    if (bufferingTimer) return;
    bufferingTimer = setTimeout(() => {
      bufferingTimer = null;
      if (video.readyState < 3) setVideoStatus("Buffering...");
    }, 700);
  };
  video.addEventListener("playing", clearBuffering);
  video.addEventListener("canplay", clearBuffering);
  video.addEventListener("waiting", showBufferingSoon);
  video.addEventListener("stalled", showBufferingSoon);
  // Live view has no native controls, so a stray pause (media key, system
  // sleep, browser quirk) looks exactly like a buffering stream. Resume it.
  video.addEventListener("pause", () => {
    if (
      state.view === "live" &&
      state.hls &&
      state.status &&
      state.status.authenticated
    ) {
      video.play().catch(() => {});
    }
  });
  video.addEventListener("dblclick", () => {
    state.digitalZoom = 1;
    applyDigitalZoom();
  });

  window.addEventListener("pointerup", stopAll);
  window.addEventListener("pointercancel", stopAll);

  window.addEventListener("pagehide", () => {
    if (state.talkActive) {
      state.talkActive = false;
      cleanupTalkAudio();
      try {
        navigator.sendBeacon("/api/talk/stop");
      } catch (error) {}
    }
  });
  window.addEventListener("blur", () => {
    cancelTalkKeyHold();
    state.keysHeld.clear();
    stopAll();
    Object.keys(KEY_VECTORS).forEach((key) => {
      const button = keyButton(key);
      if (button) button.classList.remove("active");
    });
  });

  // Diagnostic aid: click "Keys" in the top bar (or add ?keys to the URL) and
  // every key event is shown in that panel and logged by the server.
  window.addEventListener(
    "keydown",
    (event) => {
      if (!state.keyDebug) return;
      const info = {
        key: event.key,
        code: event.code,
        keyCode: event.keyCode,
        resolved: shortcutKey(event),
        target: describeNode(event.target),
        active: describeNode(document.activeElement),
        view: state.view,
        setupOpen: !$("#setup-modal").hidden,
        keysOpen: !$("#keys-modal").hidden,
        secure: window.isSecureContext,
        hasFocus: document.hasFocus(),
        repeat: event.repeat,
        ctrl: event.ctrlKey,
        alt: event.altKey,
        shift: event.shiftKey,
        meta: event.metaKey,
        trusted: event.isTrusted,
      };
      if (event.target instanceof HTMLInputElement && event.target.type === "password") {
        info.key = "[redacted]";
        info.code = "[redacted]";
      }
      const panel = $("#keys-last");
      if (panel) panel.textContent = JSON.stringify(info, null, 2);
      fetch("/api/__key?d=" + encodeURIComponent(JSON.stringify(info)), {
        keepalive: true,
      }).catch(() => {});
    },
    true
  );

  window.addEventListener("keydown", (event) => {
    if (!$("#setup-modal").hidden || state.view !== "live") return;
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const target = event.target;
    if (
      target instanceof Element &&
      target.closest(
        'input:not([type="range"]):not([type="button"]):not([type="submit"]), textarea, [contenteditable]:not([contenteditable="false"])'
      )
    ) {
      return;
    }
    const shortcut = shortcutKey(event);
    const onSelect = target instanceof Element && Boolean(target.closest("select"));
    // Let a focused select keep its own arrow/space/enter behaviour, but still
    // allow letter and number shortcuts to work from it.
    if (onSelect && (!shortcut || shortcut === "space")) return;
    if (event.repeat) return;
    if (
      event.key === "+" ||
      event.key === "=" ||
      event.code === "Equal" ||
      event.code === "NumpadAdd"
    ) {
      event.preventDefault();
      zoomBy(ZOOM_STEP);
      return;
    }
    if (
      event.key === "-" ||
      event.key === "_" ||
      event.code === "Minus" ||
      event.code === "NumpadSubtract"
    ) {
      event.preventDefault();
      zoomBy(-ZOOM_STEP);
      return;
    }
    if (shortcut === "space") {
      handleTalkKeyDown(event);
      return;
    }
    if (shortcut === "h") {
      event.preventDefault();
      goHome();
      return;
    }
    if (shortcut === "l") {
      event.preventDefault();
      if (!lightAvailable()) {
        toast(
          "Add your Tapo account password in Settings to use the spotlight.",
          "error"
        );
        return;
      }
      setSpotlight(!state.spotlightOn);
      return;
    }
    if (shortcut === "d" || shortcut === "n" || shortcut === "a") {
      event.preventDefault();
      if (!lightAvailable()) {
        toast(
          "Add your Tapo account password in Settings to change night mode.",
          "error"
        );
        return;
      }
      setNightMode(shortcut === "d" ? "off" : shortcut === "n" ? "on" : "auto");
      return;
    }
    if (shortcut === "s") {
      event.preventDefault();
      takeSnapshot();
      return;
    }
    if (/^\d$/.test(shortcut)) {
      const index = shortcut === "0" ? 9 : Number(shortcut) - 1;
      const preset = state.presets[index];
      event.preventDefault();
      if (!preset) {
        toast(`No preset ${shortcut === "0" ? 10 : shortcut} saved`, "error");
        return;
      }
      goToPreset(preset.token);
      return;
    }
    const arrow = arrowKey(event);
    if (arrow) {
      event.preventDefault();
      if (state.keysHeld.has(arrow)) return;
      state.keysHeld.add(arrow);
      const button = keyButton(arrow);
      if (button) button.classList.add("active");
      sendKeyMove();
    }
  }, true);

  window.addEventListener("keyup", (event) => {
    if (shortcutKey(event) === "space") {
      handleTalkKeyUp(event);
      return;
    }
    const arrow = arrowKey(event);
    if (arrow && state.keysHeld.has(arrow)) {
      state.keysHeld.delete(arrow);
      const button = keyButton(arrow);
      if (button) button.classList.remove("active");
      sendKeyMove();
    }
  }, true);
}

function describeNode(node) {
  if (!node) return null;
  if (node === document) return "#document";
  if (node === window) return "#window";
  if (!(node instanceof Element)) return String(node);
  const id = node.id ? `#${node.id}` : "";
  const classes =
    typeof node.className === "string" && node.className.trim()
      ? "." + node.className.trim().split(/\s+/).join(".")
      : "";
  return `${node.tagName.toLowerCase()}${id}${classes}`;
}

async function init() {
  bindControls();
  try {
    const status = await api("/api/status");
    applyStatus(status);
    if (status.authenticated) {
      state.liveStopped = false;
      startVideo();
      loadPresets();
    } else {
      openSetup();
    }
  } catch (error) {
    showError(error);
    openSetup();
  }

  setInterval(async () => {
    try {
      const status = await api("/api/status");
      applyStatus(status);
    } catch (error) {}
  }, 5000);
}

init();
