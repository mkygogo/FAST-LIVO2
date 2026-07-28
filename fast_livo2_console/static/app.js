const $ = (id) => document.getElementById(id);

let statusTimer = null;
let recordClockTimer = null;
let pointWs = null;
let cameraWs = null;
let cameraReconnectTimer = null;
let cameraFramePending = null;
let cameraFrameDrawing = false;
let cameraFrameGeneration = 0;
let cameraObjectUrl = null;
let recordCanvasContext = null;
let recordCanvasMetrics = null;
let cameraMetadataUpdatedAt = 0;
let cameraExposureMode = "Off";
let cameraExposureLast = null;
let cameraExposureStableFrames = 0;
let cameraConfigParams = {};
let scanState = "idle";
let recordState = "idle";
let recordElapsedBase = 0;
let recordElapsedSyncedAt = performance.now();
let workflowMode = "idle";
let pointStreamMode = "mapping";
let recordRadarPreviewEnabled = false;
let recordSensorRates = {};
let recordSensorHealth = {};
let activePageId = "fastlivo";
let cameraStreamProfile = "default";
let lastOfflineStatus = "";
let sceneMode = "live";
let qualityMode = localStorage.getItem("jr.preview.quality") || "mini";
let colorBoostEnabled = localStorage.getItem("jr.preview.colorBoost") !== "off";
if (localStorage.getItem("jr.preview.tiltCorrectionDefaultV2") !== "done") {
  localStorage.setItem("jr.preview.tiltCorrection", "off");
  localStorage.setItem("jr.preview.tiltCorrectionDefaultV2", "done");
}
let tiltCorrectionEnabled = localStorage.getItem("jr.preview.tiltCorrection") === "on";
let pointSizeScale = Number(localStorage.getItem("jr.preview.pointSize") || 1);

const tiltCorrectionDeg = -30;
const tiltCorrectionRad = tiltCorrectionDeg * Math.PI / 180;

let viewMode = "fps";
let followEnabled = true;
let pathPoints = [];
let currentPose = null;
let lastHeading = 0;
let lastPointStamp = 0;
let hasRgb = false;
let pointChunks = [];
let totalPoints = 0;
let rawPointTotal = 0;
let currentMapName = "";
let dataMaps = [];
let selectedDataMapId = null;
let dataMapsLoading = false;

let scene = null;
let camera = null;
let renderer = null;
let grid = null;
let pathLine = null;
let poseArrow = null;
let cameraTarget = null;
let orbitYaw = 0;
let orbitPitch = 0.72;
let orbitDistance = 12;
let fpsPosition = null;
let fpsYaw = 0;
let fpsPitch = 0;
let dragging = false;
let lastPointer = null;
let renderFrames = 0;
let renderFps = 0;
let fpsStarted = performance.now();
let lastRenderTime = performance.now();

const stickState = {
  move: { x: 0, y: 0, active: false, id: null, cx: 0, cy: 0 },
  look: { x: 0, y: 0, active: false, id: null, cx: 0, cy: 0 },
};

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  }[ch]));
}

function toast(message) {
  const box = $("toast");
  box.innerHTML = `<span class="toast-msg">${escapeHtml(message)}</span>`;
  setTimeout(() => {
    if (box.textContent === message) box.innerHTML = "";
  }, 4200);
}

function setText(id, text) {
  const el = $(id);
  if (el && el.textContent !== String(text)) el.textContent = text;
}

function setScanState(state, detail = "") {
  scanState = state;
  const labels = {
    idle: "未开始",
    starting: "启动中",
    scanning: "扫描中",
    saving: "保存中",
    complete: "已完成",
    error: "错误",
  };
  setText("scanStateText", detail || labels[state] || state);
  $("startScan").disabled = state === "starting" || state === "scanning" || state === "saving";
  $("finishScan").disabled = state === "idle" || state === "starting" || state === "saving";
  $("startScan").textContent = state === "complete" ? "重新实时建图" : "开始实时建图";
}

function setRecordState(state, detail = "") {
  recordState = state;
  const labels = {idle: "未开始", starting: "设备启动中", recording: "正在无损录制", stopping: "正在完成bag索引", valid: "录制完成，数据完整", invalid: "数据校验异常"};
  const text = detail || labels[state] || state;
  setText("recordStateText", text);
  setText("recordHudState", text);
  $("recordHealth")?.classList.toggle("recording", state === "recording");
  $("startRecord").disabled = state === "starting" || state === "recording" || state === "stopping" || workflowMode === "offline_mapping" || workflowMode === "realtime_mapping";
  $("stopRecord").disabled = state !== "recording";
  if ($("toggleRecordRadarPreview")) $("toggleRecordRadarPreview").disabled = state !== "recording";
}

function resetRecordSensorStatus() {
  recordSensorRates = {};
  recordSensorHealth = {};
  syncRecordElapsed(0);
  setText("recordCameraHz", "预览 - · 保存 -");
  setText("recordLidarHz", "-");
  setText("recordImuHz", "-");
  setText("recordCameraHealthValue", "等待");
  setText("recordLidarHealthValue", "等待");
  setText("recordImuHealthValue", "等待");
  setText("recordLidarDetail", "点数尚未收到");
  setText("recordImuDetail", "等待惯导消息");
  setText("recordSensorSummary", "正在确认三路数据");
  ["recordCameraHealth", "recordLidarHealth", "recordImuHealth"].forEach((id) => {
    const card = $(id);
    card?.classList.remove("bad");
    card?.classList.add("waiting");
  });
}

function updateRecordSensorStatus() {
  const specs = [
    ["recordCameraHealth", "recordCameraHealthValue", "/left_camera/image", 8],
    ["recordLidarHealth", "recordLidarHealthValue", "/livox/lidar", 5],
    ["recordImuHealth", "recordImuHealthValue", "/livox/imu", 100],
  ];
  let ready = 0;
  let bad = 0;
  for (const [cardId, valueId, topic, minimum] of specs) {
    const value = Number(recordSensorRates[topic]);
    const known = Number.isFinite(value);
    const healthy = known && value >= minimum;
    const card = $(cardId);
    card?.classList.toggle("waiting", !known);
    card?.classList.toggle("bad", known && !healthy);
    setText(valueId, healthy ? "正常" : known ? "异常" : "等待");
    if (healthy) ready++;
    else if (known) bad++;
  }
  const points = Number(recordSensorHealth.lidar_points);
  const lidarAge = Number(recordSensorHealth.lidar_age);
  const imuAge = Number(recordSensorHealth.imu_age);
  if (Number.isFinite(points) && points > 0) {
    setText("recordLidarDetail", `${formatCount(points)} 点/帧${Number.isFinite(lidarAge) ? ` · ${lidarAge.toFixed(2)} 秒前` : ""}`);
  }
  if (Number.isFinite(imuAge)) setText("recordImuDetail", `最后消息 ${imuAge.toFixed(2)} 秒前`);
  setText("recordSensorSummary", ready === 3 ? "三路数据正常" : bad ? "数据频率异常" : "正在确认三路数据");
}

function wsScheme() {
  return location.protocol === "https:" ? "wss" : "ws";
}

function pointBudget() {
  return qualityMode === "pc" ? 2800000 : 720000;
}

function pointBaseSize() {
  return sceneMode === "final" ? 0.025 : 0.035;
}

function pointRenderSize() {
  return pointBaseSize() * pointSizeScale;
}

function formatCount(n) {
  if (n >= 1000000) return `${(n / 1000000).toFixed(2)}M`;
  if (n >= 1000) return `${Math.round(n / 1000)}k`;
  return String(n || 0);
}

function fmtSize(bytes) {
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return hours > 0
    ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

function syncRecordElapsed(seconds) {
  recordElapsedBase = Math.max(0, Number(seconds) || 0);
  recordElapsedSyncedAt = performance.now();
  setText("recordElapsed", formatDuration(recordElapsedBase));
}

function updateRecordClock() {
  if (recordState !== "recording") return;
  const elapsed = recordElapsedBase + Math.max(0, performance.now() - recordElapsedSyncedAt) / 1000;
  setText("recordElapsed", formatDuration(elapsed));
}

function clampByte(value) {
  return Math.max(0, Math.min(255, Math.round(value)));
}

function displayColor(r, g, b) {
  const rr = Number.isFinite(r) ? r : 120;
  const gg = Number.isFinite(g) ? g : 220;
  const bb = Number.isFinite(b) ? b : 255;
  if (!colorBoostEnabled) return [rr, gg, bb];
  const luma = 0.299 * rr + 0.587 * gg + 0.114 * bb;
  const saturation = 4.0;
  const gain = 1.75;
  const lift = 8;
  return [
    clampByte((luma + (rr - luma) * saturation) * gain + lift),
    clampByte((luma + (gg - luma) * saturation) * gain + lift),
    clampByte((luma + (bb - luma) * saturation) * gain + lift),
  ];
}

function applyTiltCorrection(v) {
  if (!tiltCorrectionEnabled) return v;
  const ca = Math.cos(tiltCorrectionRad);
  const sa = Math.sin(tiltCorrectionRad);
  const x = v.x;
  const y = v.y;
  v.x = x * ca - y * sa;
  v.y = x * sa + y * ca;
  return v;
}

function rosToThree(p) {
  return applyTiltCorrection(new THREE.Vector3(p[0], p[2], -p[1]));
}

function headingToThreeDirection(yaw) {
  return applyTiltCorrection(new THREE.Vector3(Math.cos(yaw), 0, -Math.sin(yaw))).normalize();
}

function showTab(id) {
  activePageId = id;
  document.body.classList.toggle("record-mode", id === "record");
  document.querySelectorAll(".tab").forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === id));
  document.querySelectorAll(".page").forEach((page) => page.classList.toggle("active", page.id === id));
  if (id === "fastlivo" || (id === "record" && recordRadarPreviewEnabled)) {
    attachThreeRenderer(id === "record" ? "recordThreeViewport" : "threeViewport");
    setTimeout(resizeThree, 60);
  }
  if (id === "record") setTimeout(resizeRecordCanvas, 0);
  if (id === "data") refreshDataMaps();
  if (id === "camera") {
    refreshCameraConfig();
    ensureCameraStream();
  }
}

function attachThreeRenderer(targetId) {
  const target = $(targetId);
  if (renderer?.domElement && target && renderer.domElement.parentElement !== target) target.appendChild(renderer.domElement);
}

function toggleCameraSettings() {
  const sheet = $("cameraSettingsSheet");
  const button = $("cameraSettingsToggle");
  if (!sheet || !button) return;
  sheet.hidden = !sheet.hidden;
  button.setAttribute("aria-expanded", String(!sheet.hidden));
  button.textContent = sheet.hidden ? "更多参数⌄" : "收起参数⌃";
}

