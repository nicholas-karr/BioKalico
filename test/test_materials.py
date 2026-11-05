import pathlib
import time
import typing

import pytest
from klippy_testing import (
    FakeWebhooks,
    FakeWebRequest,
    FakeWebRequestError,
    PrinterShim,
)

import klippy.extras.materials as materials


def _load_materials(printer, config, state_file):
    section = config.getsection("materials")
    # Redirect the state file to a temp path before construction, so the extra
    # never touches the real ~/printer_data state and _load_state starts empty.
    section.fileconfig.set("materials", "state_file", str(state_file))
    printer.add_object("webhooks", FakeWebhooks())
    printer.command_error = Exception
    return materials.load_config(section)


def _fill_slots(printer):
    # Slot contents are only ever set at runtime, as Mainsail does: resin_a
    # in sla:1 and vat:1, pla in toolhead1:1.
    set_slot = printer.lookup_object("webhooks").endpoints["materials/set_slot"]
    for group, material in (
        ("sla", "resin_a"),
        ("toolhead1", "pla"),
        ("vat", "resin_a"),
    ):
        set_slot(
            FakeWebRequest({"group": group, "slot": 1, "material": material})
        )


def test_materials_status(
    config_root: typing.Annotated[pathlib.Path, "test_configs/materials"],
    tmp_path,
):
    start_args = {"config_file": str(config_root / "printer.cfg")}
    with PrinterShim(start_args) as printer:
        config = printer.lookup_object("configfile").read_main_config()
        m = _load_materials(printer, config, tmp_path / "state.cfg")

        st = m.get_status(0.0)
        # definitions parsed from config, exposed as clean (unprefixed) params
        assert set(st["materials"]) == {"resin_a", "pla"}
        assert st["materials"]["resin_a"]["channel_type"] == "sla"
        assert st["materials"]["resin_a"]["source"] == "config"
        # unit is derived: fluids in mL, filament in grams
        assert st["materials"]["resin_a"]["unit"] == "mL"
        assert st["materials"]["pla"]["unit"] == "g"
        # a material's default color is an ordinary params_color property;
        # materials without one simply don't have the key
        assert st["materials"]["resin_a"]["color"] == "#9e9e9e"
        assert st["materials"]["pla"].get("color") is None
        # groups in config order; the config never says what a slot holds,
        # so every slot starts empty
        assert list(st["slot_groups"]) == ["sla", "toolhead1", "vat"]
        for group in st["slot_groups"].values():
            assert all(slot["material"] is None for slot in group["slots"])

        _fill_slots(printer)
        st = m.get_status(0.0)
        sla = st["slot_groups"]["sla"]
        assert sla["count"] == 3 and len(sla["slots"]) == 3
        assert sla["slots"][0]["material"] == "resin_a"
        # a slot's own color starts unset (falls back to the material's
        # default in the UI) until explicitly overridden
        assert sla["slots"][0]["color"] is None
        assert sla["slots"][1]["material"] is None
        assert st["slot_groups"]["toolhead1"]["slots"][0]["material"] == "pla"
        # active_slot: config default for the toolhead, none for the SLA rack
        assert st["slot_groups"]["toolhead1"]["active_slot"] == 1
        assert st["slot_groups"]["sla"]["active_slot"] is None

        # uncovered_since by mode:
        # - uncovered_mode "auto" (sla): nothing is uncovered until a usage
        #   command makes a slot active, even though slot 1 already holds a
        #   material.
        assert sla["slots"][0]["uncovered_since"] is None
        # - uncovered_mode "off" (toolhead1, the default): never shown, even
        #   though this slot's config default makes it "active".
        assert (
            st["slot_groups"]["toolhead1"]["slots"][0]["uncovered_since"]
            is None
        )
        # - uncovered_mode "always" (vat): an occupied slot is uncovered
        #   from the moment its material was put in.
        assert st["slot_groups"]["vat"]["slots"][0]["material"] == "resin_a"
        assert (
            st["slot_groups"]["vat"]["slots"][0]["uncovered_since"] is not None
        )


