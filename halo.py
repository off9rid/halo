"""HALO (Hand-Activated Launch Overlay): control the Mac with hand gestures through the webcam.

Gestures (while ARMED):
  swipe left / right      switch desktop
  pinch 👌 and move       grab the front window and drag it
  pinch 👌 and hold still resize mode (1 s): open palm = bigger, fist = smaller, pinch again = done
  closed fist, hold       play / pause media
  open palm, hold still   Mission Control
  thumbs up / down, hold  volume up / down
  peace sign, hold        screenshot to Desktop
  point up, long hold     quit the front app (Terminal/Finder are protected)
  three fingers, hold     next track
  shaka 🤙, hold           minimize the front window
  rock-on (ILoveYou) hold arm / disarm everything

Run:  ./halo            (HUD window)
      ./halo --headless (no window)
"""
import argparse
import math
import os
import subprocess
import threading
import time
from collections import deque
import warnings

warnings.filterwarnings("ignore", message="SymbolDatabase.GetPrototype")

import cv2
import numpy as np
import mediapipe as mp
from mediapipe.tasks.python import BaseOptions, vision

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.path.join(HERE, "gesture_recognizer.task")

GOLD = (0, 190, 255)
RED = (70, 70, 255)
WHITE = (235, 235, 235)

COOLDOWN = 0.9          # seconds between discrete actions
SWIPE_WINDOW = 0.35     # seconds of palm history used for swipes
SWIPE_DIST = 0.22       # fraction of frame width
HOLD_TIME = 0.8         # seconds for hold gestures
HOLD_TIMES = {"Pointing_Up": 1.5}   # quitting an app deserves a longer, deliberate hold
HOLD_GRACE = 0.35       # a hold survives the model losing the gesture this long
PINCH_ON, PINCH_OFF = 0.28, 0.45   # thumb-index gap / hand size, with hysteresis
DRAG_GAIN = 1.6         # window travel (screen widths) per frame width of hand travel
DRAG_SMOOTH = 0.45      # 0..1, lower = smoother but laggier window drag
RESIZE_HOLD = 1.0       # seconds of a still pinch before it turns into resize mode
RESIZE_STILL = 0.03     # how far the hand may drift (frame fraction) during that second
RESIZE_RATE = 0.7       # window grows/shrinks by this fraction per second
RESIZE_IDLE = 4.0       # resize mode ends by itself after this long without palm/fist

MEDIA_KEY_JXA = """
ObjC.import('AppKit');
function post(down) {
  var ev = $.NSEvent.otherEventWithTypeLocationModifierFlagsTimestampWindowNumberContextSubtypeData1Data2(
    14, $.NSMakePoint(0, 0), down ? 0xa00 : 0xb00, 0, 0, null, 8,
    (KEY << 16) | ((down ? 0xa : 0xb) << 8), -1);
  $.CGEventPost(0, ev.CGEvent);
}
post(true); post(false);
"""


def sh(*args):
    def run():
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode:
            print("  action failed:", (r.stderr or r.stdout).strip(), flush=True)
    threading.Thread(target=run, daemon=True).start()


def osa(script):
    sh("osascript", "-e", script)


def media(spotify_cmd, key):
    # macOS drops the synthetic media key, so drive Spotify directly when it's open.
    if subprocess.run(["pgrep", "-x", "Spotify"], capture_output=True).returncode == 0:
        osa(f'tell application "Spotify" to {spotify_cmd}')
    else:
        sh("osascript", "-l", "JavaScript", "-e", MEDIA_KEY_JXA.replace("KEY", str(key)))


notify = print  # set by main() to flash target.js results on the HUD


def target(verb):
    """Quit the front app or minimize its window, via target.js."""
    def run():
        r = subprocess.run(["osascript", "-l", "JavaScript", os.path.join(HERE, "target.js"),
                            str(os.getpid()), verb], capture_output=True, text=True)
        out = (r.stdout or r.stderr).strip()
        print("  ", out, flush=True)
        if out.startswith("protected"):
            notify(out[10:].upper() + " IS PROTECTED")
        elif out == "none":
            notify("NO WINDOW")
        elif r.returncode == 0:
            notify(out.upper())
    threading.Thread(target=run, daemon=True).start()


