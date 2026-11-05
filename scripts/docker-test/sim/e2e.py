#!/usr/bin/env python3
"""End-to-end test of the toolheads feature against the simulated printer.

Run from the host once scripts/docker-test/sim/setup.sh has finished inside the
test container (run.sh --sim does both). Everything goes through the real
stack: Klipper in file-output mode, Moonraker's HTTP API and, with --ui, the
built Mainsail driven by headless Chromium.

Usage: e2e.py [--ui] [--shots DIR] [--http-port 17125] [--web-port 18080]
"""

import argparse
import os
import sys
import time

from harness import (
    PCL,
    PLA,
    SILICONE,
    USB_INK,
    VAT_1,
    VAT_2,
    Api,
    Blocked,
    check,
    failures,
    in_container,
    reset,
    wait_for,
)


def scenario_startup(api):
    print("\n-- startup state")
    st = api.toolheads()
    check(st["dock_count"] == 3, "the sim declares three docks")
    check(st["mounted"] == "nhk", "nhk starts mounted")
    check(
        st["toolheads"]["usb"]["state"] == "disconnected",
        "a head whose board is absent starts disconnected",
    )
    check(
        st["toolheads"]["syringe"]["state"] == "docked",
        "the syringe starts docked",
    )
    check(
        st["awaiting_mount"] is None and not st["busy"],
        "nothing is awaited at startup",
    )
    sel = st["selectors"]
    check("sla" in sel, "the SLA group is a material selector")


def scenario_manual_state(api):
    print("\n-- manual state edits")
    reset(api)
    ok, _ = api.call("set_state", toolhead="solenoid", state="docked", dock=2)
    check(
        ok and api.state("solenoid") == ("docked", 2),
        "a disconnected head moves to a dock",
    )
    ok, _ = api.call("reorder_docks", order=["solenoid", "syringe", None])
    check(
        ok and api.docks() == ["solenoid", "syringe", None],
        "docked heads are reordered",
    )
    ok, msg = api.call("set_state", toolhead="nhk", state="docked", dock=2)
    check(
        not ok and "occupied" in msg,
        "docking into an occupied dock is refused",
        msg,
    )
    check(
        api.state("nhk") == ("mounted", None), "a refused edit changes nothing"
    )
    ok, msg = api.call("reorder_docks", order=["solenoid", None, None])
    check(not ok, "an order that drops a docked head is refused", msg)
    ok, _ = api.call("set_state", toolhead="syringe", state="mounted")
    check(
        ok
        and api.state("nhk") == ("unmounted", None)
        and api.state("syringe")[0] == "mounted",
        "mounting a head sets the previous one aside",
    )
    reset(api)


def scenario_auto_swap(api):
    print("\n-- automatic exchange between docked heads")
    reset(api)
    check(api.gcode("G28")[0], "the sim homes")
    api.gcode("SIM_CLEAR_LOG")
    ok, msg = api.gcode("SELECT_MATERIAL MATERIAL=" + PCL)
    check(ok, "selecting a docked head's material succeeds", msg)
    check(
        api.simlog() == "dropoff nhk dock=2;pickup syringe dock=1;",
        "the old head is dropped off, then the new one picked up, each told its dock",
        api.simlog(),
    )
    check(
        api.state("syringe")[0] == "mounted"
        and api.state("nhk") == ("docked", 2),
        "the registry follows the exchange",
    )
    api.gcode("SIM_CLEAR_LOG")
    check(
        api.gcode("SELECT_MATERIAL MATERIAL=" + PCL)[0] and api.simlog() == "",
        "asking again for the material already in place moves nothing",
    )
    ok, _ = api.gcode("SELECT_MATERIAL MATERIAL=" + PLA)
    check(
        ok and api.simlog() == "dropoff syringe dock=1;pickup nhk dock=2;",
        "and back again",
        api.simlog(),
    )
    ok, msg = api.gcode("SELECT_MATERIAL MATERIAL=FFF_NOT_A_MATERIAL")
    check(
        not ok and "Unknown material" in msg,
        "an unknown material is an error",
        msg,
    )


