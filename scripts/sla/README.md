# SLA Projector Display Server

`image-display.py` shows images and video frames fullscreen on the SLA
projector and switches the projector's power over its RS232 link. Klipper
talks to it over TCP through the `[image_display]` section (see
[Config_Reference.md](../../docs/Config_Reference.md#image_display)).

## You usually don't need this page

`biokalico-installer.sh install` asks whether the printer has an SLA
projector. If you answer yes, or `printer.cfg` (or a file it includes) has
an `[image_display]` section, the installer sets the server up: it installs
the Python and system packages, creates
`~/printer_data/config/image-display-config.yaml` from the template, and
installs and starts `bioslicer-image-display.service`. On a host with no X
server it also sets up lightdm to provide one. Printers without a projector
are left alone. Every update restarts the service with the new code. If you
add `[image_display]` to a printer that is already installed, run an update
(the Update button in Mainsail, or `bash
~/klipper/scripts/biokalico-installer.sh update image-display`).

During a print, regular BioKalico drives the server: the `PRINT_START`
macro in the `bio_config/` templates turns the projector on with
`PROJECTOR_ON` for SLA prints, and the SLA macros in
[`biokalico_extras/printer/sla_video_macros.cfg`](../../biokalico_extras/printer/sla_video_macros.cfg)
load the videos embedded in the G-code and show their frames. The projector
card on the Mainsail dashboard covers manual power control. The commands are
listed under [image_display](../../docs/G-Codes.md#image_display) in the
G-code reference, and the macros in
[biokalico_extras/README.md](../../biokalico_extras/README.md).

The rest of this page is for troubleshooting, hosts set up without the
installer, and development.

## Files

- `image-display.py`: the server.
- `image-display-config.yaml.template`: the server config, copied into
  `~/printer_data/config/` on install.
- `image-display-launcher.sh`: what the service runs. It waits for the X
  display, picks a Python interpreter, then starts the server.
- `install-image-display-service.sh`: writes and starts the systemd unit.
- `setup-display.sh`: sets up lightdm autologin on a host with no X server,
  then runs `install-image-display-service.sh`.
- `projector_backends.py`: projector serial command sets (Optoma only).
- `sla_video_runtime.py`: video loading and frame decoding with ffmpeg.
- `send-image.py`: sends one command to the server from the shell.
- `gen-test-images.py`: generates test patterns and sends them to the server.

## Configuration

The live config is `~/printer_data/config/image-display-config.yaml`. The
installer never overwrites it; the comments in the template describe every
option. Restart the service after editing it:

```bash
sudo systemctl restart bioslicer-image-display.service
```

Things to check first:

- `host` stays `127.0.0.1` unless Klipper runs on another machine. Listening
  on anything else requires `auth_token`, set to the same value as
  `auth_token` under `[image_display]` in `printer.cfg`.
- `projector_device` is the serial adapter wired to the projector's RS232
  port. For a plain screen with no power link, set `projector_control: false`.
- On a host with a desktop (any window manager running), the server only
  uses a display whose EDID name has a projector brand in it (Optoma, Epson,
  and so on), so it never takes over the desktop's own monitor. For a
  projector of another brand, set `monitor_index`. A bare X server with no
  desktop also accepts any HDMI output.
- The server only binds to a monitor whose aspect ratio is in
  `allowed_aspect_ratios`, and with `require_exact_resolution` it rejects
  images whose aspect ratio differs from the bound monitor. To see the
  monitors it can find, stop the service and run
  `~/klippy-env/bin/python image-display.py --enum-monitors` from a session
  with `DISPLAY` set.

## Checking the service

SLA prints cannot start while the server is down: `PROJECTOR_ON` waits for
it until `startup_timeout` runs out. Check it first when SLA prints fail.

```bash
systemctl status bioslicer-image-display.service
journalctl -u bioslicer-image-display.service -n 100
```

The Restart All button in Mainsail's power menu restarts this service along
with Klipper and Moonraker.

## Setting up the service without the installer

The service needs an X display on the host and a Python with the packages
in `../klippy-requirements.txt`. The launcher uses the first of these that
exists: `~/klipper/.venv`, `~/klipper/scripts/sla/.venv`, then
`~/klippy-env`. Set `PYTHON_BIN` to use another interpreter.

On a host that already runs an X server (a desktop display manager or its
own `xorg.service`):

```bash
sudo SERVICE_USER=$USER bash ~/klipper/scripts/sla/install-image-display-service.sh
```

On a host with no X server, such as Raspberry Pi OS Lite, install lightdm
and let `setup-display.sh` configure it. This makes lightdm log
`SERVICE_USER` in automatically on every boot:

```bash
sudo apt-get install -y lightdm
sudo SERVICE_USER=$USER bash ~/klipper/scripts/sla/setup-display.sh
```

Both scripts refuse to run the service as root. Other variables they accept:
`CONFIG_PATH` (default `~/printer_data/config/image-display-config.yaml`),
`PYTHON_BIN`, and `DISPLAY_VALUE` (for example `:1`) when the launcher cannot
find the display on its own.

## Running the server by hand

For development, stop the service first so the port is free, then run the
server from a session with `DISPLAY` set:

```bash
sudo systemctl stop bioslicer-image-display.service
cd ~/klipper/scripts/sla
python3 -m venv .venv
.venv/bin/pip install -r ../klippy-requirements.txt
.venv/bin/python image-display.py ~/printer_data/config/image-display-config.yaml
```

While this `.venv` exists the service launcher prefers it over
`~/klippy-env` (see the order above), so delete it when you are done.

## Sending commands directly

`send-image.py` sends one JSON command and prints the reply. It needs only
the standard library. On localhost it
starts the service if the connection is refused; pass `--no-ensure-running`
to turn that off. If the server has an `auth_token`, add `"auth_token"` to
the JSON.

```bash
python3 send-image.py localhost 5555 '{"type":"STATUS"}'
python3 send-image.py localhost 5555 '{"type":"DISPLAY_IMAGE","path":"/data/frame.png"}'
python3 send-image.py localhost 5555 '{"type":"LOAD_VIDEOS_FROM_GCODE","gcode_path":"/data/job.gcode"}'
python3 send-image.py localhost 5555 '{"type":"SHOW_VIDEO_FRAME","name":"1","frame":240}'
python3 send-image.py localhost 5555 '{"type":"CLEAR"}'
```

Server commands: `DISPLAY_IMAGE`, `CLEAR`, `LOAD_VIDEO`,
`LOAD_VIDEOS_FROM_GCODE`, `SHOW_VIDEO_FRAME`, `LIST_VIDEOS`, `UNLOAD_VIDEO`,
`UNLOAD_ALL`, `PROJECTOR_ON`, `PROJECTOR_OFF`, `STATUS`.

`gen-test-images.py` draws a test pattern and sends it. It needs Pillow, so
run it with the Klipper Python. The `DISPLAY_TEST_*` macros in
`sla_video_macros.cfg` run the same script from Mainsail.

```bash
~/klippy-env/bin/python gen-test-images.py --pattern checkerboard --aspect 16:9 localhost 5555
~/klippy-env/bin/python gen-test-images.py --pattern nested-squares --aspect square --offset-x 120 localhost 5555
```
