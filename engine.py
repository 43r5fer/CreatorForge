"""Generation engine for Forge.

Holds the Perplexity built-in model client, the multi-file streaming protocol the
model writes projects in, the preview assembler, and the offline fallback project.
Kept separate from api_server.py so the HTTP layer stays readable.
"""
from __future__ import annotations

import json
import os
import functools
import re

import design
import verify as verifier

PPLX_MODEL = os.environ.get("PPLX_MODEL", "claude_sonnet_4_6")
THINK_BUDGET = int(os.environ.get("PPLX_THINK_BUDGET", "6000"))
BUILD_TOKENS = int(os.environ.get("PPLX_BUILD_TOKENS", "32000"))


# --------------------------------------------------------------------------- models

def pplx_client():
    """An async Anthropic client pointed at Perplexity's built-in models.

    Credentials are injected into the server process, so constructing the client
    is enough — there is no key to pass around.
    """
    from anthropic import AsyncAnthropic
    return AsyncAnthropic()


async def pplx_probe(model: str | None = None) -> dict:
    c = pplx_client()
    mid = model or PPLX_MODEL
    m = await c.messages.create(model=mid, max_tokens=8,
                                messages=[{"role": "user", "content": "ok"}])
    return {"model": mid, "stop": m.stop_reason}


async def pplx_stream(system: str, user: str, max_tokens: int = 4000,
                      think: int = 0, model: str | None = None):
    """Yield ('reason'|'text', chunk) from a built-in model.

    Extended thinking is enabled when `think` is set. The proxy returns thinking
    in summarised form, so treat 'reason' chunks as a bonus rather than the main
    window into the model's work — the plan phase is what we show the user.
    """
    c = pplx_client()
    kw = {}
    if think:
        # The thinking budget has to leave room for the answer itself.
        kw["thinking"] = {"type": "enabled", "budget_tokens": min(think, max_tokens - 1024)}
    async with c.messages.stream(model=(model or PPLX_MODEL), max_tokens=max_tokens,
                                 system=system,
                                 messages=[{"role": "user", "content": user}],
                                 **kw) as st:
        async for ev in st:
            if ev.type != "content_block_delta":
                continue
            d = ev.delta
            t = getattr(d, "type", "")
            if t == "thinking_delta":
                yield "reason", d.thinking
            elif t == "text_delta":
                yield "text", d.text


# --------------------------------------------------------------------------- prompts

STACK = """server.py            FastAPI + SQLite backend: REST endpoints under /api, serves ./static
requirements.txt     pinned dependencies
static/index.html    markup only, loads Tailwind via CDN plus styles.css and app.js
static/styles.css    custom CSS on top of Tailwind (component classes, animations)
static/app.js        all frontend logic, talks to the backend over fetch
README.md            what it does, how to run it, endpoint list"""

SYS_PLAN = (
    "You are a senior software architect. Given an app idea and the user's decisions, write a "
    "terse build plan for a real full-stack web app with this file layout:\n\n" + STACK + "\n\n"
    "Cover: the screens and their layout, the SQLite schema, every REST endpoint with its "
    "method and shape, the frontend state, the visual direction (Tailwind utilities plus custom "
    "CSS), and the edge cases and failure states to handle. Open with two lines of ART "
    "DIRECTION: the named typeface, the palette with hex values, and the one-word mood. "
    "Think hard about the data model and "
    "the error paths before you write anything. "
    "Output at most 260 words of plain bullet points. No code."
)

SYS_BUILD = (
    "You are a senior full-stack engineer. Build the requested app as a complete, working "
    "project. Emit every file with this exact protocol and nothing else — no prose, no markdown "
    "fences, no commentary before, between or after:\n\n"
    "<<<FILE path/name.ext>>>\n"
    "...the entire file content...\n"
    "<<<ENDFILE>>>\n\n"
    "Emit exactly these files, in this order:\n\n" + STACK + "\n\n"
    "Hard requirements:\n"
    "- server.py: FastAPI app, SQLite via sqlite3 with the table created at startup, full CRUD "
    "REST endpoints returning JSON, pydantic models for request bodies, correct HTTP status "
    "codes, and StaticFiles mounted so GET / serves static/index.html. Runs with "
    "`uvicorn server:app`. Must include `if __name__ == \"__main__\":` running uvicorn on "
    "port 8000.\n"
    "- static/index.html: semantic markup, Tailwind via "
    "<script src=\"https://cdn.tailwindcss.com\"></script>, links styles.css, loads app.js as a "
    "deferred script. No inline logic.\n"
    "- static/styles.css: real custom CSS — component classes, focus states, transitions. Not "
    "an empty file.\n"
    "- static/app.js: fetches from the backend, renders the UI, handles loading, empty and "
    "error states, and validates input before sending.\n"
    "- Responsive on phone and desktop. Accessible labels on every control.\n"
    "- Every endpoint the frontend calls must exist in server.py with a matching shape.\n\n"
    + design.DESIGN + "\n\n" + design.REVIEW
)

SYS_FIX = design.SYS_FIX


