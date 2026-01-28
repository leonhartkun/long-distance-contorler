import asyncio
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Any, Dict

import pyautogui
import websockets
from flask import Flask, jsonify, make_response, render_template, request
from logging.handlers import RotatingFileHandler

# ====== 新增：预览截图依赖（不影响原功能）======
import base64
from io import BytesIO
import mss
from PIL import Image
from typing import Set

# ================== logging（控制台干净 + 文件全量） ==================
base_dir = os.path.dirname(os.path.abspath(__file__))
log_file = os.path.join(base_dir, "logs/ldc.log")

logger = logging.getLogger("ldc")
logger.setLevel(logging.INFO)

formatter = logging.Formatter(
    "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)

file_handler = RotatingFileHandler(
    log_file,
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(formatter)

logger.handlers.clear()
logger.addHandler(console_handler)
logger.addHandler(file_handler)
logger.propagate = False

# 降低第三方噪音（可按需调）
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

# ================== 配置 ==================
http_host = "0.0.0.0"
http_port = 8888

ws_host = "0.0.0.0"
ws_port = 8765

shared_token = "123456"

move_gain = 1.6
scroll_gain = 1.0

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0

# ====== 新增：预览配置（不影响原输入）======
preview_size = 500
preview_fps = 24
preview_jpeg_quality = 65
preview_only_when_client = True

logger.info("preview_size: %s", preview_size)
logger.info("preview_fps: %s", preview_fps)

app = Flask(__name__)

button_list = [
    {"id": "screen_off", "text": "熄屏"},
    {"id": "up", "text": "↑"},
    {"id": "screen_on", "text": "亮屏"},
    {"id": "seek_forward", "text": "←"},
    {"id": "toggle_pause", "text": "␣"},
    {"id": "seek_backward", "text": "→"},
    {"id": "volume_up", "text": "音量+"},
    {"id": "down", "text": "↓"},
    {"id": "volume_down", "text": "音量-"},
    {"id": "brightness_up", "text": "亮度+"},
    # 需要“按住⌘Tab浏览”就加这些按钮：
    {"id": "cmd_hold_on", "text": "⌘按住"},
    {"id": "brightness_down", "text": "亮度-"},
    {"id": "tab_prev", "text": "←Tab"},
    {"id": "cmd_hold_off", "text": "⌘松开"},
    {"id": "tab_next", "text": "Tab→"},
]

# ================== 系统动作工具 ==================
def run_cmd(cmd_list: list[str]) -> None:
    try:
        subprocess.run(cmd_list, check=False)
    except Exception:
        logger.exception("subprocess failed: %s", cmd_list)


def screen_off() -> None:
    # conda 环境 path 可能怪，写死更稳
    run_cmd(["/usr/bin/pmset", "displaysleepnow"])


def screen_on() -> None:
    # 唤醒显示器：不睡眠、不抢前台
    run_cmd(["/usr/bin/caffeinate", "-u", "-t", "1"])
    pyautogui.press("enter")
    time.sleep(0.1)
    for i in "251013":
        pyautogui.press(i)
        time.sleep(0.1)
    pyautogui.press("enter")


def brightness_up():
    subprocess.run(["osascript", "-e",
        'tell application "System Events" to key code 144'
    ], check=False)

def brightness_down():
    subprocess.run(["osascript", "-e",
        'tell application "System Events" to key code 145'
    ], check=False)

def volume_delta(delta: int) -> None:
    script = f"""
    set v to output volume of (get volume settings)
    set v to v + ({int(delta)})
    if v < 0 then set v to 0
    if v > 100 then set v to 100
    set volume output volume v
    """
    run_cmd(["osascript", "-e", script])


def media_play_pause() -> None:
    pyautogui.press("space")
    #run_cmd(["osascript", "-e", 'tell application "System Events" to key code 16 using command down'])


def media_next() -> None:
    pyautogui.press("right")
    #run_cmd(["osascript", "-e", 'tell application "System Events" to key code 17 using command down'])


def media_prev() -> None:
    pyautogui.press("left")
    #run_cmd(["osascript", "-e", 'tell application "System Events" to key code 18 using command down'])


# ================== “按住⌘ + Tab 浏览 + 松开⌘”状态机（可选） ==================
cmd_hold_active = False
cmd_hold_last_ts = 0.0
cmd_hold_timeout_s = 8.0


def cmd_hold_on() -> None:
    global cmd_hold_active, cmd_hold_last_ts
    if not cmd_hold_active:
        pyautogui.keyDown("command")
        cmd_hold_active = True
        logger.info("cmd_hold_on")
    cmd_hold_last_ts = time.time()


def cmd_hold_off() -> None:
    global cmd_hold_active
    if cmd_hold_active:
        pyautogui.keyUp("command")
        cmd_hold_active = False
        logger.info("cmd_hold_off")


def cmd_tab_next() -> None:
    cmd_hold_on()
    pyautogui.press("tab")
    logger.debug("cmd_tab_next")


def cmd_tab_prev() -> None:
    cmd_hold_on()
    pyautogui.keyDown("shift")
    pyautogui.press("tab")
    pyautogui.keyUp("shift")
    logger.debug("cmd_tab_prev")


def cmd_hold_watchdog() -> None:
    while True:
        time.sleep(0.5)
        if cmd_hold_active and (time.time() - cmd_hold_last_ts) > cmd_hold_timeout_s:
            logger.warning("cmd_hold timeout -> auto release")
            cmd_hold_off()


threading.Thread(target=cmd_hold_watchdog, daemon=True).start()

# ================== Flask ==================
@app.get("/")
def index():
    _ = request.args.get("v", "")
    resp = make_response(render_template("index.html", token=shared_token, ws_port=ws_port, button_list=button_list))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.post("/trigger")
def trigger():
    payload = request.get_json(silent=True) or {}
    action_id = str(payload.get("action_id", "")).strip()
    logger.info("trigger action=%s", action_id)

    try:
        if action_id == "seek_forward":
            media_next()
        elif action_id == "seek_backward":
            media_prev()
        elif action_id == "up":
            pyautogui.press("up")
        elif action_id == "down":
            pyautogui.press("down")
        elif action_id == "toggle_pause":
            media_play_pause()

        elif action_id == "screen_off":
            screen_off()
        elif action_id == "screen_on":
            screen_on()

        elif action_id == "volume_up":
            volume_delta(+5)
        elif action_id == "volume_down":
            volume_delta(-5)

        elif action_id == "cmd_hold_on":
            cmd_hold_on()
        elif action_id == "cmd_hold_off":
            cmd_hold_off()
        elif action_id == "tab_next":
            cmd_tab_next()
        elif action_id == "tab_prev":
            cmd_tab_prev()
        elif action_id == "brightness_up":
            brightness_up()
        elif action_id == "brightness_down":
            brightness_down()
        else:
            logger.warning("unknown action=%s", action_id)

        return jsonify({"ok": True, "action_id": action_id})
    except Exception:
        logger.exception("trigger failed action=%s", action_id)
        return jsonify({"ok": False, "action_id": action_id}), 500


# ================== WS -> queue -> pyautogui（防崩） ==================
event_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=3000)


def enqueue_event(data: Dict[str, Any]) -> None:
    try:
        event_queue.put_nowait(data)
    except queue.Full:
        logger.warning("event_queue full, drop event type=%s", data.get("type"))


def mouse_worker() -> None:
    logger.info("mouse_worker started")
    while True:
        data = event_queue.get()
        try:
            event_type = data.get("type")

            if event_type in ("ping", "hello"):
                continue

            if event_type == "move":
                dx = float(data.get("dx", 0.0)) * move_gain
                dy = float(data.get("dy", 0.0)) * move_gain
                pyautogui.moveRel(dx, dy, duration=0)

            elif event_type == "click":
                pyautogui.click()

            elif event_type == "double_click":
                logger.info("double_click event_type=%s", event_type)
                pyautogui.click()
                time.sleep(0.01)
                pyautogui.click()

            elif event_type == "right_click":
                pyautogui.click(button="right")

            elif event_type == "scroll":
                dy = float(data.get("dy", 0.0)) * scroll_gain
                pyautogui.scroll(int(dy))

            else:
                logger.debug("unknown ws event_type=%s", event_type)

        except Exception:
            logger.exception("mouse_worker error")
        finally:
            event_queue.task_done()


# ====== 新增：WS 客户端集合 + 预览广播（不影响原输入）======
connected_clients: Set[websockets.WebSocketServerProtocol] = set()
clients_lock = threading.Lock()
ws_loop: asyncio.AbstractEventLoop | None = None


def capture_mouse_area_b64(size: int):
    """
    return: (b64_jpeg, cursor_x, cursor_y) or (None, None, None)
    cursor_x/cursor_y 是鼠标在截图窗口内的像素坐标 [0, size)
    """
    try:
        x, y = pyautogui.position()
        half = size // 2

        with mss.mss() as sct:
            v = sct.monitors[0]
            min_left = int(v["left"])
            min_top = int(v["top"])
            max_right = int(v["left"] + v["width"])
            max_bottom = int(v["top"] + v["height"])

            left = int(x - half)
            top = int(y - half)

            if left < min_left:
                left = min_left
            if top < min_top:
                top = min_top

            if left + size > max_right:
                left = max_right - size
            if top + size > max_bottom:
                top = max_bottom - size

            monitor = {"left": left, "top": top, "width": size, "height": size}
            shot = sct.grab(monitor)

        img = Image.frombytes("RGB", shot.size, shot.rgb)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=preview_jpeg_quality, optimize=True)

        # 鼠标在截图窗口中的位置（关键）
        cx = int(x - left)
        cy = int(y - top)

        # clamp 到 [0, size-1] 防止极端边界抖动
        if cx < 0:
            cx = 0
        elif cx >= size:
            cx = size - 1

        if cy < 0:
            cy = 0
        elif cy >= size:
            cy = size - 1

        return base64.b64encode(buf.getvalue()).decode("ascii"), cx, cy

    except Exception:
        logger.exception("capture_mouse_area failed")
        return None, None, None