ACTIONS = {
    "desktop_left": lambda: osa('tell application "System Events" to key code 123 using control down'),
    "desktop_right": lambda: osa('tell application "System Events" to key code 124 using control down'),
    "play_pause": lambda: media("playpause", 16),
    "next_track": lambda: media("next track", 17),
    "quit_app": lambda: target("quit"),
    "minimize": lambda: target("minimize"),
    "mission_control": lambda: sh("open", "-a", "Mission Control"),
    "volume_up": lambda: osa("set volume output volume ((output volume of (get volume settings)) + 6)"),
    "volume_down": lambda: osa("set volume output volume ((output volume of (get volume settings)) - 6)"),
    "screenshot": lambda: sh("screencapture", "-x", os.path.expanduser(
        time.strftime("~/Desktop/halo-%Y-%m-%d-%H%M%S.png"))),
}
LABELS = {
    "desktop_left": "<< DESKTOP", "desktop_right": "DESKTOP >>", "play_pause": "PLAY / PAUSE",
    "mission_control": "MISSION CONTROL", "volume_up": "VOLUME +", "volume_down": "VOLUME -",
    "screenshot": "SCREENSHOT", "next_track": "NEXT TRACK", "quit_app": "QUIT APP",
    "minimize": "MINIMIZE", "arm": "ARMED", "disarm": "DISARMED", "grab": "GRAB",
    "resize": "RESIZE: PALM + / FIST -", "grow": "BIGGER", "shrink": "SMALLER", "resize_done": "SIZE LOCKED",
}


