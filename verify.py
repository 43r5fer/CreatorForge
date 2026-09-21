"""Static checks on a generated project.

Runs in milliseconds, no install, no execution. The goal is not to prove the app
correct — it is to catch the handful of mistakes a model actually makes, with
enough detail that the repair pass can fix them in one shot.
"""

from __future__ import annotations

import ast
import re

# --------------------------------------------------------------------------- helpers

ROUTE_RE = re.compile(
    r'@(?:app|router)\.(get|post|put|patch|delete)\(\s*[fr]?["\']([^"\']+)["\']',
    re.I,
)
FETCH_RE = re.compile(r'fetch\(\s*([`"\'])([^`"\']*?)\1')
FETCH_TPL_RE = re.compile(r'fetch\(\s*`([^`]*)`')
API_LIT_RE = re.compile(r'([`"\'])(/api/[^`"\'\s]*)\1')
METHOD_RE = re.compile(r'method\s*:\s*["\'](\w+)["\']', re.I)
ID_RE = re.compile(r'getElementById\(\s*["\']([\w-]+)["\']')
SEL_ID_RE = re.compile(r'querySelector(?:All)?\(\s*["\']#([\w-]+)')
# most projects wrap it in a $() helper, so also read bare '#id' selector literals
HASH_LIT_RE = re.compile(r'["\'`]#([A-Za-z][\w-]*)["\'`]')
HEX_RE = re.compile(r'^(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$')
# e.g. const $ = id => document.getElementById(id)   /   function $(i){return document...}
HELPER_RE = re.compile(
    r'(?:const|let|var)\s+(\$\w*|\w+)\s*=\s*\(?\s*\w+\s*\)?\s*=>\s*document\.getElementById'
    r'|function\s+(\$\w*|\w+)\s*\([^)]*\)\s*\{\s*return\s+document\.getElementById'
)
HTML_ID_RE = re.compile(r'\bid\s*=\s*["\']([\w-]+)["\']')
SRC_RE = re.compile(r'(?:src|href)\s*=\s*["\']([^"\'#?]+)["\']')


def _norm(path: str) -> str:
    """/api/items/{id} and /api/items/3 both collapse to /api/items/*."""
    p = path.split("?")[0].rstrip("/") or "/"
    # a hole glued straight onto a segment is a query string or suffix, not a new
    # segment: /api/books${params} is still /api/books
    p = re.sub(r"(?<=[^/])\$\{[^}]*\}", "", p)
    p = re.sub(r"\$\{[^}]*\}", "*", p)        # JS template holes, before path params
    p = re.sub(r"\{[^}]*\}", "*", p)          # FastAPI path params
    p = re.sub(r"/\d+(?=/|$)", "/*", p)       # literal ids
    return p


def _issue(kind, file, msg, line=None, detail=""):
    return {"kind": kind, "file": file, "line": line, "msg": msg, "detail": detail}


# --------------------------------------------------------------------------- checks

def _check_python(files: dict) -> list:
    out = []
    for path, src in files.items():
        if not path.endswith(".py"):
            continue
        try:
            ast.parse(src)
        except SyntaxError as e:
            out.append(_issue(
                "syntax", path,
                f"Python syntax error: {e.msg}",
                e.lineno,
                (e.text or "").strip()[:160],
            ))
    return out


def _check_js_balance(files: dict) -> list:
    """Catches a truncated or unclosed JS file, which is the usual stream failure."""
    out = []
    pairs = {"}": "{", ")": "(", "]": "["}
    for path, src in files.items():
        if not path.endswith(".js"):
            continue
        # strip strings, template literals and comments before counting
        s = re.sub(r"//[^\n]*", "", src)
        s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
        s = re.sub(r"`(?:\\.|[^`\\])*`", "``", s, flags=re.S)
        s = re.sub(r"'(?:\\.|[^'\\\n])*'", "''", s)
        s = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', s)
        stack = []
        for ch in s:
            if ch in "{([":
                stack.append(ch)
            elif ch in pairs:
                if stack and stack[-1] == pairs[ch]:
                    stack.pop()
                else:
                    stack.append("!")
                    break
        if stack:
            out.append(_issue(
                "syntax", path,
                f"Unbalanced brackets — {len(stack)} block(s) left open. "
                "The file is probably truncated or missing a closing brace.",
            ))
    return out


