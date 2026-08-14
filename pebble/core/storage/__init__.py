"""Pluggable storage backend for turnstone persistence.

Supports SQLite (default, zero-config) and PostgreSQL (multi-node, production).
"""

from pebble.core.storage._protocol import StorageBackend, StorageConflictError
from pebble.core.storage._registry import (
    StorageUnavailableError,
    get_storage,
    init_storage,
    is_storage_initialized,
    reset_storage,
)

__all__ = [
    "StorageBackend",
    "StorageConflictError",
    "StorageUnavailableError",
    "get_storage",
    "init_storage",
    "is_storage_initialized",
    "reset_storage",
]
