import soco
import requests
from PIL import Image
from io import BytesIO
import time
from datetime import datetime, timedelta
import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler

# Setup logging with rotation
log_path = "/home/alan/sonospi/sonospi.log"
handler = RotatingFileHandler(log_path, maxBytes=1024*1024, backupCount=3)
logging.basicConfig(
    handlers=[handler],
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s',
)
logger = logging.getLogger(__name__)

# --- Framebuffer verification ---
FB_DEV = "/dev/fb0"
FB_SYSFS = "/sys/class/graphics/fb0"

def _read(path, default=None):
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception:
        return default

# Probe framebuffer size & bpp from sysfs (fallback to 720x720x32)
virtual_size = _read(os.path.join(FB_SYSFS, "virtual_size"), "720,720")
try:
    WIDTH, HEIGHT = [int(x) for x in virtual_size.split(",")[:2]]
except Exception:
    WIDTH, HEIGHT = 720, 720

try:
    BPP = int(_read(os.path.join(FB_SYSFS, "bits_per_pixel"), "32"))
except Exception:
    BPP = 32

PIXEL_FORMAT = "BGRA"  # empirically correct for this setup (rgba 8/16,8/8,8/0,8/24)
BYTES_PER_PIXEL = BPP // 8
FRAMEBUFFER_BYTES = WIDTH * HEIGHT * BYTES_PER_PIXEL

logger.info(f"Framebuffer sysinfo: {WIDTH}x{HEIGHT} @ {BPP}bpp, writing format={PIXEL_FORMAT}")

# Set up directories
blank_path = "/home/alan/sonospi/black.png"

# Ensure blank image exists
if not os.path.exists(blank_path):
    logger.warning(f"Blank image not found at {blank_path}, creating fallback black image.")
    from PIL import Image as _PILImage
    _PILImage.new('RGB', (WIDTH, HEIGHT), 'black').save(blank_path)

# Clear framebuffer on start
try:
    with open(FB_DEV, 'wb') as fb:
        fb.write(b'\x00' * FRAMEBUFFER_BYTES)
    logger.info("Framebuffer cleared.")
except Exception as e:
    logger.warning(f"Could not clear framebuffer: {e}")

# Handle graceful shutdown
running = True

def handle_sigterm(signum, frame):
    global running
    running = False
signal.signal(signal.SIGTERM, handle_sigterm)

# --- Helpers ---

def _hhmmss_to_seconds(s: str) -> int:
    try:
        parts = [int(p) for p in s.split(":")]
        if len(parts) == 3:
            h, m, sec = parts
        elif len(parts) == 2:
            h, m, sec = 0, parts[0], parts[1]
        else:
            return 0
        return h*3600 + m*60 + sec
    except Exception:
        return 0

# Blank the screen
blank_displayed = False


def display_image(image):
    try:
        # Resize and convert to expected pixel order
        img = image.resize((WIDTH, HEIGHT)).convert('RGBA')
        raw = img.tobytes('raw', PIXEL_FORMAT)  # BGRA bytes
        with open(FB_DEV, 'wb') as fb:
            fb.write(raw)
        logger.debug(f"Wrote {len(raw)} bytes to framebuffer ({WIDTH}x{HEIGHT}x{BPP}).")
    except Exception as e:
        logger.error(f"Failed to write image to framebuffer: {e}")


def blank_screen():
    global blank_displayed, last_image_url
    try:
        from PIL import Image as _PILImage
        image = _PILImage.open(blank_path)
        display_image(image)
        blank_displayed = True
        last_image_url = None  # force refresh on next valid art
        logger.info("No album art found. Screen blanked.")
    except Exception as e:
        logger.error(f"Failed to blank screen: {e}")

# Display the image
last_image_url = None

# Track last known positions to detect stale/stuck PLAYING states
# key: speaker.uid -> { 'pos': seconds, 't': datetime }
last_pos = {}


def display_album_art(url, speaker_name):
    global last_image_url, blank_displayed
    if url == last_image_url:
        return
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        from PIL import Image as _PILImage
        image = _PILImage.open(BytesIO(response.content))
        display_image(image)
        last_image_url = url
        blank_displayed = False
        logger.info(f"Displaying album art from {speaker_name}: {url}")
    except Exception as e:
        logger.error(f"Failed to fetch or display image: {e}")
        blank_screen()

# Main loop
speakers = list(soco.discover()) or []
logger.info(f"Discovered {len(speakers)} speakers.")
last_discovery = datetime.now()

# How long we tolerate a non-advancing position while state==PLAYING before treating as stopped
STALE_WINDOW_SEC = 12

while running:
    try:
        # Rediscover speakers every 5 minutes
        if datetime.now() - last_discovery > timedelta(minutes=5):
            speakers = list(soco.discover()) or []
            logger.info(f"Rediscovered {len(speakers)} speakers.")
            last_discovery = datetime.now()

        found_art = False
        now = datetime.now()

        for speaker in speakers:
            try:
                tinfo = speaker.get_current_transport_info()
                state = (tinfo or {}).get('current_transport_state', '')
                if state != 'PLAYING':
                    continue  # only consider actively playing speakers

                track_info = speaker.get_current_track_info() or {}
                art_url = track_info.get('album_art')
                pos_s = _hhmmss_to_seconds(track_info.get('position', '0:00:00'))

                # Detect stale PLAYING states (position not advancing)
                lp = last_pos.get(speaker.uid)
                if lp is not None:
                    prev_pos, prev_t = lp['pos'], lp['t']
                    if pos_s <= prev_pos and (now - prev_t).total_seconds() >= STALE_WINDOW_SEC:
                        logger.info(f"Ignoring stale PLAYING on {speaker.player_name} (position not advancing).")
                        continue
                # update last pos snapshot
                last_pos[speaker.uid] = {'pos': pos_s, 't': now}

                if not art_url:
                    continue
                full_url = art_url if art_url.startswith('http') else f"http://{speaker.ip_address}:1400{art_url}"
                display_album_art(full_url, speaker.player_name)
                found_art = True
                break

            except Exception as e:
                logger.warning(f"Error checking speaker {getattr(speaker, 'player_name', 'unknown')}: {e}")

        if not found_art:
            blank_screen()

    except Exception as e:
        logger.error(f"Unexpected error in loop: {e}")
        blank_screen()

    time.sleep(5)

# Cleanup on exit
blank_screen()
logger.info("Shutting down.")