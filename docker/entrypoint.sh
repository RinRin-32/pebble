#!/bin/sh
# Run database migrations before starting the service.
#
# Failure is deliberately non-fatal: on a fresh SQLite bootstrap the schema is
# built from metadata and alembic legitimately reports conflicts.  But silence
# is not the same as tolerance -- a rename once left this line invoking a module
# that no longer existed, and every node skipped migrations without a word.
# So the failure is still survivable, and now it is at least audible.
if ! python -m pebble.core.storage._migrate; then
    echo "entrypoint: migrations did not complete cleanly (continuing)" >&2
fi

# Execute the actual command
exec "$@"
