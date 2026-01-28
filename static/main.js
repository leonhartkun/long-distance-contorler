const app_cfg = window.app_config || {};
const token = app_cfg.token;
const ws_port = app_cfg.ws_port;

const status_el = document.getElementById("status");
const debug_el = document.getElementById("debug");
const debug2_el = document.getElementById("debug2");
const debug3_el = document.getElementById("debug3");
const trigger_status_el = document.getElementById("trigger_status");

const pad_el = document.getElementById("pad_area");

// 新增：预览 img
const preview_img = document.getElementById("mouse_preview");

function set_status(t) { if (status_el) status_el.textContent = t; }
function set_debug(t) { if (debug_el) debug_el.textContent = t; }
function set_debug2(t) { if (debug2_el) debug2_el.textContent = t; }
function set_debug3(t) { if (debug3_el) debug3_el.textContent = t; }
function set_trigger_status(t) { if (trigger_status_el) trigger_status_el.textContent = t; }

const ws_url = `ws://${location.hostname}:${ws_port}/`;
set_debug(`page: ${location.href}`);
set_debug2(`ws:   ${ws_url}`);

let ws = null;
let heartbeat_timer = null;
let connect_timeout_timer = null;
let reconnect_timer = null;

const connect_timeout_ms = 2500;

let retry_count = 0;
const fast_retry_limit = 5;
const fast_retry_delay_ms = 150;
const slow_retry_delay_ms = 900;
const slow_retry_delay_max_ms = 3500;

let last_connect_try_ms = 0;
const min_connect_interval_ms = 250;

function stop_heartbeat() {
  if (heartbeat_timer) {
    clearInterval(heartbeat_timer);
    heartbeat_timer = null;
  }
}

function start_heartbeat() {
  heartbeat_timer = setInterval(() => {
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({ token, type: "ping", t: Date.now() }));
    }
  }, 15000);
}

function clear_timers() {
  if (connect_timeout_timer) {
    clearTimeout(connect_timeout_timer);
    connect_timeout_timer = null;
  }
  if (reconnect_timer) {
    clearTimeout(reconnect_timer);
    reconnect_timer = null;
  }
}

function schedule_reconnect(reason) {
  clear_timers();
  stop_heartbeat();

  const delay = (retry_count < fast_retry_limit)
    ? fast_retry_delay_ms
    : Math.min(
        slow_retry_delay_ms + (retry_count - fast_retry_limit) * 300,
        slow_retry_delay_max_ms
      );

  retry_count += 1;
  set_status(`disconnected ❌ (${reason}) retry in ${delay}ms`);
  set_debug3(`state: try=${retry_count} net=${navigator.onLine ? "online" : "offline"}`);

  reconnect_timer = setTimeout(() => {
    connect_ws("reconnect_timer");
  }, delay);
}

function connect_ws(trigger) {
  const now = Date.now();
  if (now - last_connect_try_ms < min_connect_interval_ms) return;
  last_connect_try_ms = now;

  clear_timers();

  try {
    if (ws) {
      ws.onopen = ws.onclose = ws.onerror = ws.onmessage = null;
      ws.close();
    }
  } catch (e) {}

  set_status(`connecting… (${trigger})`);
  set_debug3(`state: try=${retry_count} net=${navigator.onLine ? "online" : "offline"}`);

  ws = new WebSocket(ws_url);

  connect_timeout_timer = setTimeout(() => {
    if (!ws) return;
    if (ws.readyState === 0) { // CONNECTING
      try { ws.close(); } catch (e) {}
      schedule_reconnect("connect_timeout");
    }
  }, connect_timeout_ms);

  ws.onopen = () => {
    clear_timers();
    retry_count = 0;
    set_status("connected ✅");
    stop_heartbeat();
    start_heartbeat();

    // 立即发 hello，防 Safari / 路由器把空闲 ws 回收
    ws.send(JSON.stringify({ token, type: "hello", t: Date.now() }));
  };

  ws.onclose = () => schedule_reconnect("onclose");

  ws.onerror = () => {
    try { ws.close(); } catch (e) {}
    schedule_reconnect("onerror");
  };

  // 新增：接收预览帧
  ws.onmessage = (ev) => {
    let data = null;
    try { data = JSON.parse(ev.data); } catch (e) { return; }
    if (!data || typeof data !== "object") return;

    if (data.type === "preview" && data.image && preview_img) {
      preview_img.src = "data:image/jpeg;base64," + data.image;
    }
  };
}

// 多触发点：不靠刷新
window.addEventListener("DOMContentLoaded", () => connect_ws("domcontentloaded"));
window.addEventListener("load", () => connect_ws("load"));
window.addEventListener("pageshow", () => connect_ws("pageshow"));
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) connect_ws("visibility_show");
});
window.addEventListener("online", () => connect_ws("online"));
window.addEventListener("focus", () => connect_ws("focus"));

setInterval(() => {
  if (!ws) return;
  if (ws.readyState === 0) connect_ws("watchdog_connecting");
}, 5000);

function send_event(obj) {
  if (ws && ws.readyState === 1) {
    ws.send(JSON.stringify({ token, ...obj }));
  }
}

