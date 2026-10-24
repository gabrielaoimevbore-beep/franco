"""The optional Precleared check in the native send path: off by default, one
check per address, a stopping verdict fails the outbox item with the reason,
an unavailable check holds cold email without failing it, replies are never
checked. No network: every Precleared call goes to an httpx.MockTransport."""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import httpx
import pytest
import yaml
from pydantic import ValidationError

from franco.config import EnvConfig, PreclearedConfig, load_config, load_env
from franco.integrations.precleared import BUY_URL, SETTINGS_PREFIX, PreclearedClient
from tests.phase1_helpers import make_config
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    Cfg,
    Env,
    FakeProvider,
    _now_iso,
    make_sender,
    seed_prospect,
    state,
)

ROOT = Path(__file__).resolve().parent.parent
JANE = "jane@example.com"
SAM = "sam@example.com"
REPLY = "reply@example.com"

OK_BODY = {
    "verdict": "ok_to_email",
    "confidence": "high",
    "reasons": [{
        "rule_key": "gb_corporate_ok",
        "text": "Corporate subscribers need no prior consent.",
        "source_title": "PECR reg. 22",
    }],
    "next_step": "Send, with a footer that says who you are and how to opt out.",
    "receipt": {"id": "cc_rcpt_OK0000000001"},
}

CONSENT_BODY = {
    "verdict": "consent_needed",
    "confidence": "high",
    "reasons": [{
        "rule_key": "gb_individual_consent",
        "text": "Sole traders count as individuals, so consent is needed.",
        "source_title": "PECR reg. 22(2)",
    }],
    "next_step": "Do not send cold email. Get consent first.",
    "receipt": {"id": "cc_rcpt_NO0000000001"},
}

UNKNOWN_BODY = {
    "verdict": "unknown",
    "reasons": [{"note": "The company register could not be reached."}],
    "next_step": "Treat as consent needed, or check again later.",
    "receipt": {"id": "cc_rcpt_UN0000000001"},
}


def mock_precleared(calls, body=None, status=200):
    """A Precleared stand-in that records each request."""
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"json": json.loads(request.content), "headers": dict(request.headers)})
        if status != 200:
            return httpx.Response(status, json={"error": "daily_limit", "message": "Limit reached."})
        return httpx.Response(200, json=body)
    return httpx.MockTransport(handler)


def mock_status(status, body):
    """A Precleared stand-in that answers every request with one status and body."""
    return httpx.MockTransport(lambda request: httpx.Response(status, json=body))


def mock_raw(status, content):
    """A Precleared stand-in that answers every request with raw bytes."""
    return httpx.MockTransport(lambda request: httpx.Response(
        status, content=content, headers={"Content-Type": "application/json"},
    ))


def with_check(sender, transport, api_key=""):
    sender.precleared = PreclearedClient(sender_country="GB", api_key=api_key, transport=transport)
    return sender


async def queue(state, pid, step=1, kind="sequence", to=JANE, campaign="c1"):
    return await state.add_outbox_item(
        prospect_id=pid, to_email=to, subject="hello",
        body="Fine body. Question?", send_at=_now_iso(), status="approved",
        campaign_id=campaign if kind == "sequence" else "", step=step, kind=kind,
    )


async def jane(state, status="new"):
    return await seed_prospect(state, email=JANE, status=status)


async def queue_reply(state):
    pid = await seed_prospect(state, email=REPLY, status="replied")
    return await queue(state, pid, kind="reply", to=REPLY)


async def logged_receipts(state):
    async with aiosqlite.connect(state.db_path) as db:
        async with db.execute(
            "SELECT details_json FROM actions WHERE action_type = 'email_sent'"
        ) as cursor:
            rows = await cursor.fetchall()
    return [json.loads(r[0]).get("precleared_receipt", "") for r in rows]


def enabled_config(**precleared):
    settings = {"enabled": True, "sender_country": "GB", **precleared}
    return SimpleNamespace(
        compliance=SimpleNamespace(precleared=PreclearedConfig(**settings)),
        product=SimpleNamespace(description="Route planning for delivery firms"),
    )


# ── off by default ──


def test_check_is_off_unless_enabled():
    assert PreclearedClient.from_config(Cfg(), Env()) is None
    assert PreclearedClient.from_config(make_config(), EnvConfig()) is None
    off = SimpleNamespace(compliance=SimpleNamespace(precleared=PreclearedConfig()))
    assert PreclearedClient.from_config(off, Env()) is None


