"""K4 — 👍 / 👎 on an Influx note for one of its profiles (REQUIREMENTS §10).

Driven through the real routes against the shipped fake, whose note write
(``FakeNoteWrites``) reproduces the vendored ``lithos_note_update`` payloads and
whose notes are the vendored ``lithos_read`` Influx note, normalized by the
real client's normalizer. Asserted on what a request returns and on what the
fake recorded — its write log (the exact arguments the real client would have
sent) and its reads — never on internals.

The canonical write is checked against the contract itself: posting 👎 for
``ai-coding`` on the vendored note must send EXACTLY the vendored canonical
request, which is that note's tags byte-for-byte with the verdict appended,
under the operator, at the version the read returned.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterator
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from fastapi.testclient import TestClient

from lithos_lens.config import load_config
from lithos_lens.fake_dataset import FakeLithosDataset
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.lithos_client import LithosToolError
from lithos_lens.normalizers import normalize_note
from lithos_lens.note_feedback import apply_verdict, note_feedback, note_profiles
from lithos_lens.note_writes import NoteUpdateResult, normalize_note_update
from lithos_lens.operator import OPERATOR_COOKIE_NAME
from lithos_lens.tasks import NoteRecord
from lithos_lens.web import create_app
from lithos_lens.write_funnel import AUDIT_EVENT
from tests.conftest import load_contract

ORIGIN = "http://lens.test"
SAME_ORIGIN = {"Origin": ORIGIN}
OPERATOR = "dave"
NOTHING_CHANGED = "Nothing was changed."

_READ = load_contract("lithos_read")["responses"]["variants"]["influx_archive_terminal"]
_UPDATE = load_contract("lithos_note_update")

#: The vendored Influx note: profile ai-coding, version 2.
INFLUX_ID = _READ["id"]
INFLUX_PATH = _READ["path"]
INFLUX_TAGS = tuple(_READ["metadata"]["tags"])

#: An ingested note in two profiles, already 👍 for one of them, with tags
#: around the verdict that must survive a patch in place (a duplicate too).
TWO_ID = "33333333-3333-4333-8333-333333333333"
TWO_TAGS = (
    "profile:ai-coding",
    "influx:accepted:ai-coding",
    "source:blog",
    "profile:robotics",
    "ingested-by:influx",
    "source:blog",
    "Mixed Case: kept",
)

#: An ingested note in no profile: nothing to give feedback for.
BARE_ID = "44444444-4444-4444-8444-444444444444"


def _influx_note(note_id: str = INFLUX_ID, **overrides: Any) -> NoteRecord:
    """The vendored note through the real normalizer, frontmatter overridden."""
    metadata = {**_READ["metadata"], "id": note_id, **overrides}
    return normalize_note({**_READ, "id": note_id, "metadata": metadata})


def _dataset() -> FakeLithosDataset:
    notes = {
        INFLUX_ID: _influx_note(),
        TWO_ID: _influx_note(TWO_ID, tags=list(TWO_TAGS), version=7),
        BARE_ID: _influx_note(
            BARE_ID, tags=["source:blog", "ingested-by:influx"], version=1
        ),
    }
    return FakeLithosDataset(
        notes=notes,
        note_paths={
            INFLUX_PATH: INFLUX_ID,
            "articles/blog/two.md": TWO_ID,
            "articles/blog/bare.md": BARE_ID,
        },
    )


class FeedbackFake(FakeLithosClient):
    """The fake, recording each note read with its ``max_length``."""

    def __init__(self) -> None:
        super().__init__(None, dataset=_dataset())
        self.note_reads: list[tuple[str, int | None]] = []
        self.update_error: Exception | None = None
        #: Scripted outcomes of the next note reads, in order: an exception
        #: makes that read fail (Lithos briefly unavailable), None lets it
        #: through. Empty: every read answers.
        self.read_errors: list[Exception | None] = []

    async def read_note(
        self, knowledge_id: str, *, max_length: int | None = None
    ) -> NoteRecord | None:
        self.note_reads.append((knowledge_id, max_length))
        if self.read_errors:
            error = self.read_errors.pop(0)
            if error is not None:
                raise error
        return await super().read_note(knowledge_id, max_length=max_length)

    async def note_update(
        self,
        note_id: str,
        *,
        agent: str,
        tags: tuple[str, ...] | list[str],
        expected_version: int,
    ) -> NoteUpdateResult:
        if self.update_error is not None:
            self.write_calls.append(("lithos_note_update", {"id": note_id}))
            raise self.update_error
        return await super().note_update(
            note_id, agent=agent, tags=tags, expected_version=expected_version
        )

    def updates(self) -> list[dict[str, Any]]:
        return [args for tool, args in self.write_calls if tool == "lithos_note_update"]

    def tags_of(self, note_id: str) -> tuple[str, ...]:
        return self.effective_notes()[note_id].tags


@pytest.fixture
def config(lithos_lens_config_env: Path):
    return load_config(lithos_lens_config_env)


@pytest.fixture
def fake() -> FeedbackFake:
    return FeedbackFake()


@pytest.fixture
def client(config, fake: FeedbackFake) -> Iterator[TestClient]:
    app = create_app(config, lithos_client_factory=lambda _: fake)
    with TestClient(app, base_url=ORIGIN) as test_client:
        test_client.cookies.set(OPERATOR_COOKIE_NAME, OPERATOR)
        fake.note_reads.clear()
        fake.write_calls.clear()
        yield test_client


def _post(
    client: TestClient,
    note_id: str,
    verdict: str,
    profile: str = "ai-coding",
    *,
    next_url: str = "/knowledge",
    headers: dict[str, str] | None = None,
):
    return client.post(
        f"/note/{quote(note_id, safe='')}/feedback",
        data={"profile": profile, "verdict": verdict, "next": next_url},
        headers=SAME_ORIGIN if headers is None else headers,
        follow_redirects=False,
    )


def _text(html: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def _forms(html: str, note_id: str) -> list[str]:
    """Every feedback form for ``note_id`` on the page, as markup."""
    action = re.escape(f'action="/note/{note_id}/feedback"')
    return re.findall(
        rf'<form class="feedback-action"[^>]*{action}.*?</form>', html, re.S
    )


class _FormParser(HTMLParser):
    """One rendered form's method, action, hidden inputs and submit buttons."""

    def __init__(self) -> None:
        super().__init__()
        self.method = ""
        self.action = ""
        self.fields: dict[str, str] = {}
        self.submits: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = {key: value or "" for key, value in attrs}
        if tag == "form":
            self.method = found.get("method", "get").lower()
            self.action = found.get("action", "")
        elif tag == "input" and found.get("name"):
            self.fields[found["name"]] = found.get("value", "")
        elif tag == "button" and found.get("type", "submit") == "submit":
            self.submits.append((found.get("name", ""), found.get("value", "")))


