"""The optional Precleared check in the native send path: off by default;
sending uses only stored verdicts, and a new address is checked after the
cycle's sending and decided on the next cycle; one check per address; a
stopping verdict fails the outbox item with the reason; an unavailable check
holds cold email without failing it; replies are never checked. No network:
every Precleared call goes to an httpx.MockTransport."""

import asyncio
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
from tests.phase1_helpers import (
    RecordingProvider, account_row, import_accounts, make_config, make_writer,
)
from tests.phase1_helpers import make_sender as make_config_sender
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

BLOCKED_BODY = {
    "verdict": "blocked",
    "confidence": "high",
    "reasons": [{
        "rule_key": "recipient_opted_out",
        "text": "The recipient has opted out of marketing email.",
        "source_title": "PECR reg. 22(3)",
    }],
    "next_step": "Do not send. Keep the address on your suppression list.",
    "receipt": {"id": "cc_rcpt_BL0000000001"},
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


async def cycles(sender, n=2):
    """Run n send cycles: a new address is checked in one, decided in the next."""
    for _ in range(n):
        await sender._drain_due()


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
@pytest.mark.parametrize("body", [OK_BODY, CONSENT_BODY, BLOCKED_BODY, UNKNOWN_BODY],
                         ids=["ok_to_email", "consent_needed", "blocked", "unknown"])
async def test_only_ok_to_email_allows_sending_fresh_or_stored(state, body):
    client = PreclearedClient("GB", transport=mock_precleared([], body))

    fresh = await client.clear(JANE, state)
    stored = await client.clear(JANE, state)

    allowed = body["verdict"] == "ok_to_email"
    assert (fresh.allowed, stored.allowed) == (allowed, allowed)
    assert fresh.verdict == stored.verdict == body["verdict"]
    assert not fresh.held and not stored.held


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


async def age_entry(state, email, days):
    entry = json.loads(await state.get_setting(SETTINGS_PREFIX + email))
    entry["checked_at"] = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    await state.set_setting(SETTINGS_PREFIX + email, json.dumps(entry))


@pytest.mark.asyncio
async def test_verdict_is_reused_for_30_days_then_checked_again(state):
    calls = []
    client = PreclearedClient("GB", transport=mock_precleared(calls, OK_BODY))
    await client.clear(JANE, state)

    await age_entry(state, JANE, 29)
    assert (await client.clear(JANE, state)).allowed
    assert len(calls) == 1

    await age_entry(state, JANE, 31)
    assert (await client.clear(JANE, state)).allowed
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_unknown_verdict_stops_the_email_and_is_kept_for_a_day(state):
    calls = []
    client = PreclearedClient("GB", transport=mock_precleared(calls, UNKNOWN_BODY))

    result = await client.clear(JANE, state)

    assert not result.allowed and not result.held
    assert result.verdict == "unknown"
    assert "could not be reached" in result.reason
    assert (await client.clear(JANE, state)).verdict == "unknown"
    assert len(calls) == 1
    # It can be temporary, so after a day another email asks again.
    await age_entry(state, JANE, 2)
    await client.clear(JANE, state)
    assert len(calls) == 2


# ── verdicts in the send path ──


@pytest.mark.asyncio
async def test_new_address_is_checked_after_sending_and_sent_next_cycle(state):
    pid = await jane(state)
    await queue(state, pid)
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, OK_BODY))

    await sender._drain_due()
    assert provider.sent == []
    assert len(calls) == 1

    await sender._drain_due()
    assert len(provider.sent) == 1
    assert len(calls) == 1
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
async def test_no_email_is_sent_after_a_check_in_the_same_cycle(state):
    # A new address first in the queue, then cold email with a stored
    # verdict and a reply: both go out before Precleared is asked anything.
    pid = await jane(state)
    other = await seed_prospect(state, email=SAM)
    await queue(state, pid)
    await queue(state, other, to=SAM)
    await queue_reply(state)
    await PreclearedClient("GB", transport=mock_precleared([], OK_BODY)).clear(SAM, state)
    events = []

    def handler(request):
        events.append(("check", json.loads(request.content)["recipient_email"]))
        return httpx.Response(200, json=OK_BODY)

    class Recording(FakeProvider):
        async def send_email(self, to_email, *args, **kwargs):
            events.append(("send", to_email))
            return await super().send_email(to_email, *args, **kwargs)

    sender = with_check(make_sender(state, Recording()), httpx.MockTransport(handler))
    await sender._drain_due()

    assert events == [("send", SAM), ("send", REPLY), ("check", JANE)]


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
@pytest.mark.parametrize("body", [CONSENT_BODY, BLOCKED_BODY, UNKNOWN_BODY],
                         ids=["consent_needed", "blocked", "unknown"])