@pytest.mark.asyncio
async def test_disabled_sender_sends_without_a_check(state):
    pid = await jane(state)
    await queue(state, pid)
    provider = FakeProvider()
    sender = make_sender(state, provider)
    assert sender.precleared is None
    await sender._drain_due()
    assert len(provider.sent) == 1


# ── config ──


def test_config_needs_a_supported_sender_country_when_enabled():
    with pytest.raises(ValidationError):
        PreclearedConfig(enabled=True)
    with pytest.raises(ValidationError):
        PreclearedConfig(enabled=True, sender_country="FR")
    for country in ("gb", "US", "ae"):
        assert PreclearedConfig(enabled=True, sender_country=country).sender_country == country.upper()
    assert PreclearedConfig(enabled=True, sender_country="uk").sender_country == "GB"
    assert PreclearedConfig(enabled=True, sender_country="US").address_source == "unknown"
    with pytest.raises(ValidationError):
        PreclearedConfig(enabled=True, sender_country="US", address_source="guessed")


def test_from_config_reads_every_setting_the_key_and_the_pitch():
    client = PreclearedClient.from_config(
        enabled_config(sender_country="US", address_source="published", prospect_country="us"),
        SimpleNamespace(precleared_api_key="pc_live_test"),
    )
    assert client.sender_country == "US"
    assert client.address_source == "published"
    assert client.prospect_country == "US"
    assert client.api_key == "pc_live_test"
    assert client.pitch == "Route planning for delivery firms"


def test_prospect_country_is_optional_and_normalised():
    assert PreclearedConfig(enabled=True, sender_country="US").prospect_country == ""
    assert PreclearedConfig(enabled=True, sender_country="US", prospect_country=" us ").prospect_country == "US"
    assert PreclearedConfig(enabled=True, sender_country="GB", prospect_country="uk").prospect_country == "GB"
    with pytest.raises(ValidationError):
        PreclearedConfig(enabled=True, sender_country="US", prospect_country="United States")


@pytest.mark.parametrize("yaml_value", [False, True])
def test_unquoted_yaml_country_is_an_error_not_ignored(yaml_value):
    # YAML reads an unquoted NO (Norway) as False and ON as True.
    with pytest.raises(ValidationError, match="quote"):
        PreclearedConfig(enabled=True, sender_country="US", prospect_country=yaml_value)


def test_client_normalises_prospect_country_like_the_config():
    assert PreclearedClient("US", prospect_country="uk").prospect_country == "GB"
    assert PreclearedClient("US", prospect_country="usa").prospect_country == "US"
    with pytest.raises(ValueError):
        PreclearedClient("US", prospect_country="garbage!!")


def test_settings_load_from_franco_yaml(tmp_path):
    path = tmp_path / "franco.yaml"
    path.write_text(yaml.safe_dump({
        "persona": {"name": "H", "company": "Example Co", "role": "B",
                    "email": "h@example.com", "linkedin": "", "tone": "plain"},
        "product": {"name": "P", "description": "D", "pricing": "$1",
                    "key_benefits": ["x"], "objection_responses": {}},
        "icp": {"industries": ["SaaS"], "company_size": "10-50",
                "titles": ["CEO"], "geography": ["US"]},
        "compliance": {"precleared": {
            "enabled": True, "sender_country": "us", "address_source": "published",
            "prospect_country": "usa",
        }},
    }))
    settings = load_config(str(path)).compliance.precleared
    assert (settings.enabled, settings.sender_country, settings.address_source,
            settings.prospect_country) == (True, "US", "published", "US")


def test_template_franco_yaml_ships_with_the_check_off():
    settings = load_config(str(ROOT / "franco.yaml")).compliance.precleared
    assert settings.enabled is False
    assert settings.address_source == "unknown"


def test_load_env_reads_the_key(monkeypatch):
    monkeypatch.setenv("PRECLEARED_API_KEY", "  pc_live_test  ")
    assert load_env().precleared_api_key == "pc_live_test"


def test_warns_that_instantly_campaigns_are_not_checked(state, caplog):
    from franco.agents.sender import Sender

    config = make_config(
        channels={"email": {"provider": "instantly"}},
        compliance={"precleared": {"enabled": True, "sender_country": "US"}},
    )
    with caplog.at_level(logging.WARNING, logger="franco.sender"):
        Sender(brain=None, state=state, config=config, env=EnvConfig())
    assert "not checked" in caplog.text


