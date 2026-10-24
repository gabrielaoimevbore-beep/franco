"""Configuration loader for Franco. Reads franco.yaml + .env."""

import logging
import os
import re
from datetime import time as _time
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError, field_validator, model_validator

from franco.paths import PROJECT_ROOT

logger = logging.getLogger("franco.config")


class ConfigError(Exception):
    """Raised when Franco's configuration is missing or invalid."""


class ConfigFileNotFoundError(ConfigError, FileNotFoundError):
    """Config file is missing. Subclasses FileNotFoundError for
    backward compatibility with existing callers/tests."""


class PersonaConfig(BaseModel):
    name: str
    company: str
    role: str
    email: str
    linkedin: str
    tone: str


class OfferConfig(BaseModel):
    primary: str = ""
    entry: str = ""
    goal: str = "book_call"  # book_call, start_trial, get_reply
    booking_method: str = "calendar_link"  # calendar_link, suggest_times, ask_preference
    booking_url: str = ""
    meeting_duration: str = "15 minutes"
    meeting_owner: str = ""


class ProductConfig(BaseModel):
    name: str
    description: str
    pricing: str
    key_benefits: list[str]
    objection_responses: dict[str, str]
    offer: OfferConfig = OfferConfig()


class ICPConfig(BaseModel):
    industries: list[str]
    company_size: str
    titles: list[str]
    geography: list[str]
    # Role keywords that indicate a company is in-market right now (a company
    # hiring a "Head of Growth" is buying growth tooling). Empty → falls back
    # to `titles`. Used for careers-page scanning and job-board discovery.
    hiring_signals: list[str] = []
    # Discovery needs a radius, not a place name. Maps each entry in
    # `geography` to "lat,lng,radius_km" — e.g.
    #   "Denver, CO": "39.7392,-104.9903,50"
    # Without one, listings providers can only match on the business name.
    geo_coordinates: dict[str, str] = {}


class EmailChannelConfig(BaseModel):
    enabled: bool = True
    # "instantly" (legacy), "gmail" (Gmail/Workspace via API — recommended),
    # or "smtp" (any SMTP+IMAP mailbox: AgentMail, Fastmail, ...)
    provider: str = "instantly"
    max_daily_sends: int = 50
    # When True, also send to catch-all ("risky") domains, not just verified
    # mailboxes. Off by default — catch-alls accept everything, so a bad
    # guess still bounces.
    send_to_risky: bool = False
    # Copilot mode (native providers): every outgoing email waits in the
    # outbox for your approval (dashboard → Outbox, or `franco outbox`).
    # Set false for full autopilot once you trust the output.
    require_approval: bool = True
    # Kill switch: pause all sending when bounces exceed this fraction of
    # sent mail (measured over the trailing sends). 0 disables the switch.
    max_bounce_rate: float = 0.05

    @field_validator("max_daily_sends")
    @classmethod
    def _sends_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_daily_sends must be >= 0")
        return v


class LinkedInChannelConfig(BaseModel):
    enabled: bool = True
    max_daily_connections: int = 20
    max_daily_messages: int = 10


class ChannelsConfig(BaseModel):
    email: EmailChannelConfig = EmailChannelConfig()
    linkedin: LinkedInChannelConfig = LinkedInChannelConfig()


class QuietHoursConfig(BaseModel):
    start: str = "22:00"
    end: str = "07:00"
    timezone: str = "America/New_York"

    @field_validator("start", "end")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        try:
            _time.fromisoformat(v)
        except ValueError:
            raise ValueError(
                f"'{v}' is not a valid time. Use 24h HH:MM format, e.g. '22:00'."
            )
        return v

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, v: str) -> str:
        import pytz

        if v not in pytz.all_timezones_set:
            raise ValueError(
                f"'{v}' is not a valid timezone. Use an IANA name like 'America/New_York'."
            )
        return v


