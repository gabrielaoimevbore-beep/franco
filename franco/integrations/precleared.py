"""Optional per-recipient cold-email check through Precleared (precleared.dev).

Off by default. With ``compliance.precleared.enabled: true``, the sender asks
Precleared about each recipient before the first cold email of a sequence:
may a cold marketing email go to this address under the rules of the
recipient's country (UK PECR, US CAN-SPAM, Canada's CASL, Germany's UWG and
others)? Those rules turn on facts the address doesn't show, such as whether
a UK recipient is a limited company or a sole trader, so Precleared looks
them up. Anything other than ``ok_to_email`` stops the email, with the rule
and the next step, and the signed receipt id is logged with the send so the
decision can be shown later.

One check per address: a definite verdict is cached in the settings table for
CACHE_DAYS, together with a fingerprint of everything sent with the address
(sender country, address source, product description and, if set, prospect
country). A cached verdict is reused only while those are unchanged, so a
three-step sequence costs one check but a config change forces a fresh one.
Replies are never checked. Precleared receives the recipient's address, your
sender country, the address source you configure, the first 200 characters of
your product description (used to judge relevance under CASL and French
rules) and, if you set one, your prospects' country, which it uses only when
it can't work out the recipient's country from the address, company records
or the company's website.

A check that doesn't produce a verdict (network trouble, daily allowance used
up, a rate limit, a rejected key, a malformed answer) holds cold email for a
later cycle instead of failing it. Replies still go out.

Without an API key Precleared allows a few checks a day per IP address; a free
key (https://precleared.dev/get-key, PRECLEARED_API_KEY) gives this install its
own daily allowance. Beyond the free allowance, checks are paid
(https://precleared.dev/buy).
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from franco.config import normalize_prospect_country

API_URL = "https://precleared.dev/api/v1/check"
BUY_URL = "https://precleared.dev/buy"
GET_KEY_URL = "https://precleared.dev/get-key"
# Precleared answers within 25 seconds (with verdict "unknown" if a register
# lookup is slow), so the read timeout sits just above that.
TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
CACHE_DAYS = 30
# Bump when the facts in _facts change meaning, so older cache entries are
# ignored rather than reused.
CACHE_VERSION = 1
SETTINGS_PREFIX = "precleared:"
VERDICTS = {"ok_to_email", "consent_needed", "blocked", "unknown"}
DEFINITE_VERDICTS = {"ok_to_email", "consent_needed", "blocked"}
USER_AGENT = "franco (+https://github.com/GortexAI/franco)"


@dataclass
class Clearance:
    """Precleared's answer for one address.

    ``allowed``: send it. ``held``: the check could not be made right now
    (network trouble, daily allowance used up, rate limit, rejected key,
    malformed answer), so the email stays queued and is tried again on a later
    cycle. Otherwise the email is stopped and ``reason`` says why.
    """

    allowed: bool
    held: bool = False
    verdict: str = ""
    reason: str = ""
    receipt_id: str = ""


class PreclearedClient:
    def __init__(
        self,
        sender_country: str,
        api_key: str = "",
        address_source: str = "unknown",
        pitch: str = "",
        prospect_country: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.sender_country = sender_country
        self.api_key = api_key or ""
        self.address_source = address_source
        self.pitch = (pitch or "").strip()[:200]
        self.prospect_country = normalize_prospect_country(prospect_country)
        self._transport = transport  # tests inject httpx.MockTransport

    @classmethod
    def from_config(cls, config, env) -> "PreclearedClient | None":
        """Build a client when the check is switched on, else return None."""
        compliance = getattr(config, "compliance", None)
        settings = getattr(compliance, "precleared", None)
        if settings is None or getattr(settings, "enabled", False) is not True:
            return None
        product = getattr(config, "product", None)
        return cls(
            sender_country=settings.sender_country,
            api_key=getattr(env, "precleared_api_key", ""),
            address_source=settings.address_source,
            pitch=getattr(product, "description", ""),
            prospect_country=getattr(settings, "prospect_country", ""),
        )

    def _redact(self, text: str) -> str:
        if self.api_key and text:
            return text.replace(self.api_key, "***REDACTED***")
        return text or ""

    def _facts(self) -> dict:
        """Everything sent with the address. A cached verdict is reused only
        while these are unchanged, so it always answers the same question."""
        facts = {
            "sender_country": self.sender_country,
            "address_source": self.address_source,
            "existing_customer": False,
        }
        if self.pitch:
            facts["pitch"] = self.pitch
        if self.prospect_country:
            # Precleared uses this only when the address, company records and
            # the company's website don't show the recipient's country.
            facts["default_recipient_country"] = self.prospect_country
        return facts

    def _fingerprint(self) -> str:
        facts = {"v": CACHE_VERSION, **self._facts()}
        return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()

    async def cached(self, to_email: str, state) -> Clearance | None:
        """The stored verdict for this address and these facts, or None."""
        raw = await state.get_setting(SETTINGS_PREFIX + _address(to_email))
        return _from_cache(raw, self._fingerprint())

    async def check(self, to_email: str, state) -> Clearance:
        """Ask Precleared now, and store a definite verdict for next time."""
        email = _address(to_email)
        try:
            result = await self._check(email)
        except Exception as e:
            # Whatever went wrong, an unfinished check holds cold email; it
            # never stops the cycle.
            return Clearance(allowed=False, held=True, reason=self._redact(
                f"Precleared check failed ({type(e).__name__}: {e})")[:240])
        if not result.held and result.verdict in DEFINITE_VERDICTS:
            # One entry per address: a fresh verdict replaces any earlier one.
            await state.set_setting(SETTINGS_PREFIX + email, json.dumps({
                "verdict": result.verdict,
                "reason": result.reason,
                "receipt_id": result.receipt_id,
                "checked_at": _utcnow().isoformat(),
                "inputs": self._fingerprint(),
            }))
        return result

    async def clear(self, to_email: str, state) -> Clearance:
        """The verdict for one address, from the cache when possible."""
        cached = await self.cached(to_email, state)
        return cached if cached is not None else await self.check(to_email, state)

    async def _check(self, email: str) -> Clearance:
        payload = {"recipient_email": email, **self._facts()}
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport) as client:
                resp = await client.post(API_URL, json=payload, headers=headers)
        except httpx.HTTPError as e:
            return Clearance(allowed=False, held=True,
                             reason=self._redact(f"Precleared unreachable: {e}"))

        if resp.status_code == 429:
            if self.api_key or _error_code(resp) == "rate_limited":
                # A key that runs out of checks gets a 402, so a 429 on a key
                # is Precleared's per-minute rate limit.
                return Clearance(allowed=False, held=True,
                                 reason="Precleared rate limit reached; trying again next cycle")
            # Keyless allowance used up: a free key gives this install its own.
            return Clearance(
                allowed=False, held=True,
                reason="Precleared daily allowance used up; add PRECLEARED_API_KEY "
                       f"(free key: {GET_KEY_URL}) or wait until tomorrow",
            )
        if resp.status_code == 402:
            # The key has no checks left today and no prepaid checks, so
            # another key won't help: buying more (or tomorrow) will.
            return Clearance(
                allowed=False, held=True,
                reason="Precleared: your key has no checks left today; "
                       f"add more at {BUY_URL} or wait until tomorrow",
            )
        if resp.status_code == 401:
            return Clearance(allowed=False, held=True,
                             reason="PRECLEARED_API_KEY was rejected (wrong or revoked)")
        if resp.status_code != 200:
            return Clearance(allowed=False, held=True,
                             reason=self._redact(f"Precleared HTTP {resp.status_code}: {resp.text[:200]}"))

        try:
            data = resp.json()
        except (ValueError, RecursionError):
            return Clearance(allowed=False, held=True, reason="Precleared returned invalid JSON")

        # Only a JSON object with a known verdict counts as an answer.
        # Anything else is treated like an unavailable check, so cold email
        # is held, not failed, and the rest of the cycle carries on.
        if not isinstance(data, dict):
            return Clearance(allowed=False, held=True,
                             reason="Precleared returned an unexpected response (not a JSON object)")
        verdict = data.get("verdict")
        if not isinstance(verdict, str) or verdict not in VERDICTS:
            return Clearance(allowed=False, held=True,
                             reason="Precleared returned a response without a known verdict")

        receipt = data.get("receipt")
        receipt_id = _text(receipt.get("id")) if isinstance(receipt, dict) else ""
        if verdict == "ok_to_email":
            return Clearance(allowed=True, verdict=verdict, receipt_id=receipt_id)
        return Clearance(
            allowed=False,
            verdict=verdict,
            reason=_reason(data),
            receipt_id=receipt_id,
        )


def _address(to_email: str) -> str:
    return (to_email or "").strip().lower()


def _text(value) -> str:
    """A stripped string, or "" for anything that isn't one."""
    return value.strip() if isinstance(value, str) else ""


