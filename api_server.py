#!/usr/bin/env python3
"""Forge — full-stack AI app generator backend.

Uses a local Ollama model (default: qwen2.5-coder:3b) to generate apps.
Set OLLAMA_URL / OLLAMA_MODEL env vars to point at your machine.
If Ollama is unreachable, a deterministic offline generator is used so the
product still works end-to-end.
"""
import asyncio
import base64
import io
import json
import os
import random
import re
import secrets
import sqlite3
import string
import time
import zipfile

import httpx

import engine
import catalog
import design
import verify as verifier
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response, HTMLResponse, PlainTextResponse, JSONResponse
from pydantic import BaseModel

# Generation engine. Preference: OpenRouter reasoning model -> local Ollama -> built-in.
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "openrouter/free")
# When a managed credential gateway is present, it holds the key and we never see it.
_GW = os.environ.get("CUSTOM_CRED_OPENROUTER_AI_URL", "").rstrip("/")
_GW_TOKEN = os.environ.get("CUSTOM_CRED_OPENROUTER_AI_PROXY_AUTH_KEY", "")
OPENROUTER_PROXY = bool(_GW and _GW_TOKEN)
OPENROUTER_URL = (_GW + "/api/v1/chat/completions") if OPENROUTER_PROXY \
    else "https://openrouter.ai/api/v1/chat/completions"