async function toggleCameraFullscreen() {
  const shell = document.querySelector(".camera-debug-shell");
  if (!shell) return;
  try {
    if (document.fullscreenElement || document.webkitFullscreenElement) {
      if (document.exitFullscreen) await document.exitFullscreen();
      else if (document.webkitExitFullscreen) document.webkitExitFullscreen();
      return;
    }
    if (shell.requestFullscreen) await shell.requestFullscreen();
    else if (shell.webkitRequestFullscreen) shell.webkitRequestFullscreen();
    else toast("当前浏览器不支持全屏");
  } catch (err) {
    toast(`全屏失败: ${err.message}`);
  }
}

function syncCameraControlOutputs() {
  const exp = $("camExposure");
  const fr = $("camFrameRate");
  const gamma = $("camGamma");
  const sat = $("camSaturation");
  if (exp && $("camExposureValue")) $("camExposureValue").textContent = String(exp.value);
  if (fr && $("camFrameRateValue")) $("camFrameRateValue").textContent = String(fr.value);
  if (gamma && $("camGammaValue")) $("camGammaValue").textContent = Number(gamma.value).toFixed(2);
  if (sat && $("camSaturationValue")) $("camSaturationValue").textContent = String(sat.value);
}

function setCameraConfigDirty(dirty) {
  const hint = $("cameraPendingHint");
  const actions = document.querySelector(".camera-settings-actions");
  const apply = $("applyCameraConfig");
  hint?.classList.toggle("pending", dirty);
  actions?.classList.toggle("pending", dirty);
  if (hint) {
    hint.textContent = dirty
      ? "参数尚未生效：需要点击“应用并重启相机”"
      : "当前显示的是相机已生效参数";
  }
  if (apply) apply.textContent = dirty ? "应用并重启（有修改）" : "应用并重启相机";
}

function readCameraFormParams() {
  return {
    width: Number(cameraConfigParams.width || 2448),
    height: Number(cameraConfigParams.height || 2048),
    Offset_x: Number(cameraConfigParams.Offset_x || 0),
    Offset_y: Number(cameraConfigParams.Offset_y || 0),
    ExposureTime: Number($("camExposure")?.value || 6000),
    ExposureAutoString: cameraExposureMode,
    AutoExposureTimeLowerLimit: 100,
    AutoExposureTimeUpperLimit: Number($("camAutoExposureMax")?.value || 10) * 1000,
    AutoExposureAOIUsageIntensity: true,
    AutoExposureAOIWidth: Number(cameraConfigParams.AutoExposureAOIWidth || 1840),
    AutoExposureAOIHeight: Number(cameraConfigParams.AutoExposureAOIHeight || 1536),
    AutoExposureAOIOffsetX: Number(cameraConfigParams.AutoExposureAOIOffsetX || 304),
    AutoExposureAOIOffsetY: Number(cameraConfigParams.AutoExposureAOIOffsetY || 256),
    GainAuto: cameraExposureMode === "Off" ? Number($("camGainAuto")?.value || 0) : 0,
    FrameRate: Number($("camFrameRate")?.value || 10),
    FrameRateEnable: Boolean($("camFrameRateEnable")?.checked),
    Gamma: Number($("camGamma")?.value || 0.7),
    GammaEnable: Boolean($("camGammaEnable")?.checked),
    Saturation: Number($("camSaturation")?.value || 128),
    SaturationEnable: Boolean($("camSaturationEnable")?.checked),
    TriggerModeString: "Off",
  };
}

function fillCameraForm(params = {}) {
  cameraConfigParams = {...cameraConfigParams, ...params};
  const setVal = (id, value) => {
    const el = $(id);
    if (!el || value == null) return;
    if (el.type === "checkbox") el.checked = Boolean(value);
    else el.value = value;
  };
  setVal("camExposure", params.ExposureTime);
  if (params.AutoExposureTimeUpperLimit != null) {
    const autoLimit = $("camAutoExposureMax");
    const autoLimitUs = Number(params.AutoExposureTimeUpperLimit);
    // Config is microseconds; UI options are whole milliseconds (10/20/30/40/50).
    const autoLimitMs = Number.isFinite(autoLimitUs) ? Math.round(autoLimitUs / 1000) : NaN;
    if (autoLimit && Number.isFinite(autoLimitMs) && autoLimitMs > 0) {
      autoLimit.querySelectorAll("[data-current-value]").forEach((option) => option.remove());
      const value = String(autoLimitMs);
      const supported = Array.from(autoLimit.options).some((option) => option.value === value);
      if (!supported) {
        const current = document.createElement("option");
        current.value = value;
        current.textContent = `当前 ${value} ms（请选择新档位）`;
        current.dataset.currentValue = "true";
        autoLimit.prepend(current);
      }
      autoLimit.value = value;
      // If assignment still failed, force-select the matching option.
      if (autoLimit.value !== value) {
        const match = Array.from(autoLimit.options).find((option) => option.value === value);
        if (match) match.selected = true;
      }
    }
  }
  setVal("camGainAuto", params.GainAuto);
  setVal("camFrameRate", params.FrameRate);
  setVal("camFrameRateEnable", params.FrameRateEnable);
  setVal("camGamma", params.Gamma);
  setVal("camGammaEnable", params.GammaEnable);
  setVal("camSaturation", params.Saturation);
  setVal("camSaturationEnable", params.SaturationEnable);
  const nextMode = params.ExposureAutoString || "Off";
  setCameraExposureMode(nextMode, nextMode !== cameraExposureMode);
  syncCameraControlOutputs();
}

function setCameraExposureMode(mode, resetSamples = true) {
  cameraExposureMode = ["Off", "Once", "Continuous"].includes(mode) ? mode : "Off";
  if (resetSamples) {
    cameraExposureLast = null;
    cameraExposureStableFrames = 0;
  }
  document.querySelectorAll("[data-exposure-mode]").forEach((button) => {
    const active = button.dataset.exposureMode === cameraExposureMode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
  document.querySelectorAll(".camera-manual-exposure input, .camera-manual-exposure select").forEach((control) => {
    control.disabled = cameraExposureMode !== "Off";
  });
  document.querySelectorAll(".camera-manual-exposure").forEach((control) => {
    control.classList.toggle("disabled", cameraExposureMode !== "Off");
  });
  const labels = {Off: "手动", Once: "自动一次 · 调节中", Continuous: "连续自动"};
  setText("cameraExposureStatus", `${labels[cameraExposureMode]} · 等待曝光数据`);
}

function updateCameraExposure(exposureUs) {
  const value = Number(exposureUs);
  if (!Number.isFinite(value) || value <= 0) return;
  if (cameraExposureMode === "Once") {
    if (cameraExposureLast == null) cameraExposureStableFrames = 1;
    else {
      const change = Math.abs(value - cameraExposureLast) / Math.max(Math.abs(cameraExposureLast), 1);
      cameraExposureStableFrames = change < 0.01 ? cameraExposureStableFrames + 1 : 1;
    }
  }
  cameraExposureLast = value;
  const formatted = value >= 1000 ? `${(value / 1000).toFixed(2)} ms` : `${value.toFixed(0)} µs`;
  if (cameraExposureMode === "Once") {
    setText("cameraExposureStatus", `自动一次 · ${cameraExposureStableFrames >= 5 ? "已锁定" : "调节中"} · ${formatted}`);
  } else if (cameraExposureMode === "Continuous") {
    setText("cameraExposureStatus", `连续自动 · ${formatted}`);
  } else {
    setText("cameraExposureStatus", `手动 · ${formatted}`);
  }
}

async function applyCameraExposureMode(mode) {
  if (!["Off", "Once", "Continuous"].includes(mode)) return;
  setCameraExposureMode(mode);
  const params = readCameraFormParams();
  params.ExposureAutoString = mode;
  if (mode !== "Off") params.GainAuto = 0;
  toast("曝光方式切换中，相机会重启约 2 秒…");
  try {
    closeCameraStream();
    const res = await fetch("/api/camera/config", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({params, restart: true}),
    });
    const data = await res.json();
    if (!res.ok || data.ok === false) throw new Error(data.output || data.message || `HTTP ${res.status}`);
    fillCameraForm(data.params || params);
    setText("cameraRunState", "重启中/运行中");
    setText("cameraDebugMeta", "相机重启中，准备重连预览…");
    setTimeout(() => ensureCameraStream(cameraStreamProfile || "default", true), 1500);
  } catch (err) {
    toast(`曝光方式切换失败: ${err.message}`);
    await refreshCameraConfig();
    if (cameraStreamShouldReconnect()) {
      setTimeout(() => ensureCameraStream(cameraStreamProfile || "default", true), 1500);
    }
  }
}

async function refreshCameraConfig() {
  try {
    const res = await fetch("/api/camera/config", { cache: "no-store" });
    const data = await res.json();
    if (!res.ok || data.ok === false) throw new Error(data.output || data.message || `HTTP ${res.status}`);
    fillCameraForm(data.params || {});
    setCameraConfigDirty(false);
    setText("cameraConfigPath", data.path || "-");
    setText("cameraRunState", data.camera_running ? "运行中" : "未运行");
    if (data.note) setText("cameraConfigNote", data.note);
    const presetBox = $("cameraPresetActions");
    if (presetBox && data.presets) {
      const keys = Object.keys(data.presets);
      if (keys.length) {
        presetBox.innerHTML = keys.map((key) => {
          const preset = data.presets[key] || {};
          const label = preset.label || key;
          const params = preset.params || {};
          const exposure = params.ExposureTime != null ? `${Number(params.ExposureTime) / 1000}ms` : "";
          const gain = {0: "固定增益", 1: "一次自动", 2: "自动增益"}[params.GainAuto] || "";
          const summary = [exposure, gain].filter(Boolean).join(" · ") || preset.description || "快速应用";
          return `<button type="button" class="primary" data-camera-preset="${escapeHtml(key)}"><strong>${escapeHtml(label)}</strong><small>${escapeHtml(summary)}</small></button>`;
        }).join("");
        presetBox.querySelectorAll("[data-camera-preset]").forEach((btn) => {
          btn.addEventListener("click", () => applyCameraPreset(btn.dataset.cameraPreset));
        });
      }
    }
  } catch (err) {
    toast(`相机参数读取失败: ${err.message}`);
  }
}

async function applyCameraConfig(restart = true) {
  const params = readCameraFormParams();
  toast(restart ? "写入配置并重启相机…" : "仅保存配置…");
  try {
    if (restart) closeCameraStream();
    const res = await fetch("/api/camera/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ params, restart }),
    });
    const data = await res.json();
    if (!res.ok || data.ok === false) throw new Error(data.output || data.message || `HTTP ${res.status}`);
    fillCameraForm(data.params || params);
    setCameraConfigDirty(!restart);
    setText("cameraRunState", restart ? "重启中/运行中" : ($("cameraRunState")?.textContent || "-"));
    toast(data.output || (restart ? "已应用并重启" : "已保存"));
    if (data.output) $("logBox").textContent = data.output;
    await refreshStatus();
    await refreshCameraConfig();
    if (!restart) setCameraConfigDirty(true);
    if (restart) {
      setText("cameraDebugMeta", "相机重启中，准备重连预览…");
      setTimeout(() => {
        ensureCameraStream(cameraStreamProfile || "default", true);
        toast("请查看预览是否曝光正常");
      }, 1500);
    }
  } catch (err) {
    toast(`应用失败: ${err.message}`);
    if (restart && cameraStreamShouldReconnect()) {
      setTimeout(() => ensureCameraStream(cameraStreamProfile || "default", true), 1500);
    }
  }
}