def test_materials_endpoints_and_persistence(
    config_root: typing.Annotated[pathlib.Path, "test_configs/materials"],
    tmp_path,
):
    state_file = tmp_path / "state.cfg"
    start_args = {"config_file": str(config_root / "printer.cfg")}

    with PrinterShim(start_args) as printer:
        config = printer.lookup_object("configfile").read_main_config()
        m = _load_materials(printer, config, state_file)
        _fill_slots(printer)
        wh = printer.lookup_object("webhooks").endpoints

        # An apostrophe in comments is fine over the webhooks JSON path.
        req = FakeWebRequest(
            {
                "group": "sla",
                "slot": 2,
                "material": "resin_a",
                "amount": 500.0,
                "container_height": 30,
                "comments": "it's fresh",
            }
        )
        wh["materials/set_slot"](req)
        assert req.sent["material"] == "resin_a"
        assert req.sent["amount"] == 500.0
        assert req.sent["comments"] == "it's fresh"
        assert req.sent["date_inserted"] is not None
        # sla is uncovered_mode "auto": slot 2 isn't the active slot yet, so
        # it isn't uncovered just because it was assigned a material.
        assert req.sent["uncovered_since"] is None
        assert state_file.exists()

        # MATERIAL_CONSUME decrements and clamps at zero
        printer.call("MATERIAL_CONSUME GROUP=sla SLOT=2 AMOUNT=200")
        assert m.slots[("sla", 2)]["amount"] == 300.0
        printer.call("MATERIAL_CONSUME GROUP=sla SLOT=2 AMOUNT=99999")
        assert m.slots[("sla", 2)]["amount"] == 0.0
        # a usage command makes that slot active, and so uncovered in "auto"
        status = m.get_status(0.0)
        assert status["slot_groups"]["sla"]["active_slot"] == 2
        assert (
            status["slot_groups"]["sla"]["slots"][1]["uncovered_since"]
            is not None
        )
        # slot 1 holds a material but is not active, so stays covered
        assert (
            status["slot_groups"]["sla"]["slots"][0]["uncovered_since"] is None
        )

        # clear_slot empties the slot
        wh["materials/clear_slot"](FakeWebRequest({"group": "sla", "slot": 1}))
        assert (
            m.get_status(0.0)["slot_groups"]["sla"]["slots"][0]["material"]
            is None
        )

        # a user-created definition round-trips and can be deleted
        wh["materials/set_definition"](
            FakeWebRequest(
                {
                    "id": "pcl",
                    "display_name": "PCL",
                    "params": {"channel_type": "fff"},
                }
            )
        )
        assert m.get_status(0.0)["materials"]["pcl"]["source"] == "user"
        wh["materials/delete_definition"](FakeWebRequest({"id": "pcl"}))
        assert "pcl" not in m.get_status(0.0)["materials"]

        # deleting a material still referenced by a slot is rejected
        with pytest.raises(Exception):
            wh["materials/delete_definition"](FakeWebRequest({"id": "resin_a"}))

    # Reload from the same state file: the edits above must persist.
    with PrinterShim(start_args) as printer:
        config = printer.lookup_object("configfile").read_main_config()
        m2 = _load_materials(printer, config, state_file)
        slot = m2.get_status(0.0)["slot_groups"]["sla"]["slots"][1]
        assert slot["material"] == "resin_a"
        assert slot["amount"] == 0.0
        assert slot["comments"] == "it's fresh"
        # the cleared slot 1 stays cleared across reload
        assert (
            m2.get_status(0.0)["slot_groups"]["sla"]["slots"][0]["material"]
            is None
        )
        # the active channel set by MATERIAL_CONSUME, and its derived
        # uncovered state, both persist across reload
        status2 = m2.get_status(0.0)
        assert status2["slot_groups"]["sla"]["active_slot"] == 2
        assert (
            status2["slot_groups"]["sla"]["slots"][1]["uncovered_since"]
            is not None
        )


def test_materials_rejects_negative_consumption(
    config_root: typing.Annotated[pathlib.Path, "test_configs/materials"],
    tmp_path,
):
    start_args = {"config_file": str(config_root / "printer.cfg")}
    with PrinterShim(start_args) as printer:
        config = printer.lookup_object("configfile").read_main_config()
        m = _load_materials(printer, config, tmp_path / "state.cfg")
        _fill_slots(printer)
        before = m.get_status(0.0)["slot_groups"]["toolhead1"]["slots"][0][
            "amount"
        ]

        with pytest.raises(Exception):
            printer.call("MATERIAL_CONSUME GROUP=toolhead1 SLOT=1 AMOUNT=-10")
        with pytest.raises(Exception):
            printer.call("MATERIAL_MARK_USED GROUP=toolhead1 SLOT=1 AMOUNT=-10")

        after = m.get_status(0.0)["slot_groups"]["toolhead1"]["slots"][0][
            "amount"
        ]
        assert after == before