async def test_stopping_verdict_fails_the_item_with_the_reason(state, body):
    pid = await jane(state)
    await queue(state, pid)
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared([], body))

    await cycles(sender)

    assert provider.sent == []
    failed = await state.get_outbox(status="failed")
    assert len(failed) == 1
    assert failed[0]["error"].startswith(f"precleared: {body['verdict']}: ")
    assert body["next_step"] in failed[0]["error"]


@pytest.mark.asyncio
async def test_consent_reason_names_the_rule_and_its_source(state):
    pid = await jane(state)
    await queue(state, pid)
    sender = with_check(make_sender(state, FakeProvider()), mock_precleared([], CONSENT_BODY))

    await cycles(sender)

    error = (await state.get_outbox(status="failed"))[0]["error"]
    assert "Sole traders count as individuals" in error
    assert "PECR reg. 22(2)" in error


@pytest.mark.asyncio
async def test_stored_blocked_verdict_stops_the_email_without_a_check(state):
    pid = await jane(state)
    await queue(state, pid)
    await PreclearedClient("GB", transport=mock_precleared([], BLOCKED_BODY)).clear(JANE, state)
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, OK_BODY))

    await sender._drain_due()

    assert calls == [] and provider.sent == []
    assert (await state.get_outbox(status="failed"))[0]["error"].startswith("precleared: blocked")


@pytest.mark.asyncio
async def test_follow_up_is_cancelled_when_the_first_email_was_stopped(state):
    pid = await jane(state)
    await queue(state, pid, step=1)
    await queue(state, pid, step=2)
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, CONSENT_BODY))

    await cycles(sender)

    assert provider.sent == []
    statuses = {i["step"]: i["status"] for i in await state.get_outbox()}
    assert statuses == {1: "failed", 2: "cancelled"}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_follow_up_is_checked_when_there_is_no_current_verdict(state):
    # Step 1 went out before the check was switched on; step 2 is checked,
    # and a stopping verdict ends the sequence.
    pid = await jane(state)
    provider = FakeProvider()
    sender = make_sender(state, provider)
    await queue(state, pid, step=1)
    await sender._drain_due()
    assert len(provider.sent) == 1

    calls = []
    with_check(sender, mock_precleared(calls, CONSENT_BODY))
    await queue(state, pid, step=2)
    await queue(state, pid, step=3)
    await cycles(sender)

    assert len(provider.sent) == 1
    assert {i["step"]: i["status"] for i in await state.get_outbox()} == {
        1: "sent", 2: "failed", 3: "cancelled"}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_one_check_per_address(state):
    pid = await jane(state)
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, OK_BODY))

    await queue(state, pid, step=1)
    await cycles(sender)
    await queue(state, pid, step=2)
    await sender._drain_due()

    assert len(provider.sent) == 2
    assert len(calls) == 1
    assert await state.get_setting(SETTINGS_PREFIX + JANE)
    assert await logged_receipts(state) == ["cc_rcpt_OK0000000001"] * 2


@pytest.mark.asyncio
async def test_two_emails_to_one_new_address_share_one_check(state):
    pid = await jane(state)
    await queue(state, pid, step=1)
    await queue(state, pid, step=1, campaign="c2")
    calls = []
    sender = with_check(make_sender(state, FakeProvider()), mock_precleared(calls, CONSENT_BODY))

    await cycles(sender)

    assert len(calls) == 1
    assert len(await state.get_outbox(status="failed")) == 2


