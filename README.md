# SonosPi

SonosPi is a minimalist Raspberry Pi–powered display that shows the currently playing album art from your Sonos system. Designed to be simple, unobtrusive, and always-on, it turns off the screen when playback is paused and wakes it when music resumes.

Credit for the concept and original version goes to **Mark Hank**, who wrote a fantastic how to guide that you'll find here: [https://www.hackster.io/mark-hank/sonos-album-art-on-raspberry-pi-screen-5b0012. ](https://www.hackster.io/mark-hank/sonos-album-art-on-raspberry-pi-screen-5b0012. )

This rewrite was created  because the original version stopped working due to updated dependencies. It's functionally close to the original but lacks support for track name overlay. 

---

## Features

* Detects the **coordinator** of a Sonos speaker group
* Fetches and displays **album art** on a framebuffer-connected screen using `fbi`
* **Avoids flicker** by skipping redundant image updates
* **Automatically turns the screen off** when playback is paused
* Works on **Raspberry Pi OS Bookworm** (tested on Pi 3A+ with a HyperPixel Square display)
* **Systemd service** for running on boot
* Includes **log rotation**

---

## Hardware Requirements

* Raspberry Pi (tested on **3A+**)
* Framebuffer-connected display (e.g., **HyperPixel Square**)

---

## Software Requirements

* Raspberry Pi OS (Bookworm recommended)
* Python 3
* `soco` (Sonos control library)
* `fbi` (for displaying images to framebuffer)

Install dependencies:

```bash
sudo apt install fbi python3-pip
pip3 install soco requests
```

---

## Setup

1. **Clone this repository**

```bash
git clone https://github.com/amarks/sonospi.git
cd sonospi
```

2. **Make the display script executable**

```bash
chmod +x sonos_album_art_loop.py
```

3. **Test it manually**

```bash
./sonos_album_art_loop.py
```

4. **Install the systemd service** (optional but recommended)

```bash
sudo cp sonospi.service /etc/systemd/system/
sudo systemctl enable sonospi
sudo systemctl start sonospi
```

---

## Systemd Service Overview

`sonospi.service` runs the display script at boot and ensures it continues running in the background. You can check its status with:

```bash
sudo systemctl status sonospi
```

Logs are stored with rotation in place to prevent file bloat.

---

## Logging

Logs are written to `~/sonospi.log` and automatically rotated using Python's built-in `RotatingFileHandler`.

---

## Screen Rotation

If your display is mounted in a non-default orientation (e.g., vertical), you can rotate the framebuffer by editing the `config.txt` file:

```bash
sudo nano /boot/config.txt
```

Add or modify the following line:

```
display_lcd_rotate=1  # Values: 0=0°, 1=90°, 2=180°, 3=270°
```

Then reboot:

```bash
sudo reboot
```

---

## Troubleshooting

* **Black screen?** Make sure `fbi` has permission to write to the framebuffer.
* **No album art showing?** Ensure a Sonos group is playing and reachable on the local network.
* **Startup flicker?** This has been minimized by suppressing initial "loading" frames.

---

## To Do / Possible Enhancements

* Display track info and artist text overlay
* Switch to `Pillow` and `pygame` for more graphical flexibility
* Integrate touch support for skipping tracks (on supported displays)

---

## License

MIT License

---

## Author

[amarks](https://github.com/amarks)

---

## Credits

* **Mark Hank** – Original Sonos album art concept and guide: [Hackster.io article](https://www.hackster.io/mark-hank/sonos-album-art-on-raspberry-pi-screen-5b0012)
* The [SoCo](https://github.com/SoCo/SoCo) project – Python library for Sonos integration
* The Raspberry Pi community for framebuffer and `fbi` tips
