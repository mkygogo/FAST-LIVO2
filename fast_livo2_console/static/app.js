const $ = (id) => document.getElementById(id);

let statusTimer = null;
let ws = null;
let previewMode = "lidar";
let viewMode = "top";
let followEnabled = true;
let points = [];
let pathPoints = [];
let currentPose = null;
let lastHeading = 0;
let zoom = 42;
let rotX = -0.72;
let rotZ = -0.55;
let viewYawOffset = 0;
let panX = 0;
let panY = 0;
let dragging = false;
let lastPointer = null;
let lastTap = 0;
let autoFitDone = false;

function toast(message) {
  const box = $("toast");
  box.innerHTML = `<span class="toast-msg">${escapeHtml(message)}</span>`;
  setTimeout(() => {
    if (box.textContent === message) box.innerHTML = "";
  }, 4200);
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  }[ch]));
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

async function refreshStatus() {
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    $("serviceState").textContent = "在线";
    $("hostName").textContent = data.host || "-";
    $("memState").textContent = data.memory?.used_percent == null ? "-" : `${data.memory.used_percent}%`;
    $("netState").textContent = data.network?.enp1s0 || "-";
    $("pingState").textContent = data.network?.mid360_ping_ok ? "ping 正常" : "未通";

    const topics = data.topics || [];
    $("topicList").innerHTML = topics.length
      ? topics.map((topic) => `<span class="chip">${escapeHtml(topic)}</span>`).join("")
      : `<span class="chip">暂无 ROS topic</span>`;
    $("cameraState").textContent = topics.some((t) => t.includes("camera") || t.includes("rgb_img")) ? "检测到图像 topic" : "等待硬件";

    const running = data.running || {};
    if (running.lidar?.length) $("hzLidar").textContent = $("hzLidar").textContent === "-" ? "驱动运行中" : $("hzLidar").textContent;
    if (running.lio?.length) $("hzCloud").textContent = $("hzCloud").textContent === "-" ? "建图运行中" : $("hzCloud").textContent;
    if (running.fusion?.length) $("hzCloud").textContent = $("hzCloud").textContent === "-" ? "融合运行中" : $("hzCloud").textContent;
  } catch (err) {
    $("serviceState").textContent = "离线";
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

function showTab(id) {
  document.querySelectorAll(".tab").forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === id));
  document.querySelectorAll(".page").forEach((page) => page.classList.toggle("active", page.id === id));
  if (id === "preview") resizeCanvas();
}

function setPreviewMode(mode) {
  previewMode = mode;
  $("modeLidar").classList.toggle("active", mode === "lidar");
  $("modeMapping").classList.toggle("active", mode === "mapping");
  $("viewerMeta").textContent = mode === "lidar" ? "雷达原始点云" : "雷达建图输出";
  if (mode === "mapping") {
    setViewMode("top");
    followEnabled = true;
    centerView(false);
  }
  autoFitDone = false;
  updateFollowButton();
  if (ws) connectPreview();
}

function connectPreview() {
  if (ws) {
    ws.close();
    ws = null;
    $("connectPreview").textContent = "连接预览";
    toast("预览已断开");
    return;
  }
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${scheme}://${location.host}/ws/points?mode=${previewMode}`);
  $("connectPreview").textContent = "断开预览";
  ws.onopen = () => toast("预览连接中");
  ws.onclose = () => {
    ws = null;
    $("connectPreview").textContent = "连接预览";
    $("viewerMeta").textContent = "预览已断开";
  };
  ws.onerror = () => toast("预览连接失败");
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "points") {
      points = msg.points || [];
      if (!autoFitDone) autoFitPoints();
      $("viewerMeta").textContent = `${msg.topic} · ${msg.count}/${msg.raw_count} 点`;
      draw();
    } else if (msg.type === "path") {
      pathPoints = msg.points || [];
      if (Number.isFinite(msg.yaw)) lastHeading = msg.yaw;
      updateHeadingFromPath();
      if (!currentPose && pathPoints.length) {
        currentPose = { position: pathPoints[pathPoints.length - 1], yaw: lastHeading };
      }
      draw();
    } else if (msg.type === "odom") {
      currentPose = { position: msg.position || [0, 0, 0], yaw: msg.yaw ?? lastHeading };
      if (Number.isFinite(msg.yaw)) lastHeading = msg.yaw;
      draw();
    } else if (msg.type === "rates") {
      updateRates(msg.rates || {});
    } else if (msg.type === "status") {
      $("viewerMeta").textContent = msg.message;
    }
  };
}