# ── what is sent, and what is cached ──


@pytest.mark.asyncio
async def test_pitch_and_prospect_country_are_sent_only_when_set(state):
    calls = []
    await PreclearedClient("US", transport=mock_precleared(calls, OK_BODY)).clear(JANE, state)
    assert "pitch" not in calls[0]["json"]
    assert "default_recipient_country" not in calls[0]["json"]

    client = PreclearedClient("US", pitch="  Delivery software  ", prospect_country="US",
                              transport=mock_precleared(calls, OK_BODY))
    await client.clear(SAM, state)
    assert calls[1]["json"]["pitch"] == "Delivery software"
    assert calls[1]["json"]["default_recipient_country"] == "US"


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [
    {"sender_country": "US"},
    {"address_source": "published"},
    {"pitch": "Delivery software"},
    {"prospect_country": "US"},
])
async def test_changing_any_fact_sent_forces_a_fresh_check(state, changed):
    calls = []
    before = {"sender_country": "GB", "address_source": "unknown", "pitch": "", "prospect_country": ""}
    await PreclearedClient(transport=mock_precleared(calls, OK_BODY), **before).clear(JANE, state)
    after = PreclearedClient(transport=mock_precleared(calls, CONSENT_BODY), **{**before, **changed})

    result = await after.clear(JANE, state)

    assert len(calls) == 2
    assert not result.allowed and result.verdict == "consent_needed"
    # The new verdict replaced the old one, so the same question is now cached.
    await after.clear(JANE, state)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_cache_entry_without_the_same_facts_is_not_reused(state):
    await state.set_setting(SETTINGS_PREFIX + "person@example.com", json.dumps({
        "verdict": "ok_to_email", "reason": "", "receipt_id": "cc_rcpt_OLD0000000001",
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }))
    calls = []
    client = PreclearedClient("GB", transport=mock_precleared(calls, CONSENT_BODY))

    result = await client.clear("person@example.com", state)

    assert not result.allowed
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_expired_verdict_is_checked_again(state):
    calls = []
    client = PreclearedClient("GB", transport=mock_precleared(calls, OK_BODY))
    await client.clear(JANE, state)
    entry = json.loads(await state.get_setting(SETTINGS_PREFIX + JANE))
    entry["checked_at"] = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    await state.set_setting(SETTINGS_PREFIX + JANE, json.dumps(entry))

    assert (await client.clear(JANE, state)).allowed
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_unknown_verdict_stops_the_email_but_is_not_cached(state):
    calls = []
    client = PreclearedClient("GB", transport=mock_precleared(calls, UNKNOWN_BODY))

    result = await client.clear(JANE, state)

    assert not result.allowed and not result.held
    assert result.verdict == "unknown"
    assert "could not be reached" in result.reason
    assert await state.get_setting(SETTINGS_PREFIX + JANE) == ""
    await client.clear(JANE, state)
    assert len(calls) == 2


# ── verdicts in the send path ──


@pytest.mark.asyncio
async def test_ok_verdict_sends_and_logs_the_receipt(state):
    pid = await jane(state)
    await queue(state, pid)
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, OK_BODY))

    await sender._drain_due()

    assert len(provider.sent) == 1
    assert calls[0]["json"] == {
        "recipient_email": JANE,
        "sender_country": "GB",
        "address_source": "unknown",
        "existing_customer": False,
    }
    assert "authorization" not in calls[0]["headers"]
    assert calls[0]["headers"]["user-agent"].startswith("franco")
    assert await logged_receipts(state) == ["cc_rcpt_OK0000000001"]


@pytest.mark.asyncio
async def test_api_key_is_sent_as_bearer(state):
    pid = await jane(state)
    await queue(state, pid)
    calls = []
    sender = with_check(make_sender(state, FakeProvider()),
                        mock_precleared(calls, OK_BODY), api_key="pc_live_test")
    await sender._drain_due()
    assert calls[0]["headers"]["authorization"] == "Bearer pc_live_test"


@pytest.mark.asyncio
async def test_consent_needed_fails_the_item_with_the_reason(state):
    pid = await jane(state)
    await queue(state, pid)
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared([], CONSENT_BODY))

    await sender._drain_due()

    assert provider.sent == []
    failed = await state.get_outbox(status="failed")
    assert len(failed) == 1
    assert failed[0]["error"].startswith("precleared: consent_needed")
    assert "PECR reg. 22(2)" in failed[0]["error"]
    assert "Get consent first" in failed[0]["error"]