async function applyCameraPreset(presetId) {
  if (!presetId) return;
  toast(`应用预设 ${presetId}…`);
  try {
    closeCameraStream();
    const res = await fetch("/api/camera/preset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ preset: presetId, restart: true }),
    });
    const data = await res.json();
    if (!res.ok || data.ok === false) throw new Error(data.output || data.message || `HTTP ${res.status}`);
    fillCameraForm(data.params || {});
    toast(data.output || `已应用 ${data.preset_label || presetId}`);
    if (data.output) $("logBox").textContent = data.output;
    await refreshStatus();
    await refreshCameraConfig();
    setText("cameraDebugMeta", "相机重启中，准备重连预览…");
    setTimeout(() => ensureCameraStream(cameraStreamProfile || "default", true), 1500);
  } catch (err) {
    toast(`预设失败: ${err.message}`);
    if (cameraStreamShouldReconnect()) {
      setTimeout(() => ensureCameraStream(cameraStreamProfile || "default", true), 1500);
    }
  }
}

function formatTime(ts) {
  if (ts == null || ts === "") return "-";
  if (typeof ts === "string" && Number.isNaN(Number(ts))) return ts;
  const d = new Date(Number(ts) * (Number(ts) < 1e12 ? 1000 : 1));
  if (Number.isNaN(d.getTime())) return String(ts);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function dataMapHasPreview(map) {
  if (!map) return false;
  if (map.has_raw || map.has_downsampled) return true;
  return (map.files || []).some((f) => f.name === "all_raw_points.pcd" || f.name === "all_downsampled_points.pcd");
}

function dataMapPreviewFile(map) {
  const files = map?.files || [];
  if (map?.has_raw || files.some((f) => f.name === "all_raw_points.pcd")) return "all_raw_points.pcd";
  if (map?.has_downsampled || files.some((f) => f.name === "all_downsampled_points.pcd")) return "all_downsampled_points.pcd";
  return null;
}

function renderDataList() {
  const list = $("dataMapList");
  if (!list) return;
  if (dataMapsLoading && !dataMaps.length) {
    list.innerHTML = `<div class="data-list-empty">加载中…</div>`;
    return;
  }
  if (!dataMaps.length) {
    list.innerHTML = `<div class="data-list-empty">暂无扫描数据<br>完成录制后会出现在此</div>`;
    return;
  }
  list.innerHTML = dataMaps.map((map) => {
    const active = map.id === selectedDataMapId ? " active" : "";
    const sub = map.saved_at || formatTime(map.mtime);
    const size = map.total_size != null ? fmtSize(map.total_size) : "";
    const labels = {recording: "录制中", ready: "待建图", running: "建图中", completed: "已完成", failed: "失败", cancelled: "已停止", invalid: "数据异常"};
    return `<button type="button" class="data-list-item${active}" data-map-id="${escapeHtml(map.id)}">
      ${escapeHtml(map.id)} · ${escapeHtml(labels[map.status] || map.status || "-")}
      <span>${escapeHtml(sub)}${size ? ` · ${escapeHtml(size)}` : ""}</span>
    </button>`;
  }).join("");
  list.querySelectorAll("[data-map-id]").forEach((btn) => {
    btn.addEventListener("click", () => selectDataMap(btn.dataset.mapId));
  });
}

function renderDataDetail(map) {
  const detail = $("dataMapDetail");
  const previewBtn = $("openDataPreview");
  const offlineBtn = $("startOfflineMap");
  const deleteBtn = $("deleteDataScan");
  if (!detail) return;
  if (!map) {
    detail.innerHTML = `<div class="empty"><strong>选择左侧扫描记录</strong><span>完成建图后，结果会出现在此列表</span></div>`;
    if (previewBtn) previewBtn.disabled = true;
    if (offlineBtn) offlineBtn.disabled = true;
    if (deleteBtn) deleteBtn.disabled = true;
    return;
  }
  const files = map.files || [];
  const previewFile = dataMapPreviewFile(map);
  const rows = files.length
    ? files.map((f) => `<tr><td>${escapeHtml(f.name)}</td><td>${escapeHtml(fmtSize(f.size))}</td><td>${escapeHtml(formatTime(f.mtime))}</td></tr>`).join("")
    : `<tr><td colspan="3">无白名单文件</td></tr>`;
  const metaBits = [];
  const labels = {recording: "录制中", ready: "待离线建图", running: "离线建图中", completed: "已完成", failed: "建图失败，可重试", cancelled: "已停止，可重试", invalid: "原始数据异常"};
  metaBits.push(`状态 ${escapeHtml(labels[map.status] || map.status || "-")}`);
  if (map.bag_duration != null) metaBits.push(`时长 ${Number(map.bag_duration).toFixed(1)} 秒`);
  if (map.saved_at) metaBits.push(`保存时间 ${escapeHtml(map.saved_at)}`);
  if (map.copied_count != null) metaBits.push(`已复制 ${map.copied_count}`);
  if (map.missing_count != null && map.missing_count > 0) metaBits.push(`缺失 ${map.missing_count}`);
  const hint = previewFile
    ? ""
    : `<p class="data-hint">缺少点云文件，无法预览</p>`;
  detail.innerHTML = `
    <div class="kv">
      <span>扫描 ID</span><strong>${escapeHtml(map.id)}</strong>
      <span>修改时间</span><strong>${escapeHtml(formatTime(map.mtime))}</strong>
      <span>总大小</span><strong>${escapeHtml(map.total_size != null ? fmtSize(map.total_size) : "-")}</strong>
      <span>目录路径</span><strong>${escapeHtml(map.path || "-")}</strong>
    </div>
    ${metaBits.length ? `<p class="data-hint" style="color:#5b6b78">${metaBits.join(" · ")}</p>` : ""}
    <table class="data-file-table">
      <thead><tr><th>文件</th><th>大小</th><th>修改时间</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    ${hint}
  `;
  if (previewBtn) previewBtn.disabled = !previewFile;
  if (offlineBtn) {
    offlineBtn.disabled = !map.can_offline_map;
    offlineBtn.textContent = map.has_map ? "重新离线建图" : "离线建图";
  }
  if (deleteBtn) {
    deleteBtn.disabled = map.can_delete === false;
    deleteBtn.textContent = "删除数据";
  }
}

function selectDataMap(id) {
  selectedDataMapId = id;
  const map = dataMaps.find((m) => m.id === id) || null;
  renderDataList();
  renderDataDetail(map);
}

async function refreshDataMaps() {
  const btn = $("refreshDataMaps");
  if (dataMapsLoading) return;
  dataMapsLoading = true;
  if (btn) btn.disabled = true;
  renderDataList();
  try {
    const res = await fetch("/api/fastlivo/scans", { cache: "no-store" });
    const data = await res.json();
    if (!res.ok || data.ok === false) throw new Error(data.message || `HTTP ${res.status}`);
    dataMaps = data.scans || [];
    if (selectedDataMapId && !dataMaps.some((m) => m.id === selectedDataMapId)) {
      selectedDataMapId = null;
    }
    if (!selectedDataMapId && dataMaps.length) selectedDataMapId = dataMaps[0].id;
    renderDataList();
    renderDataDetail(dataMaps.find((m) => m.id === selectedDataMapId) || null);
  } catch (err) {
    toast(`建图列表加载失败: ${err.message}`);
    renderDataList();
    renderDataDetail(dataMaps.find((m) => m.id === selectedDataMapId) || null);
  } finally {
    dataMapsLoading = false;
    if (btn) btn.disabled = false;
  }
}

async function startOfflineMap() {
  const scan = dataMaps.find((item) => item.id === selectedDataMapId);
  if (!scan?.can_offline_map) return;
  if (scan.has_map && !window.confirm("将重新处理原始bag；只有新结果完整通过验证后才会替换当前地图。继续吗？")) return;
  try {
    const res = await fetch("/api/fastlivo/offline/start", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({scan_id: scan.id}),
    });
    const data = await res.json();
    if (!res.ok || data.ok === false) throw new Error(data.message || data.output || `HTTP ${res.status}`);
    lastOfflineStatus = "starting";
    renderOfflineProgress(data.job || {});
    await refreshDataMaps();
    toast("离线建图已开始，可以留在数据管理页查看进度");
  } catch (err) {
    toast(`离线建图启动失败: ${err.message}`);
  }
}

async function cancelOfflineMap() {
  try {
    const res = await fetch("/api/fastlivo/offline/cancel", {method: "POST"});
    const data = await res.json();
    if (!res.ok || data.ok === false) throw new Error(data.message || `HTTP ${res.status}`);
    toast("正在安全停止离线建图");
  } catch (err) {
    toast(`停止失败: ${err.message}`);
  }
}

async function deleteDataScan() {
  const scan = dataMaps.find((item) => item.id === selectedDataMapId);
  if (!scan) return;
  if (scan.can_delete === false) {
    toast("录制中或离线建图中的数据不能删除");
    return;
  }
  const sizeText = scan.total_size != null ? fmtSize(scan.total_size) : "未知大小";
  const ok = window.confirm(
    `确定删除扫描数据？\n\nID：${scan.id}\n大小：${sizeText}\n\n将永久删除 bag、点云和导出目录，不可恢复。`
  );
  if (!ok) return;
  const deleteBtn = $("deleteDataScan");
  if (deleteBtn) deleteBtn.disabled = true;
  try {
    const res = await fetch("/api/fastlivo/scans/delete", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({scan_id: scan.id}),
    });
    const data = await res.json();
    if (!res.ok || data.ok === false) throw new Error(data.message || data.output || `HTTP ${res.status}`);
    if (selectedDataMapId === scan.id) selectedDataMapId = null;
    await refreshDataMaps();
    toast(data.message || `已删除 ${scan.id}`);
  } catch (err) {
    toast(`删除失败: ${err.message}`);
    if (deleteBtn) deleteBtn.disabled = scan.can_delete === false;
  }
}

function renderOfflineProgress(job = {}) {
  const active = ["starting", "running", "draining", "saving", "cancel_requested"].includes(job.status);
  const box = $("offlineProgress");
  if (box) box.hidden = !active && !["failed", "cancelled"].includes(job.status);
  $("cancelOfflineMap").hidden = !active;
  const labels = {starting: "启动FAST-LIVO2", running: job.paused ? "积压较高，已暂停回放" : "正在回放原始数据", draining: "回放结束，正在排空缓存", saving: "正在保存并验证模型", cancel_requested: "正在安全停止", failed: `失败：${job.error || "未知错误"}`, cancelled: "离线建图已停止"};
  setText("offlineProgressText", labels[job.status] || "离线建图");
  setText("offlineLagText", job.lag != null ? `积压 ${Number(job.lag).toFixed(2)} 秒` : "-");
  if ($("offlineProgressBar")) $("offlineProgressBar").value = Math.round(Number(job.progress || 0) * 100);
}

