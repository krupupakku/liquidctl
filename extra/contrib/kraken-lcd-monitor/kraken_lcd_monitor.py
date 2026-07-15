#!/usr/bin/env python3

"""kraken-lcd-monitor - show live CPU/GPU temperatures on a Kraken LCD.

Periodically renders a CPU/GPU temperature gauge, similar in style to NZXT
CAM, and pushes it to the LCD of a Kraken cooler using liquidctl as a
library.  This is a personal automation script, not part of liquidctl itself:
it is meant to be run continuously (e.g. as a systemd service) and just calls
the public `set_screen` driver API on an interval.

Requirements:
  liquidctl (with the Python API), pillow, psutil

Usage:
  kraken_lcd_monitor.py [options]
  kraken_lcd_monitor.py --list-sensors [options]
  kraken_lcd_monitor.py --help

Options:
  --interval <seconds>     Update interval in seconds [default: 2]
  --cpu-sensor <chip.label>  Sensor to use for CPU temperature (see --list-sensors)
  --gpu-sensor <chip.label>  Sensor to use for GPU temperature (see --list-sensors)
  --warn-temp <celsius>    Temperature at which the bar turns yellow [default: 60]
  --crit-temp <celsius>    Temperature at which the bar turns red [default: 80]
  --scale-max <celsius>    Temperature that corresponds to a full bar [default: 100]
  --font <path>            Path to a .ttf/.otf font to use instead of the
                            auto-detected system font
  --force                  Always resend the frame, even if temperatures
                            (rounded to the nearest degree) haven't changed
  --list-sensors           List available psutil temperature sensors and exit
  -m, --match <substring>  Filter devices by description substring
  -n, --pick <number>      Pick among many results for a given filter
  --vendor <id>            Filter devices by vendor id
  --product <id>           Filter devices by product id
  --release <number>       Filter devices by release number
  --serial <number>        Filter devices by serial number
  --bus <bus>              Filter devices by bus
  --address <address>      Filter devices by address in bus
  --usb-port <port>        Filter devices by USB port in bus
  -v, --verbose            Output additional information
  -g, --debug              Show debug information on stderr
  --version                Display the version number
  --help                   Show this message

Examples:
  kraken_lcd_monitor.py --list-sensors
  kraken_lcd_monitor.py --match kraken --interval 2
  kraken_lcd_monitor.py --cpu-sensor k10temp.Tctl --gpu-sensor amdgpu.edge

Copyright Jonas Malaco and contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""

import io
import logging
import signal
import sys
import time

import liquidctl.cli as _borrow
import psutil
import usb
from docopt import docopt
from liquidctl.driver import find_liquidctl_devices
from PIL import Image, ImageDraw, ImageFont

VERSION = "0.1.0"

LOGGER = logging.getLogger(__name__)

# rendered at 2x and downsampled for smoother edges (supersampling)
SUPERSAMPLE = 2
CANVAS_SIZE = 240

# candidate system fonts, tried in order until one is found
_FONT_CANDIDATES = [
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/NotoSans-Bold.ttf",
    "/usr/share/fonts/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]

_COLOR_OK = (95, 209, 76)  # green
_COLOR_WARN = (240, 200, 60)  # yellow
_COLOR_CRIT = (220, 70, 60)  # red
_COLOR_BG = (0, 0, 0)
_COLOR_TEXT = (235, 235, 235)
_COLOR_TRACK = (55, 55, 55)


class NoSensorFoundError(Exception):
    pass


def find_font(explicit_path=None):
    candidates = [explicit_path] if explicit_path else _FONT_CANDIDATES
    for path in candidates:
        try:
            ImageFont.truetype(path, 10)
            return path
        except (OSError, IOError):
            continue
    raise FileNotFoundError(
        "no usable font found; install one of the common system fonts or pass --font <path>"
    )


def read_all_sensors():
    """Return a flat {"chip.label": value} dict from psutil, CPU freq included."""
    sensors = {}
    for chip, entries in psutil.sensors_temperatures().items():
        for entry in entries:
            label = entry.label or "unnamed"
            key = f"{chip}.{label.lower().replace(' ', '_')}"
            sensors[key] = entry.current
    return sensors


def show_sensors():
    sensors = read_all_sensors()
    print("{:<40}  {:>10}".format("Sensor identifier", "Value (°C)"))
    print("-" * 55)
    for k, v in sensors.items():
        print(f"{k:<40}  {v:>10.1f}")


def guess_sensor(sensors, preferred_chips_labels):
    """Pick the first matching chip/label pair from a preference list."""
    for chip, label in preferred_chips_labels:
        key = f"{chip}.{label}"
        if key in sensors:
            return key
    return None


def resolve_sensor(explicit, sensors, preferred, kind):
    if explicit:
        if explicit not in sensors:
            raise NoSensorFoundError(
                f"sensor '{explicit}' not found; run --list-sensors to see available sensors"
            )
        return explicit
    guessed = guess_sensor(sensors, preferred)
    if not guessed:
        raise NoSensorFoundError(
            f"could not auto-detect a {kind} sensor; run --list-sensors and pass "
            f"--{kind}-sensor <chip.label> explicitly"
        )
    LOGGER.info("auto-detected %s sensor: %s", kind, guessed)
    return guessed


def bar_color(temp, warn_temp, crit_temp):
    if temp >= crit_temp:
        return _COLOR_CRIT
    if temp >= warn_temp:
        return _COLOR_WARN
    return _COLOR_OK


def render_frame(cpu_temp, gpu_temp, font_path, warn_temp, crit_temp, scale_max):
    size = CANVAS_SIZE * SUPERSAMPLE
    img = Image.new("RGB", (size, size), _COLOR_BG)
    draw = ImageDraw.Draw(img)

    label_font = ImageFont.truetype(font_path, int(22 * SUPERSAMPLE))
    number_font = ImageFont.truetype(font_path, int(56 * SUPERSAMPLE))

    margin = int(28 * SUPERSAMPLE)
    bar_height = int(14 * SUPERSAMPLE)
    bar_width = size - 2 * margin
    row_height = size // 2

    def draw_row(y_offset, label, temp):
        label_pos = (margin, y_offset)
        draw.text(label_pos, label, font=label_font, fill=_COLOR_TEXT)

        number_text = f"{round(temp)}°"
        bbox = draw.textbbox((0, 0), number_text, font=number_font)
        text_w = bbox[2] - bbox[0]
        number_pos = (size - margin - text_w, y_offset - int(6 * SUPERSAMPLE))
        draw.text(number_pos, number_text, font=number_font, fill=_COLOR_TEXT)

        bar_y = y_offset + int(34 * SUPERSAMPLE)
        bar_box = [margin, bar_y, margin + bar_width, bar_y + bar_height]
        draw.rounded_rectangle(bar_box, radius=bar_height // 2, fill=_COLOR_TRACK)

        fraction = max(0.0, min(1.0, temp / scale_max))
        fill_width = int(bar_width * fraction)
        if fill_width > 0:
            fill_box = [margin, bar_y, margin + fill_width, bar_y + bar_height]
            color = bar_color(temp, warn_temp, crit_temp)
            draw.rounded_rectangle(fill_box, radius=bar_height // 2, fill=color)

    draw_row(row_height // 2 - int(10 * SUPERSAMPLE), "CPU", cpu_temp)
    draw_row(row_height + row_height // 2 - int(10 * SUPERSAMPLE), "GPU", gpu_temp)

    img = img.resize((CANVAS_SIZE, CANVAS_SIZE), Image.LANCZOS)

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def main():
    args = docopt(__doc__, version=VERSION)

    if args["--debug"]:
        args["--verbose"] = True
        logging.basicConfig(level=logging.DEBUG, format="[%(levelname)s] %(name)s: %(message)s")
    elif args["--verbose"]:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING, format="%(message)s")

    if args["--list-sensors"]:
        show_sensors()
        return

    interval = float(args["--interval"])
    warn_temp = float(args["--warn-temp"])
    crit_temp = float(args["--crit-temp"])
    scale_max = float(args["--scale-max"])
    force = args["--force"]

    font_path = find_font(args["--font"])
    LOGGER.info("using font: %s", font_path)

    sensors = read_all_sensors()
    cpu_sensor = resolve_sensor(
        args["--cpu-sensor"],
        sensors,
        preferred=[("k10temp", "tctl"), ("k10temp", "tdie"), ("coretemp", "package_id_0")],
        kind="cpu",
    )
    gpu_sensor = resolve_sensor(
        args["--gpu-sensor"],
        sensors,
        preferred=[("amdgpu", "edge"), ("amdgpu", "junction")],
        kind="gpu",
    )

    frwd = _borrow._make_opts(args)
    devices = list(find_liquidctl_devices(**frwd))
    if len(devices) == 0:
        LOGGER.error("no matching liquidctl device found")
        sys.exit(1)
    if len(devices) > 1:
        LOGGER.error("multiple matching devices found, narrow down with --match/--pick")
        sys.exit(1)
    device = devices[0]

    running = True

    def handle_stop(signum, frame):
        nonlocal running
        LOGGER.info("received signal %s, stopping", signum)
        running = False

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    LOGGER.info("connecting to %s", device.description)
    device.connect()
    try:
        device.initialize()

        last_cpu = None
        last_gpu = None

        while running:
            try:
                all_sensors = read_all_sensors()
                cpu_temp = all_sensors[cpu_sensor]
                gpu_temp = all_sensors[gpu_sensor]

                if (
                    force
                    or last_cpu is None
                    or round(cpu_temp) != last_cpu
                    or round(gpu_temp) != last_gpu
                ):
                    frame_buffer = render_frame(
                        cpu_temp, gpu_temp, font_path, warn_temp, crit_temp, scale_max
                    )
                    device.set_screen("lcd", "static", frame_buffer)
                    last_cpu = round(cpu_temp)
                    last_gpu = round(gpu_temp)
                    LOGGER.debug("updated LCD: CPU %.1f°C, GPU %.1f°C", cpu_temp, gpu_temp)
            except usb.core.USBError as err:
                LOGGER.warning("USB error while updating LCD, will retry: %s", err)
            except KeyError as err:
                LOGGER.error("sensor %s disappeared: %s", err, err)

            for _ in range(int(interval * 10)):
                if not running:
                    break
                time.sleep(0.1)
    finally:
        LOGGER.info("disconnecting from %s", device.description)
        device.disconnect()


if __name__ == "__main__":
    try:
        main()
    except NoSensorFoundError as err:
        print(f"error: {err}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass
