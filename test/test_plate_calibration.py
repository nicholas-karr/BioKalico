import pathlib

import pytest
from klippy_testing import PrinterShim

from klippy.extras import plate_calibration

CONFIG_DIR = (
    pathlib.Path(__file__).parent / "test_configs" / "plate_calibration"
)


class CommandError(Exception):
    pass


class FakeReactor:
    def monotonic(self):
        return 0.0


class FakeToolhead:
    def __init__(self):
        self.homed = "xyz"
        self.position = [10.0, 20.0, 30.0, 0.0]

    def get_status(self, eventtime):
        return {"homed_axes": self.homed}

    def get_position(self):
        return list(self.position)


class FakeProjector:
    def __init__(self):
        self.rect = None
        self.shown = []
        self.cleared = 0
        self.width, self.height = 1920, 1080

    def set_plate_rect(self, rect):
        self.rect = None if rect is None else list(rect)

    def show_calibration_pattern(self, gcmd, pattern, rect=None):
        self.shown.append((pattern, rect))

    def clear_pattern(self, gcmd):
        self.cleared += 1

    def get_status(self, eventtime):
        return {"window_width": self.width, "window_height": self.height}


class FakeTool:
    def __init__(self):
        self.params = {
            "gcode_x_offset": 1.5,
            "gcode_y_offset": -2.0,
            "gcode_z_offset": 0.25,
        }
        self.original_params = {}

    def set_parameter(self, name, value):
        if name not in self.original_params:
            self.original_params[name] = self.params[name]
        self.params[name] = value

    def reset_parameter(self, name):
        if name in self.original_params:
            self.params[name] = self.original_params[name]


class FakeGcodeMove:
    def __init__(self):
        self.offset = [0.0, 0.0, 0.0]

    def cmd_SET_GCODE_OFFSET(self, gcmd):
        for pos, axis in enumerate("XYZ"):
            self.offset[pos] = gcmd.get_float(axis, self.offset[pos])


class FakePrintStats:
    state = "standby"


class Env:
    pass


def new_env(tmp_path, with_projector=True, with_tool=True):
    printer = PrinterShim({"config_file": str(CONFIG_DIR / "printer.cfg")})
    config = printer.lookup_object("configfile").read_main_config()
    config.getsection("plate_calibration").fileconfig.set(
        "plate_calibration", "state_file", str(tmp_path / "plate.cfg")
    )
    env = Env()
    env.handlers = {}
    env.infos = []
    env.toolhead = FakeToolhead()
    env.projector = FakeProjector()
    env.tool = FakeTool()
    env.gcode_move = FakeGcodeMove()
    env.print_stats = FakePrintStats()
    printer.command_error = CommandError
    printer.get_reactor = lambda: FakeReactor()
    printer.register_event_handler = lambda ev, cb: env.handlers.setdefault(
        ev, []
    ).append(cb)
    printer.lookup_object("gcode").respond_info = env.infos.append
    printer.add_object("toolhead", env.toolhead)
    printer.add_object("gcode_move", env.gcode_move)
    printer.add_object("print_stats", env.print_stats)
    if with_projector:
        printer.add_object("image_display", env.projector)
    if with_tool:
        printer.add_object("tool sla", env.tool)
    env.cal = plate_calibration.load_config(
        config.getsection("plate_calibration")
    )
    for cb in env.handlers.get("klippy:connect", []):
        cb()
    env.call = printer.call
    env.fire = lambda ev, *args: [cb(*args) for cb in env.handlers.get(ev, [])]
    return env


def test_rect_is_saved_applied_and_survives_a_restart(tmp_path):
    env = new_env(tmp_path)
    env.call("CAL_PLATE_RECT_SAVE X=100 Y=50 W=1600 H=900")
    assert env.projector.rect == [100, 50, 1600, 900]
    assert env.cal.get_status()["calibrated"]

    again = new_env(tmp_path)
    assert again.projector.rect == [100, 50, 1600, 900]
    assert again.cal.get_status()["rect"] == [100, 50, 1600, 900]
    assert again.cal.get_status()["calibrated_at"]


def test_preview_does_not_save(tmp_path):
    env = new_env(tmp_path)
    env.call("CAL_PLATE_RECT X=1 Y=2 W=30 H=40")
    assert env.projector.shown == [("rect", (1, 2, 30, 40))]
    assert env.cal.get_status()["rect"] is None
    assert env.cal.get_status()["preview"] == "rect"
    assert not (tmp_path / "plate.cfg").exists()