def fix_prompt(files: dict, issues: list, note: str = "") -> str:
    """The user turn for a repair or change pass.

    A repair only needs the files the errors touch. A change request the user typed has
    no error list to narrow things down, so the model gets every source file and decides
    for itself which ones to re-emit.
    """
    hot = {i["file"] for i in issues if i.get("file") in files}
    # always give the model both sides of a contract, or it cannot judge which is wrong
    if any(i["kind"] in ("contract", "dom") for i in issues):
        for p in files:
            if p.endswith((".py", ".html", ".js")):
                hot.add(p)
    change_only = bool(note.strip()) and not issues
    if change_only:
        hot = {p for p in files
               if p.endswith((".py", ".html", ".js", ".css")) or "/" not in p}
    parts = []
    if issues:
        parts += ["ERRORS FOUND:", verifier.brief(issues)]
        if note:
            parts += ["", "ALSO REPORTED BY THE USER:", note.strip()[:2000]]
    else:
        parts += [
            "THE USER WANTS THIS CHANGED:",
            note.strip()[:2000],
            "",
            "Make exactly that change and nothing else. Keep the existing visual design, "
            "structure, file names and every feature that already works. Re-emit only the "
            "files you actually had to modify, each one complete.",
        ]
    parts += ["", "THE CURRENT FILES:"]
    for p in sorted(hot):
        parts.append(f"<<<FILE {p}>>>\n{files[p]}\n<<<ENDFILE>>>")
    return "\n".join(parts)


# --------------------------------------------------------------------------- integrations

# Every integration ships two paths: a real one that switches on as soon as the keys
# exist, and a dev path that works immediately with no account anywhere. That is what
# lets a generated project run the moment it is unzipped.
import catalog

