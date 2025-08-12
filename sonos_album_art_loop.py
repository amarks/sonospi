#!/usr/bin/env python3
import os
import time
import signal
import logging
import subprocess
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from io import BytesIO

import soco
import requests
from PIL import Image

# -------- Optional touchscreen (evdev) ----------
try:
    from evdev import InputDevice, list_devices, ecodes
    _EVDEV = True
except Exception:
    _EVDEV = False

# ---------- Logging ----------
LOG_PATH = "/home/alan/sonospi/sonospi.log"
handler = RotatingFileHandler(LOG_PATH, maxBytes=1024 * 1024, backupCount=3)
logging.basicConfig(handlers=[handler], level=logging.INFO,
                    format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ---------- Framebuffer ----------
FB_DEV = "/dev/fb0"
FB_SYSFS = "/sys/class/graphics/fb0"
_BACKLIGHT_NODE = "/sys/class/backlight/rpi_backlight"  # your device
_BACKLIGHT_LAST_BRIGHTNESS = None

def _read(path, default=None):
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception:
        return default

def _read_int(path, default=None):
    v = _read(path, None)
    try:
        return int(v) if v is not None else default
    except Exception:
        return default

def _set_file(path, value):
    try:
        with open(path, "w") as f:
            f.write(str(value))
        return True
    except Exception as e:
        logger.debug(f"Write failed {path} -> {value}: {e}")
        return False

# Probe framebuffer (fallback 720x720x32)
virtual_size = _read(os.path.join(FB_SYSFS, "virtual_size"), "720,720")
try:
    WIDTH, HEIGHT = [int(x) for x in virtual_size.split(",")[:2]]
except Exception:
    WIDTH, HEIGHT = 720, 720

try:
    BPP = int(_read(os.path.join(FB_SYSFS, "bits_per_pixel"), "32"))
except Exception:
    BPP = 32

PIXEL_FORMAT = "BGRA"   # empirically correct for HyperPixel Square
BYTES_PER_PIXEL = BPP // 8
FRAMEBUFFER_BYTES = WIDTH * HEIGHT * BYTES_PER_PIXEL

logger.info(f"Framebuffer: {WIDTH}x{HEIGHT}@{BPP}bpp format={PIXEL_FORMAT}")

# Ensure a blank image exists
blank_path = "/home/alan/sonospi/black.png"
if not os.path.exists(blank_path):
    Image.new("RGB", (WIDTH, HEIGHT), "black").save(blank_path)

# Clear framebuffer on start
try:
    with open(FB_DEV, "wb") as fb:
        fb.write(b"\x00" * FRAMEBUFFER_BYTES)
    logger.info("Framebuffer cleared on startup.")
except Exception as e:
    logger.warning(f"Could not clear framebuffer: {e}")

# ---------- Display power control (true off/on) ----------
def display_power_off():
    ok = False
    # 1) Stop scanout
    if _set_file(f"{FB_SYSFS}/blank", "1"):
        ok = True
        logger.info("Display: fb0 blank=1")
    # 2) Backlight OFF (preferred via bl_power)
    node = _BACKLIGHT_NODE
    if node and os.path.isdir(node):
        # remember brightness (best effort)
        global _BACKLIGHT_LAST_BRIGHTNESS
        cur = _read_int(f"{node}/brightness")
        if cur is not None:
            _BACKLIGHT_LAST_BRIGHTNESS = cur
        if _set_file(f"{node}/bl_power", "1"):
            ok = True
            logger.info("Backlight: off (bl_power=1)")
        else:
            # fallback to brightness=0
            if _set_file(f"{node}/brightness", "0"):
                ok = True
                logger.info("Backlight: off (brightness=0)")
    # 3) HDMI path (harmless if not present)
    try:
        subprocess.run(
            ["/usr/bin/vcgencmd", "display_power", "0"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception:
        pass
    return ok

def display_power_on():
    # 1) Unblank scanout
    if _set_file(f"{FB_SYSFS}/blank", "0"):
        logger.info("Display: fb0 blank=0")
    # 2) Backlight ON (+ restore brightness if we have it)
    node = _BACKLIGHT_NODE
    if node and os.path.isdir(node):
        _set_file(f"{node}/bl_power", "0")
        global _BACKLIGHT_LAST_BRIGHTNESS
        if _BACKLIGHT_LAST_BRIGHTNESS is None:
            # default to current or mid-brightness
            cur = _read_int(f"{node}/brightness", 128)
            _BACKLIGHT_LAST_BRIGHTNESS = cur
        _set_file(f"{node}/brightness", str(_BACKLIGHT_LAST_BRIGHTNESS))
        logger.info("Backlight: on (bl_power=0)")
    # 3) HDMI path
    try:
        subprocess.run(
            ["/usr/bin/vcgencmd", "display_power", "1"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception:
        pass

# ---------- Shutdown handling ----------
running = True
def _handle_sigterm(signum, frame):
    global running
    running = False
signal.signal(signal.SIGTERM, _handle_sigterm)

# ---------- Helpers ----------
def _hhmmss_to_seconds(s: str) -> int:
    try:
        parts = [int(p) for p in s.split(":")]
        if len(parts) == 3:
            h, m, sec = parts
        elif len(parts) == 2:
            h, m, sec = 0, parts[0], parts[1]
        else:
            return 0
        return h * 3600 + m * 60 + sec
    except Exception:
        return 0

def _display_image(image: Image.Image):
    try:
        img = image.resize((WIDTH, HEIGHT)).convert("RGBA")
        raw = img.tobytes("raw", PIXEL_FORMAT)
        with open(FB_DEV, "wb") as fb:
            fb.write(raw)
    except Exception as e:
        logger.error(f"Framebuffer write failed: {e}")

last_image_url = None
blank_displayed = False

def blank_screen():
    """Prefer true panel off; fallback to drawing black."""
    global blank_displayed, last_image_url
    if display_power_off():
        blank_displayed = True
        last_image_url = None
        logger.info("Screen off.")
        return
    # Fallback: draw black if power-off not possible
    try:
        image = Image.open(blank_path)
        _display_image(image)
        blank_displayed = True
        last_image_url = None
        logger.info("Screen blanked (framebuffer black).")
    except Exception as e:
        logger.error(f"Blanking failed: {e}")

def display_album_art(url, speaker_name):
    """Ensure panel is on; redraw if screen was previously off even for same URL."""
    global last_image_url, blank_displayed
    # If the screen was previously off, force a redraw even if URL unchanged
    if url == last_image_url and not blank_displayed:
        return
    if url == last_image_url and blank_displayed:
        logger.info("Art: forcing redraw of existing URL because screen was off/blanked")

    display_power_on()
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        image = Image.open(BytesIO(r.content))
        _display_image(image)
        last_image_url = url
        blank_displayed = False
        logger.info(f"Art: {speaker_name} -> {url}")
    except Exception as e:
        logger.error(f"Art fetch/display failed: {e}")
        blank_screen()

# ---------- Touch controls ----------
# Single tap = play/pause; Double tap = next track (register on RELEASE).
_last_active_speaker_uid = None
_last_active_speaker_lock = os._wrap_close if False else None  # placeholder to avoid linter warnings
import threading
_last_active_speaker_lock = threading.Lock()

def set_last_active_speaker_uid(uid):
    global _last_active_speaker_uid
    with _last_active_speaker_lock:
        _last_active_speaker_uid = uid

def get_last_active_speaker_uid():
    with _last_active_speaker_lock:
        return _last_active_speaker_uid

def _open_touch_device():
    if not _EVDEV:
        return None
    forced = os.environ.get("TOUCH_EVENT")
    logger.info("Touch: scanning input devices... (forced=%s)", forced or "None")
    if forced:
        try:
            dev = InputDevice(forced)
            logger.info(f"Touch: using forced {forced} ({dev.name})")
            return dev
        except Exception as e:
            logger.warning(f"Touch: failed opening {forced}: {e}")
    try:
        best = None
        for p in list_devices():
            d = InputDevice(p)
            logger.info(f"Touch: candidate {p} name='{d.name}'")
            name = (d.name or "").lower()
            if any(s in name for s in ("touch", "ft5", "goodix", "ili", "edt", "pixcir", "egalax", "ep0")):
                logger.info(f"Touch: auto-selected {p} ({d.name})")
                return d
            if best is None:
                best = d
        if best:
            logger.info(f"Touch: fallback {best.path} ({best.name})")
            return best
    except Exception as e:
        logger.warning(f"Touch: discovery error: {e}")
    return None

def _device_capabilities(dev):
    try:
        caps = dev.capabilities(verbose=False)
        keys = set(caps.get(ecodes.EV_KEY, []))
        abs_codes = set(code for code in caps.get(ecodes.EV_ABS, []))
        use_btn = ecodes.BTN_TOUCH in keys
        use_mt = ecodes.ABS_MT_TRACKING_ID in abs_codes
        if use_btn:
            return True, False
        if use_mt:
            return False, True
    except Exception:
        pass
    return True, False

def _find_active_coordinator_from_list(speakers_list):
    target_uid = get_last_active_speaker_uid()
    if target_uid:
        for s in speakers_list:
            if getattr(s, "uid", None) == target_uid:
                try:
                    return s.group.coordinator if s.group else s
                except Exception:
                    return s
    # fallback: any PLAYING coordinator
    for s in speakers_list:
        try:
            tinfo = s.get_current_transport_info() or {}
            if tinfo.get("current_transport_state") == "PLAYING":
                return s.group.coordinator if s.group else s
        except Exception:
            continue
    return speakers_list[0] if speakers_list else None

def _seconds_to_hhmmss(total: int) -> str:
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h}:{m:02}:{s:02}"

def _toggle_play_pause(soco_obj):
    try:
        state = (soco_obj.get_current_transport_info() or {}).get("current_transport_state", "")
        if state == "PLAYING":
            logger.info("Touch: single tap - pause")
            soco_obj.pause()
        else:
            # Rewind slightly if near end so resume doesn't instantly advance
            try:
                ti = soco_obj.get_current_track_info() or {}
                pos = _hhmmss_to_seconds(ti.get("position", "0:00:00"))
                dur = _hhmmss_to_seconds(ti.get("duration", "0:00:00"))
                remain = max(0, dur - pos)
                rewind_thresh = int(os.environ.get("TOUCH_RESUME_REWIND_SEC", "5"))
                rewind_back = int(os.environ.get("TOUCH_RESUME_BACK_SEC", "3"))
                logger.info(f"Touch: resume request (pos={ti.get('position')} dur={ti.get('duration')} remain={remain}s)")
                if dur > 0 and remain <= rewind_thresh:
                    new_pos = max(0, pos - rewind_back)
                    seek_to = _seconds_to_hhmmss(new_pos)
                    try:
                        soco_obj.seek(seek_to)
                        logger.info(f"Touch: near end → seek back to {seek_to} before play")
                    except Exception as e:
                        logger.warning(f"Touch: seek before resume failed: {e}")
            except Exception as e:
                logger.debug(f"Touch: resume pre-check failed: {e}")
            logger.info("Touch: single tap - play")
            soco_obj.play()
    except Exception as e:
        logger.warning(f"Touch: toggle failed: {e}")

def _next_track(soco_obj):
    try:
        soco_obj.next()
    except Exception as e:
        logger.warning(f"Touch: next-track failed: {e}")

def _start_touch_listener(get_speakers_callable, on_after_action=None):
    if not _EVDEV:
        logger.info("Touch: evdev not available; disabled.")
        return None
    dbl_window = float(os.environ.get("TOUCH_DBL_TAP_MS", "400")) / 1000.0

    def worker():
        dev = _open_touch_device()
        if not dev:
            logger.info("Touch: no device found. Set TOUCH_EVENT=/dev/input/eventX and restart.")
            return
        use_btn, use_mt = _device_capabilities(dev)
        logger.info(f"Touch: caps BTN_TOUCH={use_btn} ABS_MT_TRACKING_ID={use_mt}")
        logger.info(f"Touch: listening on {dev.path} ({dev.name}); mode={'BTN' if use_btn else 'MT'}; dbl={int(dbl_window*1000)}ms")

        contact_active = False
        last_tap_time = 0.0
        pending_timer = None
        pending_lock = threading.Lock()
        refractory_until = 0.0  # ignore duplicate releases within 20ms

        def do_toggle():
            speakers_now = get_speakers_callable()
            coord = _find_active_coordinator_from_list(speakers_now)
            if coord:
                _toggle_play_pause(coord)
                if on_after_action:
                    try:
                        on_after_action()
                    except Exception:
                        pass

        def do_next():
            speakers_now = get_speakers_callable()
            coord = _find_active_coordinator_from_list(speakers_now)
            if coord:
                _next_track(coord)
                if on_after_action:
                    try:
                        on_after_action()
                    except Exception:
                        pass

        def consume_single():
            nonlocal pending_timer
            with pending_lock:
                pending_timer = None
            logger.info("Touch: single tap confirmed")
            do_toggle()

        def on_tap_release():
            nonlocal pending_timer, last_tap_time, refractory_until
            now = time.time()
            if now < refractory_until:
                return
            refractory_until = now + 0.02
            with pending_lock:
                if pending_timer is None:
                    pending_timer = threading.Timer(dbl_window, consume_single)
                    pending_timer.daemon = True
                    pending_timer.start()
                    last_tap_time = now
                else:
                    if now - last_tap_time <= dbl_window:
                        try:
                            pending_timer.cancel()
                        except Exception:
                            pass
                        pending_timer = None
                        logger.info("Touch: double tap confirmed")
                        do_next()

        for event in dev.read_loop():
            try:
                if use_btn and event.type == ecodes.EV_KEY and event.code == ecodes.BTN_TOUCH:
                    if event.value == 1 and not contact_active:
                        contact_active = True
                    elif event.value == 0 and contact_active:
                        contact_active = False
                        on_tap_release()
                elif use_mt and event.type == ecodes.EV_ABS and event.code == ecodes.ABS_MT_TRACKING_ID:
                    if event.value != -1 and not contact_active:
                        contact_active = True
                    elif event.value == -1 and contact_active:
                        contact_active = False
                        on_tap_release()
            except Exception:
                pass

    t = threading.Thread(target=worker, daemon=True, name="touch-handler")
    t.start()
    return t

# ---------- Main album-art loop ----------
speakers = list(soco.discover()) or []
logger.info(f"Discovered {len(speakers)} speakers.")
last_discovery = datetime.now()

def _get_speakers_snapshot():
    return speakers

def _force_refresh_after_action():
    global last_image_url, blank_displayed
    blank_displayed = True    # so next draw forces redraw even if same URL
    last_image_url = last_image_url  # unchanged

_start_touch_listener(_get_speakers_snapshot, _force_refresh_after_action)

STALE_WINDOW_SEC = 12
last_pos = {}

while running:
    try:
        if datetime.now() - last_discovery > timedelta(minutes=5):
            speakers = list(soco.discover()) or []
            logger.info(f"Rediscovered {len(speakers)} speakers.")
            last_discovery = datetime.now()

        found_art = False
        now = datetime.now()

        for speaker in speakers:
            try:
                tinfo = speaker.get_current_transport_info()
                state = (tinfo or {}).get("current_transport_state", "")
                if state not in ("PLAYING", "TRANSITIONING"):
                    continue

                track_info = speaker.get_current_track_info() or {}
                art_url = track_info.get("album_art")
                if not art_url:
                    continue

                # staleness: position must advance unless source doesn't provide position
                pos_s = _hhmmss_to_seconds(track_info.get("position", "0:00:00"))
                lp = last_pos.get(speaker.uid)
                if lp is not None:
                    prev_pos, prev_t = lp["pos"], lp["t"]
                    if pos_s <= prev_pos and (now - prev_t).total_seconds() >= STALE_WINDOW_SEC:
                        # treat as stale PLAYING
                        continue
                last_pos[speaker.uid] = {"pos": pos_s, "t": now}

                full_url = art_url if art_url.startswith("http") else f"http://{speaker.ip_address}:1400{art_url}"
                display_album_art(full_url, speaker.player_name)
                set_last_active_speaker_uid(getattr(speaker, "uid", None))
                found_art = True
                break
            except Exception as e:
                logger.warning(f"Speaker check error ({getattr(speaker, 'player_name', 'unknown')}): {e}")

        if not found_art:
            blank_screen()
    except Exception as e:
        logger.error(f"Main loop error: {e}")
        blank_screen()

    time.sleep(5)

blank_screen()
logger.info("Shutting down.")
