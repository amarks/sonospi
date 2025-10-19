#!/usr/bin/env python3
import os
import time
import signal
import logging
import subprocess
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from io import BytesIO
import threading

import soco
import requests
from PIL import Image, ImageDraw

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
_BACKLIGHT_NODE = "/sys/class/backlight/rpi_backlight"  # adjust if needed
_BACKLIGHT_LAST_BRIGHTNESS = None

# --- UI config (minimal) ---
FEEDBACK_ENABLE = os.environ.get("FEEDBACK_ENABLE", "1") != "0"
FEEDBACK_ICON_FRACTION = float(os.environ.get("FEEDBACK_ICON_FRACTION", "0.35"))
FEEDBACK_FG_ALPHA = int(os.environ.get("FEEDBACK_FG_ALPHA", "230"))
FEEDBACK_COLOR = os.environ.get("FEEDBACK_COLOR", "#FFFFFF")
FEEDBACK_PAUSE_BLANK_MS = int(os.environ.get("FEEDBACK_PAUSE_BLANK_MS", "1000"))
FEEDBACK_AA_SCALE = int(os.environ.get("FEEDBACK_AA_SCALE", "4"))
FEEDBACK_RADIUS_FRAC = float(os.environ.get("FEEDBACK_RADIUS_FRAC", "0.22"))

# --- Helpers to read/write sysfs ---
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