def test_materials_rejects_usage_without_inventory(
    config_root: typing.Annotated[pathlib.Path, "test_configs/materials"],
    tmp_path,
):
    start_args = {"config_file": str(config_root / "printer.cfg")}
    with PrinterShim(start_args) as printer:
        config = printer.lookup_object("configfile").read_main_config()
        m = _load_materials(printer, config, tmp_path / "state.cfg")
        _fill_slots(printer)

        # A material with no measured amount must stay "unknown";
        # subtracting from it must not silently convert that state to zero.
        assert m.slots[("toolhead1", 1)]["amount"] is None
        with pytest.raises(Exception):
            printer.call("MATERIAL_CONSUME GROUP=toolhead1 SLOT=1 AMOUNT=10")
        with pytest.raises(Exception):
            printer.call("MATERIAL_MARK_USED GROUP=toolhead1 SLOT=1 AMOUNT=10")
        assert m.slots[("toolhead1", 1)]["amount"] is None

        # Usage commands must not make an empty slot the active channel.
        with pytest.raises(Exception):
            printer.call("MATERIAL_MARK_USED GROUP=sla SLOT=2")
        assert m.active.get("sla") is None


def test_materials_rejects_invalid_numeric_state_and_fully_clears(
    config_root: typing.Annotated[pathlib.Path, "test_configs/materials"],
    tmp_path,
):
    start_args = {"config_file": str(config_root / "printer.cfg")}
    with PrinterShim(start_args) as printer:
        config = printer.lookup_object("configfile").read_main_config()
        m = _load_materials(printer, config, tmp_path / "state.cfg")
        _fill_slots(printer)
        wh = printer.lookup_object("webhooks").endpoints

        for field, value in (
            ("amount", -1),
            ("amount", float("nan")),
            ("container_height", float("inf")),
            ("date_inserted", -1),
        ):
            with pytest.raises(Exception):
                wh["materials/set_slot"](
                    FakeWebRequest(
                        {"group": "toolhead1", "slot": 1, field: value}
                    )
                )

        # Selecting "None" through the edit form uses set_slot rather than
        # clear_slot, so it must not leave hidden inventory metadata behind.
        wh["materials/set_slot"](
            FakeWebRequest(
                {
                    "group": "toolhead1",
                    "slot": 1,
                    "amount": 100,
                    "comments": "old spool",
                }
            )
        )
        req = FakeWebRequest(
            {
                "group": "toolhead1",
                "slot": 1,
                "material": None,
                "amount": 100,
                "comments": "stale form values",
            }
        )
        wh["materials/set_slot"](req)
        assert req.sent["material"] is None
        assert req.sent["amount"] is None
        assert req.sent["comments"] == ""
        assert req.sent["date_inserted"] is None


def test_materials_reassigning_same_material_stamps_missing_date_inserted(
    config_root: typing.Annotated[pathlib.Path, "test_configs/materials"],
    tmp_path,
):
    # Assigning a slot the material it already has must still set a
    # date_inserted that was cleared.
    state_file = tmp_path / "state.cfg"
    start_args = {"config_file": str(config_root / "printer.cfg")}
    with PrinterShim(start_args) as printer:
        config = printer.lookup_object("configfile").read_main_config()
        m = _load_materials(printer, config, state_file)
        _fill_slots(printer)
        wh = printer.lookup_object("webhooks").endpoints

        req = FakeWebRequest({"group": "vat", "slot": 1, "date_inserted": None})
        wh["materials/set_slot"](req)
        assert req.sent["date_inserted"] is None

        req = FakeWebRequest({"group": "vat", "slot": 1, "material": "resin_a"})
        wh["materials/set_slot"](req)
        assert req.sent["date_inserted"] is not None
        assert req.sent["uncovered_since"] is not None


def test_materials_self_heals_missing_date_inserted(
    config_root: typing.Annotated[pathlib.Path, "test_configs/materials"],
    tmp_path,
):
    # A saved record with a material but no date_inserted gets one on load.
    state_file = tmp_path / "state.cfg"
    state_file.write_text(
        "[slot vat:1]\n"
        "material = 'resin_a'\n"
        "amount = None\n"
        "container_height = None\n"
        "comments = ''\n"
        "last_used = None\n"
    )
    start_args = {"config_file": str(config_root / "printer.cfg")}
    with PrinterShim(start_args) as printer:
        config = printer.lookup_object("configfile").read_main_config()
        m = _load_materials(printer, config, state_file)
        slot = m.get_status(0.0)["slot_groups"]["vat"]["slots"][0]
        assert slot["material"] == "resin_a"
        assert slot["uncovered_since"] is not None


