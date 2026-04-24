# Dedicated pulse train output pin handling


class MCU_pulse_pin:
    def __init__(
        self, pin_params, start_value, shutdown_value, max_triggers_per_sec
    ):
        self._mcu = pin_params["chip"]
        self._pin = pin_params["pin"]
        self._invert = pin_params["invert"]
        self._oid = self._mcu.create_oid()
        self._mcu.register_config_callback(self._build_config)
        self._last_clock = 0
        self._start_cmd = self._stop_cmd = None
        self._pulse_value = 1 ^ self._invert
        self._start_value = (not not start_value) ^ self._invert
        self._shutdown_value = (not not shutdown_value) ^ self._invert
        self._max_triggers_per_sec = max_triggers_per_sec
        self._computed_max_triggers_per_sec = 0.0
        self._clock_freq = 0

    def get_mcu(self):
        return self._mcu

    def get_max_triggers_per_sec(self):
        return self._computed_max_triggers_per_sec

    def _build_config(self):
        cmd_queue = self._mcu.alloc_command_queue()
        curtime = self._mcu.get_printer().get_reactor().monotonic()
        printtime = self._mcu.estimated_print_time(curtime)
        self._last_clock = self._mcu.print_time_to_clock(printtime + 0.200)
        self._mcu.add_config_cmd(
            "config_pulse_out oid=%d pin=%s value=%d default_value=%d"
            % (
                self._oid,
                self._pin,
                self._pulse_value,
                self._shutdown_value,
            )
        )
        self._mcu.add_config_cmd(
            "stop_pulse_out oid=%d" % (self._oid,), on_restart=True
        )
        self._mcu.add_config_cmd(
            "set_digital_out pin=%s value=%d"
            % (self._pin, self._start_value),
            is_init=True,
        )
        self._start_cmd = self._mcu.lookup_command(
            "start_pulse_out oid=%c clock=%u width_ticks=%u"
            " period_ticks=%u count=%u",
            cq=cmd_queue,
        )
        self._stop_cmd = self._mcu.lookup_command(
            "stop_pulse_out oid=%c", cq=cmd_queue
        )
        self._clock_freq = int(self._mcu.get_constant_float("CLOCK_FREQ") + 0.5)
        if self._max_triggers_per_sec is None:
            # Cap default pulse rate so low-end MCUs fail fast before jitter.
            self._computed_max_triggers_per_sec = max(
                1.0, float(self._clock_freq) / 20000.0
            )
        else:
            self._computed_max_triggers_per_sec = self._max_triggers_per_sec

    def start_pulses(
        self, print_time, pulse_width_us, triggers_per_sec, trigger_count
    ):
        if self._start_cmd is None:
            raise self._mcu.get_printer().command_error(
                "Pulse pin command interface is not initialized"
            )
        if pulse_width_us < 100:
            raise self._mcu.get_printer().command_error(
                "PULSE_WIDTH_US must be at least 100"
            )
        if triggers_per_sec > self._computed_max_triggers_per_sec:
            raise self._mcu.get_printer().command_error(
                "TRIGGERS_PER_SEC exceeds supported limit (%.1f)"
                % (self._computed_max_triggers_per_sec,)
            )
        width_ticks = int((pulse_width_us * self._clock_freq + 999999) // 1000000)
        period_ticks = self._mcu.seconds_to_clock(1.0 / triggers_per_sec)
        if period_ticks <= width_ticks:
            raise self._mcu.get_printer().command_error(
                "Pulse period must be greater than pulse width"
            )
        if width_ticks > period_ticks // 10:
            raise self._mcu.get_printer().command_error(
                "Pulse duty cycle exceeds 10%"
            )
        clock = self._mcu.print_time_to_clock(print_time)
        self._start_cmd.send(
            [self._oid, clock, width_ticks, period_ticks, trigger_count],
            minclock=self._last_clock,
            reqclock=clock,
        )
        self._last_clock = clock

    def stop_pulses(self):
        if self._stop_cmd is None:
            raise self._mcu.get_printer().command_error(
                "Pulse pin command interface is not initialized"
            )
        self._stop_cmd.send([self._oid])


class PrinterPulsePin:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.last_print_time = 0.0
        self.last_pulse_width_us = config.getint(
            "pulse_width_us", 100, minval=100
        )
        self.last_triggers_per_sec = config.getfloat(
            "triggers_per_sec", 1.0, above=0.0
        )
        self.last_trigger_count = config.getint("trigger_count", 1, minval=0)
        start_value = config.getint("value", 0, minval=0, maxval=1)
        shutdown_value = config.getint("shutdown_value", 0, minval=0, maxval=1)
        max_triggers_per_sec = config.getfloat(
            "max_triggers_per_sec", None, above=0.0
        )
        ppins = self.printer.lookup_object("pins")
        pin_params = ppins.lookup_pin(config.get("pin"), can_invert=True)
        self.mcu_pin = MCU_pulse_pin(
            pin_params, start_value, shutdown_value, max_triggers_per_sec
        )
        pin_name = config.get_name().split()[1]
        gcode = self.printer.lookup_object("gcode")
        gcode.register_mux_command(
            "START_PULSE_PIN",
            "PIN",
            pin_name,
            self.cmd_START_PULSE_PIN,
            desc=self.cmd_START_PULSE_PIN_help,
        )
        gcode.register_mux_command(
            "STOP_PULSE_PIN",
            "PIN",
            pin_name,
            self.cmd_STOP_PULSE_PIN,
            desc=self.cmd_STOP_PULSE_PIN_help,
        )

    def get_status(self, eventtime):
        return {
            "pulse_width_us": self.last_pulse_width_us,
            "triggers_per_sec": self.last_triggers_per_sec,
            "trigger_count": self.last_trigger_count,
            "max_triggers_per_sec": self.mcu_pin.get_max_triggers_per_sec(),
        }

    def _start_pulse_pin(
        self, print_time, pulse_width_us, triggers_per_sec, trigger_count
    ):
        min_sched_time = self.mcu_pin.get_mcu().min_schedule_time()
        print_time = max(print_time, self.last_print_time + min_sched_time)
        self.mcu_pin.start_pulses(
            print_time, pulse_width_us, triggers_per_sec, trigger_count
        )
        self.last_print_time = print_time
        self.last_pulse_width_us = pulse_width_us
        self.last_triggers_per_sec = triggers_per_sec
        self.last_trigger_count = trigger_count

    cmd_START_PULSE_PIN_help = "Start a configurable pulse train on a pin"

    def cmd_START_PULSE_PIN(self, gcmd):
        pulse_width_us = gcmd.get_int(
            "PULSE_WIDTH_US", self.last_pulse_width_us, minval=100
        )
        triggers_per_sec = gcmd.get_float(
            "TRIGGERS_PER_SEC", self.last_triggers_per_sec, above=0.0
        )
        trigger_count = gcmd.get_int(
            "TRIGGER_COUNT", self.last_trigger_count, minval=0
        )
        toolhead = self.printer.lookup_object("toolhead")
        toolhead.register_lookahead_callback(
            lambda print_time: self._start_pulse_pin(
                print_time,
                pulse_width_us,
                triggers_per_sec,
                trigger_count,
            )
        )

    cmd_STOP_PULSE_PIN_help = "Stop an active pulse train on a pin"

    def cmd_STOP_PULSE_PIN(self, gcmd):
        self.mcu_pin.stop_pulses()
        self.last_trigger_count = 0


def load_config_prefix(config):
    return PrinterPulsePin(config)
