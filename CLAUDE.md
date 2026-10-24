# Franco — Autonomous AI Sales Agent

Franco is an autonomous sales agent powered by Claude Code. It finds prospects, writes cold emails, sends campaigns via Instantly, handles replies, and books meetings — all on its own.

**You (Claude) are the guide.** When someone opens this project, your job is to help them understand what Franco is, get it configured, and start closing deals. Be conversational, not robotic. Explain things simply. Ask one thing at a time.

---

## When Someone First Opens This Project

Start by checking what state things are in. Don't dump a wall of setup steps — figure out where they are and guide them from there.

### Quick health check (do this silently):
1. Does `.venv/` exist? → If not, they need install
2. Is `franco` importable? → If not, dependencies need installing
3. Does `.env` exist with real values? → If not, they need API keys
4. Does `franco.yaml` have real values (not "Your Company")? → If not, they need product training
5. Does `data/franco.db` exist? → If not, Franco hasn't run yet

### Then introduce yourself based on what you find:

**If nothing is set up:**
> "This is Franco — an autonomous AI sales agent. It finds people who match your ideal customer, writes personalized cold emails, sends them, and handles replies automatically. It runs on your Claude Max subscription so there's no extra cost.
>
> Let me help you get it set up. It takes about 5 minutes. First, let me install the dependencies..."

**If partially set up:**
> "Looks like Franco is partially configured. [specific thing] is done but [specific thing] still needs setting up. Want me to pick up where you left off?"

**If fully set up:**
> "Franco is configured and ready to go. Want me to start it, show you the dashboard, or explain how it works?"

---

## Explaining Franco to Users

People will ask "how does this work?" — explain it simply:

- **"What does Franco do?"** → It's like having a tireless sales assistant. Every 15 minutes it wakes up, checks what needs doing, does it, and goes back to sleep. It finds prospects, writes emails, sends campaigns, and responds to replies.

- **"How does it find people?"** → It searches the web (DuckDuckGo, Bing, Google) for companies matching your target profile, visits their websites, and finds team members. For emails, it learns each company's address pattern (from their site, or a free Hunter domain lookup) and verifies a single candidate through a free verification tier — every address is tagged verified / catch-all / guess, and only deliverable ones get sent. No expensive tools needed.

- **"How does it write emails?"** → Three modes (`outreach.mode`):
  - `generated` (default): proven frameworks (AIDA, PAS) with strict rules: short, personal, no AI-sounding language, tailored to each person.
  - `templates` (approved copy): the user's signed-off sequences are sent word for word to accounts they researched; no model writes them.
  - `raad`: Franco puts each account in the narrowest funnel it fits from what it observed (e.g. "marketplace already running AdSense"). It writes email 1 from that funnel's story, one of at most three customer benefits, the buyer's role angle and a sourced fact, then sends the funnel's approved follow-ups. Named after Raad Mobrem (CEO of Intro), whose outbound approach it follows. See "Raad mode" in the README.

- **"Can I give it my own list?"** → Yes, as CSV files, from the terminal or the dashboard (Contacts → Import CSV). There are three kinds, each with a template:
  - `accounts`: researched accounts with a campaign and a sourced observation;
  - `contacts`: companies, optionally with a person each;
  - `suppressions`: do-not-contact addresses or domains.

  `franco import --template --kind contacts > contacts.csv`, then `franco import contacts.csv --kind contacts --dry-run`, then run it without `--dry-run`. Every row is validated first, and nothing is written on a dry run. Suppression lists also work with `franco suppress import FILE`.

- **"Why isn't anything sending?"** → Check these in order:
  1. **Preview.** New approved-copy campaigns and raad funnels start in preview: the first `preview_accounts` (default 10) accounts per owner are staged for review, and nothing sends until someone runs `franco preview signoff KEY --by NAME` (or uses the Outbox sign-off button).
  2. **Postal address.** `compliance.postal_address` may be blank; no cold email sends without it.
  3. **Kill switch.** Check `franco sending`.

- **"Is it safe?"** → Yes. It runs locally on your machine, has daily usage limits, quiet hours, and send limits built in. It can't delete files, access your bank account, or do anything outside its sales workflow. Everything it does is logged in a local SQLite database you can inspect anytime.