INTEGRATIONS = {
    "auth_google": {
        "label": "Google sign-in",
        "why": "users log in",
        "env": ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"],
        "deps": ["authlib>=1.3.0", "itsdangerous>=2.1.2"],
        "module": "integrations/auth.py",
        "build": (
            "Google OAuth 2.0 login. integrations/auth.py exposes `router` with GET "
            "/api/auth/login (redirect to Google), GET /api/auth/callback (exchange code, "
            "create a signed session cookie), GET /api/auth/me and POST /api/auth/logout, "
            "plus a `current_user(request)` dependency and a `users` table "
            "(id, email, name, picture, provider, created_at). "
            "DEV PATH: when GOOGLE_CLIENT_ID is unset, /api/auth/login renders a tiny local "
            "sign-in form that accepts any email and issues the same session cookie, so the "
            "whole logged-in experience works with zero setup. Say which mode is active in "
            "the JSON from /api/auth/me."
        ),
        "setup": [
            "Open console.cloud.google.com → APIs & Services → Credentials.",
            "Create an OAuth client ID of type Web application.",
            "Add http://localhost:8000/api/auth/callback as an authorised redirect URI.",
            "Copy the client ID and secret into .env as GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.",
        ],
    },
    "db_supabase": {
        "label": "Supabase Postgres",
        "why": "data lives in a hosted database",
        "env": ["SUPABASE_URL", "SUPABASE_SERVICE_KEY"],
        "deps": ["httpx>=0.27.0"],
        "module": "integrations/db.py",
        "build": (
            "integrations/db.py is one storage layer with two backends behind the same "
            "functions (`fetch_all`, `fetch_one`, `insert`, `update`, `delete`). When "
            "SUPABASE_URL and SUPABASE_SERVICE_KEY are set it talks to the Supabase REST API "
            "over httpx with the apikey and Authorization headers. Otherwise it uses the local "
            "SQLite file. server.py only ever calls these functions, never sqlite3 directly, "
            "so switching to Supabase is a matter of filling in .env. Include the SQL to create "
            "the tables in Supabase as a comment block at the top of the module."
        ),
        "setup": [
            "Create a project at supabase.com and open Project Settings → API.",
            "Copy the Project URL and the service_role key.",
            "Run the CREATE TABLE block from the top of integrations/db.py in the SQL editor.",
            "Put both values in .env as SUPABASE_URL and SUPABASE_SERVICE_KEY.",
        ],
    },
    "storage_files": {
        "label": "File uploads",
        "why": "users upload images or documents",
        "env": ["S3_BUCKET", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_ENDPOINT"],
        "deps": [],
        "module": "integrations/storage.py",
        "build": (
            "integrations/storage.py exposes `save(upload) -> url` and `delete(url)`. Default "
            "path writes to ./uploads and serves it as a mounted static route, which needs no "
            "account at all. When S3_BUCKET and the S3 keys are set it uploads there instead "
            "with a signed PUT and returns the public URL. server.py gets a POST "
            "/api/upload endpoint using UploadFile that validates content type and size."
        ),
        "setup": [
            "Nothing to do — uploads land in ./uploads and are served from /uploads.",
            "To move to object storage, set S3_BUCKET, S3_ACCESS_KEY, S3_SECRET_KEY and S3_ENDPOINT in .env.",
        ],
    },
    "payments_stripe": {
        "label": "Stripe payments",
        "why": "the app charges money",
        "env": ["STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"],
        "deps": ["httpx>=0.27.0"],
        "module": "integrations/payments.py",
        "build": (
            "integrations/payments.py exposes `router` with POST /api/checkout (creates a "
            "Stripe Checkout Session over the REST API and returns its url), POST "
            "/api/stripe/webhook (marks the order paid), and an `orders` table. "
            "DEV PATH: without STRIPE_SECRET_KEY, /api/checkout returns a local "
            "/checkout/mock?order=<id> url that marks the order paid on confirm, so the full "
            "purchase flow is clickable with no Stripe account."
        ),
        "setup": [
            "Grab your test secret key from dashboard.stripe.com/test/apikeys.",
            "Set STRIPE_SECRET_KEY in .env.",
            "For webhooks run: stripe listen --forward-to localhost:8000/api/stripe/webhook",
            "Copy the printed signing secret into STRIPE_WEBHOOK_SECRET.",
        ],
    },
    "email": {
        "label": "Transactional email",
        "why": "the app sends mail",
        "env": ["RESEND_API_KEY", "MAIL_FROM"],
        "deps": ["httpx>=0.27.0"],
        "module": "integrations/mailer.py",
        "build": (
            "integrations/mailer.py exposes `async send(to, subject, html)`. With "
            "RESEND_API_KEY set it posts to the Resend API. Without it, the message is written "
            "to an `outbox` table and printed to the console, and GET /api/outbox lists it — "
            "so email-driven flows are testable with no provider."
        ),
        "setup": [
            "Create an API key at resend.com/api-keys.",
            "Set RESEND_API_KEY and MAIL_FROM (a verified sender) in .env.",
        ],
    },
    "maps": {
        "label": "Maps and places",
        "why": "the app shows locations",
        "env": ["MAPS_API_KEY"],
        "deps": [],
        "module": "integrations/geo.py",
        "build": (
            "Use Leaflet from a CDN with OpenStreetMap tiles — no key, works out of the box. "
            "integrations/geo.py wraps geocoding: Nominatim by default, and the Google "
            "Geocoding API when MAPS_API_KEY is set. Expose GET /api/geocode?q= returning "
            "lat/lon, and cache results in a table so repeat lookups are free."
        ),
        "setup": [
            "Nothing required — Leaflet and OpenStreetMap need no key.",
            "For Google geocoding accuracy, set MAPS_API_KEY in .env.",
        ],
    },
    "ai": {
        "label": "AI text generation",
        "why": "the app calls a language model",
        "env": ["OPENAI_API_KEY", "OPENAI_BASE_URL", "AI_MODEL"],
        "deps": ["httpx>=0.27.0"],
        "module": "integrations/ai.py",
        "build": (
            "integrations/ai.py exposes `async complete(system, user) -> str` posting to an "
            "OpenAI-compatible /chat/completions endpoint (OPENAI_BASE_URL defaults to "
            "https://api.openai.com/v1, AI_MODEL to gpt-4o-mini). Without a key it returns a "
            "clearly-labelled canned response so the feature is still demoable. Never call the "
            "model from the browser — always through a backend endpoint."
        ),
        "setup": [
            "Set OPENAI_API_KEY in .env.",
            "To use a different provider, point OPENAI_BASE_URL at its OpenAI-compatible URL and set AI_MODEL.",
        ],
    },
    "realtime": {
        "label": "Realtime updates",
        "why": "several people see changes at once",
        "env": [],
        "deps": [],
        "module": "integrations/live.py",
        "build": (
            "integrations/live.py holds a connection manager and a WebSocket route at /ws "
            "that broadcasts change events to every client. server.py broadcasts after each "
            "write; app.js opens the socket, re-renders on a message, and reconnects with "
            "backoff when it drops. No keys, no service — this runs on the app's own server."
        ),
        "setup": ["Nothing to configure — the WebSocket runs on the app's own server."],
    },
}

# Plain-language triggers. Matched against the prompt, the answers and the plan.
_TRIGGERS = {
    "auth_google": ["login", "log in", "sign in", "signin", "sign up", "signup", "account",
                    "auth", "user profile", "my account", "google", "per user", "users can save",
                    "personal", "private", "member", "permission", "role"],
    "db_supabase": ["supabase", "postgres", "hosted database", "cloud database", "sync across",
                    "multiple devices", "shared data", "team", "production database"],
    "storage_files": ["upload", "photo", "image", "picture", "avatar", "attachment", "file",
                      "pdf", "document", "gallery", "scan"],
    # Taking money is a big thing to add to someone's app uninvited, so this needs a word that
    # only appears when money genuinely changes hands. "buy", "purchase" and "order" are gone:
    # a shopping list remembers what you buy and a cafe app takes orders, neither charges a card.
    "payments_stripe": ["pay", "payment", "checkout", "stripe", "subscription", "pricing",
                        "cart", "invoice", "billing", "premium", "paid plan", "charge card",
                        "sell", "storefront", "e-commerce", "ecommerce"],
    "email": ["email", "e-mail", "notify", "notification", "reminder", "invite", "newsletter",
              "confirmation", "magic link", "reset password"],
    "maps": ["map", "location", "address", "nearby", "distance", "route", "gps", "geo",
             "restaurant", "store locator", "delivery"],
    "ai": ["ai ", "gpt", "llm", "chatbot", "chat bot", "summarise", "summarize", "generate text",
           "recommend", "suggestion", "smart", "assistant", "translate", "sentiment"],
    "realtime": ["realtime", "real-time", "collaborate", "collaborative", "chat room",
                 "live chat", "group chat", "multiplayer", "presence", "instantly see",
                 "live updates", "see each other"],
}


# The original eight predate categories; label them, then fold in the long tail.
for _k, _c in {
    "auth_google": "Sign-in", "db_supabase": "Data", "storage_files": "Data",
    "payments_stripe": "Money", "email": "Reach", "maps": "Insight",
    "ai": "AI models", "realtime": "Data",
}.items():
    INTEGRATIONS[_k].setdefault("cat", _c)
INTEGRATIONS["auth_google"].setdefault("signup", "https://console.cloud.google.com")
INTEGRATIONS["db_supabase"].setdefault("signup", "https://supabase.com/dashboard")
INTEGRATIONS["storage_files"].setdefault("signup", "https://aws.amazon.com/s3/")
INTEGRATIONS["payments_stripe"].setdefault("signup", "https://dashboard.stripe.com/apikeys")
INTEGRATIONS["email"].setdefault("signup", "https://resend.com/api-keys")
INTEGRATIONS["maps"].setdefault("signup", "https://console.cloud.google.com")
INTEGRATIONS["ai"].setdefault("signup", "https://platform.openai.com/api-keys")
INTEGRATIONS["realtime"].setdefault("signup", "")

INTEGRATIONS.update(catalog.EXTRA)
_TRIGGERS.update(catalog.TRIGGERS)

# Superseded by the named providers above. Hidden from the connect form, kept in the catalog
# so anything generated before the split still resolves its module.
INTEGRATIONS["ai"]["legacy"] = True
_TRIGGERS["ai_openai"] = sorted(set(_TRIGGERS["ai_openai"]) | set(_TRIGGERS.pop("ai")))


# Some env vars are only a model name, and the generated code already carries a sane default.
# Demanding them would make those rows impossible to tick even though the key is in place, so
# they are recorded as nice-to-have and left out of the "is this connected?" test.
OPTIONAL_ENV = {
    "CLAUDE_MODEL", "GEMINI_MODEL", "OPENAI_MODEL", "GROQ_MODEL", "OPENROUTER_MODEL",
}


def req_env(key: str) -> list[str]:
    """The env vars that must be present before an integration counts as connected."""
    env = INTEGRATIONS.get(key, {}).get("env", [])
    need = [e for e in env if e not in OPTIONAL_ENV]
    # Never return nothing for something that does need an account.
    return need or env


def is_optional_env(name: str) -> bool:
    return name in OPTIONAL_ENV


def cat_of(key: str) -> str:
    return INTEGRATIONS.get(key, {}).get("cat") or "Other"


def _one_ai(found: list[str], connected: set[str] | None = None) -> list[str]:
    """Asking for "an AI feature" should not demand five different accounts.

    Every provider in AI_KEYS does the same job, so if the prompt matched more than one we
    keep a single provider: one the person has already connected if possible, otherwise the
    first in preference order. The generic "ai" entry is dropped whenever a named provider
    survives, since it would write a second, redundant module.
    """
    names = [k for k in found if k in catalog.AI_KEYS]
    if not names:
        return found
    have = [k for k in catalog.AI_KEYS if k in names and k in (connected or set())]
    keep = have[0] if have else next(k for k in catalog.AI_KEYS if k in names)
    return [k for k in found if k == keep or (k not in catalog.AI_KEYS and k != "ai")] \
        if len(names) > 1 or "ai" in found else found


@functools.lru_cache(maxsize=None)
def _trigger_re(key: str):
    """Whole-word matcher for one integration's triggers.

    Substring matching was finding "route" inside "Express routes" and pulling a maps
    integration into apps that have nothing to do with maps. Word boundaries stop that while
    still matching multi-word phrases and hyphenated terms like "real-time".
    """
    terms = [re.escape(t.strip()) for t in _TRIGGERS.get(key, ()) if t.strip()]
    if not terms:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(terms) + r")(?!\w)")