class Mover:
    """Talks to mover.js, a long-running osascript that moves windows."""

    def __init__(self, on_reply):
        self.proc = subprocess.Popen(
            ["osascript", "-l", "JavaScript", os.path.join(HERE, "mover.js"), str(os.getpid())],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        self.last_move = 0.0

        def read():
            for line in self.proc.stdout:
                on_reply(line.strip())
        threading.Thread(target=read, daemon=True).start()

    def send(self, line):
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass

    def grab(self):
        self.send("grab")

    def move(self, dx, dy, now):
        if now - self.last_move >= 1 / 30:
            self.last_move = now
            self.send(f"move {dx:.4f} {dy:.4f}")

    def scale(self, k):
        self.send(f"scale {k:.4f}")

    def release(self):
        self.send("release")

    def close(self):
        self.proc.terminate()


def dist(a, b):
    return math.hypot(a.x - b.x, a.y - b.y)


def custom_shape(lm, hand_size):
    """Shapes the canned model doesn't know: 'Three' (index+middle+ring up) and 'Shaka' (thumb+pinky out)."""
    up = {tip: dist(lm[0], lm[tip]) > 1.1 * dist(lm[0], lm[tip - 2]) for tip in (8, 12, 16, 20)}
    down = {tip: dist(lm[0], lm[tip]) < dist(lm[0], lm[tip - 2]) for tip in (8, 12, 16, 20)}
    thumb_out = dist(lm[4], lm[9]) / hand_size > 0.8
    if up[8] and up[12] and up[16] and down[20]:
        return "Three"
    if thumb_out and up[20] and down[8] and down[12] and down[16]:
        return "Shaka"
    return None


class Engine:
    def __init__(self, mover=None):
        self.mover = mover
        self.drag = None               # (anchor x, anchor y, smoothed x, smoothed y) while pinching
        self.armed = True
        self.last_action = float("-inf")
        self.palm = deque()            # (t, x, y) of palm centre
        self.pinching = False
        self.hold_name = None
        self.hold_start = 0.0
        self.hold_seen = 0.0
        self.hold_fired = False
        self.flash = ("", 0.0)         # (label, time) shown on the HUD
        self.gesture = None            # canned or custom gesture seen this frame
        self.pinch_t0 = 0.0            # when the current pinch started
        self.pinch_drift = 0.0         # how far the hand has wandered since then
        self.resize = False            # resize mode: palm grows the grabbed window, fist shrinks it
        self.resize_seen = 0.0         # last palm/fist (or entry) in resize mode
        self.resize_last = 0.0         # last scale step, for frame-rate independent speed
        self.wait_release = False      # the pinch that ended resize mode: ignore it until the fingers open

    def fire(self, name, now, cooldown=COOLDOWN):
        if now - self.last_action < cooldown:
            return
        self.last_action = now
        self.flash = (LABELS[name], now)
        if name in ACTIONS:
            ACTIONS[name]()
        print(time.strftime("%H:%M:%S"), LABELS[name], flush=True)

    def update(self, lm, gesture, now):
        """lm: 21 landmarks (mirrored frame), gesture: canned category name."""
        hand_size = dist(lm[0], lm[9]) or 1e-6
        cx, cy = lm[9].x, lm[9].y

        gesture = custom_shape(lm, hand_size) or gesture
        self.gesture = gesture

        # Resize mode (entered by holding a pinch still): palm/fist resize, nothing else fires.
        if self.resize:
            gap = dist(lm[4], lm[8]) / hand_size
            others_up = sum(dist(lm[0], lm[tip]) > dist(lm[0], lm[tip - 2]) for tip in (12, 16, 20))
            if self.pinching:
                if gap > PINCH_OFF:
                    self.pinching = False      # let go of the pinch, keep hold of the window
                return
            if gap < PINCH_ON and others_up >= 2:
                self._end_resize(now)          # pinch again = done
                return
            dt = min(0.1, now - self.resize_last)
            self.resize_last = now
            if gesture in ("Open_Palm", "Closed_Fist"):
                grow = gesture == "Open_Palm"
                if self.mover:
                    self.mover.scale((1 + RESIZE_RATE) ** (dt if grow else -dt))
                self.flash = (LABELS["grow" if grow else "shrink"], now)
                self.resize_seen = now
            elif now - self.resize_seen > RESIZE_IDLE:
                self._end_resize(now)
            return

        # Holds: palm still, gesture sustained.
        if self.pinching or self.wait_release:
            gesture = None             # a pinch often reads as a fist; ignore holds while dragging
        holdable = {"Open_Palm", "Thumb_Up", "Thumb_Down", "Victory", "ILoveYou", "Closed_Fist",
                    "Pointing_Up", "Three", "Shaka"}
        if gesture in holdable:
            if gesture != self.hold_name:
                self.hold_name, self.hold_start, self.hold_fired = gesture, now, False
            self.hold_seen = now
        elif self.hold_name and now - self.hold_seen > HOLD_GRACE:
            self.hold_name = None
        held = self.hold_name and now - self.hold_start >= HOLD_TIMES.get(self.hold_name, HOLD_TIME)

        if held and self.hold_name == "ILoveYou" and not self.hold_fired:
            self.hold_fired = True
            self.armed = not self.armed
            self.fire("arm" if self.armed else "disarm", now, cooldown=0)
            return
        if not self.armed:
            self.palm.clear()
            self._release()
            return

        if held:
            if self.hold_name == "Thumb_Up":
                self.fire("volume_up", now, cooldown=0.35)
            elif self.hold_name == "Thumb_Down":
                self.fire("volume_down", now, cooldown=0.35)
            elif not self.hold_fired and self.hold_name == "Closed_Fist":
                self.hold_fired = True
                self.fire("play_pause", now)
            elif not self.hold_fired and self.hold_name == "Victory":
                self.hold_fired = True
                self.fire("screenshot", now)
            elif not self.hold_fired and self.hold_name == "Open_Palm" and self._still(now):
                self.hold_fired = True
                self.fire("mission_control", now)
            elif not self.hold_fired and self.hold_name in ("Pointing_Up", "Three", "Shaka"):
                self.hold_fired = True
                self.fire({"Pointing_Up": "quit_app", "Three": "next_track", "Shaka": "minimize"}[self.hold_name], now)

        # Pinch (OK sign: thumb + index touching, other fingers up) grabs and drags the front window.
        gap = dist(lm[4], lm[8]) / hand_size
        others_up = sum(dist(lm[0], lm[tip]) > dist(lm[0], lm[tip - 2]) for tip in (12, 16, 20))
        if self.wait_release and gap > PINCH_OFF:
            self.wait_release = False
        if not self.pinching and not self.wait_release and gap < PINCH_ON and others_up >= 2:
            self.pinching = True
            self.hold_name = None
            self.palm.clear()
            self.drag = (cx, cy, cx, cy)
            self.pinch_t0, self.pinch_drift = now, 0.0
            self.fire("grab", now, cooldown=0)
            if self.mover:
                self.mover.grab()
        elif self.pinching and gap > PINCH_OFF:
            self._release()
        if self.pinching:
            ax, ay, sx, sy = self.drag
            sx += DRAG_SMOOTH * (cx - sx)
            sy += DRAG_SMOOTH * (cy - sy)
            self.drag = (ax, ay, sx, sy)
            self.pinch_drift = max(self.pinch_drift, abs(cx - ax), abs(cy - ay))
            if now - self.pinch_t0 > RESIZE_HOLD and self.pinch_drift < RESIZE_STILL:
                self.resize = True     # held still: resize instead of drag
                self.resize_seen = self.resize_last = now
                self.fire("resize", now, cooldown=0)
                return
            if self.mover:
                self.mover.move((sx - ax) * DRAG_GAIN, (sy - ay) * DRAG_GAIN, now)
            return                     # no swipes mid-drag

        # Swipe: fast horizontal travel of the palm centre.
        self.palm.append((now, cx, cy))
        while self.palm and now - self.palm[0][0] > SWIPE_WINDOW:
            self.palm.popleft()
        if len(self.palm) >= 4:
            dx = cx - self.palm[0][1]
            dy = cy - self.palm[0][2]
            if abs(dx) > SWIPE_DIST and abs(dx) > 2 * abs(dy):
                self.fire("desktop_right" if dx > 0 else "desktop_left", now)
                self.palm.clear()
                self.hold_name = None

    def _still(self, now):
        pts = [(x, y) for t, x, y in self.palm if now - t < 0.4]
        if len(pts) < 3:
            return True
        xs, ys = zip(*pts)
        return max(xs) - min(xs) < 0.04 and max(ys) - min(ys) < 0.04

    def _release(self):
        if (self.pinching or self.resize) and self.mover:
            self.mover.release()
        self.pinching = False
        self.resize = False
        self.drag = None

    def _end_resize(self, now):
        self.resize = False
        self.wait_release = True
        self.pinching = False
        self.drag = None
        if self.mover:
            self.mover.release()
        self.fire("resize_done", now, cooldown=0)

    def lost_hand(self):
        self.gesture = None
        self.palm.clear()
        self.hold_name = None
        if self.resize:                # a dropped frame mid-resize shouldn't end it; the idle timer will
            self.pinching = False
            return
        self._release()


# ---------- HUD ----------
# Overlays are drawn on their own layer and given a light glow over a dimmed feed.
HOLO = (255, 215, 60)
MONO = cv2.FONT_HERSHEY_PLAIN
FONT = cv2.FONT_HERSHEY_SIMPLEX
TIPS = (4, 8, 12, 16, 20)
AA = cv2.LINE_AA


def scale(col, k):
    return tuple(int(c * k) for c in col)


def brackets(img, x0, y0, x1, y1, L, col, t=1):
    for x, y, sx, sy in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
        cv2.line(img, (x, y), (x + sx * L, y), col, t, AA)
        cv2.line(img, (x, y), (x, y + sy * L), col, t, AA)


def draw_hud(frame, lm, gesture, eng, fps, now, legend=True):
    h, w = frame.shape[:2]
    col = HOLO if eng.armed else RED
    dim = scale(col, 0.5)

    base = cv2.convertScaleAbs(frame, alpha=0.6)
    holo = np.zeros_like(frame)

    if lm:
        pts = [(int(p.x * w), int(p.y * h)) for p in lm]
        c = pts[9]
        r = max(18, int(dist(lm[0], lm[9]) * w * 0.5))
        for conn in vision.HandLandmarksConnections.HAND_CONNECTIONS:
            cv2.line(holo, pts[conn.start], pts[conn.end], col, 1, AA)
        for i, p in enumerate(pts):
            cv2.circle(holo, p, 4 if i in TIPS else 2, WHITE, -1, AA)

        cv2.circle(holo, c, r, dim, 1, AA)
        a = (now * 90) % 360
        for k in range(3):
            cv2.ellipse(holo, c, (r + 8, r + 8), 0, a + k * 120, a + k * 120 + 60, col, 2, AA)

        if eng.hold_name and not eng.hold_fired:
            p = min(1.0, (now - eng.hold_start) / HOLD_TIMES.get(eng.hold_name, HOLD_TIME))
            ring = RED if eng.hold_name == "Pointing_Up" else GOLD
            cv2.ellipse(holo, c, (r + 20, r + 20), -90, 0, 360 * p, ring, 3, AA)

    brackets(holo, 12, 12, w - 12, h - 12, 36, dim, 1)
    cv2.putText(holo, "HALO", (28, 44), FONT, 0.6, col, 1, AA)
    cv2.putText(holo, ("ARMED" if eng.armed else "DISARMED") + f"   {fps:.0f} fps",
                (28, 64), FONT, 0.4, dim, 1, AA)

    label, t = eng.flash
    age = now - t
    if label and age < 1.0:
        k = 1 - age ** 2
        size = cv2.getTextSize(label, FONT, 1.0, 2)[0]
        cv2.putText(holo, label, ((w - size[0]) // 2, h - 40), FONT, 1.0, scale(GOLD, k), 2, AA)

    if legend:
        draw_legend(base, holo, gesture, eng, now, col)

    glow = cv2.resize(cv2.GaussianBlur(cv2.resize(holo, (w // 2, h // 2)), (0, 0), 3), (w, h))
    return cv2.add(base, cv2.addWeighted(holo, 1.0, glow, 0.6, 0))


# (icon emoji, gesture, action it does, canned gesture name, actions that light the row up)
LEGEND = [
    ("👈", "SWIPE", "switch desktop", None, ("desktop_left", "desktop_right")),
    ("👌", "PINCH + MOVE", "drag window", None, ("grab",)),
    ("👌", "PINCH + HOLD 1s", "resize: palm + / fist -", None, ("resize", "grow", "shrink", "resize_done")),
    ("✊", "FIST hold", "play / pause", "Closed_Fist", ("play_pause",)),
    ("🖐", "PALM hold", "mission control", "Open_Palm", ("mission_control",)),
    ("👍", "THUMB UP hold", "volume +", "Thumb_Up", ("volume_up",)),
    ("👎", "THUMB DOWN hold", "volume -", "Thumb_Down", ("volume_down",)),
    ("✌️", "PEACE hold", "screenshot", "Victory", ("screenshot",)),
    ("☝️", "POINT UP 1.5s", "quit app", "Pointing_Up", ("quit_app",)),
    ("3", "3 FINGERS hold", "next track", "Three", ("next_track",)),
    ("🤙", "SHAKA hold", "minimize", "Shaka", ("minimize",)),
    ("🤟", "ROCK-ON hold", "arm / disarm", "ILoveYou", ("arm", "disarm")),
]
ICON = 24
_icons = {}


def icon(emoji):
    """Emoji rendered once with Apple Color Emoji, as a (luminance, alpha) pair for tinting."""
    if emoji not in _icons:
        from PIL import Image, ImageDraw, ImageFont
        font = ImageFont.truetype("/System/Library/Fonts/Apple Color Emoji.ttc", 160)
        chars = [c for c in emoji if c != "️"]
        img = Image.new("RGBA", (180 * len(chars), 180), (0, 0, 0, 0))
        for i, c in enumerate(chars):
            ImageDraw.Draw(img).text((180 * i, 0), c + ("️" if "️" in emoji else ""),
                                     font=font, embedded_color=True)
        img = img.crop(img.getbbox())
        img = img.resize((max(1, round(img.width * ICON / img.height)), ICON), Image.LANCZOS)
        a = np.asarray(img).astype(np.float32)
        lum = 0.35 + 0.65 * a[:, :, :3].mean(axis=2, keepdims=True) / 255
        _icons[emoji] = (lum, a[:, :, 3:] / 255.0)
    return _icons[emoji]


def paste(holo, emoji, x, y, col):
    """Emoji as a monochrome hologram in `col`; plain text (no emoji for it) is drawn as-is."""
    if emoji.isascii():
        cv2.putText(holo, emoji, (x + 6, y + ICON - 3), cv2.FONT_HERSHEY_DUPLEX, 0.8, col, 2, AA)
        return
    lum, alpha = icon(emoji)
    h, w = alpha.shape[:2]
    roi = holo[y:y + h, x:x + w]
    if roi.shape[:2] != (h, w):
        return
    tint = (lum * alpha * np.array(col, np.float32)).astype(np.uint8)
    np.maximum(roi, tint, out=roi)


def draw_legend(base, holo, gesture, eng, now, col):
    h, w = base.shape[:2]
    row, pad, pw = 32, 12, 330
    ph = pad * 2 + 30 + row * len(LEGEND) + 12
    x0, y0 = w - pw - 24, 24
    dim = scale(col, 0.45)

    base[y0:y0 + ph, x0:x0 + pw] //= 3
    cut = 14
    frame = np.array([(x0 + cut, y0), (x0 + pw, y0), (x0 + pw, y0 + ph - cut),
                      (x0 + pw - cut, y0 + ph), (x0, y0 + ph), (x0, y0 + cut)], np.int32)
    cv2.polylines(holo, [frame], True, col, 1, AA)
    cv2.putText(holo, "GESTURES", (x0 + pad + 6, y0 + pad + 12), FONT, 0.48, col, 1, AA)
    cv2.line(holo, (x0 + pad, y0 + pad + 22), (x0 + pw - pad, y0 + pad + 22), dim, 1)

    flashed, t = eng.flash
    for i, (emoji, name, does, canned, actions) in enumerate(LEGEND):
        top = y0 + pad + 32 + row * i
        lit = (canned and gesture == canned) or (canned is None and "grab" in actions and eng.pinching) \
            or (flashed and now - t < 1.0 and flashed in {LABELS[a] for a in actions})
        c = GOLD if lit else col
        if lit:
            cv2.rectangle(holo, (x0 + 4, top - 2), (x0 + 7, top + ICON + 2), GOLD, -1)
        paste(holo, emoji, x0 + pad + 4, top, c)
        cv2.putText(holo, name, (x0 + pad + 62, top + 17), FONT, 0.42, GOLD if lit else WHITE, 1, AA)
        cv2.putText(holo, does, (x0 + pad + 196, top + 17), FONT, 0.42, c if lit else dim, 1, AA)

    cv2.putText(holo, "h: hide   q: quit", (x0 + pad + 6, y0 + ph - 10), MONO, 0.9, dim, 1, AA)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true", help="no preview window")
    ap.add_argument("--camera", type=int, default=0)
    args = ap.parse_args()

    recognizer = vision.GestureRecognizer.create_from_options(vision.GestureRecognizerOptions(
        base_options=BaseOptions(model_asset_path=MODEL),
        running_mode=vision.RunningMode.VIDEO,
        num_hands=1,
        min_hand_detection_confidence=0.6,
        min_tracking_confidence=0.5,
    ))

    cap = cv2.VideoCapture(args.camera)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 960)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 540)
    if not cap.isOpened():
        raise SystemExit("Camera unavailable. Grant Terminal camera access in "
                         "System Settings > Privacy & Security > Camera.")

    def on_mover(msg):
        if msg.startswith("grabbed"):
            eng.flash = ("GRAB  " + msg[8:].upper(), time.monotonic())
        elif msg == "none":
            eng.flash = ("NO WINDOW TO GRAB", time.monotonic())
        else:
            print("  window move failed:", msg, flush=True)

    eng = Engine(Mover(on_mover))
    global notify
    notify = lambda msg: setattr(eng, "flash", (msg, time.monotonic()))
    start = time.monotonic()
    fps, last = 0.0, start
    legend = True
    print("HALO running. Rock-on sign (hold) toggles arm. h toggles the gesture list, Ctrl+C or q to quit.", flush=True)

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            frame = cv2.flip(frame, 1)
            now = time.monotonic()
            fps = 0.9 * fps + 0.1 / max(now - last, 1e-3)
            last = now

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            result = recognizer.recognize_for_video(
                mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), int((now - start) * 1000))

            lm = result.hand_landmarks[0] if result.hand_landmarks else None
            gesture = result.gestures[0][0].category_name if result.gestures else None
            if gesture == "None":
                gesture = None
            if lm:
                eng.update(lm, gesture, now)
            else:
                eng.lost_hand()
            gesture = eng.gesture

            if not args.headless:
                if not globals().get("_fs"):
                    globals()["_fs"]=True
                    cv2.namedWindow("HALO", cv2.WND_PROP_FULLSCREEN)
                    cv2.setWindowProperty("HALO", cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
                cv2.imshow("HALO", draw_hud(frame, lm, gesture, eng, fps, now, legend))
                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), 27):
                    break
                if key == ord("h"):
                    legend = not legend
    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
        cv2.destroyAllWindows()
        recognizer.close()
        eng.mover.close()


if __name__ == "__main__":
    main()
