import soco
import requests
from PIL import Image
from io import BytesIO
import time
import subprocess
from datetime import datetime
import logging
import os
import shutil
import signal
from logging.handlers import RotatingFileHandler

# Setup logging with rotation
log_path = "/home/alan/sonospi/sonospi.log"
handler = RotatingFileHandler(log_path, maxBytes=1024*1024, backupCount=3)
logging.basicConfig(
    handlers=[handler],
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
)
log = logging.info

def find_active_playing_speaker():
    speakers = list(soco.discover())
    if not speakers:
        log("No Sonos speakers found on network.")
        return None

    for spk in speakers:
        try:
            state = spk.get_current_transport_info().get("current_transport_state", "UNKNOWN")
            track = spk.get_current_track_info()
            uri = track.get("uri", "")
            album_art = track.get("album_art", "")
            if state == "PLAYING" and album_art and not uri.startswith("x-rincon:"):
                log(f"Selected active speaker: {spk.player_name}")
                return spk
        except Exception as e:
            log(f"Error checking speaker {spk}: {e}")

    log("No active playing speaker with valid album art found.")
    return None

# Ensure black.png exists
black_image_path = "/home/alan/sonospi/black.png"
if not os.path.exists(black_image_path):
    try:
        img = Image.new("RGB", (720, 720), color="black")
        img.save(black_image_path)
        log("Created black screen image.")
    except Exception as e:
        log(f"Error creating black screen image: {e}")

# Create static display image file
current_image_path = "/tmp/current_art.jpg"
try:
    shutil.copyfile(black_image_path, current_image_path)
    log("Initialized current_art.jpg with black image.")
except Exception as e:
    log(f"Error initializing current image: {e}")

# Function to kill any running fbi process
def kill_fbi():
    try:
        subprocess.run(["sudo", "pkill", "-f", "fbi"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log(f"Error killing fbi: {e}")

# Function to display an image using fbi
def show_image(path):
    kill_fbi()
    time.sleep(0.2)
    subprocess.Popen(
        ["sudo", "fbi", "-T", "1", "-d", "/dev/fb0", "--noverbose", "-a", path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

# Show initial black screen
show_image(current_image_path)
log("Started with black screen.")

last_url = ""
last_state = ""
coordinator = None

try:
    while True:
        try:
            # Re-evaluate active speaker periodically
            new_coordinator = find_active_playing_speaker()
            if new_coordinator:
                if coordinator != new_coordinator:
                    log(f"Switching coordinator to: {new_coordinator.player_name}")
                coordinator = new_coordinator
            else:
                log("Falling back to manual IP: 192.168.4.36")
                coordinator = soco.SoCo("192.168.4.36")

            transport_info = coordinator.get_current_transport_info()
            state = transport_info.get("current_transport_state", "UNKNOWN")
            log(f"Playback State: {state}")

            if state == "PLAYING":
                if last_state != "PLAYING":
                    log("Playback state changed to PLAYING.")

                track_info = coordinator.get_current_track_info()
                log(f"Track Info: {track_info}")
                album_art_url = track_info.get("album_art")

                if album_art_url:
                    log(f"Album art URL: {album_art_url}")

                    if album_art_url != last_url:
                        log("Fetching new album art...")
                        try:
                            response = requests.get(album_art_url, timeout=10)
                            img = Image.open(BytesIO(response.content))
                            img = img.resize((720, 720))
                            img.save("/tmp/album_art.jpg")
                            response.close()

                            # Compare with current image before displaying
                            with open("/tmp/album_art.jpg", "rb") as new_img, open(current_image_path, "rb") as current_img:
                                if new_img.read() != current_img.read():
                                    shutil.copyfile("/tmp/album_art.jpg", current_image_path)
                                    log("Downloaded and updated album art.")
                                    show_image(current_image_path)
                                    log("Displayed new album art.")
                                else:
                                    log("Image is identical to current. Skipping display update.")

                            last_url = album_art_url
                        except Exception as e:
                            log(f"Error fetching album art: {e}")
                            time.sleep(10)
                            continue
                    else:
                        log("No change in album art.")
                else:
                    log("No album art URL available.")

                # Turn screen on
                try:
                    with open("/sys/class/backlight/rpi_backlight/bl_power", "w") as f:
                        f.write("0")
                except Exception as e:
                    log(f"Error turning on screen: {e}")

                last_state = "PLAYING"

            else:
                if last_state != "NOT_PLAYING":
                    log("Speaker is not playing. Turning screen OFF.")
                    try:
                        with open("/sys/class/backlight/rpi_backlight/bl_power", "w") as f:
                            f.write("1")
                    except Exception as e:
                        log(f"Error turning off screen: {e}")
                    last_url = ""
                else:
                    log("No change in playback state, skipping screen update.")

                last_state = "NOT_PLAYING"

        except Exception as loop_error:
            log(f"Unexpected error in main loop: {loop_error}")
            time.sleep(10)

        time.sleep(5)

except KeyboardInterrupt:
    log("Script stopped by user.")
