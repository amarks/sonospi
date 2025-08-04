import soco
import requests
from PIL import Image
from io import BytesIO

# Discover Sonos speakers
devices = soco.discover()
if not devices:
    print("No Sonos devices found.")
    exit(1)

# Pick the first device
speaker = list(devices)[0]
print("Using speaker:", speaker.player_name)

# Get current track info
track_info = speaker.get_current_track_info()
album_art_url = track_info.get("album_art")
print("Album art URL:", album_art_url)

if not album_art_url:
    print("No album art available.")
    exit(1)

# Fetch the album art image
response = requests.get(album_art_url)
img = Image.open(BytesIO(response.content))
img.save("/tmp/album_art.jpg")
print("Album art saved to /tmp/album_art.jpg")
