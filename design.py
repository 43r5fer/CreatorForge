"""Art direction and model catalog for Forge.

Kept in its own module so the prompt text can hold quotes and newlines freely
without fighting the f-strings in engine.py.
"""

# --------------------------------------------------------------------------- models

# Each model charges its own rate per generated file. `think` is the reasoning
# budget in tokens. `out` caps the build response.
MODELS = {
    "claude_sonnet_4_6": {
        "label": "Claude Sonnet 4.6",
        "blurb": "Best all-rounder. Sharpest visual taste of the set.",
        "per_file": 2,
        "think": 6000,
        "build_think": 2500,
        "out": 32000,
        "default": True,
    },
    "claude_opus_4_7": {
        "label": "Claude Opus 4.7",
        "blurb": "Deepest reasoning. Use it for complex, multi-screen apps.",
        "per_file": 5,
        "think": 10000,
        "build_think": 3000,
        "out": 32000,
    },
    "claude_haiku_4_5": {
        "label": "Claude Haiku 4.5",
        "blurb": "Fast and cheap. Good for small, single-purpose tools.",
        "per_file": 1,
        "think": 2500,
        "build_think": 1500,
        "out": 24000,
    },
    "gemini_3_1_pro": {
        "label": "Gemini 3.1 Pro",
        "blurb": "Strong at dense, data-heavy dashboards.",
        "per_file": 3,
        "think": 6000,
        "build_think": 2500,
        "out": 32000,
    },
    "gpt_5_1": {
        "label": "GPT-5.1",
        "blurb": "Careful, literal. Follows a long brief closely.",
        "per_file": 3,
        "think": 6000,
        "build_think": 2500,
        "out": 32000,
    },
}

DEFAULT_MODEL = "claude_sonnet_4_6"


def model_info(mid: str) -> dict:
    """`think` is the plan-phase budget; `build_think` is the smaller one used while
    the model is actually writing files."""
    return MODELS.get(mid) or MODELS[DEFAULT_MODEL]


def public_models() -> list:
    out = []
    for mid, m in MODELS.items():
        out.append({
            "id": mid,
            "label": m["label"],
            "blurb": m["blurb"],
            "per_file": m["per_file"],
            "default": bool(m.get("default")),
        })
    return out


# --------------------------------------------------------------------------- art direction