async function openDataPreview() {
  const map = dataMaps.find((m) => m.id === selectedDataMapId);
  const filename = dataMapPreviewFile(map);
  if (!map || !filename) {
    toast("缺少点云文件");
    return;
  }
  try {
    toast("正在加载模型…");
    await loadMapFile(map.id, filename);
    showTab("fastlivo");
    setScanState("complete", `已打开 ${map.id}`);
    toast(`已切换为最终模型预览 · ${map.id}`);
  } catch (err) {
    toast(`预览失败: ${err.message}`);
  }
}

async function postAction(path) {
  toast("执行中...");
  try {
    const res = await fetch(path, { method: "POST" });
    const data = await res.json();
    toast(data.ok ? "已执行" : "执行失败");
    await refreshStatus();
    if (data.output || data.message) {
      $("logBox").textContent = data.output || data.message;
      showTab("logs");
    }
  } catch (err) {
    toast(`请求失败: ${err.message}`);
  }
}

async function listGsDatasets() {
  try {
    const res = await fetch("/api/gs/datasets", { cache: "no-store" });
    const data = await res.json();
    const rows = (data.datasets || []).map((item) => {
      const sync = item.sync?.status || "not_synced";
      const counts = `images=${item.image_count ?? "-"} poses=${item.pose_count ?? "-"} matched=${item.matched_count ?? "-"}`;
      const errors = (item.errors || []).length ? ` errors=${item.errors.join("; ")}` : "";
      return `${item.id} · ok=${item.ok} · ${sync} · ${counts}${errors}\n${item.path}`;
    });
    $("logBox").textContent = rows.length ? rows.join("\n\n") : "no GS-LIVO datasets";
    showTab("logs");
  } catch (err) {
    toast(`GS数据包查询失败: ${err.message}`);
  }
}

async function syncLatestGsDataset() {
  toast("正在同步GS数据包...");
  try {
    const res = await fetch("/api/gs/sync_latest", { method: "POST" });
    const data = await res.json();
    $("logBox").textContent = data.output || JSON.stringify(data, null, 2);
    showTab("logs");
    toast(data.ok ? "GS数据包同步完成" : "GS数据包同步失败");
  } catch (err) {
    toast(`GS同步请求失败: ${err.message}`);
  }
}

async function refreshStatus() {
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    setText("serviceState", "在线");
    setText("hostName", data.host || "-");
    setText("memState", data.memory?.used_percent == null ? "-" : `${data.memory.used_percent}%`);
    setText("netState", data.network?.enp1s0 || "-");
    setText("pingState", data.network?.mid360_ping_ok ? "ping 正常" : "未通");

    const storage = data.storage || {};
    const disk = storage.disk || {};
    const maps = storage.maps || {};
    const bags = storage.bags || {};
    setText("diskTotal", disk.total_bytes != null ? fmtSize(disk.total_bytes) : "-");
    setText(
      "diskUsedFree",
      disk.used_bytes != null && disk.free_bytes != null
        ? `${fmtSize(disk.used_bytes)} / ${fmtSize(disk.free_bytes)}`
        : "-"
    );
    setText("diskUsedPct", disk.used_percent != null ? `${disk.used_percent}%` : "-");
    setText(
      "mapsDirSize",
      maps.bytes != null ? fmtSize(maps.bytes) : "-"
    );
    setText(
      "bagsDirSize",
      bags.bytes != null ? fmtSize(bags.bytes) : "-"
    );
    setText(
      "storageCounts",
      `${maps.count != null ? maps.count : "-"} / ${bags.count != null ? bags.count : "-"}`
    );
    setText("mapsDirPath", maps.path || "-");
    setText("bagsDirPath", bags.path || "-");

    const topics = data.topics || [];
    const topicHtml = topics.length
      ? topics.map((topic) => `<span class="chip">${escapeHtml(topic)}</span>`).join("")
      : `<span class="chip">暂无 ROS topic</span>`;
    if ($("topicList").innerHTML !== topicHtml) $("topicList").innerHTML = topicHtml;
    setText("cameraState", topics.some((t) => t.includes("camera") || t.includes("rgb_img")) ? "检测到图像 topic" : "等待硬件");

    workflowMode = data.workflow || "idle";
    if (workflowMode !== "realtime_mapping") {
      $("startScan").disabled = workflowMode !== "idle";
      $("finishScan").disabled = true;
    }
    const recording = data.recording || {};
    if (recording.active) {
      setRecordState("recording", "正在无损录制");
      syncRecordElapsed(recording.elapsed);
      setText("recordSize", fmtSize(recording.size || 0));
      setText("recordDiskFree", fmtSize(recording.free_bytes || 0));
      setText("recordTimeLeft", recording.estimated_seconds_left == null ? "计算中" : `${Math.floor(recording.estimated_seconds_left / 60)} 分钟`);
      setText("recordScanId", recording.scan_id || "-");
      $("recordHealth")?.classList.toggle("warning", Boolean(recording.warning));
      if (!cameraWs) ensureCameraStream("recording");
      if (!pointWs) ensurePointStream(recordRadarPreviewEnabled ? "lidar" : "health");
    } else if (recordState === "recording") {
      setRecordState("idle");
    } else {
      setRecordState(recordState);
    }

    const offline = data.offline || {};
    const previousOfflineStatus = lastOfflineStatus;
    renderOfflineProgress(offline);
    lastOfflineStatus = offline.status || "";
    const activeOfflineStates = ["starting", "running", "draining", "saving", "cancel_requested"];
    if (previousOfflineStatus && activeOfflineStates.includes(previousOfflineStatus) && offline.status === "completed") {
      await refreshDataMaps();
      await loadMapFile(offline.scan_id, offline.result_file || "all_raw_points.pcd");
      showTab("fastlivo");
      setScanState("complete", `离线建图完成 · ${offline.scan_id}`);
      toast("离线建图完成，已加载最终模型");
    } else if (previousOfflineStatus !== lastOfflineStatus && $("data")?.classList.contains("active")) {
      await refreshDataMaps();
    }

    const running = data.running || {};
    if (running.lidar?.length && $("hzLidar").textContent === "-") setText("hzLidar", "驱动运行中");
    if (workflowMode === "realtime_mapping" && running.fusion?.length && scanState === "idle") {
      sceneMode = "live";
      setViewMode("fps");
      ensureCameraStream("default");
      ensurePointStream("mapping");
      setScanState("scanning", "扫描中");
    }
    if (workflowMode !== "realtime_mapping" && scanState === "scanning") setScanState("idle");
    const processing = data.processing || {};
    if (workflowMode === "realtime_mapping" && processing.lag != null) {
      const lag = Number(processing.lag);
      setScanState("scanning", lag > 2 ? `实时地图落后 ${lag.toFixed(1)} 秒，原始bag仍在保存` : `实时建图中 · 积压 ${lag.toFixed(2)} 秒`);
    }
  } catch (err) {
    setText("serviceState", "离线");
  }
}

async function loadLogs(target) {
  const res = await fetch(`/api/logs?target=${encodeURIComponent(target)}`);
  const data = await res.json();
  const text = (data.logs || [])
    .map((item) => `===== ${item.name} =====\n${item.tail}`)
    .join("\n\n");
  $("logBox").textContent = text || "没有日志";
}

function updateRates(rates) {
  if (rates["/livox/lidar"] != null) {
    setText("hzLidar", `${rates["/livox/lidar"]} Hz`);
    setText("recordLidarHz", `${rates["/livox/lidar"]} Hz`);
  }
  if (rates["/livox/imu"] != null) {
    setText("hzImu", `${rates["/livox/imu"]} Hz`);
    setText("recordImuHz", `${rates["/livox/imu"]} Hz`);
  }
  if (rates["/cloud_registered"] != null) setText("hzCloud", `${rates["/cloud_registered"]} Hz`);
  if (rates["/path"] != null) setText("hzPath", `${rates["/path"]} Hz`);
  if (rates["/aft_mapped_to_init"] != null) setText("hzOdom", `${rates["/aft_mapped_to_init"]} Hz`);
  recordSensorRates = {...recordSensorRates, ...rates};
  const previewRate = recordSensorRates["/hikrobot_camera/preview/compressed"];
  const savedRate = recordSensorRates["/left_camera/image"];
  const mappedRate = recordSensorRates["/rgb_img"];
  if (previewRate != null || savedRate != null || mappedRate != null) {
    setText(
      "cameraMeta",
      `预览 ${previewRate ?? "-"} Hz · 保存 ${savedRate ?? "-"} Hz${mappedRate != null ? ` · 建图 ${mappedRate} Hz` : ""}`,
    );
    setText("recordCameraHz", `预览 ${previewRate ?? "-"} · 保存 ${savedRate ?? "-"}`);
  }
  updateRecordSensorStatus();
}

function closePointStream() {
  if (pointWs) {
    pointWs.close();
    pointWs = null;
  }
}

function closeCameraStream() {
  if (cameraReconnectTimer) {
    clearTimeout(cameraReconnectTimer);
    cameraReconnectTimer = null;
  }
  if (cameraWs) {
    const socket = cameraWs;
    cameraWs = null;
    socket.onclose = null;
    socket.close();
  }
  cameraFramePending = null;
  cameraFrameGeneration++;
  if (cameraObjectUrl) {
    URL.revokeObjectURL(cameraObjectUrl);
    cameraObjectUrl = null;
  }
}

function cameraStreamShouldReconnect() {
  return Boolean($("camera")?.classList.contains("active") || scanState === "starting" || scanState === "scanning" || recordState === "starting" || recordState === "recording");
}

function ensurePointStream(mode = pointStreamMode) {
  if (pointWs && pointStreamMode === mode) return;
  if (pointWs) closePointStream();
  pointStreamMode = mode;
  pointWs = new WebSocket(`${wsScheme()}://${location.host}/ws/points?mode=${encodeURIComponent(mode)}&quality=${qualityMode}`);
  $("connectStreams").textContent = "重连预览";
  pointWs.onopen = () => {
    if (mode === "health") setText("recordSensorSummary", "健康监控已连接");
    else setText("viewerMeta", "三维实时预览已连接");
  };
  pointWs.onclose = () => {
    pointWs = null;
    if (mode === "health") setText("recordSensorSummary", "健康监控已断开");
    else if (sceneMode === "live") setText(mode === "lidar" ? "recordMapHud" : "viewerMeta", "三维实时预览已断开");
  };
  pointWs.onerror = () => {
    if (mode === "health") setText("recordSensorSummary", "健康监控连接失败");
    else toast("三维连接失败");
  };
  pointWs.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "points") addLivePointBatch(msg);
    else if (msg.type === "path") updatePath(msg);
    else if (msg.type === "odom") updateOdom(msg);
    else if (msg.type === "rates") {
      if (msg.health) recordSensorHealth = {...recordSensorHealth, ...msg.health};
      updateRates(msg.rates || {});
    }
    else if (msg.type === "status") {
      if (mode === "health") setText("recordSensorSummary", msg.message);
      else setText(mode === "lidar" ? "recordMapHud" : "viewerMeta", msg.message);
    }
  };
}

