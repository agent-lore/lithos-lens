"""T3-W1 — posture and operator identity.

Four groups, in the order the acceptance criteria are written:

- the ORIGIN check and the ``next`` rule, as pure tables (``write_guards``);
- IDENTITY resolution and the id rule (``operator``);
- the impersonation guard and register-once at the seam W4 calls
  (``OperatorRegistry.ensure_registered``), asserted on the fake's call log;
- the ROUTES — the operator page, the chrome chip, and ``/tasks/new``.

Everything here asserts on what a request returns and on the calls the fake
recorded, never on internals: the identity module is deep precisely so that
"an archived non-human id is refused with no registration call" is a unit
test rather than a browser session.
"""

from __future__ import annotations

import asyncio
import functools
import html
import inspect
import re
from dataclasses import replace
from pathlib import Path
from textwrap import dedent

import pytest
from fastapi.testclient import TestClient

from lithos_lens.config import load_config
from lithos_lens.fake_dataset import FakeLithosDataset
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.lithos_client import LithosToolError
from lithos_lens.operator import (
    OPERATOR_COOKIE_MAX_AGE_S,
    OPERATOR_COOKIE_NAME,
    REFUSAL_BELONGS_TO_AGENT,
    REFUSAL_REGISTRATION_FAILED,
    REFUSAL_SERVICE_AGENT,
    OperatorRegistry,
    resolve_operator,
    valid_operator_id,
)
from lithos_lens.tasks import RESERVED_TASK_PATH_SEGMENTS, AgentRecord
from lithos_lens.web import create_app
from lithos_lens.write_guards import safe_next, same_origin

SERVICE_AGENT_ID = "lithos-lens-test"

# The fixtures every guard case needs: an id held by a typed agent, one held
# by an ARCHIVED typed agent (which `lithos_agent_list` hides and the exact
# lookup returns), one registered with no type at all, and one archived HUMAN.
GUARD_DATASET = FakeLithosDataset(
    agents=(
        AgentRecord(id="agent-zero", name="Agent Zero", type="claude-code"),
        AgentRecord(id="scratch-runner", name="scratch-runner", type=""),
        AgentRecord(id="dave", name="Dave", type="human"),
    ),
    archived_agents=(
        AgentRecord(id="old-worker", name="Old Worker", type="claude-code"),
        AgentRecord(id="retired-dave", name="Retired Dave", type="human"),
    ),
)


def _run(coro):
    return asyncio.run(coro)


def _fake() -> FakeLithosClient:
    return FakeLithosClient(None, dataset=GUARD_DATASET)


def _registry() -> OperatorRegistry:
    return OperatorRegistry(service_agent_id=SERVICE_AGENT_ID)


def _registrations(fake: FakeLithosClient) -> list[tuple[str, dict]]:
    return [call for call in fake.tool_calls if call[0] == "lithos_agent_register"]


def _page_text(response) -> str:
    """The rendered page with HTML entities resolved.

    Copy is asserted against the constants the module defines, and those carry
    apostrophes and dashes Jinja escapes on the way out — comparing against the
    escaped spelling would pin the escaping rather than the sentence."""
    return html.unescape(response.text)


# ── the Origin check (§5C.6, PRD clarification 4) ──────────────────────


