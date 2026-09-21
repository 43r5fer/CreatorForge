"""The rest of the integration catalog.

engine.py defines the original eight; this module adds the long tail and is merged in at
import time. Every entry follows the same contract as the originals:

    label     what a person calls it
    cat       group heading in the connect form
    why       the reason an app would need it, phrased for a non-developer
    env       the keys a person has to paste in. Empty list = nothing to connect.
    deps      pip packages appended to requirements.txt
    module    the single file the generated app puts this behind
    build     instructions handed to the model writing the code
    setup     numbered steps a person follows to get the keys
    signup    where to go to create the account

The rule every entry obeys: the generated app must run with the keys missing. Each module
has a local fallback path so a project is never dead on arrival, and it upgrades itself the
moment real keys appear in .env.
"""

EXTRA: dict[str, dict] = {

    # ---------------------------------------------------------------- AI models
    "ai_claude": {
        "label": "Claude (Anthropic)",
        "cat": "AI models",
        "why": "the app thinks, writes or answers with Claude",
        "env": ["ANTHROPIC_API_KEY", "CLAUDE_MODEL"],
        "deps": ["httpx>=0.27.0"],
        "module": "integrations/ai_claude.py",
        "signup": "https://console.anthropic.com/settings/keys",
        "build": (
            "integrations/ai_claude.py talks to the Anthropic Messages API "
            "(POST https://api.anthropic.com/v1/messages) over httpx with headers "
            "x-api-key: ANTHROPIC_API_KEY and anthropic-version: 2023-06-01. Expose "
            "`async def ask(prompt, system='', max_tokens=1024) -> str` and "
            "`async def stream(prompt, system='')` yielding text deltas from the SSE "
            "content_block_delta events. CLAUDE_MODEL defaults to claude-sonnet-4-5. Read the "
            "reply from data['content'][0]['text']. When ANTHROPIC_API_KEY is unset, `ask` "
            "returns a clearly-marked canned answer so every screen still renders, and "
            "`available()` returns False so the UI can show a 'connect Claude' hint instead of "
            "a broken panel. Surface 401 as 'Claude key rejected' and 429 as 'Claude rate "
            "limited, try again' rather than letting the raw error reach the browser."
        ),
        "setup": [
            "Open console.anthropic.com and sign in.",
            "Go to Settings → API keys and click Create key.",
            "Copy the key — it is only shown once.",
            "Paste it as ANTHROPIC_API_KEY. Leave CLAUDE_MODEL blank for the default.",
        ],
    },
    "ai_gemini": {
        "label": "Gemini (Google)",
        "cat": "AI models",
        "why": "the app uses Gemini for text, images or long documents",
        "env": ["GEMINI_API_KEY", "GEMINI_MODEL"],
        "deps": ["httpx>=0.27.0"],
        "module": "integrations/ai_gemini.py",
        "signup": "https://aistudio.google.com/apikey",
        "build": (
            "integrations/ai_gemini.py calls the Generative Language REST API: POST "
            "https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
            "?key=GEMINI_API_KEY with body {'contents':[{'parts':[{'text':prompt}]}]} and an "
            "optional systemInstruction. Expose `async def ask(prompt, system='') -> str` reading "
            "data['candidates'][0]['content']['parts'][0]['text'], plus "
            "`async def describe_image(bytes, mime, prompt)` using an inline_data part for "
            "Gemini's vision path. GEMINI_MODEL defaults to gemini-2.5-flash. Without a key, "
            "`available()` is False and `ask` returns a marked placeholder. Treat a blocked "
            "safety response (no candidates) as a friendly 'Gemini declined that request' "
            "message, never a 500."
        ),
        "setup": [
            "Open aistudio.google.com/apikey while signed in to a Google account.",
            "Click Create API key and pick a project.",
            "Copy the key.",
            "Paste it as GEMINI_API_KEY. Leave GEMINI_MODEL blank for the default.",
        ],
    },
    "ai_openai": {
        "label": "ChatGPT (OpenAI)",
        "cat": "AI models",
        "why": "the app uses GPT models for chat, writing or embeddings",
        "env": ["OPENAI_API_KEY", "OPENAI_MODEL"],
        "deps": ["httpx>=0.27.0"],
        "module": "integrations/ai_openai.py",
        "signup": "https://platform.openai.com/api-keys",
        "build": (
            "integrations/ai_openai.py calls POST https://api.openai.com/v1/chat/completions "
            "over httpx with Authorization: Bearer OPENAI_API_KEY. Expose "
            "`async def ask(prompt, system='', max_tokens=1024) -> str`, "
            "`async def stream(prompt, system='')` yielding deltas from the SSE "
            "choices[0].delta.content, and `async def embed(texts) -> list[list[float]]` hitting "
            "/v1/embeddings with text-embedding-3-small. OPENAI_MODEL defaults to gpt-4o-mini. "
            "Without a key `available()` is False and `ask` returns a marked placeholder. Map a "
            "401 to 'OpenAI key rejected' and insufficient_quota to 'OpenAI account is out of "
            "credit' so the cause is obvious from the UI."
        ),
        "setup": [
            "Open platform.openai.com/api-keys and sign in.",
            "Click Create new secret key and copy it.",
            "Make sure the account has credit under Settings → Billing.",
            "Paste the key as OPENAI_API_KEY. Leave OPENAI_MODEL blank for the default.",
        ],
    },
    "ai_groq": {
        "label": "Groq",
        "cat": "AI models",
        "why": "the app needs very fast, cheap open-model replies",
        "env": ["GROQ_API_KEY", "GROQ_MODEL"],
        "deps": ["httpx>=0.27.0"],
        "module": "integrations/ai_groq.py",
        "signup": "https://console.groq.com/keys",
        "build": (
            "integrations/ai_groq.py calls POST https://api.groq.com/openai/v1/chat/completions, "
            "which is OpenAI-shaped, with Authorization: Bearer GROQ_API_KEY. Expose `ask` and "
            "`stream` with the same signatures as the other AI modules so they are "
            "interchangeable. GROQ_MODEL defaults to llama-3.3-70b-versatile. Groq is the right "
            "default when the app needs an answer inside a page load; say so in a comment. "
            "Without a key `available()` is False."
        ),
        "setup": [
            "Open console.groq.com/keys and sign in.",
            "Create an API key and copy it.",
            "Paste it as GROQ_API_KEY. Leave GROQ_MODEL blank for the default.",
        ],
    },
    "ai_openrouter": {
        "label": "OpenRouter (any model)",
        "cat": "AI models",
        "why": "one key reaches Claude, GPT, Gemini, Llama and hundreds more",
        "env": ["OPENROUTER_API_KEY", "OPENROUTER_MODEL"],
        "deps": ["httpx>=0.27.0"],
        "module": "integrations/ai_router.py",
        "signup": "https://openrouter.ai/keys",
        "build": (
            "integrations/ai_router.py calls POST https://openrouter.ai/api/v1/chat/completions "
            "with Authorization: Bearer OPENROUTER_API_KEY. Same `ask`/`stream` contract as the "
            "other AI modules. OPENROUTER_MODEL defaults to anthropic/claude-sonnet-4.5 and any "
            "slug from openrouter.ai/models works, so this is the module to use when the app "
            "lets a person pick their own model — expose `async def models()` listing what the "
            "key can reach. Without a key `available()` is False."
        ),
        "setup": [
            "Open openrouter.ai/keys and sign in.",
            "Create a key and add a little credit under Settings → Credits.",
            "Paste the key as OPENROUTER_API_KEY.",
            "Optionally set OPENROUTER_MODEL to any slug from openrouter.ai/models.",
        ],
    },
    "ai_images": {
        "label": "Image generation",
        "cat": "AI models",
        "why": "the app makes pictures from a description",
        "env": ["IMAGE_API_KEY", "IMAGE_PROVIDER"],
        "deps": ["httpx>=0.27.0"],
        "module": "integrations/ai_image.py",
        "signup": "https://platform.openai.com/api-keys",
        "build": (
            "integrations/ai_image.py exposes `async def make(prompt, size='1024x1024') -> url`. "
            "IMAGE_PROVIDER selects the backend: 'openai' posts to "
            "https://api.openai.com/v1/images/generations with model gpt-image-1, 'replicate' "
            "posts to https://api.replicate.com/v1/predictions and polls until the prediction "
            "succeeds. Default is openai. Save the returned bytes through "
            "integrations/storage.py if that module exists so images survive a restart, and "
            "return the stored URL. Without a key, `make` returns a deterministic local SVG "
            "placeholder built from the prompt text so layouts stay intact, and `available()` "
            "is False."
        ),
        "setup": [
            "Decide on a provider: OpenAI (simplest) or Replicate (more models).",
            "Create a key at platform.openai.com/api-keys or replicate.com/account/api-tokens.",
            "Paste it as IMAGE_API_KEY.",
            "Set IMAGE_PROVIDER to openai or replicate.",
        ],
    },
    "ai_speech": {
        "label": "Speech and transcription",
        "cat": "AI models",
        "why": "the app reads text aloud or turns recordings into text",
        "env": ["SPEECH_API_KEY", "SPEECH_VOICE"],
        "deps": ["httpx>=0.27.0"],
        "module": "integrations/speech.py",
        "signup": "https://platform.openai.com/api-keys",
        "build": (
            "integrations/speech.py exposes `async def say(text) -> bytes` (POST "
            "https://api.openai.com/v1/audio/speech, model gpt-4o-mini-tts, voice SPEECH_VOICE "
            "defaulting to alloy) and `async def transcribe(audio_bytes, filename) -> str` (POST "
            "/v1/audio/transcriptions, model whisper-1, multipart body). server.py gets GET "
            "/api/speak?text= streaming audio/mpeg and POST /api/transcribe taking an "
            "UploadFile. Without a key, `say` returns b'' and the frontend falls back to the "
            "browser's built-in speechSynthesis, which needs no account at all — implement that "
            "fallback in the frontend, not just the comment."
        ),
        "setup": [
            "Create a key at platform.openai.com/api-keys.",
            "Paste it as SPEECH_API_KEY.",
            "Optionally set SPEECH_VOICE to alloy, echo, fable, onyx, nova or shimmer.",
        ],
    },
    "ai_vector": {
        "label": "Vector search (Pinecone)",
        "cat": "AI models",
        "why": "the app answers questions from your own documents",
        "env": ["PINECONE_API_KEY", "PINECONE_INDEX"],
        "deps": ["httpx>=0.27.0"],
        "module": "integrations/vectors.py",
        "signup": "https://app.pinecone.io",
        "build": (
            "integrations/vectors.py exposes `async def add(id, text, meta={})`, "
            "`async def search(query, k=5) -> list[dict]` and `async def drop(id)`. It embeds "
            "text through whichever AI module is connected, then upserts and queries the "
            "Pinecone index over its REST API with the Api-Key header. Without "
            "PINECONE_API_KEY, fall back to an in-process store that keeps vectors in a list "
            "and ranks by cosine similarity in pure Python — correct for a few hundred "
            "documents, which is enough to demo retrieval end to end. Say which mode is active "
            "in `status()`."
        ),
        "setup": [
            "Create an index at app.pinecone.io with dimension 1536 and metric cosine.",
            "Copy the API key from the API Keys page.",
            "Paste it as PINECONE_API_KEY and the index name as PINECONE_INDEX.",
        ],
    },

    # ---------------------------------------------------------------- sign-in
    "auth_github": {
        "label": "GitHub sign-in",
        "cat": "Sign-in",
        "why": "developers log in with GitHub",
        "env": ["GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET"],
        "deps": ["authlib>=1.3.0", "itsdangerous>=2.1.2"],
        "module": "integrations/auth_github.py",
        "signup": "https://github.com/settings/developers",
        "build": (
            "GitHub OAuth. integrations/auth_github.py exposes `router` with GET "
            "/api/auth/github/login and /api/auth/github/callback, exchanging the code at "
            "https://github.com/login/oauth/access_token and reading the profile from "
            "https://api.github.com/user. It writes into the same `users` table and issues the "
            "same signed session cookie as integrations/auth.py, so the two sign-in buttons are "
            "interchangeable and a person can use either. Without a client ID the route returns "
            "a 503 JSON explaining which key is missing, and the frontend hides the GitHub "
            "button rather than showing one that fails."
        ),
        "setup": [
            "Open github.com/settings/developers → New OAuth App.",
            "Set the callback URL to http://localhost:8000/api/auth/github/callback.",
            "Generate a client secret.",
            "Paste both as GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET.",
        ],
    },
    "auth_phone": {
        "label": "Phone OTP sign-in",
        "cat": "Sign-in",
        "why": "users sign in with a code sent to their phone",
        "env": ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM"],
        "deps": ["httpx>=0.27.0"],
        "module": "integrations/auth_phone.py",
        "signup": "https://console.twilio.com",
        "build": (
            "integrations/auth_phone.py exposes POST /api/auth/phone/send (generates a 6-digit "
            "code, stores it hashed with a 10-minute expiry, sends it over the Twilio Messages "
            "API) and POST /api/auth/phone/verify (checks the code, issues the shared session "
            "cookie). Rate-limit to 3 sends per number per 15 minutes and never return the code "
            "in a response. DEV PATH: with no Twilio keys the code is printed to the server log "
            "and also returned in the JSON only when a DEV_OTP=1 env flag is set, so the flow is "
            "testable locally without an account."
        ),
        "setup": [
            "Sign in at console.twilio.com and buy or use the trial number.",
            "Copy the Account SID and Auth Token from the console dashboard.",
            "Paste them as TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN.",
            "Put the sending number in TWILIO_FROM in +E.164 form, e.g. +15551234567.",
        ],
    },

    # ---------------------------------------------------------------- data
    "db_postgres": {
        "label": "Postgres (Neon / any host)",
        "cat": "Data",
        "why": "real SQL database with a connection string",
        "env": ["DATABASE_URL"],
        "deps": ["asyncpg>=0.29.0"],
        "module": "integrations/pg.py",
        "signup": "https://neon.tech",
        "build": (
            "integrations/pg.py keeps an asyncpg pool and exposes the same "
            "`fetch_all`/`fetch_one`/`execute` surface as the SQLite layer, so server.py does "
            "not care which is live. It creates its tables on startup with CREATE TABLE IF NOT "
            "EXISTS. Without DATABASE_URL it delegates to the local SQLite module, translating "
            "the handful of syntax differences ($1 placeholders to ?, SERIAL to AUTOINCREMENT) "
            "in one documented helper. Pool size 5, statement timeout 15s, and the connection "
            "string is never logged."
        ),
        "setup": [
            "Create a free Postgres at neon.tech (or use Railway, Supabase, RDS).",
            "Copy the pooled connection string.",
            "Paste it as DATABASE_URL — it starts postgresql://.",
            "Make sure it ends with ?sslmode=require for hosted databases.",
        ],
    },
    "db_mongo": {
        "label": "MongoDB Atlas",
        "cat": "Data",
        "why": "documents rather than tables",
        "env": ["MONGODB_URI", "MONGODB_DB"],
        "deps": ["motor>=3.4.0"],
        "module": "integrations/mongo.py",
        "signup": "https://cloud.mongodb.com",
        "build": (
            "integrations/mongo.py opens a motor AsyncIOMotorClient and exposes "
            "`coll(name)` plus thin `find`/`insert`/`update`/`remove` helpers that convert "
            "ObjectId to str on the way out so the JSON is browser-safe. Create indexes on "
            "startup. Without MONGODB_URI it falls back to the local SQLite layer, storing each "
            "document as JSON in a single table keyed by collection and id — the same helper "
            "signatures, so no calling code changes."
        ),
        "setup": [
            "Create a free cluster at cloud.mongodb.com.",
            "Add a database user, and allow your IP under Network Access.",
            "Click Connect → Drivers and copy the connection string.",
            "Paste it as MONGODB_URI and the database name as MONGODB_DB.",
        ],
    },
    "cache_redis": {
        "label": "Redis",
        "cat": "Data",
        "why": "caching, rate limits, queues and sessions",
        "env": ["REDIS_URL"],
        "deps": ["redis>=5.0.0"],
        "module": "integrations/cache.py",
        "signup": "https://upstash.com",
        "build": (
            "integrations/cache.py exposes `async def get(k)`, `async def set(k, v, ttl=None)`, "
            "`async def incr(k, ttl)` and a `@cached(ttl)` decorator. With REDIS_URL it uses "
            "redis.asyncio; without it, an in-process dict with real TTL expiry, which behaves "
            "identically for a single server. Use `incr` for the rate limiter on any public "
            "endpoint the app exposes."
        ),
        "setup": [
            "Create a free database at upstash.com (or run redis locally).",
            "Copy the redis:// or rediss:// URL.",
            "Paste it as REDIS_URL.",
        ],
    },
    "search_algolia": {
        "label": "Instant search (Algolia)",
        "cat": "Data",
        "why": "typo-tolerant search that responds as you type",
        "env": ["ALGOLIA_APP_ID", "ALGOLIA_ADMIN_KEY", "ALGOLIA_INDEX"],
        "deps": ["httpx>=0.27.0"],
        "module": "integrations/search.py",
        "signup": "https://dashboard.algolia.com",
        "build": (
            "integrations/search.py exposes `async def index(objects)`, "
            "`async def query(q, filters='') -> list[dict]` and `async def unindex(ids)`, "
            "talking to https://{ALGOLIA_APP_ID}-dsn.algolia.net over httpx with the "
            "X-Algolia-API-Key headers. Without keys it falls back to SQL LIKE search over the "
            "local database with a small typo tolerance (match on the first 4 characters of each "
            "word), which is good enough for a few thousand rows. Never ship the admin key to "
            "the browser — all searching goes through the app's own endpoint."
        ),
        "setup": [
            "Create an app at dashboard.algolia.com.",
            "Open Settings → API keys and copy the Application ID and Admin API key.",
            "Paste them as ALGOLIA_APP_ID and ALGOLIA_ADMIN_KEY.",
            "Pick any name for ALGOLIA_INDEX — it is created on first write.",
        ],
    },

    # ---------------------------------------------------------------- money
    "payments_razorpay": {
        "label": "Razorpay (India)",
        "cat": "Money",
        "why": "take UPI, cards and netbanking in rupees",
        "env": ["RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET"],
        "deps": ["httpx>=0.27.0"],
        "module": "integrations/razorpay.py",
        "signup": "https://dashboard.razorpay.com",
        "build": (
            "integrations/razorpay.py exposes `async def order(amount_paise, receipt) -> dict` "
            "(POST https://api.razorpay.com/v1/orders with basic auth of key id and secret), a "
            "`verify(order_id, payment_id, signature)` check using an HMAC-SHA256 of "
            "f'{order_id}|{payment_id}' against RAZORPAY_KEY_SECRET, and POST /api/pay/webhook "
            "verifying the X-Razorpay-Signature header against RAZORPAY_WEBHOOK_SECRET. Amounts "
            "are integer paise everywhere — never floats. The frontend uses the standard "
            "checkout.js handler with only the public key id. TEST PATH: with no keys, "
            "`order` returns a fake order and the frontend shows a 'test mode — no money moves' "
            "banner so the full purchase flow is clickable."
        ),
        "setup": [
            "Sign in at dashboard.razorpay.com and stay in Test mode to start.",
            "Open Account & Settings → API keys and generate a key pair.",
            "Paste them as RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET.",
            "Add a webhook to /api/pay/webhook and copy its secret into RAZORPAY_WEBHOOK_SECRET.",
        ],
    },

    # ---------------------------------------------------------------- reach
    "sms": {
        "label": "SMS and WhatsApp",
        "cat": "Reach",
        "why": "the app texts people",
        "env": ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM"],
        "deps": ["httpx>=0.27.0"],
        "module": "integrations/sms.py",
        "signup": "https://console.twilio.com",
        "build": (
            "integrations/sms.py exposes `async def send(to, body)` posting to the Twilio "
            "Messages API with basic auth. Prefix TWILIO_FROM with 'whatsapp:' to route over "
            "WhatsApp instead — expose `async def whatsapp(to, body)` that does this. Without "
            "keys every message is appended to a local outbox table and shown on an "
            "/api/outbox debug endpoint, so the app's notification logic is fully testable "
            "without spending anything."
        ),
        "setup": [
            "Sign in at console.twilio.com.",
            "Copy the Account SID and Auth Token from the dashboard.",
            "Paste them as TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN.",
            "Set TWILIO_FROM to your Twilio number in +E.164 form.",
        ],
    },
    "push": {
        "label": "Push notifications",
        "cat": "Reach",
        "why": "the app reaches a phone when it is closed",
        "env": ["VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT"],
        "deps": ["pywebpush>=2.0.0"],
        "module": "integrations/push.py",
        "signup": "https://web.dev/articles/push-notifications-web-push-protocol",
        "build": (
            "Web Push over VAPID, no third-party account needed. integrations/push.py exposes "
            "POST /api/push/subscribe storing the browser's PushSubscription JSON, "
            "`async def notify(user_id, title, body, url)` sending through pywebpush, and it "
            "deletes subscriptions that come back 404 or 410. The frontend gets a service "
            "worker (sw.js) with a push listener calling showNotification, and asks permission "
            "only after a deliberate tap, never on page load. Without VAPID keys, "
            "`available()` is False and the UI hides the enable-notifications control. Include "
            "the one-line command to generate the key pair in a comment."
        ),
        "setup": [
            "Generate a VAPID key pair: python -c \"from py_vapid import Vapid01; v=Vapid01(); v.generate_keys(); print(v.public_key, v.private_key)\"",
            "Paste the two values as VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY.",
            "Set VAPID_SUBJECT to mailto:you@example.com.",
        ],
    },
    "calendar": {
        "label": "Google Calendar",
        "cat": "Reach",
        "why": "the app creates or reads calendar events",
        "env": ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"],
        "deps": ["httpx>=0.27.0", "authlib>=1.3.0"],
        "module": "integrations/calendar.py",
        "signup": "https://console.cloud.google.com",
        "build": (
            "integrations/calendar.py reuses the Google OAuth tokens from integrations/auth.py, "
            "asking for the extra scope "
            "https://www.googleapis.com/auth/calendar.events. It exposes "
            "`async def events(start, end)` and `async def add(summary, start, end, desc='')` "
            "against https://www.googleapis.com/calendar/v3/calendars/primary/events, refreshing "
            "the access token when it 401s. Store times as timezone-aware ISO 8601. Without "
            "Google keys it falls back to a local events table so the calendar UI works "
            "offline, and marks each event source as 'local' or 'google' in the JSON."
        ),
        "setup": [
            "In console.cloud.google.com enable the Google Calendar API for your project.",
            "Use the same OAuth client as Google sign-in, or create one.",
            "Add the calendar.events scope on the OAuth consent screen.",
            "Keys are the same GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.",
        ],
    },

    # ---------------------------------------------------------------- insight
    "analytics": {
        "label": "Analytics",
        "cat": "Insight",
        "why": "see which pages and features people actually use",
        "env": ["PLAUSIBLE_DOMAIN", "PLAUSIBLE_API_KEY"],
        "deps": ["httpx>=0.27.0"],
        "module": "integrations/analytics.py",
        "signup": "https://plausible.io",
        "build": (
            "integrations/analytics.py exposes `async def track(event, props={})` and "
            "`async def stats(period='30d')`. With PLAUSIBLE_DOMAIN set it posts events to "
            "https://plausible.io/api/event and reads the Stats API with the bearer key. "
            "Without keys it records events into a local table and `stats` aggregates from "
            "there with plain SQL — which means the app's own admin dashboard has real numbers "
            "on day one. No cookies, no personal data, no consent banner needed; note that in "
            "a comment."
        ),
        "setup": [
            "Add your site at plausible.io and copy the domain exactly as entered.",
            "Paste it as PLAUSIBLE_DOMAIN.",
            "For the stats dashboard, create a key under Settings → API keys as PLAUSIBLE_API_KEY.",
        ],
    },
    "errors": {
        "label": "Error tracking (Sentry)",
        "cat": "Insight",
        "why": "find out when the app breaks for someone else",
        "env": ["SENTRY_DSN"],
        "deps": ["sentry-sdk>=2.0.0"],
        "module": "integrations/errors.py",
        "signup": "https://sentry.io",
        "build": (
            "integrations/errors.py calls sentry_sdk.init(dsn=SENTRY_DSN, "
            "traces_sample_rate=0.1, send_default_pii=False) when the DSN is present and is a "
            "no-op otherwise, so the import is always safe. Expose `capture(exc, **ctx)`. Add a "
            "FastAPI exception handler that captures, then returns a plain JSON error with a "
            "short reference id the person can quote — never a stack trace in the response."
        ),
        "setup": [
            "Create a project at sentry.io and pick the FastAPI platform.",
            "Copy the DSN it shows you.",
            "Paste it as SENTRY_DSN.",
        ],
    },
    "weather": {
        "label": "Weather",
        "cat": "Insight",
        "why": "the app shows forecasts or conditions",
        "env": ["OPENWEATHER_API_KEY"],
        "deps": ["httpx>=0.27.0"],
        "module": "integrations/weather.py",
        "signup": "https://openweathermap.org/api",
        "build": (
            "integrations/weather.py exposes `async def now(lat, lon)` and "
            "`async def forecast(lat, lon, days=5)`. With a key it uses OpenWeather's "
            "/data/2.5/weather and /data/2.5/forecast. Without one it uses Open-Meteo "
            "(https://api.open-meteo.com/v1/forecast), which needs no key at all — so weather "
            "works out of the box and the key only buys extra fields. Cache responses for 10 "
            "minutes per coordinate through integrations/cache.py. Normalise both providers "
            "into one shape so the frontend never branches."
        ),
        "setup": [
            "Weather already works with no key via Open-Meteo.",
            "For OpenWeather, sign up at openweathermap.org/api and copy the key.",
            "Paste it as OPENWEATHER_API_KEY. Keys take a few minutes to activate.",
        ],
    },

    # ---------------------------------------------------------------- shipping
    "deploy": {
        "label": "Deploy to a server",
        "cat": "Shipping",
        "why": "put the app on the internet under your own domain",
        "env": ["VERCEL_TOKEN", "RENDER_API_KEY"],
        "deps": [],
        "module": "integrations/deploy.md",
        "signup": "https://vercel.com/account/tokens",
        "build": (
            "integrations/deploy.md is documentation, not code. Write the exact steps to ship "
            "this specific project: the vercel.json needed to route every path to the FastAPI "
            "app, the build command, which env vars from .env must be re-entered in the host's "
            "dashboard (list them by name), and how to point a custom domain. Add a second "
            "section for Render using a render.yaml with the start command. Be concrete about "
            "this project's own file names rather than generic."
        ),
        "setup": [
            "For Vercel: create a token at vercel.com/account/tokens.",
            "For Render: create one at dashboard.render.com/u/settings/api-keys.",
            "Paste whichever you use. Both are optional — Forge can publish without them.",
        ],
    },
}