def detect_integrations(prompt: str, answers: list[dict] | None = None,
                        plan: str = "", connected: set[str] | None = None) -> list[str]:
    """Which integrations this app actually needs, in catalog order."""
    hay = " ".join([
        (prompt or ""),
        " ".join(f"{a.get('q','')} {a.get('a','')}" for a in (answers or [])),
        plan or "",
    ]).lower()
    found = []
    for key in INTEGRATIONS:
        rx = _trigger_re(key)
        if rx and rx.search(hay):
            found.append(key)
    found = _one_ai(found, connected)
    # Razorpay and Stripe do the same job. If the prompt named rupees or UPI, that settles it.
    if "payments_razorpay" in found and "payments_stripe" in found:
        indian = any(t in hay for t in ("upi", "rupee", "inr", "razorpay", "netbanking",
                                        "phonepe", "paytm", "india"))
        found.remove("payments_stripe" if indian else "payments_razorpay")
    # Google login is implied by anything that stores per-person data behind a paywall.
    if "payments_stripe" in found and "auth_google" not in found:
        found.insert(0, "auth_google")
    return found


def integration_brief(ids: list[str], configured: dict[str, bool] | None = None) -> str:
    """The extra instructions handed to the model for the detected integrations."""
    if not ids:
        return ""
    configured = configured or {}
    lines = [
        "",
        "INTEGRATIONS — this app needs the following. Build each one as its own module under "
        "integrations/ and wire it into server.py with `app.include_router(...)` where a router "
        "is described. Emit those module files too, after server.py.",
        "",
    ]
    for key in ids:
        spec = INTEGRATIONS[key]
        have = [e for e in spec["env"] if configured.get(e)]
        state = ("Keys are already present in .env: " + ", ".join(have)) if have else \
                ("No keys are set, so the dev path must carry the whole feature."
                 if spec["env"] else "Needs no keys at all.")
        lines.append(f"* {spec['label']} — {spec['module']}\n  {spec['build']}\n  {state}")
    lines += [
        "",
        "Integration rules, without exception:",
        "- Read every secret with os.environ.get(), using exactly the variable names listed "
        "above, and never hard-code one. A module called integrations/env.py already loads "
        "the .env file into os.environ for you — do not write your own loader and do not add "
        "python-dotenv.",
        "- The app must start and every feature must be usable with an empty .env. A missing "
        "key switches on the dev path; it never raises and never shows a blank screen.",
        "- A key that is present but WRONG must not break the app either. Wrap every call out "
        "to a third-party service in try/except, log the reason, and fall back to that "
        "integration's dev path so the user still gets a working response. Never let a "
        "provider error surface as a 500.",
        "- Add GET /api/health returning each integration's name and whether it is running "
        "'live' or 'dev', built from integrations.env.status(...). Never return key values.",
        "- Add each integration's packages to requirements.txt.",
        "- README.md gets a '## Integrations' section listing each one, whether it is running "
        "live or in dev mode, and the exact steps to switch it live.",
    ]
    return "\n".join(lines)


