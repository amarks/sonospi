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

## Notes for Raspberry Pi 3A+ and HyperPixel Setup

I had loads of trouble getting the screen to work with my older Raspberry, so I'm including some notes about that. You can probably ignore this section if you have a newer Pi. For best results on a Raspberry Pi 3A+ with a HyperPixel Square display, I recommend:

### 1. Use Raspberry Pi OS Bookworm Lite

* Download the **Lite** version (headless) from the [official Raspberry Pi downloads page](https://www.raspberrypi.com/software/operating-systems/).
* Avoid using the desktop version — the framebuffer-based display works more reliably without a GUI.

### 2. Enable SPI and I2C

```bash
sudo raspi-config
```

* Navigate to `Interface Options`
* Enable both **SPI** and **I2C**

### 3. Install Legacy HyperPixel Drivers

The modern Pimoroni HyperPixel installer doesn't support Bookworm correctly. Instead, use the legacy version:

```bash
git clone https://github.com/pimoroni/hyperpixel4
cd hyperpixel4
sudo ./install.sh
```

* 📎 Source: [https://github.com/pimoroni/hyperpixel4](https://github.com/pimoroni/hyperpixel4)

If you're using a square display, select the square variant when prompted.

### 4. Target the Correct Framebuffer

HyperPixel usually renders to `/dev/fb1`, not the default `/dev/fb0`. You may need to change the `fbi` command in the script to:

```bash
sudo fbi -T 1 -d /dev/fb1 -noverbose -a /tmp/current_album.jpg
```

If you’re testing manually, try:

```bash
sudo fbi -T 1 -d /dev/fb1 -noverbose -a path/to/your/image.jpg
```

These steps help avoid blank screens, unresponsive touch, or incorrect display mapping.

For best results on a Raspberry Pi 3A+ with a HyperPixel Square display, we recommend:

* Using **Raspberry Pi OS Bookworm Lite** (headless, no desktop environment)

* Enabling **SPI and I2C** via `raspi-config`:

  ```bash
  sudo raspi-config
  ```

  Navigate to `Interface Options`, then enable both SPI and I2C.

* Installing **legacy HyperPixel drivers**:
  Pimoroni’s newer installer may not work on Bookworm. Instead, clone the legacy HyperPixel 4 repo and follow instructions:

  ```bash
  git clone https://github.com/pimoroni/hyperpixel4
  cd hyperpixel4
  sudo ./install.sh
  ```

* Ensuring the **correct framebuffer device** is targeted.
  HyperPixel typically maps to `/dev/fb1`, so you may need to edit the script’s `fbi` command to:

  ```bash
  sudo fbi -T 1 -d /dev/fb1 -noverbose ...
  ```

These steps help avoid blank screens or device mismatch errors with `fbi`.

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