function setRecordRadarPreview(enabled, reconnect = true) {
  recordRadarPreviewEnabled = Boolean(enabled);
  const preview = $("recordRadarPreview");
  const button = $("toggleRecordRadarPreview");
  if (preview) preview.hidden = !recordRadarPreviewEnabled;
  if (button) {
    button.classList.toggle("active", recordRadarPreviewEnabled);
    button.textContent = recordRadarPreviewEnabled ? "关闭雷达" : "雷达预览";
  }
  if (recordRadarPreviewEnabled) {
    attachThreeRenderer("recordThreeViewport");
    setTimeout(resizeThree, 60);
  } else {
    attachThreeRenderer("threeViewport");
    clearScene(false);
  }
  if (reconnect && recordState === "recording") {
    closePointStream();
    ensurePointStream(recordRadarPreviewEnabled ? "lidar" : "health");
  }
}

function activeCameraTarget() {
  if (activePageId === "record") return ["recordCameraImage", "recordCameraEmpty", "recordCameraMeta"];
  if (activePageId === "camera") return ["cameraDebugImage", "cameraDebugEmpty", "cameraDebugMeta"];
  if (activePageId === "fastlivo") return ["cameraImage", "cameraEmpty", "cameraMeta"];
  return null;
}

function updateCameraFrameMetadata(msg, metaId) {
  const now = performance.now();
  if (now - cameraMetadataUpdatedAt < 500) return;
  cameraMetadataUpdatedAt = now;
  if (metaId) setText(metaId, `${msg.topic || "image"} · ${msg.width || "?"}x${msg.height || "?"}`);
  updateCameraExposure(msg.exposure_us);
  if (msg.exposure_us) {
    setText("recordExposure", Number(msg.exposure_us) >= 1000
      ? `${(Number(msg.exposure_us) / 1000).toFixed(2)} ms`
      : `${Number(msg.exposure_us).toFixed(0)} µs`);
  }
}

function resizeRecordCanvas() {
  const canvas = $("recordCameraImage");
  if (!canvas || !$("record")?.classList.contains("active")) return null;
  canvas.style.display = "block";
  const rect = canvas.getBoundingClientRect();
  // The preview JPEG is already 960 pixels wide. A 1:1 CSS-pixel backing
  // buffer avoids the extra fill work of a device-pixel-ratio multiplier.
  const width = Math.max(1, Math.floor(rect.width));
  const height = Math.max(1, Math.floor(rect.height));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
    recordCanvasContext = null;
  }
  if (!recordCanvasContext) {
    recordCanvasContext = canvas.getContext("2d", {alpha: false, desynchronized: true});
    recordCanvasContext.fillStyle = "#080d12";
    recordCanvasContext.fillRect(0, 0, width, height);
  }
  recordCanvasMetrics = {width, height};
  return recordCanvasMetrics;
}

async function pumpRecordCameraFrames() {
  if (cameraFrameDrawing) return;
  cameraFrameDrawing = true;
  try {
    while (cameraFramePending) {
      const current = cameraFramePending;
      cameraFramePending = null;
      let bitmap;
      try {
        bitmap = await createImageBitmap(current.blob);
        if (current.generation !== cameraFrameGeneration) continue;
        const canvas = $("recordCameraImage");
        if (!canvas || !$("record")?.classList.contains("active")) continue;
        // A newer frame supersedes one that was still being decoded.
        if (cameraFramePending) continue;
        const metrics = recordCanvasMetrics || resizeRecordCanvas();
        if (!metrics || !recordCanvasContext) continue;
        await new Promise((resolve) => requestAnimationFrame(resolve));
        if (current.generation !== cameraFrameGeneration || cameraFramePending) continue;
        const {width, height} = metrics;
        const ctx = recordCanvasContext;
        const scale = Math.min(width / bitmap.width, height / bitmap.height);
        const drawWidth = Math.max(1, Math.round(bitmap.width * scale));
        const drawHeight = Math.max(1, Math.round(bitmap.height * scale));
        const x = Math.round((width - drawWidth) / 2);
        const y = Math.round((height - drawHeight) / 2);
        ctx.drawImage(bitmap, x, y, drawWidth, drawHeight);
        if ($("recordCameraEmpty")) $("recordCameraEmpty").style.display = "none";
        updateCameraFrameMetadata(current.metadata, "recordCameraMeta");
      } catch (err) {
        setText("recordCameraMeta", `视频解码失败: ${err.message}`);
      } finally {
        bitmap?.close();
      }
    }
  } finally {
    cameraFrameDrawing = false;
    if (cameraFramePending) pumpRecordCameraFrames();
  }
}

function displayCameraBlob(blob, metadata) {
  const target = activeCameraTarget();
  if (!target) return;
  if (target[0] === "recordCameraImage") {
    // Keep only the newest frame while ImageBitmap decoding is in progress.
    // Dropping an obsolete preview frame must never block rosbag recording.
    cameraFramePending = {blob, metadata, generation: cameraFrameGeneration};
    pumpRecordCameraFrames();
    return;
  }
  const image = $(target[0]);
  if (!image) return;
  if (cameraObjectUrl) URL.revokeObjectURL(cameraObjectUrl);
  cameraObjectUrl = URL.createObjectURL(blob);
  image.src = cameraObjectUrl;
  image.style.display = "block";
  if ($(target[1])) $(target[1]).style.display = "none";
  updateCameraFrameMetadata(metadata, target[2]);
}

function parseCameraBinaryFrame(buffer) {
  if (!(buffer instanceof ArrayBuffer) || buffer.byteLength < 5) return null;
  const view = new DataView(buffer);
  const metadataLength = view.getUint32(0, false);
  if (metadataLength <= 0 || metadataLength > buffer.byteLength - 4) return null;
  const metadataBytes = new Uint8Array(buffer, 4, metadataLength);
  const metadata = JSON.parse(new TextDecoder().decode(metadataBytes));
  const jpeg = new Uint8Array(buffer, 4 + metadataLength);
  return {metadata, blob: new Blob([jpeg], {type: "image/jpeg"})};
}

function legacyBase64Blob(value) {
  const encoded = String(value || "").replace(/^data:image\/jpeg;base64,/, "");
  const binary = atob(encoded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new Blob([bytes], {type: "image/jpeg"});
}

function ensureCameraStream(profile = cameraStreamProfile, force = false) {
  if (!force && cameraWs && cameraStreamProfile === profile) return;
  if (cameraWs) closeCameraStream();
  cameraStreamProfile = profile;
  if (cameraReconnectTimer) {
    clearTimeout(cameraReconnectTimer);
    cameraReconnectTimer = null;
  }
  const socket = new WebSocket(`${wsScheme()}://${location.host}/ws/camera?quality=${qualityMode}&profile=${encodeURIComponent(profile)}`);
  socket.binaryType = "arraybuffer";
  cameraWs = socket;
  socket.onopen = () => setText("cameraMeta", "视频连接中");
  socket.onclose = () => {
    if (cameraWs === socket) cameraWs = null;
    setText("cameraMeta", "视频已断开");
    setText("cameraDebugMeta", "视频已断开");
    if (cameraStreamShouldReconnect()) {
      setText("cameraDebugMeta", "视频已断开，正在重连…");
      cameraReconnectTimer = setTimeout(() => {
        cameraReconnectTimer = null;
        ensureCameraStream();
      }, 1500);
    }
  };
  socket.onerror = () => setText("cameraDebugMeta", "视频连接失败，准备重连…");
  socket.onmessage = (event) => {
    if (event.data instanceof ArrayBuffer) {
      try {
        const frame = parseCameraBinaryFrame(event.data);
        if (frame) displayCameraBlob(frame.blob, frame.metadata);
      } catch (err) {
        setText("recordCameraMeta", `视频帧错误: ${err.message}`);
      }
      return;
    }
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch (err) {
      return;
    }
    // Bridge may send base64 under "data" (current) or legacy "jpeg_b64".
    const b64 = msg.jpeg_b64 || msg.data || msg.jpeg || "";
    if (msg.type === "image" && b64) {
      displayCameraBlob(legacyBase64Blob(b64), msg);
    } else if ((msg.type === "rate" || msg.type === "rates") && msg.rates) {
      updateRates(msg.rates);
    } else if (msg.type === "status") {
      const metaId = activePageId === "record" ? "recordCameraMeta" : activePageId === "camera" ? "cameraDebugMeta" : "cameraMeta";
      setText(metaId, msg.message || "视频状态更新");
    }
  };
}

async function startScanWorkflow() {
  showTab("fastlivo");
  setScanState("starting", "启动中");
  sceneMode = "live";
  currentMapName = "";
  rawPointTotal = 0;
  closePointStream();
  closeCameraStream();
  clearScene(false);
  setViewMode("fps");
  followEnabled = true;
  updateFollowButton();
  setText("mapTitle", "实时累计地图");
  setText("viewerMeta", "正在启动雷达、相机和 FAST-LIVO2");
  try {
    const res = await fetch("/api/fastlivo/start_all", { method: "POST" });
    const data = await res.json();
    if (!data.ok) throw new Error(data.output || data.message || "启动失败");
    ensureCameraStream("default");
    ensurePointStream("mapping");
    setScanState("scanning", "扫描中");
    toast("实时建图已开始");
  } catch (err) {
    setScanState("error", `启动失败: ${err.message}`);
    toast(`启动失败: ${err.message}`);
  } finally {
    await refreshStatus();
  }
}

async function finishScanWorkflow() {
  setScanState("saving", "保存中");
  setText("viewerMeta", "正在停止 FAST-LIVO2 并保存官方 PCD");
  try {
    const res = await fetch("/api/fastlivo/stop", { method: "POST" });
    const data = await res.json();
    if (!data.ok) throw new Error(data.output || data.message || "保存失败");
    closePointStream();
    closeCameraStream();
    await loadFinalMapFromStopResult(data);
    setScanState("complete", "已完成，已打开最终模型");
    toast("建图完成，已加载最终模型");
  } catch (err) {
    setScanState("error", `保存失败: ${err.message}`);
    toast(`保存失败: ${err.message}`);
  } finally {
    await refreshStatus();
  }
}

async function startRecordWorkflow() {
  showTab("record");
  setRecordState("starting");
  closePointStream();
  closeCameraStream();
  clearScene(false);
  setRecordRadarPreview(false, false);
  resetRecordSensorStatus();
  sceneMode = "live";
  setViewMode("free");
  try {
    const res = await fetch("/api/fastlivo/record/start", {method: "POST"});
    const data = await res.json();
    if (!res.ok || data.ok === false) throw new Error(data.message || data.output || `HTTP ${res.status}`);
    setText("recordScanId", data.scan_id || "-");
    ensureCameraStream("recording");
    ensurePointStream("health");
    setRecordState("recording");
    toast("无损数据录制已开始");
  } catch (err) {
    setRecordState("invalid", `启动失败: ${err.message}`);
    toast(`录制启动失败: ${err.message}`);
  } finally {
    await refreshStatus();
  }
}

async function stopRecordWorkflow() {
  setRecordState("stopping");
  try {
    const res = await fetch("/api/fastlivo/record/stop", {method: "POST"});
    const data = await res.json();
    if (!res.ok || data.ok === false) throw new Error(data.message || (data.bag_info?.errors || []).join("；") || `HTTP ${res.status}`);
    closePointStream();
    closeCameraStream();
    setRecordRadarPreview(false, false);
    selectedDataMapId = data.scan_id || selectedDataMapId;
    setRecordState("valid");
    await refreshDataMaps();
    showTab("data");
    toast("录制完成，数据校验通过；请选择离线建图");
  } catch (err) {
    closePointStream();
    closeCameraStream();
    setRecordRadarPreview(false, false);
    setRecordState("invalid", `录制结束，但校验异常: ${err.message}`);
    await refreshDataMaps();
    showTab("data");
    toast(`数据校验异常: ${err.message}`);
  } finally {
    await refreshStatus();
  }
}

async function loadFinalMapFromStopResult(data) {
  const scanDir = data.scan_dir || data.save?.scan_dir;
  if (!scanDir) {
    await loadLatestMap();
    return;
  }
  const scanId = scanDir.replaceAll("\\", "/").split("/").filter(Boolean).pop();
  await loadMapFile(scanId, "all_raw_points.pcd");
}

async function loadLatestMap() {
  const res = await fetch("/api/fastlivo/maps", { cache: "no-store" });
  const data = await res.json();
  const map = (data.maps || []).find((item) => item.files?.some((f) => f.name === "all_raw_points.pcd"));
  if (!map) {
    toast("还没有可打开的地图");
    return;
  }
  await loadMapFile(map.id, "all_raw_points.pcd");
  setScanState("complete", "已打开最新模型");
}

async function loadMapFile(scanId, filename) {
  sceneMode = "final";
  closePointStream();
  clearScene(false);
  setText("mapTitle", "最终模型");
  setText("viewerMeta", `正在加载 ${scanId}/${filename}`);
  const url = `/api/fastlivo/maps/${encodeURIComponent(scanId)}/${encodeURIComponent(filename)}`;
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`地图加载失败 ${res.status}`);
  const buffer = await res.arrayBuffer();
  const parsed = parsePcd(buffer, pointBudget());
  currentMapName = `${scanId}/${filename}`;
  rawPointTotal = parsed.total;
  addPointObject(parsed.points, { hasRgb: parsed.hasRgb, replace: true });
  fitViewToPoints(parsed.points);
  setViewMode("fps");
  followEnabled = false;
  updateFollowButton();
  setText("viewerMeta", `${currentMapName} · 显示 ${formatCount(parsed.points.length)} / 原始 ${formatCount(parsed.total)}`);
  updateMapStats();
}