def broadcast_preview(payload: str) -> None:
    global ws_loop
    if ws_loop is None:
        return

    with clients_lock:
        clients = list(connected_clients)

    if not clients:
        return

    async def _send_all() -> None:
        dead = []
        for c in clients:
            try:
                await c.send(payload)
            except Exception:
                dead.append(c)

        if dead:
            with clients_lock:
                for d in dead:
                    connected_clients.discard(d)

    try:
        asyncio.run_coroutine_threadsafe(_send_all(), ws_loop)
    except Exception:
        # 预览失败不影响主功能
        pass


def preview_worker() -> None:
    logger.info("preview_worker started size=%s fps=%s", preview_size, preview_fps)
    interval = 1.0 / max(1, int(preview_fps))

    while True:
        try:
            if preview_only_when_client:
                with clients_lock:
                    if not connected_clients:
                        time.sleep(0.3)
                        continue

            img_b64, cx, cy = capture_mouse_area_b64(preview_size)
            if img_b64:
                payload = json.dumps({
                    "type": "preview",
                    "image": img_b64,
                    "size": preview_size,
                    "cx": cx,
                    "cy": cy,
                })
                broadcast_preview(payload)

        except Exception:
            # 预览线程不许拖死主功能
            logger.exception("preview_worker error")

        time.sleep(interval)