def _check_contract(files: dict) -> list:
    """Every endpoint the frontend calls must exist in the backend."""
    out = []
    backend = "\n".join(v for k, v in files.items() if k.endswith(".py"))
    if not backend:
        return out
    routes = {(m.group(1).lower(), _norm(m.group(2))) for m in ROUTE_RE.finditer(backend)}
    paths = {p for _, p in routes}
    if not routes:
        return out

    for path, src in files.items():
        if not path.endswith(".js"):
            continue
        # Direct fetch() calls, plus any /api literal — most projects wrap fetch in a
        # helper like api(path), so the literal is the only reliable signal.
        calls, seen_at = [], set()
        for m in FETCH_RE.finditer(src):
            calls.append((m.group(2), m.start(), True))
        for m in FETCH_TPL_RE.finditer(src):
            calls.append((m.group(1), m.start(), True))
        for m in API_LIT_RE.finditer(src):
            calls.append((m.group(2), m.start(), False))
        for url, at, direct in calls:
            if not url.startswith("/api"):
                continue
            n = _norm(url)
            if (n, direct) in seen_at:
                continue
            seen_at.add((n, direct))
            line = src.count("\n", 0, at) + 1
            tail = src[at:at + 400]
            verb = (METHOD_RE.search(tail).group(1).lower()
                    if METHOD_RE.search(tail) else "get")
            if n not in paths:
                out.append(_issue(
                    "contract", path,
                    f"fetch('{url}') has no matching route in the backend.",
                    line,
                    "Backend routes: " + ", ".join(sorted(paths)[:14]),
                ))
            elif direct and (verb, n) not in routes:
                have = sorted({v.upper() for v, p in routes if p == n})
                out.append(_issue(
                    "contract", path,
                    f"fetch('{url}') uses {verb.upper()}, but the backend only "
                    f"defines {', '.join(have)} for that path.",
                    line,
                ))
    return out


def _check_dom(files: dict) -> list:
    """Every id the script reaches for must exist in the markup."""
    out = []
    html = "\n".join(v for k, v in files.items() if k.endswith(".html"))
    if not html:
        return out
    ids = set(HTML_ID_RE.findall(html))
    for path, src in files.items():
        if not path.endswith(".js"):
            continue
        seen = set()
        rxs = [ID_RE, SEL_ID_RE, HASH_LIT_RE]
        for hm in HELPER_RE.finditer(src):
            name = hm.group(1) or hm.group(2)
            rxs.append(re.compile(re.escape(name) + r'\(\s*["\']([\w-]+)["\']\s*\)'))
        for rx in rxs:
            for m in rx.finditer(src):
                name = m.group(1)
                if name in ids or name in seen or HEX_RE.match(name):
                    continue
                # ids created at runtime by the script itself are fine
                if re.search(rf'''id\s*=\s*["'`]?\{{?\s*{re.escape(name)}''', src):
                    continue
                if f'"{name}"' in src and f'id="{name}"' in src:
                    continue
                seen.add(name)
                out.append(_issue(
                    "dom", path,
                    f"The script looks up #{name}, which does not exist in the markup.",
                    src.count("\n", 0, m.start()) + 1,
                ))
    return out


def _check_assets(files: dict) -> list:
    """<script src> and <link href> must point at files that were generated."""
    out = []
    have = set(files)
    base = {p.split("/")[-1] for p in files}
    for path, src in files.items():
        if not path.endswith(".html"):
            continue
        for m in SRC_RE.finditer(src):
            ref = m.group(1)
            if ref.startswith(("http", "//", "data:", "mailto:")):
                continue
            leaf = ref.split("/")[-1]
            if not leaf or "." not in leaf:
                continue
            cand = ref.lstrip("/")
            if cand in have or f"static/{cand}" in have or leaf in base:
                continue
            out.append(_issue(
                "asset", path,
                f"References {ref}, but no such file was generated.",
                src.count("\n", 0, m.start()) + 1,
            ))
    return out


def _check_truncation(files: dict) -> list:
    out = []
    for path, src in files.items():
        s = src.rstrip()
        if not s:
            out.append(_issue("empty", path, "The file is empty."))
            continue
        if re.search(r"(rest of the code|\.\.\. as above|# \.\.\.$|// \.\.\.$|TODO: implement)",
                     s, re.I):
            out.append(_issue(
                "stub", path,
                "The file contains a placeholder instead of real code.",
            ))
    return out


# --------------------------------------------------------------------------- entry

def verify(files: dict) -> list:
    """Return a list of issue dicts, worst first. Empty list means nothing found."""
    issues = []
    for fn in (_check_python, _check_js_balance, _check_truncation,
               _check_contract, _check_dom, _check_assets):
        try:
            issues += fn(files)
        except Exception as e:                       # a checker must never break a build
            issues.append(_issue("checker", "-", f"Check {fn.__name__} failed: {e}"))
    rank = {"syntax": 0, "empty": 0, "stub": 1, "contract": 2, "dom": 3, "asset": 4}
    issues.sort(key=lambda i: rank.get(i["kind"], 9))
    return issues[:24]


def brief(issues: list) -> str:
    """Format issues for the repair prompt."""
    lines = []
    for i in issues:
        where = i["file"] + (f":{i['line']}" if i.get("line") else "")
        lines.append(f"- [{i['kind']}] {where} — {i['msg']}")
        if i.get("detail"):
            lines.append(f"    {i['detail']}")
    return "\n".join(lines)
