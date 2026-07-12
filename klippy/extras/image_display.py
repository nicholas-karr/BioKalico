# image_display.py - Kalico extra for the image-display TCP server
#
# Maintains a persistent connection to the image-display server and exposes
# G-code commands for SLA projector control. Commands block the G-code queue
# via reactor.pause() polling so that layer timing is exact.
#
# Configuration (printer.cfg):
#   [image_display]
#   host: 127.0.0.1   # image-display server host
#   port: 5555        # image-display server port
#   timeout: 5.0      # per-command timeout in seconds
#   screen_detect_path: /sys/class/drm/card1-HDMI-A-1/status
#                       # optional: sysfs DRM status file; when this reads
#                       # "connected" PROJECTOR_ON succeeds even if the serial
#                       # command never responds (user pressed physical button)

import json
import logging
import os
import queue
import socket
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 0.002   # 2 ms reactor poll interval when waiting for a response
_PROJECTOR_POLL = 0.5    # dwell interval while waiting for projector (keeps Klipper BUSY)
_PROJECTOR_KICK_INTERVAL = 5.0  # min seconds between re-issued PROJECTOR_ON serial kicks
_STANDBY_DELAY = 300.    # 5 minutes
_RECONNECT_WARN_INTERVAL = 60.  # minimum seconds between reconnection warnings
_STATUS_POLL_INTERVAL = 5.0     # seconds between background status polls
_STATUS_STALE_THRESHOLD = 3 * _STATUS_POLL_INTERVAL  # cache older than this is stale
_THREAD_JOIN_TIMEOUT = 2.0      # seconds to wait for worker/poll threads to exit


