#!/usr/bin/env python3
# Minimal Moonraker API client for tools that run on the printer host.
#
# [simple_password_auth] can require a login for every request, including
# ones from this host, and a login needs a browser. Moonraker accepts its API
# key in an X-Api-Key header even when logins are forced, so host-side tools
# (printer-services.sh, monitoring.sh, reset-ftdi-hub.sh, build_and_flash.py)
# call Moonraker through this file, which reads that key from Moonraker's own
# database.
#
# The key is only ever sent to a loopback address. Without a readable key the
# request is sent as it is, which still works when local_bypass is on.
#
# Usage:
#   moonraker_api.py [--post] [--timeout SECONDS] URL_OR_PATH
#
# A path starting with "/" is appended to $MOONRAKER_URL (default
# http://127.0.0.1:7125). The response body goes to stdout. Exit status is 0
# for a 2xx or 3xx answer, 22 for an HTTP error (like curl -f, with nothing
# printed), and 7 when the server cannot be reached.
import argparse
import ipaddress
import os
import pathlib
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

API_USER = "_API_KEY_USER_"
DEFAULT_URL = "http://127.0.0.1:7125"
EXIT_HTTP_ERROR = 22
EXIT_UNREACHABLE = 7


def read_api_key(printer_data=None):
    """Moonraker's API key, or None if its database cannot be read."""
    printer_data = (
        printer_data
        or os.environ.get("PRINTER_DATA")
        or os.path.expanduser("~/printer_data")
    )
    db_path = pathlib.Path(printer_data).resolve() / "database"
    db_path = db_path / "moonraker-sql.db"
    try:
        con = sqlite3.connect(
            db_path.as_uri() + "?mode=ro", uri=True, timeout=2
        )
        try:
            row = con.execute(
                "SELECT password FROM authorized_users WHERE username = ?",
                (API_USER,),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return row[0] if row and row[0] else None


def is_loopback(url):
    host = urllib.parse.urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def build_request(url, method="GET", printer_data=None):
    request = urllib.request.Request(
        url, method=method, data=b"" if method == "POST" else None
    )
    if is_loopback(url):
        key = read_api_key(printer_data)
        if key:
            request.add_header("X-Api-Key", key)
    return request


def call(url, method="GET", timeout=10.0, printer_data=None):
    """Return the response body as bytes. Raises urllib.error.URLError, and
    its HTTPError subclass for an HTTP error status."""
    request = build_request(url, method, printer_data)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Call Moonraker with the host's API key."
    )
    ap.add_argument("target", help="full URL, or a path such as /server/info")
    ap.add_argument("--post", action="store_true", help="send a POST")
    ap.add_argument("--timeout", type=float, default=10.0)
    args = ap.parse_args(argv)

    url = args.target
    if url.startswith("/"):
        url = os.environ.get("MOONRAKER_URL", DEFAULT_URL).rstrip("/") + url
    try:
        body = call(url, "POST" if args.post else "GET", args.timeout)
    except urllib.error.HTTPError:
        return EXIT_HTTP_ERROR
    except (urllib.error.URLError, OSError):
        return EXIT_UNREACHABLE
    sys.stdout.write(body.decode(errors="replace"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
