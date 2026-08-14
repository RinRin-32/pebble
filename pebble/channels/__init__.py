"""Shared channel infrastructure for turnstone communication integrations.

Provides the :class:`ChannelAdapter` protocol, the :class:`ChannelRouter`
for workstream mapping, and shared formatting / configuration utilities.
"""

from pebble.channels._protocol import ChannelAdapter
from pebble.channels._routing import ChannelRouter

__all__ = [
    "ChannelAdapter",
    "ChannelRouter",
]
