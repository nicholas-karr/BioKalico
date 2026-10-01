# Ambient Recorder

A continuous, low-frame-rate H.264 timelapse of the printer, compiled into
one video file per calendar day. It runs around the clock, independent of
whether a print is happening - so there's always a record of what the machine
did, print or no print. This is a separate feature from the per-print
timelapse (`deps/moonraker/moonraker/components/timelapse.py`), which only
captures during a print.

Size is the priority: frames are pulled one at a time from crowsnest's
snapshot endpoint and piped straight into a single long-lived
ffmpeg/libx264 process, so interframe (P-frame) compression is doing the
work. Hours of an unchanging scene cost almost nothing on disk. Playback is a
sped-up timelapse (a full day is about 2.4 hours at the default settings,
not a 24-hour real-time file) at a deliberately low 1 fps: each captured
frame gets a full second of playback, so standard "seek +-1s" player
controls land exactly on each captured frame - easier to browse frame by
frame than a higher framerate would allow, at the cost of a longer finished
video. Raise `output_framerate` for a shorter, faster-moving result instead.

## Turning it on

The recorder is installed by `scripts/biokalico-installer.sh` as part of a
normal install/update, but it ships **off**. To enable it:

1. Edit `~/printer_data/config/ambient-recorder-config.yaml` (seeded from
   [../../bio_config/ambient-recorder-config.yaml.example](../../bio_config/ambient-recorder-config.yaml.example)
   on first install) and set `enabled: true`.
2. Apply it:
   ```
   scripts/biokalico-installer.sh update ambient-recorder
   ```
   That turns the `ambient-recorder` systemd service on (or off) to match the
   config, and needs no other steps.

To turn it back off, set `enabled: false` and run the same command.

## Where the videos go

- Finished per-day videos: `~/printer_data/timelapse/ambient/YYYY-MM-DD.mp4`
- In-progress footage: `~/printer_data/timelapse/ambient/tmp/` (one "session"
  file per run of the service; these are concatenated into the day's file
  once the day is over, then removed). Since each session file is already a
  valid, playable mp4 the moment it exists (see "Reboots and crashes" below),
  today's recording-so-far is just whichever session file is currently
  growing in there - no separate "preview" step needed.

`output_dir` defaults to a subfolder of Moonraker's own per-print timelapse
directory specifically so both show up in Mainsail's Timelapse tab without
any extra file-root setup - that tab just browses whatever's physically
nested under `~/printer_data/timelapse/`, the same way it already shows that
component's own `tmp/` folder.

## Reboots and crashes

The current session file is a fragmented mp4, so it stays a valid, playable
file as it grows - an unclean shutdown (power loss, `kill -9`) loses at most
the last open fragment (bounded by `keyframe_interval_seconds`, default 60s),
never the whole file. A day is only finalized once it is fully over, so a
mid-day reboot just leaves several session files for that day, all
concatenated together when the day rolls over. The service restarts itself on
failure and comes back automatically after a reboot.

## Requirements

- `ffmpeg` (already a dependency of this stack).
- A working camera served by crowsnest (the default `snapshot_url` points at
  crowsnest's stock `[cam 1]` on port 8080).
- No Python packages beyond the standard library - the daemon runs under the
  system `/usr/bin/python3`.