def scenario_manual_mount(api):
    print("\n-- a head that must be mounted by hand")
    reset(api)
    api.gcode("SIM_CLEAR_LOG")
    before = api.query("toolhead")["toolhead"]["position"]
    job = Blocked(api, "SELECT_MATERIAL MATERIAL=" + SILICONE)
    seen = wait_for(lambda: api.toolheads()["awaiting_mount"] is not None)
    check(seen, "the request waits and publishes awaiting_mount")
    aw = api.toolheads()["awaiting_mount"] or {}
    check(
        aw.get("toolhead") == "solenoid"
        and aw.get("material") == "silicone"
        and aw.get("remove") == "nhk",
        "it names the head, the material and the head to remove",
        aw,
    )
    time.sleep(1.5)
    check(
        api.simlog() == ""
        and api.query("toolhead")["toolhead"]["position"] == before,
        "nothing moves while it waits",
    )
    ok, msg = api.call("set_state", toolhead="syringe", state="unmounted")
    check(
        not ok and "in progress" in msg, "edits are refused while it waits", msg
    )
    ok, msg = api.call("confirm_mount", removed_state="docked", removed_dock=1)
    check(not ok, "a bad answer is refused and it keeps waiting", msg)
    ok, _ = api.call("confirm_mount", removed_state="docked", removed_dock=3)
    job.join(30)
    check(
        ok and job.result == (True, ""),
        "confirming finishes the command",
        job.result,
    )
    check(
        api.state("solenoid")[0] == "mounted"
        and api.state("nhk") == ("docked", 3),
        "the new head is mounted and the old one is where the user said",
    )
    check(api.simlog() == "", "the user's mounting caused no motion")

    print("-- cancelling")
    job = Blocked(api, "SELECT_MATERIAL MATERIAL=" + PLA)
    wait_for(lambda: api.toolheads()["awaiting_mount"] is not None)
    api.call("cancel_mount")
    job.join(30)
    ok, msg = job.result or (True, "")
    check(
        not ok and "cancelled" in msg,
        "cancelling fails the waiting command",
        job.result,
    )
    check(
        api.state("solenoid")[0] == "mounted"
        and api.toolheads()["awaiting_mount"] is None,
        "and changes nothing",
    )
    reset(api)


def scenario_selector(api):
    print("\n-- SLA vats are switched by the selector macro")
    reset(api)
    api.gcode("G28")
    ok, msg = api.gcode("SELECT_MATERIAL MATERIAL=" + VAT_2)
    check(ok, "selecting a vat material rotates the vat table", msg)
    active = api.query("materials")["materials"]["slot_groups"]["sla"][
        "active_slot"
    ]
    check(active == 2, "and the vat that is in place is recorded", active)
    check(
        api.state("nhk") == ("mounted", None) and api.simlog() == "",
        "no toolhead is exchanged for it",
    )
    ok, msg = api.gcode("SELECT_MATERIAL MATERIAL=" + VAT_1)
    check(
        ok
        and api.query("materials")["materials"]["slot_groups"]["sla"][
            "active_slot"
        ]
        == 1,
        "and the other vat",
        msg,
    )


def scenario_preflight(api):
    print("\n-- checking a print before it starts")
    reset(api)
    ok, msg = api.gcode(
        "TOOLHEADS_PREFLIGHT MATERIALS=%s,%s,%s,%s"
        % (PLA, PCL, SILICONE, VAT_1)
    )
    check(ok, "a print whose materials can all be had passes", msg)
    ok, msg = api.gcode(
        "TOOLHEADS_PREFLIGHT MATERIALS=%s,FFF_NOPE_BIOSLICERTRIDENT" % (PLA,)
    )
    check(
        not ok and "Unknown material" in msg,
        "an unknown material is reported",
        msg,
    )
    api.gcode("SIM_CLEAR_LOG")
    ok, msg = api.gcode("PRINT_START MATERIALS=FFF_NOPE_BIOSLICERTRIDENT")
    check(
        not ok and "Unknown material" in msg,
        "PRINT_START stops before anything moves",
        msg,
    )