@pytest.mark.parametrize(
    ("origin", "referer", "host", "expected"),
    [
        # Same host and port, however each side spells the port.
        ("http://lens.lan:8000", None, "lens.lan:8000", True),
        ("http://lens.lan", None, "lens.lan:80", True),
        ("http://lens.lan:80", None, "lens.lan", True),
        ("http://LENS.LAN:8000", None, "lens.lan:8000", True),
        ("https://lens.lan", None, "lens.lan:443", True),
        # A DIFFERENT PORT on the same hostname is a different origin.
        ("http://lens.lan:8001", None, "lens.lan:8000", False),
        ("http://lens.lan", None, "lens.lan:8000", False),
        # A different host.
        ("http://evil.example", None, "lens.lan:8000", False),
        # Referer is the fallback only when Origin is ABSENT.
        (None, "http://lens.lan:8000/tasks?project=x", "lens.lan:8000", True),
        (None, "http://evil.example/page", "lens.lan:8000", False),
        # Present but unusable: no benefit of the doubt, and no fallback to a
        # Referer that would then be the sender's choice of evidence.
        ("null", "http://lens.lan:8000/tasks", "lens.lan:8000", False),
        ("not a url", None, "lens.lan:8000", False),
        ("http://lens.lan:notaport", None, "lens.lan:8000", False),
        ("file:///tmp/x.html", None, "lens.lan:8000", False),
        # An EXPLICIT port 0 is a port, not an absent one — on either side.
        ("http://lens.lan:0", None, "lens.lan", False),
        ("http://lens.lan", None, "lens.lan:0", False),
        ("http://lens.lan:0", None, "lens.lan:0", True),
        # Present but blank: still the browser's answer, so no Referer
        # fallback — presence decides which header is read, validity only
        # decides the verdict.
        ("   ", "http://lens.lan:8000/tasks", "lens.lan:8000", False),
        # Present but EMPTY — `Origin:` with no value. Not an absence: the
        # Referer must not be consulted (Starlette hands the route "" here and
        # None for a header never sent, and the route passes both through).
        ("", "http://lens.lan:8000/tasks", "lens.lan:8000", False),
        # Neither header at all.
        (None, None, "lens.lan:8000", False),
        # No Host to compare against.
        ("http://lens.lan:8000", None, "", False),
    ],
)
def test_same_origin_compares_host_and_port(
    origin: str | None, referer: str | None, host: str, expected: bool
) -> None:
    assert same_origin(origin=origin, referer=referer, host=host) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/tasks?project=lithos-loom", "/tasks?project=lithos-loom"),
        ("/tasks/abc123", "/tasks/abc123"),
        ("/", "/"),
        # Not a same-origin relative path: every one of these falls back.
        ("//evil.example/x", "/fallback"),
        ("/\\evil.example", "/fallback"),
        ("http://evil.example/x", "/fallback"),
        ("javascript:alert(1)", "/fallback"),
        ("tasks", "/fallback"),
        ("", "/fallback"),
        (None, "/fallback"),
        ("/tasks\r\nSet-Cookie: x=1", "/fallback"),
        # The WHOLE control category, not just C0 and DEL: the C1 range
        # arrives percent-encoded through an ordinary request (%C2%85 is NEL,
        # a line break to some parsers).
        ("/tasks\x00x", "/fallback"),
        ("/tasks\x7f", "/fallback"),
        ("/tasks\x85x", "/fallback"),
        ("/tasks\x80", "/fallback"),
        ("/tasks\x9f", "/fallback"),
        # …and the characters just OUTSIDE it stay ordinary path content.
        ("/tasks\u00a0x", "/tasks\u00a0x"),
        ("/tasks\u00e9", "/tasks\u00e9"),
    ],
)
def test_safe_next_accepts_only_a_relative_path(
    value: str | None, expected: str
) -> None:
    assert safe_next(value, default="/fallback") == expected


# ── identity resolution and the id rule (D3, clarification 6) ──────────


@pytest.mark.parametrize(
    ("cookie", "default", "expected_id", "expected_source"),
    [
        ("dave", "configured", "dave", "cookie"),
        # The rule's first character is [a-z0-9]: a digit-leading cookie is a
        # valid identity, not junk to fall through.
        ("0-person", "configured", "0-person", "cookie"),
        ("7", "", "7", "cookie"),
        (None, "configured", "configured", "default"),
        (None, "0-person", "0-person", "default"),
        ("", "configured", "configured", "default"),
        (None, "", "", "none"),
        # An invalid cookie is ABSENT, so the configured default still applies.
        ("Dave", "configured", "configured", "default"),
        ("dave smith", "configured", "configured", "default"),
        ("-dave", "configured", "configured", "default"),
        ("d" * 64, "configured", "configured", "default"),
        ("../../etc/passwd", "configured", "configured", "default"),
        ("<script>", "", "", "none"),
    ],
)
def test_cookie_beats_default_beats_none(
    cookie: str | None, default: str, expected_id: str, expected_source: str
) -> None:
    identity = resolve_operator(cookie=cookie, default_operator=default)

    assert identity.id == expected_id
    assert identity.source == expected_source
    assert identity.resolved is bool(expected_id)


@pytest.mark.parametrize(
    "value",
    [
        "dave",
        "dave-smith",
        "d",
        "a1",
        "operator-2",
        "d" * 63,
        # Digit-leading is inside ^[a-z0-9][a-z0-9-]{0,62}$ (clarification 6).
        "0",
        "0-person",
        "7" * 63,
    ],
)
def test_the_id_rule_accepts_a_lowercase_slug(value: str) -> None:
    assert valid_operator_id(value)


@pytest.mark.parametrize(
    "value",
    ["", "Dave", "dave_smith", "-dave", "dave ", " dave", "dav e", "d" * 64, "dävé"],
)
def test_the_id_rule_refuses_anything_else(value: str) -> None:
    assert not valid_operator_id(value)


# ── the guard and register-once, at the seam W4 calls ──────────────────


def test_the_first_ensure_registers_once_and_the_second_makes_no_call() -> None:
    """Register-once: Lithos auto-registers an unknown ``agent`` UNTYPED on any
    write, so the first write's identity is registered as ``type="human"``
    first — and exactly once per process, because the ledger is in memory."""
    fake, registry = _fake(), _registry()

    first = _run(registry.ensure_registered(fake, "dave"))
    calls_after_first = list(fake.tool_calls)
    second = _run(registry.ensure_registered(fake, "dave"))

    assert first.ok and second.ok
    assert _registrations(fake) == [
        ("lithos_agent_register", {"id": "dave", "type": "human"})
    ]
    # The second attempt makes NO call at all — not even the lookup.
    assert fake.tool_calls == calls_after_first