function asciiHeader(buffer) {
  const bytes = new Uint8Array(buffer);
  const needle = [68, 65, 84, 65, 32]; // DATA 
  for (let i = 0; i < Math.min(bytes.length - 12, 16384); i++) {
    let ok = true;
    for (let j = 0; j < needle.length; j++) if (bytes[i + j] !== needle[j]) ok = false;
    if (ok) {
      let eol = i;
      while (eol < bytes.length && bytes[eol] !== 10) eol++;
      return {
        text: new TextDecoder("utf-8").decode(bytes.slice(0, eol + 1)),
        dataOffset: eol + 1,
      };
    }
  }
  throw new Error("PCD header DATA line not found");
}

function parsePcd(buffer, maxPoints) {
  const { text, dataOffset } = asciiHeader(buffer);
  const header = {};
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const [key, ...rest] = trimmed.split(/\s+/);
    header[key.toUpperCase()] = rest;
  }
  const fields = header.FIELDS || [];
  const sizes = (header.SIZE || fields.map(() => "4")).map(Number);
  const types = header.TYPE || fields.map(() => "F");
  const counts = (header.COUNT || fields.map(() => "1")).map(Number);
  const total = Number(header.POINTS?.[0] || header.WIDTH?.[0] || 0);
  const dataType = (header.DATA?.[0] || "").toLowerCase();
  if (!total) throw new Error("PCD POINTS is missing");
  if (dataType !== "binary" && dataType !== "ascii") throw new Error(`Unsupported PCD DATA ${dataType}`);

  const offsets = {};
  let pointStep = 0;
  fields.forEach((name, idx) => {
    offsets[name] = { offset: pointStep, size: sizes[idx] || 4, type: types[idx] || "F", count: counts[idx] || 1 };
    pointStep += (sizes[idx] || 4) * (counts[idx] || 1);
  });
  const xField = offsets.x;
  const yField = offsets.y;
  const zField = offsets.z;
  const rgbField = offsets.rgb || offsets.rgba;
  if (!xField || !yField || !zField) throw new Error("PCD missing x/y/z fields");
  const step = Math.max(1, Math.ceil(total / maxPoints));
  const out = [];

  if (dataType === "ascii") {
    const body = new TextDecoder("utf-8").decode(new Uint8Array(buffer, dataOffset));
    const lines = body.trim().split(/\r?\n/);
    const xIdx = fields.indexOf("x");
    const yIdx = fields.indexOf("y");
    const zIdx = fields.indexOf("z");
    const rgbIdx = Math.max(fields.indexOf("rgb"), fields.indexOf("rgba"));
    for (let i = 0; i < lines.length; i += step) {
      const vals = lines[i].trim().split(/\s+/).map(Number);
      const rgb = rgbIdx >= 0 ? unpackRgbNumber(vals[rgbIdx]) : [120, 220, 255];
      out.push([vals[xIdx], vals[yIdx], vals[zIdx], rgb[0], rgb[1], rgb[2]]);
    }
    return { points: out.filter((p) => p.slice(0, 3).every(Number.isFinite)), total, hasRgb: rgbIdx >= 0 };
  }

  const view = new DataView(buffer, dataOffset);
  for (let i = 0; i < total; i += step) {
    const base = i * pointStep;
    if (base + pointStep > view.byteLength) break;
    const x = readPcdScalar(view, base + xField.offset, xField);
    const y = readPcdScalar(view, base + yField.offset, yField);
    const z = readPcdScalar(view, base + zField.offset, zField);
    let rgb = [120, 220, 255];
    if (rgbField) {
      const packed = view.getUint32(base + rgbField.offset, true);
      rgb = [(packed >> 16) & 255, (packed >> 8) & 255, packed & 255];
    }
    if (Number.isFinite(x) && Number.isFinite(y) && Number.isFinite(z)) out.push([x, y, z, rgb[0], rgb[1], rgb[2]]);
  }
  return { points: out, total, hasRgb: Boolean(rgbField) };
}

function readPcdScalar(view, offset, field) {
  if (field.type === "F") return field.size === 8 ? view.getFloat64(offset, true) : view.getFloat32(offset, true);
  if (field.type === "U") {
    if (field.size === 1) return view.getUint8(offset);
    if (field.size === 2) return view.getUint16(offset, true);
    return view.getUint32(offset, true);
  }
  if (field.size === 1) return view.getInt8(offset);
  if (field.size === 2) return view.getInt16(offset, true);
  return view.getInt32(offset, true);
}

function unpackRgbNumber(value) {
  if (!Number.isFinite(value)) return [120, 220, 255];
  const buffer = new ArrayBuffer(4);
  const view = new DataView(buffer);
  if (Math.abs(value) < 1e-30) view.setFloat32(0, value, true);
  else view.setUint32(0, value >>> 0, true);
  const packed = view.getUint32(0, true);
  return [(packed >> 16) & 255, (packed >> 8) & 255, packed & 255];
}

function initThree() {
  const wrap = $("threeViewport");
  scene = new THREE.Scene();
  scene.background = new THREE.Color(0x111820);
  camera = new THREE.PerspectiveCamera(68, 1, 0.03, 2000);
  renderer = new THREE.WebGLRenderer({ antialias: false, powerPreference: "high-performance" });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  wrap.appendChild(renderer.domElement);

  cameraTarget = new THREE.Vector3(0, 0, 0);
  fpsPosition = new THREE.Vector3(0, 1.2, 0);
  grid = new THREE.GridHelper(60, 60, 0x35505c, 0x21313a);
  scene.add(grid);
  scene.add(new THREE.AmbientLight(0xffffff, 1.0));

  const axes = new THREE.AxesHelper(1.4);
  axes.position.set(0, 0.02, 0);
  scene.add(axes);

  poseArrow = new THREE.ArrowHelper(new THREE.Vector3(1, 0, 0), new THREE.Vector3(0, 0.08, 0), 1.1, 0x38bdf8, 0.28, 0.18);
  poseArrow.visible = false;
  scene.add(poseArrow);

  pathLine = new THREE.Line(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({ color: 0xffcf5a, linewidth: 2 }));
  scene.add(pathLine);

  initThreePointer();
  initJoysticks();
  resizeThree();
  requestAnimationFrame(renderLoop);
}

function resizeThree() {
  if (!renderer || !camera) return;
  const host = renderer.domElement.parentElement || $("threeViewport");
  const rect = host.getBoundingClientRect();
  renderer.setSize(Math.max(1, Math.floor(rect.width)), Math.max(1, Math.floor(rect.height)), false);
  camera.aspect = Math.max(1, rect.width) / Math.max(1, rect.height);
  camera.updateProjectionMatrix();
}

function renderLoop(now) {
  const threeVisible = activePageId === "fastlivo" || (activePageId === "record" && recordRadarPreviewEnabled);
  if (!threeVisible) {
    lastRenderTime = now;
    requestAnimationFrame(renderLoop);
    return;
  }
  const dt = Math.min(0.05, Math.max(0.001, (now - lastRenderTime) / 1000));
  lastRenderTime = now;
  renderFrames++;
  if (now - fpsStarted >= 1000) {
    renderFps = renderFrames;
    renderFrames = 0;
    fpsStarted = now;
    updateMapStats();
  }
  updateManualFps(dt);
  updateCameraPose();
  renderer.render(scene, camera);
  requestAnimationFrame(renderLoop);
}

