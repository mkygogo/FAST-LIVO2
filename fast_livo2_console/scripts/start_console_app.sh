#!/usr/bin/env bash
# Launch JR Scanner console as a dedicated app shell (Chromium App preferred).
set -euo pipefail

CONSOLE_URL="http://127.0.0.1:8090"
HEALTH_URL="${CONSOLE_URL}/api/status"
APP_CLASS="jr-scanner-console"
# Snap Chromium is confined: ~/.config/<custom> is not writable.
# Prefer ~/snap/chromium/common (always OK for snap) then classic ~/.config.
if [[ -d "$HOME/snap/chromium" ]] || [[ -x /snap/bin/chromium ]]; then
  PROFILE_DIR="${HOME}/snap/chromium/common/jr-scanner-console"
else
  PROFILE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/jr-scanner-console-chrome"
fi
FF_PROFILE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/jr-scanner-console-firefox"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/jr-scanner-console"
LOG_FILE="${STATE_DIR}/app.log"
WM_CLASS="${APP_CLASS}"

mkdir -p "$PROFILE_DIR" "$STATE_DIR"

log() {
  printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$LOG_FILE" 2>/dev/null || true
}

notify_err() {
  local msg="$1"
  log "ERROR: $msg"
  if command -v zenity >/dev/null 2>&1; then
    zenity --error --title="JR扫描仪控制台" --text="$msg" --width=360 2>/dev/null || true
  elif command -v notify-send >/dev/null 2>&1; then
    notify-send -u critical "JR扫描仪控制台" "$msg" 2>/dev/null || true
  else
    printf '%s\n' "$msg" >&2
  fi
}

notify_info() {
  local msg="$1"
  log "INFO: $msg"
  if command -v notify-send >/dev/null 2>&1; then
    notify-send "JR扫描仪控制台" "$msg" 2>/dev/null || true
  fi
}

resolve_chromium() {
  local c
  for c in \
    google-chrome-stable \
    google-chrome \
    chromium-browser \
    chromium \
    /snap/bin/chromium
  do
    if command -v "$c" >/dev/null 2>&1; then
      command -v "$c"
      return 0
    fi
    if [[ -x "$c" ]]; then
      printf '%s\n' "$c"
      return 0
    fi
  done
  return 1
}

resolve_firefox() {
  if command -v firefox >/dev/null 2>&1; then
    command -v firefox
    return 0
  fi
  if [[ -x /snap/bin/firefox ]]; then
    printf '%s\n' /snap/bin/firefox
    return 0
  fi
  return 1
}