# The single biggest lever on whether the generated app looks like a real product
# or like a 2013 Bootstrap template. Be specific: vague instructions ("make it
# beautiful") produce the default look every time.
DESIGN = """
DESIGN — this carries as much weight as the code. A working app that looks generic is a failure.

Art direction
- Derive the palette from the subject, not from a default. A coffee roaster is warm clay and
  cream; a trading tool is near-black and one signal colour; a kids' app is bright and rounded.
  Decide the direction first, then commit to it in every file.
- Exactly one accent colour, used only for the primary action and active state. Everything else
  is a neutral ramp. Do not colour headings, icons and borders in the accent too.
- Pick light or dark deliberately and design for it properly. If dark, use a warm or cool
  near-black (#0c0d10, #12100e), never pure #000, and lift surfaces with subtle borders rather
  than heavy shadows.

Typography — the fastest way to look modern or dated
- Load a real typeface. Fontshare (Satoshi, General Sans, Bespoke Serif, Switzer) or Google
  Fonts (Inter, Instrument Serif, Sora, DM Sans, Space Grotesk). Never leave it on system-ui,
  Arial, or Tailwind's default stack.
- One display face for headings plus one text face, or a single family across two weights with
  real contrast (e.g. 700 against 400). Never three families.
- Headings are tight: `letter-spacing:-0.02em` to `-0.04em` and `line-height:1.05` at large
  sizes. Body copy sits at 1.6 line-height and 60–75 characters per line.
- Use a genuine scale with visible jumps (e.g. 13 / 15 / 18 / 24 / 34 / 52px). Four sizes,
  not nine near-identical ones.

Layout and space
- Whitespace is the main tool. Section padding of 80–120px on desktop reads as considered;
  16px everywhere reads as a prototype.
- Constrain text with `max-width` and align to a grid. Break symmetry somewhere — an offset
  hero, an asymmetric two-column split — so it is not one centred stack all the way down.
- Design the real content, not boxes. Write specific, plausible copy: real product names,
  real prices, real sentences. Never "Lorem ipsum", never "Card title", never "Item 1".

Detail that separates real products from templates
- Icons are inline SVG with `stroke="currentColor"`, `stroke-width="1.5"`, `fill="none"`.
  Emoji are never icons. No 📷 🚀 ✨ in headings, buttons or nav.
- Borders over shadows: `1px solid` at low contrast beats a drop shadow on every card. If you
  use shadow, make it large, soft and nearly invisible.
- Every interactive element gets a hover state and a visible `:focus-visible` ring, with a
  120–200ms transition. Buttons must change on press.
- Consistent radii from one scale (e.g. 8 / 12 / 16px). Do not mix pill buttons with square cards.
- Real states: a designed empty state with a short line of guidance and a primary action, a
  skeleton or spinner while loading, and an inline error that says what to do next. Never a
  bare "No data" or a browser `alert()`.
- Tables and lists get generous row padding, a quiet header row, and alignment by type —
  numbers right-aligned and tabular (`font-variant-numeric: tabular-nums`).

Modern web craft — make it read as a 2026 website, not a 2014 admin panel
- Build a real page, not a single form on a blank canvas. Even a tool gets structure: a
  sticky top bar (56-64px, 1px bottom border, background at 85% opacity with
  `backdrop-filter: blur(10px)`), a content region with proper vertical rhythm, and a quiet
  footer. The user should be able to scroll and feel sections change.
- Give the app an identity: a short product name and a small inline-SVG logo mark in the nav,
  not the raw prompt text as an <h1>.
- The first screen must state what the thing is in one confident line at 34-52px, with a
  supporting sentence under it and the primary action immediately reachable. No app should
  open on an unexplained empty grid.
- Motion is subtle and purposeful. A 200-400ms fade-and-rise on content entering the
  viewport via IntersectionObserver, transforms on hover, a smooth height or opacity change
  when panels open. Animate `transform` and `opacity` only, never `width`/`top`/`left`.
  Always respect `@media (prefers-reduced-motion: reduce)` by disabling it.
- Depth comes from layering, not decoration: a slightly different surface colour for cards
  against the page, a hairline border, and one soft shadow reserved for what floats
  (modals, dropdowns, drawers).
- Panels that slide in from the edge (a right-hand drawer for add/edit) feel current;
  a centred box with a grey overlay feels old. Either way, trap focus and close on Escape.
- Sweat the small type: uppercase 11-12px labels with `letter-spacing:.08em` for section
  eyebrows, tabular numbers for any figure, and `text-wrap: balance` on headings.
- Interactive polish that costs little and reads as expensive: a focus ring that matches the
  accent, a pressed state on buttons, keyboard shortcuts with a visible hint, optimistic UI
  on save, and a toast that slides in rather than an alert.

Banned — these are the tells of generated work
- Unstyled Tailwind defaults: `bg-blue-500`, `bg-gray-100` cards, `rounded-lg shadow-md` on
  everything, `text-gray-600` body on white with no hierarchy.
- Purple-to-pink or blue-to-cyan gradient heroes. Gradient text. Glassmorphism blur panels.
- Everything centred in a single narrow column with equal gaps.
- Emoji as decoration or icons. `alert()` or `confirm()` for feedback.
- Placeholder image services and grey boxes. Draw an inline SVG or use a CSS pattern instead.
- Text glyphs standing in for icons: no ★ ☆ ✓ ✗ → as rating stars, checkmarks or arrows.
  Draw them as inline SVG so stroke weight and size match everything else.
- Dated chrome: beveled or inset borders, `box-shadow` with a hard offset, 2px+ outlines,
  pure #000 text on pure #fff, full-width tables with visible gridlines on every cell,
  or a left sidebar of plain text links with no spacing system.

Responsive
- Design mobile first and verify it: nothing may overflow horizontally at 380px. Tap targets
  at least 44px. Navigation collapses properly rather than wrapping into a mess.
""".strip()


# --------------------------------------------------------------------------- self-review

REVIEW = """
Before you emit a single character, think the whole build through: the schema, every endpoint
and its exact response shape, which frontend function calls it, and the failure path for each.

Then, as you write, hold these invariants — a break in any of them is a bug the user will hit
on first run:
- Every URL `app.js` fetches exists in `server.py` with the same method, and the JSON keys the
  frontend reads are the keys the backend actually returns. Check each one against the other.
- Every element id or class `app.js` looks up exists in `index.html`, spelled identically.
- Every CSS class used in the markup is either a real Tailwind utility or defined in styles.css.
- Every SQL statement matches the CREATE TABLE columns, and every `?` has an argument.
- Every function, import and variable you reference is defined. No imports you do not use.
- Python must parse: consistent indentation, closed brackets, no stray markdown.
- Async correctness: `await` on every coroutine, and no blocking sleep in a handler.

Write complete files. Never abbreviate with "// rest of the code" or "# ... as above".
""".strip()


# --------------------------------------------------------------------------- repair

SYS_FIX = (
    "You are debugging a project you just wrote. The user reports the errors below. "
    "Work out the true root cause of each one, then re-emit ONLY the files you need to "
    "change, complete, using this exact protocol and nothing else:\n\n"
    "<<<FILE path/name.ext>>>\n"
    "...the entire corrected file...\n"
    "<<<ENDFILE>>>\n\n"
    "Rules:\n"
    "- Emit whole files, never fragments or diffs.\n"
    "- Do not rename files, drop features, or restyle anything that was not broken.\n"
    "- Fix the cause, not the symptom. If app.js calls an endpoint that does not exist, "
    "decide which side is wrong and correct that side.\n"
    "- If an error is not real, skip it rather than inventing a change.\n"
    "- No prose before, between or after the files."
)