function updateRates(rates) {
  if (rates["/livox/lidar"] != null) $("hzLidar").textContent = `${rates["/livox/lidar"]} Hz`;
  if (rates["/livox/imu"] != null) $("hzImu").textContent = `${rates["/livox/imu"]} Hz`;
  if (rates["/cloud_registered"] != null) $("hzCloud").textContent = `${rates["/cloud_registered"]} Hz`;
  if (rates["/path"] != null) $("hzPath").textContent = `${rates["/path"]} Hz`;
  if (rates["/aft_mapped_to_init"] != null) $("hzOdom").textContent = `${rates["/aft_mapped_to_init"]} Hz`;
}

function resizeCanvas() {
  const canvas = $("cloudCanvas");
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(rect.width * ratio));
  canvas.height = Math.max(1, Math.floor(rect.height * ratio));
  draw();
}

function viewCenter() {
  if (followEnabled && currentPose?.position && previewMode === "mapping") return currentPose.position;
  if (followEnabled && pathPoints.length && previewMode === "mapping") return pathPoints[pathPoints.length - 1];
  return [0, 0, 0];
}

function topRotation() {
  const heading = previewMode === "mapping" ? lastHeading : 0;
  return Math.PI / 2 - heading + viewYawOffset;
}

function rotate2(dx, dy, angle) {
  const ca = Math.cos(angle);
  const sa = Math.sin(angle);
  return [dx * ca - dy * sa, dx * sa + dy * ca];
}

function project(p, canvas) {
  const center = viewCenter();
  const dx = p[0] - center[0];
  const dy = p[1] - center[1];
  const dz = p[2] - center[2];

  if (viewMode === "top") {
    const [rx, ry] = rotate2(dx, dy, topRotation());
    return [
      canvas.width / 2 + panX + rx * zoom,
      canvas.height / 2 + panY - ry * zoom,
      dz,
    ];
  }

  if (viewMode === "front") {
    const [rx] = rotate2(dx, dy, viewYawOffset);
    return [
      canvas.width / 2 + panX + rx * zoom,
      canvas.height / 2 + panY - dz * zoom,
      dy,
    ];
  }

  if (viewMode === "side") {
    const [, ry] = rotate2(dx, dy, viewYawOffset);
    return [
      canvas.width / 2 + panX + ry * zoom,
      canvas.height / 2 + panY - dz * zoom,
      dx,
    ];
  }

  const sx = Math.sin(rotX), cx = Math.cos(rotX);
  const sz = Math.sin(rotZ + viewYawOffset), cz = Math.cos(rotZ + viewYawOffset);
  const x1 = dx * cz - dy * sz;
  const y1 = dx * sz + dy * cz;
  const y2 = y1 * cx - dz * sx;
  return [
    canvas.width / 2 + panX + x1 * zoom,
    canvas.height / 2 + panY - y2 * zoom,
    dz,
  ];
}

function drawGrid(ctx, canvas) {
  if (viewMode !== "top") return;
  const center = viewCenter();
  const spanMeters = Math.max(canvas.width, canvas.height) / Math.max(zoom, 1) + 4;
  const step = spanMeters > 25 ? 5 : spanMeters > 10 ? 2 : 1;
  const minX = Math.floor((center[0] - spanMeters) / step) * step;
  const maxX = Math.ceil((center[0] + spanMeters) / step) * step;
  const minY = Math.floor((center[1] - spanMeters) / step) * step;
  const maxY = Math.ceil((center[1] + spanMeters) / step) * step;

  ctx.save();
  ctx.lineWidth = 1;
  ctx.strokeStyle = "rgba(148, 163, 184, 0.22)";
  for (let x = minX; x <= maxX; x += step) {
    const a = project([x, minY, center[2]], canvas);
    const b = project([x, maxY, center[2]], canvas);
    ctx.beginPath();
    ctx.moveTo(a[0], a[1]);
    ctx.lineTo(b[0], b[1]);
    ctx.stroke();
  }
  for (let y = minY; y <= maxY; y += step) {
    const a = project([minX, y, center[2]], canvas);
    const b = project([maxX, y, center[2]], canvas);
    ctx.beginPath();
    ctx.moveTo(a[0], a[1]);
    ctx.lineTo(b[0], b[1]);
    ctx.stroke();
  }
  ctx.restore();
}

function drawAxis(ctx, canvas) {
  const center = viewCenter();
  const base = project(center, canvas);
  const xEnd = project([center[0] + 1.2, center[1], center[2]], canvas);
  const yEnd = project([center[0], center[1] + 1.2, center[2]], canvas);
  ctx.save();
  ctx.lineWidth = 4;
  ctx.strokeStyle = "#f97316";
  ctx.beginPath();
  ctx.moveTo(base[0], base[1]);
  ctx.lineTo(xEnd[0], xEnd[1]);
  ctx.stroke();
  ctx.strokeStyle = "#22c55e";
  ctx.beginPath();
  ctx.moveTo(base[0], base[1]);
  ctx.lineTo(yEnd[0], yEnd[1]);
  ctx.stroke();
  ctx.restore();
}

