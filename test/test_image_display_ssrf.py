"""Tests for image-display's protections against fetching private URLs.

image-display.py imports pyglet, which needs a display, so this module is
skipped when that fails. Run under `xvfb-run` to include it.
"""

import http.server
import importlib.util
import os
import socket
import sys
import threading

import pytest

_MODULE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "sla", "image-display.py"
)


def _load_image_display_module():
    sla_dir = os.path.dirname(_MODULE_PATH)
    if sla_dir not in sys.path:
        sys.path.insert(0, sla_dir)
    spec = importlib.util.spec_from_file_location(
        "_image_display_under_test", _MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    image_display = _load_image_display_module()
    _import_error = None
except Exception as exc:  # pragma: no cover - environment-dependent
    image_display = None
    _import_error = exc

pytestmark = pytest.mark.skipif(
    image_display is None,
    reason="scripts/sla/image-display.py needs a display to import "
    f"(pyglet): {_import_error!r}",
)


def _make_server(tmp_path, **overrides):
    config = {
        "host": "127.0.0.1",
        "port": 0,
        "auth_token": "",
        "allow_remote_image_urls": True,
        "allow_private_image_urls": False,
        "max_download_bytes": 1024 * 1024,
        "projector_control": False,
        "video_cache_dir": str(tmp_path / "cache"),
    }
    config.update(overrides)
    return image_display.ImageDisplayServer(config)


def test_validate_image_url_rejects_private_address(tmp_path, monkeypatch):
    server = _make_server(tmp_path)
    monkeypatch.setattr(
        image_display.socket,
        "getaddrinfo",
        lambda host, port, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", port))
        ],
    )
    with pytest.raises(ValueError, match="Private"):
        server._validate_image_url("http://evil.example/x.png")


def test_validate_image_url_allows_private_when_opted_in(tmp_path, monkeypatch):
    server = _make_server(tmp_path, allow_private_image_urls=True)
    monkeypatch.setattr(
        image_display.socket,
        "getaddrinfo",
        lambda host, port, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", port))
        ],
    )
    assert (
        server._validate_image_url("http://internal.example/x.png")
        == "127.0.0.1"
    )


def test_download_pins_connection_to_validated_address_not_hostname(
    tmp_path, monkeypatch
):
    # DNS rebinding: the download must connect to the address that was
    # validated, not resolve the hostname again. The mocked getaddrinfo
    # returns an address different from the test server's, and the fake
    # create_connection checks that it is asked for exactly that address.
    payload = b"fake-image-bytes-for-ssrf-pin-test"

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    real_host, real_port = httpd.server_address
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        server = _make_server(tmp_path)
        validated_ip = "93.184.216.34"  # a real global address, distinct from the local test server
        real_getaddrinfo = socket.getaddrinfo

        def fake_getaddrinfo(host, port, *args, **kwargs):
            if host == "rebind.invalid":
                return [
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        0,
                        "",
                        (validated_ip, port),
                    )
                ]
            # The redirect below reconnects to the real local test server by
            # numeric IP, which still needs real resolution to go through.
            return real_getaddrinfo(host, port, *args, **kwargs)

        monkeypatch.setattr(
            image_display.socket, "getaddrinfo", fake_getaddrinfo
        )

        requested_addresses = []
        real_create_connection = socket.create_connection

        def fake_create_connection(address, *args, **kwargs):
            requested_addresses.append(address[0])
            # Connect to the local test server so the rest of the download
            # still runs.
            return real_create_connection(
                (real_host, real_port), *args, **kwargs
            )

        monkeypatch.setattr(
            image_display.socket, "create_connection", fake_create_connection
        )

        local_path, is_temp = server._resolve_image_path(
            f"http://rebind.invalid:{real_port}/image.png"
        )
        try:
            assert requested_addresses == [validated_ip]
            with open(local_path, "rb") as f:
                assert f.read() == payload
        finally:
            if is_temp:
                os.remove(local_path)
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
