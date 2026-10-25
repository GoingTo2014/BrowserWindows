from __future__ import annotations

import asyncio
import hmac
import json
import os
import queue
import secrets
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, redirect, render_template, request, session, url_for
from flask_sock import Sock
from pynput import keyboard, mouse
import av
import numpy as np
from aiortc import RTCPeerConnection, RTCConfiguration, RTCIceServer, RTCSessionDescription, RTCRtpSender
from aiortc.mediastreams import MediaStreamError, VideoStreamTrack

ROOT = Path(__file__).resolve().parent
PASSWORD = os.environ.get("DESKTOP_PASSWORD", "")
APP_SECRET = os.environ.get("APP_SECRET") or secrets.token_urlsafe(48)
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"

if os.name != "nt":
    raise RuntimeError("BrowserWindows currently requires Windows.")
if not PASSWORD:
    raise RuntimeError("Set DESKTOP_PASSWORD before starting BrowserWindows.")

app = Flask(__name__, template_folder="web")
app.secret_key = APP_SECRET
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
    PERMANENT_SESSION_LIFETIME=3600,
)
sock = Sock(app)

# A dedicated event loop is required because Flask's request loop ends after each response.
rtc_loop = asyncio.new_event_loop()
rtc_peers: set[RTCPeerConnection] = set()
rtc_peers_lock = threading.Lock()

def _run_rtc_loop():
    asyncio.set_event_loop(rtc_loop)
    rtc_loop.run_forever()

threading.Thread(target=_run_rtc_loop, name="webrtc-loop", daemon=True).start()

failed_logins: dict[str, tuple[int, float]] = {}
login_lock = threading.Lock()


@app.before_request
def require_login():
    if request.path == "/login" or request.endpoint == "static":
        return None
    if not session.get("authenticated"):
        if request.path == "/ws":
            return "Authentication required", 401
        return redirect(url_for("login"))
    return None


@app.get("/login")
def login():
    return render_template("login.html", error=None)


@app.post("/login")
def login_post():
    ip = request.remote_addr or "unknown"
    now = time.time()
    with login_lock:
        attempts, locked_until = failed_logins.get(ip, (0, 0.0))
        if locked_until > now:
            return render_template("login.html", error="Too many attempts. Wait a minute and try again."), 429

    supplied = request.form.get("password", "")
    if hmac.compare_digest(supplied.encode("utf-8"), PASSWORD.encode("utf-8")):
        with login_lock:
            failed_logins.pop(ip, None)
        session.clear()
        session["authenticated"] = True
        session.permanent = True
        return redirect(url_for("index"))

    with login_lock:
        attempts, _ = failed_logins.get(ip, (0, 0.0))
        attempts += 1
        failed_logins[ip] = (attempts, now + 60 if attempts >= 8 else 0.0)
    return render_template("login.html", error="Incorrect password."), 401


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def index():
    return render_template("index.html")


def read_exact(stream, count: int) -> bytes | None:
    parts = []
    remaining = count
    while remaining:
        part = stream.read(remaining)
        if not part:
            return None
        parts.append(part)
        remaining -= len(part)
    return b"".join(parts)


def read_mp4_box(stream) -> tuple[bytes, bytes] | None:
    header = read_exact(stream, 8)
    if not header:
        return None
    size = int.from_bytes(header[:4], "big")
    kind = header[4:8]
    header_size = 8
    if size == 1:
        extended = read_exact(stream, 8)
        if not extended:
            return None
        size = int.from_bytes(extended, "big")
        header += extended
        header_size = 16
    elif size == 0:
        raise RuntimeError("Unexpected unbounded MP4 box from FFmpeg.")
    if size < header_size or size > 64 * 1024 * 1024:
        raise RuntimeError(f"Invalid MP4 box size: {size}")
    payload = read_exact(stream, size - header_size)
    if payload is None:
        return None
    return kind, header + payload


class StreamState:
    def __init__(self):
        self.init_ready = threading.Event()
        self.init_segment: bytes | None = None
        self.fragments: queue.Queue[bytes] = queue.Queue(maxsize=8)
        self.error: str | None = None
        self.process: subprocess.Popen | None = None


def enqueue_latest(target: queue.Queue[bytes], fragment: bytes) -> None:
    try:
        target.put_nowait(fragment)
        return
    except queue.Full:
        pass
    try:
        target.get_nowait()
    except queue.Empty:
        pass
    try:
        target.put_nowait(fragment)
    except queue.Full:
        pass


def read_ffmpeg_output(state: StreamState) -> None:
    init_boxes: list[bytes] = []
    fragment_boxes: list[bytes] = []
    in_fragments = False
    try:
        assert state.process is not None and state.process.stdout is not None
        while True:
            item = read_mp4_box(state.process.stdout)
            if item is None:
                break
            kind, box = item
            if not state.init_ready.is_set():
                init_boxes.append(box)
                if kind == b"moov":
                    state.init_segment = b"".join(init_boxes)
                    state.init_ready.set()
                continue

            if kind == b"moof":
                fragment_boxes = [box]
                in_fragments = True
            elif in_fragments:
                fragment_boxes.append(box)
                if kind == b"mdat":
                    enqueue_latest(state.fragments, b"".join(fragment_boxes))
                    fragment_boxes = []
                    in_fragments = False
    except Exception as exc:
        state.error = str(exc)
    finally:
        state.init_ready.set()


