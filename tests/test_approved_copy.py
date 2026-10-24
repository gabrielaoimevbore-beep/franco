"""Approved-copy outreach end to end: the template writer stages verbatim
sequences for imported accounts (with the preview cap), and the sender
holds, orders, stops and re-times them."""

from datetime import date, datetime, timedelta, timezone

import pytest
import pytz

from franco.models.campaign import Campaign, EmailStep
from franco.models.company import Company
from franco.models.prospect import Prospect
from franco.outreach import (
    add_business_days, build_footer, first_send_day, local_date, send_at_utc,
)
from tests.phase1_helpers import (
    ADDRESS, PAST, RecordingProvider, account_row, import_accounts, make_config,
    make_sender, make_writer,
)
from tests.test_outbox_native import state  # noqa: F401  (fixture)


async def outbox(state, status=None):
    return await state.get_outbox(status=status, limit=500)


async def stage(state, config, rows, campaigns=("M1",)):
    await import_accounts(state, rows, campaigns)
    await make_writer(state, config).run()


# ── writer: what gets staged ──


@pytest.mark.asyncio
async def test_stages_every_step_with_footer_business_days_and_seq_day(state):
    config = make_config()
    await stage(state, config, [account_row(1)])

    items = sorted(await outbox(state), key=lambda i: i["step"])
    assert [i["step"] for i in items] == [1, 2, 3]
    assert [i["seq_day"] for i in items] == [1, 5, 9]
    footer = build_footer(config.compliance, config.persona)
    assert footer.endswith('Reply "no thanks" to stop.') and ADDRESS in footer
    assert all(i["body"].endswith(footer) for i in items)
    assert items[0]["subject"] == "Sponsored listings at Co1"
    assert "I noticed your seller help center describes a paid Featured Listing upgrade." \
        in items[0]["body"]
    assert all(i["status"] == "pending_review" for i in items)

    tz = config.outreach.timezone
    day1 = first_send_day(datetime.now(pytz.timezone(tz)), 9)
    assert [i["send_at"] for i in items] == [
        send_at_utc(add_business_days(day1, d - 1), 9, tz) for d in (1, 5, 9)]

    p = await state.get_prospect_by_email("dana1@co1.com")
    assert p.status == "queued"
    campaigns = await state.get_campaigns_by_status("active")
    assert campaigns[0].campaign_key == "M1"


@pytest.mark.asyncio
async def test_skips_unverified_unknown_campaign_and_stale_observation(state):
    config = make_config()
    old = (date.today() - timedelta(days=200)).isoformat()
    await import_accounts(state, [
        account_row(1, email_status="guess"),
        account_row(2, campaign="F1"),            # imported, but not configured
        account_row(3, source_date=old),          # older than 90 days
    ], campaigns=("M1", "F1"))
    await make_writer(state, config).run()
    assert await outbox(state) == []
    for i in (1, 2, 3):
        assert (await state.get_prospect_by_email(f"dana{i}@co{i}.com")).status == "new"


@pytest.mark.asyncio
async def test_skips_account_without_any_observation(state):
    config = make_config()
    cid = await state.add_company(Company(name="Bare", domain="bare.com"))
    await state.update_company_account(cid, campaign_key="M1", owner="Hemin")
    await state.add_prospect(Prospect(company_id=cid, first_name="A", last_name="B",
                                      email="a@bare.com", email_status="verified"))
    await make_writer(state, config).run()
    assert await outbox(state) == []


@pytest.mark.asyncio
async def test_unfilled_merge_field_skips_the_account(state):
    steps = [{"day": 1, "subject": "Hi {{first_name}}", "body": "About {{funding_round}}."}]
    config = make_config(outreach={"campaigns": {"M1": {"steps": steps}}})
    await stage(state, config, [account_row(1)])
    assert await outbox(state) == []
    assert (await state.get_prospect_by_email("dana1@co1.com")).status == "new"


