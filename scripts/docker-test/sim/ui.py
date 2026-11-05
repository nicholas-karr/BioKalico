"""Drives the built Mainsail with headless Chromium, over the DevTools protocol.

Used by e2e.py --ui. Every action here is a real click in the real page; the
backend is then asked whether it happened. Screenshots go to --shots so a
person can look at them.

Needs /usr/bin/chromium and the websocket-client Python package on the host.
"""

import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request

import websocket
from harness import PLA, SILICONE, Blocked, check, reset, wait_for

DEBUG_PORT = 9333


class Browser:
    def __init__(self, shots):
        self.shots = shots
        os.makedirs(shots, exist_ok=True)
        self.profile = tempfile.mkdtemp(prefix="sim-chromium-")
        self.proc = subprocess.Popen(
            [
                "chromium",
                "--headless=new",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--remote-allow-origins=*",
                "--remote-debugging-port=%d" % DEBUG_PORT,
                "--window-size=1500,1900",
                "--user-data-dir=" + self.profile,
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.ws = websocket.create_connection(self._page_url(), timeout=60)
        self._id = 0
        self.send("Page.enable")

    def _page_url(self):
        for _ in range(60):
            try:
                pages = json.load(
                    urllib.request.urlopen(
                        "http://127.0.0.1:%d/json/list" % DEBUG_PORT, timeout=2
                    )
                )
                for page in pages:
                    if page.get("type") == "page":
                        return page["webSocketDebuggerUrl"]
            except OSError:
                pass
            time.sleep(0.5)
        raise RuntimeError("Chromium did not start")

    def send(self, method, **params):
        self._id += 1
        self.ws.send(
            json.dumps({"id": self._id, "method": method, "params": params})
        )
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == self._id:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message.get("result", {})

    def js(self, expression):
        result = self.send(
            "Runtime.evaluate",
            expression=expression,
            returnByValue=True,
            awaitPromise=True,
        )
        return result.get("result", {}).get("value")

    def goto(self, url):
        self.send("Page.navigate", url=url)

    def wait_js(self, expression, timeout=30):
        return wait_for(lambda: bool(self.js(expression)), timeout, 0.4)

    def click(self, selector, text=None, index=0):
        """Click the index-th element matching selector whose text contains text."""
        return bool(
            self.js(
                """(() => {
            const els = [...document.querySelectorAll(%s)].filter(e => %s);
            if (els.length <= %d) return false;
            els[%d].scrollIntoView({block: 'center'});
            els[%d].click();
            return true;
        })()"""
                % (
                    json.dumps(selector),
                    "true"
                    if text is None
                    else "e.textContent.includes(%s)" % json.dumps(text),
                    index,
                    index,
                    index,
                )
            )
        )

    def text(self, selector):
        return (
            self.js(
                "(document.querySelector(%s) || {}).innerText || ''"
                % json.dumps(selector)
            )
            or ""
        )

    def shot(self, name):
        data = self.send(
            "Page.captureScreenshot", format="png", captureBeyondViewport=True
        )["data"]
        with open(os.path.join(self.shots, name + ".png"), "wb") as f:
            f.write(base64.b64decode(data))

    def close(self):
        try:
            self.ws.close()
        finally:
            self.proc.terminate()
            shutil.rmtree(self.profile, ignore_errors=True)


def open_dashboard(browser, url):
    browser.goto(url)
    return browser.wait_js(
        "!!document.querySelector('.toolheads-panel .v-expansion-panel')", 60
    )


def expand_all(browser):
    browser.js("""document.querySelectorAll('.toolheads-panel .v-expansion-panel:not(.v-expansion-panel--active) '
        + '.v-expansion-panel-header').forEach(h => h.click())""")
    time.sleep(0.8)


def dialog_visible(browser):
    return bool(
        browser.js("""(() => {
        const d = document.querySelector('.toolhead-mount-dialog');
        return !!d && d.getBoundingClientRect().width > 0 && getComputedStyle(d).visibility !== 'hidden';
    })()""")
    )


def run(api, args):
    print("\n-- the Mainsail UI")
    url = "http://127.0.0.1:%d/" % args.web_port
    reset(api)
    api.gcode("G28")
    browser = Browser(args.shots)
    try:
        check(
            open_dashboard(browser, url),
            "the Toolheads card appears on the dashboard",
        )
        card = browser.text(".toolheads-panel")
        check(
            "Toolheads" in card
            and "Toolhead 1 (nhk)" in card
            and "Syringe" in card,
            "it lists the toolheads",
            card[:200],
        )
        check(
            "Dock 1" in card and "Empty" in card,
            "it shows the docks and their occupants",
            card[:300],
        )
        browser.shot("01-toolheads-collapsed")
        expand_all(browser)
        browser.shot("02-toolheads-expanded")

        materials = browser.text(".materials-panel")
        check(
            "SLA" in materials
            and "Toolhead 1 (nhk)" not in materials
            and "Syringe" not in materials,
            "the Materials card keeps the SLA vats and no longer lists toolhead groups",
            materials[:200],
        )
        browser.shot("03-materials-card")

        print("-- reordering docks with the arrows")
        check(
            browser.click(
                ".toolheads-panel button[title*='next dock']", index=0
            ),
            "the move-down arrow is there",
        )
        check(
            wait_for(lambda: api.docks() == [None, "syringe", None]),
            "moving a dock down reorders the heads",
            api.docks(),
        )
        check(
            browser.click(
                ".toolheads-panel button[title*='previous dock']", index=1
            ),
            "and the move-up arrow",
        )
        check(
            wait_for(lambda: api.docks() == ["syringe", None, None]),
            "moving it back up",
            api.docks(),
        )

        print("-- changing a state from the card")
        browser.js(
            "document.querySelectorAll('.toolheads-panel .v-expansion-panel')[2].scrollIntoView()"
        )
        check(
            browser.click(
                ".toolheads-panel .v-expansion-panel:nth-child(3) .v-select .v-select__slot"
            ),
            "the state select of the third head opens",
        )
        time.sleep(0.5)
        check(
            browser.click(
                ".v-menu__content.menuable__content__active .v-list-item",
                "Docked",
            ),
            "Docked can be picked",
        )
        check(
            wait_for(lambda: api.state("solenoid")[0] == "docked"),
            "the head is recorded as docked",
            api.state("solenoid"),
        )
        browser.shot("04-solenoid-docked")

        print("-- mounting a docked head from the card")
        api.gcode("SIM_CLEAR_LOG")
        check(
            browser.click(".toolheads-panel .v-btn", "Mount"),
            "the Mount button is offered for a docked head",
        )
        check(
            wait_for(lambda: api.state("syringe")[0] == "mounted"),
            "the printer exchanges the heads",
            api.simlog(),
        )
        # the solenoid took dock 2 above, so the outgoing head goes to the free dock 3
        check(
            api.simlog() == "dropoff nhk dock=3;pickup syringe dock=1;",
            "in the right order",
            api.simlog(),
        )
        reset(api)

        print("-- the prompt for a head that must be mounted by hand")
        job = Blocked(api, "SELECT_MATERIAL MATERIAL=" + SILICONE)
        check(
            wait_for(lambda: dialog_visible(browser), 20),
            "the mount dialog opens by itself",
        )
        dialog = browser.text(".toolhead-mount-dialog")
        check(
            "Microfluidic solenoid" in dialog
            and "Toolhead 1 (nhk)" in dialog
            and "will not move" in dialog,
            "it says which head to mount and which to take off, and that nothing moves",
            dialog,
        )
        browser.shot("05-mount-dialog")
        check(
            browser.click(
                ".toolhead-mount-dialog .v-radio label", "Docked in dock 3"
            ),
            "the removed head can be put in a dock",
        )
        check(
            browser.click(".toolhead-mount-dialog .v-btn", "I have mounted it"),
            "and the mount is confirmed",
        )
        job.join(30)
        check(
            job.result == (True, ""), "the waiting command finishes", job.result
        )
        check(
            api.state("solenoid")[0] == "mounted"
            and api.state("nhk") == ("docked", 3),
            "the head is mounted and the old one docked where it was put",
        )
        check(
            wait_for(lambda: not dialog_visible(browser), 15),
            "the dialog goes away",
        )

        print("-- cancelling the prompt")
        job = Blocked(api, "SELECT_MATERIAL MATERIAL=" + PLA)
        check(
            wait_for(lambda: dialog_visible(browser), 20),
            "the dialog opens again",
        )
        check(
            browser.click(".toolhead-mount-dialog .v-btn", "Cancel"),
            "Cancel is clicked",
        )
        job.join(30)
        ok, message = job.result or (True, "")
        check(
            not ok and "cancelled" in message,
            "the waiting command fails",
            job.result,
        )
        check(
            wait_for(lambda: not dialog_visible(browser), 15),
            "and the dialog goes away",
        )
        browser.shot("06-after-cancel")
    finally:
        browser.close()
        reset(api)