def env_files(ids: list[str], values: dict[str, str] | None = None) -> dict[str, str]:
    """.env.example always, plus a real .env pre-filled with whatever keys Forge holds."""
    if not ids:
        return {}
    values = values or {}
    ex, real = ["# Copy to .env and fill in what you need.",
                "# Every value is optional — empty means that integration runs in dev mode.", ""], \
               ["# Written by Forge. Keys it already had are filled in.", ""]
    for key in ids:
        spec = INTEGRATIONS[key]
        if not spec["env"]:
            continue
        ex.append(f"# {spec['label']}")
        real.append(f"# {spec['label']}")
        for e in spec["env"]:
            ex.append(f"{e}=")
            real.append(f"{e}={values.get(e, '')}")
        ex.append("")
        real.append("")
    out = {".env.example": "\n".join(ex).rstrip() + "\n"}
    if any(values.get(e) for k in ids for e in INTEGRATIONS[k]["env"]):
        out[".env"] = "\n".join(real).rstrip() + "\n"
    return out


def integration_deps(ids: list[str]) -> list[str]:
    seen = []
    for key in ids:
        for d in INTEGRATIONS[key]["deps"]:
            if d not in seen:
                seen.append(d)
    return seen


def public_catalog() -> list[dict]:
    """Catalog for the UI — labels, env var names, setup steps. Never any values."""
    return [{"id": k, "label": v["label"], "why": v["why"], "env": v["env"],
             "module": v["module"], "setup": v["setup"]} for k, v in INTEGRATIONS.items()]


# --------------------------------------------------------------------------- env wiring

ENV_LOADER = '''"""Loads .env into os.environ before anything else reads it.

Forge writes this file. It has no dependencies on purpose - importing it is enough, so
`uvicorn server:app` picks the keys up with no extra install and no export step.
"""
import os
from pathlib import Path

# Different libraries spell the same secret differently. Anything written under the
# left-hand name is also exposed under its aliases, so a module reading either one works.
ALIASES = {
    "S3_ACCESS_KEY": ["AWS_ACCESS_KEY_ID"],
    "S3_SECRET_KEY": ["AWS_SECRET_ACCESS_KEY"],
    "S3_BUCKET": ["AWS_S3_BUCKET", "BUCKET_NAME"],
    "S3_ENDPOINT": ["AWS_ENDPOINT_URL", "S3_ENDPOINT_URL"],
    "SUPABASE_SERVICE_KEY": ["SUPABASE_KEY", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY"],
    "GOOGLE_CLIENT_ID": ["GOOGLE_OAUTH_CLIENT_ID"],
    "GOOGLE_CLIENT_SECRET": ["GOOGLE_OAUTH_CLIENT_SECRET"],
    "OPENAI_API_KEY": ["AI_API_KEY", "LLM_API_KEY"],
    "RESEND_API_KEY": ["MAIL_API_KEY", "EMAIL_API_KEY"],
    "MAPS_API_KEY": ["GOOGLE_MAPS_API_KEY"],
    "STRIPE_SECRET_KEY": ["STRIPE_API_KEY"],
}


def _parse(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip().strip('"').strip("'")
        if val:
            out[key.strip()] = val
    return out


def load(path=None):
    """Read .env if present, without clobbering anything already in the environment."""
    root = Path(__file__).resolve().parent.parent
    files = [Path(path)] if path else [root / ".env", root / ".env.local"]
    for candidate in files:
        if candidate.exists():
            for key, val in _parse(candidate.read_text()).items():
                os.environ.setdefault(key, val)
    for name, alts in ALIASES.items():
        val = os.environ.get(name)
        if val:
            for alt in alts:
                os.environ.setdefault(alt, val)
        else:
            for alt in alts:
                if os.environ.get(alt):
                    os.environ.setdefault(name, os.environ[alt])
                    break
    return os.environ


def status(*names):
    """For a /api/health endpoint: which keys are present, never their values."""
    return {n: bool(os.environ.get(n)) for n in names}


load()
'''

PKG_INIT = '"""Integration modules generated by Forge."""\n'

_ENV_IMPORT = "from integrations import env as _env  # loads .env before anything reads it"