# ================== WebSocket server ==================
async def ws_handler(websocket):
    logger.info("ws client connected")

    # 新增：登记客户端（不改你原逻辑）
    with clients_lock:
        connected_clients.add(websocket)

    try:
        async for message in websocket:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                logger.debug("ws json decode failed")
                continue

            if not isinstance(data, dict):
                continue

            if data.get("token") != shared_token:
                logger.warning("ws bad token")
                continue

            enqueue_event(data)

    except websockets.ConnectionClosed:
        pass
    except Exception:
        logger.exception("ws handler error")
    finally:
        # 新增：移除客户端
        with clients_lock:
            connected_clients.discard(websocket)

        logger.info("ws client disconnected")


async def start_ws_server() -> None:
    global ws_loop
    ws_loop = asyncio.get_running_loop()

    logger.info("ws listening on ws://%s:%s/", ws_host, ws_port)
    async with websockets.serve(ws_handler, ws_host, ws_port, max_size=2**20):
        await asyncio.Future()


def start_http_server() -> None:
    logger.info("http listening on http://%s:%s/", http_host, http_port)
    app.run(host=http_host, port=http_port, debug=False, use_reloader=False)


def main() -> None:
    threading.Thread(target=mouse_worker, daemon=True).start()

    # 新增：预览线程（完全独立，不碰输入队列）
    threading.Thread(target=preview_worker, daemon=True).start()

    threading.Thread(target=start_http_server, daemon=True).start()
    asyncio.run(start_ws_server())


if __name__ == "__main__":
    main()