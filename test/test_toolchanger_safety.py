from types import SimpleNamespace
from unittest import mock

import pytest

from klippy.extras import toolchanger as toolchanger_module
from klippy.extras.bed_thermal_adjust import BED_COOLDOWN_TIME, BedThermalAdjust
from klippy.extras.tool_probe_endstop import ToolProbeEndstop
from klippy.extras.toolchanger import (
    AMBIGUOUS_TOOL,
    DETECT_PRESENT,
    Toolchanger,
    ToolInterval,
    ToolMissingHelper,
)


def _ambiguous_changer():
    changer = Toolchanger.__new__(Toolchanger)
    changer.detected_tool = None
    changer.tools = {
        0: SimpleNamespace(name="T0", detect_state=DETECT_PRESENT),
        1: SimpleNamespace(name="T1", detect_state=DETECT_PRESENT),
    }
    # Minimal state for process_error() (called via validate_detected_tool)
    # to run without a full Toolchanger/printer setup: no tool change in
    # progress, no error_gcode configured.
    changer.current_change_id = -1
    changer.error_gcode = None
    return changer


def test_require_detected_tool_rejects_ambiguous_detection():
    changer = _ambiguous_changer()
    responses = []

    # Ambiguous detection must differ from "no tool" (None).
    assert changer.require_detected_tool(responses.append) is AMBIGUOUS_TOOL
    assert responses == ["Multiple tools detected: ['T0', 'T1']"]


def test_validate_detected_tool_rejects_ambiguous_even_when_no_tool_expected():
    # Verifying that no tool is mounted must fail when two tools are
    # detected at once.
    changer = _ambiguous_changer()

    with pytest.raises(Exception, match="Multiple tools detected"):
        changer.validate_detected_tool(
            None, respond_info=lambda *a: None, raise_error=Exception
        )


def test_deactivate_invalidates_delayed_tool_missing_callback():
    helper = ToolMissingHelper.__new__(ToolMissingHelper)
    helper.active_intervals = [ToolInterval(10.0, "T0")]
    helper.tool_lasttime = 10.0

    helper.deactivate_at_time(11.0)

    assert helper.active_intervals[-1].end == 11.0
    assert helper.tool_lasttime == 0.0


def _bed_thermal_adjust(active, active_timer, inactive_timer):
    adjust = BedThermalAdjust.__new__(BedThermalAdjust)
    adjust.active = active
    adjust.use_bed_temp = True
    adjust.active_timer = active_timer
    adjust.inactive_timer = inactive_timer
    adjust.ambient_temp = 20.0
    adjust.heater_bed = mock.Mock()
    adjust.heater_bed.get_status.return_value = {"temperature": 45.0}
    adjust.update_heater_bed = mock.Mock()
    return adjust


def test_bed_ambient_is_recaptured_only_after_cooldown():
    # The bed only stands in for room temperature after it has been off for
    # the whole cooldown period.
    gcmd = mock.Mock()
    gcmd.get_float.return_value = 60.0  # turning the heater back on

    # Off, but not yet cooled down (no time has passed): no resample.
    not_yet_cooled = _bed_thermal_adjust(
        active=False, active_timer=10.0, inactive_timer=10.0
    )
    not_yet_cooled.cmd_M140(gcmd)
    assert not_yet_cooled.ambient_temp == 20.0

    # Off, exactly at the cooldown boundary: resample happens.
    cooled = _bed_thermal_adjust(
        active=False, active_timer=10.0, inactive_timer=10.0 + BED_COOLDOWN_TIME
    )
    cooled.cmd_M140(gcmd)
    assert cooled.ambient_temp == 45.0


def test_bed_ambient_is_not_recaptured_while_actively_heating():
    # Never resample while the bed is heating. bed_cooled_down is forced
    # true (not possible in practice) to show that self.active alone
    # prevents it.
    adjust = _bed_thermal_adjust(
        active=True, active_timer=10.0, inactive_timer=10.0 + BED_COOLDOWN_TIME
    )
    gcmd = mock.Mock()
    gcmd.get_float.return_value = 60.0

    adjust.cmd_M140(gcmd)
    assert adjust.ambient_temp == 20.0


def _bare_tool_probe_endstop():
    endstop = ToolProbeEndstop.__new__(ToolProbeEndstop)
    endstop.probes = []
    endstop.tool_number_to_probe = {}
    endstop.mcu_probe = mock.Mock()
    return endstop


def test_tool_probe_endstop_add_probe_is_idempotent():
    # A [tool_probe] with `tool:` registers itself for crash detection, and
    # the toolchanger may add the same probe again for Z probing. The
    # second add must be a no-op.
    endstop = _bare_tool_probe_endstop()
    tool_probe = SimpleNamespace(tool_number=3, mcu_probe=mock.Mock())

    endstop.add_probe(mock.Mock(), tool_probe)
    endstop.add_probe(mock.Mock(), tool_probe)

    assert endstop.probes == [tool_probe]
    assert endstop.tool_number_to_probe == {3: tool_probe}
    assert endstop.mcu_probe.add_mcu.call_count == 1


def test_tool_probe_endstop_add_probe_still_rejects_real_duplicate_tool_number():
    endstop = _bare_tool_probe_endstop()
    probe_a = SimpleNamespace(tool_number=3, mcu_probe=mock.Mock())
    probe_b = SimpleNamespace(tool_number=3, mcu_probe=mock.Mock())
    config = mock.Mock()
    config.error = Exception

    endstop.add_probe(config, probe_a)
    with pytest.raises(Exception, match="Duplicate tool probe"):
        endstop.add_probe(config, probe_b)


def test_toolchanger_add_probe_reuses_existing_tool_probe_endstop():
    # If a [tool_probe] already registered the shared ToolProbeEndstop as
    # 'probe', add_probe must reuse it instead of creating a second one.
    existing = _bare_tool_probe_endstop()
    changer = Toolchanger.__new__(Toolchanger)
    changer.tool_probe_endstop = None
    changer.config = mock.Mock()
    changer.printer = mock.Mock()
    changer.printer.lookup_object.return_value = existing

    def _fail_if_constructed(self, *args, **kwargs):
        raise AssertionError("should not construct a second ToolProbeEndstop")

    with mock.patch.object(
        toolchanger_module.tool_probe_endstop.ToolProbeEndstop,
        "__init__",
        _fail_if_constructed,
    ):
        probe = SimpleNamespace(tool_number=1, mcu_probe=mock.Mock())
        changer.add_probe(probe)

    assert changer.tool_probe_endstop is existing
    assert existing.probes == [probe]


def test_toolchanger_add_probe_still_rejects_genuine_probe_conflict():
    # A real [probe] section registered as 'probe' is still a conflict.
    changer = Toolchanger.__new__(Toolchanger)
    changer.tool_probe_endstop = None
    changer.config = mock.Mock()
    changer.printer = mock.Mock()
    changer.printer.lookup_object.return_value = (
        SimpleNamespace()
    )  # not a ToolProbeEndstop

    constructed = []

    def _fake_init(self, config, standalone=True):
        constructed.append(standalone)
        self.probes = []
        self.tool_number_to_probe = {}
        self.mcu_probe = mock.Mock()

    with mock.patch.object(
        toolchanger_module.tool_probe_endstop.ToolProbeEndstop,
        "__init__",
        _fake_init,
    ):
        probe = SimpleNamespace(tool_number=1, mcu_probe=mock.Mock())
        changer.add_probe(probe)

    assert constructed == [False]