def test_an_absent_id_is_accepted_and_registered() -> None:
    """The lookup finding nothing is the ordinary case for a new operator."""
    fake, registry = _fake(), _registry()

    assert _run(registry.ensure_registered(fake, "newcomer")).ok
    assert fake.tool_calls == [
        ("lithos_agent_info", {"id": "newcomer"}),
        ("lithos_agent_register", {"id": "newcomer", "type": "human"}),
    ]


@pytest.mark.parametrize(
    ("operator", "reason"),
    [
        # Lens's own service agent: a write must name the person, never Lens.
        (SERVICE_AGENT_ID, REFUSAL_SERVICE_AGENT),
        # An id held by a typed agent.
        ("agent-zero", REFUSAL_BELONGS_TO_AGENT),
        # An id held by an ARCHIVED typed agent — the case `lithos_agent_list`
        # would have shown as absent, so re-registering it would have re-typed
        # and un-archived a real agent.
        ("old-worker", REFUSAL_BELONGS_TO_AGENT),
        # Registered with NO type (what Lithos's own auto-registration leaves).
        ("scratch-runner", REFUSAL_BELONGS_TO_AGENT),
    ],
)
def test_an_id_that_belongs_to_an_agent_is_refused_without_registering(
    operator: str, reason: str
) -> None:
    fake, registry = _fake(), _registry()

    checked = _run(registry.ensure_registered(fake, operator))

    assert not checked.ok
    assert checked.reason == reason
    # The phrase §5C.5 names, as a literal: asserting only against the module
    # constant would let the copy and the expectation drift together.
    if operator != SERVICE_AGENT_ID:
        assert "that id belongs to an agent" in checked.reason.lower()
    assert _registrations(fake) == []


@pytest.mark.parametrize("operator", ["dave", "retired-dave"])
def test_a_human_id_is_accepted_archived_or_not(operator: str) -> None:
    """The type is the whole question: an archived HUMAN is still a human, and
    re-registering it un-archives the person's own entry rather than
    overwriting an agent's."""
    fake, registry = _fake(), _registry()

    assert _run(registry.ensure_registered(fake, operator)).ok
    assert _registrations(fake) == [
        ("lithos_agent_register", {"id": operator, "type": "human"})
    ]


def test_a_failed_lookup_refuses_a_new_identity() -> None:
    """Lens cannot tell "absent" from "unreadable", and only one of those is
    safe to register — so the lookup failing refuses, and changes nothing."""
    fake, registry = _fake(), _registry()
    fake.agent_info_error = LithosToolError("lithos is down", code="timeout")

    checked = _run(registry.ensure_registered(fake, "newcomer"))

    assert not checked.ok
    assert checked.code == "lookup_failed"
    assert "nothing was changed" in checked.reason
    assert _registrations(fake) == []


def test_an_identity_already_registered_survives_a_failed_lookup() -> None:
    """The other half of that rule: an operator mid-session keeps writing when
    Lithos's lookup goes away, because this process already REGISTERED that id
    — so the second write makes no call at all, the lookup included."""
    fake, registry = _fake(), _registry()
    assert _run(registry.ensure_registered(fake, "dave")).ok
    calls_before = list(fake.tool_calls)
    fake.agent_info_error = LithosToolError("lithos is down", code="timeout")

    assert _run(registry.ensure_registered(fake, "dave")).ok
    assert fake.tool_calls == calls_before


def test_the_pages_check_does_not_stand_in_for_the_seams_lookup() -> None:
    """The guard binds at the seam, and nowhere else.

    ``POST /operator`` checks an id up front so a refusal is immediate — but
    that answer is about the moment it ran. If an acceptance were remembered,
    an id chosen on the page and claimed by an agent BEFORE the first write
    would be registered as a human anyway, overwriting that agent: a
    page-to-write window far wider than the lookup-to-register one
    clarification 3 accepts, and one Lens closes by not caching. So the seam
    looks the id up itself, every time, until the id is registered.
    """
    fake, registry = _fake(), _registry()
    assert _run(registry.check(fake, "newcomer")).ok

    # An agent takes the id between the page visit and the first write.
    fake.dataset = replace(
        GUARD_DATASET,
        agents=(*GUARD_DATASET.agents, AgentRecord(id="newcomer", type="claude-code")),
    )
    fake.tool_calls.clear()

    checked = _run(registry.ensure_registered(fake, "newcomer"))

    assert not checked.ok
    assert checked.reason == REFUSAL_BELONGS_TO_AGENT
    # The seam did its OWN lookup, and registered nothing.
    assert fake.tool_calls == [("lithos_agent_info", {"id": "newcomer"})]