@pytest.mark.asyncio
async def test_one_contact_per_account(state):
    config = make_config()
    await stage(state, config, [account_row(1), account_row(1, email="lee@co1.com",
                                                            first_name="Lee")])
    staged = {i["to_email"] for i in await outbox(state)}
    assert len(staged) == 1
    # The next cycle doesn't start the second contact either.
    await make_writer(state, config).run()
    assert len({i["to_email"] for i in await outbox(state)}) == 1


@pytest.mark.asyncio
async def test_suppressed_prospect_is_marked_and_skipped(state):
    config = make_config()
    await import_accounts(state, [account_row(1)])
    await state.add_suppression("dana1@co1.com", reason="asked")
    await make_writer(state, config).run()
    assert await outbox(state) == []
    assert (await state.get_prospect_by_email("dana1@co1.com")).status == "suppressed"


@pytest.mark.asyncio
async def test_blank_postal_address_stages_nothing(state):
    config = make_config(compliance={"postal_address": ""})
    await stage(state, config, [account_row(1)])
    assert await outbox(state) == []


# ── writer: preview ──


@pytest.mark.asyncio
async def test_preview_caps_accounts_and_ignores_autopilot(state):
    config = make_config(outreach={"preview_accounts": 2})
    await state.set_autopilot("on")
    await stage(state, config, [account_row(i) for i in (1, 2, 3)])

    items = await outbox(state)
    assert len({i["to_email"] for i in items}) == 2
    assert all(i["status"] == "pending_review" for i in items)
    assert await state.staged_accounts_by_owner("M1") == {"Hemin": 2}

    # Another cycle while still unsigned stages nothing more.
    await make_writer(state, config).run()
    assert len({i["to_email"] for i in await outbox(state)}) == 2

    await state.set_signoff("M1", "Hemin")
    await make_writer(state, config).run()
    items = await outbox(state)
    assert len({i["to_email"] for i in items}) == 3
    third = [i for i in items if i["status"] == "approved"]
    assert len(third) == 3 and len({i["to_email"] for i in third}) == 1


@pytest.mark.asyncio
async def test_preview_counts_per_owner(state):
    config = make_config(outreach={"preview_accounts": 1})
    await stage(state, config, [account_row(1, owner="A"), account_row(2, owner="A"),
                                account_row(3, owner="B")])
    assert await state.staged_accounts_by_owner("M1") == {"A": 1, "B": 1}


@pytest.mark.asyncio
async def test_preview_off_respects_autopilot(state):
    config = make_config(outreach={"preview_accounts": 0})
    await state.set_autopilot("on")
    await stage(state, config, [account_row(1)])
    assert {i["status"] for i in await outbox(state)} == {"approved"}


# ── sender: preview hold ──


async def make_due(state):
    for item in await outbox(state):
        await state.update_outbox_item(item["id"], send_at=PAST)


@pytest.mark.asyncio
async def test_unsigned_preview_is_held_then_sent_after_signoff(state):
    config = make_config()
    await stage(state, config, [account_row(1)])
    await state.approve_outbox()
    await make_due(state)
    provider = RecordingProvider()
    sender = make_sender(state, config, provider)

    await sender._drain_due()
    assert provider.sent == []
    assert {i["status"] for i in await outbox(state)} == {"approved"}

    await state.set_signoff("M1", "Hemin")
    await sender._drain_due()
    assert provider.sent[0]["subject"] == "Sponsored listings at Co1"
    assert provider.sent[0]["headers"] == {
        "List-Unsubscribe": "<mailto:sender@example.com?subject=unsubscribe>"}


# ── sender: sequence rules (no preview: a campaign without a key) ──


async def seed_sequence(state, prospect_id, email, statuses=("approved", "approved"),
                        body="Short note.", company_id=""):
    camp = Campaign(id="", name="plain", channel="email", status="active",
                    sequence=[EmailStep(step=1, subject="s1", body="b"),
                              EmailStep(step=2, subject="s2", body="b")],
                    prospect_ids=[prospect_id])
    cid = await state.add_campaign(camp)
    for step, status in zip((1, 2), statuses):
        await state.add_outbox_item(prospect_id=prospect_id, to_email=email,
                                    subject=f"step {step}", body=body, send_at=PAST,
                                    status=status, campaign_id=cid, step=step,
                                    seq_day=1 if step == 1 else 5)
    return cid


