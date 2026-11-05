import pathlib

import jinja2
import pytest
from klippy_testing import (
    FakeWebhooks,
    FakeWebRequest,
    FakeWebRequestError,
    PrinterShim,
)

import klippy.extras.materials as materials
import klippy.extras.toolheads as toolheads


class CommandError(Exception):
    """Stands in for printer.command_error, the only error a webhooks
    handler may raise without shutting Klipper down."""


class FakeReactor:
    """Time only advances when the code under test pauses."""

    def __init__(self):
        self.now = 0.0
        self.on_pause = []  # callables run on successive pauses

    def monotonic(self):
        return self.now

    def pause(self, waketime):
        self.now = waketime
        if not self.on_pause:
            raise AssertionError("waited with nothing left to answer it")
        self.on_pause.pop(0)()

    def register_callback(self, cb, waketime=0.0):
        cb(self.now)


class FakeTemplate:
    def __init__(self, source, scripts):
        self.source = source
        self.scripts = scripts

    def create_template_context(self):
        return {}

    def run_gcode_from_command(self, context):
        # Klipper macros use single braces for expressions.
        env = jinja2.Environment(
            block_start_string="{%",
            block_end_string="%}",
            variable_start_string="{",
            variable_end_string="}",
        )
        text = env.from_string(self.source).render(**context)
        self.scripts.append(text.strip())


class FakeGcodeMacro:
    def __init__(self, scripts):
        self.scripts = scripts

    def load_template(self, config, option, default=None):
        return FakeTemplate(config.get(option), self.scripts)


class FakeTool:
    def __init__(self, name):
        self.name = name
        self.params = {}
        self.t_command_restore_axis = "XYZ"

    def set_parameter(self, name, value):
        self.params[name] = value


class FakeToolchanger:
    """Stands in for the vendored engine; records only physical exchanges."""

    def __init__(self):
        self.status = "uninitialized"
        self.active_tool = None
        self.moves = []
        self.fail = None  # exception to raise from select_tool
        self.swallow = False  # return without completing, like error_gcode

    def initialize(self, tool=None):
        self.status = "ready"
        self.active_tool = tool

    def set_active_tool(self, tool):
        self.active_tool = tool

    def select_tool(self, gcmd, tool, restore_axis):
        if self.fail is not None:
            raise self.fail
        if self.swallow:
            return
        if self.active_tool is not None:
            self.moves.append(
                "dropoff %s dock=%s"
                % (self.active_tool.name, self.active_tool.params.get("dock"))
            )
        if tool is not None:
            self.moves.append(
                "pickup %s dock=%s" % (tool.name, tool.params.get("dock"))
            )
        self.active_tool = tool


class FakeMcu:
    def __init__(self):
        self.non_critical_disconnected = False


class FakeKinematics:
    def __init__(self, toolhead):
        self.toolhead = toolhead
        self.cleared = []

    def clear_homing_state(self, axes):
        self.cleared.append(list(axes))
        self.toolhead.homed_axes = ""


class FakeGcodeMove:
    def __init__(self):
        self.offsets = []

    def cmd_SET_GCODE_OFFSET(self, gcmd):
        self.offsets.append(tuple(gcmd.get_float(a) for a in "XYZ"))


class FakeToolhead:
    def __init__(self):
        self.homed_axes = ""
        self.kinematics = FakeKinematics(self)
        self.position = [30.0, 40.0, 5.0, 0.0]

    def wait_moves(self):
        pass

    def get_position(self):
        return list(self.position)

    def get_kinematics(self):
        return self.kinematics

    def get_status(self, eventtime):
        return {
            "homed_axes": self.homed_axes,
            "axis_minimum": [0.0, -5.0, -100000.0, 0.0],
            "axis_maximum": [240.0, 250.0, 100000.0, 0.0],
        }


class FakeForceMove:
    def __init__(self, toolhead):
        self.toolhead = toolhead
        self.positions = []

    def cmd_SET_KINEMATIC_POSITION(self, gcmd):
        self.positions.append(tuple(gcmd.get_float(a) for a in ("X", "Y", "Z")))
        self.toolhead.homed_axes = "xyz"


class FakeSdcard:
    def __init__(self):
        self.active = False

    def is_active(self):
        return self.active


class Env:
    """A Toolheads instance wired to fakes, plus handles for assertions."""


# What each slot holds. Slot contents are only ever set at runtime, as
# Mainsail does, so a first start fills them through the same endpoint.
SLOT_MATERIALS = (
    ("pla_head", 1, "pla"),
    ("gel_head", 1, "gel"),
    ("syringe", 1, "bio"),
    ("usb_head", 1, "usbmat"),
    ("vat", 1, "resin_a"),
    ("vat", 2, "resin_b"),
)