def ensure_env_loading(files: dict) -> list[str]:
    """Drop the loader in and make server.py import it first. Returns the paths touched."""
    touched = []
    # Always Forge's loader, even if the model wrote its own — ours handles the alias
    # spellings and never raises on a missing file.
    if files.get("integrations/env.py") != ENV_LOADER:
        files["integrations/env.py"] = ENV_LOADER
        touched.append("integrations/env.py")
    if "integrations/__init__.py" not in files:
        files["integrations/__init__.py"] = PKG_INIT
        touched.append("integrations/__init__.py")

    src = files.get("server.py")
    if src and "integrations import env" not in src:
        lines = src.split("\n")
        at = 0
        first = lines[0].lstrip() if lines else ""
        for quote in ('"""', "'''"):
            if first.startswith(quote):
                if first.count(quote) >= 2:
                    at = 1
                else:
                    for i in range(1, len(lines)):
                        if quote in lines[i]:
                            at = i + 1
                            break
                break
        while at < len(lines) and lines[at].startswith("from __future__"):
            at += 1
        lines.insert(at, _ENV_IMPORT)
        files["server.py"] = "\n".join(lines)
        touched.append("server.py")
    return touched


# --------------------------------------------------------------------------- protocol

OPEN_RE = re.compile(r"<<<\s*FILE\s+([^\n>]+?)\s*>>>[ \t]*\n?")
CLOSE = "<<<ENDFILE>>>"
_KEEP = 300  # enough buffer that a marker split across chunks still matches


class ProjectParser:
    """Incrementally turns the model's stream into per-file events.

    Yields ('open', path, ''), ('body', path, chunk) and ('close', path, '') so the
    UI can open a tab and type into it while the model is still writing.
    """

    def __init__(self):
        self.buf = ""
        self.cur: str | None = None
        self.order: list[str] = []
        self.parts: dict[str, list[str]] = {}

    def feed(self, text: str):
        self.buf += text
        while True:
            if self.cur is None:
                m = OPEN_RE.search(self.buf)
                if not m:
                    if len(self.buf) > _KEEP:
                        self.buf = self.buf[-_KEEP:]   # drop stray prose, keep a tail
                    return
                path = clean_path(m.group(1))
                self.buf = self.buf[m.end():]
                if not path:
                    continue
                self.cur = path
                if path not in self.parts:
                    self.parts[path] = []
                    self.order.append(path)
                yield "open", path, ""
            else:
                i = self.buf.find(CLOSE)
                if i == -1:
                    safe = len(self.buf) - (len(CLOSE) - 1)
                    if safe > 0:
                        out, self.buf = self.buf[:safe], self.buf[safe:]
                        self.parts[self.cur].append(out)
                        yield "body", self.cur, out
                    return
                out = self.buf[:i]
                if out:
                    self.parts[self.cur].append(out)
                    yield "body", self.cur, out
                yield "close", self.cur, ""
                self.buf = self.buf[i + len(CLOSE):]
                self.cur = None

    def finish(self):
        """Close a file the model left hanging when it hit the token ceiling."""
        if self.cur is not None:
            if self.buf.strip():
                self.parts[self.cur].append(self.buf)
            yield "close", self.cur, ""
            self.cur = None
        self.buf = ""

    def files(self) -> dict:
        out = {}
        for p in self.order:
            body = "".join(self.parts[p]).strip("\n")
            if body.strip():
                out[p] = body
        return out


def clean_path(raw: str) -> str:
    p = raw.strip().strip('"\'`').lstrip("./").replace("\\", "/")
    p = re.sub(r"\s+", " ", p)
    if not p or p.startswith("/") or ".." in p.split("/"):
        return ""
    return p[:120]


# --------------------------------------------------------------------------- preview

PREVIEW_SHIM = """
<script>
/* Storage shim. The thumbnail and gallery iframes are sandboxed without
   allow-same-origin, so touching localStorage throws a SecurityError and kills the whole
   app before it paints. Hand it a working in-memory store instead, so a generated app
   that saves state still runs in every preview surface. */
(function(){
  function usable(name){
    try { var s = window[name]; s.setItem('__forge','1'); s.removeItem('__forge'); return true; }
    catch(e){ return false; }
  }
  function shim(){
    var m = {};
    return {
      getItem: function(k){ return Object.prototype.hasOwnProperty.call(m, k) ? m[k] : null; },
      setItem: function(k, v){ m[k] = String(v); },
      removeItem: function(k){ delete m[k]; },
      clear: function(){ m = {}; },
      key: function(i){ return Object.keys(m)[i] != null ? Object.keys(m)[i] : null; },
      get length(){ return Object.keys(m).length; }
    };
  }
  ['localStorage','sessionStorage'].forEach(function(name){
    if(usable(name)) return;
    try { Object.defineProperty(window, name, {value: shim(), configurable: true, writable: true}); }
    catch(e){ try { window[name] = shim(); } catch(e2){} }
  });
})();
</script>
<script>
/* Forge preview shim — the generated backend is not running inside this iframe, so
   fetch() calls to the API are answered from an in-memory store. Enough for the UI to
   render, load, add and delete. Download the project to run the real backend. */
(function(){
  var db = {}, seq = 1;
  function key(u){ return String(u).split('?')[0].replace(/\\/$/,'').replace(/\\/\\d+$/,''); }
  function reply(body, status){
    return Promise.resolve(new Response(JSON.stringify(body),
      {status: status||200, headers:{'Content-Type':'application/json'}}));
  }
  var real = window.fetch ? window.fetch.bind(window) : null;
  window.fetch = function(input, init){
    init = init || {};
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    if(!/^(\\/|\\.\\/)?api\\//.test(String(url).replace(/^https?:\\/\\/[^/]+/,''))){
      return real ? real(input, init) : reply({}, 404);
    }
    var k = key(url), m = (init.method || (input && input.method) || 'GET').toUpperCase();
    db[k] = db[k] || [];
    var payload = {};
    try { payload = init.body ? JSON.parse(init.body) : {}; } catch(e){}
    if(m === 'GET'){
      var one = String(url).match(/\\/(\\d+)\\/?(\\?|$)/);
      if(one){
        var hit = db[k].filter(function(r){ return String(r.id) === one[1]; })[0];
        return hit ? reply(hit) : reply({detail:'Not found'}, 404);
      }
      return reply(db[k]);
    }
    if(m === 'POST'){
      var rec = Object.assign({id: seq++, created_at: new Date().toISOString()}, payload);
      db[k].push(rec); return reply(rec, 201);
    }
    if(m === 'PUT' || m === 'PATCH'){
      var id2 = (String(url).match(/\\/(\\d+)\\/?(\\?|$)/)||[])[1];
      var row = db[k].filter(function(r){ return String(r.id) === id2; })[0];
      if(!row) return reply({detail:'Not found'}, 404);
      Object.assign(row, payload); return reply(row);
    }
    if(m === 'DELETE'){
      var id3 = (String(url).match(/\\/(\\d+)\\/?(\\?|$)/)||[])[1];
      db[k] = db[k].filter(function(r){ return String(r.id) !== id3; });
      return reply({ok:true});
    }
    return reply({ok:true});
  };
})();
</script>
"""