@pytest.mark.asyncio
async def test_stopping_verdict_is_remembered_for_other_campaigns(state):
    pid = await jane(state)
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, CONSENT_BODY))

    await queue(state, pid, step=1)
    await cycles(sender)
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

    await cycles(sender)

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
async def test_new_checks_are_capped_per_cycle(state):
    for n in range(10):
        pid = await seed_prospect(state, email=f"person{n}@example.com")
        await queue(state, pid, to=f"person{n}@example.com")
    calls = []
    sender = with_check(make_sender(state, FakeProvider()), mock_precleared(calls, CONSENT_BODY))

    await sender._drain_due()
    # At most one check per email the cycle could send (8); the rest wait.
    assert len(calls) == 8
    assert len(await state.get_outbox(status="approved")) == 10

    await sender._drain_due()
    assert len(await state.get_outbox(status="failed")) == 8
    assert len(calls) == 10


@pytest.mark.asyncio
async def test_checks_stop_once_the_cycles_checking_time_is_used(state, monkeypatch):
    import franco.agents.sender as sender_module

    monkeypatch.setattr(sender_module, "CHECK_SECONDS_PER_CYCLE", 0.05)
    for n in range(3):
        pid = await seed_prospect(state, email=f"person{n}@example.com")
        await queue(state, pid, to=f"person{n}@example.com")
    calls = []

    async def slow(request):
        calls.append(1)
        await asyncio.sleep(0.1)
        return httpx.Response(200, json=OK_BODY)

    sender = with_check(make_sender(state, FakeProvider()), httpx.MockTransport(slow))
    await sender._drain_due()

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_new_addresses_are_checked_even_when_the_cycle_sends_its_full_budget(state):
    for n in range(9):
        pid = await seed_prospect(state, email=f"cleared{n}@example.com")
        await queue(state, pid, to=f"cleared{n}@example.com")
        await PreclearedClient("GB", transport=mock_precleared([], OK_BODY)).clear(
            f"cleared{n}@example.com", state)
    for n in range(3):
        pid = await seed_prospect(state, email=f"new{n}@example.com")
        await queue(state, pid, to=f"new{n}@example.com")
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, OK_BODY))

    await sender._drain_due()
    assert len(provider.sent) == 8  # the budget, even with a ninth cleared email
    assert len(calls) == 3

    await sender._drain_due()
    assert len(provider.sent) == 12


async def queue_people(state, emails):
    for email in emails:
        pid = await seed_prospect(state, email=email)
        await queue(state, pid, to=email)


@pytest.mark.asyncio
async def test_addresses_that_keep_failing_do_not_hold_up_new_ones(state):
    refused = [f"refused{n}@example.com" for n in range(8)]
    await queue_people(state, refused + [SAM])
    calls = []

    def handler(request):
        email = json.loads(request.content)["recipient_email"]
        calls.append(email)
        if email in refused:
            return httpx.Response(500, text="server error")
        return httpx.Response(200, json=OK_BODY)

    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), httpx.MockTransport(handler))
    await cycles(sender, 3)

    assert [m["to"] for m in provider.sent] == [SAM]
    assert calls[8] == SAM  # first in the second cycle, ahead of the failures
    assert len(await state.get_outbox(status="approved")) == 8


@pytest.mark.asyncio
async def test_failed_addresses_do_not_use_up_the_search_after_the_budget_is_sent(state):
    refused = [f"refused{n}@example.com" for n in range(8)]
    await queue_people(state, refused)
    calls = []

    def handler(request):
        email = json.loads(request.content)["recipient_email"]
        calls.append(email)
        return httpx.Response(500 if email in refused else 200, json=OK_BODY)

    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), httpx.MockTransport(handler))
    await sender._drain_due()  # all eight fail
    for n in range(8):
        await queue_people(state, [f"cleared{n}@example.com"])
        await PreclearedClient("GB", transport=mock_precleared([], OK_BODY)).clear(
            f"cleared{n}@example.com", state)
    await queue_people(state, [SAM])
    calls.clear()

    await sender._drain_due()

    assert len(provider.sent) == 8
    assert calls[0] == SAM


@pytest.mark.asyncio
async def test_an_outage_does_not_push_early_prospects_behind_new_ones(state):
    early = [f"early{n}@example.com" for n in range(4)]
    await queue_people(state, early)
    outage = {"on": True}

    def handler(request):
        if outage["on"]:
            return httpx.Response(429, json={"error": "daily_limit"})
        return httpx.Response(200, json=OK_BODY)

    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), httpx.MockTransport(handler))
    await cycles(sender, 4)  # each cycle, one early address meets the used-up allowance

    outage["on"] = False
    for batch in range(3):
        await queue_people(state, [f"later{batch}_{n}@example.com" for n in range(8)])
        await sender._drain_due()

    sent = [m["to"] for m in provider.sent]
    assert all(e in sent for e in early)