- **"What does it cost?"** → Just your Claude Max subscription (which you already have). The only paid integration is Instantly for sending emails (their cheapest plan works). Everything else — prospecting, email writing, reply handling — is included.

- **"Can it use ChatGPT / Codex instead of Claude?"** → Yes, opt-in. Install Codex CLI 0.159.3 (`npm install -g @openai/codex@0.159.3`; other versions are refused), run `codex login` with ChatGPT, and set `brain: { provider: codex, model: gpt-5.5 }` in franco.yaml (or choose codex in `franco setup`, which suggests gpt-5.5, or `franco train <url> --provider codex --model gpt-5.5`). gpt-5.5 is the verified text-only model; a blank model fails on current accounts because the live catalog hides it, and code-mode models are refused. Claude stays the default; Franco never switches providers on its own. Each Codex call runs isolated with no tools — see "Using a ChatGPT plan instead (Codex)" in the README. macOS and Linux only; on Windows, Codex is refused, so use Claude. Don't edit the user's own `~/.codex` config to make it work.

- **"What's the dashboard?"** → Run `franco dashboard` to see a local web UI at localhost:5555. It opens on **Today**: anything waiting on a decision, then the pipeline. The other tabs:
  - Signals, Companies, Activity, Usage, Settings;
  - **Contacts**, with Import CSV;
  - **Campaigns**, with the funnel table in raad mode;
  - **Outbox**: each draft with why it exists, edit with `E`, preview sign-off cards, the autopilot toggle and the kill switch;
  - Conversations.

  It only accepts requests from its own page on localhost.

- **"Where is my data?"** → All local, in `data/` (gitignored, never commit it):
  - `franco.db` holds every company, person, email, reply and decision (personal data).
  - `gmail_token.json` is the Gmail refresh token; treat it as a password.
  - `franco.log`, `franco.pid` and `analytics.json` are the rest.

  To back up while running: `sqlite3 data/franco.db ".backup data/backup.db"`. Before starting fresh, export suppressions (`franco suppress export`) and re-import them, so opt-outs survive. The README section "Your data folder" covers inspection, moving machines, and deletion requests.

- **"What are signals?"** → Signals are the facts Franco collects about a business: who its current agency is, whether it's running ads, whether it has online booking, how it ranks. **Franco proposes; you confirm.** Nothing is collected until the user says yes on the Signals tab (or `franco signals --confirm ...`). That keeps spend intentional and makes every prospect list explainable — a cohort is a query over signals a human chose, not a black box.

---

## Setup Flow

Walk through these steps conversationally. Ask one thing at a time. Don't overwhelm.

### Step 1: Install Dependencies

If they just want to *use* Franco, install it from PyPI and create a home folder:

```bash
pipx install franco          # or: pip install franco, in a virtualenv
franco init ~/franco && cd ~/franco
```

The home folder holds franco.yaml, .env, editable prompts/ and skills/, and data/. Franco finds it as `$FRANCO_HOME`, else the source checkout, else the current folder when it has a franco.yaml, else `~/.franco`. If they cloned the repo (to change Franco itself), the clone is the home:

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
```

Then install the browser for LinkedIn prospecting:
```bash
python -m playwright install chromium
```

### Step 2: API Keys (`.env`)

Franco needs an **email provider** (to send + read replies) and, strongly recommended, an **email verifier**. Pick one provider:

```
# --- Email provider: choose ONE, set channels.email.provider to match ---
GMAIL_CLIENT_ID=            # Recommended: Gmail/Workspace. Then run 'franco gmail auth'
GMAIL_CLIENT_SECRET=
# --- or SMTP+IMAP (AgentMail, Fastmail, Workspace app password) ---
SMTP_HOST=  SMTP_PORT=587  SMTP_USERNAME=  SMTP_PASSWORD=
IMAP_HOST=  IMAP_PORT=993
# --- or legacy Instantly ---
INSTANTLY_API_KEY=