def _error_code(resp: httpx.Response) -> str:
    """The ``error`` field of a JSON error body, or ""."""
    try:
        data = resp.json()
    except (ValueError, RecursionError):
        return ""
    return _text(data.get("error")) if isinstance(data, dict) else ""


def _reason(data: dict) -> str:
    """One readable line: the rule that decided it (or the note), then the next step.

    Precleared's ``reasons`` lead with the matching rule as
    ``{"rule_key", "text", "source_title", "source_url"}``, or with a
    ``{"note": ...}`` when no rule could be applied. Fields with an
    unexpected shape are skipped.
    """
    reasons = data.get("reasons")
    first = reasons[0] if isinstance(reasons, list) and reasons and isinstance(reasons[0], dict) else {}
    text = _text(first.get("text")) or _text(first.get("note"))
    source = _text(first.get("source_title"))
    parts = []
    if text:
        parts.append(f"{text} ({source})" if source else text)
    next_step = _text(data.get("next_step"))
    if next_step:
        parts.append(next_step)
    return " ".join(parts)[:240]


def _from_cache(raw: str, inputs: str) -> Clearance | None:
    """A cached verdict, or None when there is none to trust.

    Entries decided on different facts, expired or malformed entries all
    count as missing.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("inputs") != inputs:
        return None
    verdict = data.get("verdict")
    if not isinstance(verdict, str) or verdict not in DEFINITE_VERDICTS:
        return None
    try:
        checked_at = datetime.fromisoformat(str(data["checked_at"]))
    except (KeyError, ValueError, TypeError):
        return None
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    if _utcnow() - checked_at > timedelta(days=CACHE_DAYS):
        return None
    return Clearance(
        allowed=verdict == "ok_to_email",
        verdict=verdict,
        reason=_text(data.get("reason")),
        receipt_id=_text(data.get("receipt_id")),
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