def scenario_hot_plug(api):
    print("\n-- a head on a board that is plugged in")
    reset(api)
    check(
        api.state("usb")[0] == "disconnected",
        "the head is disconnected while its board is absent",
    )
    ok, msg = api.call("set_state", toolhead="usb", state="docked", dock=2)
    check(not ok, "it cannot be placed by hand meanwhile", msg)
    job = Blocked(api, "SELECT_MATERIAL MATERIAL=" + USB_INK)
    wait_for(lambda: api.toolheads()["awaiting_mount"] is not None)
    ok, msg = api.call("confirm_mount")
    check(
        not ok
        and "cannot be placed" in msg
        and api.toolheads()["awaiting_mount"] is not None,
        "confirming a head whose board is unplugged is refused, and the request keeps waiting",
        msg,
    )
    api.call("cancel_mount")
    job.join(30)
    in_container(
        "python3",
        "/home/biokalico/klipper/scripts/docker-test/sim/usb_board.py",
        "plug",
    )
    check(
        wait_for(lambda: api.state("usb")[0] != "disconnected", 25),
        "plugging the board in reconnects the head",
        api.state("usb"),
    )
    check(
        api.state("usb") == ("unmounted", None),
        "and it comes back unmounted, ready to be placed",
        api.state("usb"),
    )
    ok, _ = api.call("set_state", toolhead="usb", state="docked", dock=2)
    check(ok and api.state("usb") == ("docked", 2), "then it is placed by hand")
    job = Blocked(api, "SELECT_MATERIAL MATERIAL=" + USB_INK)
    wait_for(lambda: api.toolheads()["awaiting_mount"] is not None)
    api.call("confirm_mount")
    job.join(30)
    check(
        job.result == (True, "") and api.state("usb")[0] == "mounted",
        "and mounted for a material",
    )
    reset(api)
    api.call("set_state", toolhead="usb", state="unmounted")


def scenario_restart(api):
    print("\n-- state survives a restart")
    reset(api)
    api.call("set_state", toolhead="solenoid", state="docked", dock=3)
    api.call("reorder_docks", order=["solenoid", "syringe", None])
    before = api.toolheads()
    in_container("sudo", "systemctl", "restart", "klipper")
    check(api.wait_ready(120), "klippy comes back")
    after = api.toolheads()
    same = all(
        before["toolheads"][n][k] == after["toolheads"][n][k]
        for n in before["toolheads"]
        for k in ("state", "dock")
    )
    check(
        same and before["docks"] == after["docks"],
        "every head is where it was",
        (before["docks"], after["docks"]),
    )
    reset(api)


def finish(api):
    """Leave the sim as setup.sh does: the pluggable board out, everything at its initial place."""
    reset(api)
    api.call("set_state", toolhead="usb", state="unmounted")
    in_container(
        "python3",
        "/home/biokalico/klipper/scripts/docker-test/sim/usb_board.py",
        "unplug",
    )
    in_container("sudo", "systemctl", "restart", "klipper")
    api.wait_ready(120)


API_SCENARIOS = [
    scenario_startup,
    scenario_manual_state,
    scenario_auto_swap,
    scenario_manual_mount,
    scenario_selector,
    scenario_preflight,
    scenario_hot_plug,
    scenario_restart,
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ui",
        action="store_true",
        help="also drive the Mainsail UI with headless Chromium",
    )
    parser.add_argument(
        "--shots", default="sim-shots", help="directory for UI screenshots"
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=int(os.environ.get("MOONRAKER_PORT", 17125)),
    )
    parser.add_argument(
        "--web-port", type=int, default=int(os.environ.get("HTTP_PORT", 18080))
    )
    args = parser.parse_args()

    api = Api(args.http_port)
    if not api.wait_ready(30):
        sys.exit(
            "the simulated printer is not ready; run scripts/docker-test/sim/setup.sh in the container first"
        )

    for scenario in API_SCENARIOS:
        scenario(api)

    if args.ui:
        import ui

        ui.run(api, args)

    finish(api)
    print()
    if failures:
        print("==> %d E2E CHECK(S) FAILED:" % len(failures))
        for what in failures:
            print("    - " + what)
        sys.exit(1)
    print("==> ALL E2E CHECKS PASSED")


if __name__ == "__main__":
    main()