def build(printer, tmp_path, overrides=(), mcu_disconnected=False):
    config = printer.lookup_object("configfile").read_main_config()
    materials_state = tmp_path / "materials.cfg"
    first_start = not materials_state.exists()
    config.getsection("materials").fileconfig.set(
        "materials", "state_file", str(materials_state)
    )
    config.getsection("toolheads").fileconfig.set(
        "toolheads", "state_file", str(tmp_path / "toolheads.cfg")
    )
    for section, option, value in overrides:
        config.getsection(section).fileconfig.set(section, option, value)

    env = Env()
    env.printer = printer
    env.reactor = FakeReactor()
    env.scripts = []
    env.infos = []
    env.handlers = {}
    env.tc = FakeToolchanger()
    env.mcu = FakeMcu()
    env.mcu.non_critical_disconnected = mcu_disconnected
    env.sdcard = FakeSdcard()
    env.webhooks = FakeWebhooks()
    env.toolhead = FakeToolhead()
    env.force_move = FakeForceMove(env.toolhead)
    env.gcode_move = FakeGcodeMove()

    printer.command_error = CommandError
    printer.get_reactor = lambda: env.reactor
    printer.is_shutdown = lambda: False
    printer.register_event_handler = lambda ev, cb: env.handlers.setdefault(
        ev, []
    ).append(cb)
    env.fire = lambda ev: [cb() for cb in env.handlers.get(ev, [])]
    env.events = []
    printer.send_event = lambda ev, *args: env.events.append((ev, *args))
    gcode = printer.lookup_object("gcode")
    gcode.respond_info = lambda msg: env.infos.append(msg)
    gcode.run_script = lambda script: printer.call(script)
    # The shim does not hand back the handler that register_command(None)
    # removes, which the G28 guard relies on.
    env.g28_calls = []
    gcode.register_command("G28", lambda gcmd: env.g28_calls.append(gcmd))
    register_command = gcode.register_command

    def register(cmd, func, *args, **kwargs):
        if func is None:
            return gcode.ready_gcode_handlers.pop(cmd.upper(), None)
        return register_command(cmd, func, *args, **kwargs)

    gcode.register_command = register

    printer.add_object("webhooks", env.webhooks)
    printer.add_object("toolhead", env.toolhead)
    printer.add_object("gcode_move", env.gcode_move)
    env.materials = materials.load_config(config.getsection("materials"))
    printer.add_object("materials", env.materials)
    if first_start:
        for group, slot, material in SLOT_MATERIALS:
            env.webhooks.endpoints["materials/set_slot"](
                FakeWebRequest(
                    {"group": group, "slot": slot, "material": material}
                )
            )
    printer.add_object("tool a", FakeTool("a"))
    printer.add_object("tool b", FakeTool("b"))
    printer.add_object("mcu nhk", env.mcu)
    loadable = {
        "materials": env.materials,
        "toolchanger": env.tc,
        "gcode_macro": FakeGcodeMacro(env.scripts),
        "force_move": env.force_move,
    }
    printer.load_object = lambda cfg, name, default=None: loadable.get(
        name, default
    )
    env.t = toolheads.load_config(config.getsection("toolheads"))
    env.fire("klippy:connect")
    env.fire("klippy:ready")
    env.call = printer.call
    env.web = lambda path, **params: env.webhooks.endpoints[path](
        FakeWebRequest(params)
    )
    env.state = lambda name: (
        env.t.heads[name].state,
        env.t.heads[name].dock,
    )
    return env


CONFIG_DIR = pathlib.Path(__file__).parent / "test_configs" / "toolheads"


def new_env(tmp_path, overrides=(), mcu_disconnected=False):
    # The config file is only read; overrides are applied in memory.
    printer = PrinterShim({"config_file": str(CONFIG_DIR / "printer.cfg")})
    return build(printer, tmp_path, overrides, mcu_disconnected)


@pytest.fixture
def env(tmp_path):
    return new_env(tmp_path)


def test_status_from_config(env):
    st = env.t.get_status(0.0)
    assert st["dock_count"] == 2
    assert st["docks"] == ["gel", None]
    assert st["mounted"] == "pla"
    assert st["order"] == ["pla", "gel", "syringe", "usb"]
    assert st["toolheads"]["pla"]["state"] == "mounted"
    assert st["toolheads"]["gel"]["state"] == "docked"
    assert st["toolheads"]["gel"]["dock"] == 1
    assert st["toolheads"]["syringe"]["state"] == "unmounted"
    assert st["toolheads"]["syringe"]["can_swap"] is False
    assert st["toolheads"]["pla"]["can_swap"] is True
    assert st["toolheads"]["pla"]["materials"] == ["pla"]
    assert st["toolheads"]["usb"]["follows_mcu"] is True
    assert st["selectors"] == {"turntable": {"material_group": "vat"}}
    assert st["awaiting_mount"] is None


