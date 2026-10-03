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
import html
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
        ("http://lens.lan:8000", "", "lens.lan:8000", True),
        ("http://lens.lan", "", "lens.lan:80", True),
        ("http://lens.lan:80", "", "lens.lan", True),
        ("http://LENS.LAN:8000", "", "lens.lan:8000", True),
        ("https://lens.lan", "", "lens.lan:443", True),
        # A DIFFERENT PORT on the same hostname is a different origin.
        ("http://lens.lan:8001", "", "lens.lan:8000", False),
        ("http://lens.lan", "", "lens.lan:8000", False),
        # A different host.
        ("http://evil.example", "", "lens.lan:8000", False),
        # Referer is the fallback only when Origin is ABSENT.
        ("", "http://lens.lan:8000/tasks?project=x", "lens.lan:8000", True),
        ("", "http://evil.example/page", "lens.lan:8000", False),
        # Present but unusable: no benefit of the doubt, and no fallback to a
        # Referer that would then be the sender's choice of evidence.
        ("null", "http://lens.lan:8000/tasks", "lens.lan:8000", False),
        ("not a url", "", "lens.lan:8000", False),
        ("http://lens.lan:notaport", "", "lens.lan:8000", False),
        ("file:///tmp/x.html", "", "lens.lan:8000", False),
        # Neither header at all.
        ("", "", "lens.lan:8000", False),
        # No Host to compare against.
        ("http://lens.lan:8000", "", "", False),
    ],
)
def test_same_origin_compares_host_and_port(
    origin: str, referer: str, host: str, expected: bool
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
        (None, "configured", "configured", "default"),
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
    "value", ["dave", "dave-smith", "d", "a1", "operator-2", "d" * 63]
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


def test_an_identity_already_verified_survives_a_failed_lookup() -> None:
    """The other half of that rule: an operator mid-session keeps working when
    Lithos goes away, because the id was cleared and registered earlier in this
    process."""
    fake, registry = _fake(), _registry()
    assert _run(registry.ensure_registered(fake, "dave")).ok
    fake.agent_info_error = LithosToolError("lithos is down", code="timeout")

    assert _run(registry.ensure_registered(fake, "dave")).ok


def test_a_failed_registration_refuses_the_write() -> None:
    fake, registry = _fake(), _registry()
    fake.register_operator_fails = True

    checked = _run(registry.ensure_registered(fake, "newcomer"))

    assert not checked.ok
    assert checked.code == "registration_failed"
    assert checked.reason == REFUSAL_REGISTRATION_FAILED
    assert "nothing was changed" in checked.reason
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


def _client(
    *, default_operator: str = "", fake: FakeLithosClient | None = None
) -> tuple[TestClient, FakeLithosClient]:
    lithos = fake if fake is not None else _fake()
    config = _config_with_writes(default_operator=default_operator)
    app = create_app(config, lithos_client_factory=lambda _: lithos)
    return TestClient(app, base_url="http://lens.test"), lithos


def test_a_post_from_a_foreign_origin_is_refused_with_no_lithos_call(
    lithos_lens_config_env: Path,
) -> None:
    client, fake = _client()
    with client:
        fake.tool_calls.clear()
        response = client.post(
            "/operator",
            data={"operator": "dave"},
            headers={"Origin": "http://evil.example"},
        )

    assert response.status_code == 403
    assert "Nothing was changed" in response.text
    assert fake.tool_calls == []


def test_a_post_with_the_same_host_on_another_port_is_refused(
    lithos_lens_config_env: Path,
) -> None:
    client, fake = _client()
    with client:
        fake.tool_calls.clear()
        response = client.post(
            "/operator",
            data={"operator": "dave"},
            headers={"Origin": "http://lens.test:8001"},
        )

    assert response.status_code == 403
    assert fake.tool_calls == []


def test_a_post_with_neither_origin_nor_referer_is_refused(
    lithos_lens_config_env: Path,
) -> None:
    client, fake = _client()
    with client:
        fake.tool_calls.clear()
        # TestClient sends no Origin of its own; blanking Referer keeps both
        # headers out, which is the "no evidence" case.
        response = client.post("/operator", data={"operator": "dave"})

    assert response.status_code == 403
    assert fake.tool_calls == []


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
    assert f"Max-Age={OPERATOR_COOKIE_MAX_AGE_S}" in cookie
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
    assert body.count("choose an operator to act") == 1
    assert "Acting as" not in body


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


def test_tasks_new_is_never_routed_as_a_task_id(
    lithos_lens_config_env: Path,
) -> None:
    """``new`` is a reserved task-path segment (§5C.7): the create form W7 adds
    lives there, and until it does the path is a 404 — never the detail page of
    a task whose id happens to be ``new``. The id itself stays addressable
    through the existing id-in-query alias."""
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

    assert by_path.status_code == 404
    assert "not a task" in by_path.text
    # The same id, reached the way every page word is reached.
    assert by_alias.status_code == 200
    assert "A task genuinely called new" in by_alias.text


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