@pytest.mark.asyncio
async def test_a_success_does_not_forgive_addresses_that_failed_on_their_own(state):
    refused = [f"refused{n}@example.com" for n in range(8)]
    await queue_people(state, refused)
    calls = []

    def handler(request):
        email = json.loads(request.content)["recipient_email"]
        calls.append(email)
        return httpx.Response(500 if email in refused else 200, json=OK_BODY)

    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), httpx.MockTransport(handler))
    await sender._drain_due()  # the refused addresses all fail
    await queue_people(state, [f"fresh{n}@example.com" for n in range(8)])
    await sender._drain_due()  # the fresh ones are cleared; the refused aren't reached
    await queue_people(state, [SAM])
    calls.clear()

    await sender._drain_due()

    assert len(provider.sent) == 8
    assert calls[0] == SAM


@pytest.mark.asyncio
async def test_without_the_check_a_cycle_still_stops_at_its_budget(state):
    for n in range(10):
        await queue_people(state, [f"person{n}@example.com"])
    provider = FakeProvider()
    sender = make_sender(state, provider)

    await sender._drain_due()

    assert len(provider.sent) == 8


@pytest.mark.asyncio
async def test_an_address_that_always_times_out_does_not_stop_the_others(state, monkeypatch):
    import franco.integrations.precleared as precleared

    monkeypatch.setattr(precleared, "TOTAL_TIMEOUT", 0.05)
    await queue_people(state, ["slow@example.com", SAM])
    calls = []

    async def handler(request):
        email = json.loads(request.content)["recipient_email"]
        calls.append(email)
        if email == "slow@example.com":
            await asyncio.sleep(1)
        return httpx.Response(200, json=OK_BODY)

    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), httpx.MockTransport(handler))
    await cycles(sender, 3)

    assert [m["to"] for m in provider.sent] == [SAM]
    assert calls[:3] == ["slow@example.com", SAM, "slow@example.com"]


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    b"not json", json.dumps([]).encode(), json.dumps({"verdict": "ok_with_conditions"}).encode(),
], ids=["not-json", "not-an-object", "unknown-verdict"])
async def test_unreadable_answer_stops_checks_for_the_cycle(state, content):
    await queue_people(state, [JANE, SAM])
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, content=content)

    sender = with_check(make_sender(state, FakeProvider()), httpx.MockTransport(handler))
    await sender._drain_due()

    assert len(calls) == 1
    assert len(await state.get_outbox(status="approved")) == 2


@pytest.mark.asyncio
async def test_pausing_sending_stops_checks_already_under_way(state):
    await queue_people(state, [JANE, SAM, "third@example.com"])
    calls = []

    async def pause_meanwhile(request):
        calls.append(1)
        await state.set_setting("sending_paused", "paused from dashboard")
        return httpx.Response(200, json=OK_BODY)

    sender = with_check(make_sender(state, FakeProvider()), httpx.MockTransport(pause_meanwhile))
    await sender._drain_due()

    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["rejected", "suppressed"])
async def test_address_whose_email_was_stopped_after_the_queue_was_read_is_not_checked(
        state, change):
    pid = await jane(state)
    item_id = await queue(state, pid)
    await queue_reply(state)
    calls = []

    class ChangedMidCycle(FakeProvider):
        async def send_email(self, *args, **kwargs):
            if change == "rejected":
                await state.update_outbox_item(item_id, status="rejected")
            else:
                await state.add_suppression(JANE, reason="asked by phone")
            return await super().send_email(*args, **kwargs)

    sender = with_check(make_sender(state, ChangedMidCycle()), mock_precleared(calls, OK_BODY))
    await sender._drain_due()

    assert calls == []


# ── the email is decided on fresh data the cycle after its check ──