def test_a_failed_registration_leaves_no_acceptance_behind_either() -> None:
    """Same rule on the retry path: a registration that failed has verified
    nothing durable, so the next write re-reads the registry rather than
    trusting the lookup that preceded the failure."""
    fake, registry = _fake(), _registry()
    fake.register_operator_fails = True
    assert not _run(registry.ensure_registered(fake, "newcomer")).ok

    fake.dataset = replace(
        GUARD_DATASET,
        agents=(*GUARD_DATASET.agents, AgentRecord(id="newcomer", type="claude-code")),
    )
    fake.register_operator_fails = False
    fake.tool_calls.clear()

    checked = _run(registry.ensure_registered(fake, "newcomer"))

    assert not checked.ok
    assert checked.reason == REFUSAL_BELONGS_TO_AGENT
    assert _registrations(fake) == []


class _BarrierClient(FakeLithosClient):
    """A fake whose lookup blocks until ``waiters`` calls have reached it.

    Deterministic, not timing-based: two ``ensure_registered`` calls are made
    to overlap for real (a double submit, two tabs — both in the operational
    model) by holding the first inside its Lithos call until the second has
    started, which is exactly the interleaving a check-then-act ledger loses.
    If the registry serialises properly the second never reaches the lookup,
    so the barrier is released once the arrival count is reached OR the gather
    completes — hence the release on the first arrival below.
    """

    def __init__(self) -> None:
        super().__init__(None, dataset=GUARD_DATASET)
        self.started = asyncio.Event()
        self.may_proceed = asyncio.Event()

    async def agent_info(self, agent_id: str):
        self.started.set()
        await self.may_proceed.wait()
        return await super().agent_info(agent_id)


def test_two_concurrent_first_writes_register_the_identity_once() -> None:
    """Register-once is a CONCURRENCY claim, not just a sequential one.

    Two first writes for one identity — a double submit, or two tabs — must
    produce exactly one ``lithos_agent_register``. Unserialised, both observe
    an empty ledger, both look the id up and both register, which is the
    check-then-act race this pins out.
    """
    fake, registry = _BarrierClient(), _registry()

    async def _driver() -> list:
        first = asyncio.create_task(registry.ensure_registered(fake, "newcomer"))
        second = asyncio.create_task(registry.ensure_registered(fake, "newcomer"))
        # Both tasks are started and the leader is parked inside its lookup
        # before either is allowed to finish: no sleeps, no wall clock.
        await fake.started.wait()
        await asyncio.sleep(0)
        fake.may_proceed.set()
        return list(await asyncio.gather(first, second))

    results = _run(_driver())

    assert all(result.ok for result in results), "both writers must be cleared"
    assert _registrations(fake) == [
        ("lithos_agent_register", {"id": "newcomer", "type": "human"})
    ]
    assert fake.tool_calls == [
        ("lithos_agent_info", {"id": "newcomer"}),
        ("lithos_agent_register", {"id": "newcomer", "type": "human"}),
    ]


def test_a_failed_registration_refuses_the_write() -> None:
    fake, registry = _fake(), _registry()
    fake.register_operator_fails = True

    checked = _run(registry.ensure_registered(fake, "newcomer"))

    assert not checked.ok
    assert checked.code == "registration_failed"
    assert checked.reason == REFUSAL_REGISTRATION_FAILED
    # §5C.5's own sentence, literal rather than by constant.
    assert (
        checked.reason.lower()
        == "could not register the operator identity; nothing was changed."
    )
    # And it is not remembered as registered: the next write tries again.
    fake.register_operator_fails = False
    assert _run(registry.ensure_registered(fake, "newcomer")).ok


def test_a_default_operator_naming_an_agent_is_refused_at_the_seam() -> None:
    """The guard is part of ensure_registered, not of the operator page, so it
    covers an identity that never passed through a form: a configured
    ``default_operator`` holding an agent's id (PRD clarification 1)."""
    config = _config_with_writes(default_operator="agent-zero")
    fake, registry = _fake(), OperatorRegistry(service_agent_id=config.lithos.agent_id)
    identity = resolve_operator(
        cookie=None, default_operator=config.writes.default_operator
    )
    assert identity.source == "default"

    checked = _run(registry.ensure_registered(fake, identity.id))

    assert not checked.ok
    assert checked.reason == REFUSAL_BELONGS_TO_AGENT
    assert _registrations(fake) == []


def test_an_invalid_identity_never_reaches_lithos() -> None:
    fake, registry = _fake(), _registry()

    checked = _run(registry.ensure_registered(fake, "Not An Id"))

    assert not checked.ok
    assert checked.code == "invalid_id"
    assert fake.tool_calls == []


# ── the routes ─────────────────────────────────────────────────────────


def _config_with_writes(*, default_operator: str = "", tmp_path: Path | None = None):
    """A loaded config whose only non-default section is ``[writes]``."""
    import tempfile

    directory = tmp_path or Path(tempfile.mkdtemp())
    config_path = directory / "lithos-lens.toml"
    config_path.write_text(
        dedent(
            f"""
            [lithos-lens]
            environment = "test"

            [lithos-lens.lithos]
            agent_id = "{SERVICE_AGENT_ID}"

            [lithos-lens.writes]
            default_operator = "{default_operator}"
            """
        )
    )
    return load_config(config_path)


