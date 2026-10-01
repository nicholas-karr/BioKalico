# Fixing a Monitoring Alert with a Coding Agent

You got an email like "Monitoring alert on `<hostname>`" because
[`monitoring.sh`](../monitoring.sh) found something wrong on the printer
host: disk space, a failed or crash-looping service, an expiring SSL cert,
an unreachable HTTP endpoint, or Klipper stuck in an error state. The alert
text itself already says what's wrong; it's the starting point for
everything below.

This walks through installing a coding agent - a tool that can read logs,
run commands, and edit files on its own, not just chat - on the printer
host, and using it to investigate and fix the alert. It assumes you already
have terminal or SSH access to the host. If you don't yet, or need a
refresher, see [QUICKSTART.md](../QUICKSTART.md) sections 4 and 5 first.

You can use any coding agent for this. This guide is written for Claude
Code, though, so if you pick a different one, the rest of the guide won't
apply as written: follow that agent's own install and usage instructions
instead.

## 1. Connect to the host and go to the repo

```bash
ssh <username>@<hostname>.local
cd ~/klipper
```

`~/klipper` is this repo's checkout on the host - it's what `monitoring.sh`
lives in, and it's also where the printer's own code (Klipper, the
Moonraker/Mainsail forks) lives, which matters if the alert turns out to be
printer-related rather than purely a host issue.

## 2. Install Claude Code

[Claude Code](https://claude.com/product/claude-code) is Anthropic's coding
agent. It runs in the terminal, and unlike a plain chat window, it can
actually read your logs and configs, run diagnostic commands, and make
edits, asking you before anything that changes something.

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

Confirm it installed:

```bash
claude --version
```

You'll need a Claude account to use it - a
[Claude Pro, Max, Team, or Enterprise plan](https://claude.com/pricing), or
a pay-as-you-go [Claude Console](https://platform.claude.com/) account. If
you don't have one yet, the next step will prompt you to sign up or log in
in your browser.

## 3. Start a session and hand it the alert

From `~/klipper`:

```bash
claude
```

The first time, it'll open your browser to log in. Once you're at the
prompt, paste in the full text of the alert email and ask it to look into
it, e.g.:

```
I got this monitoring alert on this printer host, can you investigate and
fix it?

<paste the full alert email text here>
```

The more of the email you paste, the better - `monitoring.sh` puts the
specific service name, error, or percentage right in the alert text, so
there's usually no guessing involved about what's failing.

If the agent needs more context to figure out what's going on, it's safe to
also tell it to read `monitoring.sh` and
`~/printer_data/config/monitoring.conf` (the config that defines which
services, ports, and certs are being watched), so it understands what the
check that fired was actually looking for.

## 4. Review what it does

Claude Code asks before running commands or editing files, unless you've
set it to a more autonomous permission mode. For a monitoring alert, it's
generally safe to approve anything read-only - `journalctl`, `systemctl
status`, `df -h`, `cat` on a log file - without a second thought. Slow down
and read before approving anything that changes state: restarting or
stopping a service, deleting files, or editing config, especially if a
print might be running. If you're not sure what a command it wants to run
actually does, ask it to explain before approving.

## 5. Confirm the fix

`monitoring.sh` runs from cron every 15 minutes, so the alert clears itself
on its own once the underlying problem is gone. To check sooner, run it by
hand:

```bash
bash ~/klipper/monitoring.sh
```

No output means nothing's currently wrong. If it emails again, the fix
didn't fully take, or a different check is now failing - hand the agent the
new alert text the same way.

## If it can't fix it

Some alerts need a decision only you can make (which files to delete to
free disk space, whether to renew or replace an expiring cert, hardware
that needs physically reseating). The agent can still do the legwork -
showing you what's using the disk, or exactly what's failing and where -
even when the last step is yours. For anything printer-specific rather than
host-specific, see [QUICKSTART.md's troubleshooting
section](../QUICKSTART.md#15-troubleshooting) and
[README.md](../README.md)'s documentation index.