@pytest.mark.asyncio
async def test_email_rejected_during_its_check_is_not_sent(state):
    pid = await jane(state)
    item_id = await queue(state, pid)

    async def reject_meanwhile(request):
        await state.update_outbox_item(item_id, status="rejected")
        return httpx.Response(200, json=OK_BODY)

    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), httpx.MockTransport(reject_meanwhile))
    await cycles(sender)

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
    await cycles(sender)

    assert (await state.get_outbox_item(item_id))["status"] == "rejected"


@pytest.mark.asyncio
async def test_suppression_added_during_the_check_wins(state):
    pid = await jane(state)
    item_id = await queue(state, pid)
    calls = []

    async def suppress_meanwhile(request):
        calls.append(1)
        await state.add_suppression(JANE, reason="asked by phone")
        return httpx.Response(200, json=OK_BODY)

    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), httpx.MockTransport(suppress_meanwhile))
    await cycles(sender)

    item = await state.get_outbox_item(item_id)
    assert item["status"] == "cancelled" and "suppressed" in item["error"]
    assert provider.sent == []
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_reply_during_the_check_stops_the_email(state):
    pid = await jane(state)
    item_id = await queue(state, pid)

    async def reply_meanwhile(request):
        await state.update_prospect_status(pid, "replied")
        return httpx.Response(200, json=OK_BODY)

    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), httpx.MockTransport(reply_meanwhile))
    await cycles(sender)

    assert provider.sent == []
    assert "replied" in (await state.get_outbox_item(item_id))["error"]


@pytest.mark.asyncio
async def test_address_found_invalid_during_the_check_is_not_sent(state):
    pid = await jane(state)
    item_id = await queue(state, pid)

    async def invalid_meanwhile(request):
        await state.update_prospect_email(pid, JANE, "invalid")
        return httpx.Response(200, json=OK_BODY)

    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), httpx.MockTransport(invalid_meanwhile))
    await cycles(sender)

    assert provider.sent == []
    assert "gate" in (await state.get_outbox_item(item_id))["error"]


@pytest.mark.asyncio
async def test_signoff_withdrawn_during_the_check_wins(state):
    config = make_config()
    await import_accounts(state, [account_row(1)])
    await make_writer(state, config).run()
    await state.approve_outbox()
    for item in await state.get_outbox(limit=500):
        await state.update_outbox_item(item["id"], send_at="2000-01-01T00:00:00")
    await state.set_signoff("M1", "Hemin")

    async def withdraw_meanwhile(request):
        await state.clear_signoff("M1")
        return httpx.Response(200, json=OK_BODY)

    provider = RecordingProvider()
    sender = with_check(make_config_sender(state, config, provider),
                        httpx.MockTransport(withdraw_meanwhile))
    await cycles(sender)

    assert provider.sent == []


@pytest.mark.asyncio
async def test_kill_switch_set_during_a_check_holds_the_next_cycle(state):
    pid = await jane(state)
    await queue(state, pid)

    async def pause_meanwhile(request):
        await state.set_setting("sending_paused", "paused from dashboard")
        return httpx.Response(200, json=OK_BODY)

    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), httpx.MockTransport(pause_meanwhile))
    await sender._run_native()
    await sender._run_native()

    assert provider.sent == []
    assert len(await state.get_outbox(status="approved")) == 1


@pytest.mark.asyncio
async def test_nothing_is_checked_once_sending_is_paused(state):
    pid = await jane(state)
    await queue(state, pid)
    await queue_reply(state)
    calls = []

    class PausedMidCycle(FakeProvider):
        async def send_email(self, *args, **kwargs):
            await state.set_setting("sending_paused", "paused from dashboard")
            return await super().send_email(*args, **kwargs)

    sender = with_check(make_sender(state, PausedMidCycle()), mock_precleared(calls, OK_BODY))
    await sender._drain_due()

    assert calls == []


# ── the footer the verdict assumes ──


def test_check_with_the_footer_off_is_a_compliance_problem():
    from franco.outreach import compliance_problem

    on = {"enabled": True, "sender_country": "GB"}
    problem = compliance_problem(
        make_config(compliance={"require_footer": False, "precleared": on}).compliance)
    assert "require_footer" in problem
    assert compliance_problem(make_config(compliance={"require_footer": False}).compliance) == ""
    assert compliance_problem(make_config(compliance={"precleared": on}).compliance) == ""