/* ================== 按钮：POST /trigger ================== */
async function post_trigger(action_id) {
  try {
    set_trigger_status(`trigger: ${action_id} ...`);
    const resp = await fetch("/trigger", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action_id })
    });
    const data = await resp.json();
    if (data && data.ok) {
      set_trigger_status(`ok: ${data.action_id}`);
    } else {
      set_trigger_status("trigger failed");
    }
  } catch (e) {
    set_trigger_status(`trigger error: ${String(e)}`);
  }
}

document.querySelectorAll(".action_btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const action_id = btn.getAttribute("data_action_id") || "";
    post_trigger(action_id);
  });
});

/* ================== 触摸板（下半屏 pad_area） ================== */
/* 单指点按=左键；单指双击=双击；双指点按=右键；双指滑动=滚动；单指拖动=移动 */

let last_x = null;
let last_y = null;

let moved = false;
let active = new Map();

// 双指点按识别
let two_finger_down = false;
let two_finger_start_ms = 0;
let two_finger_moved = false;
let last_center_y = null;

const tap_move_threshold = 6;
const two_finger_tap_max_ms = 250;

// ===== 单指双击检测（自实现，移动端稳定）=====
let last_tap_ms = 0;
let last_tap_x = 0;
let last_tap_y = 0;

const double_tap_max_ms = 320;   // 两次点击最大间隔
const double_tap_max_dist = 18;  // 两次点击允许最大位移（像素）

function get_center_point() {
  const pts = Array.from(active.values());
  if (pts.length < 2) return null;
  return {
    x: (pts[0].x + pts[1].x) / 2,
    y: (pts[0].y + pts[1].y) / 2
  };
}

// pointercancel 收口：iOS 偶发 cancel，不处理会“卡状态”
function reset_touch_state() {
  last_x = null;
  last_y = null;
  moved = false;

  active.clear();

  two_finger_down = false;
  two_finger_moved = false;
  last_center_y = null;
}

if (pad_el) {
  pad_el.addEventListener("pointerdown", (e) => {
    pad_el.setPointerCapture(e.pointerId);
    active.set(e.pointerId, { x: e.clientX, y: e.clientY });

    if (active.size === 1) {
      last_x = e.clientX;
      last_y = e.clientY;
      moved = false;
    }

    if (active.size === 2) {
      two_finger_down = true;
      two_finger_start_ms = Date.now();
      two_finger_moved = false;

      const c = get_center_point();
      last_center_y = c ? c.y : null;
    }

    e.preventDefault();
  }, { passive: false });

  pad_el.addEventListener("pointermove", (e) => {
    if (!active.has(e.pointerId)) return;
    active.set(e.pointerId, { x: e.clientX, y: e.clientY });

    if (active.size === 1) {
      const dx = e.clientX - last_x;
      const dy = e.clientY - last_y;

      if (Math.abs(dx) + Math.abs(dy) > 1) moved = true;

      last_x = e.clientX;
      last_y = e.clientY;

      send_event({ type: "move", dx, dy });

    } else if (active.size === 2) {
      const c = get_center_point();
      if (!c) return;

      if (last_center_y === null) last_center_y = c.y;
      const dy = c.y - last_center_y;
      last_center_y = c.y;

      if (Math.abs(dy) > tap_move_threshold) {
        two_finger_moved = true;
      }

      // 只有明显在滑动才发送滚动，避免轻微抖动误触
      if (Math.abs(dy) > 1) {
        send_event({ type: "scroll", dy: -dy * 3 });
      }
    }

    e.preventDefault();
  }, { passive: false });

  pad_el.addEventListener("pointerup", (e) => {
    const was_size = active.size; // 抬起前的手指数
    active.delete(e.pointerId);

    // ===== 双指点按 -> 右键 =====
    if (was_size === 2 && two_finger_down) {
      const dt = Date.now() - two_finger_start_ms;

      if (!two_finger_moved && dt <= two_finger_tap_max_ms) {
        send_event({ type: "right_click" });
      }

      two_finger_down = false;
      two_finger_moved = false;
      last_center_y = null;

      // 防止误触发单指 click/double_click
      moved = true;
    }

    // ===== 单指点按/双击 =====
    if (was_size === 1 && !moved) {
      const now = Date.now();

      const dt = now - last_tap_ms;
      const dx = (last_x ?? 0) - last_tap_x;
      const dy = (last_y ?? 0) - last_tap_y;
      const dist2 = dx * dx + dy * dy;
      const max_dist2 = double_tap_max_dist * double_tap_max_dist;

      if (dt > 0 && dt <= double_tap_max_ms && dist2 <= max_dist2) {
        // 命中双击：发 double_click，清零，避免三连击怪相
        send_event({ type: "double_click" });
        last_tap_ms = 0;
      } else {
        // 单击延迟发送：给第二下留窗口
        last_tap_ms = now;
        last_tap_x = last_x ?? 0;
        last_tap_y = last_y ?? 0;

        setTimeout(() => {
          if (last_tap_ms === now) {
            send_event({ type: "click" });
          }
        }, double_tap_max_ms + 10);
      }
    }

    // 全部抬起：重置
    if (active.size === 0) {
      last_x = null;
      last_y = null;
      moved = false;

      two_finger_down = false;
      two_finger_moved = false;
      last_center_y = null;
    }

    e.preventDefault();
  }, { passive: false });

  pad_el.addEventListener("pointercancel", (e) => {
    reset_touch_state();
    e.preventDefault();
  }, { passive: false });

  pad_el.addEventListener("contextmenu", (e) => e.preventDefault());
}