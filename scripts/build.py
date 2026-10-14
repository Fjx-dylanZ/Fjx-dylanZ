#!/usr/bin/env python3
"""Build the profile README.

    python3 scripts/build.py fetch             pull GitHub data into data/github.json
    python3 scripts/build.py render            render assets/*.svg and README.md
    python3 scripts/build.py fetch render      both
    python3 scripts/build.py press TITLE USER  apply an "hjkl: <key>" issue, then render

Standard library only, so the GitHub Actions jobs need no setup step.
"""
from __future__ import annotations

import datetime as dt
import functools
import hashlib
import html
import json
import math
import os
import random
import re
import subprocess
import sys
import textwrap
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "profile.toml"
DATA = ROOT / "data" / "github.json"
STATE = ROOT / "data" / "hjkl.json"
GARDEN = ROOT / "data" / "garden.json"
ASSETS = ROOT / "assets"
TEMPLATE = ROOT / "templates" / "README.md"
README = ROOT / "README.md"

# Ghostty's "Citruszest" palette plus the JankyBorders active/inactive colors from my dotfiles.
C = {
    "wall": "#0b0b0b", "bg": "#121212", "panel": "#161616", "raised": "#1f1f1f", "line": "#2a2a2a",
    "fg": "#bfbfbf", "bright": "#f9f9f9", "dim": "#808080", "mute": "#555555",
    "red": "#ff5454", "green": "#00cc7a", "yellow": "#ffd400", "blue": "#00bfff",
    "magenta": "#ff90fe", "cyan": "#48d1cc", "green2": "#1affa3", "blue2": "#33cfff",
    "active": "#f77f00", "inactive": "#3a5a40",
}
PALETTE = ["#404040", "#ff5454", "#00cc7a", "#ffd400", "#00bfff", "#ff90fe", "#48d1cc", "#bfbfbf",
           "#808080", "#ff1a75", "#1affa3", "#ffff00", "#33cfff", "#ffb2fe", "#00fff2", "#f9f9f9"]
HEAT = ["#1d1d1d", "#11402c", "#0a6e48", "#00b36b", "#1affa3"]
FONT = "ui-monospace,'SF Mono',SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"
CW = 0.6  # advance width of a monospace glyph, in em
# key: (workspace step, window step, what it does, keycap legend). Workspaces are stacked
# rows, so j/k are paneru's focus south/north and h/l its focus west/east. gg isn't a step:
# like vim's, it goes back to the top, the first window of the first workspace.
KEYS = {"gg": (0, 0, "back to the first pane", "home"),
        "h": (0, -1, "focus west", "west"), "j": (1, 0, "focus south, to the workspace below", "south"),
        "k": (-1, 0, "focus north, to the workspace above", "north"), "l": (0, 1, "focus east", "east")}


# ── GitHub data ──────────────────────────────────────────────────────────────

@functools.cache
def gh_token() -> str | None:
    for var in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def request(path: str, body: dict | None = None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "profile-readme-build"}
    if token := gh_token():
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"https://api.github.com/{path}", data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch(cfg: dict) -> None:
    login = cfg["login"]
    prev = json.loads(DATA.read_text()) if DATA.exists() else {}
    data = {"fetched_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
    data["public_repos"] = request(f"users/{login}")["public_repos"]

    query = """query($login: String!) { user(login: $login) { contributionsCollection {
        totalCommitContributions totalPullRequestContributions
        contributionCalendar { totalContributions weeks { contributionDays { date contributionCount } } }
    } } }"""
    try:
        res = request("graphql", {"query": query, "variables": {"login": login}})
        cc = res["data"]["user"]["contributionsCollection"]
        data["weeks"] = [[d["contributionCount"] for d in w["contributionDays"]]
                         for w in cc["contributionCalendar"]["weeks"]]
        data["totals"] = {"contributions": cc["contributionCalendar"]["totalContributions"],
                          "commits": cc["totalCommitContributions"],
                          "prs": cc["totalPullRequestContributions"]}
    except Exception as exc:  # GraphQL is the flakiest call; keep yesterday's numbers.
        print(f"contributions: {exc}; keeping previous data", file=sys.stderr)
        data["weeks"], data["totals"] = prev.get("weeks", []), prev.get("totals", {})

    DATA.parent.mkdir(exist_ok=True)
    DATA.write_text(json.dumps(data, indent=1) + "\n")
    fetch_weather(cfg)


# ── hjkl state ───────────────────────────────────────────────────────────────

def clamp_focus(spaces: list, focus: dict) -> dict:
    """{"ws": focused workspace, "win": the focused window in each workspace}, kept in range."""
    win = focus.get("win", [])
    return {"ws": min(max(focus.get("ws", 0), 0), len(spaces) - 1),
            "win": [min(max(win[i] if i < len(win) else 0, 0), len(space["windows"]) - 1)
                    for i, space in enumerate(spaces)]}


def load_state(cfg: dict) -> dict:
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    spaces = cfg["workspace"]
    state.setdefault("presses", 0)
    state.pop("players", None)  # (state once kept usernames; it doesn't anymore)
    state["log"] = [{k: e[k] for k in ("key", "at", "to") if k in e} for e in state.get("log", [])]
    state.update(clamp_focus(spaces, state))
    if state.get("prev"):
        state["prev"] = clamp_focus(spaces, state["prev"])
    return state


def focused(cfg: dict, focus: dict) -> dict:
    return cfg["workspace"][focus["ws"]]["windows"][focus["win"][focus["ws"]]]


def where(cfg: dict, focus: dict) -> str:
    return f'{cfg["workspace"][focus["ws"]]["key"]}/{focused(cfg, focus)["id"]}'


def press(cfg: dict, title: str, user: str = "") -> str:
    """Apply an "hjkl: <key>" press. `user` is only for the thank-you (empty for anonymous presses
    from worker/): nothing about who pressed is stored."""
    m = re.fullmatch(r"\s*hjkl:\s*(gg|[hjkl])\s*", title, re.IGNORECASE)
    if not m:
        raise ValueError("unknown key: titles look like `hjkl: h` (one of gg, h, j, k, l)")
    key = m.group(1).lower()
    user = user if re.fullmatch(r"[A-Za-z0-9-]{1,39}", user) else ""
    state = load_state(cfg)
    spaces = cfg["workspace"]
    state["prev"] = {"ws": state["ws"], "win": list(state["win"])}
    dws, dwin, action, _ = KEYS[key]
    if key == "gg":
        state["ws"], state["win"][0] = 0, 0
    elif dws:
        state["ws"] = (state["ws"] + dws) % len(spaces)
    else:
        n = len(spaces[state["ws"]]["windows"])
        state["win"][state["ws"]] = (state["win"][state["ws"]] + dwin) % n
    state["presses"] += 1
    target = where(cfg, state)
    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    state["log"] = [{"key": key, "at": now, "to": target}, *state["log"]][:8]
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1) + "\n")
    if cfg.get("garden"):  # every keypress tends the garden too
        garden = load_garden()
        garden["visits"] = [*garden["visits"], {"at": now, "key": key}][-3000:]
        save_garden(garden)
    return (f"`{key}` ({action}): focus is now on **{target}**. "
            + (f"Thanks @{user}! " if user else "Thanks! ") + f"That was keypress #{state['presses']}.")


# ── SVG helpers ──────────────────────────────────────────────────────────────

def esc(s) -> str:
    return html.escape(str(s), quote=True)


def num(v) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


def attrs(**kw) -> str:
    return "".join(f' {k.rstrip("_").replace("_", "-")}="{esc(num(v))}"'
                   for k, v in kw.items() if v is not None)


def text(x, y, content, fill=None, size=12, **kw) -> str:
    """`content` is a string, or a list of (string, color) spans."""
    if isinstance(content, str):
        content = [(content, None)]
    body = "".join(f"<tspan{attrs(fill=c)}>{esc(s)}</tspan>" if c else esc(s) for s, c in content)
    return f"<text{attrs(x=x, y=y, fill=fill or C['fg'], font_size=size, **kw)}>{body}</text>"


def rect(x, y, w, h, **kw) -> str:
    return f"<rect{attrs(x=x, y=y, width=w, height=h, **kw)}/>"


def doc(w, h, body, css="", label="") -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}" role="img" aria-label="{esc(label)}">'
            f"<title>{esc(label)}</title>"
            f"<style>text{{font-family:{FONT};white-space:pre}}{css}</style>"
            f"{''.join(body)}</svg>\n")


GLOW = '<filter id="glow" x="-10%" y="-10%" width="120%" height="120%"><feGaussianBlur stdDeviation="3"/></filter>'


def frame(x, y, w, h, title) -> list[str]:
    """A tiled window with the JankyBorders inactive border."""
    return [rect(x, y, w, h, rx=9, fill=C["panel"], stroke=C["inactive"], stroke_width=1.5),
            text(x + 14, y + 19, title, C["dim"], 11),
            f'<line{attrs(x1=x + 1, y1=y + 28, x2=x + w - 1, y2=y + 28, stroke=C["line"])}/>']


def ring(x, y, w, h, **kw) -> str:
    """The active border, glowing, over a focused window."""
    r = rect(x - 1, y - 1, w + 2, h + 2, rx=10, fill="none", stroke=C["active"], stroke_width=2.5)
    return f'<g{attrs(**kw)}><g filter="url(#glow)" opacity=".7">{r}</g>{r}</g>'


def prompt(x, y, cmd, size=12.5) -> list[str]:
    return [text(x, y, "❯", C["green"], size, font_weight="bold"),
            text(x + 2 * size * CW, y, cmd, C["bright"], size)]


def clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def is_light(color: str) -> bool:
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.299 * r + 0.587 * g + 0.114 * b > 150


def parse_ts(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def keyframes(name: str, prop: str, points: list[tuple[float, str]], period: float) -> str:
    frames = "".join(f"{t / period * 100:.2f}%{{{prop}:{v}}}" for t, v in points)
    return f"@keyframes {name}{{{frames}}}"


def onoff(start: float, end: float, period: float, fade: float = 0.12) -> list[tuple[float, str]]:
    """Opacity points: visible during [start, end) of a looping period."""
    pts = [(0, "1" if start <= 0 else "0")]
    if start > 0:
        pts += [(start, "0"), (start + fade, "1")]
    if end < period:
        pts += [(end, "1"), (end + fade, "0")]
    pts.append((period, "1" if end >= period else "0"))
    return pts


def terminal(x, y, size, lh, lines, delay=None) -> tuple[list[str], str]:
    """A shell session, typed out once on load starting at `delay` seconds (static if None).

    lines: ("cmd", str) | ("out", spans) | ("blank",) | ("cursor",). Each line sits under a
    panel-colored cover that shrinks away. Every cover runs one animation spanning the whole
    intro, with its reveal as keyframes inside it: Chrome samples SVG-in-<img> animations
    coarsely and can miss short standalone ones, but never a keyframe in a running animation.
    """
    out, reveals, t, cw = [], [], 0.4 + (delay or 0), size * CW

    def cover(cx, baseline, w, start, dur, steps):
        if delay is None:
            return
        out.append(rect(cx, baseline - size * 0.95, w, size * 1.35, fill=C["panel"], class_="cv",
                        style=f"animation:tp{len(reveals)} var(--intro) linear forwards"))
        reveals.append((start, dur, steps))

    for i, line in enumerate(lines):
        by = y + i * lh
        kind = line[0]
        if kind in ("cmd", "cursor"):
            out.append(text(x, by, "❯", C["green"], size, font_weight="bold"))
            if kind == "cmd":
                cmd = line[1]
                out.append(text(x + 2 * cw, by, cmd, C["bright"], size))
                dur = len(cmd) * 0.045
                cover(x - 2, by, 2 * cw + 2, t, 0.05, 1)
                cover(x + 2 * cw - 1, by, len(cmd) * cw + 3, t + 0.25, dur, len(cmd))
                t += 0.25 + dur + 0.35
            else:
                out.append(rect(x + 2 * cw, by - size * 0.85, cw, size * 1.1, fill=C["fg"], class_="blink"))
                cover(x - 2, by, 4 * cw, t, 0.05, 1)
        elif kind == "out":
            n = sum(len(s) for s, _ in line[1])
            out.append(text(x, by, line[1], C["fg"], size))
            out.extend(line[2:])  # extra SVG drawn on this line, under its cover
            cover(x - 2, by, n * cw + 6, t, 0.05, 1)
            if i + 1 == len(lines) or lines[i + 1][0] != "out":
                t += 0.45

    if not reveals:
        return out, ""
    intro = t + 0.5
    css = [f":root{{--intro:{intro:.2f}s}}"]
    for k, (start, dur, steps) in enumerate(reveals):
        a, b = start / intro * 100, (start + dur) / intro * 100
        css.append(f"@keyframes tp{k}{{0%,{a:.2f}%{{transform:scaleX(1);animation-timing-function:steps({steps},end)}}"
                   f"{b:.2f}%,100%{{transform:scaleX(0)}}}}")
    return out, "".join(css)


# ── the desktop's windows ────────────────────────────────────────────────────
# Each kind draws its contents inside a frame at (x, y, w, h). `ctx` carries cfg, data,
# state, the focused window's id, `delay` (when the camera has settled) and a shared `css` list.

WINDOWS: dict[str, tuple[str, int, callable]] = {}


def kind(wid: str, title: str, width: int):
    def register(fn):
        WINDOWS[wid] = (title, width, fn)
        return fn
    return register


@kind("zsh", "zsh — ~", 470)
def w_zsh(ctx, x, y, w, h):
    cfg = ctx.cfg
    made = cfg.get("made", [])
    col = max((len(n) for n in made[::2]), default=0) + 3
    ls = [("out", [(f"{n}/".ljust(col), C["blue"]) for n in made[i:i + 2]]) for i in range(0, len(made), 2)]
    size, lh, x0, y0 = 13, 20, x + 20, y + 56
    lines = [("cmd", "whoami"),
             ("out", [(cfg["name"], C["bright"]), ("  @" + cfg["login"], C["active"])]),
             ("out", [(cfg["education"], C["dim"])]),
             ("blank",), ("cmd", "cat now.txt")]
    prefix, phrases = cfg.get("now_prefix", "working on the next") + " ", cfg.get("now", [])
    bad = cfg.get("now_glitch") if cfg.get("now_glitch") in phrases else None
    origin, frames, settle = len(lines), [], 0.5
    if phrases:
        lines.append(("out", [(prefix, None), (" " * max(map(len, phrases)), None)]))
    lines += [("blank",), ("cmd", "ls ~/src"), *ls, ("blank",), ("cursor",)]
    if bad:
        # what the screen looks like with the phrase up, for the crash to tear apart
        rows = [[("❯ ", C["green"]), (ln[1], C["bright"])] if ln[0] == "cmd" else
                [("❯ ", C["green"]), ("█", C["fg"])] if ln[0] == "cursor" else
                ln[1] if ln[0] == "out" else [] for ln in lines]
        rows[origin] = [(prefix, None), (bad, C["active"])]
        frames = crash(x, y, w, h, rows, origin, bad)
    if phrases:
        holds = {bad: settle + sum(s for _, s in frames) + 0.8} if bad else None
        strip, period, starts = morph(x0 + len(prefix) * size * CW, y0 + origin * lh, size, phrases, ctx.css, holds)
        lines[origin] += (strip,)
    out, css = terminal(x0, y0, size, lh, lines, ctx.delay if ctx.focus == "zsh" else None)
    ctx.css.append(css)
    if bad:
        out.append(overlay(x, y, w, h, frames, period, starts[phrases.index(bad)] + settle, ctx.css, "crash"))
    return out


@kind("atv", "atv-remote — appletv-remote-tui", 470)
def w_atv(ctx, x, y, w, h):
    """appletv-remote-tui on a loop: a few REMOTE keys, then TEXT INSERT typing a search, then back."""
    T = 12.0

    def show(name, *spans):
        pts = [(0, "1" if spans[0][0] == 0 else "0")]
        for a, b in spans:
            if a > 0:
                pts += [(a, "0"), (a + 0.1, "1")]
            if b < T:
                pts += [(b, "1"), (b + 0.1, "0")]
        pts.append((T, "1" if spans[-1][1] >= T else "0"))
        ctx.css.append(keyframes(name, "opacity", pts, T) + f".{name}{{animation:{name} {T}s linear infinite}}")
        return name

    def chips(mode, fill):
        right, out = x + w - 16, []
        for label, bg, ink in [(mode, fill, C["wall"]), ("CONNECTED", "#1f2a33", C["fg"])]:
            cw = (len(label) + 2) * 11 * CW
            out += [rect(right - cw, y + 43, cw, 18, rx=3, fill=bg),
                    text(right - cw / 2, y + 56, label, ink, 11, text_anchor="middle", font_weight="bold")]
            right -= cw + 6
        return "".join(out)

    b, d = C["bright"], C["dim"]
    out = [text(x + 18, y + 56, [("Bedroom", C["bright"]), (" · 192.168.1.57", C["fg"])], None, 12.5,
                font_weight="bold")]
    for i, spans in enumerate([
            [("h j k l", b), ("  navigate    ", d), ("⏎", b), ("  select    ", d), (";", b), ("  hold next", d)],
            [("H J K L", b), ("  swipe       ", d), ("space", b), ("  play/pause   ", d), ("g", b), ("  home", d)],
            [("esc", b), ("  back    ", d), ("i", b), ("  edit text    ", d), ("d", b), ("  devices    ", d),
             ("?", b), ("  help", d)]]):
        out.append(text(x + 18, y + 90 + i * 18, spans, None, 11.5))
    out.append(text(x + 18, y + 154, "Available: navigation · swipe · back · home · playback", C["dim"], 11))
    out.append(text(x + 18, y + 176, "Keyboard: focused · text synchronized", C["dim"], 11))
    box = (x + 18, y + 188, w - 36, 34)

    remote = [chips("REMOTE", C["green"]), rect(*box, rx=3, fill=C["raised"], stroke=C["line"]),
              text(x + 30, y + 210, "Focus a text field on Apple TV, then press i", C["mute"], 12),
              text(x + 18, y + h - 20, "REMOTE · ; hold next (1 second) · i edit TV text", C["mute"], 11)]
    out.append(f'<g class="{show("atv-r", (0, 4.5), (10.5, T))}">{"".join(remote)}</g>')

    cw = 12.5 * CW
    insert = [chips("TEXT INSERT", C["active"]),
              rect(*box, rx=3, fill=C["raised"], stroke=C["blue"], stroke_width=1.5),
              text(x + 30, y + 210, "severance", C["bright"], 12.5),
              rect(x + 30 + 9 * cw, y + 200, cw, 14, fill=C["fg"], class_="blink"),
              rect(x + 28, y + 196, 10 * cw + 4, 18, fill=C["raised"], class_="cv atvt"),
              text(x + 18, y + 252, "Editing Apple TV text", C["fg"], 12),
              text(x + 18, y + h - 20, "TEXT INSERT · type normally · Esc normal · Enter sync + select",
                   C["mute"], 11)]
    ctx.css.append("@keyframes atvt{0%,41.67%{transform:scaleX(1);animation-timing-function:steps(10,end)}"
                   f"51.67%,100%{{transform:scaleX(0)}}}}.atvt{{animation:atvt {T}s linear infinite}}")
    out.append(f'<g class="{show("atv-i", (4.5, 10.5))}">{"".join(insert)}</g>')

    for i, (msg, a, z) in enumerate([("Connected to Bedroom", 0, 1.5), ("Sent right", 1.5, 3.0),
                                     ("Sent select", 3.0, 4.5), ("Synced text · sent select", 10.5, T)]):
        out.append(text(x + 18, y + 252, msg, C["fg"], 12, class_=show(f"atv-s{i}", (a, z))))
    return out


@kind("nsk", "nsk — native-space-kit", 500)
def w_nsk(ctx, x, y, w, h):
    """native-space-kit's CLI on a loop, with Mission Control above it following along: a Desktop is
    created, switched to and handed a window, all with SIP on, then cleaned up with real commands
    (destroy refuses the active Desktop, and --migrate sends the window home). The workspaces in
    profile.toml stand in for the Desktops, each with as many tiles as it has windows."""
    size, lh = 12, 17
    cw, x0 = size * CW, x + 18
    spaces = ctx.cfg["workspace"]
    ids, new, win = [5, 9, 12, 17, 23, 28][:len(spaces)], 118, 3147

    def jq(s):  # jq's colors: keys blue, numbers yellow, booleans green
        return [(m[0], C["blue2"] if m[0][0] == '"' else C["yellow"] if m[0].isdigit() else
                 C["green"] if m[0] in ("true", "false") else C["fg"])
                for m in re.finditer(r'"\w+"|\d+|true|false|[^"\w]+', s)]

    # the session: two screens of (command, output), the second after a clear
    t, pages, at = 0.6, [], {}
    for screen in [[("list", "nsk list | jq -c '[.spaces[].id]'", jq(json.dumps(ids, separators=(",", ":")))),
                    ("create", "nsk create | jq .created.id", jq(str(new))),
                    ("activate", f"nsk activate {new} | jq .activated.active", jq("true")),
                    ("move", f"nsk move-window {win} {new}",
                     jq(f'{{"space_id":{new},"space_ids":[{new}],"window_id":{win}}}')),
                    ("sip", "csrutil status", [("System Integrity Protection status: ", C["fg"]), ("enabled.", C["green"])])],
                   [("cleanup", f"nsk activate {ids[0]} >/dev/null && nsk destroy {new} --migrate", jq(f'{{"destroyed":{new}}}'))]]:
        lines = []
        for key, cmd, output in screen:
            lines.append((cmd, t))
            t += 0.15 + len(cmd) * 0.045 + 0.25  # typed, then Enter
            lines.append((output, t))
            at[key] = t
            t += 0.6
        t += 1.2 if pages else 1.0  # (then it clears, or loops)
        pages.append((lines, t))
    T = t

    hidden = ' opacity="0"'  # where reduced motion leaves the loop: the first screen, nothing created yet

    def show(name, *spans):  # visible during each (start, end) span of the loop; returns the class
        pts = [(0, "0")] + [p for a, b in spans for p in ((a, "1"), (b, "0"))] + [(T, "0")]
        ctx.css.append(keyframes(f"nsk-{name}", "opacity", pts, T) + f".nsk-{name}{{animation:nsk-{name} {T:.2f}s step-end infinite}}")
        return f"nsk-{name}"

    # Mission Control: a thumbnail per Desktop, then Mission Control's "+" where the new one goes
    slots = len(spaces) + 1
    tw = min(72, (w - 48 - (slots - 1) * 10) / slots)
    th, sx0, ty = 44, x + (w - slots * tw - (slots - 1) * 10) / 2, y + 42
    sx = lambda i: sx0 + i * (tw + 10)
    out = [rect(x + 1.5, y + 29, w - 3, 92, fill=C["bg"])]

    def tiles(i, n, skip=None):
        tile = (tw - 10 - (n - 1) * 3) / n
        return [rect(round(sx(i) + 5 + k * (tile + 3), 1), ty + 6, round(tile, 1), th - 12, rx=1.5,
                     fill=C["inactive"]) for k in range(n) if k != skip]

    for i, space in enumerate(spaces):
        out += [rect(round(sx(i), 1), ty, round(tw, 1), th, rx=4, fill=C["wall"], stroke=C["line"])]
        out += tiles(i, len(space["windows"]), skip=len(space["windows"]) - 1 if i == 0 else None)
        out.append(text(round(sx(i) + tw / 2, 1), ty + th + 15, space["key"], C["dim"], 10.5, text_anchor="middle"))
    last = sx(slots - 1)
    plus = [rect(round(last, 1), ty, round(tw, 1), th, rx=4, fill="none", stroke=C["mute"], stroke_dasharray="4 3"),
            text(round(last + tw / 2, 1), ty + th / 2 + 6, "+", C["mute"], 16, text_anchor="middle")]
    made = [rect(round(last, 1), ty, round(tw, 1), th, rx=4, fill=C["wall"], stroke=C["line"]),
            text(round(last + tw / 2, 1), ty + th + 15, f"Desktop {slots}", C["dim"], 10.5, text_anchor="middle")]
    gone = at["cleanup"] + 0.5
    out.append(f'<g class="{show("plus", (0, at["create"]), (gone, T))}">{"".join(plus)}</g>')
    out.append(f'<g class="{show("made", (at["create"], gone))}"{hidden}>{"".join(made)}</g>')
    for i, spans in [(0, [(0, at["activate"]), (at["cleanup"], T)]), (slots - 1, [(at["activate"], at["cleanup"])])]:
        outline = ring(round(sx(i), 1), ty, round(tw, 1), th)
        out.append(f'<g class="{show(f"on{i}", *spans)}"{hidden if i else ""}>{outline}</g>')

    # window 3147: the last tile on ~, flown to the new Desktop, where it's alone and fills it, and back
    n0 = len(spaces[0]["windows"])
    tile = (tw - 10 - (n0 - 1) * 3) / n0
    home = sx(0) + 5 + (n0 - 1) * (tile + 3)
    still, away = "translate(0,0) scaleX(1)", f"translate({last + 5 - home:.1f}px,0) scaleX({(tw - 10) / tile:.2f})"
    fly = [(0, still), (at["move"], still), (at["move"] + 0.35, away),
           (at["cleanup"] + 0.15, away), (at["cleanup"] + 0.5, still), (T, still)]
    ctx.css.append(keyframes("nsk-fly", "transform", fly, T)
                   + f".nsk-fly{{transform-box:fill-box;transform-origin:0 50%;animation:nsk-fly {T:.2f}s linear infinite}}")
    out.append(rect(round(home, 1), ty + 6, round(tile, 1), th - 12, rx=1.5, fill=C["active"], class_="nsk-fly"))

    # the terminal, a screen at a time; commands typed behind a shrinking cover
    y0 = y + 148
    for p, (lines, end) in enumerate(pages):
        for i, (content, t0) in enumerate(lines):
            by, name = y0 + i * lh, f"l{p}{i}"
            if isinstance(content, str):  # a command
                n = len(content)
                cover = rect(round(x0 + 2 * cw - 1, 1), by - size * 0.95, round(n * cw + 3, 1), size * 1.35,
                             fill=C["panel"], class_=f"cv nsk-t{p}{i}")
                a, b = (t0 + 0.15) / T * 100, (t0 + 0.15 + n * 0.045) / T * 100
                ctx.css.append(f"@keyframes nsk-t{p}{i}{{0%,{a:.2f}%{{transform:scaleX(1);animation-timing-function:steps({n},end)}}"
                               f"{b:.2f}%,100%{{transform:scaleX(0)}}}}.nsk-t{p}{i}{{animation:nsk-t{p}{i} {T:.2f}s linear infinite}}")
                body = text(x0, by, [("❯ ", C["green"]), (content, C["bright"])], None, size) + cover
            else:
                body = text(x0, by, content, None, size)
            out.append(f'<g class="{show(name, (t0, end))}"{hidden if p else ""}>{body}</g>')
        by = y0 + len(lines) * lh
        prompt_ = text(x0, by, "❯", C["green"], size) + rect(round(x0 + 2 * cw, 1), by - size * 0.85, round(cw, 1),
                                                             size * 1.1, fill=C["fg"], class_="blink")
        out.append(f'<g class="{show(f"c{p}", (lines[-1][1] + 0.3, end))}"{hidden if p else ""}>{prompt_}</g>')
    return out


SCRAMBLE = "#%&*+=<>/\\|~^$@!?"


def morph(x, y, size, phrases, css, holds=None, name="now") -> tuple[str, float, list[float]]:
    """Cycle through phrases with an ASCII decode: a band of scrambled glyphs sweeps left to
    right, leaving the next phrase behind it. Every frame is one line of a vertical strip under a
    one-line clip, and a single step-end animation slides the strip, so no frame needs its own
    animation (Chrome can skip short ones in an <img>).

    Returns the SVG, the loop length, and when each phrase settles within the loop. `holds`
    gives some phrases a longer stay, in seconds."""
    rng = random.Random(len(phrases))  # seeded: the same profile always renders the same SVG
    hold, step, steps = 1.4, 0.045, 7
    frames, starts = [], []  # frames: (spans, seconds on screen)
    for i, a in enumerate(phrases):
        b = phrases[(i + 1) % len(phrases)]
        starts.append(sum(d for _, d in frames))
        frames.append(([(a, C["active"])], (holds or {}).get(a, hold)))
        width = max(len(a), len(b))
        # when each column settles on b, in 0..1: mostly left to right, a little ragged
        settle = [0.15 + j / max(width - 1, 1) * 0.55 + rng.random() * 0.15 for j in range(width)]
        for k in range(1, steps + 1):
            p, spans = k / steps, []
            for j in range(width):
                if p >= settle[j]:
                    ch, color = (b[j] if j < len(b) else " "), C["active"]
                elif p >= settle[j] - 0.35:
                    ch, color = rng.choice(SCRAMBLE), C["green"]
                else:
                    ch, color = (a[j] if j < len(a) else " "), C["active"]
                if spans and spans[-1][1] == color:
                    spans[-1] = (spans[-1][0] + ch, color)
                else:
                    spans.append((ch, color))
            frames.append((spans, step))
    lh, total = size * 1.8, sum(d for _, d in frames)
    stops, t = [], 0.0
    for i, (_, d) in enumerate(frames):
        stops.append(f"{t / total * 100:.3f}%{{transform:translateY({-i * lh:.1f}px)}}")
        t += d
    stops.append(f"100%{{transform:translateY({-(len(frames) - 1) * lh:.1f}px)}}")
    css.append(f"@keyframes {name}{{{''.join(stops)}}}.{name}{{animation:{name} {total:.2f}s step-end infinite}}")
    width = max(map(len, phrases)) * size * CW + 4
    lines = "".join(text(x, y + i * lh, spans, None, size) for i, (spans, _) in enumerate(frames))
    return (f'<clipPath id="clip-{name}">{rect(x - 2, y - size, width, size * 1.4)}</clipPath>'
            f'<g clip-path="url(#clip-{name})"><g class="{name}">{lines}</g></g>'), total, starts


GLITCH = "0123456789abcdef#%&*<>/\\|~^$@!?▓▒░"


def crash(x, y, w, h, rows, origin, phrase) -> list[tuple[str, float]]:
    """Frames of the zsh pane falling apart: corruption spreads out from row `origin`, the
    window melts down, turns into a hex dump of itself, then reboots. Each frame is (svg, seconds)
    and redraws the whole pane, title bar included, over the real one."""
    rng = random.Random(7)
    x0, y0 = x + 20, y + 56
    hues = [C["red"], C["magenta"], C["green2"], C["blue2"], C["yellow"]]

    def corrupt(spans, q):
        if q <= 0:
            return spans
        flip = lambda s: "".join(rng.choice(GLITCH) if rng.random() < (q if ch != " " else q / 4) else ch
                                 for ch in s)
        if q < 0.5:  # light damage keeps each span's color
            return [(flip(s), c) for s, c in spans]
        return [(flip("".join(s for s, _ in spans)), rng.choice(hues))]

    def screen(title, rows_, size=13, lh=20, jitter=0.0, split=0.0, bars=0, dy=0):
        out = [rect(x + 1.5, y + 1.5, w - 3, h - 3, rx=8, fill=C["panel"]),
               text(x + 14, y + 19, title if isinstance(title, list) else [(title, C["dim"])], None, 11),
               f'<line{attrs(x1=x + 1, y1=y + 28, x2=x + w - 1, y2=y + 28, stroke=C["line"])}/>']
        for i, spans in enumerate(rows_):
            if not spans:
                continue
            dx = rng.uniform(-jitter, jitter) if jitter and rng.random() < 0.5 else 0
            by = y0 + dy + i * lh
            if split and rng.random() < split:  # RGB split: red and cyan ghosts either side
                plain = "".join(s for s, _ in spans)
                out.append(text(x0 + dx - 2.5, by, plain, C["red"], size, opacity=".6"))
                out.append(text(x0 + dx + 2.5, by, plain, C["blue2"], size, opacity=".6"))
            out.append(text(x0 + dx, by, spans, None, size))
        for _ in range(bars):
            out.append(rect(x + 1.5, y + rng.uniform(32, h - 20), w - 3, rng.choice([2, 3, 6, 11]),
                            fill=rng.choice(hues), opacity=f"{rng.uniform(0.25, 0.7):.2f}"))
        return "".join(out)

    title = [("zsh — ~", C["dim"])]
    frames = []
    for k in range(1, 7):  # infection, spreading out from the phrase
        rows_k = [corrupt(r, min(1.0, 0.2 * k - 0.13 * abs(i - origin))) for i, r in enumerate(rows)]
        frames.append((screen(corrupt(title, 0.5) if k > 3 else title, rows_k, jitter=k, split=0.06 * k), 0.08))
    for k in range(7):  # meltdown
        rows_k = [corrupt(r or [(" " * rng.randint(8, 48), None)], 0.55 + 0.05 * k) for r in rows]
        frames.append((screen(corrupt(title, 0.8), rows_k, jitter=16, split=0.5, bars=rng.randint(1, 3),
                              dy=rng.choice([0, 0, -4, 3])), 0.06))

    # the pane reads its own memory: an xxd dump of what it was showing, scrolling to the phrase on repeat
    data = ("\n".join("".join(s for s, _ in r) for r in rows) + "\n" + (phrase + " ") * 4).encode()
    per, shown = 12, 14

    def xxd(start):
        out = []
        for li in range(start, min(start + shown, -(-len(data) // per))):
            chunk = data[li * per:(li + 1) * per]
            hexes = " ".join(chunk[i:i + 2].hex() for i in range(0, len(chunk), 2)).ljust(per // 2 * 5 - 1)
            ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            out.append([(f"{li * per:08x}: ", C["mute"]), (hexes, C["green"]), ("  " + ascii_, C["fg"])])
        return out

    last = max(0, -(-len(data) // per) - shown)
    hexdump = [("xxd /proc/self/mem", C["dim"])]
    for start, secs, q in [(0, 0.3, 0), (0, 0.06, 0.35), (last // 3, 0.25, 0), (2 * last // 3, 0.25, 0),
                           (2 * last // 3, 0.06, 0.4), (last, 0.45, 0)]:
        rows_k = [corrupt(r, q) for r in xxd(start)]
        frames.append((screen(hexdump, rows_k, size=12, lh=19, jitter=8 if q else 0, split=0.5 if q else 0,
                              bars=1 if q else 0), secs))

    frames.append((screen(title, [[("█", C["fg"])]]), 0.3))  # reboot: an empty shell
    for q in (0.45, 0.2, 0.06):
        frames.append((screen(title, [corrupt(r, q) for r in rows], jitter=8 * q, split=q), 0.07))
    return frames


def overlay(x, y, w, h, frames, period, start, css, name) -> str:
    """Show `frames` over a pane from `start` seconds into a looping `period`, and nothing
    otherwise. Same strip trick as morph(): one step-end animation, frame 0 left empty."""
    pitch = h + 40
    stops, t = ["0%{transform:translateY(0)}"], start
    for i, (_, secs) in enumerate(frames, 1):
        stops.append(f"{t / period * 100:.3f}%{{transform:translateY({-i * pitch}px)}}")
        t += secs
    stops += [f"{t / period * 100:.3f}%{{transform:translateY(0)}}", "100%{transform:translateY(0)}"]
    css.append(f"@keyframes {name}{{{''.join(stops)}}}.{name}{{animation:{name} {period:.2f}s step-end infinite}}")
    strip = "".join(f'<g transform="translate(0 {i * pitch})">{svg}</g>' for i, (svg, _) in enumerate(frames, 1))
    return (f'<clipPath id="clip-{name}">{rect(x, y, w, h, rx=9)}</clipPath>'
            f'<g clip-path="url(#clip-{name})"><g class="{name}">{strip}</g></g>')


def logo(x, y) -> list[str]:
    """fastfetch's logo slot: a tiny paneru desktop, with the next workspace peeking below."""
    out = [rect(x, y, 120, 10, rx=3, fill=C["raised"]), rect(x + 4, y + 2.5, 14, 5, rx=2, fill=C["active"]),
           rect(x + 21, y + 2.5, 10, 5, rx=2, fill=C["mute"]), rect(x + 34, y + 2.5, 10, 5, rx=2, fill=C["mute"])]
    for cx, cw, focus in [(0, 30, False), (36, 54, True), (96, 24, False)]:
        out.append(rect(x + cx, y + 16, cw, 118, rx=4, fill=C["bg"], stroke=C["active" if focus else "inactive"],
                        stroke_width=2 if focus else 1.5))
    for i, (w, color) in enumerate([(34, "magenta"), (24, "blue2"), (40, "green"), (18, "yellow"),
                                    (30, "blue2"), (22, "green"), (36, "fg"), (14, "cyan")]):
        out.append(rect(x + 43, y + 26 + i * 13, w, 5, rx=2, fill=C[color]))
    for i, w in enumerate([16, 10, 18, 12, 14, 8]):
        out.append(rect(x + 6, y + 26 + i * 13, w, 5, rx=2, fill=C["mute"]))
        out.append(rect(x + 101, y + 26 + i * 13, min(w, 13), 5, rx=2, fill=C["mute"]))
    below = rect(x, y + 142, 52, 44, rx=4, fill=C["bg"], stroke=C["inactive"], stroke_width=1.5) + \
        rect(x + 58, y + 142, 62, 44, rx=4, fill=C["bg"], stroke=C["inactive"], stroke_width=1.5)
    out.append(f'<g opacity=".35">{below}</g>')
    out.append(text(x + 60, y + 214, [("h ", C["active"]), ("j ", C["active"]), ("k ", C["active"]),
                                      ("l", C["active"])], None, 13, text_anchor="middle", font_weight="bold"))
    return out


@kind("fastfetch", "fastfetch", 450)
def w_fastfetch(ctx, x, y, w, h):
    ff = ctx.cfg.get("fastfetch", {})
    host = ff.get("host", "me@mac")
    user, _, machine = host.partition("@")
    out = logo(x + 24, y + 54)
    tx = x + 170
    out.append(text(tx, y + 58, [(user, C["active"]), ("@", C["fg"]), (machine, C["active"])], None, 12.5,
                    font_weight="bold"))
    out.append(text(tx, y + 76, "─" * len(host), C["dim"], 12.5))
    rows = ff.get("rows", [])[:10]
    for i, (k, v) in enumerate(rows):
        out.append(text(tx, y + 96 + i * 18, [(k.ljust(10), C["active"]), (v, C["fg"])], None, 12))
    by = y + 96 + len(rows) * 18 + 2
    for i, color in enumerate(PALETTE):
        out.append(rect(tx + (i % 8) * 30, by + (i // 8) * 16, 30, 16, fill=color))
    return out


PANERU_TOML = [
    [("[bindings]", "magenta")],
    *[[(f"window_focus_{d}".ljust(18), "blue2"), (" = ", "dim"), (f'"alt + shift + cmd + ctrl - {k}"', "green")]
      for d, k in [("west", "h"), ("south", "j"), ("north", "k"), ("east", "l")]],
    [("window_swap_west".ljust(18), "blue2"), (" = ", "dim"), ('"alt - h"', "green")],
    [("window_swap_east".ljust(18), "blue2"), (" = ", "dim"), ('"alt - l"', "green")],
    [("window_center".ljust(18), "blue2"), (" = ", "dim"), ('"alt + shift + cmd + ctrl - c"', "green")],
    [],
    [("[options]", "magenta")],
    [("preset_column_widths", "blue2"), (" = ", "dim"), ("[0.25, 0.33, 0.50, 0.66, 0.75]", "yellow")],
    [("animation_speed", "blue2"), (" = ", "dim"), ("55", "yellow")],
    [("sliver_width", "blue2"), (" = ", "dim"), ("7", "yellow")],
]


def vim_status(x, y, w, h, name, ft) -> list[str]:
    sy = y + h - 26
    return [rect(x + 1.5, sy, w - 3, 20, fill=C["raised"]), rect(x + 1.5, sy, 60, 20, fill=C["active"]),
            text(x + 31.5, sy + 14, "NORMAL", C["wall"], 11, text_anchor="middle", font_weight="bold"),
            text(x + 70, sy + 14, name, C["fg"], 11),
            text(x + w - 10, sy + 14, ft, C["dim"], 11, text_anchor="end")]


def vim_lines(x, y, lines, rows, lh=16, size=11.5) -> list[str]:
    out = []
    for i in range(rows):
        by = y + i * lh
        if i < len(lines):
            out.append(text(x + 24, by, str(i + 1), C["mute"], size, text_anchor="end"))
            if lines[i]:
                out.append(text(x + 32, by, [(s, C[c]) for s, c in lines[i]], None, size))
        else:
            out.append(text(x + 14, by, "~", C["mute"], size))
    return out


@kind("nvim", "nvim — ~/.config/paneru/paneru.toml", 470)
def w_nvim(ctx, x, y, w, h):
    lh, top = 16, y + 52
    ctx.css.append(keyframes("cl", "transform", [(i, f"translateY({p * lh}px)") for i, p in
                                                 enumerate([0, 1, 2, 3, 2, 1, 0])], 6)
                   + ".cursorline{animation:cl 4.8s steps(1,end) infinite}")
    out = [f'<g class="cursorline">{rect(x + 1.5, top + lh - 11.5, w - 3, lh, fill=C["raised"])}</g>']
    out += vim_lines(x, top, PANERU_TOML, (h - 84) // lh, lh)
    out += vim_status(x, y, w, h, "paneru.toml", "toml  2:1")
    return out


@kind("ghostty", "ghostty — Citruszest", 450)
def w_ghostty(ctx, x, y, w, h):
    out = prompt(x + 18, y + 54, "ghostty +show-config")
    for i, (k, v) in enumerate([("theme", "Citruszest"), ("background-opacity", "0.87"),
                                ("background-blur-radius", "20"), ("macos-titlebar-style", "hidden")]):
        out.append(text(x + 18, y + 76 + i * 18, [(k, C["blue2"]), (" = ", C["dim"]),
                                                  (v, C["yellow" if v[0].isdigit() else "green"])], None, 12))
    sw, sh = 46, 30
    for i, color in enumerate(PALETTE):
        sx, sy = x + 20 + (i % 8) * (sw + 6), y + 166 + (i // 8) * 62
        out.append(rect(sx, sy, sw, sh, rx=4, fill=color))
        out.append(text(sx + sw / 2, sy + 20, str(i), C["wall"] if is_light(color) else C["bright"], 11,
                        text_anchor="middle", font_weight="bold"))
        out.append(text(sx + sw / 2, sy + sh + 14, color[1:], C["dim"], 9, text_anchor="middle"))
    cx = x + 20
    for label, color in [("bg", "#121212"), ("fg", "#bfbfbf"), ("cursor", "#666666"), ("selection", "#ff8c00")]:
        out.append(rect(cx, y + 299, 10, 10, rx=2, fill=color, stroke=C["line"]))
        out.append(text(cx + 15, y + 308, [(label, C["dim"]), (" " + color[1:], C["fg"])], None, 10))
        cx += 15 + (len(label) + 7) * 10 * CW + 14
    return out


@kind("borders", "nvim — ~/.config/borders/bordersrc", 420)
def w_borders(ctx, x, y, w, h):
    src = [[("options", "blue2"), ("=(", "fg")],
           [("    style", "blue2"), ("=", "dim"), ("round", "green")],
           [("    width", "blue2"), ("=", "dim"), ("6", "yellow")],
           [("    hidpi", "blue2"), ("=", "dim"), ("off", "green")],
           [("    active_color", "blue2"), ("=", "dim"), ("0xfff77f00", "yellow")],
           [("    inactive_color", "blue2"), ("=", "dim"), ("0xff3a5a40", "yellow")],
           [(")", "fg")],
           [("borders", "magenta"), (' "${options[@]}"', "green")]]
    top = y + 52
    out = vim_lines(x, top, src, 9)
    for i, color in [(4, C["active"]), (5, C["inactive"])]:
        out.append(rect(x + 32 + 31 * 11.5 * CW, top + i * 16 - 9, 10, 10, rx=2, fill=color))
    # the two colors, on two little windows
    iy = y + 214
    for wx, ww, label, focus in [(x + 24, 170, "active", True), (x + 206, 190, "inactive", False)]:
        color = C["active"] if focus else C["inactive"]
        win = rect(wx, iy, ww, 72, rx=6, fill=C["bg"], stroke=color, stroke_width=2.5 if focus else 1.5)
        if focus:
            out.append(f'<g filter="url(#glow)" opacity=".6">{win}</g>')
        out.append(win)
        for i, lw in enumerate([0.7, 0.45, 0.6]):
            out.append(rect(wx + 12, iy + 16 + i * 16, (ww - 24) * lw, 5, rx=2,
                            fill=C["fg"] if focus else C["mute"], opacity=".5" if focus else None))
        out.append(text(wx + ww / 2, iy + 94, label, color if focus else C["green"], 11.5, text_anchor="middle"))
    return out


def key_shape(x, y, w, h, label, sub, color, hot) -> list[str]:
    stroke = C[color] if color else "#333"
    out = [rect(x, y + 2, w, h, rx=5, fill="#070707"),
           rect(x, y, w, h, rx=5, fill=C["raised"], stroke=stroke, stroke_width=1.5 if color else 1)]
    if hot:
        out.append(rect(x, y, w, h, rx=5, fill=C["active"], class_="hot"))
    ink = C["bright"] if color else C["dim"]
    if sub:
        out.append(text(x + w / 2, y + 14, label, ink, 11, text_anchor="middle"))
        out.append(text(x + w / 2, y + 26, sub, C["bright"] if hot else stroke, 9, text_anchor="middle"))
    else:
        out.append(text(x + w / 2, y + 20, label, ink, 11, text_anchor="middle"))
    return out


@kind("keyboard", "karabiner — hyper layer", 540)
def w_keyboard(ctx, x, y, w, h):
    u, gap, kh = 34, 4, 32
    hot = ctx.state["log"][0]["key"][0] if ctx.state["log"] else None  # (gg lights up g)
    special = {"tab": ("hyper", "active"), "caps": ("⌃ · esc", "green"), "a": ("", "green"), "s": ("", "green"),
               "h": ("←", "active"), "j": ("↓", "active"), "k": ("↑", "active"), "l": ("→", "active")}
    rows = [("tab", 1.5, "qwertyuiop"), ("caps", 1.75, "asdfghjkl;"), ("shift", 2.25, "zxcvbnm,./")]
    x0 = x + (w - max(m * u + len(keys) * (u + gap) for _, m, keys in rows)) / 2
    ctx.css.append(".hot{animation:hot 1.4s ease-in-out infinite}@keyframes hot{0%,100%{opacity:.8}50%{opacity:.3}}")
    out = []
    for r, (mod, mw, keys) in enumerate(rows):
        cx, ky = x0, y + 50 + r * (kh + 7)
        for label, kw in [(mod, mw * u), *((k, u) for k in keys)]:
            sub, color = special.get(label, ("", None))
            out += key_shape(cx, ky, kw, kh, label, sub, color, label == hot)
            cx += kw + gap
    legend = [("hold tab", [("hyper  ", C["fg"]), ("⌃⌥⇧⌘", C["active"])]),
              ("hyper + hjkl", [("paneru focus west · south · north · east", C["fg"])]),
              ("alt + hjkl", [("swap windows", C["fg"])]),
              ("caps", [("ctrl when held, esc when tapped", C["fg"])]),
              ("a + s", [("⌃⌘ when held together", C["fg"])])]
    for i, (k, spans) in enumerate(legend):
        out.append(text(x + 28, y + 196 + i * 22, [(k.ljust(14), C["active"]), *spans], None, 12))
    return out


@kind("herdr", "herdr — agents", 470)
def w_herdr(ctx, x, y, w, h):
    out = [text(x + 18, y + 54, [("AGENT".ljust(10), C["dim"]), ("TASK".ljust(26), C["dim"]), ("STATUS", C["dim"])],
                None, 11)]
    agents = [("explore", "trace focus_window calls", "working", "yellow"),
              ("review", "check native Space cmds", "idle", "dim"),
              ("plan", "hjkl desktop for README", "done", "green"),
              ("test", "cargo test --workspace", "working", "yellow")]
    for i, (name, task, status, color) in enumerate(agents):
        ay, color = y + 80 + i * 22, C[color]
        out.append(f'<circle{attrs(cx=x + 22, cy=ay - 4, r=4, fill=color, class_="pulse" if status == "working" else None)}/>')
        out.append(text(x + 34, ay, [(name.ljust(10), C["bright"]), (task.ljust(26), C["fg"])], None, 11))
        sx = x + 34 + 36 * 11 * CW
        if status == "working":
            out.append(f'<circle{attrs(cx=sx + 5, cy=ay - 4, r=5, fill="none", stroke=color, stroke_width=1.8, stroke_dasharray="20 12", class_="spin")}/>')
        elif status == "done":
            out.append(text(sx, ay, "✓", color, 11))
        out.append(text(sx + 16, ay, status, color, 11))
    out.append(f'<line{attrs(x1=x + 16, y1=y + 172, x2=x + w - 16, y2=y + 172, stroke=C["line"], stroke_dasharray="3 4")}/>')
    stream = [("explore", "rg -n focus_window src/", "fg"), ("explore", "14 matches in 5 files", "dim"),
              ("test", "running 128 tests", "fg"), ("test", "✓ 128 passed", "green")]
    period = 9
    for i, (who, line, color) in enumerate(stream):
        ctx.css.append(keyframes(f"st{i}", "opacity", onoff(0.8 + i * 1.4, 8.2, period), period)
                       + f".st{i}{{animation:st{i} {period}s linear infinite}}")
        out.append(text(x + 18, y + 198 + i * 20, [(who.ljust(8), C["active"]), ("▸ ", C["mute"]), (line, C[color])],
                        None, 11.5, class_=f"st{i}"))
    by = y + h - 26
    out.append(rect(x + 1.5, by, w - 3, 20, fill=C["raised"]))
    bx = x + 8
    for i, tab in enumerate(["1 paneru", "2 nsk", "3 profile"]):
        tw = (len(tab) + 2) * 11 * CW
        if i == 0:
            out.append(rect(bx, by + 3, tw, 14, rx=3, fill=C["active"]))
        out.append(text(bx + tw / 2, by + 14, tab, C["wall"] if i == 0 else C["dim"], 11, text_anchor="middle"))
        bx += tw + 4
    out.append(text(x + w - 10, by + 14, "2 working · 1 idle", C["dim"], 11, text_anchor="end"))
    return out


@kind("entropy", "omp — subagent-entropy", 420)
def w_entropy(ctx, x, y, w, h):
    out = prompt(x + 18, y + 54, "/subagent-entropy")
    out.append(text(x + 18, y + 84, [("MODEL".ljust(12), C["dim"]), ("WEIGHT", C["dim"])], None, 11))
    out.append(text(x + w - 18, y + 84, [("mode ", C["dim"]), ("weighted", C["active"])], None, 11, text_anchor="end"))
    ctx.css.append(keyframes("pick", "transform", [(i, f"translateY({p * 22}px)") for i, p in
                                                   enumerate([0, 1, 0, 2, 0, 0, 1, 0])], 7)
                   + ".pick{animation:pick 5.6s steps(1,end) infinite}")
    out.append(f'<g class="pick">{rect(x + 10, y + 95, w - 20, 22, rx=4, fill=C["raised"])}'
               f'{text(x + 18, y + 110, "▸", C["active"], 12)}</g>')
    for i, (model, weight, color) in enumerate([("opus", 0.5, "magenta"), ("sonnet", 0.3, "blue2"),
                                                ("haiku", 0.2, "cyan")]):
        my = y + 110 + i * 22
        out.append(text(x + 32, my, model, C["fg"], 12))
        out.append(rect(x + 120, my - 9, 220, 9, rx=3, fill=C["line"]))
        out.append(rect(x + 120, my - 9, 220 * weight, 9, rx=3, fill=C[color]))
        out.append(text(x + w - 18, my, f"{weight:.0%}", C["dim"], 12, text_anchor="end"))
    for i, line in enumerate(["each subagent spawn draws a model by weight;",
                              "the rest of the pool stays as fallback."]):
        out.append(text(x + 18, y + 204 + i * 18, line, C["fg"], 11.5))
    out.append(text(x + 18, y + h - 20, "$ omp plugin install github:Fjx-dylanZ/omp-subagent-entropy",
                    C["green"], 10.5))
    return out


def heatmap(x, y, weeks, cell=11, gap=3) -> list[str]:
    peak = max((c for w in weeks for c in w), default=0) or 1
    out = []
    for col, week in enumerate(weeks):
        for row, count in enumerate(week):
            level = 0 if count == 0 else 1 + min(3, int(count / peak * 4 - 1e-9))
            out.append(rect(x + col * (cell + gap), y + row * (cell + gap), cell, cell, rx=2.5, fill=HEAT[level]))
    return out


def streaks(days: list[int]) -> tuple[int, int]:
    """(current, longest) runs of days with contributions. A quiet today doesn't break the current run."""
    longest = run = 0
    for c in days:
        run = run + 1 if c else 0
        longest = max(longest, run)
    current = 0
    for c in reversed(days[:-1] if days and not days[-1] else days):
        if not c:
            break
        current += 1
    return current, longest


@kind("btop", "btop — github.com/{login}", 470)
def w_btop(ctx, x, y, w, h):
    data, totals = ctx.data, ctx.data.get("totals", {})
    weeks = data.get("weeks", [])[-30:]
    out = [text(x + 18, y + 52, [("contributions", C["fg"]), (f"  last {len(weeks)} weeks", C["dim"])], None, 11.5)]
    out += heatmap(x + 22, y + 64, weeks)
    days = [c for wk in data.get("weeks", []) for c in wk]
    rows = [("contributions, 1y", totals.get("contributions", 0), "green2"),
            ("commits", totals.get("commits", 0), "blue2"),
            ("pull requests", totals.get("prs", 0), "magenta"),
            ("active days", sum(1 for c in days if c), "yellow"),
            ("public repos", data.get("public_repos", 0), "cyan")]
    peak = max(v for _, v, _ in rows) or 1
    for i, (label, value, color) in enumerate(rows):
        ry = y + 192 + i * 19
        out.append(text(x + 18, ry, label, C["dim"], 11.5))
        out.append(rect(x + 160, ry - 8, 230, 7, rx=2, fill=C["line"]))
        out.append(rect(x + 160, ry - 8, max(3, 230 * value / peak), 7, rx=2, fill=C[color]))
        out.append(text(x + w - 18, ry, str(value), C["bright"], 11.5, text_anchor="end"))
    current, longest = streaks(days)
    out.append(text(x + 18, y + 306, [("streak ", C["dim"]), (f"{current}d", C["green2"]),
                                      ("  longest ", C["dim"]), (f"{longest}d", C["green2"]),
                                      ("  busiest day ", C["dim"]), (str(max(days, default=0)), C["green2"])],
                    None, 11.5))
    return out


@kind("hjkl", "tail -f ~/.hjkl.log", 420)
def w_hjkl(ctx, x, y, w, h):
    state = ctx.state
    out = prompt(x + 18, y + 54, "tail -f ~/.hjkl.log")
    log = state["log"][:8]
    for i, e in enumerate(log):
        ly = y + 82 + i * 22
        out.append(text(x + 18, ly, parse_ts(e["at"]).strftime("%b %d"), C["dim"], 12))
        bw = 10 + len(e["key"]) * 7
        out.append(rect(x + 76, ly - 13, bw, 17, rx=4, fill=C["raised"], stroke=C["active"]))
        out.append(text(x + 76 + bw / 2, ly, e["key"], C["bright"], 12, text_anchor="middle", font_weight="bold"))
        out.append(text(x + 76 + bw + 12, ly, [("→ ", C["dim"]), (e.get("to", ""), C["active"])], None, 12))
    if not log:
        out.append(text(x + 18, y + 82, "# quiet so far. press a key below ↓", C["mute"], 12))
    cy = y + 82 + max(len(log), 1) * 22
    out.append(rect(x + 18, cy - 11, 12 * CW, 13, fill=C["fg"], class_="blink"))
    presses = state["presses"]
    out.append(text(x + 18, y + h - 20, [(str(presses), C["green2"]), (" key" + "s" * (presses != 1) + " pressed", C["dim"])],
                    None, 11.5))
    return out


# ── the desktop ──────────────────────────────────────────────────────────────

def row_offset(view: float, margin: float, widths: list[int], gap: float, f: int) -> float:
    """Like paneru: center the focused column, but never scroll past either end of the strip."""
    xs = [sum(widths[:j]) + gap * j for j in range(len(widths))]
    total = xs[-1] + widths[-1]
    if total <= view - 2 * margin:
        return (view - total) / 2
    return max(min(view / 2 - (xs[f] + widths[f] / 2), margin), view - margin - total)


def desktop(cfg: dict, data: dict, state: dict) -> str:
    W, H = 880, 404
    top, wh, gap, margin, pitch, T = 50, 340, 10, 14, 400, 1.6
    spaces = cfg["workspace"]
    cur = {"ws": state["ws"], "win": state["win"]}
    # On load, the camera glides from where the previous key left it.
    prev = state.get("prev") if state["log"] else None
    prev = prev if prev and prev != cur else None
    ctx = SimpleNamespace(cfg=cfg, data=data, state=state, focus=focused(cfg, cur)["id"],
                          delay=T if prev else 0.0, css=[])
    css = ctx.css
    css += [".cv{transform-box:fill-box;transform-origin:100% 50%}",
            ".blink{animation:bl 1.1s steps(1) infinite}@keyframes bl{50%{opacity:0}}",
            ".spin{transform-box:fill-box;transform-origin:center;animation:spin 1s linear infinite}"
            "@keyframes spin{to{transform:rotate(360deg)}}",
            ".pulse{animation:pu 1.6s ease-in-out infinite}@keyframes pu{50%{opacity:.35}}",
            f".fin{{animation:fin {T}s linear forwards}}@keyframes fin{{0%,55%{{opacity:0}}75%,100%{{opacity:1}}}}",
            f".fout{{animation:fout {T}s linear forwards}}@keyframes fout{{0%,25%{{opacity:1}}45%,100%{{opacity:0}}}}",
            f".din{{animation:din {T}s linear forwards}}@keyframes din{{0%,55%{{opacity:.5}}75%,100%{{opacity:1}}}}",
            f".dout{{animation:dout {T}s linear forwards}}@keyframes dout{{0%,25%{{opacity:1}}45%,100%{{opacity:.5}}}}"]

    def glide(name, axis, a, b):
        css.append(f"@keyframes {name}{{0%,30%{{transform:translate{axis}({a:.1f}px)}}"
                   f"75%,100%{{transform:translate{axis}({b:.1f}px)}}}}"
                   f".{name}{{animation:{name} {T}s cubic-bezier(.3,.7,.2,1) forwards}}")

    body = [f"<defs>{GLOW}"
            '<pattern id="dots" width="18" height="18" patternUnits="userSpaceOnUse">'
            '<circle cx="2" cy="2" r="1" fill="#1a1a1a"/></pattern>'
            f'<clipPath id="vp"><rect x="4" y="42" width="{W - 8}" height="{H - 46}"/></clipPath></defs>',
            rect(0, 0, W, H, rx=14, fill=C["wall"]), rect(0, 0, W, H, rx=14, fill="url(#dots)"),
            rect(10, 8, W - 20, 28, rx=8, fill=C["bg"], stroke=C["line"])]

    # top bar: workspaces, the focused window's title, the keypress tally and date
    px = 18
    for i, space in enumerate(spaces):
        label = f"{i + 1} {space['key']}"
        w = (len(label) + 2) * 11 * CW
        active = i == cur["ws"]
        if active:
            body.append(rect(px, 13, w, 18, rx=5, fill=C["active"]))
        body.append(text(px + w / 2, 26, label, C["wall"] if active else C["dim"], 11,
                         text_anchor="middle", font_weight="bold" if active else None))
        px += w + 4
    title = WINDOWS[ctx.focus][0].format(login=cfg["login"])
    body.append(text(W / 2, 26, clip(title, 40), C["bright"], 11.5, text_anchor="middle"))
    right = [(str(state["presses"]), C["green2"]), (" key" + "s" * (state["presses"] != 1) + "   ", C["dim"]),
             (parse_ts(data["fetched_at"]).strftime("%a %b %-d"), C["fg"])]
    body.append(text(W - 20, 26, right, None, 11, text_anchor="end"))

    rows = []
    for r, space in enumerate(spaces):
        specs = space["windows"]
        widths = [WINDOWS[s["id"]][1] for s in specs]
        g, x = [], 0
        for j, spec in enumerate(specs):
            title, w, draw = WINDOWS[spec["id"]]
            y = top + r * pitch
            now_f = (r, j) == (cur["ws"], cur["win"][r])
            was_f = prev is not None and (r, j) == (prev["ws"], prev["win"][prev["ws"]])
            g += frame(x, y, w, wh, title.format(login=cfg["login"]))
            content = "".join(draw(ctx, x, y, w, wh))
            if now_f:
                g.append(f'<g{attrs(class_="din" if prev else None)}>{content}</g>')
                g.append(ring(x, y, w, wh, class_="fin" if prev else None))
            elif was_f:
                g.append(f'<g opacity=".5" class="dout">{content}</g>')
                g.append(ring(x, y, w, wh, opacity=0, class_="fout"))
            else:
                g.append(f'<g opacity=".5">{content}</g>')
            x += w + gap
        off = row_offset(W, margin, widths, gap, cur["win"][r])
        old = row_offset(W, margin, widths, gap, prev["win"][r]) if prev else off
        cls = None
        if abs(old - off) > 0.5:
            cls = f"row{r}"
            glide(cls, "X", old, off)
        rows.append(f'<g{attrs(transform=f"translate({off:.1f} 0)", class_=cls)}>{"".join(g)}</g>')

    cy = -cur["ws"] * pitch
    cls = None
    if prev and prev["ws"] != cur["ws"]:
        cls = "cam"
        glide(cls, "Y", -prev["ws"] * pitch, cy)
    body.append(f'<g clip-path="url(#vp)"><g{attrs(transform=f"translate(0 {cy})", class_=cls)}>{"".join(rows)}</g></g>')
    css.append("@media (prefers-reduced-motion:reduce){*{animation:none!important}.cv{display:none}}")
    return doc(W, H, body, "".join(css),
               f"{cfg['name']}'s desktop, a tiling window manager driven by hjkl; focus is on {where(cfg, cur)}")


# ── extra widgets ────────────────────────────────────────────────────────────
# Optional pieces around the desktop, switched on by `widgets` in profile.toml.

# Lowercase block letters in the style of "ANSI Shadow", the figlet font of LazyVim's start-screen
# logo. The font only has capitals, so these are bitmaps: two ascender rows, five x-height rows,
# two descender rows. block_letters() adds the shadow.
LOWER = {
    "d": ["     ██", "     ██", " ██████", "██   ██", "██   ██", "██   ██", " ██████"],
    "y": ["", "", "██   ██", "██   ██", "██   ██", "██   ██", " ██████", "     ██", " █████"],
    "l": ["██", "██", "██", "██", "██", "██", "██"],
    "a": ["", "", " █████", "     ██", " ██████", "██   ██", " ██████"],
    "n": ["", "", "██████", "██   ██", "██   ██", "██   ██", "██   ██"],
}
BOX = {"═": "lr", "║": "ud", "╔": "rd", "╗": "ld", "╚": "ur", "╝": "ul",
       "╠": "udr", "╣": "udl", "╦": "lrd", "╩": "lru", "╬": "udlr"}
CITRUS = ["#f77f00", "#ffdf40"]
RAMP = ".:-=+*#%@$"  # ASCII from light to dense, for the rough sketch the start screen begins as


def block_letters(word: str, rows: int = 9) -> list[str]:
    """`word` as lines of text, one letter per LOWER bitmap plus a column of spacing. ANSI Shadow's
    shadow is the letters' outline moved half a cell right and down, drawn in box characters
    wherever no █ covers it, so it falls out of the bitmap: each cell gets the box character for
    the outline edges meeting at its top-left corner."""
    bitmap = [""] * rows
    for ch in word:
        glyph = LOWER[ch]
        w = max(map(len, glyph))
        for r in range(rows):
            bitmap[r] += (glyph[r] if r < len(glyph) else "").ljust(w) + " "
    width = len(bitmap[0])
    filled = lambda r, c: 0 <= r < rows and 0 <= c < width and bitmap[r][c] == "█"
    by_edges = {frozenset(v): k for k, v in BOX.items()}
    out = []
    for r in range(rows + 1):
        line = ""
        for c in range(width):
            nw, ne, sw, se = filled(r - 1, c - 1), filled(r - 1, c), filled(r, c - 1), filled(r, c)
            edges = frozenset(d for d, e in (("u", nw != ne), ("d", sw != se), ("l", nw != sw), ("r", ne != se)) if e)
            line += "█" if se else by_edges.get(edges, " ")
        out.append(line)
    return out


def sketch(art: list[str], rng: random.Random, margin: int, rx=2, ry=1, floor=0.22, jitter=0.14) -> list[list[int]]:
    """A rough copy of `art` that can't be read: its ink blurred, jittered and quantized to RAMP.
    Each cell is an index into RAMP, or -1 for blank; rows are `margin` columns wider each side."""
    rows, width = len(art), len(art[0])
    ink = lambda r, c: ((1.0 if art[r][c] == "█" else 0.35 if art[r][c] in BOX else 0.0)
                        if 0 <= r < rows and 0 <= c < width else 0.0)
    kernel = [(dr, dc, math.exp(-dr * dr / (ry * ry + 0.5) - dc * dc / (rx * rx + 0.5)))
              for dr in range(-ry, ry + 1) for dc in range(-rx, rx + 1)]
    total = sum(w for _, _, w in kernel)
    out = []
    for r in range(rows):
        row = []
        for c in range(-margin, width + margin):
            v = sum(w * ink(r + dr, c + dc) for dr, dc, w in kernel) / total + rng.uniform(-jitter, jitter)
            row.append(-1 if v < floor else min(len(RAMP) - 1, int((v - floor) / (0.75 - floor) * len(RAMP))))
        out.append(row)
    return out


def mix(a: str, b: str, t: float) -> str:
    return "#" + "".join(f"{round(int(a[i:i + 2], 16) * (1 - t) + int(b[i:i + 2], 16) * t):02x}" for i in (1, 3, 5))


def grid_text(x, y, s: str, cw: float, fill, size) -> str:
    """Text whose characters sit exactly `cw` apart, whatever the viewer's monospace font is."""
    n = len(s) - len(s.lstrip(" "))
    s = s.strip(" ")
    return text(round(x + n * cw, 1), round(y, 1), s, fill, size, textLength=round(len(s) * cw, 1), lengthAdjust="spacing") if s else ""


def box_path(ch: str, x, y, w, h) -> str:
    """A box-drawing character as path data: a line from the cell's center to each side it joins."""
    cx, cy = x + w / 2, y + h / 2
    ends = {"u": f"V{y:.1f}", "d": f"V{y + h:.1f}", "l": f"H{x:.1f}", "r": f"H{x + w:.1f}"}
    return "".join(f"M{cx:.1f} {cy:.1f}{ends[d]}" for d in BOX[ch])


def next_word(s: str, c: int, end: bool) -> int | None:
    """Where W (or E, with `end`) takes the cursor from column c: WORDs are runs of non-blanks."""
    i = c
    if not end:
        while i < len(s) and s[i] != " ":
            i += 1
        while i < len(s) and s[i] == " ":
            i += 1
        return i if i < len(s) else None
    i += 1
    while i < len(s) and s[i] == " ":
        i += 1
    if i >= len(s):
        return None
    while i + 1 < len(s) and s[i + 1] != " ":
        i += 1
    return i


class Strokes:
    """Plans the keystrokes that redraw a buffer, moving the cursor the way nvim does. Each motion
    is the shortest that lands exactly (f/t/F and ; to a character, W/E by WORDs, + or ^ to a first
    non-blank, else a count), and each edit is an r: {count}r{char} along a line, or for a rectangle
    a visual block (^V, then j and l) and r{char}. Lines are padded to one width, so j and k keep
    the column."""

    def __init__(self, lines: list[str], cur: tuple[int, int]):
        self.lines, self.cur, self.find, self.steps = list(lines), cur, None, []

    def step(self, keys, cur, edit=None, select=None):
        self.cur = cur
        self.steps.append(SimpleNamespace(keys=keys, cur=cur, edit=edit, select=select))

    def hmove(self, s: str, c: int, t: int) -> str:
        if t == c:
            return ""
        d = abs(t - c)
        cands = [(("" if d == 1 else str(d)) + ("l" if t > c else "h"), 3, None)]
        if t > c:
            for verb, at in (("f", t), ("t", t + 1)):
                ch = s[at] if at < len(s) else " "
                if ch != " " and ch not in s[c + 1:at]:
                    cands.append((";" if self.find == (verb, ch) else verb + ch, 1, (verb, ch)))
            for verb in "WE":
                i = c
                for k in range(1, 4):
                    i = next_word(s, i, verb == "E")
                    if i is None or i > t:
                        break
                    if i == t:
                        cands.append(((str(k) if k > 1 else "") + verb, 0, None))
                        break
        elif s[t] != " " and s[t] not in s[t + 1:c]:
            cands.append((";" if self.find == ("F", s[t]) else "F" + s[t], 1, ("F", s[t])))
        if t == len(s) - len(s.lstrip(" ")):
            cands.append(("^", 0, None))
        keys, _, find = min(cands, key=lambda k: (len(k[0]), k[1]))
        self.find = find or self.find
        return keys

    def goto(self, r: int, c: int):
        cr, cc = self.cur
        s = self.lines[r]
        if r != cr:
            dr = r - cr
            if dr > 0 and c == len(s) - len(s.lstrip(" ")):
                return self.step(("" if dr == 1 else str(dr)) + "+", (r, c))
            self.step(("" if abs(dr) == 1 else str(abs(dr))) + ("j" if dr > 0 else "k"), (r, cc))
        if keys := self.hmove(s, cc, c):
            self.step(keys, (r, c))

    def replace(self, r0: int, c0: int, r1: int, c1: int, ch: str):
        self.goto(r0, c0)
        n, shown = c1 - c0 + 1, "␣" if ch == " " else ch
        if r1 > r0:
            self.step("^V", (r0, c0), select=(r0, c0, r0, c0))
            self.step(("" if r1 - r0 == 1 else str(r1 - r0)) + "j", (r1, c0), select=(r0, c0, r1, c0))
            if n > 1:
                self.step(("" if n == 2 else str(n - 1)) + "l", (r1, c1), select=(r0, c0, r1, c1))
            keys, end = "r" + shown, (r0, c0)
        else:
            keys, end = ("" if n == 1 else str(n)) + "r" + shown, (r0, c1)
        for r in range(r0, r1 + 1):
            self.lines[r] = self.lines[r][:c0] + ch * n + self.lines[r][c1 + 1:]
        self.step(keys, end, edit=(r0, c0, r1, c1, ch))


def rects(want, ok, cols: range, rows: range) -> list[tuple[int, int, int, int]]:
    """Greedy rectangles (r0, c0, r1, c1) covering every cell where want(r, c) and only cells where
    ok(r, c): each grows right while there's work ahead, then down while every row has some."""
    done, out = set(), []
    todo = lambda r, c: want(r, c) and (r, c) not in done
    for r in rows:
        for c in cols:
            if not todo(r, c):
                continue
            c1 = e = c
            while e + 1 in cols and ok(r, e + 1):
                e += 1
                c1 = e if todo(r, e) else c1
            r1 = r
            while r1 + 1 in rows and all(ok(r1 + 1, x) for x in range(c, c1 + 1)) and \
                    any(todo(r1 + 1, x) for x in range(c, c1 + 1)):
                r1 += 1
            done.update((y, x) for y in range(r, r1 + 1) for x in range(c, c1 + 1))
            out.append((r, c, r1, c1))
    return out


def redraw(start: list[str], target: list[str], spans: list[tuple[int, int]], cur) -> Strokes:
    """Strokes that redraw `start` as `target` one letter (column span) at a time: its █ mass in
    rectangles, then the noise around it blanked, then its shadow, ║ runs as visual blocks."""
    st, rows = Strokes(start, cur), range(len(target))
    solid = lambda r, c: target[r][c] == "█"
    for a, b in spans:
        cols = range(a, b + 1)
        for r0, c0, r1, c1 in rects(lambda r, c: solid(r, c) and st.lines[r][c] != "█", solid, cols, rows):
            st.replace(r0, c0, r1, c1, "█")
        for r0, c0, r1, c1 in rects(lambda r, c: not solid(r, c) and st.lines[r][c] != " ",
                                    lambda r, c: not solid(r, c), cols, rows):
            st.replace(r0, c0, r1, c1, " ")
        bar = lambda r, c: target[r][c] == "║"
        shadow = [(*box, "║") for box in rects(bar, bar, cols, rows)]
        for r in rows:
            for m in re.finditer(r"═+|[╔╗╚╝╠╣╦╩╬]", target[r][a:b + 1]):
                shadow.append((r, a + m.start(), r, a + m.end() - 1, m[0][0]))
        for edit in sorted(shadow):
            st.replace(*edit)
    return st


class Sprites:
    """Pieces that repeat across a timeline, each defined once and placed with <use>."""

    def __init__(self):
        self.ids: dict[str, str] = {}

    def use(self, svg: str, **kw) -> str:
        if svg not in self.ids:
            self.ids[svg] = f"s{len(self.ids)}"
        return f'<use href="#{self.ids[svg]}"{attrs(**kw)}/>'

    def defs(self) -> str:
        return "<defs>" + "".join(f'<g id="{i}">{svg}</g>' for svg, i in self.ids.items()) + "</defs>"


def start_widget(cfg: dict) -> str:
    """The start screen: `dylan` as a rough ASCII smudge that can't be read, open in nvim, drawn
    back into block letters by sped-up vim motions (the keys stream past at the bottom), written
    with :w, then undone to the smudge again by a fast run of u.

    Edits pile up rather than replace each other, so instead of redrawing whole frames each edit is
    its own shape over the smudge, shown from when it's made until it's undone. The cursor, gutter,
    status line and key stream run their own step-end timelines over the same loop."""
    W, size, lh, pad, margin = 880, 19, 22, 14, 3
    cw, rows_shown = size * CW, 11
    top, gx = 42, 18                       # first buffer row; gutter's left edge
    x0 = gx + 6 * cw                       # where buffer text starts
    sy = top + rows_shown * lh + 6         # statusline
    H = sy + 52
    name, word, sp, css = "dylan.txt", "dylan", Sprites(), []
    art = block_letters(word)
    rng = random.Random(1)
    smudge = sketch(art, rng, margin)
    width = pad + len(art[0]) + margin
    start = [(" " * (pad - margin) + "".join(RAMP[i] if i >= 0 else " " for i in row)).ljust(width) for row in smudge]
    target = [(" " * pad + s).ljust(width) for s in art]
    tones = [mix(*CITRUS, r / (len(art) - 1)) for r in range(len(art))]
    at = lambda r, c: (round(x0 + c * cw, 1), top + r * lh)

    # each letter's columns, shadow column included; the first and last also take the margins
    spans, x = [], pad
    for i, ch in enumerate(word):
        w = max(map(len, LOWER[ch])) + 1
        spans.append((0 if i == 0 else x, width - 1 if i == len(word) - 1 else x + w - 1))
        x += w
    # start the cursor where the first edit is: that's where undoing everything leaves it
    first = next(s.edit for s in redraw(start, target, spans, (0, 0)).steps if s.edit)
    strokes = redraw(start, target, spans, first[:2])
    assert strokes.lines == target, "the strokes should redraw the logo exactly"

    # ── when everything happens ──
    steps, edits = strokes.steps, [s for s in strokes.steps if s.edit]
    t, n = 1.4, len(steps)
    for k, s in enumerate(steps):  # eases in, so the first few keys can be read, and out
        s.t = t
        t += (0.03 if s.edit else 0.021) * (1 + 3 * max(0, 1 - k / 14) + 1.5 * max(0, 1 - (n - 1 - k) / 8))

    # then, at normal speed: G, o, the tagline typed on the new line (autoindent puts it under the
    # y's tail), and :right to push it against the window's edge
    tagline, row = cfg["tagline"], len(target)
    indent = len(target[-1]) - len(target[-1].lstrip(" "))
    edge = int((W - 18 - x0) / cw) - 1  # the columns that fit, less one for a margin
    home = edge - len(tagline)
    align = f":ri {edge}"
    lines_out = [s.rstrip() for s in target] + [" " * home + tagline]
    written_msg = f'"{name}" {len(lines_out)}L, {sum(len(s.encode()) + 1 for s in lines_out)}B written'
    trng, finale, t = random.Random(len(tagline)), [], t + 0.5

    def press(keys, cur=None, mode="NORMAL", cmd=None, join=False, wait=0.3):
        """A key at normal speed. cur is (row, col, beam?), None while typing a command."""
        nonlocal t
        finale.append(SimpleNamespace(t=t, keys=keys, cur=cur, mode=mode, cmd=cmd, join=join))
        t += wait

    press("G", (row - 1, steps[-1].cur[1], False))
    press("o", (row, indent, True), "INSERT", wait=0.3)
    for i, ch in enumerate(tagline):
        press(ch, (row, indent + i + 1, True), "INSERT", join=i > 0,
              wait=trng.uniform(0.045, 0.1) + (0.15 if ch == " " and trng.random() < 0.3 else 0))
    press("<Esc>", (row, indent + len(tagline) - 1, False), wait=0.4)
    for i in range(len(align)):
        press(align[i], None, "COMMAND", align[:i + 1], join=i > 0, wait=0.09)
    press("<CR>", (row, home, False), cmd=align, wait=0.6)
    aligned = finale[-1].t
    press(":", None, "COMMAND", ":", wait=0.15)
    press("w", None, "COMMAND", ":w", join=True, wait=0.15)
    press("<CR>", (row, home, False), cmd=written_msg, wait=0)
    opened, written = finale[1].t, t

    # then a fast run of u takes back the :right, the new line, and every edit
    rewind = written + 2.8
    changes = ["align", "line"] + edits[::-1]
    undone = {id(c): rewind + j * 1.5 / len(changes) for j, c in enumerate(changes)}
    for s in edits:
        s.undo = undone[id(s)]
    unalign, unopen = undone[id(changes[0])], undone[id(changes[1])]
    oldest = rewind + 1.5
    period = oldest + 0.3
    pct = lambda t: f"{t / period * 100:.2f}%"

    def animate(name_, prop, points, rule=True, timing="step-end") -> str:
        """A timeline of (seconds, value) points, by default stepping from one to the next, and a
        class that runs it (unless the element names the animation itself). Returns the class."""
        points = sorted(points, key=lambda p: p[0])
        frames = "".join(f"{pct(t)}{{{prop}:{v}}}" for t, v in points) + f"100%{{{prop}:{points[-1][1]}}}"
        css.append(f"@keyframes {name_}{{{frames}}}"
                   + (f".{name_}{{animation:{name_} {period:.2f}s {timing} infinite}}" if rule else ""))
        return name_

    def strip(name_, segments, clip) -> str:
        """Show each (start, svg) segment in turn: a stack of them slid by one step-end animation."""
        merged = []
        for t, svg in sorted(segments, key=lambda s: s[0]):
            if not merged or merged[-1][1] != svg:
                merged.append((t, svg))
        animate(name_, "transform", [(t, f"translateY({-i * H}px)") for i, (t, _) in enumerate(merged)])
        body = "".join(f'<g transform="translate(0 {i * H})">{sp.use(svg)}</g>' for i, (_, svg) in enumerate(merged))
        return f'<clipPath id="{name_}-c">{rect(*clip)}</clipPath><g clip-path="url(#{name_}-c)"><g class="{name_}">{body}</g></g>'

    body = frame(4, 4, W - 8, H - 8, f"nvim — ~/{name}")
    body.append(text(W - 18, 23, ":h motion.txt", C["dim"], 11, text_anchor="end"))

    # ── the smudge, its characters boiling ──
    css.append("@keyframes boil{0%{opacity:1}33.3%{opacity:0}}"
               + "".join(f".b{i}{{animation:boil .36s step-end {-0.12 * i:.2f}s infinite}}" for i in range(3)))
    for i in range(3):
        rows = smudge if i == 0 else [[min(len(RAMP) - 1, max(0, v + rng.choice((-1, 0, 1)))) if v >= 0 else -1
                                       for v in row_] for row_ in smudge]
        body.append(f'<g class="b{i}" opacity=".85">' + "".join(
            grid_text(at(r, pad - margin)[0], top + r * lh + lh * 0.74, "".join(RAMP[v] if v >= 0 else " " for v in row_),
                      cw, tones[r], size) for r, row_ in enumerate(rows)) + "</g>")

    # ── every edit, from when it's made until it's undone; visual-block selections while they last ──
    css.append(f".o{{animation:{period:.2f}s step-end infinite}}")
    for k, s in enumerate(edits):
        r0, c0, r1, c1, ch = s.edit
        x, w = at(r0, c0)[0], round((c1 - c0 + 1) * cw, 1)
        if ch == "█":
            shape = "".join(rect(x, at(r, c0)[1], w + 0.4, lh + 0.4, fill=tones[r]) for r in range(r0, r1 + 1))
        elif ch == " ":
            shape = rect(x, at(r0, c0)[1], w, (r1 - r0 + 1) * lh + 0.4, fill=C["panel"])
        else:
            shape = (f'<path d="{"".join(box_path(ch, at(r, c)[0], at(r, c)[1], cw, lh) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1))}"'
                     f' fill="none" stroke="{C["inactive"]}" stroke-width="1.8"/>')
        animate(f"e{k}", "opacity", [(0, 0), (s.t, 1), (s.undo, 0)], rule=False)
        body.append(f'<g class="o" style="animation-name:e{k}">{shape}</g>')
    for k, s in enumerate(steps):
        if s.select:
            r0, c0, r1, c1 = s.select
            animate(f"v{k}", "opacity", [(0, 0), (s.t, 1), (steps[k + 1].t, 0)], rule=False)
            body.append(f'<g class="o" style="animation-name:v{k}" opacity="0">'
                        f'{rect(*at(r0, c0), round((c1 - c0 + 1) * cw, 1), (r1 - r0 + 1) * lh, fill=C["magenta"], fill_opacity=0.35)}</g>')

    # ── the tagline, in grey so it doesn't compete: each character as it's typed, then the line where :right puts it ──
    baseline = top + row * lh + lh * 0.74
    for i, (ch, e) in enumerate(zip(tagline, finale[2:])):
        if ch != " ":
            animate(f"k{i}", "opacity", [(0, 0), (e.t, 1), (aligned, 0), (unalign, 1), (unopen, 0)], rule=False)
            body.append(f'<g class="o" style="animation-name:k{i}" opacity="0">'
                        f'{text(at(row, indent + i)[0], round(baseline, 1), ch, C["dim"], size)}</g>')
    animate("tag", "opacity", [(0, 0), (aligned, 1), (unalign, 0)], rule=False)
    body.append(f'<g class="o" style="animation-name:tag">{grid_text(at(row, home)[0], baseline, tagline, cw, C["dim"], size)}</g>')

    # ── cursor (a beam in insert mode), gutter and cursorline ──
    moves = ([(0, (*first[:2], False))] + [(s.t, (*s.cur, False)) for s in steps]
             + [(e.t, e.cur) for e in finale if e.cur] + [(unalign, (row, indent, False)), (unopen, (row - 1, indent, False))]
             + [(s.undo, (*s.edit[:2], False)) for s in edits])
    blink = ([(0, 1), (0.5, 0), (1.0, 1)] + [(e.t, int(e.mode != "COMMAND")) for e in finale]
             + [(written + 0.55 * i, 1 - i % 2) for i in range(5)])
    body.append(f'<g transform="translate({x0:.1f} {top}) scale({cw:.1f} {lh})">'  # (in cells, so the keyframes stay short)
                f'<g class="{animate("cur", "transform", [(t, f"translate({c}px,{r}px)" + (" scale(.15,1)" if beam else "")) for t, (r, c, beam) in moves])}">'
                # (difference blending inverts what's under it, like a terminal's block cursor)
                f'<rect class="{animate("blink", "opacity", blink)}" width="1" height="1" fill="#fff" opacity="0"'
                f' style="mix-blend-mode:difference"/></g></g>')

    def gutter(cur_row, count) -> str:
        out = [rect(5.5, top + cur_row * lh, W - 11, lh, fill="#fff", fill_opacity=0.04)]
        for i in range(count):
            out.append(text(gx + 5 * cw - 4, top + i * lh + lh * 0.72, str(i + 1 if i == cur_row else abs(i - cur_row)),
                            C["active"] if i == cur_row else C["mute"], 13.5, text_anchor="end",
                            font_weight="bold" if i == cur_row else None))
        return "".join(out)
    count = lambda t: len(target) + (opened <= t < unopen)
    body.append(strip("gut", [(t, gutter(r, count(t))) for t, (r, _, _) in moves], (5.5, top, W - 11, rows_shown * lh)))

    # ── status line and command line ──
    def status(mode="NORMAL", modified=True, z="") -> str:
        fs, fw, zw = 12.5, 12.5 * CW, 70
        color = {"NORMAL": C["active"], "INSERT": C["green"], "VISUAL BLOCK": C["magenta"], "COMMAND": C["yellow"]}[mode]
        mw, zx = (len(mode) + 2) * fw, W - 5.5 - zw / 2
        out = [rect(5.5, sy, W - 11, 22, fill=C["raised"]), rect(5.5, sy, mw, 22, fill=color),
               text(5.5 + mw / 2, sy + 15.5, mode, C["wall"], fs, text_anchor="middle", font_weight="bold"),
               text(5.5 + mw + fw, sy + 15.5, [("main  ", C["dim"]), (name, C["fg"]),
                                               (" ●" if modified else "", C["active"])], None, fs),
               rect(W - 5.5 - zw, sy, zw, 22, fill=color),
               text(W - 5.5 - zw - fw, sy + 15.5, "All", C["dim"], fs, text_anchor="end")]
        if z in ("ff", "rw", "play"):  # a time-lapse badge: sped up, rewinding, or back to real time
            bxs = (zx - 17,) if z == "play" else (zx - 24, zx - 17)
            tri = "".join(f"M{bx + 7:.1f} {sy + 6}l-7 5l7 5z" if z == "rw" else f"M{bx:.1f} {sy + 6}l7 5l-7 5z" for bx in bxs)
            out += [f'<path d="{tri}" fill="{C["wall"]}"/>',
                    text(zx - 6, sy + 15.5, "1×" if z == "play" else "8×", C["wall"], fs, font_weight="bold")]
        else:
            out.append(text(zx, sy + 15.5, z, C["wall"], fs, text_anchor="middle", font_weight="bold"))
        return "".join(out)

    def cmdline(msg, typing=False) -> str:
        return text(12, sy + 40, msg, C["fg"], 13) + (rect(12 + len(msg) * 13 * CW, sy + 29, 1.6, 14, fill=C["bright"])
                                                        if typing else "")

    ruler = lambda r, c: f"{r + 1}:{c + 1}"
    bar = [(0, status(z=ruler(*first[:2])))] + [(s.t, status("VISUAL BLOCK" if s.select else "NORMAL", z="ff")) for s in steps]
    bar += [(e.t, status(e.mode, z="play")) for e in finale[:-1]]
    bar += [(written, status(modified=False, z=ruler(row, home))), (rewind, status(z="rw")), (oldest, status(z=ruler(*first[:2])))]
    said = [(0, cmdline("Already at oldest change")), (steps[0].t, cmdline(""))]
    said += [(e.t, cmdline(e.cmd, e.mode == "COMMAND")) for e in finale if e.cmd]
    said += [(undone[id(c)], cmdline(f"1 change; before #{len(changes) - j}  4 seconds ago"))
             for j, c in enumerate(changes) if j % 8 == 0]
    said.append((oldest, cmdline("Already at oldest change")))
    body.append(strip("st", bar, (5.5, sy, W - 11, 22)))
    body.append(strip("cmd", said, (5.5, sy + 22, W - 11, H - 4.5 - sy - 22)))

    # ── the keys, streaming past right to left on the command line, where nvim shows typed keys ──
    px, py, pw = W - 24 - 340, sy + 25, 340
    fs = 14
    kw_ = round(fs * CW, 1)
    keys = ([(s.t, s.keys, False) for s in steps] + [(e.t, e.keys, e.join) for e in finale]
            + [(undone[id(c)], "u", False) for c in changes] + [(oldest, "u", False)])
    stream, ends = "", []
    for t, k, join in keys:
        stream += ("" if join or not stream else " ") + k
        ends.append((t, len(stream) * kw_))
    out = []
    for m in re.finditer(r"[^█␣═║╔╗╚╝╠╣╦╩╬]+", stream):
        run = m[0].strip(" ")
        if run:
            out.append(grid_text((m.start() + len(m[0]) - len(m[0].lstrip(" "))) * kw_, py + 15.5, run, kw_, C["fg"], fs))
    for i, ch in enumerate(stream):
        cx = i * kw_
        if ch == "█":
            out.append(rect(round(cx + 1, 1), py + 5, round(kw_ - 2, 1), 13, fill=C["active"]))
        elif ch in BOX:
            out.append(f'<path d="{box_path(ch, cx, py + 3, kw_, 16)}" fill="none" stroke="{C["green"]}" stroke-width="1.6"/>')
        elif ch == "␣":
            out.append(f'<path d="M{cx + 1.5:.1f} {py + 13}v4h{kw_ - 3:.1f}v-4" fill="none" stroke="{C["dim"]}" stroke-width="1.2"/>')
    right = px + pw - 12
    shown = animate("rib", "opacity", [(0, 0), (steps[0].t, 1)])
    # it glides between every 4th key while they come fast, which reads the same and keeps this
    # short, and holds still on the :w<CR> until the undos start
    shift = lambda end: f"translateX({right - end:.0f}px)"
    last = len(steps) + len(finale) - 1
    glide = [(t, shift(end)) for k, (t, end) in enumerate(ends) if k < 14 or k % 4 == 0 or len(steps) - 2 <= k <= last + 1]
    glide += [(rewind - 0.01, shift(ends[last][1])), (ends[-1][0], shift(ends[-1][1]))]
    flow = animate("flow", "transform", glide, timing="linear")
    body += ['<linearGradient id="fade" x1="0" x2="1"><stop offset="0" stop-color="#1f1f1f"/>'
             '<stop offset="1" stop-color="#1f1f1f" stop-opacity="0"/></linearGradient>',
             f'<clipPath id="rib-c">{rect(px + 4, py, pw - 8, 22)}</clipPath>',
             f'<g class="{shown}" opacity="0">{rect(px, py, pw, 22, rx=7, fill=C["raised"], stroke="#3a3a3a")}'
             f'<g clip-path="url(#rib-c)"><g class="{flow}">{"".join(out)}</g></g>'
             f'{rect(px + 1, py + 1, 90, 20, rx=6, fill="url(#fade)")}</g>']

    # ── once written, a highlight sweeps across the letters ──
    left, span = x0 + pad * cw, len(art[0]) * cw + 240
    css.append(f"@keyframes sweep{{0%,{pct(written + 0.05)}{{transform:translateX(0)}}"
               f"{pct(written + 0.95)},100%{{transform:translateX({span:.0f}px)}}}}"
               f".sweep{{animation:sweep {period:.2f}s linear infinite}}")
    letters = "".join(f"M{x0 + m.start() * cw:.1f} {top + r * lh}h{(m.end() - m.start()) * cw:.1f}v{lh}h{-(m.end() - m.start()) * cw:.1f}z"
                      for r, s in enumerate(target) for m in re.finditer(r"█+", s))
    body += ['<linearGradient id="shine" x1="0" x2="1"><stop offset="0" stop-color="#fff" stop-opacity="0"/>'
             '<stop offset=".5" stop-color="#fff" stop-opacity=".8"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>',
             f'<clipPath id="letters"><path d="{letters}"/></clipPath>',
             f'<g clip-path="url(#letters)"><g class="sweep"><rect x="{left - 135:.1f}" y="{top}" width="140" '
             f'height="{len(art) * lh}" fill="url(#shine)" transform="skewX(-20)"/></g></g>']

    css.append("@media (prefers-reduced-motion:reduce){*{animation:none!important}}")
    body.insert(0, sp.defs())
    return doc(W, H, body, "".join(css),
               f"dylan as a rough ASCII smudge, drawn back into block letters in nvim with sped-up vim motions, "
               f"then '{tagline}' typed underneath")


# ── the zen garden ───────────────────────────────────────────────────────────
# A garden that grows with the real weather where I live, fetched once a day from Open-Meteo,
# and with every keypress on the desktop. data/garden.json only records what happened (each
# finished day's weather, each visit); the tree and moss are replayed from it on every render.

WEATHER = ["weather_code", "temperature_2m_max", "temperature_2m_min", "precipitation_sum",
           "snowfall_sum", "sunshine_duration", "wind_speed_10m_max"]
# what a keypress does in the garden: (verb, tree, moss, rakes the sand)
TENDING = {"h": ("raked the sand", 0.2, 0, True), "l": ("raked the sand", 0.2, 0, True),
           "j": ("watered the moss", 0.2, 0.5, False), "k": ("tended the tree", 0.6, 0, False),
           "gg": ("sat by the stones", 0.2, 0.2, False)}


def load_garden() -> dict:
    return json.loads(GARDEN.read_text()) if GARDEN.exists() else {"days": {}, "visits": []}


def save_garden(garden: dict) -> None:
    GARDEN.parent.mkdir(exist_ok=True)
    GARDEN.write_text(json.dumps(garden, indent=1, sort_keys=True) + "\n")


def fetch_weather(cfg: dict) -> None:
    """Record every finished day since planting (Open-Meteo goes back 92 days) and the weather now."""
    g = cfg.get("garden")
    if not g:
        return
    q = urllib.parse.urlencode({"latitude": g["latitude"], "longitude": g["longitude"], "timezone": g["timezone"],
                                "past_days": 92, "forecast_days": 1, "daily": ",".join(WEATHER),
                                "current": "temperature_2m,weather_code,precipitation,wind_speed_10m,is_day"})
    try:
        req = urllib.request.Request(f"https://api.open-meteo.com/v1/forecast?{q}",
                                     headers={"User-Agent": "profile-readme-build"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            res = json.load(resp)
    except Exception as exc:  # no report today: the garden just doesn't grow
        print(f"weather: {exc}; keeping previous data", file=sys.stderr)
        return
    garden, today, daily = load_garden(), res["current"]["time"][:10], res["daily"]
    for i, day in enumerate(daily["time"]):
        if str(g["planted"]) <= day < today:
            garden["days"][day] = {k: daily[k][i] for k in WEATHER}
    garden["now"] = {"date": today, **{k: res["current"][k] for k in
                                       ("temperature_2m", "weather_code", "precipitation", "wind_speed_10m", "is_day")}}
    save_garden(garden)


def sky(code: int) -> str:
    """A WMO weather code in a word."""
    for top, word in [(1, "clear"), (3, "cloudy"), (48, "fog"), (57, "drizzle"), (67, "rain"), (77, "snow"),
                      (82, "showers"), (86, "snow"), (99, "storm")]:
        if code <= top:
            return word
    return "?"


def fahrenheit(c) -> int:
    return round(c * 9 / 5 + 32)


def grow(garden: dict, planted: str) -> SimpleNamespace:
    """Replay the garden, day by day and visit by visit. Warm sunny days grow the tree (nothing
    grows below 8°C or under snow), rain grows the moss and heat dries it, strong wind smooths
    the raked sand and strips leaves in autumn, and visitors tend it (TENDING). Returns the levels
    and a log line per event: (date, words, [(effect, color)])."""
    tree = moss = fallen = 0.0
    rake, log = 1.0, []
    events = sorted([(d, "", w) for d, w in garden["days"].items() if d >= planted]
                    + [(v["at"][:10], v["at"], v) for v in garden["visits"]], key=lambda e: e[:2])
    for day, at, e in events:
        if at:  # a visit
            verb, t, m, rakes = TENDING.get(e["key"], TENDING["h"])
            tree, moss, rake = tree + t, moss + m, 1.0 if rakes else rake
            log.append((day, [("someone ", C["bright"]), (verb, C["fg"])],
                        [("tree+", C["active"])] * (t >= 0.5) + [("moss+", C["green"])] * (m > 0)))
            continue
        n = lambda k: e.get(k) or 0
        hi, rain, snow = n("temperature_2m_max"), n("precipitation_sum"), n("snowfall_sum")
        sun, wind, month = n("sunshine_duration") / 3600, n("wind_speed_10m_max"), int(day[5:7])
        effects = []
        if hi < 8 or snow:
            effects.append(("dormant", C["dim"]))
        else:
            grew = min(1.2, sun / 10) + min(0.3, rain / 40)
            tree += grew
            if grew >= 0.5:
                effects.append(("tree+", C["active"]))
        if rain >= 1 and hi >= 4:
            moss += min(1.5, rain / 8)
            effects.append(("moss+", C["green"]))
        elif rain < 0.2 and hi >= 28 and moss:
            moss = max(0.0, moss - 0.5)
            effects.append(("dried", C["yellow"]))
        if wind >= 30:
            rake = max(0.0, rake - 0.4)
            effects.append(("wind", C["blue2"]))
        fallen = fallen * 0.97 + (1 + 3 * (wind >= 25) if month in (10, 11) else 0)
        # (the day's weather code is its worst moment, so say it with sun hours and rain instead)
        if snow:
            words = f"snow {snow / 2.54:.1f}in"
        elif rain >= 1:
            words = f"rain {rain / 25.4:.2f}in"
        else:
            words = "sunny" if sun >= 8 else "some sun" if sun >= 4 else "drizzle" if rain else "grey"
        log.append((day, [(words.ljust(12), C["fg"]),
                          (f"{fahrenheit(n('temperature_2m_min'))}–{fahrenheit(hi)}°F", C["dim"])], effects))
    return SimpleNamespace(tree=tree, moss=moss, rake=rake, fallen=fallen, log=log)


def bonsai(rng: random.Random, life: int = 26, top: int = -9) -> tuple[dict, list]:
    """A cbonsai-style tree: a trunk climbs and sways, throws shoots left and right that fork, and
    every branch ends in a spray of leaves. All branches grow one cell per tick at once, so the
    cells up to some tick are the tree at that age. Returns {(row, col): (tick, char, leaf?)}, rows
    going up from 0 (no higher than `top`), and each wooden branch's cells as [(tick, row, col)]."""
    cells = {(0, 0): (0, "|", False)}
    sprout = lambda kind, a, life, side=0: {"kind": kind, "r": a["r"], "c": a["c"], "life": life, "age": 0,
                                            "side": side, "cool": 0, "path": []}
    agents = [{**sprout("trunk", {"r": 0, "c": 0}, life, rng.choice((-1, 1))), "cool": 2}]
    branches, tick = [], 0
    while agents and tick < 99:
        tick, born = tick + 1, []
        for a in agents:
            a["life"], a["age"] = a["life"] - 1, a["age"] + 1
            kind = a["kind"]
            if kind == "trunk":
                dr = -1 if a["age"] > 1 and rng.random() < 0.45 else 0
                dc = rng.choice((-1, 0, 0, 1)) if dr else rng.choice((-1, 1))
                dc = 0 if abs(a["c"] + dc) > 3 else dc
            elif kind == "shoot":
                dr, dc = (-1 if rng.random() < 0.3 else 0), a["side"]
            else:
                dr, dc = rng.choice((-1, -1, 0, 1)), rng.choice((-1, 0, 1))
            if a["r"] + dr < top or (kind != "trunk" and a["r"] + dr > -1):  # under the frame, off the ground
                dr = 0
            a["r"], a["c"] = a["r"] + dr, a["c"] + dc
            leaf = kind == "leaf"
            ch = "&" if leaf else "|" if dr and not dc else ("/" if dc > 0 else "\\") if dr else "~"
            cells.setdefault((a["r"], a["c"]), (tick, ch, leaf))
            a["path"].append((tick, a["r"], a["c"]))
            if kind == "trunk":
                a["cool"] -= 1
                if a["life"] > 4 and a["cool"] <= 0 and a["r"] <= -2 and rng.random() < 0.7:
                    born.append(sprout("shoot", a, min(a["life"] + 2, rng.randint(6, 12)), a["side"]))
                    a["side"], a["cool"] = -a["side"], 2
            elif kind == "shoot" and a["life"] > 3 and rng.random() < 0.18:
                born.append(sprout("shoot", a, rng.randint(3, 5), rng.choice((-1, 1))))
            if not leaf and a["life"] <= 2:
                born += [sprout("leaf", a, rng.randint(3, 5)) for _ in range(4)]
                a["life"] = 0
        branches += [a["path"] for a in agents if a["life"] <= 0 and a["kind"] != "leaf"]
        agents = [a for a in agents + born if a["life"] > 0]
    return cells, branches


def chance(*key) -> float:
    """A fixed random number for `key`: the same cell gets the same roll every day, so the garden
    grows instead of reshuffling."""
    return random.Random("/".join(map(str, key))).random()


def foliage(month: int) -> tuple[list[str], float]:
    """Leaf colors and how full the canopy is, by month."""
    if month in (12, 1, 2):
        return [], 0.0
    if month in (3, 4):
        return ["#ffb2fe", "#ff90fe", "#ffb2fe", C["green2"]], 1.0
    if month == 9:
        return [C["green"], C["green2"], C["yellow"], C["active"]], 1.0
    if month == 10:
        return [C["active"], C["red"], C["yellow"], "#f99300"], 0.9
    if month == 11:
        return [C["red"], C["active"], "#9a4a1c"], 0.45
    return [C["green"], C["green2"], "#0a6e48"], 1.0


def garden_widget(cfg: dict, state: dict) -> str:
    """The zen garden in a split terminal: the garden on the left, its growth log on the right."""
    g, garden = cfg["garden"], load_garden()
    planted = dt.date.fromisoformat(str(g["planted"]))
    now = garden.get("now", {})
    today = dt.date.fromisoformat(now["date"]) if now else planted
    life = grow(garden, str(planted))
    W, H, size, lh = 880, 312, 12, 14
    cw, split, gx, gy = size * CW, 566, 18, 40
    sand = gy + 12 * lh                         # grid rows 0-11 are air, 12-16 sand
    rng = random.Random(cfg["login"])
    css, body = [], frame(4, 4, W - 8, H - 8, "zen — ~/garden")
    body.append(text(W - 18, 23, f"grows with {g.get('place', 'the')} weather · the keys above tend it", C["dim"], 11,
                     text_anchor="end"))
    body.append(f'<line{attrs(x1=split, y1=29, x2=split, y2=H - 5, stroke=C["line"])}/>')
    at = lambda r, c: (round(gx + c * cw, 1), round(gy + r * lh, 1))
    glyph = lambda r, c, ch, color, **kw: text(at(r, c)[0], round(at(r, c)[1] + lh * 0.78, 1), ch, color, size, **kw)

    # sand, raked in lines that ring the stones; wind smooths it, a visitor rakes it fresh
    stones, base = [(6, 11, 3), (20, 24, 2), (31, 33, 1)], 46  # stones: (first col, last col, rows tall); the tree's col
    rock = {(r, c) for c0, c1, rows in stones for r in range(15 - rows, 15) for c in range(c0, c1 + 1)}
    rings = [((at(0, c0)[0] + at(0, c1 + 1)[0]) / 2, sand + (3 - rows / 2) * lh, (c1 - c0 + 1) * cw / 2 + 6, rows * lh / 2 + 4)
             for c0, c1, rows in stones]
    body.append(rect(5.5, sand - 4, split - 6, H - 9.5 - sand - 22, fill="#191714"))
    hole = "".join(f'<ellipse{attrs(cx=round(x, 1), cy=round(y, 1), rx=round(rx + 18, 1), ry=round(ry + 10, 1), fill="#000")}/>'
                   for x, y, rx, ry in rings)
    lines = "".join(f"M10 {y}H{split - 6}" for y in range(int(sand), int(H - 34), 6))
    ripples = "".join(f'<ellipse{attrs(cx=round(x, 1), cy=round(y, 1), rx=round(rx + k * 6, 1), ry=round(ry + k * 3.4, 1))}/>'
                      for x, y, rx, ry in rings for k in (1, 2, 3))
    ink = f'stroke="#3a352c" stroke-width="1" fill="none" opacity="{0.3 + 0.7 * life.rake:.2f}"'
    body += [f'<mask id="raked"><rect x="0" y="0" width="{W}" height="{H}" fill="#fff"/>{hole}</mask>',
             f'<path d="{lines}" {ink} mask="url(#raked)"/>', f'<g {ink}>{ripples}</g>']

    # moss at the foot of the stones and the tree, spreading out the more it has rained
    spots = []
    for c0, c1, r0, r1 in [(c0, c1, 15 - rows, 15) for c0, c1, rows in stones] + [(base - 2, base + 2, 11, 12)]:
        for r in range(r0, r1 + 1):
            for c in range(c0 - 4, c1 + 5):
                if (r, c) not in rock and (r, c) != (11, base):
                    spots.append((abs(c - (c0 + c1) / 2) + abs(r - r1) * 2 + 3 * chance("moss", r, c), r, c))
    greens = [C["green"], "#0a6e48", "#11402c", C["green2"]]
    for _, r, c in sorted(set(spots))[:int(life.moss * 5)]:
        body.append(glyph(r, c, ".,:;'"[int(5 * chance("mc", r, c))], greens[int(4 * chance("mg", r, c))]))

    # stones, and a pebble beside one of them for every day someone came by and pressed a key
    for c0, c1, rows in stones:
        for k in range(rows):
            inset = (rows - 1 - k) * 0.5
            x, y = at(15 - rows + k, c0 + inset)
            body.append(rect(x, y, round((c1 - c0 + 1 - 2 * inset) * cw, 1), lh + 0.4, rx=3 if k == 0 else 1,
                             fill="#4a4a4a" if k == 0 else "#3a3a3a"))
    for i, who in enumerate(sorted({v["at"][:10] for v in garden["visits"]})[:24]):
        cx, cy, rx, ry = rings[i % len(rings)]
        turn = math.pi * (0.1 + 0.8 * chance("pebble", who)) + (math.pi if chance("side", who) < 0.3 else 0)
        body.append(f'<ellipse{attrs(cx=round(cx + math.cos(turn) * (rx + 26), 1), cy=round(cy + math.sin(turn) * (ry + 14), 1), rx=3.2, ry=2.2, fill=C["mute"])}/>')

    # the tree, as old as the weather (and the visitors) have let it get
    cells, branches = bonsai(random.Random(f"{cfg['login']}/bonsai"))
    mature = max(t for t, _, _ in cells.values())
    age = round(2 + (mature - 2) * (1 - math.exp(-life.tree / 45)))
    colors, full = foliage(today.month)
    wood = {(r, c): ch for (r, c), (t, ch, leaf) in cells.items() if not leaf and t <= age}
    leaves = {(r, c) for (r, c), (t, _, leaf) in cells.items() if leaf and t <= age}
    for path in branches:  # tufts where branches are still growing
        shown = [p for p in path if p[0] <= age]
        if shown and len(shown) < len(path):
            _, r, c = shown[-1]
            leaves |= {(r + dr, c + dc) for dr in (-1, 0, 1) for dc in (-2, -1, 0, 1, 2)
                       if abs(dr) + abs(dc) <= 2 and chance("tuft", r + dr, c + dc) < 0.75}
    for (r, c), ch in sorted(wood.items()):
        body.append(glyph(11 + r, base + c, ch, "#b07a3c", font_weight="bold"))
    leaves = sorted(p for p in leaves - set(wood) if colors and 11 + p[0] >= 0 and chance("leaf", *p) < full)
    for r, c in leaves:
        body.append(glyph(11 + r, base + c, "&", colors[int(len(colors) * chance("hue", r, c))], font_weight="bold"))
    clear = [c for c in range(2, 58) if not any(c0 - 1 <= c <= c1 + 1 for c0, c1, _ in stones)]
    for k in range(min(36, int(life.fallen)) if colors else 0):  # leaves on the sand
        body.append(glyph(13 + int(3 * chance("fr", k)), clear[int(len(clear) * chance("fc", k))], ",'."[int(3 * chance("fg", k))],
                          colors[int(len(colors) * chance("fh", k))]))

    # a stone lantern on the right, lit at night
    lit = not now.get("is_day", 1)
    for k, row in enumerate(["_/^\\_", " |#| ", "  |  ", " /_\\ "]):
        for i, ch in enumerate(row):
            if ch != " ":
                body.append(glyph(9 + k, 63 + i, ch, (C["yellow"] if lit else C["mute"]) if ch == "#" else "#6a6a6a",
                                  font_weight="bold"))
    if lit:
        x, y = at(10, 65.5)
        body.append('<radialGradient id="glow"><stop offset="0" stop-color="#ffd400" stop-opacity=".22"/>'
                    '<stop offset="1" stop-color="#ffd400" stop-opacity="0"/></radialGradient>'
                    f'<circle{attrs(cx=x, cy=round(y + lh / 2, 1), r=34, fill="url(#glow)")}/>')

    # the weather right now, the only thing that moves
    code, is_day = now.get("weather_code", 0), now.get("is_day", 1)
    word = sky(code)
    if word in ("rain", "drizzle", "showers", "storm") or now.get("precipitation", 0) > 0:
        css.append("@keyframes fall{0%{transform:translateY(-30px)}100%{transform:translateY(200px)}}")
        for k in range(14 if word == "drizzle" else 32):
            x = rng.uniform(12, split - 12)
            body.append(f'<line{attrs(x1=round(x, 1), y1=34, x2=round(x - 2, 1), y2=44, stroke=C["blue2"], stroke_width=1, opacity=0.35)}'
                        f' style="animation:fall {rng.uniform(1.1, 1.8):.2f}s linear {-rng.uniform(0, 2):.2f}s infinite"/>')
    elif word == "snow":
        css.append("@keyframes snow{0%{transform:translate(0,-20px)}50%{transform:translate(6px,90px)}100%{transform:translate(0,200px)}}")
        for k in range(36):
            body.append(f'<circle{attrs(cx=round(rng.uniform(12, split - 12), 1), cy=36, r=1.3, fill=C["bright"], opacity=0.7)}'
                        f' style="animation:snow {rng.uniform(7, 11):.2f}s linear {-rng.uniform(0, 11):.2f}s infinite"/>')
    elif word == "clear" and not is_day:
        body.append(f'<circle{attrs(cx=split - 60, cy=62, r=11, fill="#e8e3c8", opacity=0.85)}/>'
                    f'<circle{attrs(cx=split - 54, cy=58, r=10, fill=C["panel"])}/>')
        css.append("@keyframes tw{0%,100%{opacity:.15}50%{opacity:.7}}")
        for k in range(14):
            body.append(f'<circle{attrs(cx=round(rng.uniform(16, split - 90), 1), cy=round(rng.uniform(40, 120), 1), r=0.9, fill=C["fg"])}'
                        f' style="animation:tw {rng.uniform(2.5, 5):.2f}s ease-in-out {-rng.uniform(0, 5):.2f}s infinite"/>')
    elif word == "clear":
        body.append('<radialGradient id="sun"><stop offset="0" stop-color="#ffd400" stop-opacity=".5"/>'
                    '<stop offset="1" stop-color="#ffd400" stop-opacity="0"/></radialGradient>'
                    f'<circle{attrs(cx=split - 60, cy=64, r=46, fill="url(#sun)")}/><circle{attrs(cx=split - 60, cy=64, r=9, fill="#ffd400", opacity=0.8)}/>')
    elif word in ("cloudy", "fog"):
        css.append(f"@keyframes drift{{0%{{transform:translateX(-160px)}}100%{{transform:translateX({split}px)}}}}")
        for k in range(3 if word == "cloudy" else 6):
            body.append(f'<ellipse{attrs(cx=0, cy=round(rng.uniform(48, 110 if word == "cloudy" else 200), 1), rx=round(rng.uniform(50, 90)), ry=round(rng.uniform(10, 18)), fill=C["fg"], opacity=0.06)}'
                        f' style="animation:drift {rng.uniform(50, 80):.1f}s linear {-rng.uniform(0, 80):.1f}s infinite"/>')
    if colors and age > 6:  # now and then a leaf lets go
        css.append("@keyframes drop{0%,70%{transform:translate(0,0);opacity:0}72%{opacity:1}100%{transform:translate(-40px,110px);opacity:0}}")
        r, c = max(leaves, key=lambda p: (p[1], -p[0])) if leaves else (-5, 4)
        body.append(glyph(11 + r, base + c, "&", colors[0], font_weight="bold",
                          style="animation:drop 9s ease-in 2s infinite;opacity:0"))

    # status line
    days = (today - planted).days
    grown = (age - 2) / max(1, mature - 2)
    stage = "a seedling" if grown < 0.2 else "a sapling" if grown < 0.5 else "a young tree" if grown < 0.9 else "a grown tree"
    visitors = len(garden["visits"])
    temp = f"{fahrenheit(now['temperature_2m'])}°F {word}{'' if is_day else ' night'}" if now else "no weather yet"
    body.append(text(18, H - 18, [(f"day {days}", C["bright"]), (f" since {planted:%b %-d} · ", C["dim"]),
                                  (f"{g.get('place', 'here')} {temp}", C["fg"]), (f" · {stage} · ", C["dim"]),
                                  (f"{visitors} visit{'s' * (visitors != 1)}", C["fg"])], None, 11.5))

    # the log, newest at the bottom like `tail -f`
    lx, fs = split + 14, 11
    body += prompt(lx, 52, "tail -f garden.log", 11.5)
    rows = life.log[-13:] or [(str(planted), [("planted. waiting for the first weather report", C["mute"])], [])]
    for i, (day, words, effects) in enumerate(rows):
        spans = [(dt.date.fromisoformat(day).strftime("%b %d").replace(" 0", "  ")[:6] + "  ", C["dim"]), *words]
        for k, (effect, color) in enumerate(effects[:2]):
            spans.append((("  " if k == 0 else " ") + effect, color))
        body.append(text(lx, 74 + i * 16, spans, None, fs))
    cy = 74 + len(rows) * 16
    body.append(rect(lx, cy - 9.5, fs * CW, 11.5, fill=C["fg"], class_="blink"))
    css.append(".blink{animation:bl 1.1s steps(1) infinite}@keyframes bl{50%{opacity:0}}"
               "@media (prefers-reduced-motion:reduce){*{animation:none!important}}")
    return doc(W, H, body, "".join(css), f"a zen garden planted {planted:%b %-d, %Y} that grows with {g.get('place', 'local')} "
                                         f"weather and visitors' keypresses: day {days}, {stage}, {visitors} visits")


# ── keycaps ──────────────────────────────────────────────────────────────────

def keycap(key: str, legend: str, width: int = 64) -> str:
    W, H = width, 64
    body = ['<defs><linearGradient id="top" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0" stop-color="#2e2e2e"/><stop offset="1" stop-color="#232323"/></linearGradient></defs>',
            rect(1, 5, W - 2, H - 6, rx=11, fill="#070707"),
            rect(1, 1, W - 2, H - 7, rx=11, fill=C["raised"], stroke="#333"),
            rect(7, 4, W - 14, H - 22, rx=8, fill="url(#top)"),
            text(W / 2, 32, key, C["bright"], 20 if len(key) <= 2 else 15, text_anchor="middle", font_weight="bold"),
            text(W / 2, H - 11, legend, C["active"], 8.5, text_anchor="middle")]
    if key == "j":  # the homing bump
        body.append(rect(W / 2 - 7, 38, 14, 2, rx=1, fill=C["dim"]))
    return doc(W, H, body, label=f"{key}: {legend}")


# ── README ───────────────────────────────────────────────────────────────────

WRITTEN: set[Path] = set()


def write_asset(rel: str, content: str) -> str:
    path = ASSETS / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    WRITTEN.add(path)
    return f"assets/{rel}?v={hashlib.sha1(content.encode()).hexdigest()[:8]}"


def prune_assets() -> None:
    """Drop generated files that this render no longer uses."""
    for path in sorted(ASSETS.rglob("*"), reverse=True):
        if path.is_file() and path not in WRITTEN:
            path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def issue_url(login: str, key: str) -> str:
    body = ("Just press **Create** (no need to change anything). A GitHub Action applies the key, "
            "updates the profile, and closes this issue in about half a minute.")
    q = urllib.parse.urlencode({"title": f"hjkl: {key}", "body": body}, quote_via=urllib.parse.quote)
    return f"https://github.com/{login}/{login}/issues/new?{q}"


def render(cfg: dict) -> None:
    if not DATA.exists():
        sys.exit("no data/github.json yet: run `python3 scripts/build.py fetch` first")
    data = json.loads(DATA.read_text())
    state = load_state(cfg)
    login = cfg["login"]
    focus = {"ws": state["ws"], "win": state["win"]}

    v = {"name": cfg["name"], "login": login, "tagline": cfg["tagline"], "education": cfg["education"],
         "updated": parse_ts(data["fetched_at"]).strftime("%Y-%m-%d"), "focus": where(cfg, focus),
         "link": focused(cfg, focus).get("link") or f"https://github.com/{login}"}
    v["desktop"] = write_asset("desktop.svg", desktop(cfg, data, state))

    keys = []
    for key, (_, _, action, legend) in KEYS.items():
        src = write_asset(f"keys/{key}.svg", keycap(key, legend))
        keys.append(f'<a href="{issue_url(login, key)}"><img src="{src}" width="56" alt="{key} {legend}"></a>')
    v["keys"] = "\n".join(f"  {k}" for k in keys)

    last = state["log"][0] if state["log"] else None
    presses = state["presses"]
    v["tally"] = f"<b>{presses}</b> press{'es' * (presses != 1)} so far" + (f' · last <code>{last["key"]}</code>' if last else "")
    # the anonymous route (worker/): no GitHub account, nothing recorded about who pressed
    anon = cfg.get("hjkl", {}).get("anonymous", "").rstrip("/")
    v["anonymous"] = ("no GitHub account? press anonymously: " + " · ".join(
        f'<a href="{anon}/{key}"><b>{key}</b></a>' for key in KEYS) + "<br>\n  ") if anon else ""

    made = [f'<a href="https://github.com/{login}/{name}">{name}</a>' for name in cfg.get("made", [])]
    patches = [f'<a href="https://github.com/{repo}">{repo.split("/")[-1]}</a>' for repo in cfg.get("patches", [])]
    v["made"] = " · ".join(made) + (" · patches to " + " · ".join(patches) if patches else "")

    widgets = cfg.get("widgets", [])
    v.update(start="", garden="")
    if "start" in widgets:
        src = write_asset("widgets/start.svg", start_widget(cfg))
        v["start"] = f'<p align="center">\n  <img src="{src}" width="100%" alt="dylan">\n</p>'
    if "garden" in widgets and cfg.get("garden"):
        src = write_asset("widgets/garden.svg", garden_widget(cfg, state))
        v["garden"] = f'<p align="center">\n  <img src="{src}" width="100%" alt="zen garden">\n</p>'

    out = TEMPLATE.read_text()
    for key, value in v.items():
        out = out.replace("{{" + key + "}}", value)
    README.write_text(out)
    prune_assets()


def main(argv: list[str]) -> int:
    cfg = tomllib.loads(CONFIG.read_text())
    if not argv:
        print(__doc__)
        return 1
    if argv[0] == "press":
        if len(argv) != 3:
            print("usage: build.py press TITLE USER")
            return 1
        try:
            print(press(cfg, argv[1], argv[2]))
        except ValueError as exc:
            print(exc)
            return 2
        render(cfg)
        return 0
    for cmd in argv:
        if cmd == "fetch":
            fetch(cfg)
        elif cmd == "render":
            render(cfg)
        else:
            print(f"unknown command: {cmd}\n{__doc__}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