def find_frontend(files: dict) -> str | None:
    for p in files:
        if p.lower().rstrip("/").endswith("index.html"):
            return p
    for p in files:
        if p.lower().endswith(".html"):
            return p
    return None


def _base(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def preview_doc(files: dict, with_shim: bool = True) -> str:
    """Flatten the project's frontend into one standalone HTML document.

    The iframe preview and published pages are single documents, so linked CSS and
    JS get inlined by matching each href/src against a file in the project.
    """
    entry = find_frontend(files)
    if not entry:
        return ("<!doctype html><meta charset=utf-8>"
                "<body style=\"font:15px system-ui;padding:32px;color:#444\">"
                "This project has no HTML page to preview. Download it and run the backend.")
    doc = files[entry]
    by_base = {_base(p): c for p, c in files.items()}

    def css_sub(m):
        href = m.group(1)
        body = by_base.get(_base(href.split("?")[0]))
        return f"<style>\n{body}\n</style>" if body is not None else m.group(0)

    doc = re.sub(r"<link[^>]*rel=[\"']?stylesheet[\"']?[^>]*href=[\"']([^\"']+)[\"'][^>]*>",
                 css_sub, doc, flags=re.I)
    doc = re.sub(r"<link[^>]*href=[\"']([^\"']+\.css)[\"'][^>]*>", css_sub, doc, flags=re.I)

    def js_sub(m):
        src = m.group(1)
        body = by_base.get(_base(src.split("?")[0]))
        if body is None:
            return m.group(0)
        return "<script>\n" + body.replace("</script>", "<\\/script>") + "\n</script>"

    doc = re.sub(r"<script[^>]*src=[\"']([^\"']+)[\"'][^>]*>\s*</script>", js_sub, doc, flags=re.I)

    if with_shim:
        m = re.search(r"</head>", doc, re.I)
        if m:
            # Plain slicing, not re.sub — the shim contains regex escapes that a
            # replacement template would try to interpret.
            doc = doc[:m.start()] + PREVIEW_SHIM + doc[m.start():]
        else:
            doc = PREVIEW_SHIM + doc
    return doc


# --------------------------------------------------------------------------- fallback

def offline_project(prompt: str, answers: list[dict], title: str) -> dict:
    """A small but genuinely working full-stack project, used when no model answers."""
    name = (title or "App").strip()[:60] or "App"
    sub = (prompt or "").strip()[:200]
    picks = "\n".join(f"- {a.get('q','')} → {a.get('a','')}" for a in (answers or [])
                      if str(a.get("a", "")).strip()) or "- (no extra decisions)"
    j = json.dumps
    server = f'''"""{name} — FastAPI + SQLite backend."""
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

DB = Path(__file__).parent / "app.db"
app = FastAPI(title={j(name)})


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


@app.on_event("startup")
def setup():
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            done INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')))""")


class ItemIn(BaseModel):
    text: str


class ItemPatch(BaseModel):
    text: str | None = None
    done: bool | None = None


def row(r):
    return {{"id": r["id"], "text": r["text"], "done": bool(r["done"]),
            "created_at": r["created_at"]}}


@app.get("/api/items")
def list_items():
    with conn() as c:
        return [row(r) for r in c.execute("SELECT * FROM items ORDER BY id DESC")]


@app.post("/api/items", status_code=201)
def add_item(body: ItemIn):
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "text cannot be empty")
    with conn() as c:
        cur = c.execute("INSERT INTO items (text) VALUES (?)", (text,))
        return row(c.execute("SELECT * FROM items WHERE id=?", (cur.lastrowid,)).fetchone())


@app.patch("/api/items/{{item_id}}")
def edit_item(item_id: int, body: ItemPatch):
    with conn() as c:
        r = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not r:
            raise HTTPException(404, "no such item")
        text = body.text.strip() if body.text is not None else r["text"]
        done = int(body.done) if body.done is not None else r["done"]
        c.execute("UPDATE items SET text=?, done=? WHERE id=?", (text, done, item_id))
        return row(c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone())


@app.delete("/api/items/{{item_id}}")
def remove_item(item_id: int):
    with conn() as c:
        if not c.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
            raise HTTPException(404, "no such item")
        c.execute("DELETE FROM items WHERE id=?", (item_id,))
    return {{"ok": True}}


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
'''
    index = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}</title>