@pytest.mark.parametrize(
    "overrides,message",
    [
        ([("toolheads", "dock_count", "0")], "initial_dock"),
        (
            [("toolhead_unit pla", "material_group", "nope")],
            "Unknown material_group",
        ),
        (
            [("toolhead_unit gel", "material_group", "pla_head")],
            "claimed by more",
        ),
        (
            [("material_selector turntable", "material_group", "pla_head")],
            "claimed by more",
        ),
        ([("mcu nhk", "is_non_critical", "False")], "is_non_critical"),
        ([("toolhead_unit usb", "mcu", "missing")], r"no \[mcu missing\]"),
        ([("toolhead_unit pla", "tool", "zzz")], r"no \[tool zzz\]"),
        ([("toolhead_unit syringe", "initial_dock", "2")], "only applies"),
        ([("toolhead_unit pla", "initial_dock", "1")], "both start in dock 1"),
    ],
)
def test_config_errors(tmp_path, overrides, message):
    with pytest.raises(Exception, match=message):
        new_env(tmp_path, overrides)


def test_manual_mount_displaces_previous(env):
    env.t.set_state(env.t.heads["gel"], "mounted")
    assert env.state("gel") == ("mounted", 1)  # keeps its home dock
    assert env.state("pla") == ("unmounted", None)
    assert env.t.get_status(0.0)["mounted"] == "gel"
    assert env.t.get_status(0.0)["docks"] == ["gel", None]


def test_disconnected_head_moves_to_a_dock(env):
    env.t.set_state(env.t.heads["syringe"], "disconnected")
    assert env.state("syringe") == ("disconnected", None)
    env.t.set_state(env.t.heads["syringe"], "docked", 2)
    assert env.state("syringe") == ("docked", 2)
    assert env.t.get_status(0.0)["docks"] == ["gel", "syringe"]


def test_docking_without_a_dock_takes_the_first_free_one(env):
    env.t.set_state(env.t.heads["syringe"], "docked")
    assert env.state("syringe") == ("docked", 2)
    with pytest.raises(Exception, match="No free dock"):
        env.t.set_state(env.t.heads["pla"], "docked")


@pytest.mark.parametrize(
    "head,state,dock,message",
    [
        ("syringe", "docked", 1, "occupied"),
        ("syringe", "docked", 3, "does not exist"),
        ("syringe", "docked", 0, "does not exist"),
        ("syringe", "unmounted", 2, "no dock"),
        ("syringe", "flying", None, "State must be"),
    ],
)
def test_invalid_state_changes_leave_everything_alone(
    env, head, state, dock, message
):
    before = {n: env.state(n) for n in env.t.heads}
    with pytest.raises(Exception, match=message):
        env.t.set_state(env.t.heads[head], state, dock)
    assert {n: env.state(n) for n in env.t.heads} == before


def test_reorder_docks(env):
    env.t.set_state(env.t.heads["syringe"], "docked", 2)
    env.t.reorder_docks(["syringe", "gel"])
    assert env.t.get_status(0.0)["docks"] == ["syringe", "gel"]
    assert env.state("gel") == ("docked", 2)
    env.call("TOOLHEAD_REORDER_DOCKS ORDER=gel,syringe")
    assert env.t.get_status(0.0)["docks"] == ["gel", "syringe"]
    with pytest.raises(Exception, match="exactly the toolheads"):
        env.call("TOOLHEAD_REORDER_DOCKS ORDER=-,gel")


def test_reorder_docks_rejects_anything_but_a_permutation(env):
    env.t.set_state(env.t.heads["syringe"], "docked", 2)
    for order in (["gel"], ["gel", "gel"], ["gel", "pla"], ["gel", None]):
        with pytest.raises(Exception):
            env.t.reorder_docks(order)
    assert env.t.get_status(0.0)["docks"] == ["gel", "syringe"]


def test_state_survives_a_restart(tmp_path):
    first = new_env(tmp_path)
    first.t.set_state(first.t.heads["syringe"], "docked", 2)
    first.t.set_state(first.t.heads["gel"], "mounted")
    second = new_env(tmp_path)
    assert second.state("gel") == ("mounted", 1)
    assert second.state("syringe") == ("docked", 2)
    assert second.state("pla") == ("unmounted", None)


def test_a_bad_state_file_is_repaired_on_load(tmp_path):
    (tmp_path / "toolheads.cfg").write_text(
        "[head pla]\nstate = mounted\n"
        "[head usb]\nstate = mounted\n"
        "[head gel]\nstate = docked\ndock = 1\n"
        "[head syringe]\nstate = docked\ndock = 1\n"
        "[head ghost]\nstate = mounted\n"
    )
    env = new_env(tmp_path)
    assert env.state("pla") == ("mounted", None)
    assert env.state("usb") == ("unmounted", None)  # second mounted head
    assert env.state("gel") == ("docked", 1)
    assert env.state("syringe") == ("unmounted", None)  # dock 1 already taken
    assert "ghost" not in env.t.heads


def test_docked_heads_without_a_usable_dock_are_repaired_on_load(tmp_path):
    (tmp_path / "toolheads.cfg").write_text(
        "[head usb]\nstate = docked\n[head syringe]\nstate = docked\ndock = 3\n"
    )
    env = new_env(tmp_path)
    assert env.state("usb") == ("unmounted", None)
    assert env.state("syringe") == ("unmounted", None)  # only 2 docks