wait_for_console() {
  local i
  for i in $(seq 1 40); do
    if curl -fsS --max-time 1 "$HEALTH_URL" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

existing_pid() {
  local marker="$1"
  pgrep -f -- "$marker" 2>/dev/null | head -n1 || true
}

focus_existing() {
  local pid="$1"
  if command -v wmctrl >/dev/null 2>&1; then
    if wmctrl -xa "$WM_CLASS" 2>/dev/null; then
      return 0
    fi
    local wid
    wid="$(wmctrl -l -p 2>/dev/null | awk -v p="$pid" '$3 == p { print $1; exit }' || true)"
    if [[ -n "${wid}" ]] && wmctrl -ia "$wid" 2>/dev/null; then
      return 0
    fi
  fi
  if command -v xdotool >/dev/null 2>&1; then
    if xdotool search --classname "$WM_CLASS" windowactivate 2>/dev/null; then
      return 0
    fi
    if xdotool search --pid "$pid" windowactivate 2>/dev/null; then
      return 0
    fi
  fi
  return 1
}

reuse_or_exit() {
  local marker="$1"
  local existing
  existing="$(existing_pid "$marker")"
  if [[ -z "${existing}" ]]; then
    return 1
  fi
  log "Reusing existing app pid=${existing} marker=${marker}"
  focus_existing "$existing" || log "Existing instance found but focus failed"
  exit 0
}

ensure_firefox_profile() {
  mkdir -p "$FF_PROFILE_DIR"
  # Dedicated prefs: suppress restore / default-browser / update nags for this profile only.
  cat >"${FF_PROFILE_DIR}/user.js" <<'EOF'
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("browser.sessionstore.max_resumed_crashes", 0);
user_pref("browser.sessionstore.restore_on_demand", false);
user_pref("browser.startup.page", 0);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.rights.3.shown", true);
user_pref("browser.aboutwelcome.enabled", false);
user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);
user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification", true);
user_pref("app.update.enabled", false);
user_pref("app.update.auto", false);
user_pref("browser.tabs.warnOnClose", false);
user_pref("browser.warnOnQuit", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("browser.messaging-system.whatsNewPanel.enabled", false);
user_pref("browser.discovery.enabled", false);
user_pref("extensions.htmlaboutaddons.recommendations.enabled", false);
user_pref("browser.newtabpage.activity-stream.feeds.snippets", false);
user_pref("browser.toolbars.bookmarks.visibility", "never");
user_pref("dom.disable_beforeunload", true);
EOF
}

clear_stale_chromium_locks() {
  local lock="${PROFILE_DIR}/SingletonLock"
  local existing
  existing="$(existing_pid "--user-data-dir=${PROFILE_DIR}")"
  if [[ -n "${existing}" ]]; then
    return 0
  fi
  # Previous crash left lock files with no live process.
  rm -f \
    "${PROFILE_DIR}/SingletonLock" \
    "${PROFILE_DIR}/SingletonCookie" \
    "${PROFILE_DIR}/SingletonSocket" \
    2>/dev/null || true
  if [[ -e "$lock" || -L "$lock" ]]; then
    log "WARN: could not remove stale lock ${lock}"
  fi
}

launch_chromium() {
  local browser="$1"
  reuse_or_exit "--user-data-dir=${PROFILE_DIR}" || true
  clear_stale_chromium_locks
  log "Starting Chromium app mode: ${browser} profile=${PROFILE_DIR} → ${CONSOLE_URL}"
  exec "$browser" \
    --user-data-dir="$PROFILE_DIR" \
    --class="$WM_CLASS" \
    --app="$CONSOLE_URL" \
    --no-first-run \
    --no-default-browser-check \
    --disable-session-crashed-bubble \
    --disable-infobars \
    --disable-features=Translate,InfiniteSessionRestore \
    --disable-translate \
    --check-for-update-interval=31536000 \
    --window-size=1280,800 \
    --start-maximized \
    >>"$LOG_FILE" 2>&1
}

launch_firefox() {
  local browser="$1"
  ensure_firefox_profile
  reuse_or_exit "${FF_PROFILE_DIR}" || true
  log "Starting Firefox dedicated profile: ${browser} → ${CONSOLE_URL}"
  notify_info "未检测到 Chromium，使用 Firefox 专用配置（建议安装 chromium snap）"
  # --kiosk is too hard to exit on touch; use dedicated profile + new instance.
  exec "$browser" \
    --class="$WM_CLASS" \
    --name="$WM_CLASS" \
    --profile "$FF_PROFILE_DIR" \
    --new-instance \
    --width 1280 \
    --height 800 \
    "$CONSOLE_URL" \
    >>"$LOG_FILE" 2>&1
}

if ! wait_for_console; then
  notify_err "控制台服务未就绪（${HEALTH_URL}）。请检查：sudo systemctl status fast-livo2-console.service"
  exit 1
fi

CHROMIUM="$(resolve_chromium || true)"
if [[ -n "${CHROMIUM}" ]]; then
  launch_chromium "$CHROMIUM"
fi

FIREFOX="$(resolve_firefox || true)"
if [[ -n "${FIREFOX}" ]]; then
  launch_firefox "$FIREFOX"
fi

notify_err "未找到 Chromium/Chrome/Firefox。请安装：sudo snap install chromium"
exit 1
