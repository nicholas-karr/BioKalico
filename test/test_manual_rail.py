from types import SimpleNamespace
from unittest import mock

from klippy.extras.manual_rail import ManualRail


def test_position_range_sources_min_from_rail_when_homing_is_available():
    # With endstop_pin set and only position_max given, position_min must
    # default to 0.0 like PrinterRail, not None.
    rail = ManualRail.__new__(ManualRail)
    rail.can_home = True
    rail.rail = SimpleNamespace(get_range=mock.Mock(return_value=(0.0, 200.0)))

    pos_min, pos_max = rail._resolve_position_range(config=mock.Mock())

    assert (pos_min, pos_max) == (0.0, 200.0)


def test_position_range_is_optional_soft_clamp_without_homing():
    # Without endstop_pin, unset position_min/max mean no limit (None).
    rail = ManualRail.__new__(ManualRail)
    rail.can_home = False
    config = mock.Mock()
    config.getfloat.return_value = None

    pos_min, pos_max = rail._resolve_position_range(config)

    assert (pos_min, pos_max) == (None, None)
    config.getfloat.assert_any_call("position_min", None)
    config.getfloat.assert_any_call("position_max", None)
