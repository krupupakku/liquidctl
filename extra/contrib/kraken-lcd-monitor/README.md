# kraken-lcd-monitor

Personal automation script: shows live CPU/GPU temperatures on the LCD of an
NZXT Kraken cooler, similar in style to NZXT CAM's temperature gauges.

This is **not** part of liquidctl itself — it's a standalone script that uses
liquidctl as a library and is meant to run continuously (e.g. as a systemd
service), rendering a new frame every couple of seconds with
[Pillow](https://python-pillow.org/) and pushing it to the screen with the
existing `set_screen("lcd", "static", ...)` driver API. No changes to the
liquidctl driver are needed.

Tested against the NZXT Kraken 2024 Plus (240x240 LCD). Should also work with
other LCD-capable Krakens, but the layout is tuned for a 240x240 square
canvas.

Written for a SteamOS desktop with an AMD GPU, using `psutil` to read CPU and
GPU temperatures from hwmon (`k10temp` for CPU, `amdgpu` for GPU). Should work
on any Linux system with those sensors exposed; use `--list-sensors` to check.

## Requirements

- Python 3
- `liquidctl`, `pillow`, `psutil`
- A bold system font. The script looks for a few common ones (DejaVu Sans
  Bold, Noto Sans Bold, Liberation Sans Bold); pass `--font <path>` to use a
  different one.

## Setup (SteamOS / generic systemd Linux)

1. Set up udev rules so the device can be accessed without root, see
   [`extra/linux`](../../linux) (`71-liquidctl.rules` /
   `generate-uaccess-udev-rules.py`) or run as `root`/with `sudo`.

2. Create an isolated virtual environment (SteamOS's system Python is
   managed by pacman, so don't install packages globally):

   ```
   mkdir -p ~/kraken-lcd-monitor
   cp kraken_lcd_monitor.py ~/kraken-lcd-monitor/
   python3 -m venv ~/kraken-lcd-monitor/venv
   ~/kraken-lcd-monitor/venv/bin/pip install liquidctl pillow psutil
   ```

3. Sanity-check sensor detection and manually try a run:

   ```
   ~/kraken-lcd-monitor/venv/bin/python3 ~/kraken-lcd-monitor/kraken_lcd_monitor.py --list-sensors
   ~/kraken-lcd-monitor/venv/bin/python3 ~/kraken-lcd-monitor/kraken_lcd_monitor.py --match kraken -v
   ```

   If the auto-detected CPU/GPU sensors are wrong, pass them explicitly:

   ```
   --cpu-sensor k10temp.tctl --gpu-sensor amdgpu.edge
   ```

4. Install the systemd unit. Edit
   [`systemd/kraken-lcd-monitor.service`](systemd/kraken-lcd-monitor.service)
   first: replace `deck` with your actual username (`whoami`) if different.

   ```
   sudo cp systemd/kraken-lcd-monitor.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now kraken-lcd-monitor
   ```

   `/etc/systemd/system/` is writable on SteamOS even with the root
   filesystem otherwise read-only, so no `steamos-readonly disable` is
   needed just for this.

## Operation

| Action | Command |
| --- | --- |
| Start now + enable at boot | `sudo systemctl enable --now kraken-lcd-monitor` |
| Stop (until next boot) | `sudo systemctl stop kraken-lcd-monitor` |
| Disable (stop + don't start at boot) | `sudo systemctl disable --now kraken-lcd-monitor` |
| Status / logs | `systemctl status kraken-lcd-monitor` / `journalctl -u kraken-lcd-monitor -f` |

Stopping the service does **not** clear the screen by itself — the Kraken
keeps showing the last static frame it received. The unit's
`ExecStopPost=` automatically runs `liquidctl set lcd screen liquid` whenever
the service is stopped, disabled, or the system shuts down, restoring the
stock animated liquid-temperature display. To do this manually at any time:

```
liquidctl set lcd screen liquid
```

## Tuning

- `--interval <seconds>` (default `2`): how often to refresh. Each `static`
  update on the Kraken 2024 Plus involves a small LCD handshake plus a 240x240
  frame sent twice over USB (existing driver behavior); 2s is a safe starting
  point, lower it once you've confirmed there's enough headroom
  (`--verbose`/`--debug` logs update timing).
- `--warn-temp`, `--crit-temp`, `--scale-max`: control the color thresholds
  (green/yellow/red) and what temperature corresponds to a full bar.
- By default a frame is only re-rendered and re-sent when the rounded CPU or
  GPU temperature changes, to reduce USB writes; pass `--force` to always
  resend every interval.

## Command-line reference

See `kraken_lcd_monitor.py --help` for the full list of options (device
filtering flags like `--match`/`--pick`/`--vendor`/`--product` follow the
same conventions as the liquidctl CLI and other scripts in
[`extra/`](../..)).