def _click(client: TestClient, form: str, verdict: str):
    """Submit ``form`` as a browser would on a click of its ``verdict`` button:
    its own method and action, its successful controls, and that button."""
    parsed = _FormParser()
    parsed.feed(form)
    assert parsed.method == "post"
    [button] = [pair for pair in parsed.submits if pair[1] == verdict]
    return client.post(
        unescape(parsed.action),
        data={**parsed.fields, button[0]: button[1]},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )


def _buttons(form: str) -> dict[str, str]:
    """verdict -> aria-pressed, for one form's buttons."""
    return dict(
        re.findall(
            r'name="verdict" value="(\w+)".*?aria-pressed="(true|false)"', form, re.S
        )
    )


# ── the tag patch ─────────────────────────────────────────────────────────


def test_thumbs_down_sends_the_vendored_canonical_patch(
    client: TestClient, fake: FeedbackFake
) -> None:
    """👎 on the vendored note sends exactly the contract's canonical request:
    every tag as read, byte-for-byte and in order, plus the verdict; the
    operator as ``agent``; the read's version as ``expected_version``."""
    response = _post(client, INFLUX_ID, "rejected")

    assert response.status_code == 303
    assert fake.updates() == [_UPDATE["request"]["canonical"]]
    assert fake.updates()[0]["agent"] == OPERATOR
    assert fake.updates()[0]["expected_version"] == _READ["metadata"]["version"]
    # The pre-check is the truncated read: tags and version, not the body.
    assert fake.note_reads == [(INFLUX_ID, 1)]
    assert fake.tags_of(INFLUX_ID) == (*INFLUX_TAGS, "influx:rejected:ai-coding")


def test_thumbs_up_adds_accepted_and_drops_rejected_keeping_the_rest(
    client: TestClient, fake: FeedbackFake
) -> None:
    _post(client, INFLUX_ID, "rejected")
    fake.write_calls.clear()

    response = _post(client, INFLUX_ID, "accepted")

    assert response.status_code == 303
    [sent] = fake.updates()
    assert sent["tags"] == [*INFLUX_TAGS, "influx:accepted:ai-coding"]
    assert sent["expected_version"] == _READ["metadata"]["version"] + 1
    assert sent["agent"] == OPERATOR