def test_a_corrupt_state_file_falls_back_to_config(tmp_path):
    (tmp_path / "toolheads.cfg").write_text("this is [not valid")
    env = new_env(tmp_path)
    assert env.state("pla") == ("mounted", None)
    assert (tmp_path / "toolheads.cfg.corrupt").exists()


def test_swapping_to_a_docked_head_drops_off_before_pickup(env):
    env.call("SELECT_MATERIAL MATERIAL=FFF_GEL")
    assert env.tc.moves == ["dropoff a dock=2", "pickup b dock=1"]
    assert env.state("gel") == ("mounted", 1)
    assert env.state("pla") == ("docked", 2)
    assert env.tc.active_tool.name == "b"
    # and back again
    env.call("SELECT_MATERIAL MATERIAL=FFF_PLA")
    assert env.tc.moves[2:] == ["dropoff b dock=1", "pickup a dock=2"]
    assert env.state("pla") == ("mounted", 2)
    assert env.state("gel") == ("docked", 1)


def test_material_already_in_place_does_not_move(env):
    env.call("SELECT_MATERIAL MATERIAL=FFF_PLA")
    env.call("SELECT_MATERIAL MATERIAL=pla")  # by id as well as token
    assert env.tc.moves == []
    assert env.scripts == []


def test_the_swap_state_is_persisted(tmp_path):
    first = new_env(tmp_path)
    first.call("SELECT_MATERIAL MATERIAL=FFF_GEL")
    second = new_env(tmp_path)
    assert second.state("gel") == ("mounted", 1)
    assert second.state("pla") == ("docked", 2)


def test_engine_that_forgot_its_tool_is_resynced_before_a_swap(env):
    env.tc.status = "uninitialized"  # what any g-code error does to it
    env.tc.active_tool = None
    env.call("SELECT_MATERIAL MATERIAL=FFF_GEL")
    assert env.tc.moves == ["dropoff a dock=2", "pickup b dock=1"]


def test_a_failed_swap_changes_nothing(env):
    env.tc.fail = Exception("jammed")
    with pytest.raises(Exception, match="jammed"):
        env.call("SELECT_MATERIAL MATERIAL=FFF_GEL")
    assert env.state("pla") == ("mounted", None)
    assert env.state("gel") == ("docked", 1)
    st = env.t.get_status(0.0)
    assert st["busy"] is False
    assert st["last_error"] == "jammed"


def test_a_swap_the_engine_swallowed_is_still_a_failure(env):
    env.tc.swallow = True
    with pytest.raises(Exception, match="Toolhead change failed"):
        env.call("SELECT_MATERIAL MATERIAL=FFF_GEL")
    assert env.state("pla") == ("mounted", None)
    assert env.state("gel") == ("docked", 1)


def test_no_free_dock_for_the_outgoing_head_blocks_the_swap(env):
    env.t.set_state(env.t.heads["syringe"], "docked", 2)
    with pytest.raises(Exception, match="No free dock for PLA head"):
        env.call("SELECT_MATERIAL MATERIAL=FFF_GEL")
    assert env.tc.moves == []
    assert env.state("pla") == ("mounted", None)


def test_select_toolhead(env):
    env.call("SELECT_TOOLHEAD TOOLHEAD=gel")
    assert env.state("gel") == ("mounted", 1)
    with pytest.raises(Exception, match="Unknown toolhead"):
        env.call("SELECT_TOOLHEAD TOOLHEAD=nope")


def test_unknown_or_unheld_material_is_an_error(env):
    with pytest.raises(Exception, match="Unknown material 'NOPE'"):
        env.call("SELECT_MATERIAL MATERIAL=NOPE")
    with pytest.raises(Exception, match="holds material 'lonely'"):
        env.call("SELECT_MATERIAL MATERIAL=FFF_LONELY")
    env.call("SELECT_MATERIAL MATERIAL=")
    assert any("ignoring" in m for m in env.infos)


def test_a_material_in_several_places_comes_from_the_easiest(env):
    env.materials.set_active_slot("vat", 1)
    # put the same material in a docked and an unmounted head
    env.materials.slots[("gel_head", 1)]["material"] = "pla"
    env.materials.slots[("syringe", 1)]["material"] = "pla"
    _, kind, holder, _ = env.t._best_source("pla")
    assert holder.name == "pla"  # mounted beats docked and unmounted
    env.t.set_state(env.t.heads["gel"], "mounted")
    _, _, holder, _ = env.t._best_source("pla")
    assert holder.name == "gel"  # now mounted


def test_selecting_announces_the_holder(env):
    env.call("SELECT_MATERIAL MATERIAL=FFF_PLA")
    env.call("SELECT_MATERIAL MATERIAL=SLA_B")
    env.call("SELECT_MATERIAL MATERIAL=SLA_B")  # already there
    holder = env.t.mounted_head().name
    env.call("SELECT_TOOLHEAD TOOLHEAD=%s" % (holder,))
    assert env.events == [
        ("toolheads:selected", holder),
        ("toolheads:selected", "turntable"),
        ("toolheads:selected", "turntable"),
        ("toolheads:selected", holder),
    ]


