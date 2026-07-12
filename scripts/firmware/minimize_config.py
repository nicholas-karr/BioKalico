#!/usr/bin/env python3
# Reduce a full Klipper Kconfig .config down to a minimal preset - only
# symbols whose value differs from the Kconfig-computed default are kept,
# matching 'make savedefconfig' semantics (see lib/kconfiglib's
# write_min_config()). Used when authoring/updating a
# biokalico_extras/firmware_presets/*.config template so the preset only
# records deliberate choices (board/chip, bootloader offset, clock
# reference, ...) rather than every option's fully-expanded default.
#
# A minimal config is not directly buildable - `make olddefconfig` expands
# it back to a full .config at build time (this already happens as part of
# scripts/firmware/build_and_flash.py's normal build step).
#
# Usage: minimize_config.py <full-config> <minimal-config-out>

import os
import sys

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib", "kconfiglib"))
import kconfiglib  # noqa: E402


def main():
    if len(sys.argv) != 3:
        sys.exit("Usage: minimize_config.py <full-config> <minimal-config-out>")
    full_config, out_path = sys.argv[1], sys.argv[2]

    kconf = kconfiglib.Kconfig(os.path.join(REPO_ROOT, "src", "Kconfig"))
    print(kconf.load_config(full_config))
    print(kconf.write_min_config(out_path))


if __name__ == "__main__":
    main()
