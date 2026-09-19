# Firmware

MicroPython on the RP2040. Design depth is in [notes.md](notes.md); this file is what you need to get it running.

## Dependencies

| Item | Version | Notes |
|---|---|---|
| MicroPython for Raspberry Pi Pico | v1.28.0 (2026-04-06) as tested | v1.20 or newer is required, because the animations use `framebuf.ellipse` |
| `ssd1306` driver | 0.1.0, MIT | installed to `/lib` on the board |
| Thonny | any current version | used for flashing and for the package install |

Everything else — `machine`, `time`, `random`, `os`, `math`, `framebuf`, `micropython` — ships with MicroPython.

## Flashing the board

1. Hold BOOTSEL, plug the Pico into the PC, and release. It mounts as a USB drive.
2. Download the MicroPython UF2 for the Pico from micropython.org and drop it on that drive. The board reboots into MicroPython.
3. Open Thonny and select the interpreter "MicroPython (Raspberry Pi Pico)".

## Installing the display driver

In Thonny, open Tools → Manage packages, search for `ssd1306`, and install it. It lands in `/lib` on the board. The driver is a single MIT-licensed file and can also be copied to `/lib` by hand if you prefer.

## Loading the game

1. Copy [`src/main.py`](src/main.py) to the board as `/main.py`.
2. Optionally copy `src/cinema.py` to the board as well. It is imported inside a `try`/`except`, so the game runs unchanged if it is missing or fails to load.
3. Reset the board. `main.py` runs at boot, with or without the USB cable.

## Running it

Press the button once to start a round. Five LEDs light one second apart, hold, then go out after a random 200–3000 ms delay; press as soon as they go out. Pressing before they go out is a false start. The session best sits in the footer and resets at power-off. The slide switch beside the OLED mutes the buzzer, and the display shows the mute state and the power source.

On battery, the onboard LED flashes for one second at boot. Below an estimated 3.4 V the device warns and then enters deep sleep, which only clears on a full power cycle — leave the switch off for a few seconds before switching back on.
