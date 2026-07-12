# Declares [firmware_build <name>] sections so per-MCU build/flash presets
# can live in printer.cfg instead of a separate config file. Purely
# declarative - the actual build/flash work is done host-side by
# scripts/firmware/build_and_flash.py via the firmware_build Moonraker
# component (biokalico_extras/moonraker/firmware_build.py), which parses
# printer.cfg directly rather than depending on this object at runtime.
#
# This exists only so Klipper's config parser (which fatal-errors on any
# section not claimed by a loaded module) accepts these sections.


class FirmwareBuild:
    def __init__(self, config):
        self.name = config.get_name().split()[-1]
        self.preset = config.get("preset")
        # 'mcu' is purely informational (which live [mcu ...] status object
        # to query for mcu_version, for mismatch detection) - it is NOT
        # used to derive 'device'. 'device' is mandatory and never
        # auto-derived from a referenced mcu section's serial:/canbus_uuid:
        # a bare device path there (e.g. /dev/ttyACM0 instead of a stable
        # /dev/serial/by-id/... symlink) isn't guaranteed to still point at
        # the same physical board by the time a flash actually runs -
        # normal Klipper operation just fails to connect if that drifts,
        # but silently flashing whatever now happens to be at that path is
        # a much worse failure mode. Require the user to consciously pick
        # a stable identifier instead of inheriting one implicitly.
        self.mcu_name = config.get("mcu", self.name)
        self.device = config.get("device")

    def get_status(self, eventtime=None):
        return {
            "preset": self.preset,
            "mcu": self.mcu_name,
            "device": self.device,
        }


def load_config_prefix(config):
    return FirmwareBuild(config)