class UsageConfig(BaseModel):
    max_daily_claude_percent: float = 80.0
    heartbeat_interval_minutes: int = 15
    quiet_hours: QuietHoursConfig = QuietHoursConfig()

    @field_validator("max_daily_claude_percent")
    @classmethod
    def _valid_percent(cls, v: float) -> float:
        if not 0 < v <= 100:
            raise ValueError("max_daily_claude_percent must be between 0 and 100")
        return v

    @field_validator("heartbeat_interval_minutes")
    @classmethod
    def _valid_interval(cls, v: int) -> int:
        if v < 1:
            raise ValueError("heartbeat_interval_minutes must be at least 1")
        return v


BRAIN_PROVIDERS = ("claude", "codex")
CODEX_REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")

# Model names are passed to a CLI, so they must look like a model name and
# never like a flag ("-x") or anything a shell would treat specially.
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}$")


class BrainConfig(BaseModel):
    """Which subscription Franco thinks with.

    ``claude`` (default) runs Claude Code headless on your Claude plan.
    ``codex`` runs Codex on your ChatGPT plan. There is never an automatic
    fallback between them, and an explicit ``model`` only affects Franco —
    your own Claude Code / Codex settings are never read or changed.
    """

    provider: str = "claude"
    model: str = ""  # empty = the provider's own default
    reasoning_effort: str = ""  # codex only; empty = model default

    @field_validator("provider", mode="before")
    @classmethod
    def _valid_provider(cls, v) -> str:
        v = str(v or "claude").strip().lower()
        if v not in BRAIN_PROVIDERS:
            raise ValueError(
                f"'{v}' is not a supported brain provider. "
                f"Use one of: {', '.join(BRAIN_PROVIDERS)}."
            )
        return v

    @field_validator("model", mode="before")
    @classmethod
    def _valid_model(cls, v) -> str:
        v = str(v or "").strip()
        if v and not _MODEL_NAME_RE.match(v):
            raise ValueError(f"'{v}' is not a valid model name.")
        return v

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _valid_effort(cls, v) -> str:
        v = str(v or "").strip().lower()
        if v and v not in CODEX_REASONING_EFFORTS:
            raise ValueError(
                f"'{v}' is not a valid reasoning effort. "
                f"Use one of: {', '.join(CODEX_REASONING_EFFORTS)}."
            )
        return v


class CampaignStepConfig(BaseModel):
    """One approved email. ``day`` is the business day it goes out on,
    counted from the day the first email is sent (day 1)."""

    day: int
    subject: str
    body: str

    @field_validator("day")
    @classmethod
    def _day_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("day must be 1 or later (day 1 is the first email)")
        return v


class CampaignConfig(BaseModel):
    """An approved sequence, sent word for word. Only merge fields vary:
    {{first_name}}, {{last_name}}, {{company}}, {{title}} and
    {{verified_observation}} (a sourced fact recorded for the account)."""

    name: str = ""
    steps: list[CampaignStepConfig]

    @field_validator("steps")
    @classmethod
    def _steps_ordered(cls, v: list[CampaignStepConfig]) -> list[CampaignStepConfig]:
        if not v:
            raise ValueError("a campaign needs at least one step")
        if v[0].day != 1:
            raise ValueError("the first step must be on day 1")
        days = [s.day for s in v]
        if days != sorted(days) or len(set(days)) != len(days):
            raise ValueError("step days must be strictly increasing")
        return v


class BenefitConfig(BaseModel):
    """One of the (at most three) customer benefits every message lives in."""
    key: str
    text: str


class PersonaAngleConfig(BaseModel):
    """How the pitch shifts for one kind of buyer. ``match`` is a regex
    tested (case-insensitively) against the contact's title."""
    match: str
    angle: str

    @field_validator("match")
    @classmethod
    def _valid_regex(cls, v: str) -> str:
        import re
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"persona match '{v}' is not a valid regex: {e}")
        return v


