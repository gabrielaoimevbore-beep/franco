"""Sender — deploys campaigns via Instantly, or sends natively via the
configured mail provider (Gmail / SMTP) through the outbox approval ladder.

Native flow:
  1. Draft campaigns are STAGED: merge variables rendered per prospect,
     one outbox row per (prospect, step) with a scheduled send_at.
     Rows start as pending_review (copilot) or approved (autopilot).
  2. Each heartbeat DRAINS due approved rows: kill-switch check, stop-on-
     reply check, deterministic pre-send gate, then provider.send_email
     with jittered pacing and the daily cap.
"""

import asyncio
import logging
import random
import re
from datetime import date, datetime, timedelta, timezone

from franco.brain import Brain
from franco.config import FrancoConfig, EnvConfig
from franco.gate import pre_send_check
from franco.integrations.instantly import InstantlyClient
from franco.integrations.mail_provider import NATIVE_PROVIDERS, get_mail_provider
from franco.integrations.precleared import PreclearedClient
from franco.outreach import (
    add_business_days, build_footer, compliance_problem, local_date,
    send_at_utc, unsubscribe_headers, with_footer,
)
from franco.state import StateManager

logger = logging.getLogger("franco.sender")

# Max sends drained per heartbeat cycle — spreads volume through the day
# instead of bursting the daily cap in one minute.
MAX_SENDS_PER_CYCLE = 8
SEND_JITTER_SECONDS = (4, 15)

# Prospect pipeline statuses that mean "stop emailing this person" — and,
# because one reply speaks for the account, everyone else at their company.
STOP_STATUSES = {
    "replied", "opted_out", "lost", "meeting", "closed", "paused", "suppressed",
}

# A follow-up waits for the step before it; if that step will never go out,
# neither does the follow-up.
DEAD_STEP_STATUSES = {"rejected", "cancelled", "failed"}

KILL_SWITCH_KEY = "sending_paused"
BOUNCE_COUNT_KEY = "bounce_count"

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# Only prospects in these statuses may be added as leads. This guarantees
# we never re-send to someone already contacted, replied, opted out, or lost.
SENDABLE_STATUSES = {"new", "queued"}

# Email confidence levels safe to send to. "verified" only by default;
# "risky" (catch-all domains) is opt-in via config. Never send to a "guess"
# or "invalid" address — that bounces and burns the sending domain.
SENDABLE_EMAIL_STATUSES = {"verified"}
SENDABLE_EMAIL_STATUSES_WITH_RISKY = {"verified", "risky"}