class _EveryCallLogged(FakeLithosClient):
    """The guard fake, logging EVERY client method a request reaches.

    ``tool_calls`` records only the tools the identity path uses, so a handler
    that read ``stats`` or ``list_tasks`` before refusing would leave it empty.
    "403 before ANY Lithos call" needs the whole client surface watched: every
    public coroutine method is logged by name as it is called, whichever it is
    and whether or not the fake records it as a tool call.
    """

    def __init__(self) -> None:
        super().__init__(None, dataset=GUARD_DATASET)
        self.method_calls: list[str] = []

    def __getattribute__(self, name: str):
        attr = super().__getattribute__(name)
        if name.startswith("_") or not inspect.iscoroutinefunction(attr):
            return attr
        log = super().__getattribute__("method_calls")

        @functools.wraps(attr)
        async def logged(*args, **kwargs):
            log.append(name)
            return await attr(*args, **kwargs)

        return logged


def _client(
    *, default_operator: str = "", fake: FakeLithosClient | None = None
) -> tuple[TestClient, FakeLithosClient]:
    lithos = fake if fake is not None else _EveryCallLogged()
    config = _config_with_writes(default_operator=default_operator)
    app = create_app(config, lithos_client_factory=lambda _: lithos)
    return TestClient(app, base_url="http://lens.test"), lithos


def _refused_post(headers: dict[str, str]):
    """POST a valid identity under ``headers``; return the response and every
    client method the request reached (startup's own calls cleared first, and
    the log read before shutdown's ``close`` lands in it)."""
    client, fake = _client()
    assert isinstance(fake, _EveryCallLogged)
    with client:
        fake.tool_calls.clear()
        fake.method_calls.clear()
        response = client.post(
            "/operator",
            data={"operator": "newcomer"},
            headers=headers,
            follow_redirects=False,
        )
        reached = list(fake.method_calls)
        tools = list(fake.tool_calls)
    return response, reached, tools


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"Origin": "http://evil.example"}, id="foreign-origin"),
        # Clarification 4's acceptance addition: same hostname, other port.
        pytest.param({"Origin": "http://lens.test:8001"}, id="same-host-other-port"),
        # TestClient sends no Origin and no Referer of its own: "no evidence".
        pytest.param({}, id="neither-header"),
        pytest.param({"Referer": "http://evil.example/page"}, id="foreign-referer"),
        # Present but EMPTY, beside a same-host Referer: the Referer is the
        # fallback for an ABSENT Origin only, so this is a mismatch.
        pytest.param(
            {"Origin": "", "Referer": "http://lens.test/tasks"},
            id="empty-origin-with-same-host-referer",
        ),
        pytest.param({"Origin": "null"}, id="opaque-origin"),
    ],
)
def test_a_cross_origin_post_is_refused_before_any_lithos_call(
    lithos_lens_config_env: Path, headers: dict[str, str]
) -> None:
    response, reached, tools = _refused_post(headers)

    assert response.status_code == 403
    assert "Nothing was changed" in response.text
    assert "set-cookie" not in response.headers
    # The WHOLE client surface, not only the tools the fake records.
    assert reached == []
    assert tools == []


def test_the_refusal_recorder_would_see_a_read_before_the_check() -> None:
    """The recorder above is what makes "no Lithos call" mean ANY call: a read
    the fake does not log as a tool call still lands in ``method_calls``."""
    fake = _EveryCallLogged()
    _run(fake.stats())
    assert fake.method_calls == ["stats"]
    assert fake.tool_calls == []


def test_an_empty_origin_is_not_an_absent_one(
    lithos_lens_config_env: Path,
) -> None:
    """The same Referer that is accepted when Origin is ABSENT is refused when
    an Origin header is PRESENT and empty — presence decides which header is
    read (clarification 4: a value that does not parse is a mismatch)."""
    absent, absent_reached, _ = _refused_post({"Referer": "http://lens.test/tasks"})
    empty, empty_reached, _ = _refused_post(
        {"Origin": "", "Referer": "http://lens.test/tasks"}
    )

    assert absent.status_code == 303
    assert "lens_operator=newcomer" in absent.headers["set-cookie"]
    assert absent_reached == ["agent_info"]
    assert empty.status_code == 403
    assert "set-cookie" not in empty.headers
    assert empty_reached == []


def test_the_operator_page_states_the_identity_its_source_and_the_boundary(
    lithos_lens_config_env: Path,
) -> None:
    client, _ = _client()
    client.cookies.set(OPERATOR_COOKIE_NAME, "dave")
    with client:
        response = client.get("/operator")

    assert response.status_code == 200
    body = _page_text(response)
    assert 'data-operator-source="cookie"' in body
    assert "chosen in this browser" in body
    assert "data-operator-id>dave<" in body
    # REQUIREMENTS §5C.1's statement, in the words the requirement uses.
    assert "no authentication or authorization" in body
    assert "Anyone who can\n      reach this port can perform these actions" in body
    assert "hygiene, not security" in body


