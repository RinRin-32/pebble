"""pebble - Multi-node AI orchestration platform with tool use, agent routing, and cluster simulation.

Formerly ``turnstone``.  The rename is aliased rather than abrupt: see
``_alias_legacy_env`` below for why every ``TURNSTONE_*`` variable still works.
"""

import os

__version__ = "1.8.0a2"

#: The previous name of this project.  Kept as a constant rather than spelled
#: out at each use site so the eventual removal is one deletion, not a sweep.
LEGACY_ENV_PREFIX = "TURNSTONE_"
ENV_PREFIX = "PEBBLE_"


def _alias_legacy_env(environ: "os._Environ[str] | dict[str, str]" = os.environ) -> None:
    """Make ``TURNSTONE_*`` and ``PEBBLE_*`` interchangeable, both directions.

    A rename that only accepts the new spelling breaks every deployment the
    moment it lands: the ``.env``, the compose file, the shell profile and the
    laptop's client config all still say ``TURNSTONE_``.  A rename that only
    accepts the old one never actually completes.  So both are honoured, and
    the aliasing happens once here at import rather than at each of the ~80
    read sites -- which keeps the eventual cleanup to deleting this function.

    ``setdefault`` gives an explicitly-set variable precedence over its alias,
    so someone mid-migration who sets both gets the one they actually chose
    rather than whichever direction this loop happened to run.
    """
    for key, value in list(environ.items()):
        if key.startswith(LEGACY_ENV_PREFIX):
            environ.setdefault(ENV_PREFIX + key[len(LEGACY_ENV_PREFIX) :], value)
        elif key.startswith(ENV_PREFIX):
            environ.setdefault(LEGACY_ENV_PREFIX + key[len(ENV_PREFIX) :], value)


_alias_legacy_env()