def ffmpeg_command() -> list[str]:
    # Fragmented MP4 over a WebSocket keeps the video path on HTTPS/TCP.
    # That works through a normal HTTP tunnel; it does not need inbound UDP.
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "gdigrab", "-draw_mouse", "1", "-framerate", "30", "-i", "desktop",
        "-an",
        "-vf", "scale='min(1280,iw)':-2:flags=fast_bilinear",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-profile:v", "baseline", "-level", "3.0",
        "-pix_fmt", "yuv420p", "-r", "30", "-g", "30", "-keyint_min", "30",
        "-sc_threshold", "0", "-bf", "0",
        "-movflags", "+empty_moov+default_base_moof+frag_keyframe",
        "-frag_duration", "200000", "-flush_packets", "1",
        "-f", "mp4", "pipe:1",
    ]


class WindowsDesktopTrack(VideoStreamTrack):
    """Capture the Windows desktop with FFmpeg and hand frames to aiortc's H.264 encoder."""
    def __init__(self):
        super().__init__()
        import ctypes
        user32 = ctypes.windll.user32
        source_width = max(2, int(user32.GetSystemMetrics(0)))
        source_height = max(2, int(user32.GetSystemMetrics(1)))
        scale = min(1.0, 1280.0 / source_width)
        self.width = max(2, int(source_width * scale) // 2 * 2)
        self.height = max(2, int(source_height * scale) // 2 * 2)
        self.process = subprocess.Popen(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "gdigrab", "-draw_mouse", "1", "-framerate", "30",
                "-i", "desktop",
                "-vf", f"scale={self.width}:{self.height}:flags=fast_bilinear,format=bgr24",
                "-an", "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1",
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0,
        )

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        if self.process.stdout is None:
            raise MediaStreamError
        raw = await asyncio.to_thread(read_exact, self.process.stdout, self.width * self.height * 3)
        if raw is None:
            raise MediaStreamError
        array = np.frombuffer(raw, dtype=np.uint8).reshape((self.height, self.width, 3))
        frame = av.VideoFrame.from_ndarray(array, format="bgr24")
        frame.pts = pts
        frame.time_base = time_base
        return frame

    def stop(self):
        super().stop()
        if getattr(self, "process", None) and self.process.poll() is None:
            self.process.kill()


async def create_rtc_answer(offer_sdp: str, offer_type: str):
    configuration = RTCConfiguration(
        iceServers=[RTCIceServer(urls=["stun:stun.l.google.com:19302"])]
    )
    pc = RTCPeerConnection(configuration=configuration)
    track = None
    try:
        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type=offer_type))
        track = WindowsDesktopTrack()
        pc.addTrack(track)
        video_transceiver = next((item for item in pc.getTransceivers() if item.kind == "video"), None)
        if video_transceiver:
            h264 = [codec for codec in RTCRtpSender.getCapabilities("video").codecs
                    if codec.mimeType.lower() == "video/h264"]
            if not h264:
                raise RuntimeError("This aiortc/PyAV installation has no H.264 encoder.")
            video_transceiver.setCodecPreferences(h264)
        with rtc_peers_lock:
            rtc_peers.add(pc)

        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            if pc.connectionState in ("failed", "closed"):
                if track:
                    track.stop()
                with rtc_peers_lock:
                    rtc_peers.discard(pc)
                if pc.connectionState == "failed":
                    await pc.close()

        await pc.setLocalDescription(await pc.createAnswer())
        return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
    except Exception:
        if track:
            track.stop()
        with rtc_peers_lock:
            rtc_peers.discard(pc)
        await pc.close()
        raise


@app.post("/offer")
def webrtc_offer():
    if not session.get("authenticated"):
        return {"error": "Authentication required"}, 401
    payload = request.get_json(silent=True) or {}
    sdp = payload.get("sdp")
    offer_type = payload.get("type")
    if not isinstance(sdp, str) or len(sdp) > 200_000 or offer_type != "offer":
        return {"error": "Invalid WebRTC offer."}, 400
    try:
        future = asyncio.run_coroutine_threadsafe(create_rtc_answer(sdp, offer_type), rtc_loop)
        answer = future.result(timeout=35)
        return answer
    except Exception as exc:
        return {"error": f"WebRTC unavailable: {str(exc)[:240]}"}, 503