class FunnelConfig(BaseModel):
    """A narrow, observable segment with its own story (raad mode).

    ``match.require`` / ``match.exclude`` are conditions on what Franco
    knows about the account: an UPPERCASE signal code (``RUNNING_GOOGLE_ADS``)
    holds when the account carries that signal, ``CODE=text`` when its value
    contains ``text`` (``TECH_STACK=AdSense``; ``a|b`` means either), and a lowercase account field
    (``segment=marketplace``, ``industry=jobs``) when the field contains it.
    Funnels are tried in the order listed, so put the narrowest first.
    """
    name: str = ""
    benefit: str                     # a key from outreach.benefits
    match: dict[str, list[str]] = {}
    # The moment every company in this funnel reaches, in two or three
    # plain sentences ("Every marketplace reaches a size where ...").
    story: str
    # How we handle it, bounded to what is true today.
    solution: str
    landing_url: str = ""
    personas: list[PersonaAngleConfig] = []
    # Approved follow-ups (day 5, 9, ...). A day-1 step here is used only
    # when write_first_email is off.
    steps: list[CampaignStepConfig] = []
    write_first_email: bool = True

    @field_validator("match")
    @classmethod
    def _valid_match(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        unknown = set(v) - {"require", "exclude"}
        if unknown:
            raise ValueError(f"funnel match only takes require/exclude, not {sorted(unknown)}")
        return v

    @field_validator("steps")
    @classmethod
    def _steps_ordered(cls, v: list[CampaignStepConfig]) -> list[CampaignStepConfig]:
        days = [s.day for s in v]
        if days != sorted(days) or len(set(days)) != len(days):
            raise ValueError("funnel step days must be strictly increasing")
        return v

    @model_validator(mode="after")
    def _first_email_source(self):
        if not self.write_first_email and not (self.steps and self.steps[0].day == 1):
            raise ValueError("with write_first_email off, the funnel's steps must start on day 1")
        return self


class OutreachConfig(BaseModel):
    """How sequences are produced.

    ``generated`` (default): the writer drafts sequences with the brain.
    ``templates``: approved copy from ``campaigns`` is used verbatim for
    accounts imported with a matching campaign key; no model is involved.
    ``raad``: every account is placed in the narrowest matching funnel from
    what Franco observed about it; email 1 is written from that funnel's
    story, one customer benefit, the buyer's role and the account's own
    facts, and the funnel's approved follow-ups come after it.
    """

    mode: str = "generated"
    campaigns: dict[str, CampaignConfig] = {}
    benefits: list[BenefitConfig] = []
    funnels: dict[str, FunnelConfig] = {}
    first_email_max_words: int = 110
    # Business days are counted, and emails scheduled, in this timezone.
    timezone: str = "America/Los_Angeles"
    send_hour: int = 9
    # An observation older than this can't fill {{verified_observation}}.
    observation_max_age_days: int = 90
    # Until a campaign is signed off (`franco preview signoff KEY`), stage
    # only this many accounts per owner, always for review, and send none
    # of them. 0 turns the preview off.
    preview_accounts: int = 10
    # Approved wording per reply intent. The handler adapts it rather than
    # writing from scratch, e.g. {"later": "Understood. Is there ..."}.
    reply_templates: dict[str, str] = {}

    @field_validator("mode")
    @classmethod
    def _valid_mode(cls, v: str) -> str:
        v = (v or "generated").strip().lower()
        if v not in ("generated", "templates", "raad"):
            raise ValueError("outreach.mode must be 'generated', 'templates' or 'raad'")
        return v

    @field_validator("benefits")
    @classmethod
    def _three_benefits(cls, v: list[BenefitConfig]) -> list[BenefitConfig]:
        if len(v) > 3:
            raise ValueError("at most three customer benefits: every message lives in one")
        keys = [b.key for b in v]
        if len(set(keys)) != len(keys):
            raise ValueError("benefit keys must be unique")
        return v

    @model_validator(mode="after")
    def _funnels_valid(self):
        keys = {b.key for b in self.benefits}
        for name, funnel in self.funnels.items():
            if funnel.benefit not in keys:
                raise ValueError(
                    f"funnel '{name}' names benefit '{funnel.benefit}', which is not in "
                    f"outreach.benefits ({', '.join(sorted(keys)) or 'none'})")
            if name in self.campaigns:
                raise ValueError(f"'{name}' is both a campaign and a funnel; use one key once")
        if self.mode == "raad" and not self.funnels:
            raise ValueError("outreach.mode 'raad' needs at least one funnel")
        return self

    def sequence_keys(self) -> list[str]:
        """Every approved-copy campaign and funnel: what sign-off covers."""
        return list(self.campaigns) + list(self.funnels)

    @field_validator("send_hour")
    @classmethod
    def _valid_hour(cls, v: int) -> int:
        if not 0 <= v <= 23:
            raise ValueError("send_hour must be between 0 and 23")
        return v

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        import pytz

        if v not in pytz.all_timezones_set:
            raise ValueError(f"'{v}' is not a valid timezone.")
        return v


# Precleared's terms for where a prospect's address came from.
PRECLEARED_ADDRESS_SOURCES = ("published", "bought", "scraped", "given", "unknown")
# Where the sending business may be based, in Precleared's terms.
PRECLEARED_SENDER_COUNTRIES = ("GB", "US", "AE")


def normalize_prospect_country(v) -> str:
    """A two-letter country code ("us" -> "US", "UK" -> "GB"), or "" for none.

    Raises ValueError on anything else, including the booleans YAML makes of
    an unquoted NO (Norway) or ON, so a typo is never silently ignored.
    """
    if isinstance(v, bool):
        raise ValueError(
            f"prospect_country was read as {v}. YAML turns an unquoted NO or "
            'ON into true/false, so quote the code, e.g. prospect_country: "NO".'
        )
    v = str(v or "").strip().upper()
    v = {"UK": "GB", "USA": "US"}.get(v, v)
    if v and not (len(v) == 2 and v.isascii() and v.isalpha()):
        raise ValueError(
            f"'{v}' is not a valid prospect_country. Use a two-letter "
            "country code such as US or GB, or leave it empty."
        )
    return v


class PreclearedConfig(BaseModel):
    """Optional per-recipient cold-email check through Precleared.

    Off by default. When on, each recipient is checked before their first
    cold email against the cold-email rules of their country (UK PECR, US
    CAN-SPAM, Canada's CASL, Germany's UWG and others), and later steps reuse
    the verdict. Any verdict other than ok_to_email stops that email, with
    the reason. Needs compliance.require_footer. See
    franco/integrations/precleared.py.
    """

    enabled: bool = False
    # Where your business is based: GB, US or AE. Required when enabled.
    sender_country: str = ""
    # Where prospect addresses come from, in Precleared's terms. Franco
    # builds most addresses from a learned pattern, so the honest default is
    # "unknown"; use "published" only if yours are copied from the
    # businesses' own websites.
    address_source: str = "unknown"
    # Optional: the country your prospects are in (two letters, e.g. "US"),
    # if they are all in one. Precleared uses it only when it can't work out
    # the recipient's country from the address, company records or the
    # company's website. Leave empty if your prospects are spread out.
    prospect_country: str = ""

    @field_validator("sender_country", mode="before")
    @classmethod
    def _upper_country(cls, v) -> str:
        v = str(v or "").strip().upper()
        return "GB" if v == "UK" else v

    @field_validator("address_source", mode="before")
    @classmethod
    def _valid_source(cls, v) -> str:
        v = str(v or "unknown").strip().lower()
        if v not in PRECLEARED_ADDRESS_SOURCES:
            raise ValueError(
                f"'{v}' is not a valid address_source. "
                f"Use one of: {', '.join(PRECLEARED_ADDRESS_SOURCES)}."
            )
        return v

    @field_validator("prospect_country", mode="before")
    @classmethod
    def _valid_prospect_country(cls, v) -> str:
        return normalize_prospect_country(v)

    @model_validator(mode="after")
    def _country_when_enabled(self):
        if self.enabled and self.sender_country not in PRECLEARED_SENDER_COUNTRIES:
            raise ValueError(
                "compliance.precleared.sender_country must be GB, US or AE "
                "when the check is enabled."
            )
        return self


class ComplianceConfig(BaseModel):
    """Commercial-email obligations, enforced at send time by the gate."""

    # Physical postal address for the footer. Never guessed: when the footer
    # is required and this is blank, every cold email is blocked.
    postal_address: str = ""
    require_footer: bool = False
    footer: str = (
        "{sender_name} | {company} | {postal_address}. "
        "Business outreach about {company}. Reply \"no thanks\" to stop."
    )
    # Optional HTTPS one-click unsubscribe endpoint you host. A mailto:
    # unsubscribe to the sending address is always offered.
    unsubscribe_url: str = ""
    # Phrases no outgoing email (cold or reply) may contain.
    blocked_claims: list[str] = []
    # Acknowledge an opt-out — only after the suppression is recorded.
    send_opt_out_ack: bool = False
    opt_out_ack: str = "Understood. I have recorded the request to stop."
    # Optional per-recipient check before the first cold email. Off by default.
    precleared: PreclearedConfig = PreclearedConfig()


class FrancoConfig(BaseModel):
    persona: PersonaConfig
    product: ProductConfig
    icp: ICPConfig
    channels: ChannelsConfig = ChannelsConfig()
    usage: UsageConfig = UsageConfig()
    brain: BrainConfig = BrainConfig()
    outreach: OutreachConfig = OutreachConfig()
    compliance: ComplianceConfig = ComplianceConfig()


class EnvConfig(BaseModel):
    instantly_api_key: str = ""
    # Discovery providers
    dataforseo_login: str = ""
    dataforseo_password: str = ""
    dataforseo_sandbox: str = ""   # any truthy value routes to the free sandbox
    linkedin_email: str = ""
    linkedin_password: str = ""
    hunter_api_key: str = ""
    serper_api_key: str = ""
    reoon_api_key: str = ""
    zerobounce_api_key: str = ""
    # Native mail providers (channels.email.provider: gmail | smtp)
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    # Optional: only used when compliance.precleared.enabled is true.
    precleared_api_key: str = ""


def _format_validation_error(e: ValidationError) -> str:
    """Turn a pydantic ValidationError into a readable, actionable message."""
    lines = []
    for err in e.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def load_config(config_path: str | None = None) -> FrancoConfig:
    """Load Franco configuration from YAML file.

    Raises ConfigError with a clear, actionable message on any problem.
    """
    if config_path is None:
        config_path = _find_config_file()

    try:
        with open(config_path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise ConfigFileNotFoundError(
            f"Config file not found: {config_path}. "
            "Create one from franco.yaml.example or run 'franco setup'."
        )
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {config_path}:\n  {e}")
    except OSError as e:
        raise ConfigError(f"Could not read {config_path}: {e}")

    if data is None:
        raise ConfigError(f"{config_path} is empty. Run 'franco setup' to configure Franco.")
    if not isinstance(data, dict):
        raise ConfigError(
            f"{config_path} must contain a YAML mapping (key: value pairs), "
            f"got {type(data).__name__}."
        )

    try:
        return FrancoConfig(**data)
    except ValidationError as e:
        # Log a friendly, actionable summary, then re-raise the original
        # ValidationError so callers (and tests) keep the pydantic type.
        logger.error(
            f"Invalid configuration in {config_path}:\n{_format_validation_error(e)}\n"
            "Fix the fields above or re-run 'franco setup'."
        )
        raise


def load_brain_config(config_path: str | None = None) -> BrainConfig:
    """Read just the ``brain:`` section, without validating the rest.

    Training runs before a product is configured (the template franco.yaml
    still has placeholder values), but it must still think with the
    provider the user chose. A missing file or section means the Claude
    default; an invalid section raises rather than silently switching
    providers.
    """
    if config_path is None:
        try:
            config_path = _find_config_file()
        except ConfigFileNotFoundError:
            return BrainConfig()
    try:
        with open(config_path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        return BrainConfig()
    except (yaml.YAMLError, OSError) as e:
        raise ConfigError(f"Could not read the brain settings in {config_path}: {e}")
    section = data.get("brain") if isinstance(data, dict) else None
    if section is None:
        return BrainConfig()
    if not isinstance(section, dict):
        raise ConfigError(f"'brain' in {config_path} must be a mapping.")
    try:
        return BrainConfig(**section)
    except ValidationError as e:
        raise ConfigError(
            f"Invalid brain settings in {config_path}:\n{_format_validation_error(e)}"
        )


def load_env() -> EnvConfig:
    """Load environment variables from .env file."""
    load_dotenv()
    env = EnvConfig(
        instantly_api_key=os.getenv("INSTANTLY_API_KEY", "").strip(),
        dataforseo_login=os.getenv("DATAFORSEO_LOGIN", "").strip(),
        dataforseo_password=os.getenv("DATAFORSEO_PASSWORD", "").strip(),
        dataforseo_sandbox=os.getenv("DATAFORSEO_SANDBOX", "").strip(),
        linkedin_email=os.getenv("LINKEDIN_EMAIL", "").strip(),
        linkedin_password=os.getenv("LINKEDIN_PASSWORD", "").strip(),
        hunter_api_key=os.getenv("HUNTER_API_KEY", "").strip(),
        serper_api_key=os.getenv("SERPER_API_KEY", "").strip(),
        reoon_api_key=os.getenv("REOON_API_KEY", "").strip(),
        zerobounce_api_key=os.getenv("ZEROBOUNCE_API_KEY", "").strip(),
        gmail_client_id=os.getenv("GMAIL_CLIENT_ID", "").strip(),
        gmail_client_secret=os.getenv("GMAIL_CLIENT_SECRET", "").strip(),
        smtp_host=os.getenv("SMTP_HOST", "").strip(),
        smtp_port=int(os.getenv("SMTP_PORT", "587").strip() or 587),
        smtp_username=os.getenv("SMTP_USERNAME", "").strip(),
        smtp_password=os.getenv("SMTP_PASSWORD", "").strip(),
        imap_host=os.getenv("IMAP_HOST", "").strip(),
        imap_port=int(os.getenv("IMAP_PORT", "993").strip() or 993),
        imap_username=os.getenv("IMAP_USERNAME", "").strip(),
        imap_password=os.getenv("IMAP_PASSWORD", "").strip(),
        precleared_api_key=os.getenv("PRECLEARED_API_KEY", "").strip(),
    )
    return env


def _find_config_file() -> str:
    """Search for Franco's config.

    ``franco.local.yaml`` wins when present. It is gitignored, so a fork can
    carry a real product configuration (trained on an actual company) while
    the tracked ``franco.yaml`` stays an untrained template — nobody
    publishes their positioning, pricing, and prospect targeting by accident.
    """
    candidates = [
        Path.cwd() / "franco.local.yaml",
        PROJECT_ROOT / "franco.local.yaml",
        Path.cwd() / "franco.yaml",
        Path.cwd().parent / "franco.yaml",
        PROJECT_ROOT / "franco.yaml",
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    raise ConfigFileNotFoundError(
        "franco.yaml not found in "
        + ", ".join(str(p.parent) for p in candidates)
        + ". Run 'franco init' in an empty folder to create one, or point "
          "FRANCO_HOME at the folder that has it."
    )