# ---------- Display power control ----------
def display_power_off():
    ok = False
    if _set_file(f"{FB_SYSFS}/blank", "1"):
        ok = True
        logger.info("Display: fb0 blank=1")
    node = _BACKLIGHT_NODE
    if node and os.path.isdir(node):
        global _BACKLIGHT_LAST_BRIGHTNESS
        cur = _read_int(f"{node}/brightness")
        if cur is not None:
            _BACKLIGHT_LAST_BRIGHTNESS = cur
        if _set_file(f"{node}/bl_power", "1"):
            ok = True
            logger.info("Backlight: off (bl_power=1)")
        else:
            if _set_file(f"{node}/brightness", "0"):
                ok = True
                logger.info("Backlight: off (brightness=0)")
    try:
        subprocess.run(
            ["/usr/bin/vcgencmd", "display_power", "0"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception:
        pass
    return ok

def display_power_on():
    if _set_file(f"{FB_SYSFS}/blank", "0"):
        logger.info("Display: fb0 blank=0")
    node = _BACKLIGHT_NODE
    if node and os.path.isdir(node):
        _set_file(f"{node}/bl_power", "0")
        global _BACKLIGHT_LAST_BRIGHTNESS
        if _BACKLIGHT_LAST_BRIGHTNESS is None:
            cur = _read_int(f"{node}/brightness", 128)
            _BACKLIGHT_LAST_BRIGHTNESS = cur
        _set_file(f"{node}/brightness", str(_BACKLIGHT_LAST_BRIGHTNESS))
        logger.info("Backlight: on (bl_power=0)")
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

# ---------- Utility helpers ----------
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

def _seconds_to_hhmmss(total: int) -> str:
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h}:{m:02}:{s:02}"

def _display_image(image: Image.Image):
    try:
        img = image.resize((WIDTH, HEIGHT)).convert("RGBA")
        raw = img.tobytes("raw", PIXEL_FORMAT)
        with open(FB_DEV, "wb") as fb:
            fb.write(raw)
    except Exception as e:
        logger.error(f"Framebuffer write failed: {e}")

last_image_url = None
last_image_frame = None   # cached, screen-sized RGBA frame for instant resume
blank_displayed = False

# Cache of bad/forbidden art URLs (URL -> expiry_ts)
BAD_ART_CACHE = {}
BAD_ART_TTL_SEC = int(os.environ.get("BAD_ART_TTL_SEC", "180"))

def _is_definitely_bad_art_url(url: str) -> bool:
    if not url:
        return True
    u = url.lower()
    return ("undefined_" in u) or u.endswith("/undefined")

def _in_bad_cache(url: str) -> bool:
    exp = BAD_ART_CACHE.get(url)
    if exp is None:
        return False
    if exp < time.time():
        BAD_ART_CACHE.pop(url, None)
        return False
    return True

def _mark_bad(url: str):
    BAD_ART_CACHE[url] = time.time() + BAD_ART_TTL_SEC

def blank_screen():
    """Paint black into the buffer, then power off. Prevents stale-frame flashes on next unblank."""
    global blank_displayed, last_image_url
    try:
        image = Image.open(blank_path)
        _display_image(image)  # black buffer first
    except Exception as e:
        logger.debug(f"Blanking: framebuffer black draw failed (non-fatal): {e}")

    if display_power_off():
        logger.info("Screen off.")
    else:
        logger.info("Screen blanked (framebuffer black).")

    blank_displayed = True
    last_image_url = None

def display_album_art(url, speaker_name):
    """Fetch; write to buffer; THEN unblank. On failure keep whatever is on screen."""
    global last_image_url, last_image_frame, blank_displayed
    if _is_definitely_bad_art_url(url) or _in_bad_cache(url):
        logger.warning(f"Art: skipping bad URL: {url}")
        return False
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        image = Image.open(BytesIO(r.content)).convert("RGBA")
        # pre-scale to screen once; also cache for instant resume
        scaled = image.resize((WIDTH, HEIGHT))
        last_image_frame = scaled.copy()

        # Write first, then unblank to avoid flash
        _display_image(scaled)
        if blank_displayed:
            display_power_on()
            time.sleep(0.01)          # tiny settle; avoids right-to-left slide look
            _display_image(scaled)    # write again post-unblank
            blank_displayed = False


        last_image_url = url
        logger.info(f"Art: {speaker_name} -> {url}")
        return True
    except Exception as e:
        logger.error(f"Art fetch/display failed: {e}")
        _mark_bad(url)
        return False

# ---------- Overlay glyphs (simple + Sonos-like rounded bars) ----------
def _parse_hex_color(s, default=(255, 255, 255)):
    try:
        s = s.lstrip("#")
        if len(s) == 6:
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
        if len(s) == 3:
            return (int(s[0]*2, 16), int(s[1]*2, 16), int(s[2]*2, 16))
    except Exception:
        pass
    return default

def _round_rect_mask(draw, box, radius):
    x0, y0, x1, y1 = box
    r = max(0, int(radius))
    if r == 0:
        draw.rectangle((x0, y0, x1, y1), fill=255)
        return
    draw.rectangle((x0 + r, y0,     x1 - r, y1), fill=255)
    draw.rectangle((x0,     y0 + r, x1,     y1 - r), fill=255)
    draw.pieslice((x0, y0, x0 + 2*r, y0 + 2*r), 180, 270, fill=255)
    draw.pieslice((x1 - 2*r, y0, x1, y0 + 2*r), 270, 360, fill=255)
    draw.pieslice((x1 - 2*r, y1 - 2*r, x1, y1), 0, 90, fill=255)
    draw.pieslice((x0, y1 - 2*r, x0 + 2*r, y1), 90, 180, fill=255)

def _draw_feedback_icon(kind: str) -> Image.Image:
    size = int(min(WIDTH, HEIGHT) * FEEDBACK_ICON_FRACTION)
    cx, cy = WIDTH // 2, HEIGHT // 2
    half = size // 2
    aa = max(1, FEEDBACK_AA_SCALE)
    radius_frac = FEEDBACK_RADIUS_FRAC

    # hi-res mask for AA
    w_hi, h_hi = WIDTH * aa, HEIGHT * aa
    size_hi = size * aa
    half_hi = size_hi // 2
    cx_hi, cy_hi = cx * aa, cy * aa

    mask = Image.new("L", (w_hi, h_hi), 0)
    d = ImageDraw.Draw(mask)

    if kind == "play":
        left = cx_hi - size_hi // 3
        tip  = cx_hi + half_hi
        top  = cy_hi - half_hi
        bot  = cy_hi + half_hi
        d.polygon([(left, top), (left, bot), (tip, cy_hi)], fill=255)

    elif kind == "pause":
        bar_w = max(8*aa, size_hi // 4)
        gap   = max(bar_w,  size_hi // 4)
        rad   = int(bar_w * radius_frac)
        x1 = cx_hi - gap//2 - bar_w
        x2 = cx_hi + gap//2
        y0 = cy_hi - half_hi
        y1 = cy_hi + half_hi
        _round_rect_mask(d, (x1, y0, x1 + bar_w, y1), rad)
        _round_rect_mask(d, (x2, y0, x2 + bar_w, y1), rad)

    elif kind == "next":
        left1  = cx_hi - half_hi
        midx   = cx_hi
        right1 = cx_hi + half_hi
        top    = cy_hi - half_hi
        bot    = cy_hi + half_hi
        d.polygon([(left1, top), (midx, cy_hi), (left1, bot)], fill=255)
        d.polygon([(midx, top), (right1, cy_hi), (midx, bot)], fill=255)
        bar_gap = max(2*aa, size_hi // 12)
        bar_w   = max(6*aa, size_hi // 12)
        rad     = int(bar_w * radius_frac)
        bx0 = right1 + bar_gap
        bx1 = bx0 + bar_w
        _round_rect_mask(d, (bx0, top, bx1, bot), rad)

    else:
        r = max(6*aa, size_hi // 12)
        d.ellipse((cx_hi - r, cy_hi - r, cx_hi + r, cy_hi + r), fill=255)

    # downscale and colorize
    mask = mask.resize((WIDTH, HEIGHT), Image.LANCZOS)
    fg_rgb = _parse_hex_color(FEEDBACK_COLOR)
    alpha = max(0, min(255, FEEDBACK_FG_ALPHA))
    fg = Image.new("RGBA", (WIDTH, HEIGHT), (*fg_rgb, alpha))
    out = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    out = Image.composite(fg, out, mask)
    return out

def _show_feedback(kind: str, then_blank_after_ms: int = 0):
    if not FEEDBACK_ENABLE:
        return
    try:
        # Render overlay into buffer first
        img = _draw_feedback_icon(kind)
        _display_image(img)

        # Unblank after buffer has new frame
        if blank_displayed:
            display_power_on()
            globals()['blank_displayed'] = False

        logger.info(f"Feedback overlay: {kind}")

        # Optional quick blank after pause overlay
        if then_blank_after_ms > 0:
            def _delayed_blank():
                try:
                    time.sleep(then_blank_after_ms / 1000.0)
                    blank_screen()
                except Exception:
                    pass
            threading.Thread(target=_delayed_blank, daemon=True, name="feedback-blank").start()
    except Exception as e:
        logger.debug(f"Feedback overlay failed ({kind}): {e}")

# ---------- Touch controls ----------
_last_active_speaker_uid = None
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
    for s in speakers_list:
        try:
            tinfo = s.get_current_transport_info() or {}
            if tinfo.get("current_transport_state") == "PLAYING":
                return s.group.coordinator if s.group else s
        except Exception:
            continue
    return speakers_list[0] if speakers_list else None

def _toggle_play_pause(soco_obj):
    """Simplified: overlay only on pause; on resume show cached art immediately (no overlay)."""
    global last_image_frame, blank_displayed
    try:
        state = (soco_obj.get_current_transport_info() or {}).get("current_transport_state", "")
        if state == "PLAYING":
            # Show ❚❚ briefly, then blank
            _show_feedback("pause", then_blank_after_ms=FEEDBACK_PAUSE_BLANK_MS)
            logger.info("Touch: single tap - pause")
            soco_obj.pause()
        else:
            # Instantly show cached art (preferred), otherwise black; no glyph on resume
            if last_image_frame is not None:
                # Write before unblank
                _display_image(last_image_frame)
                if blank_displayed:
                    display_power_on()
                    # tiny settle to avoid scanline artifact, then write again
                    time.sleep(0.01)
                    _display_image(last_image_frame)
                    blank_displayed = False
                logger.info("Feedback: showed cached art on resume (no glyph)")
            else:
                # No cache yet → keep it clean: show black, then unblank
                try:
                    image = Image.open(blank_path)
                    _display_image(image)
                except Exception:
                    pass
                if blank_displayed:
                    display_power_on()
                    time.sleep(0.01)
                    # write black again to ensure full frame is what’s scanned out
                    try:
                        image = Image.open(blank_path)
                        _display_image(image)
                    except Exception:
                        pass
                    blank_displayed = False

            # Optional near-end rewind (unchanged)
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
        _show_feedback("next")
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

def _discover_speakers(timeout=5):
    try:
        zs = soco.discover(timeout=timeout) or set()
        # Return a stable list order
        return sorted(list(zs), key=lambda z: z.uid)
    except Exception:
        return []

speakers = _discover_speakers()
logger.info(f"Discovered {len(speakers)} speakers.")
last_discovery = datetime.now()


def _get_speakers_snapshot():
    return speakers

# Touch listener already resolves the active coordinator from the current snapshot
_start_touch_listener(_get_speakers_snapshot, on_after_action=None)

STALE_WINDOW_SEC = 12
last_pos = {}

while running:
    try:
        # Periodically re-discover in case IPs/groups change
        if datetime.now() - last_discovery > timedelta(minutes=5):
            speakers = _discover_speakers(timeout=5)
            logger.info(f"Rediscovered {len(speakers)} speakers.")
            last_discovery = datetime.now()

        found_art = False
        now = datetime.now()

        # Build current groups and always query the group coordinator for track/art
        try:
            groups = sorted({z.group for z in speakers}, key=lambda g: g.uid)
        except Exception:
            groups = []

        for g in groups:
            try:
                coord = g.coordinator
                if coord is None:
                    continue

                tinfo = coord.get_current_transport_info() or {}
                state = tinfo.get("current_transport_state", "")
                if state not in ("PLAYING", "TRANSITIONING"):
                    continue

                track_info = coord.get_current_track_info() or {}
                art_url = track_info.get("album_art")
                if not art_url:
                    continue

                # Stale-position guard (per coordinator)
                pos_s = _hhmmss_to_seconds(track_info.get("position", "0:00:00"))
                lp = last_pos.get(coord.uid)
                if lp is not None:
                    prev_pos, prev_t = lp["pos"], lp["t"]
                    if pos_s <= prev_pos and (now - prev_t).total_seconds() >= STALE_WINDOW_SEC:
                        continue
                last_pos[coord.uid] = {"pos": pos_s, "t": now}

                # Absolute vs device-relative album art
                if art_url.startswith("http"):
                    full_url = art_url
                else:
                    full_url = f"http://{coord.ip_address}:1400{art_url}"

                if display_album_art(full_url, coord.player_name):
                    set_last_active_speaker_uid(getattr(coord, "uid", None))
                    found_art = True
                    break
            except Exception as e:
                logger.warning(f"Group check error (coord={getattr(g.coordinator, 'player_name', 'unknown')}): {e}")

        if not found_art:
            logger.info("No valid art this cycle → blanking screen.")
            blank_screen()
    except Exception as e:
        logger.error(f"Main loop error: {e}")
        blank_screen()

    time.sleep(5)

blank_screen()
logger.info("Shutting down.")