def test_a_verdict_for_one_profile_leaves_the_other_profile_and_every_tag_alone(
    client: TestClient, fake: FeedbackFake
) -> None:
    """The two-profile note: 👎 for ai-coding replaces ITS 👍 in place of
    nothing — the duplicate, the odd tag and robotics untouched."""
    _post(client, TWO_ID, "rejected", "ai-coding")

    [sent] = fake.updates()
    assert sent["tags"] == [
        "profile:ai-coding",
        "source:blog",
        "profile:robotics",
        "ingested-by:influx",
        "source:blog",
        "Mixed Case: kept",
        "influx:rejected:ai-coding",
    ]
    assert sent["expected_version"] == 7
    fake.write_calls.clear()

    _post(client, TWO_ID, "accepted", "robotics")

    [sent] = fake.updates()
    assert sent["tags"][:-1] == list(fake.tags_of(TWO_ID))[:-1]
    assert sent["tags"][-1] == "influx:accepted:robotics"
    assert "influx:rejected:ai-coding" in sent["tags"]


def test_the_verdict_already_carried_writes_nothing_and_says_so(
    client: TestClient, fake: FeedbackFake
) -> None:
    response = _post(client, TWO_ID, "accepted", "ai-coding", next_url="/knowledge")

    assert response.status_code == 303
    assert fake.updates() == []
    page = client.get(response.headers["location"])
    assert "Already marked" in _text(page.text)
    assert "nothing was written" in _text(page.text)


def test_the_redirect_lands_on_next_with_a_receipt_and_fresh_pressed_state(
    client: TestClient, fake: FeedbackFake
) -> None:
    response = _post(client, INFLUX_ID, "rejected", next_url="/knowledge")

    location = response.headers["location"]
    assert urlsplit(location).path == "/knowledge"
    assert "receipt" in parse_qs(urlsplit(location).query)
    page = client.get(location).text
    text = _text(page)
    assert f"Marked “ {_READ['title']} ” not interesting for profile ai-coding" in text
    assert f"Recorded as {OPERATOR}." in text
    [form] = _forms(page, INFLUX_ID)
    assert _buttons(form) == {"accepted": "false", "rejected": "true"}


# ── version_conflict ──────────────────────────────────────────────────────


def test_a_version_conflict_rereads_and_retries_once_onto_the_new_tags(
    client: TestClient, fake: FeedbackFake
) -> None:
    """An agent tags the note between Lens's read and its write: the first
    patch loses (``version_conflict``), Lens reads again, and the retry keeps
    the agent's tag — it is built from the second read, at its version."""
    raced = (*INFLUX_TAGS, "agent:added-meanwhile")
    fake.pending_note_writes.append(raced)

    response = _post(client, INFLUX_ID, "rejected")

    assert response.status_code == 303
    first, second = fake.updates()
    assert first["expected_version"] == 2
    assert second["expected_version"] == 3
    assert second["tags"] == [*raced, "influx:rejected:ai-coding"]
    assert fake.note_reads == [(INFLUX_ID, 1), (INFLUX_ID, 1)]
    assert fake.tags_of(INFLUX_ID) == (*raced, "influx:rejected:ai-coding")
    page = client.get(response.headers["location"]).text
    assert "Lens read it again and applied your verdict" in _text(page)


def test_a_second_version_conflict_is_the_conflict_page_and_nothing_written(
    client: TestClient, fake: FeedbackFake
) -> None:
    fake.pending_note_writes.extend(
        [(*INFLUX_TAGS, "agent:one"), (*INFLUX_TAGS, "agent:two")]
    )

    response = _post(client, INFLUX_ID, "rejected")

    assert response.status_code == 409
    text = _text(response.text)
    assert NOTHING_CHANGED in text
    assert "The note kept changing while Lens was writing to it." in text
    assert "The note changed" in text
    assert len(fake.updates()) == 2
    assert "influx:rejected:ai-coding" not in fake.tags_of(INFLUX_ID)
    assert f'href="/note/{INFLUX_ID}"' in response.text


# ── who may write ─────────────────────────────────────────────────────────