def test_rect_needs_positive_size(tmp_path):
    env = new_env(tmp_path)
    with pytest.raises(Exception):
        env.call("CAL_PLATE_RECT_SAVE X=0 Y=0 W=0 H=10")


def test_clear_rect_is_remembered(tmp_path):
    env = new_env(tmp_path)
    env.call("CAL_PLATE_RECT_SAVE X=1 Y=2 W=3 H=4")
    env.call("CAL_PLATE_RECT_CLEAR")
    assert env.projector.rect is None
    assert new_env(tmp_path).cal.get_status()["rect"] is None


def test_focus_saves_sla_z_and_gives_it_to_the_tool(tmp_path):
    env = new_env(tmp_path)
    env.call("CAL_PROJECTOR_FOCUS_START")
    assert env.projector.shown == [("focus", None)]
    env.call("CAL_PROJECTOR_FOCUS_SAVE")
    assert env.cal.get_status()["offsets"]["sla"] == {"z": 30.0}
    assert env.tool.params["gcode_z_offset"] == 30.0

    again = new_env(tmp_path)
    assert again.tool.params["gcode_z_offset"] == 30.0
    assert again.tool.params["gcode_x_offset"] == 1.5
    assert again.tool.params["gcode_y_offset"] == -2.0


def test_focus_needs_z_homed(tmp_path):
    env = new_env(tmp_path)
    env.toolhead.homed = "xy"
    with pytest.raises(Exception, match="Home Z"):
        env.call("CAL_PROJECTOR_FOCUS_SAVE")
    assert env.cal.get_status()["offsets"] == {}


def test_nozzle_corner_is_saved_per_tool(tmp_path):
    env = new_env(tmp_path, with_tool=False)
    env.call("CAL_TOOL_SAVE TOOL=nhk")
    env.toolhead.position = [11.0, 22.0, 3.0, 0.0]
    env.call("CAL_TOOL_SAVE TOOL=syringe")
    offsets = env.cal.get_status()["offsets"]
    assert offsets["nhk"] == {"x": 10.0, "y": 20.0, "z": 30.0}
    assert offsets["syringe"] == {"x": 11.0, "y": 22.0, "z": 3.0}
    assert new_env(tmp_path, with_tool=False).cal.get_status()["offsets"] == (
        offsets
    )


def test_nozzle_save_needs_all_axes_homed(tmp_path):
    env = new_env(tmp_path)
    env.toolhead.homed = "z"
    with pytest.raises(Exception, match="Home XY"):
        env.call("CAL_TOOL_SAVE TOOL=nhk")


def test_clear_tool_resets_its_offset(tmp_path):
    env = new_env(tmp_path)
    env.call("CAL_PROJECTOR_FOCUS_SAVE")
    env.call("CAL_TOOL_CLEAR TOOL=sla")
    assert env.cal.get_status()["offsets"] == {}
    assert env.tool.params["gcode_z_offset"] == 0.25


def printing_env(tmp_path):
    env = new_env(tmp_path, with_tool=False)
    env.call("CAL_PROJECTOR_FOCUS_SAVE")
    env.toolhead.position = [11.0, 22.0, 3.0, 0.0]
    env.call("CAL_TOOL_SAVE TOOL=nhk")
    env.print_stats.state = "printing"
    return env


def test_a_print_gets_the_saved_position_as_its_origin(tmp_path):
    env = printing_env(tmp_path)
    env.fire("toolheads:selected", "sla")
    assert env.gcode_move.offset == [0.0, 0.0, 30.0]
    assert env.cal.get_status()["origin_tool"] == "sla"
    env.fire("toolheads:selected", "nhk")
    assert env.gcode_move.offset == [11.0, 22.0, 3.0]
    assert env.cal.get_status()["origin_tool"] == "nhk"


def test_a_tool_without_a_saved_position_prints_in_machine_coordinates(
    tmp_path,
):
    env = printing_env(tmp_path)
    env.fire("toolheads:selected", "sla")
    env.fire("toolheads:selected", "syringe")
    assert env.gcode_move.offset == [0.0, 0.0, 0.0]
    assert env.cal.get_status()["origin_tool"] is None