def test_materials_color(
    config_root: typing.Annotated[pathlib.Path, "test_configs/materials"],
    tmp_path,
):
    start_args = {"config_file": str(config_root / "printer.cfg")}
    with PrinterShim(start_args) as printer:
        config = printer.lookup_object("configfile").read_main_config()
        m = _load_materials(printer, config, tmp_path / "state.cfg")
        _fill_slots(printer)
        wh = printer.lookup_object("webhooks").endpoints

        # explicit color override on an occupied slot
        req = FakeWebRequest({"group": "sla", "slot": 1, "color": "#ff8800"})
        wh["materials/set_slot"](req)
        assert req.sent["color"] == "#ff8800"

        # invalid hex is rejected, not silently accepted
        with pytest.raises(Exception):
            wh["materials/set_slot"](
                FakeWebRequest({"group": "sla", "slot": 1, "color": "orange"})
            )
        # rejection didn't corrupt the previously-set color
        assert (
            m.get_status(0.0)["slot_groups"]["sla"]["slots"][0]["color"]
            == "#ff8800"
        )

        # assigning a different material resets the color override
        wh["materials/set_slot"](
            FakeWebRequest({"group": "sla", "slot": 1, "material": "pla"})
        )
        assert (
            m.get_status(0.0)["slot_groups"]["sla"]["slots"][0]["color"] is None
        )

        # setting material and color together keeps the new color
        req = FakeWebRequest(
            {
                "group": "sla",
                "slot": 1,
                "material": "resin_a",
                "color": "#112233",
            }
        )
        wh["materials/set_slot"](req)
        assert req.sent["color"] == "#112233"

        # empty string clears the override back to unset
        wh["materials/set_slot"](
            FakeWebRequest({"group": "sla", "slot": 1, "color": ""})
        )
        assert (
            m.get_status(0.0)["slot_groups"]["sla"]["slots"][0]["color"] is None
        )


def test_materials_set_uncovered_duration(
    config_root: typing.Annotated[pathlib.Path, "test_configs/materials"],
    tmp_path,
):
    start_args = {"config_file": str(config_root / "printer.cfg")}
    with PrinterShim(start_args) as printer:
        config = printer.lookup_object("configfile").read_main_config()
        m = _load_materials(printer, config, tmp_path / "state.cfg")
        _fill_slots(printer)
        wh = printer.lookup_object("webhooks").endpoints

        # status exposes uncovered_mode per group
        status = m.get_status(0.0)
        assert status["slot_groups"]["sla"]["uncovered_mode"] == "auto"
        assert status["slot_groups"]["vat"]["uncovered_mode"] == "always"
        assert status["slot_groups"]["toolhead1"]["uncovered_mode"] == "off"

        # only "auto" groups accept a duration edit
        with pytest.raises(Exception):
            wh["materials/set_uncovered_duration"](
                FakeWebRequest({"group": "vat", "slot": 1, "seconds": 60})
            )
        with pytest.raises(Exception):
            wh["materials/set_uncovered_duration"](
                FakeWebRequest({"group": "toolhead1", "slot": 1, "seconds": 60})
            )

        # sla slot 1 is not active yet, so it can't be edited
        with pytest.raises(Exception):
            wh["materials/set_uncovered_duration"](
                FakeWebRequest({"group": "sla", "slot": 1, "seconds": 60})
            )
        printer.call("MATERIAL_MARK_USED GROUP=sla SLOT=1")
        assert m.get_status(0.0)["slot_groups"]["sla"]["active_slot"] == 1

        # negative durations make no sense
        with pytest.raises(Exception):
            wh["materials/set_uncovered_duration"](
                FakeWebRequest({"group": "sla", "slot": 1, "seconds": -1})
            )

        # the reported duration becomes roughly `seconds`
        req = FakeWebRequest({"group": "sla", "slot": 1, "seconds": 3600})
        wh["materials/set_uncovered_duration"](req)
        slot = m.get_status(0.0)["slot_groups"]["sla"]["slots"][0]
        assert slot["uncovered_since"] is not None
        assert abs(time.time() - slot["uncovered_since"] - 3600) < 5


def test_set_definition_rejects_control_characters_in_id(
    config_root: typing.Annotated[pathlib.Path, "test_configs/materials"],
    tmp_path,
):
    # A newline in a material id would make the state file unreadable.
    start_args = {"config_file": str(config_root / "printer.cfg")}
    with PrinterShim(start_args) as printer:
        config = printer.lookup_object("configfile").read_main_config()
        m = _load_materials(printer, config, tmp_path / "state.cfg")
        wh = printer.lookup_object("webhooks").endpoints

        with pytest.raises(Exception):
            wh["materials/set_definition"](
                FakeWebRequest({"id": "evil\nbogus", "display_name": "Evil"})
            )
        assert "evil\nbogus" not in m.get_status(0.0)["materials"]

        # a normal id is unaffected
        wh["materials/set_definition"](
            FakeWebRequest({"id": "pcl", "display_name": "PCL"})
        )
        assert "pcl" in m.get_status(0.0)["materials"]