# What a prompt has to mention for each of these to be pulled in automatically.
TRIGGERS: dict[str, list[str]] = {
    "ai_claude": ["claude", "anthropic", "sonnet", "opus"],
    "ai_gemini": ["gemini", "google ai", "bard"],
    "ai_openai": ["chatgpt", "openai", "gpt-4", "gpt4", "gpt-5", "dall-e"],
    "ai_groq": ["groq", "llama", "fast inference"],
    "ai_openrouter": ["openrouter", "model picker", "choose a model", "switch model"],
    "ai_images": ["generate image", "image generation", "ai art", "text to image",
                  "make a picture", "avatar generator", "thumbnail generator"],
    "ai_speech": ["speech", "voice", "read aloud", "text to speech", "transcribe",
                  "transcription", "dictate", "audio note", "podcast"],
    "ai_vector": ["rag", "my documents", "knowledge base", "ask my", "semantic search",
                  "embedding", "chat with pdf", "chat with my"],
    "auth_github": ["github login", "sign in with github", "developer accounts", "github oauth"],
    "auth_phone": ["otp", "phone login", "mobile number login", "sms code", "verify phone"],
    "db_postgres": ["postgres", "postgresql", "neon", "sql database", "relational"],
    "db_mongo": ["mongo", "mongodb", "document database", "nosql"],
    # "queue" on its own catches an on-screen list of orders or tickets, which needs no Redis,
    # so the background-work sense has to be spelled out.
    "cache_redis": ["redis", "cache", "rate limit", "session store",
                    "job queue", "task queue", "background queue", "worker queue",
                    "background job", "background task"],
    "search_algolia": ["instant search", "algolia", "search as you type", "fuzzy search",
                       "typo tolerant", "autocomplete search"],
    "payments_razorpay": ["razorpay", "upi", "rupee", "inr", "indian payment", "netbanking",
                          "paytm", "phonepe"],
    "sms": ["sms", "text message", "whatsapp", "twilio"],
    "push": ["push notification", "notify my phone", "browser notification", "alert me"],
    "calendar": ["calendar", "google calendar", "schedule", "appointment", "booking",
                 "meeting", "availability"],
    "analytics": ["analytics", "track usage", "page views", "visitor", "dashboard of usage",
                  "metrics"],
    "errors": ["error tracking", "sentry", "crash report", "monitoring"],
    "weather": ["weather", "forecast", "temperature", "rain", "climate"],
    "deploy": ["deploy", "custom domain", "my own server", "hosting", "go live"],
}

# Every AI provider satisfies the same need, so wanting "an AI feature" should not demand
# all seven. These are ordered by how easy they are to get started with.
AI_KEYS = ["ai_openai", "ai_claude", "ai_gemini", "ai_groq", "ai_openrouter"]

# Group order in the connect form.
CATS = ["AI models", "Sign-in", "Data", "Money", "Reach", "Insight", "Shipping", "Other"]