@pytest.mark.parametrize("state", ["standby", "paused", "complete"])
def test_selecting_outside_a_print_leaves_the_origin_alone(tmp_path, state):
    env = printing_env(tmp_path)
    env.print_stats.state = state
    env.fire("toolheads:selected", "sla")
    assert env.gcode_move.offset == [0.0, 0.0, 0.0]
    assert env.cal.get_status()["origin_tool"] is None


@pytest.mark.parametrize("state", ["complete", "error", "cancelled"])
def test_the_origin_comes_off_when_the_print_ends(tmp_path, state):
    env = printing_env(tmp_path)
    env.fire("toolheads:selected", "sla")
    env.fire("print_stats:%s_printing" % (state,))
    assert env.gcode_move.offset == [0.0, 0.0, 0.0]
    assert env.cal.get_status()["origin_tool"] is None


def test_the_end_of_a_print_leaves_an_offset_it_did_not_set(tmp_path):
    env = printing_env(tmp_path)
    env.gcode_move.offset = [0.0, 0.0, 0.2]
    env.fire("homing:home_rails_begin", None, [])
    env.fire("print_stats:complete_printing")
    assert env.gcode_move.offset == [0.0, 0.0, 0.2]


def test_homing_is_done_in_machine_coordinates(tmp_path):
    env = printing_env(tmp_path)
    env.fire("toolheads:selected", "sla")
    env.fire("homing:home_rails_begin", None, [])
    assert env.gcode_move.offset == [0.0, 0.0, 0.0]
    # RESUME gives a paused print its offset back, so the end of the print
    # still has to take it off.
    env.gcode_move.offset = [0.0, 0.0, 30.0]
    env.fire("print_stats:complete_printing")
    assert env.gcode_move.offset == [0.0, 0.0, 0.0]


def test_homing_inside_a_running_print_restores_the_origin(tmp_path):
    env = printing_env(tmp_path)
    env.fire("toolheads:selected", "sla")
    saved = list(env.gcode_move.offset)
    assert saved != [0.0, 0.0, 0.0]
    env.fire("homing:home_rails_begin", None, [])
    assert env.gcode_move.offset == [0.0, 0.0, 0.0]
    env.fire("homing:home_rails_end", None, [])
    assert env.gcode_move.offset == saved


def test_homing_in_a_paused_print_leaves_the_restore_to_resume(tmp_path):
    env = printing_env(tmp_path)
    env.fire("toolheads:selected", "sla")
    env.fire("homing:home_rails_begin", None, [])
    env.print_stats.state = "paused"
    env.fire("homing:home_rails_end", None, [])
    assert env.gcode_move.offset == [0.0, 0.0, 0.0]


def test_changed_projector_resolution_is_flagged(tmp_path):
    env = new_env(tmp_path)
    env.call("CAL_PLATE_RECT_SAVE X=0 Y=0 W=10 H=10")
    assert not env.cal.get_status()["screen_mismatch"]
    env.projector.width = 3840
    status = env.cal.get_status()
    assert status["screen_mismatch"]
    assert not status["calibrated"]


def test_cancel_clears_only_a_preview(tmp_path):
    env = new_env(tmp_path)
    env.call("CAL_CANCEL")
    assert env.projector.cleared == 0
    env.call("CAL_PROJECTOR_FOCUS_START")
    env.call("CAL_CANCEL")
    assert env.projector.cleared == 1
    assert env.cal.get_status()["preview"] is None


def test_without_a_projector_the_commands_say_so(tmp_path):
    env = new_env(tmp_path, with_projector=False)
    with pytest.raises(Exception, match="image_display"):
        env.call("CAL_PROJECTOR_FOCUS_START")
    env.call("CAL_TOOL_SAVE TOOL=nhk")


def test_a_corrupt_state_file_is_moved_aside(tmp_path):
    (tmp_path / "plate.cfg").write_text("[plate\nnot ini")
    env = new_env(tmp_path)
    assert env.cal.get_status()["rect"] is None
    assert (tmp_path / "plate.cfg.corrupt").exists()


def test_a_bad_section_is_ignored(tmp_path):
    (tmp_path / "plate.cfg").write_text(
        "[plate]\nx = 1\ny = 2\nw = 0\nh = 4\n\n[tool nhk]\nx = 1.5\n"
    )
    env = new_env(tmp_path)
    assert env.cal.get_status()["rect"] is None
    assert env.cal.get_status()["offsets"] == {"nhk": {"x": 1.5}}