# --- Email verification (add at least one; else emails stay 'guess') ---
REOON_API_KEY=              # 600 free/mo — best free tier
ZEROBOUNCE_API_KEY=         # 100 free/mo — best for M365/Workspace catch-alls
HUNTER_API_KEY=             # 50 free/mo + pattern lookup

# --- Discovery (optional — the free OpenStreetMap source needs no key) ---
DATAFORSEO_LOGIN=  DATAFORSEO_PASSWORD=   # cheapest local-business + SERP data
DATAFORSEO_SANDBOX=                       # any value routes to the free sandbox

# --- Optional ---
LINKEDIN_EMAIL=  LINKEDIN_PASSWORD=   # LinkedIn prospecting
CLOUDFLARE_ACCOUNT_ID=  CLOUDFLARE_API_TOKEN=   # deep JS crawling for training
SERPER_API_KEY=            # web search + discovery (2,500 free, then a $50 prepaid pack)
PRECLEARED_API_KEY=        # only with compliance.precleared on; free key at precleared.dev/get-key
```

**Recommended provider — Gmail:** for <50 cold emails/day, a real Google Workspace mailbox on a *dedicated secondary domain* (never the main one) is the most deliverable, cheapest (~$7/mo) option. Set `channels.email.provider: gmail` in franco.yaml, put the OAuth client id/secret in `.env`, then run `franco gmail auth` (one-time browser login). SMTP works with any mailbox (AgentMail, Fastmail). Instantly still works as a legacy option.

**Email verification matters:** Franco learns each company's email *pattern* and verifies ONE candidate rather than guessing (raw SMTP probing is not viable from a locally-running agent — outbound port 25 is usually blocked, and Google Workspace / Microsoft 365 accept everything from an unknown IP). Without a verifier key, found emails are marked `guess` and are **never sent**.

**Approval by default:** with a native provider, every outgoing email waits in the **Outbox** for your approval (dashboard Outbox tab, or `franco outbox`). Once you trust the output, run `franco autopilot on` (or the Outbox tab's **Turn on autopilot** button) for full autopilot — no restart, and it approves drafts already waiting. `franco autopilot off` puts review back; `franco autopilot default` follows `channels.email.require_approval` in franco.yaml again. Autopilot only skips the human: the send gate, footer/claims checks, suppression, daily cap and kill switch still apply.

After getting the Instantly API key, test it:
```bash
source .venv/bin/activate && python3 -c "
import asyncio, httpx
async def test():
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get('https://api.instantly.ai/api/v2/accounts', headers={'Authorization': 'Bearer API_KEY_HERE'})
        print('Connected!' if r.status_code == 200 else f'Failed: {r.status_code}')