function drawPath(ctx, canvas) {
  if (!pathPoints.length) return;
  ctx.save();
  ctx.globalAlpha = 0.95;
  ctx.strokeStyle = "#ffcf5a";
  ctx.lineWidth = 4;
  ctx.beginPath();
  pathPoints.forEach((p, idx) => {
    const [x, y] = project(p, canvas);
    if (idx === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.restore();
}

function drawCurrentPose(ctx, canvas) {
  if (!currentPose?.position && !pathPoints.length) return;
  const pos = currentPose?.position || pathPoints[pathPoints.length - 1];
  const [x, y] = project(pos, canvas);
  const heading = lastHeading;
  const nose = [pos[0] + Math.cos(heading) * 0.55, pos[1] + Math.sin(heading) * 0.55, pos[2] || 0];
  const [nx, ny] = project(nose, canvas);
  ctx.save();
  ctx.fillStyle = "#38bdf8";
  ctx.strokeStyle = "#ffffff";
  ctx.lineWidth = 3;
  ctx.beginPath();
  ctx.arc(x, y, 9, 0, Math.PI * 2);
  ctx.fill();
  ctx.stroke();
  ctx.strokeStyle = "#38bdf8";
  ctx.lineWidth = 5;
  ctx.beginPath();
  ctx.moveTo(x, y);
  ctx.lineTo(nx, ny);
  ctx.stroke();
  ctx.restore();
}

function draw() {
  const canvas = $("cloudCanvas");
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#111820";
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  drawGrid(ctx, canvas);

  ctx.globalAlpha = 0.9;
  for (const p of points) {
    if (!p || p.length < 3) continue;
    if (p[0] === 0 && p[1] === 0 && p[2] === 0) continue;
    const [x, y, depth] = project(p, canvas);
    if (x < -20 || y < -20 || x > canvas.width + 20 || y > canvas.height + 20) continue;
    const intensity = p[3] || 0;
    const hue = previewMode === "lidar" ? 170 + Math.min(75, intensity) : 36 + Math.max(-24, Math.min(84, depth * 9));
    const size = viewMode === "top" ? 3 : 2;
    ctx.fillStyle = `hsl(${hue}, 82%, 62%)`;
    ctx.fillRect(x - size / 2, y - size / 2, size, size);
  }

  drawPath(ctx, canvas);
  drawCurrentPose(ctx, canvas);
  drawAxis(ctx, canvas);
  drawHud(ctx, canvas);
}

function drawHud(ctx, canvas) {
  const modeText = { top: "俯视", front: "前视", side: "侧视", free: "自由3D" }[viewMode];
  ctx.save();
  ctx.font = `${Math.max(14, Math.round(canvas.width / 70))}px system-ui, sans-serif`;
  ctx.fillStyle = "rgba(15, 23, 42, 0.72)";
  ctx.fillRect(12, 12, 188, 54);
  ctx.fillStyle = "#e2e8f0";
  ctx.fillText(`${modeText} · ${Math.round(zoom)} px/m`, 24, 36);
  ctx.fillStyle = followEnabled ? "#86efac" : "#fca5a5";
  ctx.fillText(followEnabled ? "跟随开启" : "手动浏览", 24, 58);
  ctx.restore();
}

function updateHeadingFromPath() {
  if (pathPoints.length < 2 || currentPose?.yaw != null) return;
  const a = pathPoints[pathPoints.length - 2];
  const b = pathPoints[pathPoints.length - 1];
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  if (Math.hypot(dx, dy) > 0.03) lastHeading = Math.atan2(dy, dx);
}

function autoFitPoints() {
  if (!points.length) return;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  const center = viewCenter();
  const angle = viewMode === "top" ? topRotation() : viewYawOffset;
  for (const p of points) {
    if (!p || p.length < 3) continue;
    if (p[0] === 0 && p[1] === 0 && p[2] === 0) continue;
    const [rx, ry] = rotate2(p[0] - center[0], p[1] - center[1], angle);
    minX = Math.min(minX, rx);
    maxX = Math.max(maxX, rx);
    minY = Math.min(minY, ry);
    maxY = Math.max(maxY, ry);
  }
  if (!Number.isFinite(minX)) return;
  const canvas = $("cloudCanvas");
  const width = Math.max(1, maxX - minX);
  const height = Math.max(1, maxY - minY);
  zoom = Math.max(12, Math.min(80, Math.min(canvas.width / (width * 1.35), canvas.height / (height * 1.35))));
  panX = 0;
  panY = 0;
  autoFitDone = true;
}

function setViewMode(mode) {
  viewMode = mode;
  document.querySelectorAll("[data-view]").forEach((btn) => btn.classList.toggle("active", btn.dataset.view === mode));
  if (mode !== "free") {
    rotX = mode === "top" ? -Math.PI / 2 : 0;
  }
  centerView(false);
  draw();
}

function centerView(showToast = true) {
  panX = 0;
  panY = 0;
  if (previewMode === "mapping") followEnabled = true;
  updateFollowButton();
  if (showToast) toast("已居中");
  draw();
}

function resetView() {
  viewMode = "top";
  followEnabled = true;
  zoom = 42;
  rotX = -0.72;
  rotZ = -0.55;
  viewYawOffset = 0;
  panX = 0;
  panY = 0;
  autoFitDone = false;
  setViewMode("top");
  updateFollowButton();
  draw();
}

function updateFollowButton() {
  const btn = $("toggleFollow");
  if (!btn) return;
  btn.classList.toggle("active", followEnabled);
  btn.textContent = followEnabled ? "跟随开" : "跟随关";
}

function fullscreenElement() {
  return document.fullscreenElement || document.webkitFullscreenElement;
}

async function toggleFullscreen() {
  const wrap = $("cloudCanvas").parentElement;
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
  const btn = $("toggleFullscreen");
  if (!btn) return;
  const active = Boolean(fullscreenElement());
  btn.classList.toggle("is-fullscreen", active);
  btn.setAttribute("aria-label", active ? "退出全屏" : "进入全屏");
  setTimeout(resizeCanvas, 80);
}

function zoomBy(multiplier) {
  zoom = Math.max(5, Math.min(160, zoom * multiplier));
  draw();
}

function rotateBy(delta) {
  viewYawOffset += delta;
  if (viewMode === "free") rotZ += delta;
  followEnabled = false;
  updateFollowButton();
  draw();
}

function initPointer() {
  const canvas = $("cloudCanvas");
  canvas.addEventListener("pointerdown", (event) => {
    const now = Date.now();
    if (now - lastTap < 320) {
      centerView();
      lastTap = 0;
      return;
    }
    lastTap = now;
    dragging = true;
    lastPointer = [event.clientX, event.clientY];
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!dragging || !lastPointer) return;
    const dx = event.clientX - lastPointer[0];
    const dy = event.clientY - lastPointer[1];
    if (viewMode === "free") {
      rotZ += dx * 0.008;
      rotX += dy * 0.008;
    } else {
      panX += dx * (window.devicePixelRatio || 1);
      panY += dy * (window.devicePixelRatio || 1);
      followEnabled = false;
      updateFollowButton();
    }
    lastPointer = [event.clientX, event.clientY];
    draw();
  });
  canvas.addEventListener("pointerup", () => {
    dragging = false;
    lastPointer = null;
  });
  canvas.addEventListener("pointercancel", () => {
    dragging = false;
    lastPointer = null;
  });
  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    zoomBy(event.deltaY > 0 ? 0.9 : 1.1);
  }, { passive: false });
}