def test_a_selector_material_runs_its_macro_and_records_the_slot(env):
    env.call("SELECT_MATERIAL MATERIAL=SLA_B")
    assert env.scripts == [
        "SELECT_VAT SLOT=2 INDEX=1 MATERIAL=resin_b GROUP=vat"
    ]
    assert env.materials.get_active_slot("vat") == 2
    env.call("SELECT_MATERIAL MATERIAL=SLA_B")  # already there
    assert len(env.scripts) == 1
    env.call("SELECT_MATERIAL MATERIAL=resin_a")
    assert (
        env.scripts[-1]
        == "SELECT_VAT SLOT=1 INDEX=0 MATERIAL=resin_a GROUP=vat"
    )
    assert env.materials.get_active_slot("vat") == 1
    assert env.tc.moves == []  # a selector never exchanges a toolhead


def test_a_failing_selector_macro_leaves_the_active_slot_alone(
    env, monkeypatch
):
    env.materials.set_active_slot("vat", 1)

    def boom(context):
        raise Exception("stuck")

    monkeypatch.setattr(
        env.t.selectors["turntable"].select_gcode,
        "run_gcode_from_command",
        boom,
    )
    with pytest.raises(Exception, match="stuck"):
        env.call("SELECT_MATERIAL MATERIAL=SLA_B")
    assert env.materials.get_active_slot("vat") == 1
    assert env.t.busy is False


def test_nothing_moves_while_waiting_for_a_manual_mount(env):
    seen = {}

    def while_waiting():
        # runs at the first pause: the command is blocked, the user is busy
        seen["awaiting"] = env.t.get_status(0.0)["awaiting_mount"]
        seen["moves"] = list(env.tc.moves)
        seen["scripts"] = list(env.scripts)
        env.web("toolheads/confirm_mount")

    env.reactor.on_pause = [while_waiting]
    env.call("SELECT_MATERIAL MATERIAL=FFF_BIO")
    assert seen["awaiting"]["toolhead"] == "syringe"
    assert seen["awaiting"]["material"] == "bio"
    assert seen["awaiting"]["remove"] == "pla"
    assert seen["awaiting"]["reason"] == "unmounted"
    assert seen["moves"] == [] and seen["scripts"] == []
    # afterwards: still no exchange, the registry just trusts the user
    assert env.tc.moves == []
    assert env.state("syringe") == ("mounted", None)
    assert env.state("pla") == ("unmounted", None)
    assert env.t.get_status(0.0)["awaiting_mount"] is None
    assert env.tc.active_tool is None  # syringe has no [tool]


def test_the_removed_head_can_be_reported_docked(env):
    def confirm():
        env.web(
            "toolheads/confirm_mount", removed_state="docked", removed_dock=2
        )

    env.reactor.on_pause = [confirm]
    env.call("SELECT_MATERIAL MATERIAL=FFF_BIO")
    assert env.state("pla") == ("docked", 2)
    assert env.state("syringe") == ("mounted", None)


def test_a_bad_answer_keeps_waiting(env):
    def bad_then_good():
        with pytest.raises(Exception, match="occupied"):
            env.web(
                "toolheads/confirm_mount",
                removed_state="docked",
                removed_dock=1,
            )
        with pytest.raises(Exception, match="Choose a dock"):
            env.web("toolheads/confirm_mount", removed_state="docked")
        with pytest.raises(Exception, match="removed toolhead can be"):
            env.web("toolheads/confirm_mount", removed_state="mounted")
        assert env.t.awaiting is not None
        env.web("toolheads/confirm_mount")

    env.reactor.on_pause = [bad_then_good]
    env.call("SELECT_MATERIAL MATERIAL=FFF_BIO")
    assert env.state("syringe") == ("mounted", None)


def test_cancelling_fails_the_command_and_changes_nothing(env):
    env.reactor.on_pause = [lambda: env.web("toolheads/cancel_mount")]
    with pytest.raises(Exception, match="cancelled"):
        env.call("SELECT_MATERIAL MATERIAL=FFF_BIO")
    assert env.state("pla") == ("mounted", None)
    assert env.state("syringe") == ("unmounted", None)
    assert env.t.awaiting is None


def test_a_docked_head_without_motion_macros_is_mounted_by_hand(env):
    env.t.set_state(env.t.heads["syringe"], "docked", 2)
    env.reactor.on_pause = [lambda: env.web("toolheads/confirm_mount")]
    env.call("SELECT_MATERIAL MATERIAL=FFF_BIO")
    assert env.tc.moves == []
    assert env.state("syringe") == ("mounted", 2)  # keeps the dock it came from


