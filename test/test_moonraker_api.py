import http.server
import os
import pathlib
import sqlite3
import subprocess
import sys
import threading

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "moonraker_api.py"
KEY = "0123456789abcdef0123456789abcdef"


def _make_db(printer_data, key):
    db_dir = printer_data / "database"
    db_dir.mkdir(parents=True)
    con = sqlite3.connect(db_dir / "moonraker-sql.db")
    con.execute(
        "CREATE TABLE authorized_users (username TEXT PRIMARY KEY, "
        "password TEXT NOT NULL)"
    )
    con.execute(
        "INSERT INTO authorized_users VALUES ('_API_KEY_USER_', ?)", (key,)
    )
    con.execute("INSERT INTO authorized_users VALUES ('biokalico', 'x')")
    con.commit()
    con.close()


class Handler(http.server.BaseHTTPRequestHandler):
    seen = []

    def _answer(self):
        Handler.seen.append((self.command, self.headers.get("X-Api-Key")))
        if self.headers.get("X-Api-Key") == KEY:
            body = b'{"result": "ok"}'
            self.send_response(200)
        else:
            body = b'{"error": "Unauthorized"}'
            self.send_response(401)
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _answer

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    Handler.seen = []
    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()


def _run(server, printer_data, *args):
    env = dict(os.environ, PRINTER_DATA=str(printer_data), MOONRAKER_URL=server)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
    )


def test_sends_the_key_from_the_database(server, tmp_path):
    _make_db(tmp_path, KEY)
    proc = _run(server, tmp_path, "/server/info")
    assert proc.returncode == 0
    assert '"ok"' in proc.stdout
    assert Handler.seen == [("GET", KEY)]


def test_post(server, tmp_path):
    _make_db(tmp_path, KEY)
    proc = _run(server, tmp_path, "--post", "/printer/firmware_restart")
    assert proc.returncode == 0
    assert Handler.seen == [("POST", KEY)]


def test_http_error_is_exit_22_with_no_output(server, tmp_path):
    _make_db(tmp_path, "wrong-key")
    proc = _run(server, tmp_path, "/server/info")
    assert proc.returncode == 22
    assert proc.stdout == ""


def test_missing_database_still_makes_the_request(server, tmp_path):
    proc = _run(server, tmp_path, "/server/info")
    assert proc.returncode == 22
    assert Handler.seen == [("GET", None)]


def test_unreachable_server_is_exit_7(tmp_path):
    proc = _run("http://127.0.0.1:1", tmp_path, "/server/info")
    assert proc.returncode == 7


def test_full_url_is_used_as_given(server, tmp_path):
    _make_db(tmp_path, KEY)
    proc = _run("http://127.0.0.1:1", tmp_path, server + "/x")
    assert proc.returncode == 0


def test_the_key_is_only_sent_to_loopback_hosts(tmp_path, monkeypatch):
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        import moonraker_api
    finally:
        sys.path.pop(0)
    _make_db(tmp_path, KEY)
    for url in (
        "http://127.0.0.1:7125/x",
        "http://localhost:7125/x",
        "http://[::1]:7125/x",
    ):
        req = moonraker_api.build_request(url, printer_data=str(tmp_path))
        assert req.get_header("X-api-key") == KEY
    for url in (
        "http://192.168.1.5:7125/x",
        "https://example.com/x",
        "http://printer.local/x",
    ):
        req = moonraker_api.build_request(url, printer_data=str(tmp_path))
        assert req.get_header("X-api-key") is None
