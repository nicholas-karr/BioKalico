import typing

import klippy.configfile
import klippy.extras.danger_options
import klippy.gcode


class Restart(Exception): ...


class PrinterShim:
    class GCode:
        error = Exception

        def __init__(self):
            self.ready_gcode_handlers = {}
            self.mux_commands = {}

        def register_command(self, cmd, func, *_, **__):
            self.ready_gcode_handlers[cmd.upper()] = func

        def register_mux_command(self, cmd, key, value, func, *_, **__):
            values = self.mux_commands.setdefault(cmd.upper(), (key, {}))[1]
            values[value] = func
            self.register_command(
                cmd, lambda gcmd: values[gcmd.get(key, None)](gcmd)
            )

        def respond_info(self, msg):
            print("info", msg)

        def respond_raw(self, msg):
            print("raw", msg)

        def request_restart(self, reason):
            raise Restart(reason)

        def create_gcode_command(self, command, commandline, params):
            return klippy.gcode.GCodeCommand(
                self, command, commandline, params, False
            )

        def call(self, cmdline):
            command, *paramlist = cmdline.split()
            func = self.ready_gcode_handlers[command.upper()]
            params = dict(param.split("=", 1) for param in paramlist)
            gcmd = klippy.gcode.GCodeCommand(
                self, command, cmdline, params, False
            )
            print("Calling", func, "with", params)
            func(gcmd)

    def __init__(self, start_args):
        self.start_args = start_args
        self.objects = {}
        self.add_object("gcode", self.GCode())
        self.add_object("configfile", klippy.configfile.PrinterConfig(self))

        self.call = self.lookup_object("gcode").call

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def get_start_args(self):
        return self.start_args

    def add_object(self, name, obj):
        self.objects[name] = obj

    @typing.overload
    def lookup_object(self, name: typing.Literal["gcode"]) -> GCode: ...
    @typing.overload
    def lookup_object(
        self, name: typing.Literal["configfile"]
    ) -> klippy.configfile.PrinterConfig: ...

    def lookup_object(self, name, *default):
        if default:
            return self.objects.get(name, default[0])
        return self.objects[name]

    def lookup_objects(self, pfx=""):
        if pfx:
            pfx = pfx + " "
        return [(k, v) for k, v in self.objects.items() if k.startswith(pfx)]

    def load_config(self):
        config = self.lookup_object("configfile").read_main_config()
        self.add_object(
            "danger_options",
            klippy.extras.danger_options.load_config(
                config.getsection("danger_options")
            ),
        )
        return config

    def set_rollover_info(self, *_): ...


_MISSING = object()


class FakeWebhooks:
    def __init__(self):
        self.endpoints = {}

    def register_endpoint(self, path, cb):
        self.endpoints[path] = cb


class FakeWebRequestError(Exception):
    """Stands in for klippy.webhooks.WebRequestError."""


class FakeWebRequest:
    """Mimics the subset of klippy.webhooks.WebRequest that extras use,
    including its strict argument types (a str argument must be a str, an
    int argument an int and not a bool, a float argument an int or float)."""

    error = FakeWebRequestError

    def __init__(self, params):
        self.params = params
        self.sent = None

    def get(self, item, default=_MISSING, types=None):
        value = self.params.get(item, default)
        if value is _MISSING:
            raise FakeWebRequestError("Missing Argument [%s]" % (item,))
        if (
            types is not None
            and type(value) not in types
            and item in self.params
        ):
            raise FakeWebRequestError("Invalid Argument Type [%s]" % (item,))
        return value

    def get_str(self, item, default=_MISSING):
        return self.get(item, default, types=(str,))

    def get_int(self, item, default=_MISSING):
        return self.get(item, default, types=(int,))

    def get_float(self, item, default=_MISSING):
        return float(self.get(item, default, types=(int, float)))

    def send(self, data):
        self.sent = data