def test_no_edits_while_waiting(env):
    def try_edits():
        with pytest.raises(Exception, match="in progress"):
            env.web("toolheads/set_state", toolhead="gel", state="mounted")
        with pytest.raises(Exception, match="in progress"):
            env.web("toolheads/reorder_docks", order=["gel", None])
        with pytest.raises(Exception, match="already in progress"):
            env.call("SELECT_MATERIAL MATERIAL=FFF_USB")
        env.web("toolheads/cancel_mount")

    env.reactor.on_pause = [try_edits]
    with pytest.raises(Exception, match="cancelled"):
        env.call("SELECT_MATERIAL MATERIAL=FFF_BIO")


def test_confirming_with_nothing_awaited_is_an_error(env):
    for path in ("toolheads/confirm_mount", "toolheads/cancel_mount"):
        with pytest.raises(Exception, match="No toolhead is being awaited"):
            env.web(path)


def test_shutdown_while_waiting_fails_the_command(env):
    env.printer.is_shutdown = lambda: True
    env.reactor.on_pause = [lambda: None]
    with pytest.raises(Exception, match="shut down"):
        env.call("SELECT_MATERIAL MATERIAL=FFF_BIO")
    assert env.t.awaiting is None


def test_preflight_reports_how_each_material_will_be_had(env):
    env.call("TOOLHEADS_PREFLIGHT MATERIALS=FFF_PLA,FFF_GEL,FFF_BIO,SLA_A")
    assert env.infos == [
        "FFF_PLA: PLA head, already mounted",
        "FFF_GEL: Gel head, automatic swap from dock 1",
        "FFF_BIO: Syringe, must be mounted by hand (unmounted)",
        "SLA_A: turntable, slot 1",
    ]
    assert env.tc.moves == [] and env.scripts == []


def test_preflight_lists_every_problem(env):
    with pytest.raises(Exception) as err:
        env.call("TOOLHEADS_PREFLIGHT MATERIALS=FFF_LONELY,NOPE,FFF_PLA")
    assert "holds material 'lonely'" in str(err.value)
    assert "Unknown material 'NOPE'" in str(err.value)


GONE = "danger:non_critical_mcu_nhk:disconnected"
BACK = "danger:non_critical_mcu_nhk:reconnected"


def unplug(env):
    env.mcu.non_critical_disconnected = True
    env.fire(GONE)


def plug_in(env):
    env.mcu.non_critical_disconnected = False
    env.fire(BACK)


def test_unplugging_marks_the_head_disconnected_and_replugging_restores_it(env):
    env.t.set_state(env.t.heads["usb"], "docked", 2)
    unplug(env)
    assert env.state("usb") == ("disconnected", None)
    assert env.t.get_status(0.0)["docks"] == ["gel", None]  # dock is free again
    plug_in(env)
    assert env.state("usb") == ("docked", 2)


def test_a_mounted_head_comes_back_mounted(env):
    env.t.set_state(env.t.heads["usb"], "mounted")
    unplug(env)
    plug_in(env)
    assert env.state("usb") == ("mounted", None)
    assert env.state("pla") == ("unmounted", None)


def test_restore_yields_to_what_happened_while_it_was_gone(env):
    env.t.set_state(env.t.heads["usb"], "docked", 2)
    unplug(env)
    env.t.set_state(env.t.heads["syringe"], "docked", 2)  # took its dock
    plug_in(env)
    assert env.state("usb") == ("unmounted", None)

    env.t.set_state(env.t.heads["usb"], "mounted")
    unplug(env)
    env.t.set_state(env.t.heads["gel"], "mounted")  # something else mounted
    plug_in(env)
    assert env.state("usb") == ("unmounted", None)
    assert env.state("gel") == ("mounted", 1)


def test_a_followed_head_cannot_be_placed_by_hand_while_unplugged(env):
    unplug(env)
    with pytest.raises(Exception, match="cannot be placed"):
        env.t.set_state(env.t.heads["usb"], "docked", 2)
    plug_in(env)
    with pytest.raises(Exception, match="marked disconnected by unplugging"):
        env.t.set_state(env.t.heads["usb"], "disconnected")


def test_a_board_that_is_absent_at_startup_starts_disconnected(tmp_path):
    env = new_env(tmp_path, mcu_disconnected=True)
    assert env.state("usb") == ("disconnected", None)
    assert env.t.heads["usb"].resume_state == "unmounted"


def test_the_restore_target_survives_a_restart(tmp_path):
    first = new_env(tmp_path)
    first.t.set_state(first.t.heads["usb"], "docked", 2)
    unplug(first)
    absent = new_env(
        tmp_path, mcu_disconnected=True
    )  # restarted while unplugged
    assert absent.state("usb") == ("disconnected", None)
    present = new_env(tmp_path)  # restarted after it was plugged in
    assert present.state("usb") == ("docked", 2)


def test_a_disconnected_owner_is_mounted_by_hand(env):
    unplug(env)
    seen = []
    env.reactor.on_pause = [
        lambda: (
            seen.append(env.t.get_status(0.0)["awaiting_mount"]["reason"]),
            env.web("toolheads/cancel_mount"),
        )
    ]
    with pytest.raises(Exception, match="cancelled"):
        env.call("SELECT_MATERIAL MATERIAL=FFF_USB")
    assert seen == ["disconnected"]