@pytest.mark.asyncio
async def test_follow_up_is_cancelled_when_the_first_email_was_stopped(state):
    pid = await jane(state)
    await queue(state, pid, step=1)
    await queue(state, pid, step=2)
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, CONSENT_BODY))

    await sender._drain_due()
    await sender._drain_due()

    assert provider.sent == []
    statuses = {i["step"]: i["status"] for i in await state.get_outbox()}
    assert statuses == {1: "failed", 2: "cancelled"}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_one_check_per_address(state):
    pid = await jane(state)
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, OK_BODY))

    await queue(state, pid, step=1)
    await sender._drain_due()
    await queue(state, pid, step=2)
    await sender._drain_due()

    assert len(provider.sent) == 2
    assert len(calls) == 1
    assert await state.get_setting(SETTINGS_PREFIX + JANE)
    assert await logged_receipts(state) == ["cc_rcpt_OK0000000001"] * 2


@pytest.mark.asyncio
async def test_stopping_verdict_is_remembered_for_other_campaigns(state):
    pid = await jane(state)
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, CONSENT_BODY))

    await queue(state, pid, step=1)
    await sender._drain_due()
    # Another campaign's first email to the same address: no second check.
    await queue(state, pid, step=1, campaign="c2")
    await sender._drain_due()

    assert provider.sent == []
    assert len(await state.get_outbox(status="failed")) == 2
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_email_the_gate_blocks_spends_no_check(state):
    pid = await jane(state)
    await state.add_outbox_item(
        prospect_id=pid, to_email=JANE, subject="hello", body="Unrendered {{first_name}} tag",
        send_at=_now_iso(), status="approved", campaign_id="c1", step=1,
    )
    calls = []
    sender = with_check(make_sender(state, FakeProvider()), mock_precleared(calls, OK_BODY))

    await sender._drain_due()

    assert calls == []
    assert "gate" in (await state.get_outbox(status="failed"))[0]["error"]


@pytest.mark.asyncio
async def test_replies_are_never_checked(state):
    pid = await jane(state, status="replied")
    await queue(state, pid, kind="reply")
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, CONSENT_BODY))

    await sender._drain_due()

    assert calls == []
    assert len(provider.sent) == 1


@pytest.mark.asyncio
async def test_fresh_checks_are_capped_per_cycle(state):
    for n in range(10):
        pid = await seed_prospect(state, email=f"person{n}@example.com")
        await queue(state, pid, to=f"person{n}@example.com")
    calls = []
    sender = with_check(make_sender(state, FakeProvider()), mock_precleared(calls, CONSENT_BODY))

    await sender._drain_due()

    # The cycle's send budget (8) bounds its fresh checks; the rest wait.
    assert len(calls) == 8
    assert len(await state.get_outbox(status="failed")) == 8
    assert len(await state.get_outbox(status="approved")) == 2


# ── whatever changes while a check runs wins ──


@pytest.mark.asyncio
async def test_email_rejected_during_the_check_is_not_sent(state):
    pid = await jane(state)
    item_id = await queue(state, pid)

    async def reject_meanwhile(request):
        await state.update_outbox_item(item_id, status="rejected")
        return httpx.Response(200, json=OK_BODY)

    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), httpx.MockTransport(reject_meanwhile))
    await sender._drain_due()

    assert provider.sent == []
    assert (await state.get_outbox_item(item_id))["status"] == "rejected"


@pytest.mark.asyncio
async def test_rejection_during_a_stopping_check_is_kept(state):
    pid = await jane(state)
    item_id = await queue(state, pid)

    async def reject_meanwhile(request):
        await state.update_outbox_item(item_id, status="rejected")
        return httpx.Response(200, json=CONSENT_BODY)

    sender = with_check(make_sender(state, FakeProvider()), httpx.MockTransport(reject_meanwhile))
    await sender._drain_due()

    assert (await state.get_outbox_item(item_id))["status"] == "rejected"


