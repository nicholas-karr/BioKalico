# Register $HOME as a Moonraker file_manager root so it shows up as a
# selectable root in Mainsail's Machine > Config Files page.
#
# This is an "unofficial component": Moonraker auto-updates itself via git,
# and a tracked-file edit to file_manager.py would either get wiped on the
# next update or (since the update manager refuses to update a dirty repo)
# block Moonraker from updating at all. Dropping an extra, untracked file in
# moonraker/components/ is the mechanism Moonraker itself recognizes for this
# case (see get_repo_info's "unofficial_components" handling). It survives
# `git pull` and doesn't touch tracked files.
#
# Deployed into place by scripts/patch-moonraker-component.sh. Source of
# truth lives here in the klipper repo; see biokalico_extras/README.md.
#
# [home_root] config options (see biokalico_extras/moonraker/moonraker_home_root.conf):
#   root_name:   name shown in Mainsail's Root selector (default: home)
#   path:        directory to expose (default: ~)
#   full_access: allow write/delete through this root (default: False)
#
# Mainsail's Configure page recursively enumerates every registered root to
# build its file tree - one server.files.get_directory call per directory,
# every time the page loads. For a root as broad as $HOME that's ruinous
# (observed directly: 11,000+ calls in 11 seconds, each one running
# file_manager's directory listing synchronously on Moonraker's event loop,
# long enough to trip its "EVENT LOOP BLOCKED" watchdog and drop Mainsail's
# websocket connection). Fixed client-side instead of here - see
# biokalico_extras/mainsail/home-root-throttle.js - since the fix needs to
# generalize to whatever ends up under $HOME, not just what's there today,
# and a rate limit on the request source is more robust than trying to
# enumerate "big" directories by name on the server.

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper


class HomeRoot:
    def __init__(self, config: ConfigHelper) -> None:
        server = config.get_server()
        file_manager = server.load_component(config, "file_manager")
        root_name = config.get("root_name", "home")
        path = config.get("path", "~")
        full_access = config.getboolean("full_access", False)
        file_manager.register_directory(root_name, path, full_access)


def load_component(config: ConfigHelper) -> HomeRoot:
    return HomeRoot(config)