def test_set_state_endpoint_and_engine_sync(env):
    env.web("toolheads/set_state", toolhead="gel", state="mounted")
    assert env.state("gel") == ("mounted", 1)
    # the engine is told about it through the queued TOOLHEADS_SYNC
    assert env.tc.active_tool.name == "b"
    env.web("toolheads/set_state", toolhead="syringe", state="docked", dock=2)
    assert env.state("syringe") == ("docked", 2)
    with pytest.raises(Exception, match="Unknown toolhead"):
        env.web("toolheads/set_state", toolhead="zzz", state="mounted")


def test_manually_unmounting_clears_the_engines_active_tool(env):
    env.call("SELECT_MATERIAL MATERIAL=FFF_PLA")
    env.tc.active_tool = env.printer.lookup_object("tool a")
    env.web("toolheads/set_state", toolhead="pla", state="unmounted")
    assert env.tc.active_tool is None


def test_reorder_endpoint(env):
    env.t.set_state(env.t.heads["syringe"], "docked", 2)
    env.web("toolheads/reorder_docks", order=["syringe", "gel"])
    assert env.t.get_status(0.0)["docks"] == ["syringe", "gel"]
    with pytest.raises(Exception, match="must be a list"):
        env.web("toolheads/reorder_docks", order="gel")


@pytest.mark.parametrize(
    "order", [[["gel"], None], [{"name": "gel"}, None], [1, None]]
)
def test_reorder_endpoint_rejects_entries_that_are_not_names(env, order):
    # Anything but a command error escaping a webhooks handler shuts Klipper
    # down, so a malformed request must be an ordinary error.
    env.t.set_state(env.t.heads["syringe"], "docked", 2)
    before = env.t.get_status(0.0)["docks"]
    with pytest.raises(CommandError):
        env.web("toolheads/reorder_docks", order=order)
    assert env.t.get_status(0.0)["docks"] == before


_ODD_VALUES = [
    None,
    "",
    "x",
    "gel",
    "docked",
    0,
    -1,
    1,
    2,
    10**30,
    1.5,
    float("nan"),
    True,
    [],
    [[1]],
    [{"a": 1}, None],
    ["gel", "gel"],
    {},
]


@pytest.mark.parametrize(
    "path,names",
    [
        ("toolheads/set_state", ("toolhead", "state", "dock")),
        ("toolheads/reorder_docks", ("order",)),
        (
            "toolheads/confirm_mount",
            ("removed_state", "removed_dock"),
        ),
        ("toolheads/cancel_mount", ()),
    ],
)
def test_endpoints_only_ever_raise_command_errors(env, path, names):
    # Any other exception escaping a webhooks handler shuts Klipper down, and
    # the request body comes straight from the network.
    env.t.set_state(env.t.heads["syringe"], "docked", 2)
    good = {
        "toolhead": "gel",
        "state": "docked",
        "order": ["syringe", "gel"],
    }
    for name in names:
        for value in _ODD_VALUES:
            params = {n: good[n] for n in names if n in good}
            params[name] = value
            try:
                env.web(path, **params)
            except (CommandError, FakeWebRequestError):
                pass
            except Exception as e:
                raise AssertionError(
                    "%s %s=%r raised %s: %s"
                    % (path, name, value, type(e).__name__, e)
                )


def test_no_edits_while_a_print_runs_or_a_change_is_busy(env):
    env.printer.add_object("virtual_sdcard", env.sdcard)
    env.sdcard.active = True
    with pytest.raises(Exception, match="while a print is running"):
        env.web("toolheads/set_state", toolhead="gel", state="mounted")
    with pytest.raises(Exception, match="while a print is running"):
        env.call("TOOLHEAD_SET_STATE TOOLHEAD=gel STATE=mounted")
    env.sdcard.active = False
    env.t.busy = True
    with pytest.raises(Exception, match="in progress"):
        env.web("toolheads/reorder_docks", order=["gel", None])
    assert env.state("gel") == ("docked", 1)


def test_set_state_gcode(env):
    env.call("TOOLHEAD_SET_STATE TOOLHEAD=syringe STATE=Docked DOCK=2")
    assert env.state("syringe") == ("docked", 2)


def test_confirming_a_head_whose_board_is_unplugged_is_refused_and_it_keeps_waiting(
    env,
):
    unplug(env)

    def confirm_then_plug_in_and_confirm():
        with pytest.raises(Exception, match="cannot be placed"):
            env.web("toolheads/confirm_mount")
        assert env.t.awaiting is not None
        plug_in(env)  # comes back unmounted
        env.web("toolheads/confirm_mount")

    env.reactor.on_pause = [confirm_then_plug_in_and_confirm]
    env.call("SELECT_MATERIAL MATERIAL=FFF_USB")
    assert env.state("usb") == ("mounted", None)


