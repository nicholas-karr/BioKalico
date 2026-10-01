# BioKalico Quick Start

This walks through everything from an empty Raspberry Pi (or laptop) to a
running, remotely-accessible BioKalico printer: installing the OS, cloning
this repo, writing your `printer.cfg`, flashing your boards, getting the
webcam working, and exposing Mainsail over the internet with a Cloudflare
Tunnel.

It assumes **zero prior experience with Linux or SSH**. Every command is
explained before you run it. If you already know your way around a
terminal, skip straight to whichever section you need; each one is
self-contained.

This is a long document on purpose. It's meant to be read section by
section as you go, not all at once.

## Contents

1. [What you'll need](#1-what-youll-need)
2. [How the pieces fit together](#2-how-the-pieces-fit-together)
3. [Prepare the host](#3-prepare-the-host)
4. [Connect to your host for the first time](#4-connect-to-your-host-for-the-first-time)
5. [A five-minute terminal survival kit](#5-a-five-minute-terminal-survival-kit)
6. [Install BioKalico](#6-install-biokalico)
7. [Find your controller boards' device paths](#7-find-your-controller-boards-device-paths)
8. [Plug in your webcam](#8-plug-in-your-webcam)
9. [Write your printer.cfg](#9-write-your-printercfg)
10. [First boot](#10-first-boot)
11. [Flash your MCU firmware](#11-flash-your-mcu-firmware)
12. [Get the webcam working](#12-get-the-webcam-working)
13. [Remote access with a Cloudflare Tunnel](#13-remote-access-with-a-cloudflare-tunnel)
14. [Turn on the BioKalico extras](#14-turn-on-the-biokalico-extras)
15. [Troubleshooting](#15-troubleshooting)
16. [Where to go next](#16-where-to-go-next)

---

## 1. What you'll need

- A host to run the software on: either a **Raspberry Pi** (4 or 5
  recommended, with a power supply and a microSD card, 16GB+) or a spare
  **Linux laptop/desktop** running Debian or Ubuntu.
- A computer to set things up from (Windows, Mac, or Linux all work) with a
  web browser.
- Your printer's electronics: mainboard(s), already wired up, connected to
  the host over USB.
- A USB webcam, if you want live video in Mainsail (optional).
- A free [Cloudflare](https://dash.cloudflare.com/sign-up) account, if you
  want remote access (optional, section 13).
- About an hour, most of which is waiting for things to download.

## 2. How the pieces fit together

Skip this if you just want to get moving. Come back to it if something
later doesn't make sense.

- **Klipper** ("Klippy") is the Python program that does all the motion
  planning. It runs on the host (the Pi/laptop) and talks to your printer's
  micro-controller(s) over USB, sending precisely-timed step commands. This
  repo *is* Klipper, specifically the BioKalico fork of it.
- **Moonraker** is a web API server that sits in front of Klipper. Nothing
  talks to Klipper directly except Moonraker; everything else (Mainsail,
  slicers, this guide's `scripts/biokalico-installer.sh`) talks to Moonraker.
- **Mainsail** is the web page you actually look at and click buttons in.
  It's a static website served by `nginx`, talking to Moonraker.
- **Crowsnest** manages webcam streams and hands them to Mainsail.
- `printer.cfg` and `moonraker.conf` are the two files that describe *your*
  specific printer: which pins do what, which serial ports to use, etc.
  They live in `~/printer_data/config/` and are **not** part of this repo;
  every printer's are different, and this repo only ships templates for
  you to start from (`bio_config/`).

## 3. Prepare the host

Pick one:

### 3a. Raspberry Pi

1. On your setup computer, download and install [Raspberry Pi
   Imager](https://www.raspberrypi.com/software/).
2. Put a microSD card into your computer (using an adapter if needed).
3. Open Raspberry Pi Imager.
   - **Device**: pick your Pi model.
   - **Operating System**: click "Choose OS" → "Other specific-purpose
     OS" → "3D printing" → **MainsailOS**. (If MainsailOS isn't listed on
     your Imager version, "Raspberry Pi OS Lite (64-bit)" works too, but
     you'll do a bit more manual setup later. MainsailOS comes with
     Mainsail/Moonraker/Klipper already installed as a starting point,
     which BioKalico's installer will then take over.)
   - **Storage**: pick your microSD card. Double-check you've selected the
     right drive. This step erases it.
4. Click the gear/settings icon (or you'll be prompted after clicking
   "Next") to open **advanced options** before writing. This step matters:
   it's what lets you skip plugging a monitor/keyboard into the Pi later:
   - Set a **hostname** (e.g. `bioprinter1`). You'll use
     `bioprinter1.local` to reach it later.
   - **Enable SSH**, and choose "Use password authentication". Set a
     username and password you'll remember.
   - If you're on Wi-Fi (not a wired Ethernet connection), fill in your
     **Wi-Fi SSID and password** here too.
5. Click **Save**, then **Write**, and confirm. This takes a few minutes
   and will erase the card.
6. Once it's done, move the microSD card into the Pi and power it on. Give
   it 1-2 minutes to boot for the first time.

### 3b. Linux laptop/desktop

1. Install Debian (stable) or Ubuntu (LTS) using their normal installers;
   see [debian.org](https://www.debian.org/CD/http-ftp/) or
   [ubuntu.com](https://ubuntu.com/download/desktop). Any standard desktop
   install works; you don't need anything special.
2. Once installed, you're already "at" the terminal. You don't need SSH at
   all if you're going to keep using this machine directly with its own
   screen/keyboard. Open a terminal application (search for "Terminal" in
   your applications menu) and skip to [section
   5](#5-a-five-minute-terminal-survival-kit).
3. If instead you want to run it headless (no monitor) and control it from
   another computer, install `openssh-server` (`sudo apt install
   openssh-server`) and continue to [section
   4](#4-connect-to-your-host-for-the-first-time).

## 4. Connect to your host for the first time

Skip this section entirely if you're on a laptop/desktop using its own
screen and keyboard.

**SSH** ("Secure Shell") is how you get a terminal *on* another computer
over the network, from your own computer. You type commands on your laptop;
they run on the Pi.

### Find the Pi's address

Give the Pi a minute or two after first power-on, then try, from your setup
computer's own terminal (see below for how to open one):

```bash
ssh <username>@<hostname>.local
```

using the username and hostname you set in Raspberry Pi Imager (e.g. `ssh
pi@bioprinter1.local`). If that doesn't resolve, find its IP address instead
by checking your router's admin page (usually `192.168.0.1` or
`192.168.1.1` in a browser, look for "connected devices" or "DHCP
clients"), then use `ssh <username>@<that-ip-address>` instead.

### Opening a terminal on your setup computer

- **Windows 10/11**: press the Start key, type `Terminal`, open it. `ssh`
  is built in.
- **Mac**: open Spotlight (Cmd+Space), type `Terminal`, open it.
- **Linux**: open your applications menu, search for `Terminal`.

### First connection

Run the `ssh` command from above. The first time, you'll see a warning
about the host's authenticity that looks alarming but is normal for a
first connection. Type `yes` and press Enter. Then enter the password you
set in Raspberry Pi Imager (typing is invisible on purpose: no characters
or dots will appear, just type it and press Enter).

You're now at a **prompt** that looks something like:

```
pi@bioprinter1:~ $
```

Everything from here on, when this guide says "run `X`", means: type `X`
at this prompt and press Enter.

## 5. A five-minute terminal survival kit

You'll only need a handful of commands for this whole process:

| Command | What it does |
|---|---|
| `pwd` | Print where you currently are ("present working directory") |
| `ls` | List files in the current directory |
| `cd foo` | Move into directory `foo` |
| `cd ..` | Move up one directory |
| `cd ~` or just `cd` | Jump straight home (`/home/<you>`) |
| `cat file.txt` | Print a file's contents to the screen |
| `nano file.txt` | Open a file in a simple on-screen text editor |
| `sudo <command>` | Run `<command>` with administrator privileges (asks for your password) |
| Ctrl+C | Cancel/interrupt whatever's currently running |
| Ctrl+Shift+V (or right-click → Paste) | Paste into the terminal |
| Tab | Autocomplete a file/folder name you've started typing |
| Up arrow | Recall your previous command |

**About `nano`**: when a step says "edit this file with nano", run `nano
<path>`, use the arrow keys to move around, type to insert text, and when
you're done press **Ctrl+O** then **Enter** to save ("Write Out"), then
**Ctrl+X** to exit.

You do not need to memorize any of this. Keep this table open in another
tab and refer back to it.

## 6. Install BioKalico

From your terminal (SSH'd into the Pi, or a local terminal on your
laptop), run:

```bash
git clone --recurse-submodules https://github.com/nicholas-karr/BioKalico.git ~/klipper
bash ~/klipper/scripts/biokalico-installer.sh install
```

- The first line downloads ("clones") this repository into `~/klipper`
  (`~` means "your home directory").
- The second line runs the installer, which:
  1. Installs the system packages needed to build Klipper+firmware, Moonraker, Mainsail, and Crowsnest.
  2. Sets up `~/klippy-env` (Klipper's own isolated Python environment) and
     the `klipper` background service.
  3. Seeds `~/printer_data/config/moonraker.conf` from this repo's template
     ([`bio_config/moonraker.conf.example`](bio_config/moonraker.conf.example)),
     if one doesn't already exist.
  4. Sets up Moonraker (from `deps/moonraker`) and mainsail-config, builds
     Mainsail from source (from `deps/mainsail`, via `npm run build`), and
     sets up Crowsnest (from `deps/crowsnest`), including the `nginx` web
     server config that actually makes Mainsail reachable in a browser (see
     section 10). The SLA projector UI, firmware build panel, `$HOME`
     config root, and shared-password login all come built-in with
     Moonraker/Mainsail themselves - no separate copy-in step.
  5. Asks, right at the start, whether this printer has an SLA projector.
     Answer yes only if it does: that sets up the projector display server
     (see [scripts/sla/README.md](scripts/sla/README.md)) and, on a Pi with
     no desktop, a minimal graphical login that the projector needs. Answer
     no (the default) and your Pi's desktop or command-line setup is left
     exactly as it is.

This takes a while (several minutes) the first time. It's building things
and downloading packages. It's safe to re-run any time; it just updates
everything to the latest version (and never overwrites config it already
seeded). You can check what's installed later with `bash
~/klipper/scripts/biokalico-installer.sh status`.

At this point Moonraker and Mainsail are both fully up if you visit
Mainsail's web page (see section 10), but Klipper itself won't start yet,
since it doesn't have a `printer.cfg` to read. That's what section 9
builds.

## 7. Find your controller boards' device paths

Your printer's mainboard (and any secondary boards, like a toolhead board)
show up to the host as USB serial devices. `printer.cfg` needs to know
exactly which one is which, but the obvious-looking name Linux gives them,
like `/dev/ttyACM0`, isn't reliable: it's assigned in whatever order the
boards happen to enumerate at boot, which can change. Instead we use
`/dev/serial/by-id/`, a folder of names built from each board's actual USB
serial number, which never changes for a given physical board.

With **all** your boards plugged in and powered on, run:

```bash
ls -l /dev/serial/by-id/
```

You'll see one line per board, something like:

```
usb-Klipper_stm32h743xx_2A0052000151323236333534-if00 -> ../../ttyACM0
usb-Klipper_rp2040_3232323236195ADE-if00 -> ../../ttyACM1
```

The long name on the left (starting `usb-Klipper_...`) is what you want.
If you have more than one board and aren't sure which line is which
physical board, unplug just one of them and run the command again. The
line that disappeared is that board. Repeat for each board, noting down
which full `usb-Klipper_...` name belongs to which physical board (e.g.
"mainboard" vs. "toolhead board").

Write these down. You'll paste them into `printer.cfg` in the next
section.

## 8. Plug in your webcam

If you have a USB webcam, just plug it in now. A single webcam almost
always lands at `/dev/video0`, which is what Crowsnest is already set up
to use out of the box (section 12); there's usually nothing to configure.

## 9. Write your printer.cfg

`moonraker.conf` doesn't need any attention here. The installer in section
6 already wrote one for you at `~/printer_data/config/moonraker.conf`
(from [`bio_config/moonraker.conf.example`](bio_config/moonraker.conf.example)),
with the `[include ...]` lines that wire up this repo's BioKalico extras
already in place. It only does this the first time (it never overwrites an
existing `moonraker.conf`), so if you already had one from a previous
install, yours was left alone.

`printer.cfg` is the one file the installer can't write for you. Your
MCU serial paths, kinematics, and thermistor types are specific to your
printer, so there's no generic default that would actually be correct.
This repo ships templates under [`bio_config/`](bio_config/) for two
specific printers (BioTrident, Printess). If yours is one of those,
they're a real starting point. If not, they're still useful as *examples*
of the structure, but you'll want to start from a template matching your
own mainboard instead. See [`config/`](config/) for board-specific
starting points (search for your board name, e.g.
`config/generic-bigtreetech-octopus-v1.1.cfg`), or the official [Klipper
config reference](https://www.klipper3d.org/Config_Reference.html).

1. Copy a template into place. For example, for BioTrident:
   ```bash
   cp ~/klipper/bio_config/biotrident.cfg ~/printer_data/config/printer.cfg
   ```
2. Edit it:
   ```bash
   nano ~/printer_data/config/printer.cfg
   ```
   Find the `[mcu]` section(s) near the top and replace the placeholder
   `serial:` value with the real `usb-Klipper_...` path(s) you found in
   section 7. Read through the rest of the file too; template comments
   (lines starting with `#`) call out other things worth checking, like
   thermistor types and bed size. Save with Ctrl+O, Enter, then exit with
   Ctrl+X.

If your `printer.cfg` has an `[image_display]` section (the BioTrident
template does) but you answered no to the SLA projector question in section
6, set up the projector display server now:

```bash
bash ~/klipper/scripts/biokalico-installer.sh update image-display
```

Until this file exists, `klipper.service` will keep restarting every 10
seconds reporting a missing config. That's expected, and doesn't stop
Moonraker or Mainsail from working; it just means the printer itself isn't
connected yet.

## 10. First boot

Restart everything so it picks up the config you just wrote:

```bash
~/klipper/scripts/printer-services.sh --restart
```

Then, from your setup computer's **browser** (not the terminal), visit:

```
http://<hostname>.local/
```

(e.g. `http://biokalico.local/`), or `http://<ip-address>/` if `.local`
doesn't resolve for you (see section 4 for finding the IP). You should see
the Mainsail interface. If the top bar says something like "Printer is
ready", you're fully up. Otherwise, check section 15.

## 11. Flash your MCU firmware

Once `printer.cfg` has your board(s) configured, Mainsail gets a
**Firmware** card under Settings that builds and flashes them straight
from the browser, no button presses, no separate flashing tools. See
[biokalico_extras/firmware_flash.md](biokalico_extras/firmware_flash.md)
for the full detail (including how to add a preset for a board that isn't
already covered); the short version:

1. In Mainsail, go to **Settings → Firmware**.
2. Each configured `[firmware_build ...]` target shows its current
   build/flash status.
3. Click **Build & Flash** for a target. Watch the log; it takes a couple
   of minutes per board.

## 12. Get the webcam working

Crowsnest was already installed by `biokalico-installer.sh`, using a default config
(`device: /dev/video0`, 640x480, 15fps) that just works for a single USB
webcam. Check Mainsail's camera view now, it's probably already streaming.

If it's not (blank, or errors in Crowsnest's log), your webcam likely
enumerated at a different device number. Check what's actually present:

```bash
ls /dev/video*
```

Then edit the config:

```bash
nano ~/printer_data/config/crowsnest.conf
```

and update `device:` under `[cam 1]` to match, then restart:

```bash
~/klipper/scripts/printer-services.sh --restart
```

## 13. Remote access with a Cloudflare Tunnel

By default Mainsail is only reachable on your local network. A **Cloudflare
Tunnel** lets you reach it from anywhere, without opening any ports on your
router or exposing your home IP address. `cloudflared` (a small program
running on the Pi) makes an outbound connection to Cloudflare, and
Cloudflare relays traffic to it.

> **Security note**: Computers on your local network open Mainsail without a
> password. Anyone connecting through the tunnel must enter a password
> first. The password is created the first time Moonraker starts; to see
> it, run `grep password: ~/printer_data/config/moonraker.conf` on the Pi.
> To require the password on your local network too, set
> `local_bypass: False` under `[simple_password_auth]` in that file. A
> Cloudflare **Quick Tunnel** (below) gets you a random, hard-to-guess URL
> for occasional use, which is fine for testing. For anything long-lived,
> also set up [Cloudflare
> Access](https://developers.cloudflare.com/cloudflare-one/policies/access/)
> in front of it (free for personal use) so it asks for a login before
> showing your printer to the internet.

### Quick tunnel (no account needed, temporary URL)

Good for trying it out. Install `cloudflared`:

```bash
curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb
sudo dpkg -i cloudflared.deb
```

(Use `cloudflared-linux-amd64.deb` instead if you're on a laptop, not a
Pi. If you're not sure, run `uname -m`: `aarch64`/`arm64` means Pi-style,
`x86_64` means laptop-style.)

Then:

```bash
cloudflared tunnel --url http://localhost:80
```

This prints a `https://<random-words>.trycloudflare.com` URL. Open that
from anywhere and you'll reach your Mainsail. It only lasts as long as
this command keeps running (closing the terminal, or a reboot, stops it);
re-running it gives you a new random URL each time.

### Persistent tunnel (free Cloudflare account, permanent URL)

1. Sign up at [dash.cloudflare.com](https://dash.cloudflare.com/sign-up)
   if you don't have an account (a domain isn't required for the tunnel
   itself, though you'll want one, even a cheap one, to get a stable,
   memorable hostname instead of a random one).
2. In the Cloudflare dashboard, go to **Zero Trust → Networks → Tunnels →
   Create a tunnel**, choose "Cloudflared", and give it a name (e.g.
   `biokalico`).
3. It'll show you an install command for your platform. On the Pi/laptop,
   run the one-liner it gives you (this installs `cloudflared` *and*
   registers it as a systemd service that survives reboots, unlike the
   quick tunnel above).
4. Back in the dashboard, add a **Public Hostname** for the tunnel: pick a
   subdomain (e.g. `printer.yourdomain.com`), service type `HTTP`, URL
   `localhost:80`.
5. Visit your chosen hostname from anywhere. It should show Mainsail.

Check the tunnel's status any time with:

```bash
sudo systemctl status cloudflared
```

## 14. Turn on the BioKalico extras

If you're using one of the `bio_config/` templates and the
`moonraker.conf.example` template from section 9, the SLA projector
controls, the firmware build panel, and the `$HOME` config-file root in
Mainsail are already wired up. `biokalico-installer.sh install` already
built/set up Moonraker and Mainsail (section 6) with these features built
in, and the `[include ...]` lines in `moonraker.conf` load their
per-printer config.

To confirm: in Mainsail, check Settings → Firmware exists (section 11), and
if you have a projector attached, look for the SLA projector power toggles
on the dashboard. If either is missing, restart services (section 10) and
check [biokalico_extras/README.md](biokalico_extras/README.md), which
covers this setup in full, useful if you're troubleshooting or wrote your
own `moonraker.conf` from scratch instead of the template.

For the actual SLA printing workflow (video pipeline, macros, slicer
setup) once the hardware side above is working, see
[scripts/sla/README.md](scripts/sla/README.md).

## 15. Troubleshooting

**Nothing loads in the browser.** Double check the Pi is powered on and on
the network (its power LED and, if wired, Ethernet link lights should be
lit). Re-check the hostname/IP from section 4.

**Mainsail loads but says the printer is in an error state, or
"klippy disconnected".** Check the Klipper log:

```bash
tail -n 50 ~/printer_data/logs/klippy.log
```

The last few lines almost always say exactly what's wrong. A common
first-time issue is a typo'd `serial:` path in `printer.cfg` (recheck
section 7), or a config section referencing hardware you haven't set up
yet (comment it out with a leading `#` on every line, for now).

**See everything at once, live**, while you try to reproduce a problem:

```bash
~/klipper/scripts/printer-services.sh --logs
```

This tails every service's log together, interleaved by time. Ctrl+C to
stop watching (this doesn't stop the services, just stops watching them).

**After any config change**, restart:

```bash
~/klipper/scripts/printer-services.sh --restart
```

**Still stuck?** Search the exact error text from `klippy.log` - Klipper's
error messages are usually specific enough to search for directly, and
most first-time issues (wrong pin, wrong thermistor type, etc.) turn up
existing answers.

You can also paste the error text into an AI tool (Google's AI overview,
ChatGPT, Claude, etc.) and ask it to explain what's wrong. Include the
relevant lines from `klippy.log`, your `printer.cfg` section for the part
that's failing, and a screenshot if the problem is visual (bad print,
UI error) - the more specific context you give it, the more useful the
answer.

**The whole box goes unresponsive, or a remote tunnel silently drops for
hours.** This is almost always the Linux kernel running out of memory and
swap at the same time, not a crash - the machine can look "frozen" from the
outside for a long time before the kernel's OOM killer finally frees enough
to recover on its own, with no reboot involved. `biokalico-installer.sh
install`/`update` now applies three protections automatically, every run:

- `/tmp` is capped (25% of RAM, up to 4G) instead of the systemd default of
  50% of RAM, so a runaway process filling it up hits an error instead of
  taking down the whole system. Takes effect on the next reboot.
- `cloudflared`, `klipper.service`, and `moonraker.service` are marked
  low-priority-to-kill (`OOMScoreAdjust=-900`), so if memory does run out,
  the kernel goes after anything else first.
- `systemd-oomd` is installed and enabled, which watches memory/swap
  pressure continuously and kills the actual offending process early,
  instead of the kernel doing it as a last resort once everything is
  already thrashing.

## 16. Where to go next

- [README.md](README.md): project overview and the full documentation
  index.
- [biokalico_extras/README.md](biokalico_extras/README.md): deep dive on
  how the Moonraker/Mainsail features in section 14 actually work, for when
  you outgrow the template `moonraker.conf`.
- [biokalico_extras/firmware_flash.md](biokalico_extras/firmware_flash.md):
  full detail on the firmware panel from section 11, including how to
  add support for a board that doesn't have a preset yet.
- [scripts/sla/README.md](scripts/sla/README.md): the SLA video pipeline,
  once your hardware is up.
- [klippy/extras/VENDORED.md](klippy/extras/VENDORED.md): toolchanger
  support.
- [Klipper's own config
  reference](https://www.klipper3d.org/Config_Reference.html): every
  config option this firmware supports, for tuning beyond the templates.
