"""Errors adapters share."""

from __future__ import annotations


class NetworkBlockedError(RuntimeError):
    """Raised by the test network guard for an unmocked outbound call (TEST_NO_SEND=1)."""
