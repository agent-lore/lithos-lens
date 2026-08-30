"""Exception hierarchy for Lithos Lens.

All Lithos Lens-raised exceptions derive from ``LithosLensError`` so callers can
catch a single base type.
"""

from __future__ import annotations


class LithosLensError(Exception):
    """Base class for all Lithos Lens exceptions."""


class ConfigError(LithosLensError):
    """Raised when required configuration is missing or invalid."""


class EventSubscriberLimit(LithosLensError):
    """Raised when the event hub is already at its subscriber ceiling.

    A refusal, not a failure: the caller turns it into a 503 and the browser
    falls back to polling. See ``events.MAX_EVENT_SUBSCRIBERS``.
    """


class UnsupportedEventEncoding(LithosLensError):
    """Raised when the upstream event stream arrives content-encoded.

    Lens asks for ``identity`` and reads raw bytes so that one socket read is
    one bounded chunk. A compressed stream would be decoded into memory before
    any cap could see it, so it is refused rather than read unbounded. See
    ``events._require_identity_encoding``.
    """