class Sender:
    def __init__(
        self,
        brain: Brain,
        state: StateManager,
        config: FrancoConfig,
        env: EnvConfig,
    ):
        self.brain = brain
        self.state = state
        self.config = config
        self.instantly = InstantlyClient(env.instantly_api_key)
        self.provider = get_mail_provider(config, env)
        # Optional per-recipient check (compliance.precleared); None when off.
        self.precleared = PreclearedClient.from_config(config, env)
        if (self.precleared is not None and not self.is_native
                and getattr(config.channels.email, "enabled", True)):
            logger.warning(
                "Sender: compliance.precleared checks native sends only "
                "(gmail, smtp); campaigns deployed to Instantly are not checked."
            )
        # Disabled in tests to skip inter-send sleeps.
        self.send_pacing = True

    @property
    def is_native(self) -> bool:
        return (
            self.config.channels.email.provider in NATIVE_PROVIDERS
            and self.provider is not None
        )

    async def run(self):
        """Deploy campaigns — natively through the outbox, or via Instantly."""
        if not self.config.channels.email.enabled:
            logger.info("Sender: Email channel disabled. Skipping.")
            return

        if self.is_native:
            await self._run_native()
            return

        logger.info("Sender: Checking for campaigns to deploy...")
        draft_campaigns = await self.state.get_campaigns_by_status("draft")
        if not draft_campaigns:
            logger.info("Sender: No draft campaigns to deploy.")
            return

        # Enforce daily send limit
        max_sends = self.config.channels.email.max_daily_sends
        sends_today = await self._count_sends_today()
        remaining = max_sends - sends_today
        if remaining <= 0:
            logger.info(f"Sender: Daily send limit reached ({sends_today}/{max_sends}). Skipping.")
            return

        for campaign in draft_campaigns:
            try:
                await self._deploy_campaign(campaign)
            except Exception as e:
                logger.error(f"Sender: Failed to deploy campaign {campaign.name}: {e}")

    def _validate_sequence(self, campaign) -> bool:
        """Never deploy a broken or empty sequence."""
        if not campaign.sequence:
            logger.error(f"Sender: Campaign '{campaign.name}' has no email sequence. Marking failed.")
            return False
        for step in campaign.sequence:
            if not (step.subject or "").strip() or not (step.body or "").strip():
                logger.error(
                    f"Sender: Campaign '{campaign.name}' step {step.step} has an empty "
                    "subject or body. Marking failed."
                )
                return False
            if step.delay_days < 0:
                logger.error(
                    f"Sender: Campaign '{campaign.name}' step {step.step} has a negative delay."
                )
                return False
        return True

    async def _deploy_campaign(self, campaign):
        """Deploy a single campaign to Instantly. Idempotent: safe to retry."""
        logger.info(f"Sender: Deploying campaign '{campaign.name}'...")

        # 0. Validate before touching the network
        if not self._validate_sequence(campaign):
            await self.state.update_campaign(campaign.id, status="failed")
            return

        # 1. Create campaign in Instantly — or resume one from a previous
        # partially-failed deploy. Never create a duplicate.
        campaign_id = campaign.instantly_campaign_id
        if campaign_id:
            logger.info(
                f"Sender: Campaign '{campaign.name}' already has Instantly ID "
                f"{campaign_id}. Resuming deploy instead of recreating."
            )
        else:
            instantly_campaign = await self.instantly.create_campaign(campaign.name)
            if not instantly_campaign or not isinstance(instantly_campaign, dict):
                logger.error(f"Sender: Failed to create Instantly campaign: {campaign.name}")
                return

            campaign_id = instantly_campaign.get("id")
            if not campaign_id:
                logger.error("Sender: No campaign ID returned from Instantly.")
                return

            # Persist the ID immediately so a crash mid-deploy resumes this
            # campaign instead of creating a second one (double-send guard).
            await self.state.update_campaign(campaign.id, instantly_campaign_id=campaign_id)
            campaign.instantly_campaign_id = campaign_id

        # 2. Set email sequence
        sequences = [
            {
                "subject": step.subject.strip(),
                "body": step.body.strip(),
                "wait": step.delay_days,
            }
            for step in campaign.sequence
        ]

        result = await self.instantly.set_campaign_emails(campaign_id, sequences)
        if result is None:
            logger.error(f"Sender: Failed to set emails for campaign {campaign_id}")
            return

        # 3. Add leads — validated, deduped, never-contacted, deliverable-only
        allow_risky = getattr(self.config.channels.email, "send_to_risky", False)
        sendable_email = (
            SENDABLE_EMAIL_STATUSES_WITH_RISKY if allow_risky
            else SENDABLE_EMAIL_STATUSES
        )
        prospects = []
        seen_emails: set[str] = set()
        skipped_unverified = 0
        for prospect_id in campaign.prospect_ids:
            prospect = await self.state.get_prospect(prospect_id)
            if not prospect or not prospect.email:
                continue
            email = prospect.email.strip().lower()
            if not EMAIL_RE.match(email):
                logger.warning(f"Sender: Skipping invalid email '{prospect.email}'")
                continue
            if email in seen_emails:
                continue
            if prospect.status not in SENDABLE_STATUSES:
                # Already contacted / replied / opted out — never double-send.
                logger.debug(
                    f"Sender: Skipping {email} (status '{prospect.status}' is not sendable)."
                )
                continue
            # Deliverability gate: never send to unverified/guessed addresses —
            # bounces are the fastest way to torch a sending domain.
            if (prospect.email_status or "guess") not in sendable_email:
                skipped_unverified += 1
                logger.debug(
                    f"Sender: Skipping {email} (email_status "
                    f"'{prospect.email_status or 'guess'}' not deliverable)."
                )
                continue
            seen_emails.add(email)
            prospects.append(prospect)

        if skipped_unverified:
            logger.info(
                f"Sender: Held back {skipped_unverified} prospect(s) with "
                f"unverified emails from '{campaign.name}'."
            )

        if not prospects:
            # Retry path: leads were already staged and marked 'contacted'
            # on a previous cycle but activation failed. Just activate.
            already_staged = False
            for pid in campaign.prospect_ids:
                p = await self.state.get_prospect(pid)
                if p and p.status == "contacted":
                    already_staged = True
                    break
            if already_staged:
                logger.info(
                    f"Sender: Leads for '{campaign.name}' already staged. Retrying activation."
                )
                if await self.instantly.activate_campaign(campaign_id) is not None:
                    await self.state.update_campaign(campaign.id, status="active")
                    logger.info(f"Sender: Campaign '{campaign.name}' activated on retry.")
                return
            logger.warning(f"Sender: No valid prospects for campaign {campaign.name}")
            return

        # Check remaining daily send budget
        max_sends = self.config.channels.email.max_daily_sends
        sends_today = await self._count_sends_today()
        remaining = max_sends - sends_today
        if remaining <= 0:
            logger.info(f"Sender: Daily limit reached. Deferring campaign '{campaign.name}'.")
            return
        if len(prospects) > remaining:
            logger.info(f"Sender: Capping leads from {len(prospects)} to {remaining} (daily limit).")
            prospects = prospects[:remaining]

        leads = [
            {
                "email": p.email.strip().lower(),
                "first_name": p.first_name,
                "last_name": p.last_name,
                "company_name": p.company,
                "variables": {
                    "title": p.title,
                    "personalization": p.personalization_notes,
                },
            }
            for p in prospects
        ]

        result = await self.instantly.add_leads(campaign_id, leads)
        if result is None:
            logger.error(f"Sender: Failed to add leads to campaign {campaign_id}")
            return

        # Mark prospects as contacted BEFORE activation: if activation
        # succeeds but this write failed, a retry would re-add the same
        # leads. Better to under-count than double-send.
        for prospect in prospects:
            await self.state.update_prospect_status(prospect.id, "contacted")

        # 4. Activate campaign
        result = await self.instantly.activate_campaign(campaign_id)
        if result is None:
            logger.error(
                f"Sender: Failed to activate campaign {campaign_id}. "
                "Leads are staged; will retry activation next cycle."
            )
            return

        # 5. Update our records
        await self.state.update_campaign(
            campaign.id,
            instantly_campaign_id=campaign_id,
            status="active",
        )

        await self.state.log_action(
            action_type="send_campaign",
            agent="sender",
            details={
                "campaign_name": campaign.name,
                "instantly_campaign_id": campaign_id,
                "leads_added": len(leads),
            },
        )

        logger.info(
            f"Sender: Campaign '{campaign.name}' deployed to Instantly "
            f"with {len(leads)} leads. Campaign ID: {campaign_id}"
        )

    async def _count_sends_today(self) -> int:
        """Count how many prospects were contacted today."""
        import aiosqlite
        today = date.today().isoformat()
        async with aiosqlite.connect(self.state.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM prospects WHERE status = 'contacted' AND updated_at LIKE ?",
                (f"{today}%",),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    # ── Native provider flow (Gmail / SMTP via the outbox) ──

    async def _run_native(self):
        """Stage new campaigns into the outbox, then drain what's due."""
        paused = await self.state.get_setting(KILL_SWITCH_KEY)
        if paused:
            logger.warning(
                f"Sender: SENDING PAUSED ({paused}). Resume from the dashboard "
                "or `franco sending resume` once the cause is fixed."
            )
            return

        for campaign in await self.state.get_campaigns_by_status("draft"):
            try:
                await self._stage_campaign_native(campaign)
            except Exception as e:
                logger.error(f"Sender: staging '{campaign.name}' failed: {e}")

        await self._drain_due()

    def _render(self, text: str, prospect) -> str:
        """Fill merge variables. The pre-send gate rejects any leftovers."""
        replacements = {
            "first_name": prospect.first_name,
            "last_name": prospect.last_name,
            "company": prospect.company,
            "title": prospect.title,
            "personalization": prospect.personalization_notes,
        }
        for key, value in replacements.items():
            text = text.replace("{{" + key + "}}", value or "")
            text = text.replace("{{ " + key + " }}", value or "")
        return text.strip()

    async def _stage_campaign_native(self, campaign):
        """Render + schedule a draft campaign's emails into the outbox."""
        if not self._validate_sequence(campaign):
            await self.state.update_campaign(campaign.id, status="failed")
            return

        require_approval = await self.state.approval_required(
            getattr(self.config.channels.email, "require_approval", True))
        initial_status = "pending_review" if require_approval else "approved"
        allow_risky = getattr(self.config.channels.email, "send_to_risky", False)
        sendable_email = (
            SENDABLE_EMAIL_STATUSES_WITH_RISKY if allow_risky
            else SENDABLE_EMAIL_STATUSES
        )

        staged = 0
        skipped = 0
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        seen_emails: set[str] = set()
        compliance = getattr(self.config, "compliance", None)
        footer = build_footer(compliance, self.config.persona) if compliance else ""

        for prospect_id in campaign.prospect_ids:
            prospect = await self.state.get_prospect(prospect_id)
            if not prospect or not prospect.email:
                continue
            email = prospect.email.strip().lower()
            if not EMAIL_RE.match(email) or email in seen_emails:
                continue
            if prospect.status not in SENDABLE_STATUSES:
                continue
            if (prospect.email_status or "guess") not in sendable_email:
                skipped += 1
                continue
            seen_emails.add(email)

            cumulative_days = 0
            for step in campaign.sequence:
                cumulative_days += max(0, step.delay_days)
                send_at = now + timedelta(days=cumulative_days)
                item_id = await self.state.add_outbox_item(
                    prospect_id=prospect.id,
                    campaign_id=campaign.id,
                    step=step.step,
                    to_email=email,
                    subject=self._render(step.subject, prospect),
                    body=with_footer(self._render(step.body, prospect), footer),
                    send_at=send_at.isoformat(),
                    status=initial_status,
                    provider=self.provider.name,
                )
                if item_id:
                    staged += 1
            await self.state.update_prospect_status(prospect.id, "queued")

        await self.state.update_campaign(campaign.id, status="active")
        if skipped:
            logger.info(
                f"Sender: held back {skipped} prospect(s) with unverified "
                f"emails from '{campaign.name}'."
            )
        if staged:
            mode = "awaiting your approval" if require_approval else "approved"
            logger.info(
                f"Sender: staged {staged} email(s) for '{campaign.name}' "
                f"({mode}). Review in the dashboard Outbox tab."
            )
            await self.state.log_action(
                action_type="stage_campaign",
                agent="sender",
                details={"campaign": campaign.name, "staged": staged, "mode": mode},
            )

    async def _sequence_hold(self, item: dict, prospect) -> str:
        """Whether a due cold email must not go out now.

        Returns "" to send, "defer" to wait (the previous step hasn't gone
        yet), or the reason it will never be sent (it gets cancelled).
        """
        if prospect.status in STOP_STATUSES:
            return f"prospect status '{prospect.status}'"

        # Approved copy that hasn't been signed off stays in preview.
        if await self._awaiting_signoff(item.get("campaign_id", "")):
            return "defer"

        blocked = await self.state.suppression_for(item["to_email"])
        if blocked:
            return f"suppressed ({blocked['kind']}: {blocked['reason'] or 'listed'})"

        # One reply (or opt-out) speaks for the whole account.
        if prospect.company_id:
            for other in await self.state.get_contacts_for_company(prospect.company_id):
                if other.id != prospect.id and other.status in STOP_STATUSES:
                    return f"account stopped ({other.email or other.full_name()}: {other.status})"

        # Follow-ups go only after the step before them actually went out.
        step = int(item.get("step") or 1)
        if step > 1 and item.get("campaign_id"):
            prev = await self.state.get_outbox_step(
                item["campaign_id"], item["prospect_id"], step - 1
            )
            if prev is not None and prev["status"] != "sent":
                if prev["status"] in DEAD_STEP_STATUSES:
                    return f"step {step - 1} was {prev['status']}"
                return "defer"
        return ""

    async def _awaiting_signoff(self, campaign_id: str) -> bool:
        outreach = getattr(self.config, "outreach", None)
        if outreach is None or outreach.preview_accounts <= 0 or not campaign_id:
            return False
        cache = self.__dict__.setdefault("_signoff_cache", {})
        if campaign_id not in cache:
            key = await self.state.campaign_key_for(campaign_id)
            cache[campaign_id] = bool(
                key and key in outreach.sequence_keys()
                and not await self.state.get_signoff(key))
        return cache[campaign_id]

    async def _retime_followups(self, item: dict):
        """Approved copy is spaced in business days from when each email
        actually went out — an email approved late doesn't squash the gap
        to the next one."""
        outreach = getattr(self.config, "outreach", None)
        if outreach is None:
            return
        sent_day = local_date(datetime.now(timezone.utc).replace(tzinfo=None),
                              outreach.timezone)
        for later in await self.state.get_later_steps(
            item["campaign_id"], item["prospect_id"], item["step"]
        ):
            gap = int(later.get("seq_day") or 0) - int(item["seq_day"])
            if gap <= 0:
                continue
            day = add_business_days(sent_day, gap)
            await self.state.update_outbox_item(
                later["id"],
                send_at=send_at_utc(day, outreach.send_hour, outreach.timezone),
            )

    async def _drain_due(self):
        """Send due, approved outbox items through the provider."""
        if not self.provider.is_configured():
            logger.warning(
                f"Sender: mail provider '{self.provider.name}' is not configured "
                "yet — outbox is holding. See CLAUDE.md → provider setup."
            )
            return

        max_daily = self.config.channels.email.max_daily_sends
        sent_today = await self.state.count_outbox_sent_today()
        budget = min(MAX_SENDS_PER_CYCLE, max_daily - sent_today)
        if budget <= 0:
            logger.info(f"Sender: daily send cap reached ({sent_today}/{max_daily}).")
            return

        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        due = await self.state.get_outbox(
            status="approved", due_before=now, limit=budget * 3
        )
        # Held cold email (an unavailable compliance check, a missing postal
        # address, a follow-up waiting for its step) stays approved and could
        # fill the page above, so replies are also fetched on their own and
        # never wait behind it. Bounded: one extra query of the same size.
        seen = {item["id"] for item in due}
        replies = await self.state.get_outbox(
            status="approved", due_before=now, limit=budget * 3,
            exclude_kind="sequence",
        )
        extra = [item for item in replies if item["id"] not in seen]
        if extra:
            due = sorted(
                due + extra,
                key=lambda i: (i.get("send_at") or "", i.get("created_at") or ""),
            )
        if not due:
            return
        self._signoff_cache = {}   # sign-off can change between cycles

        allow_risky = getattr(self.config.channels.email, "send_to_risky", False)
        compliance = getattr(self.config, "compliance", None)
        blocked_claims = tuple(getattr(compliance, "blocked_claims", ()) or ())
        footer = build_footer(compliance, self.config.persona) if compliance else ""
        cold_blocked = compliance_problem(compliance)
        if cold_blocked:
            logger.error(f"Sender: not sending cold email: {cold_blocked}.")
        sent = 0
        # One step per person per cycle: sending step 1 re-times step 2 to a
        # later business day, but step 2's stale row may still be in `due`.
        sent_sequences: set[tuple[str, str]] = set()
        # Optional compliance check (compliance.precleared). Once Precleared
        # can't be asked (allowance used up, network), cold email that needs
        # a fresh check waits for a later cycle; replies still go out. Fresh
        # checks are capped per cycle so slow answers can't stall the cycle.
        compliance_paused = False
        fresh_checks = 0
        for item in due:
            if sent >= budget:
                break
            if item["kind"] == "sequence" and \
                    (item.get("campaign_id"), item["prospect_id"]) in sent_sequences:
                continue

            prospect = await self.state.get_prospect(item["prospect_id"])
            if prospect is None:
                await self.state.update_outbox_item(
                    item["id"], status="failed", error="prospect missing"
                )
                continue

            if item["kind"] == "sequence":
                if cold_blocked:
                    continue  # held, not failed: fixing the config releases it
                hold = await self._sequence_hold(item, prospect)
                if hold == "defer":
                    continue
                if hold:
                    await self.state.update_outbox_item(
                        item["id"], status="cancelled", error=hold
                    )
                    continue

            gate = pre_send_check(
                item["to_email"], item["subject"], item["body"],
                prospect=prospect, allow_risky=allow_risky, kind=item["kind"],
                required_footer=footer, blocked_phrases=blocked_claims,
            )
            if not gate:
                await self.state.update_outbox_item(
                    item["id"], status="failed",
                    error="gate: " + "; ".join(gate.reasons)[:300],
                )
                logger.warning(
                    f"Sender: gate blocked email to {item['to_email']}: "
                    f"{gate.reasons}"
                )
                continue

            # One check per address, before the first cold email, and only
            # for email the gate has passed. Replies are never checked.
            receipt_id = ""
            if self.precleared is not None and item["kind"] == "sequence":
                clearance = await self.precleared.cached(item["to_email"], self.state)
                if clearance is None:
                    if compliance_paused or fresh_checks >= budget:
                        continue  # asked on a later cycle
                    fresh_checks += 1
                    clearance = await self.precleared.check(item["to_email"], self.state)
                    if clearance.held:
                        compliance_paused = True
                        logger.warning(
                            "Sender: compliance check unavailable, holding cold "
                            f"email until a later cycle: {clearance.reason}"
                        )
                        continue
                    # A check can take seconds, and anything may change
                    # meanwhile (a rejection, a suppression, a reply, the kill
                    # switch), so Franco's own checks run again on fresh data.
                    # If one now stops the email, the next cycle records why;
                    # the verdict is kept, so that costs no second check.
                    if await self.state.get_setting(KILL_SWITCH_KEY):
                        logger.warning("Sender: sending was paused during the cycle; stopping.")
                        break
                    fresh = await self.state.get_outbox_item(item["id"])
                    prospect = await self.state.get_prospect(item["prospect_id"])
                    if (fresh is None or fresh["status"] != "approved" or prospect is None
                            or await self._sequence_hold(fresh, prospect)
                            or not pre_send_check(
                                fresh["to_email"], fresh["subject"], fresh["body"],
                                prospect=prospect, allow_risky=allow_risky,
                                kind=fresh["kind"], required_footer=footer,
                                blocked_phrases=blocked_claims,
                            )):
                        continue
                    item = fresh
                if not clearance.allowed:
                    await self.state.update_outbox_item(
                        item["id"], status="failed",
                        error=f"precleared: {clearance.verdict}: {clearance.reason}"[:300],
                    )
                    logger.warning(
                        f"Sender: Precleared stopped email to {item['to_email']}: "
                        f"{clearance.verdict} ({clearance.reason})"
                    )
                    continue
                receipt_id = clearance.receipt_id

            send_kwargs = {
                "thread_ref": item.get("thread_ref", ""),
                "in_reply_to": item.get("in_reply_to", ""),
            }
            headers = (unsubscribe_headers(compliance, self.config.persona)
                       if item["kind"] == "sequence" else {})
            if headers:
                send_kwargs["headers"] = headers
            result = await self.provider.send_email(
                item["to_email"], item["subject"], item["body"], **send_kwargs
            )
            if not result.ok:
                await self.state.update_outbox_item(
                    item["id"], status="failed", error=result.error[:300]
                )
                continue

            sent += 1
            now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            await self.state.update_outbox_item(
                item["id"], status="sent", sent_at=now_iso,
                message_id=result.message_id, thread_ref=result.thread_ref,
            )
            if prospect.status in ("new", "queued"):
                await self.state.update_prospect_status(prospect.id, "contacted")
            if item["kind"] == "sequence":
                sent_sequences.add((item.get("campaign_id"), item["prospect_id"]))
                if item.get("seq_day"):
                    await self._retime_followups(item)

            # A sent reply belongs in its conversation thread.
            if item["kind"] == "reply" and item.get("conversation_id"):
                try:
                    convo = await self.state.get_conversation(item["conversation_id"])
                    if convo:
                        from franco.models.conversation import Message
                        convo.thread.append(
                            Message(sender="franco", content=item["body"])
                        )
                        await self.state.update_conversation(
                            convo.id, thread_json=convo.thread_json()
                        )
                except Exception as e:
                    logger.debug(f"Sender: convo append failed: {e}")
            details = {
                "to": item["to_email"], "step": item["step"],
                "kind": item["kind"], "provider": self.provider.name,
            }
            if receipt_id:
                details["precleared_receipt"] = receipt_id
            await self.state.log_action(
                action_type="email_sent",
                agent="sender",
                details=details,
            )
            logger.info(
                f"Sender: sent step {item['step']} to {item['to_email']} "
                f"via {self.provider.name}."
            )
            # Human-ish pacing BETWEEN sends (not after the last, and never
            # in tests where pacing is disabled).
            if self.send_pacing and sent < budget:
                await asyncio.sleep(random.uniform(*SEND_JITTER_SECONDS))

        if sent:
            logger.info(f"Sender: {sent} email(s) sent this cycle "
                        f"({sent_today + sent}/{max_daily} today).")
