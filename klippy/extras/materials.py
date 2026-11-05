# Materials inventory tracking for a FFF + SLA hybrid bioprinter.
#
# Each toolhead or SLA vat set has material slots. A slot holds a material
# plus its amount, container height, dates and comments. Materials and slot
# groups come from the config; slot contents and new materials are edited in
# Mainsail and saved to a state file.
#
# Copyright (C) 2026  Nicholas Karr
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import ast
import configparser
import logging
import math
import os
import re
import tempfile
import time

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# Material ids become section names in the state file. A newline in one
# would make the file unreadable, and it would be discarded on the next
# restart.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")

# Per-slot fields saved in the state file. container_height is for
# reference only; amount is never computed from it, since containers aren't
# simple cylinders. color overrides the material's params_color for this
# slot only (None uses the material's color). uncovered_since is not stored;
# see _derive_uncovered_since.
SLOT_FIELDS = (
    "material",
    "amount",
    "container_height",
    "comments",
    "color",
    "date_inserted",
    "last_used",
)

UNCOVERED_MODES = ("off", "auto", "always")


def _normalize_color(value):
    # '#' starts a comment in Klipper config files, so colors are written
    # there without it. Add it back.
    value = value.strip()
    if value and not value.startswith("#"):
        value = "#" + value
    return value


def read_state_file(path, label):
    # Returns None when the file is missing or corrupt. A corrupt file is
    # renamed to *.corrupt so Klippy can still start.
    if not os.path.exists(path):
        return None
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path)
    except configparser.Error:
        logging.exception(
            "%s: state file %s is corrupt; moving it aside and starting "
            "with empty state",
            label,
            path,
        )
        try:
            os.replace(path, path + ".corrupt")
        except OSError:
            logging.exception(
                "%s: unable to move aside corrupt state file %s", label, path
            )
        return None
    return parser