<script src="https://cdn.tailwindcss.com"></script>
<link rel="stylesheet" href="styles.css">
</head>
<body class="min-h-screen bg-white text-slate-900 antialiased">
  <main class="mx-auto max-w-2xl px-5 py-12">
    <header class="mb-8">
      <h1 class="text-3xl font-semibold tracking-tight">{name}</h1>
      <p class="mt-2 text-slate-500">{sub}</p>
    </header>

    <form id="add-form" class="flex gap-2" novalidate>
      <label for="item-text" class="sr-only">New item</label>
      <input id="item-text" name="text" required autocomplete="off" placeholder="Add an item…"
             class="field flex-1 rounded-lg border border-slate-200 px-4 py-3">
      <button type="submit" class="btn rounded-lg bg-slate-900 px-5 py-3 font-medium text-white">
        Add
      </button>
    </form>
    <p id="form-error" class="mt-2 hidden text-sm text-red-600" role="alert"></p>

    <section aria-live="polite" class="mt-8">
      <p id="loading" class="text-slate-400">Loading…</p>
      <p id="empty" class="hidden rounded-lg border border-dashed border-slate-200 p-8
                           text-center text-slate-400">Nothing here yet. Add the first item.</p>
      <ul id="list" class="divide-y divide-slate-100"></ul>
    </section>
  </main>
<script src="app.js" defer></script>
</body>
</html>
'''
    css = '''/* Custom layer on top of Tailwind. */
:root { --ring: #1156f0; }

.field { transition: border-color .15s ease, box-shadow .15s ease; }
.field:focus {
  outline: none;
  border-color: var(--ring);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--ring) 18%, transparent);
}

.btn { transition: transform .12s ease, opacity .15s ease; }
.btn:hover { opacity: .9; }
.btn:active { transform: translateY(1px); }
.btn[disabled] { opacity: .5; pointer-events: none; }

.row { animation: rise .18s ease both; }
@keyframes rise { from { opacity: 0; transform: translateY(4px); } }

.row.done .row-text { text-decoration: line-through; color: #94a3b8; }

.sr-only {
  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
}
'''
    js = '''const API = '/api/items';
const list = document.getElementById('list');
const loading = document.getElementById('loading');
const empty = document.getElementById('empty');
const form = document.getElementById('add-form');
const input = document.getElementById('item-text');
const errorBox = document.getElementById('form-error');

function fail(message) {
  errorBox.textContent = message;
  errorBox.classList.remove('hidden');
}
function clearError() { errorBox.classList.add('hidden'); }

async function api(path, options) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    let detail = 'Request failed (' + res.status + ')';
    try { const body = await res.json(); detail = body.detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

function render(items) {
  list.textContent = '';
  empty.classList.toggle('hidden', items.length > 0);
  for (const item of items) {
    const li = document.createElement('li');
    li.className = 'row flex items-center gap-3 py-3' + (item.done ? ' done' : '');

    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = item.done;
    box.className = 'h-4 w-4 rounded border-slate-300';
    box.setAttribute('aria-label', 'Mark "' + item.text + '" done');
    box.addEventListener('change', () => toggle(item, box.checked));

    const text = document.createElement('span');
    text.className = 'row-text flex-1';
    text.textContent = item.text;

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'btn text-sm text-slate-400 hover:text-red-600';
    del.textContent = 'Remove';
    del.setAttribute('aria-label', 'Remove "' + item.text + '"');
    del.addEventListener('click', () => remove(item, del));

    li.append(box, text, del);
    list.append(li);
  }
}

async function load() {
  loading.classList.remove('hidden');
  try {
    render(await api(API));
  } catch (err) {
    fail(err.message);
  } finally {
    loading.classList.add('hidden');
  }
}

async function toggle(item, done) {
  clearError();
  try {
    await api(API + '/' + item.id, { method: 'PATCH', body: JSON.stringify({ done }) });
    await load();
  } catch (err) { fail(err.message); }
}

async function remove(item, button) {
  clearError();
  button.disabled = true;
  try {
    await api(API + '/' + item.id, { method: 'DELETE' });
    await load();
  } catch (err) { fail(err.message); button.disabled = false; }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  clearError();
  const text = input.value.trim();
  if (!text) { fail('Type something first.'); input.focus(); return; }
  const button = form.querySelector('button');
  button.disabled = true;
  try {
    await api(API, { method: 'POST', body: JSON.stringify({ text }) });
    input.value = '';
    await load();
    input.focus();
  } catch (err) { fail(err.message); } finally { button.disabled = false; }
});

load();
'''
    readme = f'''# {name}

{sub}

Built from these decisions:
{picks}

## Run it

```bash
pip install -r requirements.txt
python server.py
```

Then open http://localhost:8000

## Endpoints

| Method | Path | Does |
| --- | --- | --- |
| GET | `/api/items` | List items, newest first |
| POST | `/api/items` | Create an item — `{{"text": "..."}}` |
| PATCH | `/api/items/{{id}}` | Update text or done state |
| DELETE | `/api/items/{{id}}` | Delete an item |

Data lives in `app.db` (SQLite), created on first run.
'''
    return {
        "server.py": server,
        "requirements.txt": "fastapi>=0.110\nuvicorn[standard]>=0.29\npydantic>=2.6\n",
        "static/index.html": index,
        "static/styles.css": css,
        "static/app.js": js,
        "README.md": readme,
    }