def plain_config(**over):
    return make_config(compliance={"require_footer": False}, **over)


async def person(state, email="jane@acme.com", company_id="", status="new"):
    return await state.add_prospect(Prospect(
        company_id=company_id, first_name="Jane", last_name="Doe", title="VP",
        email=email, email_status="verified", status=status))


@pytest.mark.asyncio
async def test_step_two_waits_for_step_one(state):
    pid = await person(state)
    await seed_sequence(state, pid, "jane@acme.com", statuses=("pending_review", "approved"))
    provider = RecordingProvider()
    await make_sender(state, plain_config(), provider)._drain_due()
    assert provider.sent == []
    assert {i["step"]: i["status"] for i in await outbox(state)} == {
        1: "pending_review", 2: "approved"}


@pytest.mark.asyncio
async def test_step_two_cancelled_when_step_one_rejected(state):
    pid = await person(state)
    await seed_sequence(state, pid, "jane@acme.com", statuses=("rejected", "approved"))
    await make_sender(state, plain_config())._drain_due()
    step2 = next(i for i in await outbox(state) if i["step"] == 2)
    assert step2["status"] == "cancelled" and step2["error"] == "step 1 was rejected"


@pytest.mark.asyncio
async def test_step_two_sends_after_step_one_went(state):
    pid = await person(state)
    await seed_sequence(state, pid, "jane@acme.com", statuses=("sent", "approved"))
    provider = RecordingProvider()
    await make_sender(state, plain_config(), provider)._drain_due()
    assert [s["subject"] for s in provider.sent] == ["step 2"]


@pytest.mark.asyncio
async def test_overdue_follow_up_waits_for_its_retimed_slot(state):
    config = plain_config()
    pid = await person(state)
    await seed_sequence(state, pid, "jane@acme.com")
    provider = RecordingProvider()
    await make_sender(state, config, provider)._drain_due()
    assert [s["subject"] for s in provider.sent] == ["step 1"]
    step2 = next(i for i in await outbox(state) if i["step"] == 2)
    tz = config.outreach.timezone
    today = local_date(datetime.now(timezone.utc).replace(tzinfo=None), tz)
    assert step2["status"] == "approved"
    assert step2["send_at"] == send_at_utc(add_business_days(today, 4), 9, tz)


@pytest.mark.asyncio
async def test_account_stop_when_a_colleague_replied(state):
    cid = await state.add_company(Company(name="Acme", domain="acme.com"))
    await person(state, "boss@acme.com", company_id=cid, status="replied")
    pid = await person(state, "jane@acme.com", company_id=cid)
    await seed_sequence(state, pid, "jane@acme.com")
    provider = RecordingProvider()
    await make_sender(state, plain_config(), provider)._drain_due()
    assert provider.sent == []
    step1 = next(i for i in await outbox(state) if i["step"] == 1)
    assert step1["status"] == "cancelled" and "account stopped" in step1["error"]


@pytest.mark.asyncio
async def test_suppressed_domain_is_cancelled_at_send_time(state):
    pid = await person(state)
    await seed_sequence(state, pid, "jane@acme.com")
    await state.add_suppression("acme.com", reason="competitor")
    provider = RecordingProvider()
    await make_sender(state, plain_config(), provider)._drain_due()
    assert provider.sent == []
    step1 = next(i for i in await outbox(state) if i["step"] == 1)
    assert step1["status"] == "cancelled" and "suppressed" in step1["error"]


