# Toolhead registry for a multi-toolhead bioprinter.
#
# Tracks where every physical toolhead is (disconnected, unmounted, docked on
# one of n docks, or mounted on the carriage), which materials each toolhead
# owns, and gets the right material into place when a print asks for it:
#
#  - a material held by a toolhead is switched by exchanging toolheads,
#    automatically when the wanted head is docked and has motion macros, or
#    by the user when it is not (the printer does not move while it waits);
#  - a material held by a selector group (for example a vat turntable) is
#    switched by running that group's select_gcode macro.
#
# The registry does not move a toolhead itself. Swaps are performed by the
# vendored toolchanger engine (toolchanger.py / tool.py) using the
# pickup_gcode and dropoff_gcode of each head's [tool] section. The dock of
# the head being picked up or dropped off is published to those macros as the
# tool parameter "dock" (tool.dock in Jinja, 0 when unknown).
#
# A toolhead that is too fragile to touch an endstop sets homeable: False.
# While it is mounted G28 never moves anything, and the axes only become
# usable once the user has put the head where they want the origin and run
# TOOLHEAD_SET_ORIGIN. Until then the axes are homed at the middle of their
# travel so the head can be jogged either way, but G28 and prints refuse to go
# on. The origin is a g-code offset, so the travel stays where it is. Mounting or removing
# such a head starts over, so the origin of one head is never mistaken for
# another's.
#
# A toolhead may follow a non-critical MCU ([mcu x] is_non_critical: True):
# unplugging the board marks the head disconnected, and plugging it back in
# restores the state it had.
#
# Copyright (C) 2026  Nicholas Karr
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import configparser
import logging
import os
import time

from . import materials as materials_mod
from . import toolchanger as toolchanger_mod

STATE_DISCONNECTED = "disconnected"
STATE_UNMOUNTED = "unmounted"
STATE_DOCKED = "docked"
STATE_MOUNTED = "mounted"
STATES = (STATE_DISCONNECTED, STATE_UNMOUNTED, STATE_DOCKED, STATE_MOUNTED)

# Where the head that was removed by hand can be reported to have gone.
REMOVED_STATES = (STATE_UNMOUNTED, STATE_DISCONNECTED, STATE_DOCKED)

WAIT_POLL_SECONDS = 0.2


class ToolheadUnit:
    def __init__(self, config, name):
        self.name = name
        self.display_name = config.get("display_name", name)
        # Name of the [tool <name>] section that supplies the motion macros.
        # Without it the head can only be mounted by hand.
        self.tool_name = config.get("tool", None)
        # The [material_slots <group>] this head carries; the materials in
        # those slots are the ones the head owns.
        self.material_group = config.get("material_group", None)
        # Optional [mcu <name>] (is_non_critical: True) whose presence tells
        # whether the head is connected.
        self.mcu_name = config.get("mcu", None)
        states = {s: s for s in STATES}
        self.initial_state = config.getchoice(
            "initial_state", states, STATE_UNMOUNTED
        )
        self.initial_dock = config.getint("initial_dock", None, minval=1)
        # False for a head that must never be homed.
        self.homeable = config.getboolean("homeable", True)
        self.state = self.initial_state
        self.dock = self.initial_dock
        # What to go back to when the head's MCU reconnects.
        self.resume_state = None
        self.resume_dock = None
        self.tool = None  # resolved on klippy:connect
        self.mcu = None  # resolved on klippy:ready

    def can_swap(self):
        return self.tool is not None

    def mcu_connected(self):
        return self.mcu is None or not self.mcu.non_critical_disconnected


class MaterialSelector:
    def __init__(self, config, name, gcode_macro):
        self.name = name
        self.material_group = config.get("material_group")
        self.select_gcode = gcode_macro.load_template(config, "select_gcode")


