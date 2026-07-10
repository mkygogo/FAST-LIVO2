const $ = (id) => document.getElementById(id);

let statusTimer = null;
let pointWs = null;
let cameraWs = null;
let scanState = "idle";
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
  if (el) el.textContent = text;
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
  $("startScan").textContent = state === "complete" ? "重新建图" : "开始建图";
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
  document.querySelectorAll(".tab").forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === id));
  document.querySelectorAll(".page").forEach((page) => page.classList.toggle("active", page.id === id));
  if (id === "fastlivo") setTimeout(resizeThree, 60);
  if (id === "data") refreshDataMaps();
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
    list.innerHTML = `<div class="data-list-empty">暂无已保存建图<br>完成建图后会出现在此</div>`;
    return;
  }
  list.innerHTML = dataMaps.map((map) => {
    const active = map.id === selectedDataMapId ? " active" : "";
    const sub = map.saved_at || formatTime(map.mtime);
    const size = map.total_size != null ? fmtSize(map.total_size) : "";
    return `<button type="button" class="data-list-item${active}" data-map-id="${escapeHtml(map.id)}">
      ${escapeHtml(map.id)}
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
  if (!detail) return;
  if (!map) {
    detail.innerHTML = `<div class="empty"><strong>选择左侧扫描记录</strong><span>完成建图后，结果会出现在此列表</span></div>`;
    if (previewBtn) previewBtn.disabled = true;
    return;
  }
  const files = map.files || [];
  const previewFile = dataMapPreviewFile(map);
  const rows = files.length
    ? files.map((f) => `<tr><td>${escapeHtml(f.name)}</td><td>${escapeHtml(fmtSize(f.size))}</td><td>${escapeHtml(formatTime(f.mtime))}</td></tr>`).join("")
    : `<tr><td colspan="3">无白名单文件</td></tr>`;
  const metaBits = [];
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
    const res = await fetch("/api/fastlivo/maps", { cache: "no-store" });
    const data = await res.json();
    if (!res.ok || data.ok === false) throw new Error(data.message || `HTTP ${res.status}`);
    dataMaps = data.maps || [];
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
    $("topicList").innerHTML = topics.length
      ? topics.map((topic) => `<span class="chip">${escapeHtml(topic)}</span>`).join("")
      : `<span class="chip">暂无 ROS topic</span>`;
    setText("cameraState", topics.some((t) => t.includes("camera") || t.includes("rgb_img")) ? "检测到图像 topic" : "等待硬件");

    const running = data.running || {};
    if (running.lidar?.length && $("hzLidar").textContent === "-") setText("hzLidar", "驱动运行中");
    if (running.fusion?.length && scanState === "idle") {
      sceneMode = "live";
      setViewMode("fps");
      ensureCameraStream();
      ensurePointStream();
      setScanState("scanning", "扫描中");
    }
    if (!running.fusion?.length && scanState === "scanning") setScanState("idle");
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
  if (rates["/livox/lidar"] != null) setText("hzLidar", `${rates["/livox/lidar"]} Hz`);
  if (rates["/livox/imu"] != null) setText("hzImu", `${rates["/livox/imu"]} Hz`);
  if (rates["/cloud_registered"] != null) setText("hzCloud", `${rates["/cloud_registered"]} Hz`);
  if (rates["/path"] != null) setText("hzPath", `${rates["/path"]} Hz`);
  if (rates["/aft_mapped_to_init"] != null) setText("hzOdom", `${rates["/aft_mapped_to_init"]} Hz`);
  if (rates["/rgb_img"] != null || rates["/left_camera/image"] != null) {
    setText("cameraMeta", `/rgb_img ${rates["/rgb_img"] ?? "-"} Hz · /left_camera/image ${rates["/left_camera/image"] ?? "-"} Hz`);
  }
}

function closePointStream() {
  if (pointWs) {
    pointWs.close();
    pointWs = null;
  }
}

function closeCameraStream() {
  if (cameraWs) {
    cameraWs.close();
    cameraWs = null;
  }
}

function ensurePointStream() {
  if (pointWs) return;
  pointWs = new WebSocket(`${wsScheme()}://${location.host}/ws/points?mode=mapping&quality=${qualityMode}`);
  $("connectStreams").textContent = "重连预览";
  pointWs.onopen = () => setText("viewerMeta", "三维实时预览已连接");
  pointWs.onclose = () => {
    pointWs = null;
    if (sceneMode === "live") setText("viewerMeta", "三维实时预览已断开");
  };
  pointWs.onerror = () => toast("三维连接失败");
  pointWs.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "points") addLivePointBatch(msg);
    else if (msg.type === "path") updatePath(msg);
    else if (msg.type === "odom") updateOdom(msg);
    else if (msg.type === "rates") updateRates(msg.rates || {});
    else if (msg.type === "status") setText("viewerMeta", msg.message);
  };
}