class ImageDisplay:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.host = config.get('host', '127.0.0.1')
        self.port = config.getint('port', 5555)
        self.timeout = config.getfloat('timeout', 5., above=0.)
        self._load_timeout = config.getfloat('load_timeout', 120., above=0.)
        self.h_offset = config.getfloat('h_offset', 0.)

        self._startup_timeout = config.getfloat('startup_timeout', 300., minval=0.)
        self.screen_detect_path = config.get('screen_detect_path', None)

        self._sock = None
        self._rbuf = b''
        # Guards self._sock/self._rbuf, which are written from both the
        # worker thread (_connect/_close) and the reactor thread (_close via
        # _on_disconnect), to avoid the worker overwriting self._sock right
        # after a shutdown-triggered _close() decided there was nothing to
        # close.
        self._sock_lock = threading.Lock()
        self._stopping = threading.Event()
        self._cmd_queue = queue.Queue()
        self._standby_timer = None
        self._standby_waketime: float = 0.0
        self._last_reconnect_warn = 0.

        # Background status cache, populated by a dedicated poll thread so
        # get_status() never blocks the reactor and never contends with the
        # command queue (the poll uses its own short-lived socket).
        self._status_cache: dict = {}
        self._status_cache_time: float = 0.0
        self._stop_poll = threading.Event()

        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

        self._poll_thread = threading.Thread(
            target=self._status_poll_loop, daemon=True
        )
        self._poll_thread.start()

        gcode = self.printer.lookup_object('gcode')
        for name, handler, desc in [
            ('PROJECTOR_ON', self.cmd_PROJECTOR_ON,
             "Turn on the projector"),
            ('PROJECTOR_OFF', self.cmd_PROJECTOR_OFF,
             "Turn off the projector"),
            ('DISPLAY_IMAGE', self.cmd_DISPLAY_IMAGE,
             "Display an image on the projector. PATH=<path> [ROTATION=<0/90/180/270>]"),
            ('CLEAR_DISPLAY', self.cmd_CLEAR_DISPLAY,
             "Clear the projector display"),
            ('LOAD_VIDEO', self.cmd_LOAD_VIDEO,
             "Pre-load a video for frame display. NAME=<name> PATH=<path>"),
            ('SHOW_VIDEO_FRAME', self.cmd_SHOW_VIDEO_FRAME,
             "Show a specific frame of a loaded video. NAME=<name> FRAME=<index>"),
            ('UNLOAD_VIDEO', self.cmd_UNLOAD_VIDEO,
             "Unload a previously loaded video. NAME=<name>"),
            ('UNLOAD_ALL_VIDEOS', self.cmd_UNLOAD_ALL_VIDEOS,
             "Unload all currently loaded videos"),
            ('LIST_VIDEOS', self.cmd_LIST_VIDEOS,
             "List all videos currently loaded in the image-display server"),
            ('LOAD_VIDEOS_FROM_GCODE', self.cmd_LOAD_VIDEOS_FROM_GCODE,
             "Pre-load all videos referenced in a G-code file. GCODE_PATH=<path>"),
            ('PROJECTOR_STANDBY', self.cmd_PROJECTOR_STANDBY,
             "Turn off the projector after 5 minutes if no image_display commands arrive"),
            ('_SET_IMAGE_H_OFFSET', self.cmd_SET_IMAGE_H_OFFSET,
             "Set horizontal display offset in pixels. OFFSET=<pixels>"),
        ]:
            gcode.register_command(name, handler, desc=desc)

        self.printer.register_event_handler('klippy:disconnect', self._on_disconnect)

    # -------------------------------------------------------------------------
    # Worker thread: owns the socket and all blocking I/O
    # -------------------------------------------------------------------------

    def _connect(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect((self.host, self.port))
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with self._sock_lock:
            if self._stopping.is_set():
                # Shutdown was requested while we were connecting; don't let
                # this socket become an unreachable, unclosed leak.
                try:
                    s.close()
                except Exception:
                    pass
                raise OSError("image_display: shutting down")
            self._sock = s
            self._rbuf = b''
        logger.info("image_display: connected to %s:%d", self.host, self.port)

    def _close(self):
        with self._sock_lock:
            sock = self._sock
            self._sock = None
            self._rbuf = b''
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def _recvline(self) -> bytes:
        while b'\n' not in self._rbuf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise OSError("image_display: server closed the connection")
            self._rbuf += chunk
        line, self._rbuf = self._rbuf.split(b'\n', 1)
        return line

    def _schedule_reconnect_warning(self):
        """Called from the worker thread; pushes a rate-limited warning to the UI."""
        now = time.monotonic()
        if now - self._last_reconnect_warn < _RECONNECT_WARN_INTERVAL:
            return
        self._last_reconnect_warn = now
        reactor = self.printer.get_reactor()
        reactor.register_callback(self._emit_reconnect_warning)

    def _emit_reconnect_warning(self, eventtime):
        """Called on the reactor thread; writes a warning to the Klipper console."""
        gcode = self.printer.lookup_object('gcode')
        gcode.respond_info(
            "WARNING: image_display server connection was lost and re-established. "
            "If the server process itself restarted, previously loaded videos will "
            "need to be reloaded with SLA_LOAD_GCODE_VIDEOS."
        )

    def _worker_loop(self):
        while True:
            item = self._cmd_queue.get()
            if item is None:
                break
            cmd, result_q = item
            for attempt in range(2):
                try:
                    if self._sock is None:
                        self._connect()
                    self._sock.sendall(json.dumps(cmd).encode() + b'\n')
                    response = json.loads(self._recvline())
                    result_q.put(('ok', response))
                    break
                except Exception as e:
                    self._close()
                    if attempt == 0:
                        logger.warning(
                            "image_display: send failed (%s), reconnecting", e)
                        self._schedule_reconnect_warning()
                        continue
                    result_q.put(('err', e))

    # -------------------------------------------------------------------------
    # Reactor-friendly send: yields to Klipper while waiting for the worker
    # -------------------------------------------------------------------------

    def _pause(self, reactor, eventtime):
        """reactor.pause() wrapper that returns None if the reactor raises."""
        try:
            return reactor.pause(eventtime + _POLL_INTERVAL)
        except Exception:
            return None  # reactor is shutting down; callers check for None

    def _send(self, cmd: dict, timeout: float = None) -> dict:
        self._cancel_standby()
        result_q = queue.Queue()
        self._cmd_queue.put((cmd, result_q))

        reactor = self.printer.get_reactor()
        eventtime = reactor.monotonic()
        t = timeout if timeout is not None else self.timeout
        deadline = eventtime + t + 1.

        while eventtime is not None and eventtime < deadline:
            try:
                status, value = result_q.get_nowait()
                if status == 'ok':
                    return value
                return {'status': 'error',
                        'message': 'Connection error: %s' % value}
            except queue.Empty:
                pass
            eventtime = self._pause(reactor, eventtime)

        return {'status': 'error',
                'message': 'Timed out waiting for image-display server'}

    def _require_ok(self, gcmd, response: dict):
        if response.get('status') != 'success':
            raise gcmd.error(
                "image_display: %s" % response.get('message', 'unknown error'))

    # -------------------------------------------------------------------------
    # G-code commands
    # -------------------------------------------------------------------------

    def _is_screen_connected(self):
        """Return True if the DRM sysfs status file reports 'connected'."""
        try:
            with open(self.screen_detect_path, 'r') as f:
                return f.read().strip() == 'connected'
        except OSError:
            return False

    def _xrandr_output(self):
        """Return parsed xrandr output as a list of (name, connected, active) tuples."""
        display = os.environ.get('DISPLAY', ':0')
        xauth = os.environ.get('XAUTHORITY', os.path.expanduser('~/.Xauthority'))
        env = dict(os.environ, DISPLAY=display, XAUTHORITY=xauth)
        try:
            raw = subprocess.check_output(['xrandr'], env=env,
                                          stderr=subprocess.DEVNULL,
                                          timeout=5).decode()
        except subprocess.TimeoutExpired:
            logger.warning("image_display: xrandr query timed out")
            return []
        except Exception as e:
            logger.warning("image_display: xrandr query failed: %s", e)
            return []
        results = []
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] in ('connected', 'disconnected'):
                connected = parts[1] == 'connected'
                # Active outputs have WxH+X+Y geometry in the same line
                active = connected and any('+' in p and 'x' in p for p in parts)
                results.append((parts[0], connected, active))
        return results

    def _activate_screen_output(self):
        """Run xrandr --auto on any connected-but-inactive output."""
        display = os.environ.get('DISPLAY', ':0')
        xauth = os.environ.get('XAUTHORITY', os.path.expanduser('~/.Xauthority'))
        env = dict(os.environ, DISPLAY=display, XAUTHORITY=xauth)
        for name, connected, active in self._xrandr_output():
            if connected and not active:
                logger.info("image_display: activating display output %s", name)
                try:
                    subprocess.call(['xrandr', '--output', name, '--auto'],
                                    env=env, stderr=subprocess.DEVNULL,
                                    timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning("image_display: xrandr --auto %s timed out",
                                   name)
                except Exception as e:
                    logger.warning("image_display: xrandr --auto %s failed: %s",
                                   name, e)

    @staticmethod
    def _status_is_ready(resp):
        """Confirm the server's authoritative STATUS reports the window bound to
        the projector output at its native resolution.

        The server's `display_ready` flag rejects the transient fallback modes
        the compositor briefly presents before applying its saved config, so a
        raw "screen is connected/active" check is not sufficient here.
        """
        if resp.get('status') != 'success' or not resp.get('display_ready'):
            return False
        window = resp.get('window') or {}
        return bool(window.get('width') and window.get('height'))

    @staticmethod
    def _status_summary(resp):
        """Short human-readable status string for logs / timeout errors."""
        if resp.get('status') != 'success':
            return resp.get('message', 'no response from image-display server')
        if not resp.get('projector_available'):
            return 'projector display not connected'
        window = resp.get('window') or {}
        if window.get('width'):
            return ('projector connected, window bound at %sx%s (awaiting native mode)'
                    % (window.get('width'), window.get('height')))
        return 'projector connected, window not yet bound'

    def cmd_PROJECTOR_ON(self, gcmd):
        self._cancel_standby()
        toolhead = self.printer.lookup_object('toolhead')
        reactor = self.printer.get_reactor()

        deadline = reactor.monotonic() + self._startup_timeout
        last_kick = -1e9
        last_status = 'starting'
        while reactor.monotonic() < deadline:
            now = reactor.monotonic()
            # (Re-)issue the turn-on command periodically. Each issue powers the
            # projector's serial link and nudges the server to bind its window;
            # throttle it so we don't spam the serial adapter on every poll.
            if now - last_kick >= _PROJECTOR_KICK_INTERVAL:
                self._cmd_queue.put(({'type': 'PROJECTOR_ON'}, queue.Queue()))
                last_kick = now

            # dwell() advances print_time and blocks via _check_pause() until
            # the MCU buffer drains, same mechanism as G4, so Klipper stays BUSY.
            toolhead.dwell(_PROJECTOR_POLL)

            # Helper nudge only: if the projector connector is present but its
            # output has no framebuffer yet, ask the compositor to bring it up.
            # This is never on its own sufficient to declare the display ready.
            if not self.screen_detect_path or self._is_screen_connected():
                self._activate_screen_output()

            # Authoritative readiness gate: the server confirms its window is
            # bound to the projector output at that output's native resolution.
            # A single STATUS query drives both the gate and the log summary.
            resp = self._send({'type': 'STATUS'})
            if self._status_is_ready(resp):
                window = resp.get('window') or {}
                logger.info("image_display: projector display ready (%sx%s on %s)",
                            window.get('width'), window.get('height'),
                            window.get('display_name') or '?')
                return
            last_status = self._status_summary(resp)
            logger.info("image_display: PROJECTOR_ON not ready (%s)", last_status)

        raise gcmd.error(
            "PROJECTOR_ON: timed out after %.0fs waiting for projector display "
            "(last status: %s)" % (self._startup_timeout, last_status))

    def cmd_PROJECTOR_OFF(self, gcmd):
        self._require_ok(gcmd, self._send({'type': 'PROJECTOR_OFF'}))

    def cmd_SET_IMAGE_H_OFFSET(self, gcmd):
        self.h_offset = gcmd.get_float('OFFSET')
        gcmd.respond_info("Image h_offset set to %.3f px" % self.h_offset)

    def cmd_DISPLAY_IMAGE(self, gcmd):
        path = gcmd.get('PATH')
        rotation = gcmd.get_int('ROTATION', 0, minval=0, maxval=270)
        if rotation not in (0, 90, 180, 270):
            raise gcmd.error(
                "image_display: ROTATION must be one of 0, 90, 180, 270 (got %d)"
                % rotation)
        self._require_ok(gcmd, self._send(
            {'type': 'DISPLAY_IMAGE', 'path': path, 'rotation': rotation,
             'h_offset': self.h_offset}
        ))

    def cmd_CLEAR_DISPLAY(self, gcmd):
        self._require_ok(gcmd, self._send({'type': 'CLEAR'}))

    def cmd_LOAD_VIDEO(self, gcmd):
        self._require_ok(gcmd, self._send({
            'type': 'LOAD_VIDEO',
            'name': gcmd.get('NAME'),
            'path': gcmd.get('PATH'),
        }))

    def cmd_SHOW_VIDEO_FRAME(self, gcmd):
        resp = self._send({
            'type': 'SHOW_VIDEO_FRAME',
            'name': gcmd.get('NAME'),
            'frame': gcmd.get_int('FRAME', minval=0),
            'h_offset': self.h_offset,
        })
        if resp.get('status') != 'success':
            msg = resp.get('message', 'unknown error')
            if 'not loaded' in msg:
                raise gcmd.error(
                    "image_display: %s. Server may have restarted and lost loaded "
                    "videos; re-run SLA_LOAD_GCODE_VIDEOS to recover" % msg)
            raise gcmd.error("image_display: %s" % msg)

    def cmd_UNLOAD_VIDEO(self, gcmd):
        self._require_ok(gcmd, self._send({
            'type': 'UNLOAD_VIDEO',
            'name': gcmd.get('NAME'),
        }))

    def cmd_UNLOAD_ALL_VIDEOS(self, gcmd):
        self._require_ok(gcmd, self._send({'type': 'UNLOAD_ALL'}))

    def cmd_LIST_VIDEOS(self, gcmd):
        resp = self._send({'type': 'LIST_VIDEOS'})
        if resp.get('status') != 'success':
            raise gcmd.error(
                "image_display: %s" % resp.get('message', 'error'))
        videos = resp.get('videos', {})
        if videos:
            lines = ['%s: %s' % (name, info) for name, info in videos.items()]
            gcmd.respond_info('\n'.join(lines))
        else:
            gcmd.respond_info('No videos loaded')

    def cmd_LOAD_VIDEOS_FROM_GCODE(self, gcmd):
        resp = self._send({
            'type': 'LOAD_VIDEOS_FROM_GCODE',
            'gcode_path': gcmd.get('GCODE_PATH'),
        }, timeout=self._load_timeout)
        self._require_ok(gcmd, resp)
        gcmd.respond_info("Loaded %d video(s)" % len(resp.get('loaded', [])))

    def cmd_PROJECTOR_STANDBY(self, gcmd):
        self._cancel_standby()
        reactor = self.printer.get_reactor()
        waketime = reactor.monotonic() + _STANDBY_DELAY
        self._standby_timer = reactor.register_timer(
            self._standby_callback, waketime)
        self._standby_waketime = waketime
        gcmd.respond_info("Projector standby armed: off in %.0fs" % _STANDBY_DELAY)

    def _cancel_standby(self):
        if self._standby_timer is not None:
            reactor = self.printer.get_reactor()
            reactor.unregister_timer(self._standby_timer)
            self._standby_timer = None
            self._standby_waketime = 0.0

    def _standby_callback(self, eventtime):
        self._standby_timer = None
        self._standby_waketime = 0.0
        self._cmd_queue.put(({'type': 'PROJECTOR_OFF'}, queue.Queue()))
        return self.printer.get_reactor().NEVER

    # -------------------------------------------------------------------------
    # Background status polling: separate socket, never touches cmd queue
    # -------------------------------------------------------------------------

    def _status_poll_once(self) -> None:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3.0)
            s.connect((self.host, self.port))
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.sendall(json.dumps({'type': 'STATUS'}).encode() + b'\n')
            buf = b''
            while b'\n' not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            s.close()
            response = json.loads(buf.split(b'\n')[0])
            if response.get('status') == 'success':
                self._status_cache = response
                self._status_cache_time = time.monotonic()
        except Exception as e:
            logger.debug("image_display: status poll failed: %s", e)

    def _status_poll_loop(self) -> None:
        while not self._stop_poll.wait(_STATUS_POLL_INTERVAL):
            self._status_poll_once()

    def get_status(self, eventtime) -> dict:
        cache = self._status_cache
        window = cache.get('window') or {}
        displays = cache.get('displays') or []
        status_stale = (
            self._status_cache_time == 0.0
            or (time.monotonic() - self._status_cache_time) > _STATUS_STALE_THRESHOLD
        )
        result = {
            'projector_on': bool(cache.get('projector_on', False)),
            'projector_available': bool(cache.get('projector_available', False)),
            'display_ready': bool(cache.get('display_ready', False)),
            'has_content': bool(cache.get('has_content', False)),
            'idle_seconds': cache.get('idle_seconds'),
            'last_activity_unix_time': cache.get('last_activity_unix_time'),
            'display_count': len(displays),
            'displays': list(displays),
            'window_display_name': window.get('display_name', ''),
            'window_width': window.get('width'),
            'window_height': window.get('height'),
            'standby_armed': self._standby_timer is not None,
            'standby_in_seconds': (
                max(0.0, self._standby_waketime - eventtime)
                if self._standby_timer is not None else None
            ),
            'status_stale': status_stale,
        }
        return result

    def _on_disconnect(self):
        self._cancel_standby()
        self._stopping.set()
        self._stop_poll.set()
        self._cmd_queue.put(None)   # signal worker to exit
        self._close()
        self._worker.join(_THREAD_JOIN_TIMEOUT)
        if self._worker.is_alive():
            logger.warning(
                "image_display: worker thread did not exit within %.1fs",
                _THREAD_JOIN_TIMEOUT)
        self._poll_thread.join(_THREAD_JOIN_TIMEOUT)
        if self._poll_thread.is_alive():
            logger.warning(
                "image_display: status poll thread did not exit within %.1fs",
                _THREAD_JOIN_TIMEOUT)


def load_config(config):
    return ImageDisplay(config)