@pytest.mark.asyncio
async def test_cold_email_is_held_while_the_check_is_on_and_the_footer_off(state):
    config = make_config(compliance={"require_footer": False,
                                     "precleared": {"enabled": True, "sender_country": "GB"}})
    pid = await jane(state)
    await queue(state, pid)
    await queue_reply(state)
    calls = []
    provider = RecordingProvider()
    sender = with_check(make_config_sender(state, config, provider),
                        mock_precleared(calls, OK_BODY))

    await cycles(sender)

    assert [m["to"] for m in provider.sent] == [REPLY]
    assert calls == []
    assert [i["to_email"] for i in await state.get_outbox(status="approved")] == [JANE]


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

    # The allowance isn't about one address, so no more checks this cycle.
    assert len(calls) == 1
    assert [m["to"] for m in provider.sent] == [REPLY]
    assert await state.get_outbox(status="failed") == []
    waiting = await state.get_outbox(status="approved")
    assert sorted(i["to_email"] for i in waiting) == [JANE, SAM]
    # Nothing cached, so the next cycle asks again.
    assert await state.get_setting(SETTINGS_PREFIX + JANE) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,api_key", [
    (429, {"error": "rate_limited", "retry_after": 20}, "pc_live_test"),
    (402, {"error": "payment_required"}, "pc_live_test"),
    (401, {"error": "invalid_key"}, "pc_live_test"),
], ids=["rate-limit", "no-checks-left", "rejected-key"])
async def test_failure_not_about_the_address_stops_checks_for_the_cycle(state, status, body, api_key):
    first = await jane(state)
    second = await seed_prospect(state, email=SAM)
    await queue(state, first)
    await queue(state, second, to=SAM)
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["recipient_email"])
        return httpx.Response(status, json=body)

    sender = with_check(make_sender(state, FakeProvider()), httpx.MockTransport(handler),
                        api_key=api_key)
    await sender._drain_due()

    assert calls == [JANE]
    assert len(await state.get_outbox(status="approved")) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 404, 500, 503])
async def test_one_refused_address_does_not_hold_up_the_others(state, status):
    poison = "poison@example.com"
    first = await seed_prospect(state, email=poison)
    second = await seed_prospect(state, email=SAM)
    await queue(state, first, to=poison)
    await queue(state, second, to=SAM)
    calls = []

    def handler(request):
        email = json.loads(request.content)["recipient_email"]
        calls.append(email)
        if email == poison:
            return httpx.Response(status, json={"error": "invalid_request"})
        return httpx.Response(200, json=OK_BODY)

    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), httpx.MockTransport(handler))
    await cycles(sender)

    assert [m["to"] for m in provider.sent] == [SAM]
    assert [i["to_email"] for i in await state.get_outbox(status="approved")] == [poison]
    assert calls == [poison, SAM, poison]


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

    await cycles(sender)

    assert [m["to"] for m in provider.sent] == [REPLY]
    assert len(await state.get_outbox(status="approved")) == 24
    assert await state.get_outbox(status="failed") == []


@pytest.mark.asyncio
async def test_network_error_holds_the_email_hides_the_key_and_stops_checks(state):
    first = await jane(state)
    second = await seed_prospect(state, email=SAM)
    await queue(state, first)
    await queue(state, second, to=SAM)
    calls = []

    def boom(request):
        calls.append(1)
        raise httpx.ConnectError("no route to host for pc_live_secret123")

    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), httpx.MockTransport(boom),
                        api_key="pc_live_secret123")
    result = await sender.precleared.check(JANE, state)
    calls.clear()
    await sender._drain_due()

    assert result.held and result.pause_checks
    assert "pc_live_secret123" not in result.reason
    assert len(calls) == 1
    assert provider.sent == []
    assert await state.get_outbox(status="failed") == []
    assert len(await state.get_outbox(status="approved")) == 2


@pytest.mark.asyncio
async def test_server_error_holds_and_hides_the_key(state):
    client = PreclearedClient("GB", api_key="pc_live_secret123", transport=mock_raw(
        503, b"upstream failed for key pc_live_secret123"))

    result = await client.clear("person@example.com", state)

    assert result.held and not result.allowed and not result.pause_checks
    assert "HTTP 503" in result.reason
    assert "pc_live_secret123" not in result.reason
    assert await state.get_setting(SETTINGS_PREFIX + "person@example.com") == ""