function ensureCameraStream() {
  if (cameraWs) return;
  cameraWs = new WebSocket(`${wsScheme()}://${location.host}/ws/camera?quality=${qualityMode}`);
  cameraWs.onopen = () => setText("cameraMeta", "视频连接中");
  cameraWs.onclose = () => {
    cameraWs = null;
    setText("cameraMeta", "视频已断开");
  };
  cameraWs.onerror = () => toast("视频连接失败");
  cameraWs.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "image") {
      const img = $("cameraImage");
      img.src = `data:image/jpeg;base64,${msg.data}`;
      img.style.display = "block";
      $("cameraEmpty").style.display = "none";
      setText("cameraMeta", `${msg.topic} · ${msg.width}x${msg.height}`);
    } else if (msg.type === "rates") {
      updateRates(msg.rates || {});
    } else if (msg.type === "status") {
      setText("cameraMeta", msg.message);
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
    ensureCameraStream();
    ensurePointStream();
    setScanState("scanning", "扫描中");
    toast("建图已开始");
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
  const rect = $("threeViewport").getBoundingClientRect();
  renderer.setSize(Math.max(1, Math.floor(rect.width)), Math.max(1, Math.floor(rect.height)), false);
  camera.aspect = Math.max(1, rect.width) / Math.max(1, rect.height);
  camera.updateProjectionMatrix();
}

function renderLoop(now) {
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
  rawPointTotal += msg.raw_count || 0;
  addPointObject(msg.points || [], { hasRgb: Boolean(msg.has_rgb), replace: false });
  lastPointStamp = Date.now();
  setText("viewerMeta", `${msg.topic} · 累计 ${formatCount(totalPoints)} · 本批 ${msg.count}/${msg.raw_count} · ${msg.rgb_status || "rgb"}`);
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
  const el = $("threeViewport");
  blockBrowserChrome(wrap);
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
  ensureCameraStream();
  ensurePointStream();
  setScanState("scanning", "扫描中");
}

function bindUi() {
  document.querySelectorAll(".tab").forEach((btn) => btn.addEventListener("click", () => showTab(btn.dataset.tab)));
  document.querySelectorAll("[data-action]").forEach((btn) => btn.addEventListener("click", () => postAction(btn.dataset.action)));
  document.querySelectorAll("[data-log]").forEach((btn) => btn.addEventListener("click", () => loadLogs(btn.dataset.log)));
  $("startScan").addEventListener("click", startScanWorkflow);
  $("finishScan").addEventListener("click", finishScanWorkflow);
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
  $("connectStreams").addEventListener("click", reconnectStreams);
  $("toggleFullscreen").addEventListener("click", toggleFullscreen);
  $("gsListDatasets")?.addEventListener("click", listGsDatasets);
  $("gsSyncLatest")?.addEventListener("click", syncLatestGsDataset);
  window.addEventListener("resize", resizeThree);
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
setViewMode("fps");
updateFullscreenButton();
refreshStatus();
statusTimer = setInterval(refreshStatus, 5000);