function updateCameraPose() {
  if (!camera || !cameraTarget) return;
  if (viewMode === "fps") {
    if (followEnabled && currentPose?.position && sceneMode === "live") {
      const pos = rosToThree(currentPose.position);
      fpsPosition.copy(pos).add(new THREE.Vector3(0, 0.38, 0));
      const dir = headingToThreeDirection(lastHeading);
      fpsYaw = Math.atan2(dir.x, dir.z);
      fpsPitch = 0;
    }
    const direction = new THREE.Vector3(
      Math.sin(fpsYaw) * Math.cos(fpsPitch),
      Math.sin(fpsPitch),
      Math.cos(fpsYaw) * Math.cos(fpsPitch)
    ).normalize();
    camera.position.copy(fpsPosition);
    camera.up.set(0, 1, 0);
    camera.lookAt(fpsPosition.clone().add(direction));
    return;
  }

  if (followEnabled && currentPose?.position && sceneMode === "live") cameraTarget.copy(rosToThree(currentPose.position));
  if (viewMode === "top") {
    camera.position.set(cameraTarget.x, cameraTarget.y + orbitDistance, cameraTarget.z + 0.01);
    camera.up.set(0, 0, -1);
  } else {
    const cp = Math.cos(orbitPitch);
    camera.position.set(
      cameraTarget.x + Math.sin(orbitYaw) * cp * orbitDistance,
      cameraTarget.y + Math.sin(orbitPitch) * orbitDistance,
      cameraTarget.z + Math.cos(orbitYaw) * cp * orbitDistance
    );
    camera.up.set(0, 1, 0);
  }
  camera.lookAt(cameraTarget);
}

function addLivePointBatch(msg) {
  sceneMode = "live";
  const rawLidar = msg.mode === "lidar" || pointStreamMode === "lidar";
  rawPointTotal = rawLidar ? (msg.raw_count || 0) : rawPointTotal + (msg.raw_count || 0);
  addPointObject(msg.points || [], { hasRgb: Boolean(msg.has_rgb), replace: rawLidar });
  lastPointStamp = Date.now();
  if (rawLidar) {
    setText("recordMapMeta", `${formatCount(totalPoints)} / ${formatCount(msg.raw_count || 0)} 点`);
    setText("recordMapHud", `${msg.topic} · 最新帧 · ${msg.count}/${msg.raw_count}`);
  } else {
    setText("viewerMeta", `${msg.topic} · 累计 ${formatCount(totalPoints)} · 本批 ${msg.count}/${msg.raw_count} · ${msg.rgb_status || "rgb"}`);
  }
}

function addPointObject(rows, options = {}) {
  if (!rows.length || !scene) return;
  if (options.replace) clearScene(false);
  const positions = new Float32Array(rows.length * 3);
  const colors = new Float32Array(rows.length * 3);
  for (let i = 0; i < rows.length; i++) {
    const p = rows[i];
    const v = rosToThree(p);
    positions[i * 3] = v.x;
    positions[i * 3 + 1] = v.y;
    positions[i * 3 + 2] = v.z;
    const c = displayColor(p[3], p[4], p[5]);
    colors[i * 3] = c[0] / 255;
    colors[i * 3 + 1] = c[1] / 255;
    colors[i * 3 + 2] = c[2] / 255;
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  const material = new THREE.PointsMaterial({
    size: pointRenderSize(),
    vertexColors: true,
    sizeAttenuation: true,
  });
  const object = new THREE.Points(geometry, material);
  scene.add(object);
  pointChunks.push({ object, count: rows.length });
  totalPoints += rows.length;
  hasRgb = Boolean(options.hasRgb);
  trimPointBudget();
  updateMapStats();
}

function trimPointBudget() {
  const budget = pointBudget();
  while (totalPoints > budget && pointChunks.length) {
    const chunk = pointChunks.shift();
    scene.remove(chunk.object);
    chunk.object.geometry.dispose();
    chunk.object.material.dispose();
    totalPoints -= chunk.count;
  }
}

function clearScene(showToast = true) {
  for (const chunk of pointChunks) {
    scene?.remove(chunk.object);
    chunk.object.geometry.dispose();
    chunk.object.material.dispose();
  }
  pointChunks = [];
  totalPoints = 0;
  rawPointTotal = 0;
  pathPoints = [];
  currentPose = null;
  lastPointStamp = 0;
  hasRgb = false;
  currentMapName = "";
  if (pathLine) pathLine.geometry = new THREE.BufferGeometry();
  if (poseArrow) poseArrow.visible = false;
  updateMapStats();
  if (showToast) toast("已清空网页实时缓存，不影响官方 PCD 保存");
}

function updatePath(msg) {
  pathPoints = msg.points || [];
  if (Number.isFinite(msg.yaw)) lastHeading = msg.yaw;
  updateHeadingFromPath();
  if (!currentPose && pathPoints.length) currentPose = { position: pathPoints[pathPoints.length - 1], yaw: lastHeading };
  const positions = new Float32Array(pathPoints.length * 3);
  for (let i = 0; i < pathPoints.length; i++) {
    const v = rosToThree(pathPoints[i]);
    positions[i * 3] = v.x;
    positions[i * 3 + 1] = v.y + 0.06;
    positions[i * 3 + 2] = v.z;
  }
  pathLine.geometry.dispose();
  pathLine.geometry = new THREE.BufferGeometry();
  pathLine.geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  updateMapStats();
}

function updateOdom(msg) {
  currentPose = { position: msg.position || [0, 0, 0], yaw: msg.yaw ?? lastHeading };
  if (Number.isFinite(msg.yaw)) lastHeading = msg.yaw;
  const pos = rosToThree(currentPose.position);
  poseArrow.position.copy(pos);
  poseArrow.position.y += 0.08;
  poseArrow.setDirection(headingToThreeDirection(lastHeading));
  poseArrow.visible = true;
  updateMapStats();
}

function updateHeadingFromPath() {
  if (pathPoints.length < 2 || currentPose?.yaw != null) return;
  const a = pathPoints[pathPoints.length - 2];
  const b = pathPoints[pathPoints.length - 1];
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  if (Math.hypot(dx, dy) > 0.03) lastHeading = Math.atan2(dy, dx);
}

function fitViewToPoints(rows) {
  if (!rows.length) return;
  let min = new THREE.Vector3(Infinity, Infinity, Infinity);
  let max = new THREE.Vector3(-Infinity, -Infinity, -Infinity);
  for (const p of rows) {
    const v = rosToThree(p);
    min.min(v);
    max.max(v);
  }
  cameraTarget.copy(min.clone().add(max).multiplyScalar(0.5));
  const span = max.clone().sub(min).length();
  orbitDistance = Math.max(6, Math.min(120, span * 0.45));
  fpsPosition.copy(cameraTarget).add(new THREE.Vector3(0, 1.2, Math.max(1.5, Math.min(8, span * 0.08))));
  fpsYaw = Math.PI;
  fpsPitch = 0;
}

function updateMapStats() {
  const age = lastPointStamp ? `${Math.max(0, ((Date.now() - lastPointStamp) / 1000)).toFixed(1)}s` : "-";
  const rgbText = hasRgb ? (colorBoostEnabled && sceneMode === "live" ? "RGB增强" : "RGB") : "伪彩";
  const tiltText = tiltCorrectionEnabled ? "Z-30" : "未校正";
  const modeText = sceneMode === "final" ? "最终模型" : "实时地图";
  const pointText = rawPointTotal && sceneMode === "final"
    ? `${formatCount(totalPoints)} / ${formatCount(rawPointTotal)}`
    : formatCount(totalPoints);
  setText("mapMeta", `${pointText} 点 · ${rgbText}`);
  setText("mapHud", `${modeText} · ${viewMode === "fps" ? "FPS" : viewMode === "top" ? "俯视" : "自由"} · ${followEnabled ? "跟随" : "手动"} · ${qualityMode === "pc" ? "PC" : "小主机"} · ${rgbText} · ${tiltText} · ${renderFps} fps · 延迟 ${age} · 轨迹 ${pathPoints.length}`);
  if (pointStreamMode === "lidar") {
    setText("recordMapMeta", `${formatCount(totalPoints)} 点`);
    setText("recordMapHud", `原始雷达最新帧 · ${renderFps} fps · 延迟 ${age}`);
  }
}

function updatePointMaterials() {
  const size = pointRenderSize();
  for (const chunk of pointChunks) {
    chunk.object.material.size = size;
    chunk.object.material.needsUpdate = true;
  }
  updateMapStats();
}

function setPointSizeScale(value) {
  pointSizeScale = Math.max(0.5, Math.min(4, Number(value) || 1));
  localStorage.setItem("jr.preview.pointSize", String(pointSizeScale));
  $("pointSizeScale").value = String(pointSizeScale);
  $("pointSizeValue").textContent = `${pointSizeScale.toFixed(1)}x`;
  updatePointMaterials();
}

function setViewMode(mode) {
  viewMode = mode;
  $("viewFps").classList.toggle("active", mode === "fps");
  $("viewTop").classList.toggle("active", mode === "top");
  $("viewFree").classList.toggle("active", mode === "free");
  if (mode === "fps" && currentPose?.position && sceneMode === "live") {
    followEnabled = true;
  }
  updateFollowButton();
  updateMapStats();
}

function resetView() {
  if (sceneMode === "live") {
    followEnabled = true;
    setViewMode("fps");
  } else {
    followEnabled = false;
    setViewMode("fps");
  }
  orbitYaw = 0;
  orbitPitch = 0.72;
  orbitDistance = sceneMode === "live" ? 12 : orbitDistance;
  updateMapStats();
}

function updateFollowButton() {
  const label = followEnabled && sceneMode === "live" ? "FPS跟随" : "FPS漫游";
  $("viewFps").textContent = label;
}

function setQualityMode(mode) {
  qualityMode = mode;
  localStorage.setItem("jr.preview.quality", qualityMode);
  $("toggleQuality").textContent = qualityMode === "pc" ? "PC高质量" : "小主机模式";
  trimPointBudget();
  updateMapStats();
  if (pointWs) {
    closePointStream();
    ensurePointStream();
  }
  if (cameraWs) {
    closeCameraStream();
    ensureCameraStream();
  }
}

function setColorBoost(enabled, silent = false) {
  colorBoostEnabled = Boolean(enabled);
  localStorage.setItem("jr.preview.colorBoost", colorBoostEnabled ? "on" : "off");
  $("toggleColorBoost").textContent = colorBoostEnabled ? "RGB增强" : "原始RGB";
  $("toggleColorBoost").classList.toggle("active", colorBoostEnabled);
  if (!silent && sceneMode === "live" && pointWs) {
    clearScene(false);
    closePointStream();
    ensurePointStream();
  }
  updateMapStats();
}

function setTiltCorrection(enabled, silent = false) {
  tiltCorrectionEnabled = Boolean(enabled);
  localStorage.setItem("jr.preview.tiltCorrection", tiltCorrectionEnabled ? "on" : "off");
  $("toggleTiltCorrection").textContent = tiltCorrectionEnabled ? "Z-30校正" : "未校正";
  $("toggleTiltCorrection").classList.toggle("active", tiltCorrectionEnabled);
  if (!silent && sceneMode === "live" && pointWs) {
    clearScene(false);
    closePointStream();
    ensurePointStream();
  }
  updateMapStats();
}

function fullscreenElement() {
  return document.fullscreenElement || document.webkitFullscreenElement;
}

async function toggleFullscreen() {
  const wrap = $("threeWrap");
  try {
    if (fullscreenElement()) {
      if (document.exitFullscreen) await document.exitFullscreen();
      else if (document.webkitExitFullscreen) document.webkitExitFullscreen();
    } else if (wrap.requestFullscreen) {
      await wrap.requestFullscreen();
    } else if (wrap.webkitRequestFullscreen) {
      wrap.webkitRequestFullscreen();
    }
  } catch (err) {
    toast(`全屏失败: ${err.message}`);
  }
}

function updateFullscreenButton() {
  const active = Boolean(fullscreenElement());
  $("toggleFullscreen").classList.toggle("is-fullscreen", active);
  $("toggleFullscreen").setAttribute("aria-label", active ? "退出三维全屏" : "三维全屏");
  setTimeout(resizeThree, 80);
}

function blockBrowserChrome(el) {
  if (!el) return;
  const block = (event) => {
    event.preventDefault();
    event.stopPropagation();
  };
  // Touch long-press / right-click "Save image" menus on canvas + joysticks.
  el.addEventListener("contextmenu", block);
  el.addEventListener("dragstart", block);
  el.addEventListener("selectstart", block);
}

function initThreePointer() {
  const wrap = $("threeWrap");
  const el = renderer?.domElement;
  blockBrowserChrome(wrap);
  blockBrowserChrome($("recordThreeViewport"));
  blockBrowserChrome(el);
  el.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    dragging = true;
    lastPointer = [event.clientX, event.clientY];
    el.setPointerCapture(event.pointerId);
  });
  el.addEventListener("pointermove", (event) => {
    if (!dragging || !lastPointer) return;
    const dx = event.clientX - lastPointer[0];
    const dy = event.clientY - lastPointer[1];
    if (viewMode === "top") {
      const scale = orbitDistance / Math.max(260, el.clientHeight);
      cameraTarget.x -= dx * scale;
      cameraTarget.z -= dy * scale;
    } else if (viewMode === "fps") {
      followEnabled = false;
      fpsYaw -= dx * 0.0045;
      fpsPitch = Math.max(-1.25, Math.min(1.25, fpsPitch - dy * 0.0045));
    } else {
      orbitYaw -= dx * 0.006;
      orbitPitch = Math.max(0.12, Math.min(1.45, orbitPitch + dy * 0.006));
    }
    lastPointer = [event.clientX, event.clientY];
    updateFollowButton();
    updateMapStats();
  });
  el.addEventListener("pointerup", () => {
    dragging = false;
    lastPointer = null;
  });
  el.addEventListener("pointercancel", () => {
    dragging = false;
    lastPointer = null;
  });
  el.addEventListener("wheel", (event) => {
    event.preventDefault();
    if (viewMode === "fps") {
      const dir = new THREE.Vector3(Math.sin(fpsYaw), 0, Math.cos(fpsYaw)).normalize();
      fpsPosition.addScaledVector(dir, event.deltaY > 0 ? -0.5 : 0.5);
      followEnabled = false;
    } else {
      orbitDistance = Math.max(1.5, Math.min(160, orbitDistance * (event.deltaY > 0 ? 1.12 : 0.88)));
    }
    updateFollowButton();
    updateMapStats();
  }, { passive: false });
}