class Toolheads:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.state_file = os.path.expanduser(
            config.get(
                "state_file", "~/printer_data/config/toolheads_state.cfg"
            )
        )
        self.dock_count = config.getint("dock_count", 0, minval=0)
        self.heads = {}
        for c in config.get_prefix_sections("toolhead_unit "):
            name = c.get_name().split(None, 1)[1]
            self.heads[name] = ToolheadUnit(c, name)
        self.selectors = {}
        selector_sections = config.get_prefix_sections("material_selector ")
        if selector_sections:
            gcode_macro = self.printer.load_object(config, "gcode_macro")
            for c in selector_sections:
                name = c.get_name().split(None, 1)[1]
                self.selectors[name] = MaterialSelector(c, name, gcode_macro)
        self.materials = None
        self.toolchanger = None
        self.force_move = None
        self.prev_G28 = None
        # True once the user has set the origin of the mounted head, which
        # only matters for a head that cannot be homed.
        self.origin_set = False
        if not all(h.homeable for h in self.heads.values()):
            self.force_move = self.printer.load_object(config, "force_move")
        self._validate(config)
        self._load_state()
        self.busy = False
        self.awaiting = None
        self._await_result = None
        self._await_removed = None
        self.last_error = ""
        self._current_gcmd = None  # the command a swap or prompt belongs to
        self.printer.register_event_handler(
            "klippy:connect", self._handle_connect
        )
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        if self.force_move is not None:
            self.printer.register_event_handler(
                "stepper_enable:motor_off", self._handle_motor_off
            )
        for head in self.heads.values():
            if head.mcu_name:
                self._follow_mcu(head)
        self._register_endpoints()
        self._register_commands()

    def _validate(self, config):
        groups = {}
        for head in self.heads.values():
            if head.material_group:
                groups.setdefault(head.material_group, []).append(
                    "toolhead_unit " + head.name
                )
        for sel in self.selectors.values():
            groups.setdefault(sel.material_group, []).append(
                "material_selector " + sel.name
            )
        if groups:
            self.materials = self.printer.load_object(config, "materials", None)
            if self.materials is None:
                raise config.error(
                    "[toolheads] material_group needs a [materials] section"
                )
        for gid, owners in groups.items():
            if not self.materials.has_group(gid):
                raise config.error(
                    "Unknown material_group '%s' in [%s]" % (gid, owners[0])
                )
            if len(owners) > 1:
                raise config.error(
                    "material_group '%s' is claimed by more than one of: %s"
                    % (gid, ", ".join("[%s]" % (o,) for o in owners))
                )
        if any(h.tool_name for h in self.heads.values()):
            self.toolchanger = self.printer.load_object(config, "toolchanger")
        initial_docks = {}
        for head in self.heads.values():
            if head.initial_dock is None:
                continue
            if head.initial_dock in initial_docks:
                raise config.error(
                    "Toolheads '%s' and '%s' both start in dock %d"
                    % (
                        initial_docks[head.initial_dock],
                        head.name,
                        head.initial_dock,
                    )
                )
            initial_docks[head.initial_dock] = head.name
        for head in self.heads.values():
            if head.tool_name and not config.has_section(
                "tool " + head.tool_name
            ):
                raise config.error(
                    "Toolhead '%s': no [tool %s] section"
                    % (head.name, head.tool_name)
                )
            if head.mcu_name:
                section = "mcu " + head.mcu_name
                if not config.has_section(section):
                    raise config.error(
                        "Toolhead '%s': no [%s] section" % (head.name, section)
                    )
                if not config.getsection(section).getboolean(
                    "is_non_critical", False
                ):
                    raise config.error(
                        "Toolhead '%s': [%s] must set is_non_critical: True "
                        "to be followed" % (head.name, section)
                    )
            if head.initial_state == STATE_DOCKED and (
                head.initial_dock is None or head.initial_dock > self.dock_count
            ):
                raise config.error(
                    "Toolhead '%s': initial_state docked needs initial_dock "
                    "between 1 and dock_count (%d)"
                    % (head.name, self.dock_count)
                )
            if head.initial_dock is not None and head.initial_state not in (
                STATE_DOCKED,
                STATE_MOUNTED,
            ):
                raise config.error(
                    "Toolhead '%s': initial_dock only applies to docked or "
                    "mounted heads" % (head.name,)
                )

    def _handle_connect(self):
        for head in self.heads.values():
            if head.tool_name:
                head.tool = self.printer.lookup_object("tool " + head.tool_name)
        if self.force_move is not None:
            # Wrap G28 last, so this guard runs before homing_override and
            # friends have moved anything.
            self.prev_G28 = self.gcode.register_command("G28", None)
            if self.prev_G28 is not None:
                self.gcode.register_command(
                    "G28", self.cmd_G28, desc=self.cmd_G28_help
                )

    def _handle_ready(self):
        head = self.mounted_head()
        if head is not None and not head.homeable:
            self._provisional_origin()
        # Reconcile with the boards that are actually attached now.
        for head in self.heads.values():
            if not head.mcu_name:
                continue
            head.mcu = self.printer.lookup_object("mcu " + head.mcu_name)
            if head.mcu_connected():
                self._mcu_reconnected(head)
            else:
                self._mcu_disconnected(head)

    def _follow_mcu(self, head):
        prefix = "danger:non_critical_mcu_%s:" % (head.mcu_name,)
        self.printer.register_event_handler(
            prefix + "disconnected", lambda h=head: self._mcu_disconnected(h)
        )
        self.printer.register_event_handler(
            prefix + "reconnected", lambda h=head: self._mcu_reconnected(h)
        )

    def _mcu_disconnected(self, head):
        if head.state == STATE_DISCONNECTED:
            return
        head.resume_state = head.state
        head.resume_dock = head.dock
        prev = self.mounted_head()
        head.state = STATE_DISCONNECTED
        head.dock = None
        self._save_quietly()
        self._mounted_changed(prev)

    def _mcu_reconnected(self, head):
        if head.state != STATE_DISCONNECTED:
            return
        prev = self.mounted_head()
        state, dock = head.resume_state, head.resume_dock
        head.resume_state = head.resume_dock = None
        if state == STATE_MOUNTED:
            other = self.mounted_head()
            if other is not None and other is not head:
                state = STATE_UNMOUNTED
        if state in (STATE_DOCKED, STATE_MOUNTED) and dock is not None:
            if (
                dock < 1
                or dock > self.dock_count
                or self._dock_owner(dock, ignore=head) is not None
            ):
                dock = None
        if state == STATE_DOCKED and dock is None:
            state = STATE_UNMOUNTED
        head.state = state if state in STATES else STATE_UNMOUNTED
        head.dock = (
            dock if head.state in (STATE_DOCKED, STATE_MOUNTED) else None
        )
        self._save_quietly()
        self._mounted_changed(prev)

    def _save_quietly(self):
        # Event handlers have nobody to report a failure to.
        try:
            self._save_state()
        except self.printer.command_error:
            logging.exception("toolheads: unable to save state")

    def _load_state(self):
        # Start from config, then let the persisted record win: the state
        # file is what survives a restart and web edits.
        parser = materials_mod.read_state_file(self.state_file, "toolheads")
        if parser is not None:
            for section in parser.sections():
                if not section.startswith("head "):
                    continue
                head = self.heads.get(section.split(None, 1)[1])
                if head is None:
                    continue
                state = parser.get(section, "state", fallback=None)
                if state not in STATES:
                    continue
                head.state = state
                head.dock = self._read_int(parser, section, "dock")
                resume = parser.get(section, "resume_state", fallback=None)
                if resume in STATES:
                    head.resume_state = resume
                    head.resume_dock = self._read_int(
                        parser, section, "resume_dock"
                    )
        self._repair()

    @staticmethod
    def _read_int(parser, section, option):
        try:
            return parser.getint(section, option, fallback=None)
        except ValueError:
            return None

    def _repair(self):
        # Restore the invariants after loading, so a hand-edited state file or
        # a shrunken dock_count cannot leave two heads in one dock, two
        # heads on the carriage, or a dock that no longer exists.
        seen_docks = set()
        mounted_seen = False
        for head in self.heads.values():
            if head.state in (STATE_DISCONNECTED, STATE_UNMOUNTED):
                head.dock = None
            if head.dock is not None and (
                head.dock < 1
                or head.dock > self.dock_count
                or head.dock in seen_docks
            ):
                logging.warning(
                    "toolheads: dropping invalid dock %s of %s",
                    head.dock,
                    head.name,
                )
                head.dock = None
            if head.state == STATE_DOCKED and head.dock is None:
                head.state = STATE_UNMOUNTED
            if head.state == STATE_MOUNTED:
                if mounted_seen:
                    logging.warning(
                        "toolheads: %s also marked mounted; marking it "
                        "unmounted",
                        head.name,
                    )
                    head.state = STATE_UNMOUNTED
                    head.dock = None
                mounted_seen = True
            if head.dock is not None:
                seen_docks.add(head.dock)

    def _save_state(self):
        parser = configparser.ConfigParser(interpolation=None)
        for head in self.heads.values():
            section = "head " + head.name
            parser.add_section(section)
            parser.set(section, "state", head.state)
            if head.dock is not None:
                parser.set(section, "dock", str(head.dock))
            if head.resume_state is not None:
                parser.set(section, "resume_state", head.resume_state)
                if head.resume_dock is not None:
                    parser.set(section, "resume_dock", str(head.resume_dock))
        try:
            materials_mod.write_state_file(self.state_file, parser, "toolheads")
        except OSError as e:
            logging.exception("toolheads: unable to save state file")
            raise self.printer.command_error(
                "Unable to save toolheads state: %s" % (e,)
            )

    def mounted_head(self):
        for head in self.heads.values():
            if head.state == STATE_MOUNTED:
                return head
        return None

    def _dock_owner(self, dock, ignore=None):
        for head in self.heads.values():
            if head is not ignore and head.dock == dock:
                return head
        return None

    def _free_dock(self):
        for dock in range(1, self.dock_count + 1):
            if self._dock_owner(dock) is None:
                return dock
        return None

    def _check_dock(self, head, dock):
        if dock < 1 or dock > self.dock_count:
            raise self.printer.command_error(
                "Dock %d does not exist (%d configured)"
                % (dock, self.dock_count)
            )
        owner = self._dock_owner(dock, ignore=head)
        if owner is not None:
            raise self.printer.command_error(
                "Dock %d is occupied by %s" % (dock, owner.display_name)
            )

    def _lookup(self, name):
        head = self.heads.get(name)
        if head is None:
            raise self.printer.command_error("Unknown toolhead '%s'" % (name,))
        return head

    def _require_editable(self):
        if self.busy or self.awaiting is not None:
            raise self.printer.command_error("A toolhead change is in progress")
        vsd = self.printer.lookup_object("virtual_sdcard", None)
        if vsd is not None and vsd.is_active():
            raise self.printer.command_error(
                "Toolheads cannot be edited while a print is running"
            )

    def _check_mcu_state(self, head, state):
        # A head that follows an MCU is disconnected exactly when the board is.
        if head.mcu_name is None:
            return
        if state == STATE_DISCONNECTED:
            raise self.printer.command_error(
                "%s follows mcu '%s' and is marked disconnected by unplugging "
                "it" % (head.display_name, head.mcu_name)
            )
        if not head.mcu_connected():
            raise self.printer.command_error(
                "%s cannot be placed while mcu '%s' is disconnected"
                % (head.display_name, head.mcu_name)
            )

    def set_state(self, head, state, dock=None):
        # Manual state change: the user says where the head physically is.
        # Validates everything before changing anything.
        if state not in STATES:
            raise self.printer.command_error(
                "State must be one of: %s" % (", ".join(STATES),)
            )
        self._check_mcu_state(head, state)
        if state in (STATE_DISCONNECTED, STATE_UNMOUNTED):
            if dock is not None:
                raise self.printer.command_error(
                    "A %s toolhead has no dock" % (state,)
                )
        elif state == STATE_DOCKED:
            if dock is None:
                dock = head.dock if head.dock is not None else self._free_dock()
            if dock is None:
                raise self.printer.command_error("No free dock")
            self._check_dock(head, dock)
        else:  # mounted: keeps (or is given) the dock it returns to
            if dock is None:
                dock = head.dock
            else:
                self._check_dock(head, dock)
        prev = self.mounted_head()
        if state == STATE_MOUNTED:
            other = self.mounted_head()
            if other is not None and other is not head:
                # The user has taken it off by hand; it now sits on the side.
                other.state = STATE_UNMOUNTED
                other.dock = None
        head.state = state
        head.dock = dock
        head.resume_state = head.resume_dock = None
        self._save_state()
        self._mounted_changed(prev)

    def _mounted_changed(self, prev):
        # The homed axes belong to the head they were found with.
        now = self.mounted_head()
        if now is prev:
            return
        self.origin_set = False
        if now is not None and not now.homeable:
            self._provisional_origin()
        elif prev is not None and not prev.homeable:
            toolhead = self.printer.lookup_object("toolhead", None)
            if toolhead is not None:
                toolhead.get_kinematics().clear_homing_state([0, 1, 2])

    def _set_gcode_offset(self, offsets):
        params = {a: "%.6f" % (v,) for a, v in zip("XYZ", offsets)}
        self.printer.lookup_object("gcode_move").cmd_SET_GCODE_OFFSET(
            self.gcode.create_gcode_command(
                "SET_GCODE_OFFSET", "SET_GCODE_OFFSET", params
            )
        )

    def _provisional_origin(self):
        # A head that cannot be homed has no known place, so the axes are
        # declared homed in the middle of their travel. That keeps room to
        # jog either way from wherever the head is, which is how the user
        # gets it to the place to set the origin.
        self.origin_set = False
        toolhead = self.printer.lookup_object("toolhead")
        status = toolhead.get_status(self.reactor.monotonic())
        mid = []
        for lo, hi in zip(
            status["axis_minimum"][:3], status["axis_maximum"][:3]
        ):
            mid.append((lo + hi) / 2.0 if hi - lo < 10000.0 else 0.0)
        self.force_move.cmd_SET_KINEMATIC_POSITION(
            self.gcode.create_gcode_command(
                "SET_KINEMATIC_POSITION",
                "SET_KINEMATIC_POSITION",
                {a: "%.6f" % (v,) for a, v in zip("XYZ", mid)},
            )
        )
        self._set_gcode_offset((0.0, 0.0, 0.0))

    def _handle_motor_off(self, print_time):
        # Klipper forgets the homed axes now; the head has been let go of.
        def restart_origin(eventtime):
            head = self.mounted_head()
            if head is not None and not head.homeable:
                self._provisional_origin()

        self.reactor.register_callback(restart_origin)

    def reorder_docks(self, order):
        # order lists the occupant of dock 1..n (a head name, or None for an
        # empty dock). It may only permute the heads already holding a dock.
        if len(order) != self.dock_count:
            raise self.printer.command_error(
                "Order needs %d entries, one per dock" % (self.dock_count,)
            )
        if any(n is not None and not isinstance(n, str) for n in order):
            raise self.printer.command_error(
                "Order entries must be toolhead names or null"
            )
        current = {h.name for h in self.heads.values() if h.dock is not None}
        wanted = [n for n in order if n]
        if len(set(wanted)) != len(wanted) or set(wanted) != current:
            raise self.printer.command_error(
                "Order must list exactly the toolheads that hold a dock: %s"
                % (", ".join(sorted(current)) or "none",)
            )
        for head in self.heads.values():
            head.dock = None
        for dock, name in enumerate(order, start=1):
            if name:
                self._lookup(name).dock = dock
        self._save_state()

    def owned_materials(self, head):
        if head.material_group is None:
            return []
        return self.materials.group_materials(head.material_group)

    def _resolve_material(self, key):
        if self.materials is None:
            raise self.printer.command_error(
                "Material tracking is not configured ([materials] is missing)"
            )
        mid = self.materials.resolve_material(key)
        if mid is None:
            raise self.printer.command_error("Unknown material '%s'" % (key,))
        return mid

    def _sources(self, mid):
        # Every place the material can be had: (rank, kind, holder, slot).
        # A lower rank is easier to reach: already in place, then a selector,
        # then heads from mounted to disconnected.
        found = []
        for head in self.heads.values():
            if mid not in self.owned_materials(head):
                continue
            if head.state == STATE_MOUNTED:
                rank = (0, 0)
            elif head.state == STATE_DOCKED and head.can_swap():
                rank = (2, head.dock)
            elif head.state == STATE_DOCKED:
                rank = (3, head.dock)
            elif head.state == STATE_UNMOUNTED:
                rank = (4, 0)
            else:
                rank = (5, 0)
            found.append((rank, "head", head, None))
        for sel in self.selectors.values():
            active = self.materials.get_active_slot(sel.material_group)
            slots = self.materials.group_slot_materials(sel.material_group)
            for idx, slot_mid in enumerate(slots, start=1):
                if slot_mid == mid:
                    rank = (0, 0) if idx == active else (1, idx)
                    found.append((rank, "selector", sel, idx))
        return found

    def _best_source(self, mid):
        found = self._sources(mid)
        if not found:
            raise self.printer.command_error(
                "No toolhead or selector holds material '%s'" % (mid,)
            )
        return min(found, key=lambda s: s[0])

    def _sync_engine(self, wanted):
        # The engine forgets its active tool on any g-code error, so bring it
        # back in line with the registry before every swap.
        tc = self.toolchanger
        if tc is None:
            return
        if tc.status == toolchanger_mod.STATUS_ERROR:
            raise self.printer.command_error(
                "The toolchanger is in an error state; fix the cause and run "
                "INITIALIZE_TOOLCHANGER before switching toolheads"
            )
        want_tool = wanted.tool if wanted else None
        if tc.status == toolchanger_mod.STATUS_UNINITALIZED:
            tc.initialize(want_tool)
        if tc.active_tool is not want_tool:
            tc.set_active_tool(want_tool)

    def _publish_dock(self, head, dock):
        if head is not None and head.tool is not None:
            head.tool.set_parameter("dock", dock if dock is not None else 0)

    def _engine_select(self, tool):
        tc = self.toolchanger
        self.busy = True
        try:
            tc.select_tool(self._current_gcmd, tool, self._restore_axis(tool))
        except Exception as e:
            self.last_error = str(e)
            raise
        finally:
            self.busy = False
        # select_tool swallows errors it already handled through error_gcode,
        # so success is checked against the engine's own state.
        if (
            tc.active_tool is not tool
            or tc.status != toolchanger_mod.STATUS_READY
        ):
            self.last_error = "The toolchanger did not complete the change"
            raise self.printer.command_error(
                "Toolhead change failed (toolchanger status: %s)" % (tc.status,)
            )

    @staticmethod
    def _restore_axis(tool):
        return tool.t_command_restore_axis if tool is not None else "XYZ"

    def _dock_for_dropoff(self, head):
        dock = head.dock if head.dock is not None else self._free_dock()
        if dock is None:
            raise self.printer.command_error(
                "No free dock for %s; free a dock or remove it by hand"
                % (head.display_name,)
            )
        return dock

    def _swap_to(self, head):
        # Automatic exchange from whatever is mounted to a docked head.
        mounted = self.mounted_head()
        prev_dock = None
        if mounted is not None:
            prev_dock = self._dock_for_dropoff(mounted)
            self._publish_dock(mounted, prev_dock)
        self._publish_dock(head, head.dock)
        self._sync_engine(mounted)
        self._engine_select(head.tool)
        if mounted is not None:
            mounted.state = STATE_DOCKED
            mounted.dock = prev_dock
        head.state = STATE_MOUNTED
        self._save_state()
        self._mounted_changed(mounted)
        self.last_error = ""
        self._current_gcmd.respond_info(
            "Toolhead %s mounted" % (head.display_name,)
        )

    def _ensure_head(self, head, material=None):
        # Get head onto the carriage, automatically when possible and with the
        # user's help otherwise.
        if head.state == STATE_MOUNTED:
            return
        mounted = self.mounted_head()
        can_exchange = mounted is None or mounted.can_swap()
        if head.state == STATE_DOCKED and head.can_swap() and can_exchange:
            self._swap_to(head)
            return
        self._mount_by_hand(head, material)

    def _validate_removed(self, remove, state, dock):
        if remove is None:
            return
        if state not in REMOVED_STATES:
            raise self.printer.command_error(
                "The removed toolhead can be: %s" % (", ".join(REMOVED_STATES),)
            )
        if state == STATE_DOCKED:
            if dock is None:
                raise self.printer.command_error("Choose a dock")
            self._check_dock(remove, dock)
        elif dock is not None:
            raise self.printer.command_error(
                "Only a docked toolhead has a dock"
            )

    def _mount_by_hand(self, head, material):
        # Nothing moves while this waits: the user does the work and the
        # registry trusts them that the right head ended up on the carriage.
        if self.awaiting is not None or self.busy:
            raise self.printer.command_error(
                "A toolhead change is already in progress"
            )
        gcmd = self._current_gcmd
        mounted = self.mounted_head()
        remove = mounted if mounted is not head else None
        self.awaiting = {
            "toolhead": head.name,
            "material": material,
            "reason": head.state,
            "remove": remove.name if remove is not None else None,
            "since": time.time(),
        }
        self._await_result = None
        self._await_removed = (STATE_UNMOUNTED, None)
        gcmd.respond_info(
            "Waiting for %s to be mounted by hand%s"
            % (
                head.display_name,
                " (remove %s first)" % (remove.display_name,)
                if remove is not None
                else "",
            )
        )
        try:
            while self._await_result is None:
                self.reactor.pause(self.reactor.monotonic() + WAIT_POLL_SECONDS)
                if self.printer.is_shutdown():
                    raise self.printer.command_error(
                        "Printer shut down while waiting for a toolhead"
                    )
            result = self._await_result
            removed_state, removed_dock = self._await_removed
        finally:
            self.awaiting = None
            self._await_result = None
        if result != "confirmed":
            raise self.printer.command_error(
                "Mounting %s was cancelled" % (head.display_name,)
            )
        self.set_state(head, STATE_MOUNTED)
        if remove is not None and removed_state != STATE_UNMOUNTED:
            self.set_state(remove, removed_state, removed_dock)
        self._sync_engine(head)

    def _run_selector(self, sel, idx, mid):
        if self.materials.get_active_slot(sel.material_group) == idx:
            return
        context = {
            **sel.select_gcode.create_template_context(),
            "group": sel.material_group,
            "slot": idx,
            "index": idx - 1,
            "material": mid,
        }
        self.busy = True
        try:
            sel.select_gcode.run_gcode_from_command(context)
        finally:
            self.busy = False
        self.materials.set_active_slot(sel.material_group, idx)

    def _ensure_material(self, mid):
        _, kind, holder, idx = self._best_source(mid)
        if kind == "selector":
            self._run_selector(holder, idx, mid)
        else:
            self._ensure_head(holder, material=mid)
        self.printer.send_event("toolheads:selected", holder.name)

    def _register_endpoints(self):
        webhooks = self.printer.lookup_object("webhooks")
        webhooks.register_endpoint(
            "toolheads/set_state", self._handle_set_state
        )
        webhooks.register_endpoint(
            "toolheads/reorder_docks", self._handle_reorder_docks
        )
        webhooks.register_endpoint(
            "toolheads/confirm_mount", self._handle_confirm_mount
        )
        webhooks.register_endpoint(
            "toolheads/cancel_mount", self._handle_cancel_mount
        )

    def _queue_engine_sync(self):
        if self.toolchanger is None:
            return

        def sync(eventtime):
            try:
                self.gcode.run_script("TOOLHEADS_SYNC")
            except Exception:
                logging.exception("toolheads: engine sync failed")

        self.reactor.register_callback(sync)

    def _handle_set_state(self, web_request):
        head = self._lookup(web_request.get_str("toolhead"))
        dock = web_request.get_int("dock", None)
        self._require_editable()
        self.set_state(head, web_request.get_str("state"), dock)
        self._queue_engine_sync()
        web_request.send({"toolhead": head.name})

    def _handle_reorder_docks(self, web_request):
        order = web_request.get("order")
        if not isinstance(order, list):
            raise self.printer.command_error("order must be a list")
        self._require_editable()
        self.reorder_docks(order)
        web_request.send({"order": order})

    def _handle_confirm_mount(self, web_request):
        if self.awaiting is None:
            raise self.printer.command_error("No toolhead is being awaited")
        state = web_request.get_str("removed_state", STATE_UNMOUNTED)
        dock = web_request.get_int("removed_dock", None)
        # A head that follows a board cannot be mounted while it is unplugged;
        # say so now, while the request is still waiting for a real answer.
        self._check_mcu_state(
            self.heads[self.awaiting["toolhead"]], STATE_MOUNTED
        )
        remove = None
        if self.awaiting["remove"] is not None:
            remove = self.heads[self.awaiting["remove"]]
        self._validate_removed(remove, state, dock)
        self._await_removed = (state, dock)
        self._await_result = "confirmed"
        web_request.send({"confirmed": self.awaiting["toolhead"]})

    def _handle_cancel_mount(self, web_request):
        if self.awaiting is None:
            raise self.printer.command_error("No toolhead is being awaited")
        self._await_result = "cancelled"
        web_request.send({"cancelled": self.awaiting["toolhead"]})

    def _register_commands(self):
        for name, func, desc in (
            (
                "SELECT_MATERIAL",
                self.cmd_SELECT_MATERIAL,
                "Get a material into place (MATERIAL=<id or slicer token>)",
            ),
            (
                "SELECT_TOOLHEAD",
                self.cmd_SELECT_TOOLHEAD,
                "Get a toolhead onto the carriage (TOOLHEAD=<name>)",
            ),
            (
                "TOOLHEADS_PREFLIGHT",
                self.cmd_TOOLHEADS_PREFLIGHT,
                "Check every material of a print can be had "
                "(MATERIALS=<comma separated ids or slicer tokens>)",
            ),
            (
                "TOOLHEAD_SET_STATE",
                self.cmd_TOOLHEAD_SET_STATE,
                "Record where a toolhead physically is "
                "(TOOLHEAD=<name> STATE=<state> [DOCK=<n>])",
            ),
            (
                "TOOLHEAD_REORDER_DOCKS",
                self.cmd_TOOLHEAD_REORDER_DOCKS,
                "Reassign docked toolheads to docks "
                "(ORDER=<name or - per dock, comma separated>)",
            ),
            (
                "TOOLHEAD_SET_ORIGIN",
                self.cmd_TOOLHEAD_SET_ORIGIN,
                "Make the position of a toolhead that cannot be homed its "
                "origin ([X=<mm>] [Y=<mm>] [Z=<mm>], default 0)",
            ),
            (
                "TOOLHEADS_SYNC",
                self.cmd_TOOLHEADS_SYNC,
                "Tell the toolchanger which toolhead is mounted",
            ),
        ):
            self.gcode.register_command(name, func, desc=desc)

    def cmd_SELECT_MATERIAL(self, gcmd):
        key = gcmd.get("MATERIAL", "").strip()
        if not key:
            gcmd.respond_info("SELECT_MATERIAL: no material given, ignoring")
            return
        self._current_gcmd = gcmd
        self._ensure_material(self._resolve_material(key))

    def cmd_SELECT_TOOLHEAD(self, gcmd):
        self._current_gcmd = gcmd
        head = self._lookup(gcmd.get("TOOLHEAD"))
        self._ensure_head(head)
        self.printer.send_event("toolheads:selected", head.name)

    def cmd_TOOLHEADS_PREFLIGHT(self, gcmd):
        head = self.mounted_head()
        if head is not None and not head.homeable and not self.origin_set:
            raise gcmd.error(
                "%s has no origin. Jog it to where the origin should be, "
                "then run TOOLHEAD_SET_ORIGIN" % (head.display_name,)
            )
        keys = [k.strip() for k in gcmd.get("MATERIALS", "").split(",")]
        problems = []
        for key in filter(None, keys):
            try:
                mid = self._resolve_material(key)
                _, kind, holder, idx = self._best_source(mid)
            except self.printer.command_error as e:
                problems.append(str(e))
                continue
            if kind == "selector":
                plan = "%s, slot %d" % (holder.name, idx)
            elif holder.state == STATE_MOUNTED:
                plan = "%s, already mounted" % (holder.display_name,)
            elif holder.state == STATE_DOCKED and holder.can_swap():
                plan = "%s, automatic swap from dock %d" % (
                    holder.display_name,
                    holder.dock,
                )
            else:
                plan = "%s, must be mounted by hand (%s)" % (
                    holder.display_name,
                    holder.state,
                )
            gcmd.respond_info("%s: %s" % (key, plan))
        if problems:
            raise gcmd.error("; ".join(problems))

    def cmd_TOOLHEAD_SET_STATE(self, gcmd):
        head = self._lookup(gcmd.get("TOOLHEAD"))
        self._require_editable()
        self.set_state(
            head, gcmd.get("STATE").lower(), gcmd.get_int("DOCK", None)
        )
        try:
            self._sync_engine(self.mounted_head())
        except self.printer.command_error as e:
            # The state is recorded; only the toolchanger could not be told.
            gcmd.respond_info("Toolhead state recorded, but: %s" % (e,))

    def cmd_TOOLHEAD_REORDER_DOCKS(self, gcmd):
        order = [n.strip() for n in gcmd.get("ORDER").split(",")]
        self._require_editable()
        self.reorder_docks([None if n in ("", "-") else n for n in order])

    cmd_G28_help = "Home the axes (never moves a toolhead that cannot be homed)"

    def cmd_G28(self, gcmd):
        head = self.mounted_head()
        if head is None or head.homeable:
            self.prev_G28(gcmd)
            return
        if not self.origin_set:
            raise gcmd.error(
                "%s cannot be homed. Jog it to where the origin should be, "
                "then run TOOLHEAD_SET_ORIGIN" % (head.display_name,)
            )
        gcmd.respond_info(
            "%s cannot be homed; keeping the origin that was set"
            % (head.display_name,)
        )

    def cmd_TOOLHEAD_SET_ORIGIN(self, gcmd):
        head = self.mounted_head()
        if head is None or head.homeable:
            raise gcmd.error(
                "The mounted toolhead can be homed; use G28 instead"
            )
        self._require_editable()
        values = [gcmd.get_float(a, 0.0) for a in "XYZ"]
        toolhead = self.printer.lookup_object("toolhead")
        toolhead.wait_moves()
        here = toolhead.get_position()
        self._set_gcode_offset([here[i] - values[i] for i in range(3)])
        self.origin_set = True
        gcmd.respond_info(
            "Origin of %s set here (X%.3f Y%.3f Z%.3f)"
            % ((head.display_name,) + tuple(values))
        )

    def cmd_TOOLHEADS_SYNC(self, gcmd):
        if not self.busy and self.awaiting is None:
            self._sync_engine(self.mounted_head())

    def get_status(self, eventtime):
        heads = {}
        for name, head in self.heads.items():
            heads[name] = {
                "display_name": head.display_name,
                "state": head.state,
                "dock": head.dock,
                "can_swap": head.can_swap(),
                "homeable": head.homeable,
                "material_group": head.material_group,
                "materials": self.owned_materials(head),
                "mcu": head.mcu_name,
                "follows_mcu": head.mcu_name is not None,
            }
        mounted = self.mounted_head()
        awaiting = dict(self.awaiting) if self.awaiting is not None else None
        docks = []
        for dock in range(1, self.dock_count + 1):
            owner = self._dock_owner(dock)
            docks.append(owner.name if owner is not None else None)
        return {
            "dock_count": self.dock_count,
            "docks": docks,
            "order": list(self.heads),
            "mounted": mounted.name if mounted else None,
            "origin_set": mounted is None
            or mounted.homeable
            or self.origin_set,
            "toolheads": heads,
            "selectors": {
                name: {"material_group": sel.material_group}
                for name, sel in self.selectors.items()
            },
            "awaiting_mount": awaiting,
            "busy": self.busy,
            "last_error": self.last_error,
        }


def load_config(config):
    return Toolheads(config)