def test_a_toolchanger_problem_after_set_state_is_reported_not_raised(env):
    env.tc.status = "error"
    env.call("TOOLHEAD_SET_STATE TOOLHEAD=gel STATE=mounted")
    assert env.state("gel") == ("mounted", 1)
    assert any("Toolhead state recorded, but" in m for m in env.infos)


FRAGILE = (("toolhead_unit syringe", "homeable", "False"),)


def test_heads_are_homeable_by_default(env):
    assert env.t.get_status(0.0)["toolheads"]["pla"]["homeable"] is True
    env.call("G28")
    assert len(env.g28_calls) == 1


def test_homing_is_untouched_while_a_homeable_head_is_mounted(tmp_path):
    env = new_env(tmp_path, FRAGILE)
    assert env.t.get_status(0.0)["toolheads"]["syringe"]["homeable"] is False
    env.call("G28 X=")
    assert len(env.g28_calls) == 1


def test_a_head_that_cannot_be_homed_is_never_homed(tmp_path):
    env = new_env(tmp_path, FRAGILE)
    env.call("TOOLHEAD_SET_STATE TOOLHEAD=syringe STATE=mounted")
    with pytest.raises(Exception, match="cannot be homed"):
        env.call("G28")
    with pytest.raises(Exception, match="TOOLHEAD_SET_ORIGIN"):
        env.call("G28 Z=")
    assert env.g28_calls == []


def test_the_axes_can_be_jogged_before_the_origin_is_set(tmp_path):
    env = new_env(tmp_path, FRAGILE)
    env.call("TOOLHEAD_SET_STATE TOOLHEAD=syringe STATE=mounted")
    # Middle of the travel, so there is room either way
    assert env.force_move.positions == [(120.0, 122.5, 0.0)]
    assert env.gcode_move.offsets == [(0.0, 0.0, 0.0)]
    assert env.toolhead.homed_axes == "xyz"
    assert env.t.get_status(0.0)["origin_set"] is False


def test_a_print_needs_the_origin(tmp_path):
    env = new_env(tmp_path, FRAGILE)
    env.call("TOOLHEAD_SET_STATE TOOLHEAD=syringe STATE=mounted")
    with pytest.raises(Exception, match="has no origin"):
        env.call("TOOLHEADS_PREFLIGHT MATERIALS=")
    env.call("TOOLHEAD_SET_ORIGIN")
    env.call("TOOLHEADS_PREFLIGHT MATERIALS=")


def test_the_origin_is_set_where_the_head_is_and_homing_is_then_a_no_op(
    tmp_path,
):
    env = new_env(tmp_path, FRAGILE)
    env.call("TOOLHEAD_SET_STATE TOOLHEAD=syringe STATE=mounted")
    env.call("TOOLHEAD_SET_ORIGIN Z=2")
    assert env.gcode_move.offsets[-1] == (30.0, 40.0, 3.0)
    assert env.t.get_status(0.0)["origin_set"] is True
    env.call("G28")
    assert env.g28_calls == []
    assert any("cannot be homed" in m for m in env.infos)


def test_the_origin_is_lost_when_the_motors_are_turned_off(tmp_path):
    env = new_env(tmp_path, FRAGILE)
    env.call("TOOLHEAD_SET_STATE TOOLHEAD=syringe STATE=mounted")
    env.call("TOOLHEAD_SET_ORIGIN Z=2")
    env.handlers["stepper_enable:motor_off"][0](0.0)
    assert env.t.get_status(0.0)["origin_set"] is False
    assert env.gcode_move.offsets[-1] == (0.0, 0.0, 0.0)


def test_setting_an_origin_needs_a_head_that_cannot_be_homed(tmp_path):
    env = new_env(tmp_path, FRAGILE)
    with pytest.raises(Exception, match="can be homed"):
        env.call("TOOLHEAD_SET_ORIGIN")
    assert env.force_move.positions == []


def test_the_origin_cannot_be_set_during_a_print(tmp_path):
    env = new_env(tmp_path, FRAGILE)
    env.call("TOOLHEAD_SET_STATE TOOLHEAD=syringe STATE=mounted")
    env.sdcard.active = True
    env.printer.add_object("virtual_sdcard", env.sdcard)
    with pytest.raises(Exception, match="while a print is running"):
        env.call("TOOLHEAD_SET_ORIGIN")


def test_removing_a_head_that_cannot_be_homed_forgets_the_axes(tmp_path):
    env = new_env(tmp_path, FRAGILE)
    env.call("TOOLHEAD_SET_STATE TOOLHEAD=syringe STATE=mounted")
    env.call("TOOLHEAD_SET_ORIGIN")
    env.call("TOOLHEAD_SET_STATE TOOLHEAD=pla STATE=mounted")
    assert env.toolhead.kinematics.cleared == [[0, 1, 2]]
    assert env.t.get_status(0.0)["origin_set"] is True


def test_changing_between_homeable_heads_keeps_the_axes(env):
    env.toolhead.homed_axes = "xyz"
    env.call("TOOLHEAD_SET_STATE TOOLHEAD=gel STATE=mounted")
    assert env.toolhead.kinematics.cleared == []