@pytest.mark.asyncio
async def test_server_error_hides_a_key_cut_off_at_the_limit(state):
    key = "pc_live_secret123"
    client = PreclearedClient("GB", api_key=key, transport=mock_raw(
        500, ("x" * 190 + key).encode()))

    result = await client.clear("person@example.com", state)

    assert result.held
    assert "pc_live" not in result.reason


@pytest.mark.asyncio
async def test_an_answer_that_never_finishes_holds_cold_email(state, monkeypatch):
    import franco.integrations.precleared as precleared

    monkeypatch.setattr(precleared, "TOTAL_TIMEOUT", 0.05)

    async def trickle(request):
        await asyncio.sleep(2)
        return httpx.Response(200, json=OK_BODY)

    result = await PreclearedClient("GB", transport=httpx.MockTransport(trickle)).clear(JANE, state)

    assert result.held and not result.allowed and result.pause_checks
    assert "too long" in result.reason


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
    await cycles(sender)

    assert [m["to"] for m in provider.sent] == [REPLY]
    assert [i["to_email"] for i in await state.get_outbox(status="approved")] == [JANE]


@pytest.mark.asyncio
async def test_unexpected_error_reason_hides_the_key(state):
    def broken(request):
        raise RuntimeError("bad state near pc_live_secret123")

    client = PreclearedClient("GB", api_key="pc_live_secret123",
                              transport=httpx.MockTransport(broken))
    result = await client.clear(JANE, state)

    assert result.held and not result.pause_checks
    assert "RuntimeError" in result.reason
    assert "pc_live_secret123" not in result.reason


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
    await cycles(sender)
    assert sorted(m["to"] for m in provider.sent) == [JANE, SAM]
    assert await state.get_outbox(status="approved") == []


# ── what the held reason tells the operator ──


@pytest.mark.asyncio
async def test_keyless_daily_limit_points_to_a_free_key(state):
    client = PreclearedClient("GB", transport=mock_status(429, {
        "error": "daily_limit", "get_key_url": "https://precleared.dev/get-key",
    }))

    result = await client.clear("person@example.com", state)

    assert result.held and not result.allowed and result.pause_checks
    assert "add PRECLEARED_API_KEY" in result.reason
    assert "precleared.dev/get-key" in result.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("api_key", ["pc_live_test", ""], ids=["with-key", "without-key"])
async def test_rate_limit_is_not_reported_as_a_used_up_allowance(state, api_key):
    client = PreclearedClient("GB", api_key=api_key, transport=mock_status(429, {
        "error": "rate_limited", "message": "Slow down.", "retry_after": 20,
    }))

    result = await client.clear("person@example.com", state)

    assert result.held and not result.allowed and result.pause_checks
    assert "rate limit" in result.reason
    assert "add PRECLEARED_API_KEY" not in result.reason


@pytest.mark.asyncio
async def test_rejected_key_holds_with_a_clear_reason(state):
    client = PreclearedClient("GB", api_key="pc_live_test", transport=mock_status(401, {
        "error": "invalid_key",
    }))

    result = await client.clear("person@example.com", state)

    assert result.held and result.pause_checks
    assert "PRECLEARED_API_KEY was rejected" in result.reason


@pytest.mark.asyncio
async def test_key_out_of_checks_points_to_the_buy_page_not_a_free_key(state):
    client = PreclearedClient("GB", api_key="pc_live_test", transport=mock_status(402, {
        "error": "payment_required",
        "message": "This key has no checks left today. Buy a pack to continue.",
        "buy_url": "https://example.com/somewhere-else",
    }))

    result = await client.clear("person@example.com", state)

    assert result.held and not result.allowed and result.pause_checks
    assert BUY_URL in result.reason
    assert "add PRECLEARED_API_KEY" not in result.reason
    # Franco's own wording and link, never text or links from the answer.
    assert "Buy a pack to continue" not in result.reason
    assert "example.com/somewhere-else" not in result.reason
    # Nothing cached, so the next cycle asks again.
    assert await state.get_setting(SETTINGS_PREFIX + "person@example.com") == ""