@pytest.mark.asyncio
async def test_suppression_added_during_the_check_wins(state):
    pid = await jane(state)
    item_id = await queue(state, pid)

    async def suppress_meanwhile(request):
        await state.add_suppression(JANE, reason="asked by phone")
        return httpx.Response(200, json=OK_BODY)

    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), httpx.MockTransport(suppress_meanwhile))
    await sender._drain_due()
    assert provider.sent == []

    # The next cycle records why, from the cached verdict: no second check.
    await sender._drain_due()
    item = await state.get_outbox_item(item_id)
    assert item["status"] == "cancelled" and "suppressed" in item["error"]
    assert provider.sent == []


@pytest.mark.asyncio
async def test_kill_switch_during_the_check_stops_the_cycle(state):
    pid = await jane(state)
    await queue(state, pid)
    await queue_reply(state)

    async def pause_meanwhile(request):
        await state.set_setting("sending_paused", "paused from dashboard")
        return httpx.Response(200, json=OK_BODY)

    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), httpx.MockTransport(pause_meanwhile))
    await sender._drain_due()

    assert provider.sent == []
    assert len(await state.get_outbox(status="approved")) == 2


# ── check unavailable: cold email waits, replies go ──


@pytest.mark.asyncio
async def test_daily_limit_holds_cold_email_but_replies_still_go(state):
    pid = await jane(state)
    other = await seed_prospect(state, email=SAM)
    await queue(state, pid)
    await queue(state, other, to=SAM)
    await queue_reply(state)
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, status=429))

    await sender._drain_due()

    # One failed call pauses fresh checks for the rest of the cycle.
    assert len(calls) == 1
    assert [m["to"] for m in provider.sent] == [REPLY]
    assert await state.get_outbox(status="failed") == []
    waiting = await state.get_outbox(status="approved")
    assert sorted(i["to_email"] for i in waiting) == [JANE, SAM]
    # Nothing cached, so the next cycle asks again.
    assert await state.get_setting(SETTINGS_PREFIX + JANE) == ""


@pytest.mark.asyncio
async def test_pause_still_sends_email_whose_verdict_is_cached(state):
    first = await jane(state)
    second = await seed_prospect(state, email=SAM)
    await queue(state, first)
    await queue(state, second, to=SAM)
    await PreclearedClient("GB", transport=mock_precleared([], OK_BODY)).clear(SAM, state)
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, status=429))

    await sender._drain_due()

    assert len(calls) == 1
    assert [m["to"] for m in provider.sent] == [SAM]


@pytest.mark.asyncio
async def test_held_cold_backlog_does_not_starve_replies(state):
    pid = await jane(state)
    for step in range(24):
        await queue(state, pid, step=step + 1)
    await queue_reply(state)
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared([], status=429))

    await sender._drain_due()
    await sender._drain_due()

    assert [m["to"] for m in provider.sent] == [REPLY]
    assert len(await state.get_outbox(status="approved")) == 24
    assert await state.get_outbox(status="failed") == []


@pytest.mark.asyncio
async def test_network_error_holds_the_email_and_hides_the_key(state):
    pid = await jane(state)
    await queue(state, pid)

    def boom(request):
        raise httpx.ConnectError("no route to host for pc_live_secret123")

    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), httpx.MockTransport(boom),
                        api_key="pc_live_secret123")
    result = await sender.precleared.check(JANE, state)
    await sender._drain_due()

    assert result.held and "pc_live_secret123" not in result.reason
    assert provider.sent == []
    assert await state.get_outbox(status="failed") == []
    assert len(await state.get_outbox(status="approved")) == 1


@pytest.mark.asyncio
async def test_server_error_holds_and_hides_the_key(state):
    client = PreclearedClient("GB", api_key="pc_live_secret123", transport=mock_raw(
        503, b"upstream failed for key pc_live_secret123"))

    result = await client.clear("person@example.com", state)

    assert result.held and not result.allowed
    assert "HTTP 503" in result.reason
    assert "pc_live_secret123" not in result.reason
    assert await state.get_setting(SETTINGS_PREFIX + "person@example.com") == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    json.dumps([]).encode(),
    json.dumps("ok_to_email").encode(),
    json.dumps({"verdict": "maybe"}).encode(),
    json.dumps({"reasons": []}).encode(),
    b"not json",
    b"[" * 100_000 + b"]" * 100_000,
], ids=["list", "string", "unknown-verdict", "no-verdict", "not-json", "deeply-nested"])
async def test_malformed_answer_holds_cold_email_and_reply_still_sends(state, content):
    pid = await jane(state)
    await queue(state, pid)
    await queue_reply(state)
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_raw(200, content))

    await sender._drain_due()

    assert [m["to"] for m in provider.sent] == [REPLY]
    assert await state.get_outbox(status="failed") == []
    assert [i["to_email"] for i in await state.get_outbox(status="approved")] == [JANE]
    # Nothing cached from a malformed answer, so the next cycle asks again.
    assert await state.get_setting(SETTINGS_PREFIX + JANE) == ""