@pytest.mark.asyncio
async def test_cold_mail_held_not_failed_without_postal_address(state):
    config = make_config(compliance={"postal_address": ""})
    pid = await person(state)
    await seed_sequence(state, pid, "jane@acme.com")
    await state.add_outbox_item(prospect_id=pid, to_email="jane@acme.com", kind="reply",
                                subject="Re: hi", body="Thanks!", send_at=PAST,
                                status="approved", conversation_id="c1")
    provider = RecordingProvider()
    await make_sender(state, config, provider)._drain_due()
    # Replies still go (no footer needed, no unsubscribe headers); cold mail waits.
    assert [(s["subject"], s["headers"]) for s in provider.sent] == [("Re: hi", None)]
    assert {i["status"] for i in await outbox(state) if i["kind"] == "sequence"} == {"approved"}


@pytest.mark.asyncio
async def test_held_cold_backlog_never_delays_a_due_reply(state):
    # More held cold email than one page of the outbox drain: the reply,
    # due later than all of it, still goes out this cycle.
    config = make_config(compliance={"postal_address": ""})
    pid = await person(state, email="jane@example.com")
    for step in range(1, 31):
        await state.add_outbox_item(prospect_id=pid, to_email="jane@example.com",
                                    subject=f"step {step}", body="Note.", send_at=PAST,
                                    status="approved", campaign_id="c1", step=step)
    await state.add_outbox_item(prospect_id=pid, to_email="jane@example.com", kind="reply",
                                subject="Re: hi", body="Thanks!",
                                send_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                                status="approved", conversation_id="c1")
    provider = RecordingProvider()
    await make_sender(state, config, provider)._drain_due()
    assert [s["subject"] for s in provider.sent] == ["Re: hi"]
    assert len(await outbox(state, "approved")) == 30


@pytest.mark.asyncio
async def test_unsubscribe_headers_only_on_sequence_mail(state):
    config = make_config(outreach={"campaigns": {}})
    footer = build_footer(config.compliance, config.persona)
    pid = await person(state)
    await seed_sequence(state, pid, "jane@acme.com", body="Note.\n\n" + footer)
    await state.add_outbox_item(prospect_id=pid, to_email="jane@acme.com", kind="reply",
                                subject="Re: hi", body="Thanks!", send_at=PAST,
                                status="approved", conversation_id="c1")
    provider = RecordingProvider()
    await make_sender(state, config, provider)._drain_due()
    by_subject = {s["subject"]: s["headers"] for s in provider.sent}
    assert by_subject["step 1"]["List-Unsubscribe"].startswith("<mailto:sender@example.com")
    assert by_subject["Re: hi"] is None


@pytest.mark.asyncio
async def test_missing_footer_fails_the_gate(state):
    pid = await person(state)
    await seed_sequence(state, pid, "jane@acme.com", body="No footer here.")
    provider = RecordingProvider()
    await make_sender(state, make_config(outreach={"campaigns": {}}), provider)._drain_due()
    assert provider.sent == []
    step1 = next(i for i in await outbox(state) if i["step"] == 1)
    assert step1["status"] == "failed" and "compliance footer" in step1["error"]


@pytest.mark.asyncio
async def test_retime_followups_spaces_by_business_days(state):
    config = plain_config()
    pid = await person(state)
    cid = await seed_sequence(state, pid, "jane@acme.com")
    await state.add_outbox_item(prospect_id=pid, to_email="jane@acme.com", subject="step 3",
                                body="b", send_at=PAST, status="approved",
                                campaign_id=cid, step=3, seq_day=14)
    sender = make_sender(state, config)
    await sender._retime_followups({"campaign_id": cid, "prospect_id": pid,
                                    "step": 1, "seq_day": 1})
    tz = config.outreach.timezone
    today = local_date(datetime.now(timezone.utc).replace(tzinfo=None), tz)
    by_step = {i["step"]: i["send_at"] for i in await outbox(state)}
    assert by_step[2] == send_at_utc(add_business_days(today, 4), 9, tz)
    assert by_step[3] == send_at_utc(add_business_days(today, 13), 9, tz)
    assert by_step[1] == PAST
