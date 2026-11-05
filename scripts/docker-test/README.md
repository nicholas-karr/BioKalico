# Docker install test

Runs `scripts/biokalico-installer.sh install` inside a fresh, minimal Debian
container and checks that a working app actually comes up (Moonraker,
Mainsail-over-nginx, Crowsnest, and Klipper; the last one is expected to
be restart-looping, see below), without touching real printer hardware.
Useful before pushing a change to `biokalico-installer.sh`, the `bio_config/`
templates, or anything else the install flow depends on.

It tests the **current working tree**, uncommitted changes included, not
whatever's currently on GitHub, by snapshotting it into a throwaway local
git repo and pointing the install script at that instead of the real
remote (see `run.sh`'s `BIOKALICO_ORIGIN` override). `deps/mainsail` and
`deps/moonraker` (BioKalico's own forks) get the same treatment: each
one's working tree, uncommitted changes included, is snapshotted into its
own throwaway local repo, bind-mounted into the container, and
`.gitmodules` is rewritten to fetch from that local mount instead of the
real GitHub origin - so testing today's changes inside either submodule
doesn't require pushing them first. `deps/crowsnest` is the exception: it's
upstream `mainsail-crew/crowsnest`, unmodified here, so it's re-pinned as a
real gitlink at whatever commit is currently checked out and fetched fresh
from its real origin (GitHub) inside the container - same as a real host
would - which needs real network access for that one submodule.

## Requirements

- Docker (Engine on Linux, Desktop on macOS/Windows). Nothing else: the
  container image only has what a genuinely bare host would have before
  running `biokalico-installer.sh` (systemd, `sudo`, `git`); everything else the
  installer needs, it installs itself, same as it would on a real host.
- The commands below are identical on Linux, macOS, and Windows (run from
  PowerShell or a WSL2 terminal). That's the reason this uses Docker
  specifically rather than a Linux-only tool like Podman or LXC.

**One-time Docker install, if you don't already have it:**

- **Linux**: `sudo apt-get install -y docker.io && sudo systemctl enable --now docker`
  (Debian/Ubuntu; use your distro's package manager otherwise)
- **macOS / Windows**: install [Docker
  Desktop](https://www.docker.com/products/docker-desktop/) and make sure
  it's running.

## Running it

```bash
scripts/docker-test/run.sh
```

This builds the image, starts the container, clones the current working
tree into it, runs the installer, then runs a set of checks and prints a
PASS/FAIL summary. Takes a few minutes the first time (downloading
packages, Moonraker, Mainsail, and building Mainsail from source with
`npm ci && npm run build` - there's no more prebuilt release zip to just
download).

If you don't have bash (plain Windows without WSL or Git Bash), run the
same steps by hand from PowerShell. This is exactly what `run.sh` does,
just spelled out:

```powershell
# 1. Snapshot the current tree into a temp git repo (so uncommitted changes
#    get tested too, not just what's on GitHub)
git clone . C:\temp\biokalico-snapshot

# 2. Build and start the container
docker build -t biokalico-install-test scripts\docker-test\
docker run -d --name biokalico-install-test `
    --privileged --cgroupns=host `
    -v /sys/fs/cgroup:/sys/fs/cgroup:rw `
    --tmpfs /run --tmpfs /run/lock `
    -v C:\temp\biokalico-snapshot:/mnt/biokalico-snapshot:ro `
    -p 127.0.0.1:18080:80 -p 127.0.0.1:17125:7125 `
    biokalico-install-test

# 3. Install, pointed at the snapshot instead of the real GitHub repo
docker exec -u biokalico biokalico-install-test git clone /mnt/biokalico-snapshot /home/biokalico/klipper
docker exec -u biokalico -e BIOKALICO_ORIGIN=/mnt/biokalico-snapshot biokalico-install-test bash /home/biokalico/klipper/scripts/biokalico-installer.sh install

# 4. Check it worked
docker exec biokalico-install-test systemctl is-active moonraker crowsnest nginx
docker exec biokalico-install-test curl -sf http://127.0.0.1:7125/server/info
docker exec biokalico-install-test curl -sf http://127.0.0.1/
```

(`git clone .` from the repo root only picks up committed changes, unlike
`run.sh`'s `rsync`-based snapshot. Good enough for a quick manual check,
but stage/commit first if you're testing uncommitted work this way.)

## What "success" means here

There's no real MCU or webcam inside a container, so:

- `moonraker`, `crowsnest`, and `nginx` should all be **active**.
- `klipper` is **expected** to be restart-looping. There's no
  `printer.cfg` (deliberately not auto-seeded, see `QUICKSTART.md` section
  9). The test checks its log specifically says that's why, so a
  different, unexpected failure still fails loudly.
- Moonraker's API (`:7125/server/info`) and Mainsail-over-nginx (`:80`,
  proxying to Moonraker) should both respond.
- `printer_data/config/moonraker.conf` should exist and reference
  `biokalico_extras` (proves it was seeded from
  `bio_config/moonraker.conf.example`).
- `deps/{mainsail,moonraker,crowsnest}` should all be checked out (submodule
  fetch succeeded), and `deps/mainsail/dist/index.html` should exist
  (on-host `npm run build` actually produced output).

## Simulated printer

`run.sh --sim` goes one step further: once the install checks pass, it turns
the container into a printer that does not exist and runs the end-to-end
tests against it. Klipper, Moonraker and Mainsail are the real ones, built
from your working tree; only the microcontrollers are replaced, using
Klipper's file-output mode, so nothing is ever sent to hardware.

What `sim/setup.sh` (runs in the container) sets up:

- Data dictionaries built from this tree for the stm32h743 and rp2040
  boards. They are built fresh because a dictionary from another revision
  cannot encode the commands klippy sends.
- `printer.cfg` made from `bio_config/biotrident.cfg`, the template that is
  deployed to real printers, with only the serial paths changed and
  `sim/overlay.cfg` layered on top. The overlay adds what the template
  leaves out: docks, motion macros for two toolheads that log what they are
  asked to do, and a toolhead on a non-critical board that starts unplugged.
- `klipper.service` switched to file-output mode.
- Moonraker letting requests from the host in without the shared password.
- The material slots filled through Moonraker, the same way Mainsail fills
  them. The config never says what a slot holds.

`sim/e2e.py` (runs on the host) then drives the Moonraker API: manual state
edits, automatic exchanges, the prompt for a toolhead that has to be mounted
by hand (including that nothing moves while it waits), SLA vat switching,
print preflight, plugging the fake board in, and a Klipper restart. With
Chromium and `python3-websocket` installed it also drives the Mainsail UI with
real clicks and saves screenshots (the directory is printed, or set
`SIM_SHOTS_DIR`). To re-run the checks against a container that is already
set up: `python3 scripts/docker-test/sim/e2e.py --ui --shots DIR`.

Limits: a board that is plugged in cannot be unplugged again while klippy
runs, because file-output mode has no connection to lose. That direction is
covered by `test/test_toolheads.py`.

## After it runs

The container is left running (whether checks passed or failed) so you can
poke at it:

```bash
docker exec -it -u biokalico biokalico-install-test bash
```

Or open `http://127.0.0.1:18080/` in a browser on the host to see Mainsail
itself (mapped from the container's port 80). Tear it down when you're
done:

```bash
docker rm -f biokalico-install-test
docker rmi biokalico-install-test   # optional, drops the built image too
```

## Why systemd-in-Docker

`biokalico-installer.sh` manages everything through `systemctl` (enable, restart,
status checks), and the test needs those services actually running to
verify them, not just have the install script not error out. The
`Dockerfile` here runs real `systemd` as PID 1 inside the container (the
standard, widely-documented pattern for this), which needs a few
non-default `docker run` flags (`--privileged --cgroupns=host`, the
cgroup bind-mount, tmpfs for `/run`), all already wired up in `run.sh`.
This repo's other Dockerfile (`scripts/Dockerfile`, for running Klipper
itself in a container) takes a different, simpler approach: it stubs
`systemctl` out to a no-op, but that only works because that Dockerfile
doesn't need to verify services actually started, just that the install
script didn't fail while calling into a no-op.
