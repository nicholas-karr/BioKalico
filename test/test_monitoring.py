import http.server
import json
import os
import pathlib
import subprocess
import threading

import pytest
from test_moonraker_api import KEY, _make_db

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "monitoring.sh"


class Handler(http.server.BaseHTTPRequestHandler):
    payload = {}

    def do_GET(self):
        if self.headers.get("X-Api-Key") != KEY:
            self.send_response(401)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(Handler.payload).encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()


def _check_json_error(tmp_path, spec):
    """Run monitoring.sh's check_json_error alone; return the alerts."""
    text = SCRIPT.read_text()
    start = text.index("check_json_error() {")
    end = text.index("\n}\n", start) + 3
    harness = (
        'SCRIPT_DIR="%s"\n'
        'alert() { echo "ALERT[$1] $2"; }\n'
        "%s\n"
        'check_json_error "$1"\n' % (REPO, text[start:end])
    )
    return subprocess.run(
        ["bash", "-c", harness, "bash", spec],
        capture_output=True,
        text=True,
        env=dict(os.environ, PRINTER_DATA=str(tmp_path)),
    ).stdout


def test_reports_the_field_when_it_holds_an_error(server, tmp_path):
    _make_db(tmp_path, KEY)
    Handler.payload = {"result": {"link_error": "adapter unplugged"}}
    out = _check_json_error(tmp_path, server + "/q|result.link_error")
    assert "adapter unplugged" in out


def test_quiet_when_the_field_is_null(server, tmp_path):
    _make_db(tmp_path, KEY)
    Handler.payload = {"result": {"link_error": None}}
    assert _check_json_error(tmp_path, server + "/q|result.link_error") == ""


def test_alerts_when_the_endpoint_refuses_the_request(server, tmp_path):
    # No readable key: the request is rejected, which must not read as healthy.
    out = _check_json_error(tmp_path, server + "/q|result.link_error")
    assert "Could not query" in out
    assert server + "/q" in out
    assert "check result.link_error" in out


def test_url_and_path_do_not_depend_on_a_global_named_spec(server, tmp_path):
    _make_db(tmp_path, KEY)
    Handler.payload = {"result": {"link_error": "boom"}}
    out = _check_json_error(tmp_path, server + "/q|result.link_error")
    assert "link_error on" in out