@pytest.mark.asyncio
async def test_unexpected_error_holds_cold_email_instead_of_stopping_the_cycle(state):
    pid = await jane(state)
    await queue(state, pid)
    await queue_reply(state)

    def broken(request):
        raise RuntimeError("something nobody planned for")

    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), httpx.MockTransport(broken))
    await sender._drain_due()

    assert [m["to"] for m in provider.sent] == [REPLY]
    assert [i["to_email"] for i in await state.get_outbox(status="approved")] == [JANE]


@pytest.mark.asyncio
async def test_unexpected_optional_field_shapes_are_ignored(state):
    body = {
        "verdict": "consent_needed",
        "reasons": {"text": "not a list"},
        "next_step": 5,
        "receipt": "cc_rcpt_not_an_object",
    }
    client = PreclearedClient("GB", transport=mock_precleared([], body=body))

    result = await client.clear("person@example.com", state)

    assert not result.allowed and not result.held
    assert result.verdict == "consent_needed"
    assert result.reason == ""
    assert result.receipt_id == ""


@pytest.mark.asyncio
async def test_held_cold_email_goes_out_once_the_check_answers(state):
    first = await jane(state)
    second = await seed_prospect(state, email=SAM)
    await queue(state, first)
    await queue(state, second, to=SAM)
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_status(402, {
        "error": "payment_required", "buy_url": "https://precleared.dev/buy",
    }), api_key="pc_live_test")

    await sender._drain_due()
    assert provider.sent == []
    assert len(await state.get_outbox(status="approved")) == 2

    sender.precleared._transport = mock_status(200, OK_BODY)
    await sender._drain_due()
    assert sorted(m["to"] for m in provider.sent) == [JANE, SAM]
    assert await state.get_outbox(status="approved") == []


# ── what the held reason tells the operator ──


@pytest.mark.asyncio
async def test_keyless_daily_limit_points_to_a_free_key(state):
    client = PreclearedClient("GB", transport=mock_status(429, {
        "error": "daily_limit", "get_key_url": "https://precleared.dev/get-key",
    }))

    result = await client.clear("person@example.com", state)

    assert result.held and not result.allowed
    assert "add PRECLEARED_API_KEY" in result.reason
    assert "precleared.dev/get-key" in result.reason


@pytest.mark.asyncio
async def test_rate_limit_on_a_key_is_not_reported_as_a_missing_key(state):
    client = PreclearedClient("GB", api_key="pc_live_test", transport=mock_status(429, {
        "error": "rate_limited", "message": "Slow down.", "retry_after": 20,
    }))

    result = await client.clear("person@example.com", state)

    assert result.held and not result.allowed
    assert "rate limit" in result.reason
    assert "add PRECLEARED_API_KEY" not in result.reason


@pytest.mark.asyncio
async def test_rejected_key_holds_with_a_clear_reason(state):
    client = PreclearedClient("GB", api_key="pc_live_test", transport=mock_status(401, {
        "error": "invalid_key",
    }))

    result = await client.clear("person@example.com", state)

    assert result.held
    assert "PRECLEARED_API_KEY was rejected" in result.reason


@pytest.mark.asyncio
async def test_key_out_of_checks_points_to_the_buy_page_not_a_free_key(state):
    client = PreclearedClient("GB", api_key="pc_live_test", transport=mock_status(402, {
        "error": "payment_required",
        "message": "This key has no checks left today. Buy a pack to continue.",
        "buy_url": "https://example.com/somewhere-else",
    }))

    result = await client.clear("person@example.com", state)

    assert result.held and not result.allowed
    assert BUY_URL in result.reason
    assert "add PRECLEARED_API_KEY" not in result.reason
    # Franco's own wording and link, never text or links from the answer.
    assert "Buy a pack to continue" not in result.reason
    assert "example.com/somewhere-else" not in result.reason
    # Nothing cached, so the next cycle asks again.
    assert await state.get_setting(SETTINGS_PREFIX + "person@example.com") == ""