def test_no_operator_is_sent_to_the_operator_page_and_nothing_is_written(
    config, fake: FeedbackFake
) -> None:
    app = create_app(config, lithos_client_factory=lambda _: fake)
    with TestClient(app, base_url=ORIGIN) as anonymous:
        fake.note_reads.clear()
        response = _post(anonymous, INFLUX_ID, "rejected", next_url="/knowledge")

    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert location.path == "/operator"
    assert parse_qs(location.query)["next"] == ["/knowledge"]
    assert fake.write_calls == []
    assert fake.note_reads == []


def test_a_foreign_origin_post_is_refused_before_any_lithos_call(
    client: TestClient, fake: FeedbackFake
) -> None:
    response = _post(
        client, INFLUX_ID, "rejected", headers={"Origin": "http://evil.test"}
    )

    assert response.status_code == 403
    assert fake.write_calls == []
    assert fake.note_reads == []


def test_each_attempt_leaves_one_audit_line_under_the_operator(
    client: TestClient, fake: FeedbackFake, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="lithos_lens.write_funnel")
    _post(client, INFLUX_ID, "rejected")
    _post(client, INFLUX_ID, "rejected")

    lines = [r for r in caplog.records if getattr(r, "lens_event", "") == AUDIT_EVENT]
    assert [(r.action, r.result, r.operator) for r in lines] == [  # type: ignore[attr-defined]
        ("feedback", "ok", OPERATOR),
        ("feedback", "ok", OPERATOR),
    ]
    assert lines[0].observed_status == "version 2"  # type: ignore[attr-defined]
    assert lines[1].write_unchanged is True  # type: ignore[attr-defined]
    assert lines[0].arguments == {  # type: ignore[attr-defined]
        "note_id": INFLUX_ID,
        "profile": "ai-coding",
        "verdict": "rejected",
    }


# ── refusals ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("profile", "verdict"),
    [("ai-coding", "maybe"), ("", "rejected"), ("ai-coding", "")],
)
def test_a_form_without_a_verdict_and_profile_is_refused_unwritten(
    client: TestClient, fake: FeedbackFake, profile: str, verdict: str
) -> None:
    response = _post(client, INFLUX_ID, verdict, profile)

    assert response.status_code == 400
    assert NOTHING_CHANGED in _text(response.text)
    assert fake.updates() == []


def test_a_profile_the_note_is_not_in_is_refused_unwritten(
    client: TestClient, fake: FeedbackFake
) -> None:
    response = _post(client, BARE_ID, "rejected", "ai-coding")

    assert response.status_code == 409
    assert "This note is no longer in profile ai-coding." in _text(response.text)
    assert fake.updates() == []


def test_a_note_gone_before_the_read_is_the_conflict_page(
    client: TestClient, fake: FeedbackFake
) -> None:
    response = _post(client, "55555555-5555-4555-8555-555555555555", "rejected")

    assert response.status_code == 409
    assert "This note no longer exists." in _text(response.text)
    assert fake.updates() == []


def test_lithos_refusing_the_patch_is_shown_not_swallowed(
    client: TestClient, fake: FeedbackFake
) -> None:
    envelope = next(
        e for e in _UPDATE["responses"]["errors"] if e["code"] == "invalid_input"
    )
    fake.update_error = LithosToolError(
        envelope["message"], code="invalid_input", envelope=envelope
    )

    response = _post(client, INFLUX_ID, "rejected")

    assert response.status_code == 422
    text = _text(response.text)
    assert NOTHING_CHANGED in text
    assert envelope["message"] in text


def test_a_note_deleted_under_the_patch_is_the_conflict_page(
    client: TestClient, fake: FeedbackFake
) -> None:
    envelope = next(
        e
        for e in _UPDATE["responses"]["errors"]
        if e["code"] == "note_not_found" and e["message"].startswith("Note ")
    )
    fake.update_error = LithosToolError(
        envelope["message"], code="note_not_found", envelope=envelope
    )

    response = _post(client, INFLUX_ID, "rejected")

    assert response.status_code == 409
    assert "This note no longer exists." in _text(response.text)


def test_no_answer_is_the_unknown_outcome_with_what_a_reread_shows(
    client: TestClient, fake: FeedbackFake
) -> None:
    fake.update_error = LithosToolError("did not answer within 10s", code="timeout")

    response = _post(client, INFLUX_ID, "rejected")

    assert response.status_code == 200
    text = _text(response.text)
    assert "The action may or may not have applied." in text
    assert "The note does not carry influx:rejected:ai-coding now." in text


# ── where the buttons render ─────────────────────────────────────────────


