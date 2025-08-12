import soco
import requests
from PIL import Image
from io import BytesIO
import time
from datetime import datetime, timedelta
import logging
import os
import signal
from logging.handlers import RotatingFileHandler
import threading

try:
    from evdev import InputDevice, list_devices, ecodes
    _EVDEV = True
except Exception:
    _EVDEV = False

# ---------- Logging ----------
log_path = "/home/alan/sonospi/sonospi.log"
handler = RotatingFileHandler(log_path, maxBytes=1024*1024, backupCount=3)
logging.basicConfig(handlers=[handler], level=logging.INFO,
                    format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

if not _EVDEV:
    logger.info("Touch: evdev module not available; touch disabled")

# ---------- Framebuffer ----------
FB_DEV = "/dev/fb0"
FB_SYSFS = "/sys/class/graphics/fb0"

def _read(path, default=None):
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception:
        return default

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
    from PIL import Image as _PILImage
    _PILImage.new('RGB', (WIDTH, HEIGHT), 'black').save(blank_path)

# Clear framebuffer on start
try:
    with open(FB_DEV, 'wb') as fb:
        fb.write(b'\x00' * FRAMEBUFFER_BYTES)
    logger.info("Framebuffer cleared on startup.")
except Exception as e:
    logger.warning(f"Could not clear framebuffer: {e}")

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

def _display_image(image):
    try:
        img = image.resize((WIDTH, HEIGHT)).convert('RGBA')
        raw = img.tobytes('raw', PIXEL_FORMAT)
        with open(FB_DEV, 'wb') as fb:
            fb.write(raw)
    except Exception as e:
        logger.error(f"Framebuffer write failed: {e}")

last_image_url = None
blank_displayed = False

def blank_screen():
    global blank_displayed, last_image_url
    try:
        from PIL import Image as _PILImage
        image = _PILImage.open(blank_path)
        _display_image(image)
        blank_displayed = True
        last_image_url = None
        logger.info("Screen blanked.")
    except Exception as e:
        logger.error(f"Blanking failed: {e}")

def display_album_art(url, speaker_name):
    global last_image_url, blank_displayed
    if url == last_image_url:
        return
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        from PIL import Image as _PILImage
        image = _PILImage.open(BytesIO(r.content))
        _display_image(image)
        last_image_url = url
        blank_displayed = False
        logger.info(f"Art: {speaker_name} -> {url}")
    except Exception as e:
        logger.error(f"Art fetch/display failed: {e}")
        blank_screen()

# ---------- Touch controls ----------
# Single tap = toggle play/pause; Double tap = next track.
# Register tap on RELEASE and choose ONE event source to avoid double counting.

_last_active_speaker_uid = None
_last_active_speaker_lock = threading.Lock()

def set_last_active_speaker_uid(uid):
    global _last_active_speaker_uid
    with _last_active_speaker_lock:
        _last_active_speaker_uid = uid

def get_last_active_speaker_uid():
    with _last_active_speaker_lock:
        return _last_active_speaker_uid

_TOUCH_NAME_HINTS = ("touch", "ft5", "goodix", "ili", "edt", "pixcir", "egalax")

def _open_touch_device():
    """Return an InputDevice or None. Honors TOUCH_EVENT env override.
       Logs all candidates and the chosen device."""
    if not _EVDEV:
        return None

    forced = os.environ.get("TOUCH_EVENT")
    logger.info("Touch: scanning input devices..." + (f" (forced={forced})" if forced else ""))
    try:
        if forced:
            try:
                dev = InputDevice(forced)
                logger.info(f"Touch: using forced {forced} ({dev.name})")
                return dev
            except Exception as e:
                logger.warning(f"Touch: failed opening forced device {forced}: {e}")

        candidates = []
        for p in list_devices():
            try:
                d = InputDevice(p)
                candidates.append((p, d.name))
            except Exception as e:
                candidates.append((p, f"<open failed: {e}>"))
        for p, name in candidates:
            logger.info(f"Touch: candidate {p} name='{name}'")

        # Prefer names that look like touchscreens
        for p, name in candidates:
            lname = (name or '').lower()
            if any(h in lname for h in _TOUCH_NAME_HINTS):
                try:
                    dev = InputDevice(p)
                    logger.info(f"Touch: auto-selected {p} ({dev.name})")
                    return dev
                except Exception as e:
                    logger.warning(f"Touch: failed to open candidate {p}: {e}")
        # Fallback to first device if nothing matched
        if candidates:
            p, name = candidates[0]
            try:
                dev = InputDevice(p)
                logger.info(f"Touch: fallback {p} ({dev.name})")
                return dev
            except Exception as e:
                logger.warning(f"Touch: fallback open failed for {p}: {e}")
    except Exception as e:
        logger.warning(f"Touch: discovery error: {e}")
    return None

def _device_capabilities(dev):
    """Return (use_btn_touch, use_mt) based on device capabilities, with logging."""
    try:
        caps = dev.capabilities(verbose=False)
        keys = set(caps.get(ecodes.EV_KEY, []))
        abs_codes = set(c for c in caps.get(ecodes.EV_ABS, []))
        use_btn = ecodes.BTN_TOUCH in keys
        use_mt = ecodes.ABS_MT_TRACKING_ID in abs_codes
        logger.info(f"Touch: caps BTN_TOUCH={use_btn} ABS_MT_TRACKING_ID={use_mt}")
        # Prefer BTN if available; otherwise MT
        if use_btn:
            return True, False
        if use_mt:
            return False, True
    except Exception as e:
        logger.warning(f"Touch: capability check failed: {e}")
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
    for s in speakers_list:
        try:
            tinfo = s.get_current_transport_info() or {}
            if tinfo.get("current_transport_state") == "PLAYING":
                return s.group.coordinator if s.group else s
        except Exception:
            continue
    return speakers_list[0] if speakers_list else None

def _toggle_play_pause(soco_obj):
    try:
        state = (soco_obj.get_current_transport_info() or {}).get("current_transport_state", "")
        if state == "PLAYING":
            logger.info("Touch: single tap -> pause")
            soco_obj.pause()
        else:
            logger.info("Touch: single tap -> play")
            soco_obj.play()
    except Exception as e:
        logger.warning(f"Touch: toggle failed: {e}")

def _next_track(soco_obj):
    try:
        logger.info("Touch: double tap -> next track")
        soco_obj.next()
    except Exception as e:
        # Expected for some sources
        logger.warning(f"Touch: next-track failed: {e}")

def _start_touch_listener(get_speakers_callable, on_after_action=None):
    if not _EVDEV:
        logger.info("Touch: evdev not available; disabled.")
        return None
    dbl_window = float(os.environ.get("TOUCH_DBL_TAP_MS", "400")) / 1000.0

    def worker():
        while True:
            dev = _open_touch_device()
            if not dev:
                logger.info("Touch: no device found; retry in 30s")
                time.sleep(30)
                continue
            use_btn, use_mt = _device_capabilities(dev)
            logger.info(f"Touch: listening on {dev.path} ({dev.name}); mode={'BTN' if use_btn else 'MT'}; dbl={dbl_window*1000:.0f}ms")

            contact_active = False
            last_tap_time = 0.0
            pending_timer = None
            pending_lock = threading.Lock()
            refractory_until = 0.0

            def do_toggle():
                speakers_now = get_speakers_callable()
                coord = _find_active_coordinator_from_list(speakers_now)
                if coord:
                    _toggle_play_pause(coord)
                    if on_after_action:
                        try: on_after_action()
                        except Exception: pass

            def do_next():
                speakers_now = get_speakers_callable()
                coord = _find_active_coordinator_from_list(speakers_now)
                if coord:
                    _next_track(coord)
                    if on_after_action:
                        try: on_after_action()
                        except Exception: pass

            def consume_single():
                nonlocal pending_timer
                with pending_lock:
                    pending_timer = None
                logger.info("Touch: single tap confirmed")
                do_toggle()

            def on_tap_release():
                nonlocal pending_timer, last_tap_time, refractory_until
                now_t = time.time()
                logger.debug(f"Touch: release @{now_t}")
                if now_t < refractory_until:
                    logger.debug("Touch: release ignored (refractory)")
                    return
                refractory_until = now_t + 0.02
                with pending_lock:
                    if pending_timer is None:
                        pending_timer = threading.Timer(dbl_window, consume_single)
                        pending_timer.daemon = True
                        pending_timer.start()
                        last_tap_time = now_t
                        logger.debug("Touch: started single-tap timer")
                    else:
                        if now_t - last_tap_time <= dbl_window:
                            try: pending_timer.cancel()
                            except Exception: pass
                            pending_timer = None
                            logger.info("Touch: double tap detected")
                            do_next()

            try:
                for event in dev.read_loop():
                    try:
                        if use_btn and event.type == ecodes.EV_KEY and event.code == ecodes.BTN_TOUCH:
                            if event.value == 1 and not contact_active:
                                contact_active = True
                                logger.debug("Touch: press (BTN)")
                            elif event.value == 0 and contact_active:
                                contact_active = False
                                logger.debug("Touch: release (BTN)")
                                on_tap_release()
                        elif use_mt and event.type == ecodes.EV_ABS and event.code == ecodes.ABS_MT_TRACKING_ID:
                            if event.value != -1 and not contact_active:
                                contact_active = True
                                logger.debug("Touch: press (MT)")
                            elif event.value == -1 and contact_active:
                                contact_active = False
                                logger.debug("Touch: release (MT)")
                                on_tap_release()
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(f"Touch: read_loop error: {e}; reopening in 5s")
                time.sleep(5)
                continue

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
    blank_displayed = False
    last_image_url = None

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

        # Prefer coordinators first
        ordered = []
        try:
            coords = {s.group.coordinator for s in speakers if getattr(s, 'group', None)}
            ordered = list(coords) + [s for s in speakers if s not in coords]
        except Exception:
            ordered = speakers

        for speaker in ordered:
            try:
                tinfo = speaker.get_current_transport_info()
                state = (tinfo or {}).get('current_transport_state', '')
                track_info = speaker.get_current_track_info() or {}
                art_url = track_info.get('album_art')
                pos_str = track_info.get('position', '') or '0:00:00'
                pos_s = _hhmmss_to_seconds(pos_str)

                logger.debug(f"Check {speaker.player_name}: state={state} art={'yes' if art_url else 'no'} pos={pos_str}")

                if not art_url:
                    continue

                lp = last_pos.get(speaker.uid)
                has_position = pos_str not in ('', '0:00:00') or pos_s > 0
                if state == 'PLAYING' and has_position and lp is not None:
                    prev_pos, prev_t = lp['pos'], lp['t']
                    if pos_s <= prev_pos and (now - prev_t).total_seconds() >= STALE_WINDOW_SEC:
                        logger.info(f"Skip {speaker.player_name}: stale PLAYING (pos {prev_pos}->{pos_s}).")
                        last_pos[speaker.uid] = {'pos': pos_s, 't': now}
                        continue

                last_pos[speaker.uid] = {'pos': pos_s, 't': now}

                if state in ('PLAYING', 'TRANSITIONING'):
                    full_url = art_url if art_url.startswith('http') else f"http://{speaker.ip_address}:1400{art_url}"
                    display_album_art(full_url, speaker.player_name)
                    set_last_active_speaker_uid(getattr(speaker, 'uid', None))
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