PUBLIC_BASE = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:3b")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "5463yr7y3")
# The database holds every account's sign-in code, so it must never sit inside the folder
# that gets served or bundled. Default to a private directory next to the project, and let
# FORGE_DB override it for deployments.
_HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("FORGE_DB") or os.path.join(
    os.path.dirname(_HERE), ".forge-data", "forge.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
# Carry an existing database over from the old in-project location on first run.
_OLD_DB = os.path.join(_HERE, "forge.db")
if os.path.exists(_OLD_DB) and not os.path.exists(DB_PATH):
    import shutil
    shutil.move(_OLD_DB, DB_PATH)

COST_CLARIFY = 1
COST_GENERATE = 2
COST_PUBLISH = 50          # putting an app on a public URL
RENEW_CREDITS = 30         # topped up automatically every month
RENEW_DAYS = 30
START_CREDITS = 10

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE NOT NULL,
  credits INTEGER NOT NULL DEFAULT 10,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS user_keys (
  user_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  val TEXT NOT NULL,
  at INTEGER NOT NULL,
  PRIMARY KEY (user_id, name)
);
CREATE TABLE IF NOT EXISTS devices (
  visitor_id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  last_seen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS gifts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE NOT NULL,
  credits INTEGER NOT NULL,
  note TEXT DEFAULT '',
  redeemed_by INTEGER,
  redeemed_at INTEGER,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  delta INTEGER NOT NULL,
  reason TEXT NOT NULL,
  at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  prompt TEXT NOT NULL,
  files TEXT NOT NULL,
  at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS secrets (
  name TEXT PRIMARY KEY,
  val TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publishes (
  slug TEXT PRIMARY KEY,
  project_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  summary TEXT DEFAULT '',
  html TEXT NOT NULL,
  views INTEGER NOT NULL DEFAULT 0,
  at INTEGER NOT NULL
);
"""
)
db.commit()

# Small forward migrations, so an existing forge.db keeps working.
db.executescript(
    """
CREATE TABLE IF NOT EXISTS votes (
  slug TEXT NOT NULL,
  user_id INTEGER NOT NULL,
  val INTEGER NOT NULL,
  at INTEGER NOT NULL,
  PRIMARY KEY (slug, user_id)
);
CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT NOT NULL,
  user_id INTEGER NOT NULL,
  body TEXT NOT NULL,
  at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS comments_slug ON comments (slug, at);
"""
)
db.commit()

_ucols = {r["name"] for r in db.execute("PRAGMA table_info(users)")}
if "handle" not in _ucols:
    db.execute("ALTER TABLE users ADD COLUMN handle TEXT DEFAULT ''")
    db.commit()

_pcols = {r["name"] for r in db.execute("PRAGMA table_info(publishes)")}
for _col, _decl in (("remix_of", "TEXT DEFAULT ''"),
                    ("remixes", "INTEGER NOT NULL DEFAULT 0")):
    if _col not in _pcols:
        db.execute(f"ALTER TABLE publishes ADD COLUMN {_col} {_decl}")
        db.commit()

if "remix_of" not in {r["name"] for r in db.execute("PRAGMA table_info(projects)")}:
    db.execute("ALTER TABLE projects ADD COLUMN remix_of TEXT DEFAULT ''")
    db.commit()

_pcols = {r["name"] for r in db.execute("PRAGMA table_info(publishes)")}
if "live_url" not in _pcols:
    db.execute("ALTER TABLE publishes ADD COLUMN live_url TEXT DEFAULT ''")
    db.commit()

_cols = {r["name"] for r in db.execute("PRAGMA table_info(projects)")}
if "integrations" not in _cols:
    db.execute("ALTER TABLE projects ADD COLUMN integrations TEXT DEFAULT '[]'")
    db.commit()

app = FastAPI()
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    expose_headers=["*"],
)

ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


# --- monthly credit renewal + per-user integration keys (added later, so migrate in place)
for _col, _decl in (("renew_at", "INTEGER"), ("last_renew", "INTEGER")):
    try:
        db.execute(f"ALTER TABLE users ADD COLUMN {_col} {_decl}")
    except Exception:
        pass
# Anyone who signed up before renewals existed gets their first top-up a month from now,
# not immediately — otherwise every existing account would be paid out on first load.
db.execute("UPDATE users SET renew_at=? WHERE renew_at IS NULL",
           (int(time.time()) + RENEW_DAYS * 86400,))
db.commit()


def now() -> int:
    return int(time.time())


def make_code(prefix: str, groups: int = 3) -> str:
    return prefix + "-" + "-".join(
        "".join(secrets.choice(ALPHABET) for _ in range(4)) for _ in range(groups)
    )


def ledger(uid: int, delta: int, reason: str):
    db.execute(
        "INSERT INTO ledger (user_id, delta, reason, at) VALUES (?,?,?,?)",
        (uid, delta, reason, now()),
    )


def user_public(row) -> dict:
    return {"code": row["code"], "credits": row["credits"]}


def find_user(code: str):
    return db.execute("SELECT * FROM users WHERE code=?", (code.strip().upper(),)).fetchone()


def vid(x_visitor_id, x_forge_vid=None):
    """Proxy-injected visitor id wins; fall back to the client's own id for local use."""
    return (x_visitor_id or x_forge_vid or "").split(",")[0].strip() or None


def auth(x_forge_code, x_visitor_id):
    """Resolve the current user from an access code, else from the remembered device."""
    row = None
    if x_forge_code:
        row = find_user(x_forge_code)
    if row is None and x_visitor_id:
        d = db.execute("SELECT user_id FROM devices WHERE visitor_id=?", (x_visitor_id,)).fetchone()
        if d:
            row = db.execute("SELECT * FROM users WHERE id=?", (d["user_id"],)).fetchone()
    if row is None:
        raise HTTPException(401, "Sign in required")
    if x_visitor_id:
        db.execute(
            "INSERT INTO devices (visitor_id, user_id, last_seen) VALUES (?,?,?) "
            "ON CONFLICT(visitor_id) DO UPDATE SET user_id=excluded.user_id, last_seen=excluded.last_seen",
            (x_visitor_id, row["id"], now()),
        )
        db.commit()
    return renew(row)


def spend(uid: int, amount: int, reason: str) -> int:
    row = db.execute("SELECT credits FROM users WHERE id=?", (uid,)).fetchone()
    if row["credits"] < amount:
        raise HTTPException(402, f"Not enough credits — this needs {amount}.")
    db.execute("UPDATE users SET credits=credits-? WHERE id=?", (amount, uid))
    ledger(uid, -amount, reason)
    db.commit()
    return row["credits"] - amount


# ---------------------------------------------------------------- renewal

def renew(u) -> dict:
    """Give every account RENEW_CREDITS once a month.

    Checked lazily whenever an account is loaded rather than on a timer, so it works the same
    whether the server has been up all month or just started. The due date moves forward by
    whole periods, so an account dormant for three months collects one top-up, not three —
    the credits are a monthly allowance, not a debt that accrues.
    """
    row = dict(u)
    due = row.get("renew_at") or 0
    t = now()
    if not due or t < due:
        return row
    nxt = due
    while nxt <= t:
        nxt += RENEW_DAYS * 86400
    db.execute("UPDATE users SET credits=credits+?, renew_at=?, last_renew=? WHERE id=?",
               (RENEW_CREDITS, nxt, t, row["id"]))
    ledger(row["id"], RENEW_CREDITS, "monthly renewal")
    db.commit()
    return dict(db.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone())


# ---------------------------------------------------------------- per-user integration keys

def user_keys(uid: int) -> dict:
    rows = db.execute("SELECT name, val FROM user_keys WHERE user_id=?", (uid,)).fetchall()
    return {r["name"]: r["val"] for r in rows}


def keys_for(uid: int) -> dict:
    """What a build can actually use: the admin's shared keys, with the person's own on top.

    A key someone connected themselves always wins over the shared one, so a person testing
    their own Stripe account never accidentally hits Forge's.
    """
    merged = dict(vault())
    merged.update({k: v for k, v in user_keys(uid).items() if v.strip()})
    return merged


def connected_ids(uid: int) -> list[str]:
    """Integrations this person has fully connected — every key present, none blank."""
    have = keys_for(uid)
    out = []
    for key, spec in engine.INTEGRATIONS.items():
        env = spec["env"]
        need = engine.req_env(key)
        if env and all(have.get(e, "").strip() for e in need):
            out.append(key)
    return out


def integ_rows(uid: int) -> list[dict]:
    """The whole catalog, annotated with what this person has connected."""
    mine = user_keys(uid)
    shared = vault()
    rows = []
    for key, spec in engine.INTEGRATIONS.items():
        if spec.get("legacy"):
            continue
        env = spec["env"]
        fields = [{"name": e,
                   "set_by_me": bool(mine.get(e, "").strip()),
                   "set_shared": bool(shared.get(e, "").strip()),
                   "optional": engine.is_optional_env(e)} for e in env]
        rows.append({
            "id": key,
            "label": spec["label"],
            "cat": engine.cat_of(key),
            "why": spec["why"],
            "module": spec["module"],
            "signup": spec.get("signup", ""),
            "setup": spec.get("setup", []),
            "fields": fields,
            # Nothing to connect (like realtime) counts as ready — it needs no account.
            "needs_keys": bool(env),
            "connected": (not env) or all(f["set_by_me"] or f["set_shared"]
                                          for f in fields if not f["optional"]),
            "mine": bool(env) and all(f["set_by_me"] for f in fields if not f["optional"]),
        })
    order = {c: i for i, c in enumerate(catalog.CATS)}
    rows.sort(key=lambda r: (order.get(r["cat"], 99), r["label"].lower()))
    return rows


class ConnectIn(BaseModel):
    id: str
    values: dict = {}


@app.get("/api/integrations/mine")
def my_integrations(x_visitor_id: str = Header(None), x_forge_vid: str = Header(None),
                    x_forge_code: str = Header(None)):
    u = auth(x_forge_code, vid(x_visitor_id, x_forge_vid))
    return {"integrations": integ_rows(u["id"]), "cats": catalog.CATS}


@app.post("/api/integrations/connect")
def connect_integration(body: ConnectIn, x_visitor_id: str = Header(None),
                        x_forge_vid: str = Header(None), x_forge_code: str = Header(None)):
    """Save one person's keys for one integration. Values are write-only — never read back."""
    u = auth(x_forge_code, vid(x_visitor_id, x_forge_vid))
    spec = engine.INTEGRATIONS.get(body.id)
    if not spec:
        raise HTTPException(404, "No such integration")
    allowed = set(spec["env"])
    saved = 0
    for name, val in body.values.items():
        name = str(name).strip().upper()
        if name not in allowed:
            continue
        val = str(val).strip()
        if val:
            db.execute("INSERT INTO user_keys (user_id, name, val, at) VALUES (?,?,?,?) "
                       "ON CONFLICT(user_id, name) DO UPDATE SET val=excluded.val, at=excluded.at",
                       (u["id"], name, val, now()))
            saved += 1
        else:
            db.execute("DELETE FROM user_keys WHERE user_id=? AND name=?", (u["id"], name))
    db.commit()
    rows = integ_rows(u["id"])
    me = next((r for r in rows if r["id"] == body.id), None)
    return {"ok": True, "saved": saved, "integration": me, "integrations": rows}


@app.post("/api/integrations/disconnect")
def disconnect_integration(body: ConnectIn, x_visitor_id: str = Header(None),
                           x_forge_vid: str = Header(None), x_forge_code: str = Header(None)):
    u = auth(x_forge_code, vid(x_visitor_id, x_forge_vid))
    spec = engine.INTEGRATIONS.get(body.id)
    if not spec:
        raise HTTPException(404, "No such integration")
    for e in spec["env"]:
        db.execute("DELETE FROM user_keys WHERE user_id=? AND name=?", (u["id"], e))
    db.commit()
    rows = integ_rows(u["id"])
    return {"ok": True, "integration": next((r for r in rows if r["id"] == body.id), None),
            "integrations": rows}


# ---------------------------------------------------------------- session/auth

@app.get("/api/me")
def me(x_visitor_id: str = Header(None), x_forge_vid: str = Header(None), x_forge_code: str = Header(None)):
    x_visitor_id = vid(x_visitor_id, x_forge_vid)
    try:
        row = auth(x_forge_code, x_visitor_id)
    except HTTPException:
        return {"signed_in": False, "engine": engine_state}
    return {"signed_in": True, "user": user_public(row), "engine": engine_state}


@app.post("/api/signup")
def signup(x_visitor_id: str = Header(None), x_forge_vid: str = Header(None)):
    x_visitor_id = vid(x_visitor_id, x_forge_vid)
    code = make_code("FRG")
    cur = db.execute(
        "INSERT INTO users (code, credits, created_at) VALUES (?,?,?)",
        (code, START_CREDITS, now()),
    )
    uid = cur.lastrowid
    ledger(uid, START_CREDITS, "signup bonus")
    if x_visitor_id:
        db.execute(
            "INSERT INTO devices (visitor_id, user_id, last_seen) VALUES (?,?,?) "
            "ON CONFLICT(visitor_id) DO UPDATE SET user_id=excluded.user_id, last_seen=excluded.last_seen",
            (x_visitor_id, uid, now()),
        )
    db.commit()
    return {"user": {"code": code, "credits": START_CREDITS}}


class LoginIn(BaseModel):
    code: str


@app.post("/api/login")
def login(body: LoginIn, x_visitor_id: str = Header(None), x_forge_vid: str = Header(None)):
    x_visitor_id = vid(x_visitor_id, x_forge_vid)
    row = find_user(body.code)
    if not row:
        raise HTTPException(404, "That code doesn't exist. Check it or sign up.")
    if x_visitor_id:
        db.execute(
            "INSERT INTO devices (visitor_id, user_id, last_seen) VALUES (?,?,?) "
            "ON CONFLICT(visitor_id) DO UPDATE SET user_id=excluded.user_id, last_seen=excluded.last_seen",
            (x_visitor_id, row["id"], now()),
        )
        db.commit()
    return {"user": user_public(row)}


@app.post("/api/logout")
def logout(x_visitor_id: str = Header(None), x_forge_vid: str = Header(None)):
    x_visitor_id = vid(x_visitor_id, x_forge_vid)
    if x_visitor_id:
        db.execute("DELETE FROM devices WHERE visitor_id=?", (x_visitor_id,))
        db.commit()
    return {"ok": True}


class RedeemIn(BaseModel):
    code: str


@app.post("/api/redeem")
def redeem(body: RedeemIn, x_visitor_id: str = Header(None), x_forge_vid: str = Header(None), x_forge_code: str = Header(None)):
    x_visitor_id = vid(x_visitor_id, x_forge_vid)
    u = auth(x_forge_code, x_visitor_id)
    g = db.execute("SELECT * FROM gifts WHERE code=?", (body.code.strip().upper(),)).fetchone()
    if not g:
        raise HTTPException(404, "Invalid gift code.")
    if g["redeemed_by"] is not None:
        raise HTTPException(409, "This gift code has already been used.")
    already = db.execute(
        "SELECT 1 FROM gifts WHERE redeemed_by=? AND note=?", (u["id"], g["note"])
    ).fetchone()
    if already and g["note"]:
        raise HTTPException(409, "You already redeemed a code from this batch.")
    db.execute("UPDATE gifts SET redeemed_by=?, redeemed_at=? WHERE id=?", (u["id"], now(), g["id"]))
    db.execute("UPDATE users SET credits=credits+? WHERE id=?", (g["credits"], u["id"]))
    ledger(u["id"], g["credits"], f"gift {g['code']}")
    db.commit()
    row = db.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
    return {"user": user_public(row), "added": g["credits"]}


@app.get("/api/history")
def history(x_visitor_id: str = Header(None), x_forge_vid: str = Header(None), x_forge_code: str = Header(None)):
    x_visitor_id = vid(x_visitor_id, x_forge_vid)
    u = auth(x_forge_code, x_visitor_id)
    rows = db.execute(
        "SELECT delta, reason, at FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 30", (u["id"],)
    ).fetchall()
    projs = db.execute(
        "SELECT id, title, at FROM projects WHERE user_id=? ORDER BY id DESC LIMIT 12", (u["id"],)
    ).fetchall()
    return {
        "ledger": [dict(r) for r in rows],
        "projects": [dict(p) for p in projs],
    }


# ---------------------------------------------------------------- model engine

engine_state = {"ok": False, "model": OLLAMA_MODEL, "url": OLLAMA_URL, "mode": "offline",
                "reasoning": False}

# The credential proxy terminates TLS itself, so verification is delegated to it.
OR_VERIFY = not OPENROUTER_PROXY


def or_headers():
    h = {"Content-Type": "application/json", "X-Title": "Forge"}
    if OPENROUTER_PROXY:
        h["x-api-key"] = _GW_TOKEN
    elif OPENROUTER_KEY:
        h["Authorization"] = "Bearer " + OPENROUTER_KEY
    return h


def or_available() -> bool:
    return bool(OPENROUTER_KEY or OPENROUTER_PROXY)


async def probe_engine():
    # Perplexity's built-in models first — no API key to manage, no daily free cap.
    try:
        info = await engine.pplx_probe()
        engine_state.update(ok=True, mode="pplx", reasoning=True,
                            model=info["model"], url="perplexity built-in",
                            routed_to="", error=None)
        return engine_state
    except Exception as e:
        engine_state["pplx_error"] = f"{type(e).__name__}: {e}"[:400]
    if or_available():
        try:
            async with httpx.AsyncClient(timeout=25, verify=OR_VERIFY) as c:
                r = await c.post(OPENROUTER_URL, headers=or_headers(), json={
                    "model": OPENROUTER_MODEL,
                    "messages": [{"role": "user", "content": "ok"}],
                    "max_tokens": 4,
                })
            d = r.json()
            if r.status_code == 429 or (d.get("error") or {}).get("code") == 429:
                # Key works, the free daily quota is spent. Say so plainly rather than
                # pretending the engine is missing — it comes back when the quota resets.
                engine_state.update(ok=False, mode="rate_limited", reasoning=False,
                                    model=OPENROUTER_MODEL, url="openrouter.ai",
                                    routed_to="",
                                    error="OpenRouter free daily limit reached — the "
                                          "reasoning engine returns when the quota resets.")
                return engine_state
            if "error" in d:
                raise RuntimeError(str(d["error"])[:120])
            engine_state.update(ok=True, mode="openrouter", reasoning=True,
                                model=OPENROUTER_MODEL, url="openrouter.ai",
                                routed_to=d.get("model", ""), error=None)
            return engine_state
        except Exception as e:
            engine_state.update(error="openrouter: " + str(e)[:110])
    try:
        async with httpx.AsyncClient(timeout=4) as c:
            r = await c.get(f"{OLLAMA_URL}/api/tags")
            names = [m["name"] for m in r.json().get("models", [])]
        engine_state.update(ok=True, mode="ollama", reasoning=False,
                            model=OLLAMA_MODEL, url=OLLAMA_URL, models=names, error=None)
    except Exception as e:
        engine_state.update(ok=False, mode="offline", reasoning=False,
                            model=OLLAMA_MODEL, url=OLLAMA_URL, error=str(e)[:120])
    return engine_state


async def or_stream(messages, effort="high", max_tokens=8000):
    """Stream ('reason'|'text', chunk) pairs from an OpenRouter reasoning model."""
    payload = {
        "model": OPENROUTER_MODEL, "messages": messages, "stream": True,
        "max_tokens": max_tokens, "temperature": 0.25,
    }
    if effort:
        payload["reasoning"] = {"effort": effort}
    async with httpx.AsyncClient(timeout=900, verify=OR_VERIFY) as c:
        async with c.stream("POST", OPENROUTER_URL, headers=or_headers(), json=payload) as r:
            if r.status_code >= 400:
                body = (await r.aread()).decode()[:200]
                raise RuntimeError(f"HTTP {r.status_code}: {body}")
            async for line in r.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    return
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                if obj.get("error"):
                    raise RuntimeError(str(obj["error"])[:160])
                for ch in obj.get("choices", []):
                    d = ch.get("delta") or {}
                    rt = d.get("reasoning") or (d.get("reasoning_content") if isinstance(d.get("reasoning_content"), str) else None)
                    if rt:
                        yield "reason", rt
                    if d.get("content"):
                        yield "text", d["content"]


async def or_stream_retry(messages, effort="high", max_tokens=8000,
                          require_text=False, min_text=0, want_html=False):
    """`openrouter/free` routes to a random free model each call, and some reject the
    request shape, spend the whole budget on reasoning, or emit a few useless tokens.
    Retry with progressively simpler payloads until the output looks usable. Retrying is
    only allowed before we have committed real output, and a `reset` signal tells the
    client to clear anything already streamed from a discarded attempt."""
    variants = [
        {"effort": effort, "max_tokens": max_tokens},
        {"effort": effort, "max_tokens": min(max_tokens, 8000)},
        {"effort": None, "max_tokens": min(max_tokens, 8000)},
        {"effort": None, "max_tokens": min(max_tokens, 4000)},
    ]
    last = "no attempt made"
    for i, v in enumerate(variants):
        committed = False
        text = []
        try:
            async for kind, chunk in or_stream(messages, v["effort"], v["max_tokens"]):
                if kind == "text":
                    text.append(chunk)
                    if sum(len(x) for x in text) >= min_text:
                        committed = True
                elif not require_text:
                    committed = True
                yield kind, chunk
            body = "".join(text)
            if not require_text:
                return
            if len(body.strip()) < max(min_text, 1):
                last = f"model returned only {len(body.strip())} characters"
            elif want_html and "<" not in body:
                last = "model returned no markup"
            else:
                return
        except Exception as e:
            last = str(e)
            if committed:
                return
        if i + 1 < len(variants):
            yield "reset", ""
            yield "retry", f"attempt {i + 1} gave nothing usable ({str(last)[:70]}) — retrying"
            await asyncio.sleep(1.0 + i)
    raise RuntimeError(f"all attempts failed: {last}")


async def or_once(messages, effort="high", max_tokens=1200) -> str:
    out = []
    async for kind, chunk in or_stream_retry(messages, effort, max_tokens,
                                            require_text=True, min_text=20):
        if kind == "reset":
            out.clear()
        elif kind == "text":
            out.append(chunk)
    return "".join(out)


@app.on_event("startup")
async def _startup():
    await probe_engine()


@app.get("/api/engine")
async def engine_info():
    return await probe_engine()


async def ollama_stream(prompt: str, system: str):
    """Yield text chunks from Ollama."""
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "system": system,
        "stream": True,
        "options": {"temperature": 0.3, "num_ctx": 4096},
    }
    async with httpx.AsyncClient(timeout=600) as c:
        async with c.stream("POST", f"{OLLAMA_URL}/api/generate", json=payload) as r:
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("response"):
                    yield obj["response"]
                if obj.get("done"):
                    return


async def ollama_once(prompt: str, system: str, timeout=180) -> str:
    payload = {
        "model": OLLAMA_MODEL, "prompt": prompt, "system": system, "stream": False,
        "options": {"temperature": 0.2},
    }
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(f"{OLLAMA_URL}/api/generate", json=payload)
        r.raise_for_status()
        return r.json().get("response", "")


# ---------------------------------------------------------------- clarify step

GENERIC_QUESTIONS = [
    {"q": "What is the main thing a user should be able to do on the first screen?",
     "options": ["Browse a list", "Create something new", "See a dashboard", "Search"]},
    {"q": "Pick a visual style.",
     "options": ["Minimal white", "Dark and technical", "Warm and friendly", "Bold and colourful"]},
    {"q": "Should data persist between visits?",
     "options": ["Yes, save locally", "No, session only"]},
    {"q": "Who is this for?",
     "options": ["Just me", "A small team", "Public users"]},
    {"q": "Which extra do you want most?",
     "options": ["Search and filters", "Charts and stats", "Accounts and login", "Keep it lean"]},
]


class ClarifyIn(BaseModel):
    prompt: str
    attachments: list[dict] = []


def parse_questions(text: str):
    out = []
    # try fenced or raw JSON
    m = re.search(r"\[[\s\S]*\]", text)
    if m:
        try:
            data = json.loads(m.group(0))
            for item in data:
                if isinstance(item, dict) and item.get("q"):
                    opts = item.get("options") or []
                    out.append({"q": str(item["q"])[:240],
                                # The planner writes real, descriptive options ("decrement stock
                                # only on a successful webhook"), and the buttons wrap, so they
                                # get room — trimmed on a word boundary so nothing ends mid-word.
                                "options": [clip(str(o), 110) for o in opts][:4],
                                "why": clip(str(item.get("why") or ""), 160)})
        except Exception:
            pass
    return out[:5]


# ---------------------------------------------------------------- planning

# Named apart from engine.SYS_PLAN, which is the deeper plan used during the build itself.
# This one only has to produce something a person can read and react to in a few seconds.
SYS_REASON = (
    "You are a senior engineer planning a small full-stack app before any code is written.\n\n"
    "The app WILL be built with this exact file layout, so describe that stack and no other. "
    "Never propose Node, Express, React or a different set of files:\n" + engine.STACK + "\n\n"
    "Think hard about what this app actually needs, then reply in EXACTLY two parts.\n\n"
    "PART 1 — the plan. Plain lines, no markdown, no bullets, at most 9 lines. Each line is "
    "'KEY: value' using only these keys, in this order:\n"
    "SUMMARY: one sentence describing what gets built.\n"
    "SCREEN: one per screen, naming it and what a person does there. Two to four SCREEN lines.\n"
    "DATA: the tables or collections and their key columns.\n"
    "STACK: the files you will write and what each is for.\n"
    "RISK: the one thing most likely to go wrong, and how you will avoid it.\n\n"
    "PART 2 — the open questions. A line containing only ---, then ONLY a JSON array of 3 to 5 "
    'objects: [{"q":"question","options":["a","b","c"],"why":"what this changes"}]. '
    "Ask only about decisions you genuinely cannot infer and that change the code. Never ask "
    "about styling preferences, colours, or the tech stack. Each question must be answerable by "
    "picking one option. No prose after the JSON."
)


def clip(text: str, n: int) -> str:
    """Shorten to n characters without cutting a word in half."""
    t = " ".join(text.split())
    if len(t) <= n:
        return t
    cut = t[:n].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return (cut or t[:n].rstrip()) + "\u2026"


def parse_plan(raw: str) -> tuple[list[dict], list[dict]]:
    """Split the model's reply into plan lines and questions.

    Tolerant by design: the plan renders from whatever lines parsed, and questions fall back
    to the generic set, so a model that drifts from the format still produces a usable step.
    """
    head, _, tail = raw.partition("---")
    lines = []
    for ln in head.splitlines():
        ln = ln.strip().lstrip("-*# ").strip()
        if not ln or ":" not in ln:
            continue
        key, _, val = ln.partition(":")
        key = key.strip().upper()
        if key in ("SUMMARY", "SCREEN", "DATA", "STACK", "RISK") and val.strip():
            lines.append({"k": key, "v": val.strip()[:240]})
    return lines[:9], parse_questions(tail or raw)


class PlanIn(BaseModel):
    prompt: str
    attachments: list[dict] = []


@app.post("/api/plan")
async def plan(body: PlanIn, x_visitor_id: str = Header(None),
               x_forge_vid: str = Header(None), x_forge_code: str = Header(None)):
    """Reason about the idea, then hand back a plan, the integrations needed, and questions.

    Streamed as SSE so the thinking is visible while it happens instead of the person staring
    at a spinner for half a minute. Costs the same single credit the old questions step did.
    """
    x_visitor_id = vid(x_visitor_id, x_forge_vid)
    u = auth(x_forge_code, x_visitor_id)
    if not body.prompt.strip():
        raise HTTPException(400, "Write a prompt first.")
    credits = spend(u["id"], COST_CLARIFY, "planning the app")
    uid = u["id"]
    prompt = body.prompt.strip()[:6000]
    if body.attachments:
        names = ", ".join(str(a.get("name", "file"))[:40] for a in body.attachments[:4])
        prompt += f"\n\nThe person attached: {names}. Account for them in the plan."

    async def run():
        yield sse("start", {"credits": credits, "engine": engine_state.get("mode")})
        raw = ""
        try:
            mode = engine_state.get("mode")
            if mode == "pplx":
                async for kind, chunk in engine.pplx_stream(
                        SYS_REASON, f"App idea: {prompt}", max_tokens=3000, think=4000):
                    raw += chunk if kind == "text" else ""
                    if kind == "reason":
                        yield sse("reason", {"t": chunk})
                    else:
                        # The plan lines arrive in order, so surface each one as it completes
                        # rather than waiting for the whole reply.
                        yield sse("tick", {"t": chunk})
            elif mode == "openrouter":
                raw = await or_once(
                    [{"role": "system", "content": SYS_REASON},
                     {"role": "user", "content": f"App idea: {prompt}"}],
                    effort="high", max_tokens=2600)
            elif mode == "ollama":
                raw = await ollama_once(f"App idea: {prompt}", SYS_REASON)
        except Exception as e:
            yield sse("warn", {"m": f"Planner fell back to defaults: {e}"[:200]})

        lines, qs = parse_plan(raw)
        if not lines:
            lines = [{"k": "SUMMARY", "v": f"A working app for: {prompt[:160]}"},
                     {"k": "STACK", "v": "server.py, index.html, styles.css, app.js"}]
        if not qs:
            qs = [dict(x) for x in GENERIC_QUESTIONS[:4]]

        plan_text = " ".join(l["v"] for l in lines)
        have = set(connected_ids(uid))
        ids = engine.detect_integrations(prompt, None, plan_text, connected=have)
        rows = [r for r in integ_rows(uid) if r["id"] in ids]
        yield sse("plan", {"lines": lines})
        yield sse("integrations", {"needed": rows,
                                   "missing": [r["id"] for r in rows if not r["connected"]]})
        yield sse("questions", {"questions": qs,
                                "title": " ".join(prompt.split()[:6])[:48] or "Untitled app"})
        yield sse("done", {"credits": credits})

    return StreamingResponse(run(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/clarify")
async def clarify(body: ClarifyIn, x_visitor_id: str = Header(None), x_forge_vid: str = Header(None), x_forge_code: str = Header(None)):
    x_visitor_id = vid(x_visitor_id, x_forge_vid)
    u = auth(x_forge_code, x_visitor_id)
    if not body.prompt.strip():
        raise HTTPException(400, "Write a prompt first.")
    credits = spend(u["id"], COST_CLARIFY, "clarifying questions")
    qs = []
    sys = ("You are a product analyst. Given an app idea, reply with ONLY a JSON array of 4 "
           'objects: [{"q":"question","options":["a","b","c"]}]. Questions must be specific to '
           "the idea, short, and answerable by picking one option. No prose.")
    if engine_state.get("mode") == "pplx":
        try:
            raw = ""
            async for kind, chunk in engine.pplx_stream(sys, f"App idea: {body.prompt}",
                                                        max_tokens=1200):
                if kind == "text":
                    raw += chunk
            qs = parse_questions(raw)
        except Exception:
            qs = []
    elif engine_state.get("mode") == "openrouter":
        try:
            raw = await or_once([{"role": "system", "content": sys},
                                 {"role": "user", "content": f"App idea: {body.prompt}"}],
                                effort="medium", max_tokens=900)
            qs = parse_questions(raw)
        except Exception:
            qs = []
    elif engine_state.get("mode") == "ollama":
        try:
            raw = await ollama_once(f"App idea: {body.prompt}", sys)
            qs = parse_questions(raw)
        except Exception:
            qs = []
    if not qs:
        qs = [dict(x) for x in GENERIC_QUESTIONS[:4]]
    title = " ".join(body.prompt.split()[:6])[:48] or "Untitled app"
    return {"questions": qs, "title": title, "credits": credits,
            "engine": engine_state.get("mode")}


# ---------------------------------------------------------------- generation

class GenIn(BaseModel):
    model: str = design.DEFAULT_MODEL
    resume_project: int | None = None
    prompt: str
    answers: list[dict] = []
    attachments: list[dict] = []
    title: str = "app"


def vault() -> dict:
    """Keys the admin saved. These get written into every generated project's .env."""
    rows = db.execute("SELECT name, val FROM secrets").fetchall()
    return {r["name"]: r["val"] for r in rows}


def vault_flags() -> dict:
    return {k: bool(v.strip()) for k, v in vault().items()}


def apply_integrations(files: dict, ids: list[str], _env_values: dict | None = None) -> dict:
    """Attach .env files and make sure the integration packages are in requirements.txt."""
    if not ids:
        return {}
    added = engine.env_files(ids, _env_values if _env_values is not None else vault())
    # Deterministic, not left to the model: the project must actually read its .env.
    for path in engine.ensure_env_loading(files):
        added[path] = files[path]
    deps = engine.integration_deps(ids)
    req = files.get("requirements.txt")
    if req is not None and deps:
        have = req.lower()
        missing = [d for d in deps if d.split(">=")[0].split("==")[0].lower() not in have]
        if missing:
            added["requirements.txt"] = req.rstrip() + "\n" + "\n".join(missing) + "\n"
    files.update(added)
    return added


# --------------------------------------------------------------------------- metering

class Exhausted(Exception):
    """Raised mid-build when the user's credits run out. The partial work is kept."""

    def __init__(self, written: int, spent: int):
        self.written, self.spent = written, spent


class Meter:
    """Charges per generated file, at the chosen model's rate.

    Billing happens as each file closes, so a build that stops halfway has only
    charged for the files that were actually written.
    """

    def __init__(self, uid: int, model: str, title: str):
        self.uid = uid
        self.rate = design.model_info(model)["per_file"]
        self.title = title[:30]
        self.files = 0
        self.spent = 0

    def balance(self) -> int:
        row = db.execute("SELECT credits FROM users WHERE id=?", (self.uid,)).fetchone()
        return row["credits"] if row else 0

    def charge(self, path: str) -> int:
        """Bill one file. Raises Exhausted if the balance will not cover it."""
        if self.balance() < self.rate:
            raise Exhausted(self.files, self.spent)
        db.execute("UPDATE users SET credits=credits-? WHERE id=?", (self.rate, self.uid))
        ledger(self.uid, -self.rate, f"file {path} ({self.title})")
        db.commit()
        self.files += 1
        self.spent += self.rate
        return self.balance()


def save_project(uid, title, prompt, files, integ, existing=None) -> int:
    """Insert or update a project row, returning its id."""
    if existing:
        db.execute("UPDATE projects SET files=?, integrations=?, at=? WHERE id=? AND user_id=?",
                   (json.dumps(files), json.dumps(integ), now(), existing, uid))
        db.commit()
        return existing
    cur = db.execute(
        "INSERT INTO projects (user_id, title, prompt, files, at, integrations)"
        " VALUES (?,?,?,?,?,?)",
        (uid, title, prompt, json.dumps(files), now(), json.dumps(integ)),
    )
    db.commit()
    return cur.lastrowid


@app.get("/api/models")
def models_list(x_visitor_id: str = Header(None), x_forge_vid: str = Header(None),
                x_forge_code: str = Header(None)):
    """The pickable models and what each one charges per generated file."""
    bal = None
    try:
        u = auth(x_forge_code, vid(x_visitor_id, x_forge_vid))
        bal = u["credits"]
    except Exception:
        pass
    return {"models": design.public_models(), "default": design.DEFAULT_MODEL,
            "credits": bal, "per_file_note": "charged as each file is written"}


@app.get("/api/integrations")
def integrations_catalog():
    """What Forge can wire up, and which keys it already holds. Values never leave the server."""
    flags = vault_flags()
    out = []
    for spec in engine.public_catalog():
        keys = [{"name": e, "configured": flags.get(e, False)} for e in spec["env"]]
        out.append({**spec, "keys": keys,
                    "ready": all(k["configured"] for k in keys) if keys else True,
                    "needs_keys": bool(spec["env"])})
    return {"integrations": out}


@app.post("/api/generate")
async def generate(body: GenIn, x_visitor_id: str = Header(None), x_forge_vid: str = Header(None), x_forge_code: str = Header(None)):
    x_visitor_id = vid(x_visitor_id, x_forge_vid)
    u = auth(x_forge_code, x_visitor_id)
    uid = u["id"]
    mid = body.model if body.model in design.MODELS else design.DEFAULT_MODEL
    minfo = design.model_info(mid)
    meter = Meter(uid, mid, body.title or "App")
    # Files are billed one by one as they are written, so nothing is charged up front.
    if meter.balance() < meter.rate:
        raise HTTPException(402, f"{minfo['label']} costs {meter.rate} credits per file "
                                 f"and you have {meter.balance()}. Redeem a gift code to top up.")
    credits = meter.balance()
    done_files = {}
    if body.resume_project:
        row = db.execute("SELECT files FROM projects WHERE id=? AND user_id=?",
                         (body.resume_project, uid)).fetchone()
        if row:
            done_files = json.loads(row["files"] or "{}")
    answers = "\n".join(f"{a.get('q','')} -> {a.get('a','')}" for a in body.answers
                        if str(a.get("a", "")).strip() not in ("", "no preference")) or "(none given)"
    notes = ""
    for a in body.attachments[:3]:
        notes += f"\nAttached {a.get('kind','file')} '{a.get('name','')}': {str(a.get('text',''))[:1200]}"
    user_prompt = (f"App idea:\n{body.prompt}\n\nUser decisions:\n{answers}{notes}\n\n"
                   "Build the full project now.")
    if done_files:
        already = "\n".join(f"- {k} ({len(v)} chars)" for k, v in done_files.items())
        user_prompt += (
            "\n\nThis build was interrupted when the user ran out of credits. These files are "
            "already written and must NOT be emitted again:\n" + already +
            "\n\nEmit only the files that are still missing, consistent with the ones above. "
            "Here they are for reference:\n" +
            "\n".join(f"<<<FILE {k}>>>\n{v}\n<<<ENDFILE>>>" for k, v in done_files.items()))

    _env_values = keys_for(u["id"])
    integ = engine.detect_integrations(body.prompt, body.answers,
                                       connected=set(connected_ids(u["id"])))
    # A key the person connected counts as configured, so the model writes the real path
    # rather than the offline fallback for anything they have actually set up.
    flags = {k: bool(v.strip()) for k, v in _env_values.items()}

    def file_event(ev, path, chunk):
        """Files are billed at the moment they finish, not before."""
        if ev == "open":
            return sse("file_open", {"path": path})
        if ev == "body":
            return sse("chunk", {"path": path, "t": chunk})
        left = meter.charge(path)          # raises Exhausted when the balance runs dry
        return sse("file_close", {"path": path, "cost": meter.rate, "credits": left})

    def close_ev(path, bill=True):
        """Close a file emitted outside the model stream.

        Forge's own scaffolding (the .env loader, package markers, .env files) is free —
        the user is only charged for files a model actually wrote.
        """
        if not bill:
            return sse("file_close", {"path": path, "cost": 0,
                                      "credits": meter.balance(), "free": True})
        left = meter.charge(path)
        return sse("file_close", {"path": path, "cost": meter.rate, "credits": left})

    holder = {}

    async def build():
        mode = engine_state.get("mode")
        yield sse("status", {"msg": f"engine: {mode} ({engine_state.get('model')})",
                             "credits": credits, "mode": mode,
                             "reasoning": bool(engine_state.get("reasoning"))})
        if integ:
            yield sse("integrations", {"ids": integ, "items": [
                {"id": i, "label": engine.INTEGRATIONS[i]["label"],
                 "why": engine.INTEGRATIONS[i]["why"],
                 "module": engine.INTEGRATIONS[i]["module"],
                 "needs_keys": bool(engine.INTEGRATIONS[i]["env"]),
                 "ready": all(flags.get(e) for e in engine.INTEGRATIONS[i]["env"])
                          if engine.INTEGRATIONS[i]["env"] else True}
                for i in integ]})
        parser = holder['p'] = engine.ProjectParser()
        plan = ""

        if mode == "pplx":
            # Phase 1 — architecture, data model and endpoints, before a line of code.
            yield sse("phase", {"n": 1, "label": "Thinking through the architecture"})
            try:
                async for kind, chunk in engine.pplx_stream(
                        engine.SYS_PLAN + engine.integration_brief(integ, flags),
                        user_prompt, max_tokens=3500,
                        think=minfo["think"], model=mid):
                    if kind == "reason":
                        yield sse("think", {"t": chunk})
                    else:
                        plan += chunk
                        yield sse("plan", {"t": chunk})
            except Exhausted:
                raise          # out of credits is not a model failure
            except Exception as e:
                yield sse("status", {"msg": f"planning skipped ({str(e)[:70]})"})
                plan = ""

            # Phase 2 — write every file of the project against that plan.
            yield sse("phase", {"n": 2, "label": "Writing the project"})
            build_user = user_prompt + (
                f"\n\nFollow this build plan exactly:\n{plan}" if plan.strip() else "")
            try:
                async for kind, chunk in engine.pplx_stream(
                        engine.SYS_BUILD + engine.integration_brief(integ, flags),
                        build_user, max_tokens=minfo["out"],
                        think=minfo["build_think"], model=mid):
                    if kind == "reason":
                        yield sse("think", {"t": chunk})
                        continue
                    for ev, path, body_ in parser.feed(chunk):
                        yield file_event(ev, path, body_)
                for ev, path, body_ in parser.finish():
                    yield file_event(ev, path, body_)
            except Exhausted:
                raise          # out of credits is not a model failure
            except Exception as e:
                yield sse("status", {"msg": f"model error, falling back ({str(e)[:70]})"})
                parser = holder['p'] = engine.ProjectParser()
                await probe_engine()

        elif mode == "openrouter":
            yield sse("phase", {"n": 1, "label": "Thinking through the architecture"})
            try:
                async for kind, chunk in or_stream_retry(
                        [{"role": "system", "content": engine.SYS_PLAN + engine.integration_brief(integ, flags)},
                         {"role": "user", "content": user_prompt}],
                        effort="high", max_tokens=2000, require_text=True, min_text=80):
                    if kind == "reset":
                        plan = ""
                        yield sse("reset", {"target": "plan"})
                    elif kind == "retry":
                        yield sse("status", {"msg": chunk})
                    elif kind == "reason":
                        yield sse("think", {"t": chunk})
                    else:
                        plan += chunk
                        yield sse("plan", {"t": chunk})
            except Exhausted:
                raise          # out of credits is not a model failure
            except Exception as e:
                yield sse("status", {"msg": f"planning skipped ({str(e)[:70]})"})
                plan = ""
            yield sse("phase", {"n": 2, "label": "Writing the project"})
            build_user = user_prompt + (
                f"\n\nFollow this build plan exactly:\n{plan}" if plan.strip() else "")
            try:
                async for kind, chunk in or_stream_retry(
                        [{"role": "system", "content": engine.SYS_BUILD + engine.integration_brief(integ, flags)},
                         {"role": "user", "content": build_user}],
                        effort=None, max_tokens=12000, require_text=True, min_text=500):
                    if kind == "reset":
                        parser = holder['p'] = engine.ProjectParser()
                        yield sse("reset", {"target": "code"})
                    elif kind == "retry":
                        yield sse("status", {"msg": chunk})
                    elif kind == "reason":
                        yield sse("think", {"t": chunk})
                    else:
                        for ev, path, body_ in parser.feed(chunk):
                            yield file_event(ev, path, body_)
                for ev, path, body_ in parser.finish():
                    yield file_event(ev, path, body_)
            except Exhausted:
                raise          # out of credits is not a model failure
            except Exception as e:
                yield sse("status", {"msg": f"model error, falling back ({str(e)[:70]})"})
                parser = holder['p'] = engine.ProjectParser()
                await probe_engine()

        elif mode == "ollama":
            yield sse("phase", {"n": 2, "label": "Writing the project"})
            try:
                async for chunk in ollama_stream(
                        user_prompt, engine.SYS_BUILD + engine.integration_brief(integ, flags)):
                    for ev, path, body_ in parser.feed(chunk):
                        yield file_event(ev, path, body_)
                for ev, path, body_ in parser.finish():
                    yield file_event(ev, path, body_)
            except Exhausted:
                raise          # out of credits is not a model failure
            except Exception as e:
                yield sse("status", {"msg": f"model error, using built-in generator ({str(e)[:60]})"})
                parser = holder['p'] = engine.ProjectParser()

        files = dict(done_files)
        files.update(parser.files())
        if (not files or not engine.find_frontend(files)) and engine_state.get("mode") == "pplx":
            # A model can occasionally spend its whole turn reasoning and emit nothing.
            # One retry with thinking off recovers it instead of dropping to the
            # built-in generator.
            yield sse("status", {"msg": "the model returned no code — retrying once"})
            yield sse("reset", {"target": "code"})
            parser = holder['p'] = engine.ProjectParser()
            try:
                async for kind, chunk in engine.pplx_stream(
                        engine.SYS_BUILD + engine.integration_brief(integ, flags),
                        build_user, max_tokens=minfo["out"], think=0, model=mid):
                    if kind != "text":
                        continue
                    for ev, path, body_ in parser.feed(chunk):
                        yield file_event(ev, path, body_)
                for ev, path, body_ in parser.finish():
                    yield file_event(ev, path, body_)
            except Exhausted:
                raise          # out of credits is not a model failure
            except Exception as e:
                yield sse("status", {"msg": f"retry failed ({str(e)[:60]})"})
            files = dict(done_files)
            files.update(parser.files())

        if not files or not engine.find_frontend(files):
            if engine_state.get("mode") == "rate_limited":
                why = ("OpenRouter's free daily limit is reached, so this project came from "
                       "the built-in generator.")
            else:
                why = ("The model did not return a usable project, so this one came from the "
                       "built-in generator.")
            yield sse("fallback", {"msg": why})
            yield sse("reset", {"target": "code"})
            yield sse("phase", {"n": 2, "label": "Writing the project"})
            files = engine.offline_project(body.prompt, body.answers, body.title or "App")
            holder["extra"] = {}
            for path, content in files.items():
                yield sse("file_open", {"path": path})
                for i in range(0, len(content), 500):
                    yield sse("chunk", {"path": path, "t": content[i:i + 500]})
                    await asyncio.sleep(0.004)
                holder["extra"][path] = content
                yield close_ev(path)

        # Wire the integrations: .env.example, a pre-filled .env, and the extra packages.
        for path, content in apply_integrations(files, integ, _env_values).items():
            yield sse("file_open", {"path": path})
            yield sse("chunk", {"path": path, "t": content})
            holder.setdefault("extra", {})[path] = content
            yield close_ev(path, bill=False)

        yield sse("phase", {"n": 3, "label": "Checking every file"})
        issues = verifier.verify(files)
        yield sse("phase", {"n": 4, "label": "Rendering preview"})
        preview = engine.preview_doc(files)
        pid = save_project(uid, body.title or "App", body.prompt, files, integ,
                           body.resume_project)
        if issues:
            yield sse("issues", {"project_id": pid, "count": len(issues),
                                 "issues": issues})
        yield sse("done", {"project_id": pid, "files": files,
                           "preview": preview, "entry": engine.find_frontend(files),
                           "credits": meter.balance(), "integrations": integ,
                           "billed": {"files": meter.files, "rate": meter.rate,
                                      "spent": meter.spent, "model": mid},
                           "issues": issues})

    async def stream():
        """Run the build, and if the credits run out mid-file, stop cleanly.

        Whatever was already written is saved as a project, so the user can redeem a
        gift code and resume instead of paying for the whole thing again.
        """
        try:
            async for ev in build():
                yield ev
        except Exhausted as ex:
            part = dict(done_files)
            pr = holder.get('p')
            if pr:
                part.update(pr.files())
            part.update(holder.get("extra", {}))
            pid = save_project(uid, body.title or "App", body.prompt, part, integ,
                               body.resume_project)
            written = sorted(part)
            yield sse("exhausted", {
                "project_id": pid,
                "files_written": written,
                "count": len(written),
                "spent": ex.spent,
                "rate": meter.rate,
                "model": minfo["label"],
                "credits": meter.balance(),
                "msg": (f"Credits exhausted — {len(written)} file"
                        f"{'' if len(written) == 1 else 's'} written and saved. "
                        f"Redeem a gift code to finish the build."),
            })

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class FixIn(BaseModel):
    project_id: int
    model: str = design.DEFAULT_MODEL
    note: str = ""


@app.post("/api/fix")
async def fix(body: FixIn, x_visitor_id: str = Header(None), x_forge_vid: str = Header(None),
              x_forge_code: str = Header(None)):
    """Re-run the model over its own mistakes and stream back the corrected files.

    Billed the same way as a build: per file the model actually rewrites.
    """
    x_visitor_id = vid(x_visitor_id, x_forge_vid)
    u = auth(x_forge_code, x_visitor_id)
    uid = u["id"]
    row = db.execute("SELECT * FROM projects WHERE id=? AND user_id=?",
                     (body.project_id, uid)).fetchone()
    if not row:
        raise HTTPException(404, "No such project")
    files = json.loads(row["files"] or "{}")
    integ = json.loads(row["integrations"] or "[]")

    mid = body.model if body.model in design.MODELS else design.DEFAULT_MODEL
    minfo = design.model_info(mid)
    meter = Meter(uid, mid, f"fix: {row['title'][:20]}")
    if meter.balance() < meter.rate:
        raise HTTPException(402, f"A repair costs {meter.rate} credits per file rewritten "
                                 f"and you have {meter.balance()}.")

    issues = verifier.verify(files)
    if not issues and not body.note.strip():
        return JSONResponse({"ok": True, "clean": True,
                             "msg": "Checked every file and found nothing to fix."})

    holder = {}

    async def run():
        what = (f"re-reading {len(issues)} problem(s)" if issues
                else "working on your change")
        yield sse("status", {"msg": f"{minfo['label']} is {what}",
                             "credits": meter.balance()})
        parser = holder['p'] = engine.ProjectParser()
        user_turn = engine.fix_prompt(files, issues, body.note)
        try:
            async for kind, chunk in engine.pplx_stream(
                    engine.SYS_FIX, user_turn, max_tokens=minfo["out"],
                    think=minfo["build_think"], model=mid):
                if kind == "reason":
                    yield sse("reason", {"t": chunk})
                    continue
                for ev, path, body_ in parser.feed(chunk):
                    if ev == "open":
                        yield sse("file_open", {"path": path})
                    elif ev == "body":
                        yield sse("chunk", {"path": path, "t": body_})
                    else:
                        left = meter.charge(path)
                        yield sse("file_close", {"path": path, "cost": meter.rate,
                                                 "credits": left})
        except Exhausted as ex:
            yield sse("exhausted", {"project_id": body.project_id, "spent": ex.spent,
                                    "rate": meter.rate, "model": minfo["label"],
                                    "credits": meter.balance(), "count": ex.written,
                                    "files_written": sorted(holder['p'].files()),
                                    "msg": "Credits exhausted part-way through the repair. "
                                           "Redeem a gift code and run Fix again."})
            return
        except Exception as e:
            yield sse("fail", {"msg": f"The repair pass failed: {str(e)[:160]}"})
            return

        patched = parser.files()
        if not patched:
            yield sse("fail", {"msg": "The model returned no files, so nothing was changed "
                                      "and nothing was charged."})
            return
        merged = dict(files)
        merged.update(patched)
        # never let a repair reintroduce a problem the env loader already solved
        apply_integrations(merged, integ, _env_values)
        left = verifier.verify(merged)
        save_project(uid, row["title"], row["prompt"], merged, integ, body.project_id)
        yield sse("done", {"project_id": body.project_id, "files": merged,
                           "changed": sorted(patched),
                           "preview": engine.preview_doc(merged),
                           "entry": engine.find_frontend(merged),
                           "credits": meter.balance(),
                           "before": len(issues), "issues": left,
                           "billed": {"files": meter.files, "spent": meter.spent}})

    return StreamingResponse(run(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/check/{pid}")
def check(pid: int, x_visitor_id: str = Header(None), x_forge_vid: str = Header(None),
          x_forge_code: str = Header(None)):
    """Re-run the static checks on a stored project."""
    u = auth(x_forge_code, vid(x_visitor_id, x_forge_vid))
    row = db.execute("SELECT files FROM projects WHERE id=? AND user_id=?",
                     (pid, u["id"])).fetchone()
    if not row:
        raise HTTPException(404, "No such project")
    iss = verifier.verify(json.loads(row["files"] or "{}"))
    return {"issues": iss, "count": len(iss)}





# --------------------------------------------------------------------------- community
class VoteIn(BaseModel):
    slug: str
    val: int = 1            # 1 like, -1 dislike; sending the same value again clears it


class CommentIn(BaseModel):
    slug: str
    body: str


class RemixIn(BaseModel):
    slug: str


def _counts(slug: str) -> dict:
    v = db.execute(
        "SELECT COALESCE(SUM(val=1),0) likes, COALESCE(SUM(val=-1),0) dislikes "
        "FROM votes WHERE slug=?", (slug,)).fetchone()
    c = db.execute("SELECT COUNT(*) n FROM comments WHERE slug=?", (slug,)).fetchone()
    return {"likes": v["likes"] or 0, "dislikes": v["dislikes"] or 0,
            "comments": c["n"] or 0}


def _has_page(r) -> bool:
    """Whether a published app has a frontend the gallery can actually render."""
    pr = db.execute("SELECT files FROM projects WHERE id=?", (r["project_id"],)).fetchone()
    if pr:
        try:
            return bool(engine.find_frontend(json.loads(pr["files"])))
        except Exception:
            pass
    low = (r["html"] or "").lower()
    return "<html" in low or "<body" in low


def _card(r, me_id: int | None) -> dict:
    d = {"slug": r["slug"], "title": r["title"], "summary": r["summary"] or "",
         "author": r["handle"] or f"builder-{r['user_id']}", "views": r["views"], "at": r["at"],
         "remixes": r["remixes"] or 0, "remix_of": r["remix_of"] or "",
         "live_url": r["live_url"] or "", "mine": bool(me_id and r["user_id"] == me_id)}
    d.update(_counts(r["slug"]))
    mv = 0
    if me_id:
        row = db.execute("SELECT val FROM votes WHERE slug=? AND user_id=?",
                         (r["slug"], me_id)).fetchone()
        mv = row["val"] if row else 0
    d["my_vote"] = mv
    return d


@app.get("/api/explore")
def explore(sort: str = "new", q: str = "", request: Request = None,
            x_visitor_id: str = Header(None), x_forge_vid: str = Header(None),
            x_forge_code: str = Header(None)):
    """The public gallery. Open to everyone — signing in only adds your own votes."""
    me_id = None
    try:
        me_id = auth(x_forge_code, vid(x_visitor_id, x_forge_vid))["id"]
    except HTTPException:
        pass
    rows = db.execute(
        "SELECT p.*, u.handle FROM publishes p LEFT JOIN users u ON u.id=p.user_id"
    ).fetchall()
    # The gallery is for apps people can actually open and run. A backend-only project has
    # no page to render, so it would show up as an empty tile — keep it out of the feed.
    cards = [_card(r, me_id) for r in rows if _has_page(r)]
    if q.strip():
        needle = q.strip().lower()
        cards = [c for c in cards
                 if needle in c["title"].lower() or needle in c["summary"].lower()]
    if sort == "top":
        cards.sort(key=lambda c: (c["likes"] - c["dislikes"], c["at"]), reverse=True)
    elif sort == "remixed":
        cards.sort(key=lambda c: (c["remixes"], c["at"]), reverse=True)
    elif sort == "discussed":
        cards.sort(key=lambda c: (c["comments"], c["at"]), reverse=True)
    else:
        cards.sort(key=lambda c: c["at"], reverse=True)
    return {"items": cards[:120], "count": len(cards), "signed_in": bool(me_id)}


@app.get("/api/pub/{slug}")
def pub_detail(slug: str, request: Request, x_visitor_id: str = Header(None),
               x_forge_vid: str = Header(None), x_forge_code: str = Header(None)):
    me_id = None
    try:
        me_id = auth(x_forge_code, vid(x_visitor_id, x_forge_vid))["id"]
    except HTTPException:
        pass
    r = db.execute(
        "SELECT p.*, u.handle FROM publishes p LEFT JOIN users u ON u.id=p.user_id "
        "WHERE p.slug=?", (slug,)).fetchone()
    if not r:
        raise HTTPException(404, "That app is not published")
    card = _card(r, me_id)
    card["url"] = public_url(request, slug)
    pr = db.execute("SELECT files FROM projects WHERE id=?", (r["project_id"],)).fetchone()
    # Re-flatten from the source files when they are still around, so the inline preview
    # always carries the current shims. Fall back to the HTML captured at publish time.
    card["html"] = r["html"]
    if pr:
        try:
            card["html"] = engine.preview_doc(json.loads(pr["files"]))
        except Exception:
            pass
    card["files"] = sorted(json.loads(pr["files"]).keys()) if pr else []
    card["can_remix"] = bool(pr)
    return card


@app.post("/api/vote")
def vote(body: VoteIn, x_visitor_id: str = Header(None), x_forge_vid: str = Header(None),
         x_forge_code: str = Header(None)):
    u = auth(x_forge_code, vid(x_visitor_id, x_forge_vid))
    if body.val not in (1, -1):
        raise HTTPException(400, "A vote is either up or down")
    if not db.execute("SELECT 1 FROM publishes WHERE slug=?", (body.slug,)).fetchone():
        raise HTTPException(404, "That app is not published")
    cur = db.execute("SELECT val FROM votes WHERE slug=? AND user_id=?",
                     (body.slug, u["id"])).fetchone()
    if cur and cur["val"] == body.val:
        db.execute("DELETE FROM votes WHERE slug=? AND user_id=?", (body.slug, u["id"]))
        mine = 0
    else:
        db.execute("INSERT INTO votes (slug, user_id, val, at) VALUES (?,?,?,?) "
                   "ON CONFLICT(slug, user_id) DO UPDATE SET val=excluded.val, at=excluded.at",
                   (body.slug, u["id"], body.val, now()))
        mine = body.val
    db.commit()
    out = _counts(body.slug)
    out["my_vote"] = mine
    return out


@app.get("/api/comments/{slug}")
def comments_list(slug: str, x_visitor_id: str = Header(None),
                  x_forge_vid: str = Header(None), x_forge_code: str = Header(None)):
    me_id = None
    try:
        me_id = auth(x_forge_code, vid(x_visitor_id, x_forge_vid))["id"]
    except HTTPException:
        pass
    rows = db.execute(
        "SELECT c.id, c.body, c.at, c.user_id, u.handle FROM comments c "
        "LEFT JOIN users u ON u.id=c.user_id WHERE c.slug=? ORDER BY c.at DESC LIMIT 200",
        (slug,)).fetchall()
    return {"items": [{"id": r["id"], "body": r["body"], "at": r["at"],
                       "author": r["handle"] or "someone",
                       "mine": bool(me_id and r["user_id"] == me_id)} for r in rows]}


@app.post("/api/comments")
def comment_add(body: CommentIn, x_visitor_id: str = Header(None),
                x_forge_vid: str = Header(None), x_forge_code: str = Header(None)):
    u = auth(x_forge_code, vid(x_visitor_id, x_forge_vid))
    text = body.body.strip()[:1200]
    if not text:
        raise HTTPException(400, "Write something first")
    if not db.execute("SELECT 1 FROM publishes WHERE slug=?", (body.slug,)).fetchone():
        raise HTTPException(404, "That app is not published")
    cur = db.execute("INSERT INTO comments (slug, user_id, body, at) VALUES (?,?,?,?)",
                     (body.slug, u["id"], text, now()))
    db.commit()
    return {"id": cur.lastrowid, "body": text, "at": now(),
            "author": handle_for(u), "mine": True, "count": _counts(body.slug)["comments"]}


@app.delete("/api/comments/{cid}")
def comment_del(cid: int, x_visitor_id: str = Header(None), x_forge_vid: str = Header(None),
                x_forge_code: str = Header(None)):
    u = auth(x_forge_code, vid(x_visitor_id, x_forge_vid))
    db.execute("DELETE FROM comments WHERE id=? AND user_id=?", (cid, u["id"]))
    db.commit()
    return {"ok": True}


@app.post("/api/remix")
def remix(body: RemixIn, x_visitor_id: str = Header(None), x_forge_vid: str = Header(None),
          x_forge_code: str = Header(None)):
    """Fork someone's published app into your own account.

    Copying costs nothing — you already see the code on the detail page. Credits only
    get spent if you then ask the model to change it.
    """
    u = auth(x_forge_code, vid(x_visitor_id, x_forge_vid))
    r = db.execute(
        "SELECT p.*, u.handle FROM publishes p LEFT JOIN users u ON u.id=p.user_id "
        "WHERE p.slug=?", (body.slug,)).fetchone()
    if not r:
        raise HTTPException(404, "That app is not published")
    src = db.execute("SELECT * FROM projects WHERE id=?", (r["project_id"],)).fetchone()
    if not src:
        raise HTTPException(410, "The original project is no longer available")
    files = json.loads(src["files"])
    title = f"{r['title']} (remix)"
    pid = db.execute(
        "INSERT INTO projects (user_id, title, prompt, files, integrations, remix_of, at) "
        "VALUES (?,?,?,?,?,?,?)",
        (u["id"], title, src["prompt"], json.dumps(files),
         src["integrations"] if "integrations" in src.keys() else "[]",
         body.slug, now())).lastrowid
    db.execute("UPDATE publishes SET remixes=remixes+1 WHERE slug=?", (body.slug,))
    db.commit()
    return {"project_id": pid, "title": title, "files": files,
            "entry": engine.find_frontend(files), "preview": engine.preview_doc(files),
            "from": {"slug": body.slug, "title": r["title"],
                     "author": r["handle"] or "someone"},
            "credits": u["credits"]}


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.get("/api/project/{pid}")
def get_project(pid: int, x_visitor_id: str = Header(None), x_forge_vid: str = Header(None), x_forge_code: str = Header(None)):
    x_visitor_id = vid(x_visitor_id, x_forge_vid)
    u = auth(x_forge_code, x_visitor_id)
    r = db.execute("SELECT * FROM projects WHERE id=? AND user_id=?", (pid, u["id"])).fetchone()
    if not r:
        raise HTTPException(404, "Not found")
    return {"id": r["id"], "title": r["title"], "files": json.loads(r["files"])}


@app.get("/api/download/{pid}")
def download(pid: int, code: str = "", x_visitor_id: str = Header(None), x_forge_vid: str = Header(None)):
    x_visitor_id = vid(x_visitor_id, x_forge_vid)
    u = auth(code, x_visitor_id)
    r = db.execute("SELECT * FROM projects WHERE id=? AND user_id=?", (pid, u["id"])).fetchone()
    if not r:
        raise HTTPException(404, "Not found")
    files = json.loads(r["files"])
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    slug = re.sub(r"[^a-z0-9]+", "-", r["title"].lower()).strip("-") or "app"
    return Response(
        mem.getvalue(), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{slug}.zip"'},
    )


# ---------------------------------------------------------------- publish

def slugify(text: str, fallback: str = "app") -> str:
    # strip AFTER truncating, or a cut mid-word leaves a trailing dash in the URL
    out = re.sub(r"[^a-z0-9]+", "-", (text or "").lower())[:38].strip("-")
    return out or fallback


def origin_of(request: Request) -> str:
    """The address the caller actually reached Forge on.

    Forge usually sits behind a proxy, so request.base_url is the internal
    localhost address — useless in a shared link. The forwarded headers carry the
    real host, and the browser's Origin is the last resort.
    """
    if PUBLIC_BASE:
        return PUBLIC_BASE
    h = request.headers
    host = h.get("x-forwarded-host") or h.get("host") or ""
    proto = h.get("x-forwarded-proto") or request.url.scheme or "http"
    if host and "localhost" not in host and "127.0.0.1" not in host:
        return f"{proto}://{host.split(',')[0].strip()}"
    origin = (h.get("origin") or "").rstrip("/")
    if origin and "localhost" not in origin and "null" not in origin:
        return origin
    ref = (h.get("referer") or "").rstrip("/")
    if ref.startswith("http") and "localhost" not in ref:
        parts = ref.split("/")
        if len(parts) >= 3:
            return "/".join(parts[:3])
    return str(request.base_url).rstrip("/")


def public_url(request: Request, slug: str) -> str:
    return f"{origin_of(request)}/p/{slug}"


def handle_for(u) -> str:
    """A short public nickname so published apps have an author without accounts."""
    h = (u["handle"] or "").strip() if "handle" in u.keys() else ""
    if h:
        return h
    h = "builder-" + u["code"].replace("FRG-", "").split("-")[0].lower()
    db.execute("UPDATE users SET handle=? WHERE id=?", (h, u["id"]))
    db.commit()
    return h


class PublishIn(BaseModel):
    project_id: int
    summary: str = ""


# --------------------------------------------------------------------------- real hosting
VERCEL_API = "https://api.vercel.com"


def _vercel_files(files: dict) -> list[dict]:
    """Turn a generated project into Vercel's inline file list.

    The published artefact is the frontend: the static files are uploaded as they
    are and a vercel.json rewrites unknown paths to index.html so client routing
    works. The preview shim is injected into index.html so the app is functional
    without the Python backend, which Vercel's static hosting cannot run.
    """
    out, has_index = [], False
    for path, text in files.items():
        if path.startswith("static/"):
            web = path[len("static/"):]
        elif path.endswith((".html", ".css", ".js", ".svg", ".json", ".ico", ".txt", ".webmanifest")):
            web = path
        else:
            continue            # server.py, requirements.txt, .env — never published
        if web.startswith(".") or "/." in web:
            continue
        if web == "index.html":
            text, has_index = engine.preview_doc(files), True
        out.append({"file": web, "data": text})
    if not has_index:
        out.append({"file": "index.html", "data": engine.preview_doc(files)})
        out = [f for f in out if f["file"] != "index.html" or f["data"]]
    out.append({"file": "vercel.json", "data": json.dumps(
        {"cleanUrls": True,
         "rewrites": [{"source": "/((?!.*\\.).*)", "destination": "/index.html"}]})})
    return out


def vercel_deploy(name: str, files: dict, env: dict | None = None) -> dict:
    """Push one project to Vercel. Raises RuntimeError with a readable message.

    `env` lets a caller pass the publisher's own keys so an app lands in their Vercel
    account rather than the shared one. Falls back to the admin vault.
    """
    env = env if env is not None else vault()
    token = env.get("VERCEL_TOKEN", "").strip()
    if not token:
        raise RuntimeError("no token")
    team = env.get("VERCEL_TEAM_ID", "").strip()
    payload = {
        "name": (slugify(name) or "forge-app")[:52],
        "files": _vercel_files(files),
        "target": "production",
        "projectSettings": {"framework": None, "buildCommand": None,
                            "outputDirectory": None, "installCommand": None},
    }
    url = f"{VERCEL_API}/v13/deployments?skipAutoDetectionConfirmation=1"
    if team:
        url += f"&teamId={team}"
    try:
        with httpx.Client(timeout=90) as c:
            r = c.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload)
    except Exception as e:                                  # network trouble
        raise RuntimeError(f"could not reach Vercel ({e.__class__.__name__})")
    if r.status_code >= 400:
        msg = ""
        try:
            msg = (r.json().get("error") or {}).get("message", "")
        except Exception:
            msg = r.text[:160]
        if r.status_code in (401, 403):
            raise RuntimeError("Vercel rejected the token — check it in the admin panel")
        raise RuntimeError(msg or f"Vercel returned {r.status_code}")
    d = r.json()
    host = d.get("alias") or []
    live = ("https://" + host[0]) if host else ("https://" + d.get("url", ""))
    return {"url": live, "id": d.get("id", ""),
            "inspect": d.get("inspectorUrl") or "",
            "ready": d.get("readyState", "")}


@app.post("/api/publish")
def publish(body: PublishIn, request: Request, x_visitor_id: str = Header(None),
            x_forge_vid: str = Header(None), x_forge_code: str = Header(None)):
    x_visitor_id = vid(x_visitor_id, x_forge_vid)
    u = auth(x_forge_code, x_visitor_id)
    r = db.execute("SELECT * FROM projects WHERE id=? AND user_id=?",
                   (body.project_id, u["id"])).fetchone()
    if not r:
        raise HTTPException(404, "Project not found")
    handle_for(u)          # publishing gives you a public name in the gallery
    existing = db.execute("SELECT slug FROM publishes WHERE project_id=?", (r["id"],)).fetchone()
    # Charged once per app. Pushing an update to something already live is free, so nobody
    # is penalised for fixing a typo on a page they already paid to publish.
    credits_left = None
    if not existing:
        credits_left = spend(u["id"], COST_PUBLISH, "publishing an app")
    # A published page is a single document, so the project's frontend is flattened:
    # CSS and JS inlined, with the preview shim standing in for the backend.
    html = engine.preview_doc(json.loads(r["files"]))
    if existing:
        slug = existing["slug"]
        db.execute("UPDATE publishes SET html=?, title=?, summary=?, at=? WHERE slug=?",
                   (html, r["title"], body.summary.strip()[:180], now(), slug))
    else:
        base = slugify(r["title"])
        slug = base
        n = 1
        while db.execute("SELECT 1 FROM publishes WHERE slug=?", (slug,)).fetchone():
            n += 1
            slug = f"{base}-{n}"
        db.execute(
            "INSERT INTO publishes (slug, project_id, user_id, title, summary, html, at) "
            "VALUES (?,?,?,?,?,?,?)",
            (slug, r["id"], u["id"], r["title"], body.summary.strip()[:180], html, now()),
        )
    db.commit()
    row = db.execute("SELECT views FROM publishes WHERE slug=?", (slug,)).fetchone()
    res = {"slug": slug, "url": public_url(request, slug), "views": row["views"],
           "updated": bool(existing), "indexable": "localhost" not in public_url(request, slug),
           "host": "forge", "charged": 0 if existing else COST_PUBLISH}
    if credits_left is not None:
        res["credits"] = credits_left
    # If a deploy token is saved, the app also goes live on real hosting with its own
    # domain. Failures never block publishing — the Forge-hosted page stays up.
    _pub_env = keys_for(u["id"])
    if _pub_env.get("VERCEL_TOKEN", "").strip():
        try:
            dep = vercel_deploy(r["title"] or slug, json.loads(r["files"]), _pub_env)
            db.execute("UPDATE publishes SET live_url=? WHERE slug=?", (dep["url"], slug))
            db.commit()
            res.update(host="vercel", live_url=dep["url"], indexable=True,
                       inspect=dep["inspect"])
        except RuntimeError as e:
            res["deploy_error"] = str(e)
    else:
        prev = db.execute("SELECT live_url FROM publishes WHERE slug=?", (slug,)).fetchone()
        if prev and prev["live_url"]:
            res.update(host="vercel", live_url=prev["live_url"], indexable=True)
    return res


@app.get("/api/published")
def my_published(request: Request, x_visitor_id: str = Header(None),
                 x_forge_vid: str = Header(None), x_forge_code: str = Header(None)):
    x_visitor_id = vid(x_visitor_id, x_forge_vid)
    u = auth(x_forge_code, x_visitor_id)
    rows = db.execute(
        "SELECT slug, title, views, at, project_id FROM publishes WHERE user_id=? ORDER BY at DESC",
        (u["id"],)).fetchall()
    return {"items": [dict(r, url=public_url(request, r["slug"])) for r in rows]}


@app.delete("/api/publish/{slug}")
def unpublish(slug: str, x_visitor_id: str = Header(None), x_forge_vid: str = Header(None),
              x_forge_code: str = Header(None)):
    x_visitor_id = vid(x_visitor_id, x_forge_vid)
    u = auth(x_forge_code, x_visitor_id)
    db.execute("DELETE FROM publishes WHERE slug=? AND user_id=?", (slug, u["id"]))
    db.commit()
    return {"ok": True}


SEO_HEAD = """<meta name="robots" content="index,follow">
<meta name="generator" content="Forge">
<meta property="og:type" content="website">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta name="description" content="{desc}">
<link rel="canonical" href="{url}">
"""


@app.get("/p/{slug}", response_class=HTMLResponse)
def serve_published(slug: str, request: Request):
    """Public, unauthenticated page — this is what a shared link serves."""
    r = db.execute("SELECT * FROM publishes WHERE slug=?", (slug,)).fetchone()
    if not r:
        raise HTTPException(404, "No app published at this address")
    db.execute("UPDATE publishes SET views=views+1 WHERE slug=?", (slug,))
    db.commit()
    html = r["html"]
    desc = (r["summary"] or f"{r['title']} — an app built with Forge.").replace('"', "&quot;")
    head = SEO_HEAD.format(title=r["title"].replace('"', "&quot;"), desc=desc,
                           url=public_url(request, slug))
    i = html.lower().find("<head>")
    html = html[:i + 6] + "\n" + head + html[i + 6:] if i != -1 else head + html
    return HTMLResponse(html, headers={"Cache-Control": "public, max-age=60"})


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots(request: Request):
    base = PUBLIC_BASE or str(request.base_url).rstrip("/")
    return f"User-agent: *\nAllow: /p/\nDisallow: /api/\nSitemap: {base}/sitemap.xml\n"


@app.get("/sitemap.xml")
def sitemap(request: Request):
    rows = db.execute("SELECT slug, at FROM publishes ORDER BY at DESC LIMIT 5000").fetchall()
    items = "".join(
        f"<url><loc>{public_url(request, r['slug'])}</loc>"
        f"<lastmod>{time.strftime('%Y-%m-%d', time.gmtime(r['at']))}</lastmod></url>"
        for r in rows)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + items + "</urlset>")
    return Response(xml, media_type="application/xml")


# ---------------------------------------------------------------- admin

def admin_guard(pw):
    if pw != ADMIN_PASS:
        raise HTTPException(403, "Wrong admin password")


class AdminIn(BaseModel):
    password: str


class SecretIn(BaseModel):
    password: str
    name: str
    value: str = ""


_ALLOWED_KEYS = {e for spec in engine.INTEGRATIONS.values() for e in spec["env"]}
# Not an integration — this is the token Forge uses to push published apps onto
# real hosting instead of serving them from this box.
_ALLOWED_KEYS |= {"VERCEL_TOKEN", "VERCEL_TEAM_ID"}


@app.post("/api/admin/secrets")
def admin_secrets(body: SecretIn):
    """Save or clear one integration key. Stored values are never sent back to any client."""
    admin_guard(body.password)
    name = body.name.strip().upper()
    if name not in _ALLOWED_KEYS:
        raise HTTPException(400, "Not an integration key Forge knows about")
    val = body.value.strip()
    if val:
        db.execute("INSERT INTO secrets (name, val, at) VALUES (?,?,?) "
                   "ON CONFLICT(name) DO UPDATE SET val=excluded.val, at=excluded.at",
                   (name, val, now()))
    else:
        db.execute("DELETE FROM secrets WHERE name=?", (name,))
    db.commit()
    return {"ok": True, "name": name, "configured": bool(val)}


@app.post("/api/admin/stats")
def admin_stats(body: AdminIn):
    admin_guard(body.password)
    users = db.execute("SELECT id, code, credits, created_at FROM users ORDER BY id DESC LIMIT 100").fetchall()
    gifts = db.execute(
        "SELECT g.id,g.code,g.credits,g.note,g.redeemed_at,u.code AS by_code "
        "FROM gifts g LEFT JOIN users u ON u.id=g.redeemed_by ORDER BY g.id DESC LIMIT 200"
    ).fetchall()
    agg = db.execute("SELECT COUNT(*) c, COALESCE(SUM(credits),0) s FROM users").fetchone()
    spent = db.execute("SELECT COALESCE(SUM(delta),0) s FROM ledger WHERE delta<0").fetchone()
    projs = db.execute("SELECT COUNT(*) c FROM projects").fetchone()
    pubs = db.execute("SELECT COUNT(*) c, COALESCE(SUM(views),0) v FROM publishes").fetchone()
    return {
        "users": [dict(u) for u in users],
        "gifts": [dict(g) for g in gifts],
        "totals": {"users": agg["c"], "credits_held": agg["s"],
                   "credits_spent": -spent["s"], "projects": projs["c"],
                   "published": pubs["c"], "views": pubs["v"]},
        "engine": engine_state,
    }


class GiftIn(BaseModel):
    password: str
    credits: int = 20
    count: int = 1
    note: str = ""


@app.post("/api/admin/gifts")
def admin_gifts(body: GiftIn):
    admin_guard(body.password)
    n = max(1, min(100, body.count))
    cr = max(1, min(10000, body.credits))
    made = []
    for _ in range(n):
        c = make_code("GIFT", 2)
        db.execute(
            "INSERT INTO gifts (code, credits, note, created_at) VALUES (?,?,?,?)",
            (c, cr, body.note.strip()[:40], now()),
        )
        made.append(c)
    db.commit()
    return {"codes": made, "credits": cr}


class GrantIn(BaseModel):
    password: str
    code: str
    credits: int


@app.post("/api/admin/grant")
def admin_grant(body: GrantIn):
    admin_guard(body.password)
    u = find_user(body.code)
    if not u:
        raise HTTPException(404, "No such user code")
    db.execute("UPDATE users SET credits=MAX(0,credits+?) WHERE id=?", (body.credits, u["id"]))
    ledger(u["id"], body.credits, "admin adjustment")
    db.commit()
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