def test_recent_intake_rows_offer_both_verdicts_per_profile_pressed_as_carried(
    client: TestClient,
) -> None:
    page = client.get("/knowledge").text
    intake = page[page.index('id="recent-intake"') :]

    [influx] = _forms(intake, INFLUX_ID)
    assert 'data-feedback-profile="ai-coding"' in influx
    assert _buttons(influx) == {"accepted": "false", "rejected": "false"}
    two = _forms(intake, TWO_ID)
    assert [re.search(r'data-feedback-profile="([^"]+)"', f)[1] for f in two] == [  # type: ignore[index]
        "ai-coding",
        "robotics",
    ]
    assert _buttons(two[0]) == {"accepted": "true", "rejected": "false"}
    assert _buttons(two[1]) == {"accepted": "false", "rejected": "false"}
    assert _forms(page, BARE_ID) == []


def test_the_note_page_offers_thumbs_down_only_pressed_as_carried(
    client: TestClient,
) -> None:
    _post(client, INFLUX_ID, "rejected")

    page = client.get(f"/note/{INFLUX_ID}").text

    [form] = _forms(page, INFLUX_ID)
    assert _buttons(form) == {"rejected": "true"}
    assert f'name="next" value="/note/{INFLUX_ID}"' in form
    assert _forms(client.get(f"/note/{BARE_ID}").text, BARE_ID) == []


def test_without_an_operator_no_form_renders_but_a_carried_verdict_is_stated(
    config, fake: FeedbackFake
) -> None:
    app = create_app(config, lithos_client_factory=lambda _: fake)
    with TestClient(app, base_url=ORIGIN) as anonymous:
        page = anonymous.get(f"/note/{TWO_ID}").text

    assert _forms(page, TWO_ID) == []
    assert 'data-feedback-profile="ai-coding" data-feedback-readonly' in page
    assert "👍 interesting" in unescape(page)


def test_the_graph_node_panel_offers_thumbs_down(client: TestClient) -> None:
    page = client.get(f"/knowledge/graph?focus={INFLUX_ID}&selected={INFLUX_ID}")

    assert page.status_code == 200
    [form] = _forms(page.text, INFLUX_ID)
    assert _buttons(form) == {"rejected": "false"}
    assert "selected=" in re.search(r'name="next" value="([^"]+)"', form)[1]  # type: ignore[index]


# ── the fake reproduces the contract ─────────────────────────────────────


def test_the_fake_answers_the_vendored_success_and_conflict_payloads() -> None:
    fake = FakeLithosClient(None, dataset=_dataset())
    canonical = _UPDATE["request"]["canonical"]

    stale = asyncio.run(
        fake.note_update(
            INFLUX_ID, agent="dave", tags=canonical["tags"], expected_version=1
        )
    )
    applied = asyncio.run(
        fake.note_update(
            INFLUX_ID,
            agent="dave",
            tags=canonical["tags"],
            expected_version=canonical["expected_version"],
        )
    )
    late = asyncio.run(
        fake.note_update(
            INFLUX_ID, agent="dave", tags=canonical["tags"], expected_version=2
        )
    )

    assert stale == NoteUpdateResult(
        status="version_conflict",
        current_version=2,
        message="Version conflict: expected 1, got 2",
    )
    assert applied == normalize_note_update(_UPDATE["responses"]["success"])
    assert late == normalize_note_update(
        _UPDATE["responses"]["variants"]["version_conflict"]
    )


# ── the tag scheme ───────────────────────────────────────────────────────


def test_apply_verdict_keeps_every_other_tag_in_place() -> None:
    tags = ("b", "influx:rejected:p", "a", "influx:rejected:p", "profile:p", "b")

    assert apply_verdict(tags, "p", "accepted") == (
        "b",
        "a",
        "profile:p",
        "b",
        "influx:accepted:p",
    )
    # Already so: the same tuple back, which is how the caller writes nothing.
    assert apply_verdict(("profile:p", "influx:rejected:p"), "p", "rejected") == (
        "profile:p",
        "influx:rejected:p",
    )


def test_profiles_and_pressed_state_come_from_the_tags() -> None:
    tags = ("profile:a", "profile:", "profile:b", "profile:a", "influx:rejected:b")

    assert note_profiles(tags) == ("a", "b")
    [a, b] = note_feedback(tags)
    assert (a.profile, a.accepted, a.rejected) == ("a", False, False)
    assert (b.profile, b.pressed("accepted"), b.pressed("rejected")) == (
        "b",
        False,
        True,
    )
    assert note_feedback(("source:blog",)) == ()