asyncio.run(test())
"
```

### Step 3: Product Training

This is the most important step. Franco needs to know what it's selling.

**Option A — Train from a website URL (recommended):**
```bash
source .venv/bin/activate && python -m franco.trainer https://their-website.com
```
This generates: `franco.yaml`, `skills/product_knowledge.md`, `skills/competitive_intel.md`

**Option B — Manual configuration:**
Ask the user these questions and build `franco.yaml` and `skills/product_knowledge.md`:
- What's your company name?
- What do you sell? (product/service name and one-line description)
- What does it cost?
- What are the top 3-5 benefits?
- Who's your target? (industries, job titles, company size, geography)
- What name and email should Franco use? (the "from" identity)
- What objections do you usually hear? How do you respond?
- **Offers & closing:**
  - What's the primary offer? (subscription, service, etc.)
  - Is there a low-commitment entry? (free trial, free audit, etc.)
  - What's the goal? (book a call, start a trial, get a reply)
  - How should meetings be booked? (calendar link, suggest times, ask preference)
  - How long is the call? (default: 15 minutes)
  - Who takes the meeting?

### Step 4: Confirm Signals

Franco ships a catalog of ~19 signals it knows how to collect, all **proposed** and
none active. Nothing is prospected against until the user confirms them.

Send them to `franco dashboard` → **Signals**, where each one shows a
plain-language description, what it costs, and how many companies already carry
it. Or from the terminal:

```bash
franco signals                      # review the catalog
franco signals --confirm free       # turn on everything that costs nothing
franco signals --confirm SERP_RANK  # the one paid discovery signal
```

Most signals are free (they read pages a business already publishes). Only
`SERP_RANK` and `EMAIL_STATUS` consume credits — the cost note on each row says so.

Once signals are confirmed, the **cohort builder** at the bottom of the Signals
tab turns them into a target list: pick what a good prospect must have (and what
disqualifies them) and it counts the matches live.

### Step 5: Find Businesses

Discovery is the only stage that spends money, so it always estimates first.

```bash
franco discover --providers                        # the menu, with real prices
franco discover --estimate                         # projected spend, then exits
franco discover                                    # free OpenStreetMap source
franco discover --provider dataforseo_listings --max-spend 2.00
```

The dashboard's **Discover** tab is the same thing with the tradeoffs laid out
side by side: what each source does, what it costs, its free tier, and which
`.env` keys it needs. Nothing is called until the user presses Run.

| Source | Cost | Free tier | Best for |
|---|---|---|---|
| **OpenStreetMap** (default) | free | unlimited | Trying the whole pipeline with no account. Thin coverage for service-area trades — OSM maps premises, so it has under 2,000 roofers in the entire US. A trial source, not a complete list. |
| **DataForSEO Business Listings** | $0.372/1k businesses | $1 credit | Local trades, clinics, contractors. Phone + domain + rating + whether the listing is claimed, and it can filter for businesses with **no website at all**. |
| **DataForSEO SERP** | $0.0018 a search at depth 30 | $1 credit + free sandbox | Rank as the buying signal. Positions 11-30 are the sweet spot. |
| **Serper** | ~$0.30-$1.00/1k | 2,500 free, no card | The easiest paid one to try. |

Cities are geocoded automatically (cached forever, one lookup per city), or set
`icp.geo_coordinates` in franco.yaml to control the radius:
`"Denver, CO": "39.7392,-104.9903,50"`.

**Depth 20-30, not 100.** Google removed 100-results-per-page in September 2025,
so depth 100 is now billed as ten pages at nearly every provider. Below rank 30
it is mostly directories anyway.

### Step 6: Behavior Settings

These go in `franco.yaml` under `usage:`. Use sensible defaults unless they want to customize:
- `max_daily_claude_percent`: 80 (how much of daily Claude quota to use)
- `heartbeat_interval_minutes`: 15 (how often Franco checks for work)
- `quiet_hours`: 22:00-07:00 in their timezone
- `max_daily_sends`: 50 (email send limit)

**Compliance: ask for this, never guess it.** Ask for the user's physical business postal address and put it in `compliance.postal_address`. The template ships with `require_footer: true`, so no cold email sends until it is set. Their real settings belong in `franco.local.yaml` (gitignored; it replaces `franco.yaml` when present). Never put another person's or company's address in a user's footer. Also ask whether there are claims their emails must never make; those go in `compliance.blocked_claims`. `compliance.precleared` is an optional per-recipient check before the first cold email (see README); it is off by default and needs `require_footer: true`. If they turn it on, ask where their business is based (`sender_country`: GB, US or AE) and where their addresses come from (`address_source`); never guess either.

**Outreach mode:** ask how they want emails produced: generated (default), approved copy (`templates`), or `raad` (funnels). For approved copy or raad, help them write `outreach.campaigns` or `outreach.funnels` (see README), and explain the preview and sign-off before their first batch.

### Step 7: Start Franco

```bash
source .venv/bin/activate && franco run
```

Or open the dashboard:
```bash
franco dashboard
```

---

## After Setup — Ongoing Help

Users will come back with questions and tasks. Common ones:

- **"Show me what Franco has done"** → Run `franco status` or `franco dashboard`
- **"The emails aren't good"** → Edit `skills/email_frameworks.md` and `prompts/writer.md`. Show them the current rules and help them adjust.
- **"Franco isn't finding the right people"** → Check the ICP config in `franco.yaml`. Adjust industries, titles, company_size, geography.
- **"I want to change what Franco says"** → Skills are in `skills/`, prompts are in `prompts/`. Both are plain markdown files. Edit them directly.
- **"Train Franco on a different product"** → `franco train <new-url>`
- **"How do I see the database?"** → It's at `data/franco.db`. They can open it with any SQLite tool, or ask you to query it.
- **"I just want the prospect list"** → `franco export` writes a sequencer-ready CSV (verified/risky emails only; `--all` for everything). Also available as Export buttons on the dashboard's Contacts tab. Franco is valuable purely as a list-builder even if the user never lets it send.
- **"I have a list already"** → `franco import FILE.csv --kind accounts|contacts|suppressions`, always `--dry-run` first. Or use the dashboard: Contacts → Import CSV.
- **"Which funnel is this company in?"** (raad mode) → `franco funnels --company DOMAIN`. `franco funnels` shows how all companies split.
- **"Show me the preview / sign it off"** → `franco preview KEY` (or `--csv preview.csv` to share), then `franco preview signoff KEY --by NAME`.

---

## How Franco Works (Technical Reference)

### Architecture
- **Heartbeat loop** (`main.py`): Every cycle → check quiet hours → check budget → decide → act → log → sleep
- **Brain** (`brain.py`): Wraps `claude -p --output-format json --dangerously-skip-permissions` for headless Claude calls; records exact per-call token usage into `usage_events`
- **State** (`state.py`): SQLite at `data/franco.db` with tables for companies, prospects, campaigns, conversations, actions, usage
- **Usage tracking** (`usage.py`, `integrations/quota.py`): per-call attribution (agent/task/model/tokens) for Franco's OWN Claude calls only — it never scans your other Claude Code sessions — plus a live subscription-quota gauge read the same way Claude Code's `/usage` does. The budget check throttles Franco against real quota utilization so it always leaves headroom for your own interactive Claude use (`usage.max_daily_claude_percent`). Dashboard has a Usage tab; CLI has `franco usage`.
- **Skills** (`skills/`): Markdown knowledge files injected into agent prompts
- **Signals** (`signals.py`, `state.py`): every fact Franco learns is an OBSERVATION — a row in `observations`, never a column. A new signal needs no migration, re-observing over time is a free time series, and confidence + provenance travel with the fact. The vocabulary in `signal_codes` is governed (a trigger rejects any code not in it) and gated: only `status = 'confirmed'` signals are collected, and only a human sets that. `state.cohort(require, exclude)` does the set intersection in SQL.
- **Dashboard** (`dashboard.py` + `franco/web/`): FastAPI JSON API plus plain HTML/CSS/JS served from disk — no build step. Edit `franco/web/app.css` or `app.js` and reload the page.
- **DISCOVER** (`collectors/discover.py`): the only stage that spends money, so cost discipline lives in the collector, not the database — every run estimates first, checks a spend cap between batches, reads a kill switch, and flushes observations per batch. Providers are adapters behind one interface (`DiscoveryProvider`); adding one is a small class. Junk filtering matches *patterns*, not a literal blocklist. Entity resolution is the normalised domain, falling back to the provider's `external_id` for businesses with no website.

### Sub-Agents
- **Scout**: Python does all web searching (DuckDuckGo → Bing → Google → Serper API) and email resolution (pattern-first: cache → scraped mailto → Hunter domain search → default, then verify one candidate via Reoon/ZeroBounce/Hunter/SMTP; catch-alls flagged `risky`). Claude only scores/personalizes found data. Scout also collects **buying signals**: tech stack detected on each company's site (HubSpot, Shopify, Intercom, ~35 tools — zero extra requests) and hiring signals from careers pages. With `pip install python-jobspy` (optional), a job-board strategy discovers companies actively hiring for roles in `icp.hiring_signals` (falls back to `icp.titles`) — the strongest in-market signal. Signals land in prospects' personalization notes and boost their score.
- **Writer**: Depends on `outreach.mode`:
  - `generated`: 3-email sequences (Email 1 < 75 words, Email 2 < 75, Email 3 < 40), with a strict ban list on AI patterns.
  - `templates`: renders approved copy verbatim for imported accounts (no model), on a business-day schedule.
  - `raad` (`funnels.py`): places accounts in funnels, writes email 1 from the funnel brief, and queues the funnel's approved follow-ups.

  In approved copy and raad, the first `preview_accounts` per owner are a preview held until sign-off, and every cold email gets the compliance footer.
- **Sender**: Native providers (gmail/smtp) render merge vars per prospect and STAGE each email into the `outbox` (pending_review → approved → sent) with a scheduled send time; each heartbeat drains due, approved items with human-like pacing, the daily cap, the deterministic pre-send gate, and stop-on-reply. Legacy Instantly path deploys campaigns via API. Enforces daily send limits either way.
- **Handler**: Polls the provider (or Instantly) for replies, dedups them, classifies intent, advances conversation stage, and queues auto-responses through the same outbox approval ladder. On native providers it also detects bounces → marks the address invalid, cancels the prospect's queued sends, and trips a global kill switch past a bounce-rate threshold.
- **Analyst**: Runs on idle cycles. Generates `data/analytics.json` with pipeline stats and insights.

### Conversation Stages
`initial_outreach → engaged → qualifying → presenting → negotiating → closing → closed_won / closed_lost`

### Priority Order
handle_replies > send_campaigns > write_campaigns > prospect > idle (run analyst)

### Key Commands
```bash
source .venv/bin/activate    # Always activate venv first
franco run                   # Start the heartbeat loop
franco dashboard             # Web UI at http://localhost:5555
franco setup                 # Re-run setup wizard
franco train <url>           # Train on a product website
franco status                # Pipeline summary
franco usage                 # Claude quota gauges + per-agent token usage (no dollar costs — subscription plans aren't billed per token)
franco export                # Deliverable prospects → sequencer-ready CSV (prospects.csv)
franco export --all          # Full raw list, no filters
franco gmail auth            # One-time Gmail OAuth (when provider: gmail)
franco gmail test            # Verify the Gmail connection
franco outbox                # Review queued emails; --approve <id> / --approve-all / --reject <id> / --edit <id>
franco import FILE           # CSV list: --kind accounts|contacts|suppressions; --dry-run / --verify / --template
franco preview [KEY]         # Preview of a campaign/funnel (--csv FILE); preview signoff KEY --by NAME / reopen KEY
franco funnels               # Raad mode: funnels and how companies split; --company DOMAIN explains one
franco suppress list|add|remove|import|export   # Shared do-not-contact list (CSV in/out)
franco signals               # The signal vocabulary; --confirm / --reject CODES (or 'free' / 'all')
franco discover              # Find businesses; --providers / --estimate / --provider <key>
franco autopilot on|off|default|status  # Send without review (runtime toggle)
franco sending pause|resume  # Kill switch for all outbound
```

### Common Issues
- **"command not found: franco"**: Activate venv first: `source .venv/bin/activate`
- **"ModuleNotFoundError: No module named 'franco'" after install (macOS)**: Python 3.13 silently ignores `.pth` files that carry the macOS hidden file flag, and some Macs propagate that flag into `.venv`. Run `franco install` again (it auto-fixes by linking the package), or manually: `ln -s "$(pwd)/franco" .venv/lib/python3.13/site-packages/franco`
- **"externally-managed-environment"**: Use a venv, not system Python
- **SQLite errors**: The `data/` directory is created automatically on first run
- **Claude headless mode fails**: User needs `claude login` and an active Max subscription
- **Codex calls fail** (`brain.provider: codex`): the log line says why — `codex login` with ChatGPT (also when the login "has expired or is due for renewal": Franco never renews it), install the supported Codex version (`franco status --check` shows what's wrong without a model call; while Codex is unusable Franco pauses whole cycles on purpose), pick a `brain.model` without code mode, or remove whatever Codex config layer is forcing tools/hooks/MCP back on. Never weaken Franco's isolation checks to get past it.
- **Instantly API 401**: Wrong API key or needs the Growth plan for API access
- **Cold email held with "compliance check unavailable"** (`compliance.precleared` on): the log line names the address and says why. Without a key Precleared allows only a few checks a day, so add a free `PRECLEARED_API_KEY` or wait until tomorrow; a rejected key, a network problem or a failed check of one address holds it the same way. Replies still send.
- **Google search rate limiting**: Franco automatically falls back to DuckDuckGo and Bing. For reliable search, add a Serper API key ($5/mo).