def test_corrupt_state_file_is_quarantined_instead_of_blocking_startup(
    config_root: typing.Annotated[pathlib.Path, "test_configs/materials"],
    tmp_path,
):
    # An unreadable state file is moved aside and Klippy starts with empty
    # state.
    state_file = tmp_path / "state.cfg"
    state_file.write_text("[definition foo\nbogus]\ndisplay_name = x\n")
    start_args = {"config_file": str(config_root / "printer.cfg")}
    with PrinterShim(start_args) as printer:
        config = printer.lookup_object("configfile").read_main_config()
        m = _load_materials(printer, config, state_file)
        # started with no user materials, only the config's
        assert "foo" not in m.get_status(0.0)["materials"]
        # the corrupt content was moved aside
        corrupt_backup = pathlib.Path(str(state_file) + ".corrupt")
        assert corrupt_backup.exists()
        assert "bogus" in corrupt_backup.read_text()
        # a new state file may have been saved, but without the bad content
        if state_file.exists():
            assert "bogus" not in state_file.read_text()


class CommandError(Exception):
    """Stands in for printer.command_error, the only error a webhooks
    handler may raise without shutting Klipper down."""


_ODD_VALUES = [
    None,
    "",
    " ",
    "x",
    "1",
    "#zzzzzz",
    "a\nb",
    0,
    -1,
    1,
    10**30,
    1.5,
    float("nan"),
    float("inf"),
    True,
    [],
    [1],
    [[1]],
    {},
    {"a": 1},
    {"a": [1]},
]

_ENDPOINT_ARGS = {
    "materials/set_slot": (
        "group",
        "slot",
        "material",
        "amount",
        "container_height",
        "comments",
        "color",
        "date_inserted",
    ),
    "materials/clear_slot": ("group", "slot"),
    "materials/set_definition": ("id", "display_name", "params"),
    "materials/delete_definition": ("id",),
    "materials/set_uncovered_duration": ("group", "slot", "seconds"),
}


def test_materials_endpoints_only_ever_raise_command_errors(
    config_root: typing.Annotated[pathlib.Path, "test_configs/materials"],
    tmp_path,
):
    # Any other exception escaping a webhooks handler shuts Klipper down, and
    # the request body comes straight from the network.
    start_args = {"config_file": str(config_root / "printer.cfg")}
    with PrinterShim(start_args) as printer:
        config = printer.lookup_object("configfile").read_main_config()
        _load_materials(printer, config, tmp_path / "state.cfg")
        printer.command_error = CommandError
        wh = printer.lookup_object("webhooks").endpoints
        good = {
            "group": "sla",
            "slot": 1,
            "material": "resin_a",
            "id": "pcl",
            "seconds": 5.0,
        }
        for path, names in _ENDPOINT_ARGS.items():
            for name in names:
                for value in _ODD_VALUES:
                    params = {n: good[n] for n in names if n in good}
                    params[name] = value
                    try:
                        wh[path](FakeWebRequest(params))
                    except (CommandError, FakeWebRequestError):
                        pass
                    except Exception as e:
                        raise AssertionError(
                            "%s %s=%r raised %s: %s"
                            % (path, name, value, type(e).__name__, e)
                        )


@pytest.mark.parametrize(
    "display_name",
    ["A\nB", "A\n\nB", "[section]", "; note", "#note", "a = b", "100%", "\r"],
)
def test_material_display_names_survive_a_restart(
    config_root: typing.Annotated[pathlib.Path, "test_configs/materials"],
    tmp_path,
    display_name,
):
    state_file = tmp_path / "state.cfg"
    start_args = {"config_file": str(config_root / "printer.cfg")}
    with PrinterShim(start_args) as printer:
        config = printer.lookup_object("configfile").read_main_config()
        _load_materials(printer, config, state_file)
        printer.lookup_object("webhooks").endpoints["materials/set_definition"](
            FakeWebRequest({"id": "pcl", "display_name": display_name})
        )
    with PrinterShim(start_args) as printer:
        config = printer.lookup_object("configfile").read_main_config()
        m = _load_materials(printer, config, state_file)
        assert m.get_status(0.0)["materials"]["pcl"]["display_name"] == (
            display_name.strip()
        )
        assert not (tmp_path / "state.cfg.corrupt").exists()