# ── round 2: the rendered controls, carried verdicts, warm caches, outages ──


@pytest.mark.parametrize("profile", ["ai-coding", "robotics"])
def test_clicking_a_rendered_button_records_that_rows_profile(
    client: TestClient, fake: FeedbackFake, profile: str
) -> None:
    """The form the landing renders, submitted as the browser submits it: the
    row's own profile and the clicked verdict reach the write, and the note
    ends up with exactly that verdict for exactly that profile."""
    page = client.get("/knowledge").text
    [form] = [
        f for f in _forms(page, TWO_ID) if f'data-feedback-profile="{profile}"' in f
    ]

    response = _click(client, form, "rejected")

    assert response.status_code == 303
    [sent] = fake.updates()
    others = [t for t in TWO_TAGS if t != f"influx:accepted:{profile}"]
    assert sent["tags"] == [*others, f"influx:rejected:{profile}"]
    assert urlsplit(response.headers["location"]).path == "/knowledge"


def test_a_carried_verdict_the_surface_offers_no_button_for_is_still_shown(
    client: TestClient,
) -> None:
    """The note page and the graph panel offer 👎 only; a note already 👍 for
    a profile says so beside it rather than looking unjudged."""
    graph = client.get(f"/knowledge/graph?focus={TWO_ID}&selected={TWO_ID}").text
    note = client.get(f"/note/{TWO_ID}").text

    for page in (note, graph):
        ai, robotics = _forms(page, TWO_ID)
        assert _buttons(ai) == {"rejected": "false"}
        assert 'data-feedback-carried="accepted"' in ai
        assert "👍 interesting" in unescape(ai)
        assert "data-feedback-carried" not in robotics


@pytest.mark.parametrize("agent_got_there_first", [False, True])
def test_the_graph_panel_shows_the_verdict_after_its_own_redirect(
    client: TestClient, fake: FeedbackFake, agent_got_there_first: bool
) -> None:
    """A warm note-facts cache, and no ``note.updated`` delivered yet: the
    redirect back to the selected panel must still render the verdict pressed
    — whether Lens's patch applied it, or an agent had already applied it and
    Lens wrote nothing ("Already marked")."""
    panel = f"/knowledge/graph?focus={INFLUX_ID}&selected={INFLUX_ID}"
    [before] = _forms(client.get(panel).text, INFLUX_ID)
    assert _buttons(before) == {"rejected": "false"}
    # The fake publishes its note.updated synchronously through the app's
    # hub; detached, every event from here on is one that has not arrived.
    fake.events = None
    if agent_got_there_first:
        asyncio.run(
            fake.note_update(
                INFLUX_ID,
                agent="agent-zero",
                tags=[*INFLUX_TAGS, "influx:rejected:ai-coding"],
                expected_version=2,
            )
        )
        fake.write_calls.clear()

    response = _click(client, before, "rejected")

    assert response.status_code == 303
    assert len(fake.updates()) == (0 if agent_got_there_first else 1)
    landed = client.get(response.headers["location"]).text
    if agent_got_there_first:
        assert "Already marked" in _text(landed)
    [after] = _forms(landed, INFLUX_ID)
    assert _buttons(after) == {"rejected": "true"}


def test_lithos_unreadable_at_the_pre_check_is_shown_and_nothing_written(
    client: TestClient, fake: FeedbackFake
) -> None:
    fake.read_errors.append(
        LithosToolError("did not answer within 10s", code="timeout")
    )

    response = _post(client, INFLUX_ID, "rejected")

    assert response.status_code == 503
    text = _text(response.text)
    assert NOTHING_CHANGED in text
    assert "Lens couldn't read the note, so it didn't try the write." in text
    assert "location" not in response.headers
    assert fake.updates() == []


def test_lithos_unreadable_at_the_re_read_after_a_conflict_is_shown_no_retry(
    client: TestClient, fake: FeedbackFake
) -> None:
    fake.pending_note_writes.append((*INFLUX_TAGS, "agent:added-meanwhile"))
    fake.read_errors.extend(
        [None, LithosToolError("Lithos MCP session is not available")]
    )

    response = _post(client, INFLUX_ID, "rejected")

    assert response.status_code == 503
    text = _text(response.text)
    assert NOTHING_CHANGED in text
    assert "Lens couldn't read the note" in text
    assert len(fake.updates()) == 1
    assert "influx:rejected:ai-coding" not in fake.tags_of(INFLUX_ID)