def write_state_file(path, parser, label):
    # Atomic replace, so a crash mid-write cannot leave a truncated state
    # file. Raises OSError on failure.
    state_dir = os.path.dirname(path) or "."
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=".%s-state-" % (label,), dir=state_dir, text=True
        )
        with os.fdopen(fd, "w") as f:
            parser.write(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except OSError:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def read_string_params(config):
    # Free-form string properties declared as params_<name> on a section.
    # Unlike toolchanger.get_params_dict these are plain strings, so config
    # values do not need Python-literal quoting (params_channel_type: sla,
    # not params_channel_type: 'sla').
    result = {}
    prefix = "params_"
    for option in config.get_prefix_options(prefix):
        result[option[len(prefix) :]] = config.get(option)
    if "color" in result:
        result["color"] = _normalize_color(result["color"])
        if result["color"] and not _HEX_COLOR_RE.match(result["color"]):
            raise config.error(
                "Option 'params_color' in section '%s' must be a hex code "
                "like 9e9e9e" % (config.get_name(),)
            )
    return result


class MaterialDefinition:
    def __init__(self, config, mid):
        self.id = mid
        self.display_name = config.get("display_name", mid)
        self.params = read_string_params(config)


class MaterialSlotGroup:
    def __init__(self, config, gid):
        self.id = gid
        self.display_name = config.get("display_name", gid)
        self.count = config.getint("count", minval=1)
        self.params = read_string_params(config)
        # Active slot until a usage command sets one.
        self.default_active_slot = config.getint(
            "active_slot", None, minval=1, maxval=self.count
        )
        # How a slot's uncovered (exposed) state is derived:
        #   off    (default) - not tracked.
        #   auto   - only the active slot is uncovered, since it became
        #            active. For vats whose cover is removed only when in use.
        #   always - every occupied slot is uncovered since it was inserted.
        #            For machines with no covers.
        self.uncovered_mode = config.get("uncovered_mode", "off")
        if self.uncovered_mode not in UNCOVERED_MODES:
            raise config.error(
                "Option 'uncovered_mode' in section '%s' must be one of: %s"
                % (config.get_name(), ", ".join(UNCOVERED_MODES))
            )


class Materials:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.state_file = os.path.expanduser(
            config.get(
                "state_file", "~/printer_data/config/materials_state.cfg"
            )
        )
        # Config-defined definitions and slot groups.
        self.config_materials = {}
        for c in config.get_prefix_sections("material "):
            mid = c.get_name().split(None, 1)[1]
            self.config_materials[mid] = MaterialDefinition(c, mid)
        self.groups = {}
        self.group_order = []
        for c in config.get_prefix_sections("material_slots "):
            gid = c.get_name().split(None, 1)[1]
            self.groups[gid] = MaterialSlotGroup(c, gid)
            self.group_order.append(gid)
        # Runtime, persisted state.
        self.user_materials = {}  # id -> {'display_name': str, 'params': dict}
        self.slots = {}  # (group_id, index) -> {field: value}
        self.active = {}  # group_id -> active slot index
        self.active_since = {}  # group_id -> unix time it became active
        try:
            self._load_state()
        except Exception as e:
            raise config.error("Unable to load materials state: %s" % (e,))
        # Give every occupied slot a date_inserted, which uncovered_mode
        # "always" needs. A saved record (e.g. a hand-edited file) may lack
        # one.
        patched_any = False
        for rec in self.slots.values():
            if (
                rec.get("material") is not None
                and rec.get("date_inserted") is None
            ):
                rec["date_inserted"] = time.time()
                patched_any = True
        # A saved active_slot without active_since (e.g. a hand-edited file)
        # would otherwise never get one, since _set_active only sets it when
        # the slot changes.
        for gid in self.active:
            if gid not in self.active_since:
                self.active_since[gid] = time.time()
                patched_any = True
        if patched_any:
            self._save_state()
        # Web API endpoints (Moonraker auto-exposes these to the browser as
        # printer.materials.<name>).
        webhooks = self.printer.lookup_object("webhooks")
        webhooks.register_endpoint("materials/set_slot", self._handle_set_slot)
        webhooks.register_endpoint(
            "materials/clear_slot", self._handle_clear_slot
        )
        webhooks.register_endpoint(
            "materials/set_definition", self._handle_set_definition
        )
        webhooks.register_endpoint(
            "materials/delete_definition", self._handle_delete_definition
        )
        webhooks.register_endpoint(
            "materials/set_uncovered_duration",
            self._handle_set_uncovered_duration,
        )
        # G-code commands for macro / print-time integration.
        gcode = self.printer.lookup_object("gcode")
        gcode.register_command(
            "MATERIAL_CONSUME",
            self.cmd_MATERIAL_CONSUME,
            desc=self.cmd_MATERIAL_CONSUME_help,
        )
        gcode.register_command(
            "MATERIAL_MARK_USED",
            self.cmd_MATERIAL_MARK_USED,
            desc=self.cmd_MATERIAL_MARK_USED_help,
        )

    def _load_state(self):
        parser = read_state_file(self.state_file, "materials")
        if parser is None:
            return
        for section in parser.sections():
            if section.startswith("definition "):
                mid = section.split(None, 1)[1]
                params = {}
                if parser.has_option(section, "params"):
                    try:
                        params = ast.literal_eval(parser.get(section, "params"))
                    except (ValueError, SyntaxError):
                        params = {}
                self.user_materials[mid] = {
                    "display_name": parser.get(
                        section, "display_name", fallback=mid
                    ),
                    "params": params if isinstance(params, dict) else {},
                }
            elif section.startswith("slot "):
                key = section.split(None, 1)[1]
                gid, _, idx_str = key.partition(":")
                try:
                    idx = int(idx_str)
                except ValueError:
                    continue
                rec = {}
                for field in SLOT_FIELDS:
                    if parser.has_option(section, field):
                        try:
                            rec[field] = ast.literal_eval(
                                parser.get(section, field)
                            )
                        except (ValueError, SyntaxError):
                            rec[field] = None
                self.slots[(gid, idx)] = rec
            elif section.startswith("group "):
                gid = section.split(None, 1)[1]
                if parser.has_option(section, "active_slot"):
                    try:
                        self.active[gid] = ast.literal_eval(
                            parser.get(section, "active_slot")
                        )
                    except (ValueError, SyntaxError):
                        pass
                if parser.has_option(section, "active_since"):
                    try:
                        self.active_since[gid] = ast.literal_eval(
                            parser.get(section, "active_since")
                        )
                    except (ValueError, SyntaxError):
                        pass

    def _save_state(self):
        parser = configparser.ConfigParser(interpolation=None)
        for mid, d in self.user_materials.items():
            section = "definition " + mid
            parser.add_section(section)
            parser.set(section, "display_name", str(d["display_name"]))
            parser.set(section, "params", repr(d.get("params", {})))
        for (gid, idx), rec in self.slots.items():
            section = "slot %s:%d" % (gid, idx)
            parser.add_section(section)
            for field in SLOT_FIELDS:
                parser.set(section, field, repr(rec.get(field)))
        for gid in set(self.active) | set(self.active_since):
            section = "group %s" % (gid,)
            parser.add_section(section)
            if gid in self.active:
                parser.set(section, "active_slot", repr(self.active[gid]))
            if gid in self.active_since:
                parser.set(
                    section, "active_since", repr(self.active_since[gid])
                )
        try:
            write_state_file(self.state_file, parser, "materials")
        except OSError as e:
            logging.exception("materials: unable to save state file")
            raise self.printer.command_error(
                "Unable to save materials state: %s" % (e,)
            )

    @staticmethod
    def _unit_for(params):
        # Filament is measured in grams, fluids (resin/bioink) in mL.
        # An explicit params_unit always wins.
        if params.get("unit"):
            return params["unit"]
        return "g" if params.get("channel_type") == "fff" else "mL"

    def _effective_materials(self):
        result = {}
        for mid, d in self.config_materials.items():
            entry = dict(d.params)
            entry["display_name"] = d.display_name
            entry["source"] = "config"
            entry["unit"] = self._unit_for(entry)
            result[mid] = entry
        for mid, d in self.user_materials.items():
            entry = dict(d.get("params", {}))
            entry["display_name"] = d["display_name"]
            # config-backed definitions can be overridden but not deleted.
            entry["source"] = (
                "config" if mid in self.config_materials else "user"
            )
            entry["unit"] = self._unit_for(entry)
            result[mid] = entry
        return result

    def _active_slot(self, gid):
        return self.active.get(gid, self.groups[gid].default_active_slot)

    def _set_active(self, gid, idx):
        # Only reset active_since when the slot actually changes.
        if self.active.get(gid) != idx:
            self.active_since[gid] = time.time()
        self.active[gid] = idx

    def _derive_uncovered_since(self, group, idx, material):
        # See uncovered_mode on MaterialSlotGroup.
        if material is None:
            return None
        if group.uncovered_mode == "always":
            return self.slots.get((group.id, idx), {}).get("date_inserted")
        if (
            group.uncovered_mode == "auto"
            and self._active_slot(group.id) == idx
        ):
            return self.active_since.get(group.id)
        return None

    def _empty_slot(self, idx):
        return {
            "index": idx,
            "material": None,
            "amount": None,
            "container_height": None,
            "comments": "",
            "color": None,
            "date_inserted": None,
            "last_used": None,
        }

    def _slot_view(self, gid, idx):
        group = self.groups[gid]
        rec = self.slots.get((gid, idx))
        if rec is None:
            view = self._empty_slot(idx)
        else:
            view = {"index": idx}
            for field in SLOT_FIELDS:
                view[field] = rec.get(field)
        view["uncovered_since"] = self._derive_uncovered_since(
            group, idx, view["material"]
        )
        return view

    def get_status(self, eventtime):
        groups = {}
        for gid in self.group_order:
            g = self.groups[gid]
            groups[gid] = {
                "display_name": g.display_name,
                "count": g.count,
                "params": g.params,
                "uncovered_mode": g.uncovered_mode,
                "active_slot": self._active_slot(gid),
                "slots": [
                    self._slot_view(gid, i) for i in range(1, g.count + 1)
                ],
            }
        return {"materials": self._effective_materials(), "slot_groups": groups}

    def resolve_material(self, key):
        # key is a material id or its params_slicer_token (the string the
        # slicer emits). Returns the material id, or None if nothing matches.
        materials = self._effective_materials()
        if key in materials:
            return key
        for mid, entry in materials.items():
            if entry.get("slicer_token") == key:
                return mid
        return None

    def get_material(self, mid):
        # The material's effective properties (see get_status), or None.
        return self._effective_materials().get(mid)

    def has_group(self, gid):
        return gid in self.groups

    def group_slot_materials(self, gid):
        # The material id in each slot of a group, in slot order (None for an
        # empty slot).
        return [
            self._slot_view(gid, idx)["material"]
            for idx in range(1, self.groups[gid].count + 1)
        ]

    def group_materials(self, gid):
        # Ids of the materials currently loaded in a group's slots, without
        # duplicates.
        result = []
        for mid in self.group_slot_materials(gid):
            if mid is not None and mid not in result:
                result.append(mid)
        return result

    def get_active_slot(self, gid):
        return self._active_slot(gid)

    def set_active_slot(self, gid, idx):
        # Record that slot idx of the group is now the one in use (for a
        # selector such as a vat turntable, the one rotated into place).
        self._require_slot(gid, idx)
        self._set_active(gid, idx)
        self._save_state()

    def _require_slot(self, gid, idx):
        if gid not in self.groups:
            raise self.printer.command_error(
                "Unknown material group '%s'" % (gid,)
            )
        group = self.groups[gid]
        if idx < 1 or idx > group.count:
            raise self.printer.command_error(
                "Slot %s out of range for group '%s' (1-%d)"
                % (idx, gid, group.count)
            )
        return group

    def _current_rec(self, gid, idx):
        # A mutable copy of the slot's state, keyed by the persisted field
        # names.
        view = self._slot_view(gid, idx)
        return {field: view.get(field) for field in SLOT_FIELDS}

    def _opt_float(self, params, name, minval=None):
        value = params.get(name)
        if value in (None, ""):
            return None
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise self.printer.command_error(
                "Argument '%s' must be a number" % (name,)
            )
        if not math.isfinite(value):
            raise self.printer.command_error(
                "Argument '%s' must be a finite number" % (name,)
            )
        if minval is not None and value < minval:
            raise self.printer.command_error(
                "Argument '%s' must be at least %s" % (name, minval)
            )
        return value

    def _handle_set_slot(self, web_request):
        gid = web_request.get_str("group")
        idx = web_request.get_int("slot")
        self._require_slot(gid, idx)
        rec = self._current_rec(gid, idx)
        params = web_request.params
        if "material" in params:
            mid = params["material"]
            if mid in (None, ""):
                rec = {field: None for field in SLOT_FIELDS}
                rec["comments"] = ""
                self.slots[(gid, idx)] = rec
                self._save_state()
                web_request.send(self._slot_view(gid, idx))
                return
            else:
                mid = str(mid)
                if mid not in self._effective_materials():
                    raise self.printer.command_error(
                        "Unknown material '%s'" % (mid,)
                    )
                changed = rec.get("material") != mid
                # Also stamp a slot whose date was cleared, when it is
                # re-assigned the same material.
                if changed or rec.get("date_inserted") is None:
                    rec["date_inserted"] = time.time()
                # A new material drops the old color override. A color in
                # this same request is applied below.
                if changed:
                    rec["color"] = None
                rec["material"] = mid
        if "amount" in params:
            rec["amount"] = self._opt_float(params, "amount", minval=0.0)
        if "container_height" in params:
            rec["container_height"] = self._opt_float(
                params, "container_height", minval=0.0
            )
        if "comments" in params:
            rec["comments"] = str(params["comments"])
        if "color" in params:
            color = params["color"]
            if color in (None, ""):
                rec["color"] = None
            else:
                color = str(color)
                if not _HEX_COLOR_RE.match(color):
                    raise self.printer.command_error(
                        "'color' must be a hex code like #a1b2c3"
                    )
                rec["color"] = color
        if "date_inserted" in params:
            rec["date_inserted"] = self._opt_float(
                params, "date_inserted", minval=0.0
            )
        self.slots[(gid, idx)] = rec
        self._save_state()
        web_request.send(self._slot_view(gid, idx))

    def _handle_clear_slot(self, web_request):
        gid = web_request.get_str("group")
        idx = web_request.get_int("slot")
        self._require_slot(gid, idx)
        rec = {field: None for field in SLOT_FIELDS}
        rec["comments"] = ""
        self.slots[(gid, idx)] = rec
        self._save_state()
        web_request.send(self._slot_view(gid, idx))

    def _handle_set_uncovered_duration(self, web_request):
        # Corrects the "uncovered for" time of an "auto" group's active
        # slot. For "always" groups, edit date_inserted instead.
        gid = web_request.get_str("group")
        idx = web_request.get_int("slot")
        group = self._require_slot(gid, idx)
        if group.uncovered_mode != "auto":
            raise self.printer.command_error(
                "Group '%s' has uncovered_mode '%s'; only 'auto' groups "
                "support editing uncovered duration directly"
                % (gid, group.uncovered_mode)
            )
        if self._active_slot(gid) != idx:
            raise self.printer.command_error(
                "Slot %d is not the active (uncovered) slot for group '%s'"
                % (idx, gid)
            )
        seconds = web_request.get_float("seconds")
        if not math.isfinite(seconds) or seconds < 0:
            raise self.printer.command_error(
                "'seconds' must be a finite, non-negative number"
            )
        self.active_since[gid] = time.time() - seconds
        self._save_state()
        web_request.send(self._slot_view(gid, idx))

    def _handle_set_definition(self, web_request):
        mid = web_request.get_str("id").strip()
        if not mid:
            raise self.printer.command_error("Material id must not be empty")
        if _CONTROL_CHAR_RE.search(mid):
            raise self.printer.command_error(
                "Material id must not contain control characters"
            )
        display_name = web_request.get_str("display_name", mid)
        params = web_request.get("params", {})
        if not isinstance(params, dict):
            raise self.printer.command_error("'params' must be an object")
        clean = {
            str(k): ("" if v is None else str(v)) for k, v in params.items()
        }
        if "color" in clean:
            clean["color"] = _normalize_color(clean["color"])
            if clean["color"] and not _HEX_COLOR_RE.match(clean["color"]):
                raise self.printer.command_error(
                    "'params.color' must be a hex code like #a1b2c3"
                )
        self.user_materials[mid] = {
            "display_name": display_name,
            "params": clean,
        }
        self._save_state()
        web_request.send(
            {"id": mid, "material": self._effective_materials()[mid]}
        )

    def _handle_delete_definition(self, web_request):
        mid = web_request.get_str("id")
        refs = [
            "%s:%d" % (gid, i)
            for gid in self.group_order
            for i in range(1, self.groups[gid].count + 1)
            if self._slot_view(gid, i).get("material") == mid
        ]
        if refs:
            raise self.printer.command_error(
                "Material '%s' is in use by slot(s): %s"
                % (mid, ", ".join(refs))
            )
        if mid in self.user_materials:
            del self.user_materials[mid]
            self._save_state()
        elif mid in self.config_materials:
            raise self.printer.command_error(
                "Material '%s' is defined in the config file; edit the config "
                "to remove it" % (mid,)
            )
        else:
            raise self.printer.command_error("Unknown material '%s'" % (mid,))
        web_request.send({"deleted": mid})

    cmd_MATERIAL_CONSUME_help = (
        "Subtract an amount from a material slot (GROUP= SLOT= AMOUNT=)"
    )

    def cmd_MATERIAL_CONSUME(self, gcmd):
        gid = gcmd.get("GROUP")
        idx = gcmd.get_int("SLOT")
        amount = gcmd.get_float("AMOUNT", minval=0.0)
        self._require_slot(gid, idx)
        rec = self._current_rec(gid, idx)
        if rec.get("material") is None:
            raise gcmd.error("Cannot consume from an empty material slot")
        if rec.get("amount") is None:
            raise gcmd.error(
                "Cannot consume material until the slot amount is set"
            )
        rec["amount"] = max(0.0, rec["amount"] - amount)
        rec["last_used"] = time.time()
        self.slots[(gid, idx)] = rec
        self._set_active(gid, idx)
        self._save_state()

    cmd_MATERIAL_MARK_USED_help = (
        "Mark a material slot as used now (GROUP= SLOT= [AMOUNT=])"
    )

    def cmd_MATERIAL_MARK_USED(self, gcmd):
        gid = gcmd.get("GROUP")
        idx = gcmd.get_int("SLOT")
        amount = gcmd.get_float("AMOUNT", 0.0, minval=0.0)
        self._require_slot(gid, idx)
        rec = self._current_rec(gid, idx)
        if rec.get("material") is None:
            raise gcmd.error("Cannot mark an empty material slot as used")
        if amount:
            if rec.get("amount") is None:
                raise gcmd.error(
                    "Cannot consume material until the slot amount is set"
                )
            rec["amount"] = max(0.0, rec["amount"] - amount)
        rec["last_used"] = time.time()
        self.slots[(gid, idx)] = rec
        self._set_active(gid, idx)
        self._save_state()


def load_config(config):
    return Materials(config)
