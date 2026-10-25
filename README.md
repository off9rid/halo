# HALO

**Hand-Activated Launch Overlay.** Control your Mac with hand gestures through the webcam. No extra hardware, no cloud: MediaPipe tracks your hand on-device and HALO turns gestures into window moves, media controls and shortcuts.

## Gestures

| Gesture | Action |
|---|---|
| Rock-on 🤘 (hold) | Arm / disarm everything |
| Swipe left / right | Switch desktop |
| Pinch 👌 and move | Grab the front window and drag it |
| Pinch and hold still (1 s) | Resize mode: open palm = bigger, fist = smaller, pinch again = done |
| Closed fist (hold) | Play / pause media |
| Open palm (hold still) | Mission Control |
| Thumbs up / down (hold) | Volume up / down |
| Peace sign ✌️ (hold) | Screenshot to Desktop |
| Three fingers (hold) | Next track |
| Shaka 🤙 (hold) | Minimize the front window |
| Point up (long hold) | Quit the front app (Terminal and Finder are protected) |

## Setup

macOS, Python 3.9+, a webcam.

```bash
git clone https://github.com/off9rid/halo.git && cd halo
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./halo              # HUD window with the camera preview
./halo --headless   # no window
./halo --camera 1   # pick another camera
```

The first run asks for **Camera** access, and window control needs **Accessibility** for your terminal (System Settings > Privacy & Security).

## How it works

- `halo.py`: camera loop, MediaPipe gesture recognizer, gesture smoothing and hold timers, the HUD.
- `mover.js`: long-running JXA helper that grabs, drags and scales the front window.
- `target.js`: one-shot JXA helper to quit or minimize the front app.
- `gesture_recognizer.task`: Google's MediaPipe gesture model (Apache 2.0).

Built by [@off9rid](https://github.com/off9rid).
