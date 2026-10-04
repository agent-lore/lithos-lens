"""Request-level hygiene every curated-write POST goes through (§5C.6).

Two rules, both pure and both shared by every write route this milestone adds
— the operator page here in W1, the write funnel in W4 — so that neither can be
re-decided per handler:

- :func:`same_origin`, the Origin/Referer check. CSRF hygiene on a trusted
  network, NOT authentication: it stops a page open in another tab driving Lens
  with the operator's browser, and it stops nothing else. Lens has no
  authentication at all (REQUIREMENTS §5C.1) and the operator page says so.
- :func:`safe_next`, the one rule for a ``next=`` destination. The operator page
  uses it to return the operator where they were; W4's 303 reuses it, because a
  redirect target that arrived in a request is the same question either way.

Header values arrive as strings and leave as a bool or a path: nothing here
touches a ``Request``, so each rule is a table-driven unit test rather than a
round trip through the app.
"""

from __future__ import annotations

import unicodedata
from urllib.parse import urlsplit

__all__ = [
    "ORIGIN_REFUSAL_MESSAGE",
    "safe_next",
    "same_origin",
]

#: Answered with 403, before any Lithos call.
ORIGIN_REFUSAL_MESSAGE = (
    "Refused: this POST did not come from a page served by this Lens. "
    "Nothing was changed."
)

#: The port a URL means when it states none. Also the set of schemes an
#: Origin may carry: anything else (``null``, ``file://``, a bare host) names
#: no comparable authority and is a mismatch.
_DEFAULT_PORTS: dict[str, str] = {"http": "80", "https": "443"}


def _authority(url: str) -> tuple[str, str] | None:
    """``(host, port)`` of an absolute Origin/Referer value, or None.

    None means "does not name an authority this check can compare" — an
    unparseable value, a scheme without a default port, ``Origin: null`` (sent
    for a sandboxed or opaque origin), or a value with no host. Every None is a
    mismatch at the call site; there is no benefit of the doubt, because the
    header is the whole evidence the check has.
    """
    try:
        parsed = urlsplit(url.strip())
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        # urlsplit defers port parsing to the property, so a value like
        # "http://h:notaport" raises HERE rather than at split time.
        return None
    if parsed.scheme not in _DEFAULT_PORTS or not host:
        return None
    # ``port is not None``, never truthiness: ``http://lens.lan:0`` STATES a
    # port, and port 0 is a different origin from the scheme's default. Reading
    # it as "absent" would make that value match ``Host: lens.lan`` — an
    # explicit different port the check is required to refuse.
    stated = str(port) if port is not None else _DEFAULT_PORTS[parsed.scheme]
    return host.lower(), stated


def _host_authority(host_header: str, scheme: str) -> tuple[str, str] | None:
    """``(host, port)`` of the request's ``Host`` header under ``scheme``.

    A ``Host`` carries no scheme, so the port it implies when it states none
    comes from the request's own scheme — which is what makes
    ``Origin: http://lens.lan`` match ``Host: lens.lan:80`` and vice versa.
    """
    host_header = host_header.strip()
    return _authority(f"{scheme}://{host_header}") if host_header else None


def same_origin(
    *, origin: str | None, referer: str | None, host: str, scheme: str = "http"
) -> bool:
    """Whether this POST came from a page this Lens served (§5C.6, D2).

    The ``Origin`` header's host AND port must match the request's ``Host``,
    case-insensitively, with each side's missing port filled in from its
    scheme. ``Referer`` is the fallback for the browsers (and the few form
    posts) that send no ``Origin``; a request carrying NEITHER is a mismatch,
    not a pass — "no evidence" is the case this check exists for.

    The scheme is deliberately not compared: Lens serves plain HTTP on the
    trusted network, and the rule the requirement states is host and port.
    """
    # PRESENCE decides which header is read; VALIDITY only decides the answer.
    # Present-but-unusable is not a reason to consult the Referer — an
    # ``Origin: null``, an EMPTY one, a blank one, or a malformed one is the
    # browser telling Lens where the POST came from, and falling back would let
    # a sender pick whichever header suits it. So absence is ``None`` (the
    # caller passes the header lookup through unchanged) and the test is
    # ``is not None``, never truthiness: ``Origin:`` with no value is present
    # and unparseable, which is a mismatch, not an absence.
    if origin is not None:
        claimed = _authority(origin)
    elif referer is not None:
        claimed = _authority(referer)
    else:
        return False
    if claimed is None:
        return False
    served = _host_authority(host, scheme)
    return served is not None and claimed == served


def _has_control_char(value: str) -> bool:
    """Any character in Unicode's control category (``Cc``).

    The whole category, not the C0 range: that is C0 and DEL *and* the C1
    controls U+0080-U+009F, which reach this helper through a percent-encoded
    ``next`` in an ordinary request (``%C2%85`` is U+0085, NEL — a line break
    to some parsers). Every one of them is a header-splitting or display hazard
    in a ``Location`` and none is part of a path an operator navigated to, so
    the CATEGORY is the right unit rather than a hand-written range.
    """
    return any(unicodedata.category(ch) == "Cc" for ch in value)


def safe_next(value: str | None, *, default: str) -> str:
    """``value`` if it is a same-origin relative path, else ``default``.

    Accepted only when it starts with EXACTLY one ``/`` and carries no scheme,
    no authority, no backslash and no control character. So ``//host`` (a
    scheme-relative URL), ``/\\host`` (which browsers read as ``//host``),
    ``http://host`` and ``javascript:...`` all fall back to the default
    destination rather than sending the operator off this Lens.

    One rule, one function: the operator page's return trip and W4's
    post-write 303 take the same destinations from the same untrusted place.
    """
    if not value or not value.startswith("/") or value.startswith("//"):
        return default
    if "\\" in value or _has_control_char(value):
        return default
    return value