def key_from_code(code: str):
    special = {
        "Enter": keyboard.Key.enter,
        "Escape": keyboard.Key.esc,
        "Backspace": keyboard.Key.backspace,
        "Tab": keyboard.Key.tab,
        "Space": keyboard.Key.space,
        "ShiftLeft": keyboard.Key.shift,
        "ShiftRight": keyboard.Key.shift_r,
        "ControlLeft": keyboard.Key.ctrl,
        "ControlRight": keyboard.Key.ctrl_r,
        "AltLeft": keyboard.Key.alt,
        "AltRight": keyboard.Key.alt_r,
        "MetaLeft": keyboard.Key.cmd,
        "MetaRight": keyboard.Key.cmd_r,
        "ArrowUp": keyboard.Key.up,
        "ArrowDown": keyboard.Key.down,
        "ArrowLeft": keyboard.Key.left,
        "ArrowRight": keyboard.Key.right,
        "Delete": keyboard.Key.delete,
        "Home": keyboard.Key.home,
        "End": keyboard.Key.end,
        "PageUp": keyboard.Key.page_up,
        "PageDown": keyboard.Key.page_down,
        "Insert": keyboard.Key.insert,
        "CapsLock": keyboard.Key.caps_lock,
        "F1": keyboard.Key.f1, "F2": keyboard.Key.f2,
        "F3": keyboard.Key.f3, "F4": keyboard.Key.f4,
        "F5": keyboard.Key.f5, "F6": keyboard.Key.f6,
        "F7": keyboard.Key.f7, "F8": keyboard.Key.f8,
        "F9": keyboard.Key.f9, "F10": keyboard.Key.f10,
        "F11": keyboard.Key.f11, "F12": keyboard.Key.f12,
    }
    if code in special:
        return special[code]
    if code.startswith("Key") and len(code) == 4:
        return code[-1].lower()
    if code.startswith("Digit") and len(code) == 6:
        return code[-1]
    if code.startswith("Numpad") and len(code) == 7 and code[-1].isdigit():
        return code[-1]
    if code == "Minus":
        return "-"
    if code == "Equal":
        return "="
    if code == "Comma":
        return ","
    if code == "Period":
        return "."
    if code == "Slash":
        return "/"
    if code == "Semicolon":
        return ";"
    if code == "Quote":
        return "'"
    if code == "BracketLeft":
        return "["
    if code == "BracketRight":
        return "]"
    if code == "Backslash":
        return "\\"
    if code == "Backquote":
        return "`"
    return None


def handle_input(message: dict, mouse_ctl: mouse.Controller, key_ctl: keyboard.Controller, width: int, height: int) -> None:
    kind = message.get("type")
    if kind == "move":
        x = max(0.0, min(1.0, float(message.get("x", 0))))
        y = max(0.0, min(1.0, float(message.get("y", 0))))
        mouse_ctl.position = (int(x * max(1, width - 1)), int(y * max(1, height - 1)))
    elif kind in {"mouse_down", "mouse_up"}:
        button_name = message.get("button", "left")
        button = {"left": mouse.Button.left, "right": mouse.Button.right, "middle": mouse.Button.middle}.get(button_name)
        if button is not None:
            (mouse_ctl.press if kind == "mouse_down" else mouse_ctl.release)(button)
    elif kind == "scroll":
        mouse_ctl.scroll(0, int(max(-10, min(10, float(message.get("delta", 0))))))
    elif kind in {"key_down", "key_up"}:
        key = key_from_code(str(message.get("code", "")))
        if key is not None:
            (key_ctl.press if kind == "key_down" else key_ctl.release)(key)


@sock.route("/ws")
def websocket(ws):
    if not session.get("authenticated"):
        ws.close(reason="Authentication required")
        return

    state = StreamState()
    try:
        state.process = subprocess.Popen(
            ffmpeg_command(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
    except Exception as exc:
        ws.send(json.dumps({"error": f"Could not start FFmpeg: {exc}"}))
        return

    threading.Thread(target=read_ffmpeg_output, args=(state,), daemon=True).start()
    if not state.init_ready.wait(timeout=20) or not state.init_segment:
        error = state.error or "FFmpeg did not produce a video stream. The runner may not have an accessible desktop."
        ws.send(json.dumps({"error": error}))
        state.process.kill()
        return

    try:
        ws.send(state.init_segment)
        mouse_ctl = mouse.Controller()
        key_ctl = keyboard.Controller()
        # GitHub-hosted Windows runners normally use a 1024x768 desktop.
        # Use Win32 metrics so input coordinates follow the actual desktop.
        import ctypes
        user32 = ctypes.windll.user32
        width = int(user32.GetSystemMetrics(0))
        height = int(user32.GetSystemMetrics(1))
        while state.process.poll() is None:
            try:
                raw = ws.receive(timeout=0.01)
            except TimeoutError:
                raw = None
            if raw is not None:
                try:
                    if isinstance(raw, str):
                        message = json.loads(raw)
                        if isinstance(message, dict):
                            handle_input(message, mouse_ctl, key_ctl, width, height)
                except (ValueError, TypeError, KeyError, OverflowError):
                    pass

            try:
                fragment = state.fragments.get(timeout=0.02)
                ws.send(fragment)
            except queue.Empty:
                pass
    except Exception:
        # Browser disconnects are expected; the stream is session-scoped.
        pass
    finally:
        if state.process and state.process.poll() is None:
            state.process.kill()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080, threaded=True, use_reloader=False)
