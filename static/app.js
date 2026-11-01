const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const state = {
  status: null,
  hls: null,
  speed: 0.6,
  digitalZoom: 1,
  lightBusy: false,
  presets: [],
  keysHeld: new Set(),
  activeMoves: new Set(),
};

let videoRestartTimer = null;
let toastTimer = null;
let ptzQueue = Promise.resolve();

const KEY_VECTORS = {
  ArrowUp: [0, 1],
  ArrowDown: [0, -1],
  ArrowLeft: [-1, 0],
  ArrowRight: [1, 0],
};

const ZOOM_MIN = 1;
const ZOOM_MAX = 4;
const ZOOM_STEP = 0.5;

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
  $$("#night-modes button, #spotlight-on, #spotlight-off, #light-refresh").forEach(
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
  $("#spotlight-on").classList.toggle("active", on === true);
  $("#spotlight-off").classList.toggle("active", on === false);
}

function setLightBusy(busy) {
  state.lightBusy = busy;
  $$("#night-modes button, #spotlight-on, #spotlight-off, #light-refresh").forEach(
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
  if (!lightAvailable()) return;
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
  if (!lightAvailable()) return;
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
      : "Optional · for spotlight and night mode";
  $("#disconnect-btn").hidden = !authenticated;
  $("#setup-error").hidden = true;
}

function openSetup() {
  state.keysHeld.clear();
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
  setVideoStatus("Connecting to stream...");

  if (window.Hls && window.Hls.isSupported()) {
    const hls = new window.Hls({
      lowLatencyMode: true,
      liveSyncDurationCount: 1,
      liveMaxLatencyDurationCount: 3,
      backBufferLength: 0,
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

function bindControls() {
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

  $("#spotlight-on").addEventListener("click", () => setSpotlight(true));
  $("#spotlight-off").addEventListener("click", () => setSpotlight(false));
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
      closeSetup();
      form.elements.password.value = "";
      form.elements.cloud_password.value = "";
      startVideo();
      loadPresets();
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
      openSetup();
      toast("Disconnected");
    } catch (error) {
      showError(error);
    }
  });

  $("#preset-refresh").addEventListener("click", loadPresets);

  $("#preset-goto").addEventListener("click", async () => {
    const token = $("#preset-select").value;
    if (!token) return;
    try {
      await queuePtz("/api/ptz/preset/goto", { token, speed: state.speed });
      toast("Moving to preset");
    } catch (error) {
      showError(error);
    }
  });

  $("#preset-save").addEventListener("click", async () => {
    const name = $("#preset-name").value.trim();
    try {
      await api("/api/ptz/preset/set", { method: "POST", body: { name } });
      $("#preset-name").value = "";
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

  $("#snapshot-btn").addEventListener("click", () => {
    if (!state.status || !state.status.authenticated) {
      openSetup();
      return;
    }
    window.open(`/api/snapshot?t=${Date.now()}`, "_blank", "noopener");
  });

  $("#sound-btn").addEventListener("click", () => {
    const video = $("#video");
    video.muted = !video.muted;
    $("#sound-btn").textContent = video.muted ? "Sound off" : "Sound on";
    if (!video.muted) video.play().catch(() => {});
  });

  $("#fullscreen-btn").addEventListener("click", () => {
    const shell = $("#video-shell");
    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else if (shell.requestFullscreen) {
      shell.requestFullscreen().catch(() => {});
    }
  });

  const video = $("#video");
  video.addEventListener("playing", () => setVideoStatus("", "hidden"));
  video.addEventListener("waiting", () => setVideoStatus("Buffering..."));
  video.addEventListener("stalled", () => setVideoStatus("Buffering..."));
  video.addEventListener("dblclick", () => {
    state.digitalZoom = 1;
    applyDigitalZoom();
  });

  window.addEventListener("pointerup", stopAll);
  window.addEventListener("pointercancel", stopAll);
  window.addEventListener("blur", () => {
    state.keysHeld.clear();
    stopAll();
    Object.keys(KEY_VECTORS).forEach((key) => {
      const button = keyButton(key);
      if (button) button.classList.remove("active");
    });
  });

  window.addEventListener("keydown", (event) => {
    if (!$("#setup-modal").hidden) return;
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const target = event.target;
    if (
      target instanceof Element &&
      target.closest('input:not([type="range"]), select, textarea')
    ) {
      return;
    }
    if (event.repeat) return;
    if (event.key === "+" || event.key === "=") {
      event.preventDefault();
      zoomBy(ZOOM_STEP);
      return;
    }
    if (event.key === "-" || event.key === "_") {
      event.preventDefault();
      zoomBy(-ZOOM_STEP);
      return;
    }
    if (event.key === " ") {
      event.preventDefault();
      stopAll();
      sendStop();
      return;
    }
    if (event.key === "h" || event.key === "H") {
      event.preventDefault();
      goHome();
      return;
    }
    if (KEY_VECTORS[event.key]) {
      event.preventDefault();
      if (state.keysHeld.has(event.key)) return;
      state.keysHeld.add(event.key);
      const button = keyButton(event.key);
      if (button) button.classList.add("active");
      sendKeyMove();
    }
  });

  window.addEventListener("keyup", (event) => {
    if (KEY_VECTORS[event.key] && state.keysHeld.has(event.key)) {
      state.keysHeld.delete(event.key);
      const button = keyButton(event.key);
      if (button) button.classList.remove("active");
      sendKeyMove();
    }
  });
}

async function init() {
  bindControls();
  try {
    const status = await api("/api/status");
    applyStatus(status);
    if (status.authenticated) {
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
