"""Approved-copy outreach: the deterministic pieces.

Rendering, the compliance footer, business-day scheduling, unsubscribe
headers, and stripping quoted text out of replies. No model, no I/O —
everything here is a pure function so the rules are easy to test and
impossible to "prompt around".
"""

import re
from datetime import date, datetime, time, timedelta

import pytz

# The observation recorded for an account during research.
TRIGGER_SIGNAL = "TRIGGER_OBSERVATION"

MERGE_FIELD_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render(template: str, values: dict[str, str]) -> str:
    """Fill {{field}} merge fields. Unknown or empty fields are left as-is,
    so the pre-send gate blocks the email instead of sending a gap."""

    def sub(m: re.Match) -> str:
        value = (values.get(m.group(1)) or "").strip()
        return value if value else m.group(0)

    return MERGE_FIELD_RE.sub(sub, template)


def clean_observation(text: str) -> str:
    """Make a researched observation read naturally after "I noticed ...".

    Trailing punctuation goes (the template supplies its own) and a leading
    capital is lowered unless the first word looks like a name or acronym.
    """
    text = " ".join((text or "").split()).rstrip(" .;:!")
    if not text:
        return ""
    first = text.split(" ", 1)[0]
    if first[:1].isupper() and first[1:].islower() and first.lower() in {
        "the", "your", "a", "an", "you", "you're", "there", "it", "its", "it's",
    }:
        text = text[0].lower() + text[1:]
    return text


def build_footer(compliance, persona) -> str:
    """The footer every cold email must carry. Empty when not required."""
    if not getattr(compliance, "require_footer", False):
        return ""
    return compliance.footer.format(
        sender_name=persona.name,
        company=persona.company,
        postal_address=(compliance.postal_address or "").strip(),
    ).strip()


def with_footer(body: str, footer: str) -> str:
    body = (body or "").rstrip()
    if not footer or footer in body:
        return body
    return f"{body}\n\n{footer}"


def unsubscribe_headers(compliance, persona) -> dict[str, str]:
    """List-Unsubscribe headers (RFC 2369 / RFC 8058).

    A mailto: to the sending address always works for a laptop-run sender;
    the HTTPS one-click form is added only when you host an endpoint.
    """
    if compliance is None or not getattr(compliance, "require_footer", False):
        return {}
    targets = []
    if getattr(persona, "email", ""):
        targets.append(f"<mailto:{persona.email}?subject=unsubscribe>")
    url = (getattr(compliance, "unsubscribe_url", "") or "").strip()
    if url.startswith("https://"):
        targets.insert(0, f"<{url}>")
    if not targets:
        return {}
    headers = {"List-Unsubscribe": ", ".join(targets)}
    if url.startswith("https://"):
        headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    return headers


# ── Business-day scheduling ──

def add_business_days(start: date, days: int) -> date:
    """``start`` plus ``days`` working days (Mon-Fri). 0 returns ``start``
    rolled forward to a weekday."""
    d = start
    while d.weekday() >= 5:
        d += timedelta(days=1)
    added = 0
    while added < days:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d


def first_send_day(now_local: datetime, send_hour: int) -> date:
    """Day 1: today if it's a weekday and the send hour hasn't passed,
    otherwise the next weekday."""
    d = now_local.date()
    if now_local.weekday() >= 5:
        return add_business_days(d, 0)    # the coming Monday
    if now_local.hour >= send_hour:
        return add_business_days(d, 1)
    return d


def send_at_utc(day: date, send_hour: int, tz_name: str) -> str:
    """``day`` at ``send_hour`` local time, as a naive-UTC ISO string (the
    format the outbox stores and compares against)."""
    tz = pytz.timezone(tz_name)
    local = tz.localize(datetime.combine(day, time(hour=send_hour)))
    return local.astimezone(pytz.utc).replace(tzinfo=None).isoformat()


def local_date(utc_naive: datetime, tz_name: str) -> date:
    return pytz.utc.localize(utc_naive).astimezone(pytz.timezone(tz_name)).date()


# ── Replies ──

_ATTRIBUTION_RE = re.compile(
    r"^\s*(On\s.{0,200}?wrote:|-{2,}\s*Original Message\s*-{2,}|"
    r"_{5,}|From:\s.+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def strip_quoted(text: str, footer: str = "") -> str:
    """Only what the person newly wrote: no quoted history, no signature
    of ours. Keyword rules (opt-out!) must never fire on our own footer
    quoted back at us."""
    text = (text or "").replace("\r\n", "\n")
    # Gmail wraps long attribution lines: join "On ... <\naddr> wrote:".
    text = re.sub(r"(On\s[^\n]{0,200})\n([^\n]{0,120}wrote:)", r"\1 \2", text)
    m = _ATTRIBUTION_RE.search(text)
    if m:
        text = text[: m.start()]
    lines = [ln for ln in text.split("\n") if not ln.lstrip().startswith(">")]
    text = "\n".join(lines)
    if footer:
        text = text.replace(footer, "")
    return text.strip()


def compliance_problem(compliance) -> str:
    """Why cold email can't go out at all right now, or "" if it can."""
    if compliance is None:
        return ""
    if not getattr(compliance, "require_footer", False):
        # Precleared's ok_to_email assumes the email carries a compliant
        # footer, and Franco's own footer is what provides it.
        if getattr(getattr(compliance, "precleared", None), "enabled", False) is True:
            return ("compliance.precleared is on but require_footer is off: "
                    "Precleared's ok_to_email assumes each cold email carries "
                    "your footer, so nothing will be sent")
        return ""
    if not (getattr(compliance, "postal_address", "") or "").strip():
        return ("compliance.postal_address is blank — every cold email must "
                "carry a valid postal address, so nothing will be sent")
    return ""


# ── CSV export ──

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value) -> str:
    """A cell a spreadsheet won't execute. Observations, email bodies and
    names come from imported files and scraped pages, so a value starting
    with = + - @ is prefixed with ' (OWASP CSV injection guidance)."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


def csv_safe_row(row: dict) -> dict:
    return {k: csv_safe(v) for k, v in row.items()}


# ── Preview ──

PREVIEW_CSV_COLUMNS = (
    "campaign", "owner", "company", "domain", "buyer", "title", "email",
    "observation", "source_url", "checked", "subject", "body", "status",
)


def preview_record(item: dict) -> dict:
    """One preview row: the fields a reviewer signs off on."""
    ctx = item.get("context") or {}
    return csv_safe_row({
        "campaign": item.get("campaign_key", ""), "owner": ctx.get("owner", ""),
        "company": ctx.get("company", ""), "domain": ctx.get("domain", ""),
        "buyer": ctx.get("name", ""), "title": ctx.get("title", ""),
        "email": item.get("to_email", ""), "observation": ctx.get("observation", ""),
        "source_url": ctx.get("source_url", ""), "checked": ctx.get("checked", ""),
        "subject": item.get("subject", ""), "body": item.get("body", ""),
        "status": item.get("status", ""),
    })