function initJoysticks() {
  bindJoystick("moveStick", stickState.move);
  bindJoystick("lookStick", stickState.look);
}

function bindJoystick(id, state) {
  const root = $(id);
  const knob = root.querySelector("span");
  blockBrowserChrome(root);
  const reset = () => {
    state.x = 0;
    state.y = 0;
    state.active = false;
    state.id = null;
    knob.style.transform = "translate(-50%, -50%)";
  };
  root.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    event.stopPropagation();
    const rect = root.getBoundingClientRect();
    state.cx = rect.left + rect.width / 2;
    state.cy = rect.top + rect.height / 2;
    state.active = true;
    state.id = event.pointerId;
    root.setPointerCapture(event.pointerId);
  });
  root.addEventListener("pointermove", (event) => {
    if (!state.active || state.id !== event.pointerId) return;
    event.preventDefault();
    event.stopPropagation();
    const max = root.clientWidth * 0.34;
    const dx = Math.max(-max, Math.min(max, event.clientX - state.cx));
    const dy = Math.max(-max, Math.min(max, event.clientY - state.cy));
    state.x = dx / max;
    state.y = dy / max;
    knob.style.transform = `translate(calc(-50% + ${dx}px), calc(-50% + ${dy}px))`;
    followEnabled = false;
    updateFollowButton();
  });
  root.addEventListener("pointerup", reset);
  root.addEventListener("pointercancel", reset);
}

function updateManualFps(dt) {
  if (viewMode !== "fps") return;
  const move = stickState.move;
  const look = stickState.look;
  if (look.active) {
    fpsYaw -= look.x * dt * 1.9;
    fpsPitch = Math.max(-1.25, Math.min(1.25, fpsPitch - look.y * dt * 1.5));
  }
  if (move.active) {
    // Free-fly: forward follows look (pitch included); strafe stays level.
    const forward = new THREE.Vector3(
      Math.sin(fpsYaw) * Math.cos(fpsPitch),
      Math.sin(fpsPitch),
      Math.cos(fpsYaw) * Math.cos(fpsPitch)
    ).normalize();
    const right = new THREE.Vector3(Math.cos(fpsYaw), 0, -Math.sin(fpsYaw)).normalize();
    const speed = sceneMode === "final" ? 2.6 : 1.5;
    fpsPosition.addScaledVector(forward, -move.y * speed * dt);
    fpsPosition.addScaledVector(right, -move.x * speed * dt);
  }
}

function reconnectStreams() {
  sceneMode = "live";
  closePointStream();
  closeCameraStream();
  ensureCameraStream("default");
  ensurePointStream("mapping");
  setScanState("scanning", "扫描中");
}

function bindUi() {
  document.querySelectorAll(".tab").forEach((btn) => btn.addEventListener("click", () => showTab(btn.dataset.tab)));
  document.querySelectorAll("[data-action]").forEach((btn) => btn.addEventListener("click", () => postAction(btn.dataset.action)));
  document.querySelectorAll("[data-log]").forEach((btn) => btn.addEventListener("click", () => loadLogs(btn.dataset.log)));
  $("startScan").addEventListener("click", startScanWorkflow);
  $("finishScan").addEventListener("click", finishScanWorkflow);
  $("startRecord").addEventListener("click", startRecordWorkflow);
  $("stopRecord").addEventListener("click", stopRecordWorkflow);
  $("toggleRecordRadarPreview")?.addEventListener("click", () => setRecordRadarPreview(!recordRadarPreviewEnabled));
  $("viewFps").addEventListener("click", () => {
    followEnabled = sceneMode === "live";
    setViewMode("fps");
  });
  $("viewTop").addEventListener("click", () => {
    followEnabled = sceneMode === "live";
    setViewMode("top");
  });
  $("viewFree").addEventListener("click", () => {
    followEnabled = false;
    setViewMode("free");
  });
  $("toggleQuality").addEventListener("click", () => setQualityMode(qualityMode === "mini" ? "pc" : "mini"));
  $("toggleColorBoost").addEventListener("click", () => setColorBoost(!colorBoostEnabled));
  $("toggleTiltCorrection").addEventListener("click", () => setTiltCorrection(!tiltCorrectionEnabled));
  $("pointSizeScale").addEventListener("input", (event) => setPointSizeScale(event.target.value));
  $("resetView").addEventListener("click", resetView);
  $("clearPoints").addEventListener("click", () => clearScene(true));
  $("loadLatestMap").addEventListener("click", () => loadLatestMap().catch((err) => toast(err.message)));
  $("refreshDataMaps")?.addEventListener("click", () => refreshDataMaps());
  $("openDataPreview")?.addEventListener("click", () => openDataPreview());
  $("startOfflineMap")?.addEventListener("click", () => startOfflineMap());
  $("deleteDataScan")?.addEventListener("click", () => deleteDataScan());
  $("cancelOfflineMap")?.addEventListener("click", () => cancelOfflineMap());
  $("connectStreams").addEventListener("click", reconnectStreams);
  $("toggleFullscreen").addEventListener("click", toggleFullscreen);
  $("gsListDatasets")?.addEventListener("click", listGsDatasets);
  $("gsSyncLatest")?.addEventListener("click", syncLatestGsDataset);
  $("refreshCameraConfig")?.addEventListener("click", () => refreshCameraConfig());
  $("applyCameraAutoLimit")?.addEventListener("click", () => applyCameraConfig(true));
  $("applyCameraConfig")?.addEventListener("click", () => applyCameraConfig(true));
  $("saveCameraConfigOnly")?.addEventListener("click", () => applyCameraConfig(false));
  $("cameraSettingsToggle")?.addEventListener("click", toggleCameraSettings);
  $("cameraFullscreen")?.addEventListener("click", toggleCameraFullscreen);
  document.querySelectorAll("[data-exposure-mode]").forEach((button) => {
    button.addEventListener("click", () => applyCameraExposureMode(button.dataset.exposureMode));
  });
  ["camExposure", "camFrameRate", "camGamma", "camSaturation"].forEach((id) => {
    $(id)?.addEventListener("input", () => {
      syncCameraControlOutputs();
      setCameraConfigDirty(true);
    });
  });
  ["camAutoExposureMax", "camGainAuto", "camFrameRateEnable", "camGammaEnable", "camSaturationEnable"].forEach((id) => {
    $(id)?.addEventListener("change", () => setCameraConfigDirty(true));
  });
  document.querySelectorAll("[data-camera-preset]").forEach((btn) => {
    btn.addEventListener("click", () => applyCameraPreset(btn.dataset.cameraPreset));
  });
  window.addEventListener("resize", resizeThree);
  window.addEventListener("resize", () => {
    recordCanvasMetrics = null;
    if ($("record")?.classList.contains("active")) resizeRecordCanvas();
  });
  document.addEventListener("fullscreenchange", updateFullscreenButton);
  document.addEventListener("webkitfullscreenchange", updateFullscreenButton);
}

bindUi();
initThree();
setQualityMode(qualityMode);
setColorBoost(colorBoostEnabled, true);
setTiltCorrection(tiltCorrectionEnabled, true);
setPointSizeScale(pointSizeScale);
setScanState("idle");
setRecordState("idle");
setRecordRadarPreview(false, false);
resetRecordSensorStatus();
setViewMode("fps");
updateFullscreenButton();
refreshStatus();
statusTimer = setInterval(refreshStatus, 5000);
recordClockTimer = setInterval(updateRecordClock, 250);
