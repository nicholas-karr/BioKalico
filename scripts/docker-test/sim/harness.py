"""Helpers shared by e2e.py and ui.py: a small Moonraker client and check reporting."""

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request

CONTAINER = os.environ.get("CONTAINER_NAME", "biokalico-install-test")
# run.sh exports DOCKER, which is "sudo docker" when the user cannot reach the daemon.
DOCKER = os.environ.get("DOCKER", "docker").split()
PLA = "FFF_BASIC_PLA_BIOSLICERTRIDENT"
PCL = "FFF_PCL_BIOINK_BIOSLICERTRIDENT"
SILICONE = "FFF_SILICONE_BIOSLICERTRIDENT"
USB_INK = "FFF_USB_INK_BIOSLICERTRIDENT"
VAT_1 = "SLA_STANDARD_PHOTOPOLYMER_RESIN_BIOSLICERTRIDENT"
VAT_2 = "SLA_GELMA_10_BIOSLICERTRIDENT"

failures = []


def check(ok, what, detail=""):
    print(
        ("PASS: " if ok else "FAIL: ")
        + what
        + (("  [" + str(detail) + "]") if detail and not ok else "")
    )
    if not ok:
        failures.append(what)
    return ok


class Api:
    def __init__(self, port):
        self.base = "http://127.0.0.1:%d" % port

    def request(self, method, path, body=None, timeout=90):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.load(e)
            except ValueError:
                return e.code, {}

    def gcode(self, script, timeout=90):
        """Returns (ok, error message)."""
        status, body = self.request(
            "POST", "/printer/gcode/script", {"script": script}, timeout
        )
        return status == 200, body.get("error", {}).get("message", "")

    def call(self, endpoint, **params):
        status, body = self.request(
            "POST", "/printer/toolheads/" + endpoint, params
        )
        return status == 200, body.get("error", {}).get("message", "")

    def query(self, *objects):
        path = "/printer/objects/query?" + "&".join(
            o.replace(" ", "%20") for o in objects
        )
        return self.request("GET", path)[1]["result"]["status"]

    def toolheads(self):
        return self.query("toolheads")["toolheads"]

    def state(self, name):
        head = self.toolheads()["toolheads"][name]
        return head["state"], head["dock"]

    def docks(self):
        return self.toolheads()["docks"]

    def simlog(self):
        return self.query("gcode_macro _SIMLOG")["gcode_macro _SIMLOG"][
            "events"
        ]

    def wait_ready(self, timeout=90):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if (
                    self.request("GET", "/printer/info", timeout=5)[1][
                        "result"
                    ]["state"]
                    == "ready"
                ):
                    return True
            except (OSError, KeyError, ValueError):
                pass
            time.sleep(2)
        return False


def wait_for(predicate, timeout=20, interval=0.3):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class Blocked(threading.Thread):
    """Runs a g-code command that is expected to wait for the user."""

    def __init__(self, api, script):
        super().__init__(daemon=True)
        self.api, self.script, self.result = api, script, None
        self.start()

    def run(self):
        self.result = self.api.gcode(self.script, timeout=120)


class SimError(Exception):
    pass


def in_container(*cmd):
    result = subprocess.run(
        [*DOCKER, "exec", "-u", "biokalico", CONTAINER, *cmd],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SimError(
            "in the container, %s failed: %s"
            % (" ".join(cmd), result.stderr.strip() or result.stdout.strip())
        )
    return result


def _must(step, result):
    ok, message = result
    if not ok:
        raise SimError("reset failed at %s: %s" % (step, message))


def reset(api):
    """A known layout: nhk on the carriage, syringe in dock 1, others aside."""
    _must("SIM_CLEAR_LOG", api.gcode("SIM_CLEAR_LOG"))
    for name in ("nhk", "syringe", "solenoid"):
        _must(
            "unmount " + name,
            api.call("set_state", toolhead=name, state="unmounted"),
        )
    _must(
        "dock syringe",
        api.call("set_state", toolhead="syringe", state="docked", dock=1),
    )
    _must("mount nhk", api.call("set_state", toolhead="nhk", state="mounted"))
    _must(
        "disconnect solenoid",
        api.call("set_state", toolhead="solenoid", state="disconnected"),
    )
    _must("TOOLHEADS_SYNC", api.gcode("TOOLHEADS_SYNC"))