document.querySelectorAll(".tab").forEach((btn) => btn.addEventListener("click", () => showTab(btn.dataset.tab)));
document.querySelectorAll("[data-action]").forEach((btn) => btn.addEventListener("click", () => postAction(btn.dataset.action)));
document.querySelectorAll("[data-log]").forEach((btn) => btn.addEventListener("click", () => loadLogs(btn.dataset.log)));
document.querySelectorAll("[data-view]").forEach((btn) => btn.addEventListener("click", () => setViewMode(btn.dataset.view)));

$("modeLidar").addEventListener("click", () => setPreviewMode("lidar"));
$("modeMapping").addEventListener("click", () => setPreviewMode("mapping"));
$("connectPreview").addEventListener("click", connectPreview);
$("resetView").addEventListener("click", resetView);
$("zoomIn").addEventListener("click", () => zoomBy(1.25));
$("zoomOut").addEventListener("click", () => zoomBy(0.8));
$("toggleFullscreen").addEventListener("click", toggleFullscreen);
$("rotateLeft").addEventListener("click", () => rotateBy(-Math.PI / 12));
$("rotateRight").addEventListener("click", () => rotateBy(Math.PI / 12));
$("centerView").addEventListener("click", () => centerView());
$("toggleFollow").addEventListener("click", () => {
  followEnabled = !followEnabled;
  if (followEnabled) {
    panX = 0;
    panY = 0;
  }
  updateFollowButton();
  draw();
});
$("clearPoints").addEventListener("click", () => {
  points = [];
  pathPoints = [];
  currentPose = null;
  draw();
});

window.addEventListener("resize", resizeCanvas);
document.addEventListener("fullscreenchange", updateFullscreenButton);
document.addEventListener("webkitfullscreenchange", updateFullscreenButton);
initPointer();
setViewMode("top");
updateFollowButton();
updateFullscreenButton();
resizeCanvas();
refreshStatus();
statusTimer = setInterval(refreshStatus, 5000);