def test_the_operator_page_names_the_configured_default_as_the_source(
    lithos_lens_config_env: Path,
) -> None:
    client, _ = _client(default_operator="configured-dave")
    with client:
        response = client.get("/operator")

    assert 'data-operator-source="default"' in response.text
    assert "data-operator-id>configured-dave<" in response.text


def test_setting_an_identity_sets_an_attribution_cookie_and_returns_the_operator(
    lithos_lens_config_env: Path,
) -> None:
    client, fake = _client()
    with client:
        # Startup's own service registration (type web-ui) is in the log; the
        # assertions below are about the OPERATOR registration.
        fake.tool_calls.clear()
        response = client.post(
            "/operator",
            data={"operator": "dave", "next": "/tasks?project=lithos-loom"},
            headers={"Origin": "http://lens.test"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/tasks?project=lithos-loom"
    cookie = response.headers["set-cookie"]
    assert "lens_operator=dave" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie.replace("Samesite", "SameSite")
    # One year, stated independently of the constant the handler reads, so a
    # changed constant cannot carry the expectation along with it.
    assert "Max-Age=31536000" in cookie
    assert OPERATOR_COOKIE_MAX_AGE_S == 31536000
    # Deliberately NOT Secure: Lens serves plain HTTP, so a Secure cookie
    # would never be stored (PRD clarification 5).
    assert "Secure" not in cookie
    # The page's up-front check is the lookup, not a registration: that
    # happens before the first WRITE.
    assert _registrations(fake) == []


def test_a_next_pointing_at_another_origin_is_ignored(
    lithos_lens_config_env: Path,
) -> None:
    client, _ = _client()
    with client:
        response = client.post(
            "/operator",
            data={"operator": "dave", "next": "//evil.example/steal"},
            headers={"Origin": "http://lens.test"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/operator"


def test_an_id_that_belongs_to_an_agent_is_refused_on_the_page(
    lithos_lens_config_env: Path,
) -> None:
    client, fake = _client()
    with client:
        fake.tool_calls.clear()
        response = client.post(
            "/operator",
            data={"operator": "agent-zero"},
            headers={"Origin": "http://lens.test"},
        )

    assert response.status_code == 400
    assert REFUSAL_BELONGS_TO_AGENT in _page_text(response)
    assert "set-cookie" not in response.headers
    assert _registrations(fake) == []
    # The form keeps what was typed, so one field is corrected rather than
    # the whole form retyped.
    assert 'value="agent-zero"' in response.text


def test_the_service_agents_own_id_is_refused_on_the_page(
    lithos_lens_config_env: Path,
) -> None:
    client, fake = _client()
    with client:
        fake.tool_calls.clear()
        response = client.post(
            "/operator",
            data={"operator": SERVICE_AGENT_ID},
            headers={"Origin": "http://lens.test"},
        )

    assert response.status_code == 400
    assert REFUSAL_SERVICE_AGENT in _page_text(response)
    assert fake.tool_calls == []


def test_an_invalid_id_re_renders_the_form_with_the_rule(
    lithos_lens_config_env: Path,
) -> None:
    client, _ = _client()
    with client:
        response = client.post(
            "/operator",
            data={"operator": "Dave Smith"},
            headers={"Origin": "http://lens.test"},
        )

    assert response.status_code == 400
    assert "lowercase letters, digits and dashes" in response.text
    assert "set-cookie" not in response.headers


def test_the_chrome_offers_one_choose_link_when_no_identity_resolves(
    lithos_lens_config_env: Path,
) -> None:
    client, _ = _client()
    with client:
        response = client.get("/tasks")

    body = response.text
    assert 'data-operator-resolved="no"' in body
    assert "Acting as" not in body
    # ONE affordance, and it is an ANCHOR leading to the operator page with
    # this page as its return trip — not merely the words somewhere on it.
    links = re.findall(
        r'<a class="operator-chip-choose" href="([^"]+)">choose an operator to act</a>',
        body,
    )
    assert links == ["/operator?next=%2Ftasks"]


def test_the_chrome_names_the_identity_and_offers_a_switch_back_here(
    lithos_lens_config_env: Path,
) -> None:
    client, _ = _client()
    client.cookies.set(OPERATOR_COOKIE_NAME, "dave")
    with client:
        response = client.get("/tasks?project=lithos-loom")

    body = response.text
    assert 'data-operator-resolved="yes"' in body
    assert "Acting as" in body
    assert "choose an operator to act" not in body
    # The switch link carries this page back as its return trip.
    assert "/operator?next=%2Ftasks%3Fproject%3Dlithos-loom" in body


def test_an_over_budget_filter_query_is_not_reflected_into_the_switch_link(
    lithos_lens_config_env: Path,
) -> None:
    """The chip is in the chrome of the refusal page too, and that page refuses
    to reflect the query it is rejecting (`MAX_FILTER_QUERY_BYTES`). So the
    return trip carries the path alone there — a link back to a page that would
    be refused is not a return trip."""
    from lithos_lens.tasks import MAX_FILTER_QUERY_BYTES

    oversized = "x" * (MAX_FILTER_QUERY_BYTES + 1)
    client, _ = _client()
    with client:
        response = client.get("/tasks", params={"status": oversized})

    assert response.status_code == 400
    assert oversized not in response.text
    assert "/operator?next=%2Ftasks" in response.text


@pytest.mark.parametrize(
    "submitted", [" dave", "dave ", " dave ", "\tdave", "Dave Smith", "dave_smith"]
)
def test_a_submitted_id_that_is_not_the_exact_slug_is_refused(
    lithos_lens_config_env: Path, submitted: str
) -> None:
    """Clarification 6: the submitted value ITSELF must match the id rule.

    Trimming first would accept " dave " by quietly writing a different value
    than the one submitted — the one case where being liberal in what is
    accepted sets an identity the operator did not type.
    """
    client, fake = _client()
    with client:
        fake.tool_calls.clear()
        response = client.post(
            "/operator",
            data={"operator": submitted},
            headers={"Origin": "http://lens.test"},
        )

    assert response.status_code == 400
    assert "lowercase letters, digits and dashes" in _page_text(response)
    assert "set-cookie" not in response.headers
    assert fake.tool_calls == []


def _hidden_next(page_html: str) -> list[str]:
    return re.findall(r'<input type="hidden" name="next" value="([^"]*)">', page_html)


def test_the_chip_link_carries_the_return_trip_through_the_form_and_back(
    lithos_lens_config_env: Path,
) -> None:
    """The whole choose journey as a browser takes it: the chrome's link, the
    form that link renders, and the POST that form makes. Each hop reads the
    previous one's OUTPUT, so a GET that dropped ``next`` breaks the chain
    rather than being bypassed by a hand-built POST."""
    client, _ = _client()
    with client:
        board = client.get("/tasks?project=lithos-loom")
        (link,) = re.findall(
            r'<a class="operator-chip-choose" href="([^"]+)">', board.text
        )
        page = client.get(html.unescape(link))
        hidden = [html.unescape(v) for v in _hidden_next(page.text)]
        assert hidden == ["/tasks?project=lithos-loom"]

        response = client.post(
            "/operator",
            data={"operator": "dave", "next": hidden[0]},
            headers={"Origin": "http://lens.test"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/tasks?project=lithos-loom"


@pytest.mark.parametrize(
    "rejected", ["//evil.example/steal", "http://evil.example/", "/\\evil.example"]
)
def test_a_get_next_pointing_off_this_lens_never_reaches_the_form(
    lithos_lens_config_env: Path, rejected: str
) -> None:
    client, _ = _client()
    with client:
        page = client.get("/operator", params={"next": rejected})
        # Exactly what the rendered form submits: its hidden `next`, if any.
        form = {"operator": "dave"}
        for value in _hidden_next(page.text):
            form["next"] = html.unescape(value)
        response = client.post(
            "/operator",
            data=form,
            headers={"Origin": "http://lens.test"},
            follow_redirects=False,
        )

    assert page.status_code == 200
    assert _hidden_next(page.text) == []
    assert response.headers["location"] == "/operator"


def test_a_digit_leading_id_is_accepted_on_the_page(
    lithos_lens_config_env: Path,
) -> None:
    """The form field obeys the same rule as the cookie: a digit may lead."""
    client, _ = _client()
    with client:
        response = client.post(
            "/operator",
            data={"operator": "0-person"},
            headers={"Origin": "http://lens.test"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "lens_operator=0-person" in response.headers["set-cookie"]


def test_a_refusal_keeps_the_return_trip_the_form_carried(
    lithos_lens_config_env: Path,
) -> None:
    """Correcting a refused id must still land the operator where they came
    from: the destination rides in the FORM on a POST, so a refusal that
    re-read only the query string would silently drop it."""
    client, _ = _client()
    with client:
        refused = client.post(
            "/operator",
            data={"operator": "agent-zero", "next": "/tasks?project=lithos-loom"},
            headers={"Origin": "http://lens.test"},
        )
        assert refused.status_code == 400
        assert (
            '<input type="hidden" name="next" value="/tasks?project=lithos-loom">'
            in _page_text(refused)
        )

        corrected = client.post(
            "/operator",
            data={"operator": "dave", "next": "/tasks?project=lithos-loom"},
            headers={"Origin": "http://lens.test"},
            follow_redirects=False,
        )

    assert corrected.status_code == 303
    assert corrected.headers["location"] == "/tasks?project=lithos-loom"


def test_a_refusal_drops_a_return_trip_pointing_off_this_lens(
    lithos_lens_config_env: Path,
) -> None:
    """The form field is no more trusted on the refusal path than on the
    redirect one: it goes through the same ``safe_next``."""
    client, _ = _client()
    with client:
        refused = client.post(
            "/operator",
            data={"operator": "agent-zero", "next": "//evil.example/steal"},
            headers={"Origin": "http://lens.test"},
        )

    assert refused.status_code == 400
    assert "evil.example" not in refused.text


def test_a_same_host_referer_is_accepted_when_no_origin_is_sent(
    lithos_lens_config_env: Path,
) -> None:
    """The documented fallback, exercised through the route rather than only
    through the pure helper: a handler that stopped passing ``Referer`` on
    would otherwise leave every test green."""
    client, _ = _client()
    with client:
        response = client.post(
            "/operator",
            data={"operator": "dave"},
            headers={"Referer": "http://lens.test/tasks?project=x"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "lens_operator=dave" in response.headers["set-cookie"]


def test_the_write_surface_is_always_on_whatever_the_config_says(
    lithos_lens_config_env: Path, tmp_path: Path
) -> None:
    """D2 / REQUIREMENTS §5C.1: there is no read-only mode and no `enabled`
    key. A deployment that writes NO ``[writes]`` table at all — and one that
    writes the flag the requirement dropped — gets the same surface: the
    operator page is registered and the chrome offers the identity affordance.
    What decides whether an affordance renders is the task's state and whether
    an identity resolves, never configuration.
    """
    config_path = tmp_path / "lithos-lens.toml"
    config_path.write_text(
        dedent(
            f"""
            [lithos-lens]
            environment = "test"

            [lithos-lens.lithos]
            agent_id = "{SERVICE_AGENT_ID}"
            """
        )
    )
    no_table = load_config(config_path)
    config_path.write_text(
        config_path.read_text() + "\n[lithos-lens.writes]\nenabled = false\n"
    )
    with_dead_flag = load_config(config_path)

    for config in (no_table, with_dead_flag):
        # The knob does not exist, so nothing can read it off the config.
        assert not hasattr(config.writes, "enabled")
        assert config.writes.default_operator == ""
        app = create_app(config, lithos_client_factory=lambda _: _fake())
        with TestClient(app, base_url="http://lens.test") as client:
            assert client.get("/operator").status_code == 200
            board = client.get("/tasks")
        assert "data-operator-chip" in board.text
        assert "choose an operator to act" in board.text


def test_tasks_new_is_never_routed_as_a_task_id(
    lithos_lens_config_env: Path,
) -> None:
    """``new`` is a reserved task-path segment (§5C.7): the path is the create
    form (T3-W7) — never the detail page of a task whose id happens to be
    ``new``. The id itself stays addressable through the existing id-in-query
    alias."""
    assert "new" in RESERVED_TASK_PATH_SEGMENTS
    fake = FakeLithosClient(
        None,
        dataset=FakeLithosDataset(
            tasks=(_task_called_new(),), ready_ids=frozenset({"new"})
        ),
    )
    client, _ = _client(fake=fake)
    with client:
        by_path = client.get("/tasks/new")
        by_alias = client.get("/tasks/id?task_id=new")

    assert by_path.status_code == 200
    assert "data-create-page" in by_path.text
    assert "A task genuinely called new" not in by_path.text
    # The same id, reached the way every page word is reached.
    assert by_alias.status_code == 200
    assert "A task genuinely called new" in by_alias.text


# ── the deployment surface (the example file and the container) ────────
#
# None of this is reachable from a request, so nothing else in the suite would
# notice it going missing — and each piece is a documented part of this slice:
# the shipped example states the knobs, and the container passes the default
# operator through (compose uses the env file for SUBSTITUTION only and hands
# the container a fixed list, so a missing line here is a knob that silently
# does nothing in production).

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_the_shipped_example_config_states_the_writes_knobs() -> None:
    import tomllib

    example = REPO_ROOT / "lithos-lens.example.toml"
    parsed = tomllib.loads(example.read_text(encoding="utf-8"))
    writes = parsed["lithos-lens"]["writes"]

    # Exactly the two knobs, at their shipped defaults — and NO `enabled`,
    # which is the key REQUIREMENTS §5C.1 dropped (D2).
    assert writes == {"default_operator": "", "confirm_cancel": True}
    # …and the example's own values must load, not merely parse.
    config_path = Path(example)
    assert load_config(config_path).writes.default_operator == ""


def test_the_container_passes_the_default_operator_through() -> None:
    """The env file is substitution-only: a variable absent from compose's
    `environment:` list never reaches the process."""
    compose = (REPO_ROOT / "docker" / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (REPO_ROOT / "docker" / ".env.example").read_text(encoding="utf-8")

    assert (
        "- LITHOS_LENS_WRITES_DEFAULT_OPERATOR="
        "${LITHOS_LENS_WRITES_DEFAULT_OPERATOR:-}" in compose
    )
    # The env-file template documents the variable compose substitutes from.
    assert "LITHOS_LENS_WRITES_DEFAULT_OPERATOR" in env_example


def _task_called_new():
    from lithos_lens.tasks import TaskRecord

    return TaskRecord(
        id="new",
        title="A task genuinely called new",
        description="",
        status="open",
        created_by="planner",
        created_at="2026-09-01T09:00:00+00:00",
        tags=("project:lithos-loom",),
        metadata={},
        task_type="task",
    )
