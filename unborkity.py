#!/usr/bin/env python3
"""
unborkity: diagnose and repair macOS binaries that fail with "dyld: Library not loaded ... Reason: image not found".

Strategies tried, in order of preference:
  1. brew reinstall    — suggested when binary lives under a Homebrew prefix
  2. add_rpath         — single LC_RPATH append; resolves all @rpath/foo refs
  3. change-per-ref    — per-reference rewrite via `install_name_tool -change`

After any in-place edit on Apple Silicon, the ad-hoc signature is re-applied via :

    `codesign -f -s -`

(Otherwise the kernel might reject the binary.)
"""
from __future__ import annotations

import argparse
import dataclasses
import itertools
import logging
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterable, Iterator

log = logging.getLogger("unborkity")


# ---------------------------------------------------------------------------
# 256-color palette + helpers for the main report.
# Edit numbers here to retheme. Indices are xterm-256
# (run `for c in {0..255}; do printf "\e[38;5;${c}m %3d" $c; done` to preview).
# Disaster-art uses its own 16-color palette (_ANSI dict, further down).

_C_TAG          = 231   # "[unborkity]"
_C_PATH_HEADER  = 202   # admitted-patient path
_C_PHRASE       = 185   # "found N dylib references; ..." + "[N/M]" tag
_C_KIND_BRACKET = 124   # "[" / "]" around kind
_C_KIND_WORD    = 17    # the kind word itself (absolute/rpath/...)
_C_RAW          = 51    # raw dylib reference path
_C_BORKED       = 160   # the word "BORKED" only; rest of msg stays default
_C_DONOR_PATH   = 2     # candidate dylib path on disk
_C_SYSTEM       = 88    # "system lib (untouchable)"
_C_PLAN         = 5     # surgical-plan header + install_name_tool line
_C_PLAN_BIN     = 16    # the "<bin>" placeholder
_C_ALT          = 3     # alternatives header + tip lines

_USE_COLOR = True


def _c(text: str, code: int) -> str:
    """Wrap text in xterm 256-color foreground escape; no-op if color is off."""
    if not _USE_COLOR or not text:
        return text
    return f"\033[38;5;{code}m{text}\033[0m"


def _set_color(enabled: bool) -> None:
    global _USE_COLOR
    _USE_COLOR = enabled


def _color_status(status: str, kind: str, has_resolved: bool,
                  has_candidate: bool) -> str:
    """Color a per-ref status string per the kind/state.

    Only the word "BORKED" is colored; the descriptive middle stays default.
    Candidate-donor path is colored separately.
    """
    if not _USE_COLOR:
        return status
    if kind == "system":
        return _c(status, _C_SYSTEM)
    if has_resolved:
        return status
    if not status.startswith("BORKED"):
        return status
    head = _c("BORKED", _C_BORKED)
    rest = status[len("BORKED"):]
    if has_candidate:
        idx = rest.rfind(": ")
        if idx == -1:
            return head + rest
        middle, donor = rest[:idx + 2], rest[idx + 2:]
        return head + middle + _c(donor, _C_DONOR_PATH)
    return head + rest


# Surgical metaphors for the install_name_tool log lines. The original
# unborkity called every dylib reference a "spleen". Tradition demands.
ORGANS = [
    "spleen", "heart", "brain", "esophagus", "liver", "mesentery",
    "lungs", "thymus", "tonsils", "cornea", "tongue", "bladder",
    "pancreas", "gallbladder", "appendix",
]

CRASH_LANDING = r"""
              __
              \  \     _ _
               \**\ ___\/ \
             X*#####*+^^\__\
               o/\  \
                  \__\
"""

# Body parts used in the per-ref BORKED status line. Distinct from ORGANS
# (which is consumed by apply_ops log lines) on purpose — different vibe.
BODY_PARTS = [
    "kidney", "liver", "lung", "heart", "pancreas",
    "small intestine", "large intestine", "left hand",
    "right hand", "face",
]

# Cycling pool: yield each body part once before reshuffling. Each full
# pass-through bumps the cycle counter so the donor-found line can escalate
# its tone ("found" → "complications" → "OMG so many complications").
_body_parts_pool: list[str] = []
_body_parts_cycle: int = 0


def _next_body_part() -> tuple[str, int]:
    """Return (part, cycle_number). Refills + reshuffles when pool drains."""
    global _body_parts_pool, _body_parts_cycle
    if not _body_parts_pool:
        _body_parts_pool = list(BODY_PARTS)
        random.shuffle(_body_parts_pool)
        _body_parts_cycle += 1
    return _body_parts_pool.pop(), _body_parts_cycle


def _ecosystem(path: str) -> str:
    """Classify a path's package-manager ecosystem from prefix.

    Loose substring heuristic — labels surface in the donor-found
    line so users can spot cross-ecosystem grafts (brew patient,
    conda donor) at a glance, and feed `--ecosystem-only` filtering.
    """
    real = os.path.realpath(path)
    if real.startswith("/opt/homebrew/"):
        return "brew-arm"
    if real.startswith(("/usr/local/Cellar/", "/usr/local/opt/", "/usr/local/lib/")):
        return "brew-x86"
    if real.startswith("/opt/local/"):
        return "macports"
    if any(seg in real for seg in (
        "/anaconda", "/miniconda", "/miniforge", "/radioconda",
        "/mambaforge", "/conda3/", "/conda/",
    )):
        return "conda"
    if real.startswith(("/usr/lib/", "/System/", "/Library/Apple/")):
        return "system"
    if real.startswith("/Applications/"):
        return "apps"
    return "other"


def _donor_found_msg(part: str, cycle: int, candidate: str) -> str:
    tag = f" [{_ecosystem(candidate)}]"
    if cycle <= 1:
        return f"BORKED -- donor's {part} found on disk: {candidate}{tag}"
    if cycle == 2:
        return (f"BORKED -- Complications arose! "
                f"Had to find another donor's {part} on the disk: {candidate}{tag}")
    return (f"BORKED -- OMG. So many complications! "
            f"Had to find yet another donor's {part} on the disk: {candidate}{tag}")

# Verb phrases for the "found N references; <phrase>:" header.
DIAGNOSE_PHRASES = [
    "auscultating each",
    "the quack is looking at each",
    "snake-oil saleswoman is ruminating on care of each",
    "telemedicine specialist is playing minesweeper while looking at each",
]

# Header above the install_name_tool plan in the full report.
PLAN_HEADERS = [
    "surgical plan:",
    "doctor's orders are:",
]


class UnborkityError(Exception):
    """Abnormal exit: the __main__ wrapper prints the disaster screen."""

# Reference kinds reported by `otool -L`.
SYSTEM_PREFIXES = ("/usr/lib/", "/System/", "/Library/Apple/")
HOMEBREW_PREFIXES = ("/opt/homebrew/", "/usr/local/Cellar/", "/usr/local/opt/")
SEARCH_PATHS = [
    "/opt/homebrew/lib",
    "/opt/homebrew/Cellar",
    "/opt/homebrew/opt",
    "/usr/local/lib",
    "/usr/local/Cellar",
    "/usr/local/opt",
]
# matches the parenthesized "(compatibility version ..., current version ...)" suffix
OTOOL_LINE_RE = re.compile(r"^\s+(?P<path>.+?)\s+\(compatibility version .*\)\s*$")

# External CLI tools that must be on PATH for unborkity to do anything.
# Ship with Xcode Command Line Tools (install: `xcode-select --install`).
REQUIRED_TOOLS = ("otool", "install_name_tool", "codesign")


def _preflight_tools(required: tuple[str, ...] = REQUIRED_TOOLS) -> None:
    """Verify required macOS CLI tools are on PATH.

    Raises UnborkityError listing every missing tool in one shot, plus the
    install hint. Called first thing in main() so we fail before any patient
    is admitted.
    """
    missing = [t for t in required if shutil.which(t) is None]
    if not missing:
        return
    raise UnborkityError(
        "missing required tool(s): " + ", ".join(missing) + "\n"
        "  these ship with Xcode Command Line Tools.\n"
        "  install with:  xcode-select --install\n"
        "  (already installed? confirm /usr/bin and the active toolchain "
        "(`xcode-select -p`/usr/bin) are on your PATH)"
    )


class _Spinner:
    """Tiny spinner for slow ops; no-op if stdout isn't a TTY."""
    _CHARS = "|/-\\"

    def __init__(self, prefix: str):
        self.prefix = prefix
        self._stop = threading.Event()
        self._t: threading.Thread | None = None
        self._enabled = sys.stdout.isatty()

    def __enter__(self):
        if not self._enabled:
            return self

        def loop() -> None:
            for ch in itertools.cycle(self._CHARS):
                if self._stop.is_set():
                    break
                sys.stdout.write(f"\r  {self.prefix} {ch}")
                sys.stdout.flush()
                time.sleep(0.1)

        self._t = threading.Thread(target=loop, daemon=True)
        self._t.start()
        return self

    def __exit__(self, *exc):
        if not self._enabled:
            return False
        self._stop.set()
        if self._t is not None:
            self._t.join()
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()
        return False


# ---------------------------------------------------------------------------
# disaster screen — printed on abnormal exit

_ANSI = {
    "rst":  "\033[0m",
    "red":  "\033[31m", "br_red":  "\033[91m",
    "yel":  "\033[33m", "br_yel":  "\033[93m",
    "blu":  "\033[34m", "br_blu":  "\033[94m",
    "wht":  "\033[37m", "br_wht":  "\033[97m",
    "gry":  "\033[90m",
    "mag":  "\033[35m", "br_mag":  "\033[95m",
    "org":     "\033[38;5;208m",
    "org_lt":  "\033[38;5;214m",
    "org_dk":  "\033[38;5;202m",
}

# horizon gradient (outermost edge -> inward toward the middle).
# last char of each edge is brightest red; innermost of the six is yellow.
_HORIZON_GRADIENT = ["red", "br_red", "org_dk", "org", "org_lt", "yel"]


def _gradient_edges(line: str, mid_color: str,
                    edge_palette: list[str]) -> str:
    """Color last len(palette) chars on each edge per palette (outer->inner);
    middle gets mid_color."""
    n = len(edge_palette)
    if len(line) < 2 * n:
        return _paint(line, mid_color, True)
    left = line[:n]
    middle = line[n:-n]
    right = line[-n:]
    out = ""
    for ch, col in zip(left, edge_palette):
        out += _paint(ch, col, True)
    out += _paint(middle, mid_color, True)
    for ch, col in zip(right, reversed(edge_palette)):
        out += _paint(ch, col, True)
    return out


def _paint(s: str, color: str, enable: bool) -> str:
    if not enable:
        return s
    return f"{_ANSI[color]}{s}{_ANSI['rst']}"


def _blimp_art(color: bool) -> str:
    """Hindenburg with falling flames (the ঌ chars)."""
    body = [
        "",
        "           ঌ",
        "             ঌ",
        " ঌ            ঌঌ",
        "⠀⢸⣿⣶⣦⡀⠀⠀⠀⠀ ⠀⠀⠀ঌঌঌঌ⠀⠀ঌ⠀⠀⠀⠀⠀⠀",
        "⠀⠀⠀⠈⣿⠿⠟⢓⣀⣠⣤⣤⣶⣶⣶⣶⣶⣶⣶⣶⣶⣶⣶⣶⣤⣤⣀⠀⠀⠀",
        "⠀⠀⠀⣀⣤⣶⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣄⠀",
        "⠀⠒⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠀",
        "⠀⠀⠀⠈⡉⠻⠿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠃⠀",
        "⠀⠀⠀⢠⣿⣿⣶⡦⠈⠉⠉⠛⠛⠻⠿⠿⠿⠿⠿⠿⠿⠿⠛⠛⠛⠉⠁⠀⠀⠀",
        "⠀⠀⠀⢸⠿⠟⠋⠀⠀⠀⠀⠀⠀⠀⠀⢰⣶⣶⣶⣶⡶⠀⠀⠀⠀⠀⠀⠀⠀⠀",
        "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠉⠉⠉⠉⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀",
    ]
    out = []
    for ln in body:
        if not color:
            out.append(ln)
            continue
        styled = ""
        for ch in ln:
            if ch == "ঌ":
                styled += _paint(ch, random.choice(["br_red", "br_yel", "red"]), True)
            else:
                styled += ch
        out.append(styled)
    return "\n".join(out)


def _mushroom_art(color: bool) -> str:
    """Atomic blast — top cloud is fire, base is shockwave."""
    lines = [
        "                            ____",
        "                     __,-~~/~    `---.",
        "                   _/_,---(      ,    )",
        "               __ /        <    /   )  \\___",
        "- ------===;;;'====------------------===;;;===----- -  -",
        "                  \\/  ~\"~\"~\"~\"~\"~\\~\"~)~\"/",
        "                  (_ (   \\  (     >    \\)",
        "                   \\_( _ <         >_>'",
        "                      ~ `-i' ::>|--\"",
        "                          I;|.|.|",
        "                         <|i::|i|`.",
        "                         ` ^'\"`-' \"",
    ]
    if not color:
        return "\n".join(lines)
    palette = [
        "br_yel",   # top of cloud — bright fire
        "br_yel",
        "br_red",
        "br_red",
        "org",      # shockwave horizon
        "br_red",
        "yel",
        "yel",
        "br_red",
        "gry",
        "gry",
        "gry",
    ]
    out = []
    for i, (ln, c) in enumerate(zip(lines, palette)):
        if i == 4:  # horizon shockwave: gradient on outer 6 chars each side
            out.append(_gradient_edges(ln, c, _HORIZON_GRADIENT))
        else:
            out.append(_paint(ln, c, True))
    return "\n".join(out)


def _skull_art(color: bool) -> str:
    """Bone-white skull, glowing red eye sockets (the ⢿ chars)."""
    lines = [
        "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
        "⠀⠀⠀⠀⠀⠀⠀⠀⣠⣴⣿⣿⣿⣿⣿⣿⣶⣄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
        "⠀⠀⠀⠀⠀⠀⢀⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⡄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
        "⠀⠀⠀⠀⠀⠀⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣧⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
        "⠀⠀⠀⠀⠀⠀⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡏⢿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
        "⠀⠀⠀⠀⠀⠀⢻⣿⢿⣿⣿⣿⣿⣿⣿⣿⡿⠿⠿⠶⣆⠀⠀⠀⠀⠀⠀⠀⠀⠀",
        "⠀⠀⠀⠀⠀⠀⠀⠻⣠⡿⠿⠛⠻⢿⣿⡇⠀⠀⠀⠀⢿⣄⠀⠀⠀⠀⠀⠀⠀⠀",
        "⠀⠀⠀⠀⠀⠀⠀⢸⣏⠀⠀⠀⠀⣸⡿⢷⣦⣤⣤⣴⣿⠟⠀⠀⢀⣴⣶⡄⠀⠀",
        "⠀⠀⠀⠀⠀⠀⠀⠀⣿⣤⣤⣴⣿⣿⡅⡄⢹⣿⡿⠛⠋⠀⠀⢀⣼⣿⣿⣿⡧⠀",
        "⠀⠀⠀⠀⢀⡀⠀⠀⠈⠛⠟⠛⢻⣿⣿⣿⣿⣿⡿⠀⢀⣤⣶⣿⡿⠛⠋⠉⠀⠀",
        "⠀⠀⠀⠀⣿⣿⣦⣤⣤⣄⣀⣀⡈⢛⠛⠛⠛⠁⣠⣾⡿⠛⠉⠀⠀⠀⠀⠀⣀⠀",
        "⠀⠀⠀⢼⣿⣿⣿⠿⠿⠿⠿⠿⢿⢿⣿⣿⣿⣿⣿⣿⣶⣶⣶⣶⣶⣶⣶⣾⣿⣏",
        "⠀⠀⠀⠈⠉⠀⠀⠀⠀⠀⠀⠀⣠⣶⣿⠿⠋⠁⠉⠉⠉⠉⠉⠙⠛⠛⠻⠿⠿⣿",
    ]
    out = []
    for i, ln in enumerate(lines):
        if not color:
            out.append(ln)
            continue
        # mark eye sockets red — line 4 has the lone ⢿ on the right; line 5 has ⢻⣿⢿
        if i in (4, 5):
            ln = ln.replace("⢿", _paint("⢿", "br_red", True))
            ln = ln.replace("⢻", _paint("⢻", "br_red", True))
        out.append(_paint(ln, "br_wht", True))
    return "\n".join(out)


#
# all these taken from online searching
#
_DISASTER_ART = (_blimp_art, _mushroom_art, _skull_art)


def _print_disaster(message: str) -> None:
    """Print error msg + dramatic exit screen."""
    color = sys.stdout.isatty()
    print()  # blank line above error
    print(message)
    print()  # blank line between error and art block
    print(_paint("Aieee...", "br_red", color))
    print(_paint("No chance to bail out, coming down hard!", "br_blu", color))
    print()  # blank line before art
    art_fn = random.choice(_DISASTER_ART)
    print(art_fn(color))
    print()  # blank line after art


@dataclasses.dataclass
class LibRef:
    """One line from `otool -L`."""
    raw: str                 # exactly as printed (e.g. "@rpath/libfoo.dylib")
    basename: str            # "libfoo.dylib"
    kind: str                # absolute|rpath|loader|executable|relative|system
    resolved: str | None     # what dyld would currently load (None = loader fails)
    candidate: str | None    # repair candidate located via mdfind / SEARCH_PATHS

    @property
    def is_broken(self) -> bool:
        return self.resolved is None and self.kind != "system"


@dataclasses.dataclass
class FixOp:
    """A single install_name_tool invocation."""
    op: str                  # "add_rpath" | "change"
    args: tuple[str, ...]    # passed straight to install_name_tool

    def cmd(self, binary: str) -> list[str]:
        return ["install_name_tool", *(["-" + self.op]), *self.args, binary]


# ---------------------------------------------------------------------------
# diagnosis

def classify(raw: str) -> str:
    if raw.startswith("@rpath/"):
        return "rpath"
    if raw.startswith("@loader_path/"):
        return "loader"
    if raw.startswith("@executable_path/"):
        return "executable"
    if raw.startswith(SYSTEM_PREFIXES):
        return "system"
    if raw.startswith("/"):
        return "absolute"
    return "relative"


def run_otool(binary: str) -> list[str]:
    """Return raw lib reference strings from `otool -L`. Skips first line (the binary itself)."""
    try:
        proc = subprocess.run(
            ["otool", "-L", binary],
            check=True, capture_output=True, text=True,
        )
    except FileNotFoundError as e:
        raise UnborkityError("otool not found on PATH") from e
    except subprocess.CalledProcessError as e:
        raise UnborkityError(
            f"otool failed on {binary} (rc={e.returncode}): {e.stderr.strip()}"
        ) from e

    refs: list[str] = []
    for line in proc.stdout.splitlines():
        # Skip path-echo headers: single-arch ("<path>:") and the per-arch
        # slice headers in fat/universal binaries ("<path> (architecture x):").
        # All header forms have no leading whitespace and end with ':'.
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            continue
        m = OTOOL_LINE_RE.match(line)
        if not m:
            log.debug("skipping unparseable otool line: %r", line)
            continue
        refs.append(m.group("path"))
    return refs


def get_rpaths(binary: str) -> list[str]:
    """Parse LC_RPATH entries from `otool -l`."""
    try:
        proc = subprocess.run(
            ["otool", "-l", binary],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        log.warning("otool -l failed on %s: %s", binary, e.stderr.strip())
        return []
    rpaths: list[str] = []
    in_rpath_cmd = False
    for line in proc.stdout.splitlines():
        s = line.strip()
        if s.startswith("cmd "):
            in_rpath_cmd = (s == "cmd LC_RPATH")
            continue
        if in_rpath_cmd and s.startswith("path "):
            # "path /opt/homebrew/lib (offset 12)"
            rest = s[len("path "):]
            rpath = rest.rsplit(" (offset", 1)[0]
            rpaths.append(rpath)
    return rpaths


# Per-process caches for the donor hunt.
#  _FIND_CACHE: basename -> resolved path (or None for confirmed-miss).
#  _HOT_DIRS:   MRU list of directories where past finds succeeded; tried first
#               for subsequent basenames (cheap stat vs. mdfind subprocess).
_FIND_CACHE: dict[str, str | None] = {}
_HOT_DIRS: list[str] = []
_HOT_DIRS_MAX = 16
# `--ecosystem-only`: when set, diagnose() drops candidates whose ecosystem
# differs from the patient's (e.g. brew binary, conda donor → ABI roulette).
_ECO_ONLY: bool = False


def _set_eco_only(b: bool) -> None:
    global _ECO_ONLY
    _ECO_ONLY = b
# Cap basenames per mdfind invocation. Each clause adds ~30 chars + basename;
# 50 stays comfortably under macOS ARG_MAX (~256 KB) even with very long names.
_BULK_MDFIND_CHUNK = 50


def _bump_hot_dir(d: str) -> None:
    """Move dir to front of MRU list; cap length."""
    if d in _HOT_DIRS:
        _HOT_DIRS.remove(d)
    _HOT_DIRS.insert(0, d)
    del _HOT_DIRS[_HOT_DIRS_MAX:]


def _try_hot_dirs(basename: str) -> str | None:
    """Cheap pre-check: if a past find hit dir D, look there first."""
    for d in _HOT_DIRS:
        p = os.path.join(d, basename)
        if os.path.isfile(p):
            return os.path.realpath(p)
    return None


def _bulk_mdfind(basenames: list[str]) -> dict[str, str]:
    """Run one mdfind subprocess per chunk, OR-joining basename clauses.

    Returns {basename: realpath} for hits. Misses are absent from the dict
    (so callers can fall back to walk for stragglers). Bumps each hit's
    directory into _HOT_DIRS.
    """
    found: dict[str, str] = {}
    if not basenames:
        return found
    seen = set()
    uniq = [b for b in basenames if not (b in seen or seen.add(b))]
    for i in range(0, len(uniq), _BULK_MDFIND_CHUNK):
        chunk = uniq[i:i + _BULK_MDFIND_CHUNK]
        query = " || ".join(f'kMDItemFSName == "{b}"' for b in chunk)
        try:
            out = subprocess.run(
                ["mdfind", query],
                check=False, capture_output=True, text=True, timeout=15,
            ).stdout.strip().splitlines()
        except subprocess.TimeoutExpired:
            log.debug("bulk mdfind timed out on chunk of %d", len(chunk))
            continue
        wanted = set(chunk)
        for hit in out:
            if not hit:
                continue
            base = os.path.basename(hit)
            if base in wanted and base not in found and os.path.isfile(hit):
                real = os.path.realpath(hit)
                found[base] = real
                _bump_hot_dir(os.path.dirname(real))
    return found


def prewarm_find_lib(basenames: list[str]) -> None:
    """Pre-fill _FIND_CACHE for many basenames in one shot.

    Order: hot-dir check (free), then bulk mdfind (one subprocess per chunk).
    Stragglers stay uncached; per-call find_lib() will fall through to
    its own walk.
    """
    todo = [b for b in basenames if b not in _FIND_CACHE]
    if not todo:
        return
    still_todo = []
    for b in todo:
        hit = _try_hot_dirs(b)
        if hit:
            _FIND_CACHE[b] = hit
        else:
            still_todo.append(b)
    if not still_todo:
        return
    bulk = _bulk_mdfind(still_todo)
    for b, p in bulk.items():
        _FIND_CACHE[b] = p


def find_lib(basename: str) -> str | None:
    """Locate a dylib by basename. Hot-dir → cache → mdfind → walk."""
    if basename in _FIND_CACHE:
        return _FIND_CACHE[basename]

    hit = _try_hot_dirs(basename)
    if hit:
        _FIND_CACHE[basename] = hit
        return hit

    # mdfind (single-basename — bulk path warms the cache up front)
    try:
        out = subprocess.run(
            ["mdfind", f"kMDItemFSName == {basename}"],
            check=False, capture_output=True, text=True, timeout=10,
        ).stdout.strip().splitlines()
    except subprocess.TimeoutExpired:
        log.debug("mdfind timed out on %s", basename)
        out = []
    for raw in out:
        if raw and os.path.isfile(raw):
            real = os.path.realpath(raw)
            log.debug("mdfind located %s -> %s", basename, real)
            _FIND_CACHE[basename] = real
            _bump_hot_dir(os.path.dirname(real))
            return real

    # fallback: shallow walk of usual haunts
    for root in SEARCH_PATHS:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root, followlinks=False):
            if basename in files:
                real = os.path.realpath(os.path.join(dirpath, basename))
                log.debug("walk located %s -> %s", basename, real)
                _FIND_CACHE[basename] = real
                _bump_hot_dir(os.path.dirname(real))
                return real

    log.debug("could not locate %s in mdfind or %s", basename, SEARCH_PATHS)
    _FIND_CACHE[basename] = None
    return None


def resolve_ref(raw: str, binary: str, rpaths: list[str]) -> str | None:
    """Try to map a raw lib ref to a real on-disk path."""
    kind = classify(raw)
    if kind == "system":
        # post-Big Sur the file may not exist on disk; dyld serves it from
        # the shared cache, so by definition the loader can resolve it.
        return raw
    if kind == "absolute":
        return raw if os.path.isfile(raw) else None
    if kind == "loader":
        cand = os.path.normpath(os.path.join(os.path.dirname(binary), raw[len("@loader_path/"):]))
        return cand if os.path.isfile(cand) else None
    if kind == "executable":
        cand = os.path.normpath(os.path.join(os.path.dirname(binary), raw[len("@executable_path/"):]))
        return cand if os.path.isfile(cand) else None
    if kind == "rpath":
        rel = raw[len("@rpath/"):]
        # rpath entries can themselves contain @loader_path / @executable_path
        # (very common: `@loader_path/../lib`). dyld expands these; we must too,
        # else we false-positive borked on perfectly fine binaries.
        bin_dir = os.path.dirname(binary)
        for rp in rpaths:
            expanded = rp
            if expanded.startswith("@loader_path"):
                expanded = bin_dir + expanded[len("@loader_path"):]
            elif expanded.startswith("@executable_path"):
                expanded = bin_dir + expanded[len("@executable_path"):]
            cand = os.path.normpath(os.path.join(expanded, rel))
            if os.path.isfile(cand):
                return cand
        return None
    if kind == "relative":
        # final desperate fallback: search by basename
        return find_lib(os.path.basename(raw))
    return None


def diagnose(binary: str, find_candidates: bool = True,
             progress: bool = False) -> list[LibRef]:
    """Return one LibRef per dylib reference.

    `resolved` reflects what dyld would currently load; `candidate` is a
    same-basename dylib located by mdfind / SEARCH_PATHS, used to plan repairs.
    Set `find_candidates=False` to skip the (slow) on-disk search — useful for
    bulk triage where we only need to know broken vs. healthy.
    Set `progress=True` to emit per-ref status to stderr as we go (signs of
    life during the slow on-disk donor hunt).
    """
    raws = run_otool(binary)
    rpaths = get_rpaths(binary)
    refs: list[LibRef] = []
    n = len(raws)
    if progress:
        plural = "" if n == 1 else "s"
        phrase = random.choice(DIAGNOSE_PHRASES)
        print(_c(f"  found {n} dylib reference{plural}; {phrase}:", _C_PHRASE),
              flush=True)

    # Pass 1: classify + resolve all refs up front so we know which need a
    # disk hunt. Cheap (no subprocess work).
    prepared: list[tuple[int, str, str, str | None]] = []
    for i, raw in enumerate(raws, 1):
        kind = classify(raw)
        resolved = resolve_ref(raw, binary, rpaths)
        prepared.append((i, raw, kind, resolved))

    # Bulk pre-warm: one mdfind call covers every unresolved basename.
    # Subsequent per-ref find_lib() lookups hit the cache instantly.
    if find_candidates:
        unresolved = [os.path.basename(raw)
                      for _, raw, kind, resolved in prepared
                      if resolved is None and kind != "system"]
        if unresolved:
            n_uniq = len(set(unresolved))
            if progress:
                with _Spinner(f"bulk-hunting donors for {n_uniq} ref(s)"):
                    prewarm_find_lib(unresolved)
            else:
                prewarm_find_lib(unresolved)

    binary_eco = _ecosystem(binary)
    for i, raw, kind, resolved in prepared:
        tag = f"[{i:>2}/{n}]"
        candidate = None
        rejected_donor: str | None = None
        if find_candidates and resolved is None and kind != "system":
            if progress:
                with _Spinner(f"{tag} hunting donor for {os.path.basename(raw)}"):
                    candidate = find_lib(os.path.basename(raw))
            else:
                candidate = find_lib(os.path.basename(raw))
            # --ecosystem-only: drop cross-ecosystem donor (ABI roulette).
            if candidate and _ECO_ONLY and _ecosystem(candidate) != binary_eco:
                rejected_donor = candidate
                candidate = None
        if progress:
            if kind == "system":
                status = "system lib (untouchable)"
            elif resolved:
                status = f"resolves -> {resolved}"
            elif candidate:
                part, cycle = _next_body_part()
                status = _donor_found_msg(part, cycle, candidate)
            elif rejected_donor:
                part, _ = _next_body_part()
                status = (f"BORKED -- donor {part} found at {rejected_donor} "
                          f"[{_ecosystem(rejected_donor)}] but rejected: "
                          f"ecosystem != patient's [{binary_eco}]")
            else:
                part, _ = _next_body_part()
                status = f"BORKED -- no donor {part} located"
            status_c = _color_status(status, kind, resolved is not None,
                                     candidate is not None)
            row = (
                f"  {_c(tag, _C_PHRASE)} "
                f"{_c('[', _C_KIND_BRACKET)}{_c(f'{kind:<10}', _C_KIND_WORD)}"
                f"{_c(']', _C_KIND_BRACKET)} "
                f"{_c(raw, _C_RAW)}  {status_c}"
            )
            print(row, flush=True)
        refs.append(LibRef(
            raw=raw,
            basename=os.path.basename(raw),
            kind=kind,
            resolved=resolved,
            candidate=candidate,
        ))
    return refs


def diagnose_deep(binary: str, max_depth: int = 8
                  ) -> list[tuple[str, int, list[LibRef]]]:
    """BFS the dylib graph from `binary`, diagnosing each node.

    Returns a list of (path, depth, refs) tuples in visit order. Skips
    system libs (in shared cache, untouchable). Cycle-safe via realpath
    seen-set. find_candidates=False at every node — deep mode is for
    surveying the cascade, not planning per-dep grafts.
    """
    out: list[tuple[str, int, list[LibRef]]] = []
    seen: set[str] = set()
    queue: list[tuple[str, int]] = [(binary, 0)]
    while queue:
        path, depth = queue.pop(0)
        real = os.path.realpath(path)
        if real in seen or depth > max_depth:
            continue
        seen.add(real)
        try:
            refs = diagnose(real, find_candidates=False, progress=False)
        except (UnborkityError, subprocess.CalledProcessError):
            continue
        out.append((real, depth, refs))
        for r in refs:
            if r.kind == "system" or not r.resolved:
                continue
            if os.path.realpath(r.resolved) not in seen:
                queue.append((r.resolved, depth + 1))
    return out


def render_deep(walk: list[tuple[str, int, list[LibRef]]],
                root: str) -> str:
    """Tree-ish report for --deep: only show nodes with broken refs."""
    lines: list[str] = ["", _c("--- deep report (transitive dylib graph) ---", _C_PHRASE)]
    bad_nodes = [(p, d, refs) for p, d, refs in walk
                 if any(r.is_broken for r in refs)]
    if not bad_nodes:
        n_clean = len(walk)
        lines.append(f"  no cascading borks across {n_clean} reachable lib(s) — "
                     f"clean all the way down")
        return "\n".join(lines)
    for path, depth, refs in bad_nodes:
        broken = [r for r in refs if r.is_broken]
        indent = "  " * (depth + 1)
        marker = _c("BORKED", _C_BORKED) if path == root else _c("(dep)", _C_PHRASE)
        lines.append(f"{indent}{marker} {path}  ({len(broken)} broken)")
        for r in broken:
            lines.append(f"{indent}  - {r.raw}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# planning

def under_homebrew(path: str) -> bool:
    real = os.path.realpath(path)
    return real.startswith(HOMEBREW_PREFIXES)


def brew_package_for(path: str) -> str | None:
    """Best-effort: ask `brew which-formula` (Homebrew >= 4) what owns this binary."""
    if not shutil.which("brew"):
        return None
    try:
        proc = subprocess.run(
            ["brew", "--prefix"], check=True, capture_output=True, text=True, timeout=5,
        )
        prefix = proc.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    real = os.path.realpath(path)
    # parse Cellar path: $prefix/Cellar/<formula>/<version>/...
    cellar = os.path.join(prefix, "Cellar") + "/"
    if real.startswith(cellar):
        return real[len(cellar):].split("/", 1)[0]
    return None


def plan_fixes(binary: str, refs: list[LibRef]) -> list[FixOp]:
    """Decide which install_name_tool invocations would repair the binary."""
    ops: list[FixOp] = []
    existing_rpaths = set(get_rpaths(binary))

    # Group broken refs by kind.
    broken = [r for r in refs if r.is_broken]
    if not broken:
        return ops

    # Strategy 1 — for broken @rpath refs, gather distinct dirs of candidates
    # and add them as rpaths (one rpath append can fix many refs).
    rpath_dirs: set[str] = set()
    for r in broken:
        if r.kind != "rpath":
            continue
        if r.candidate:
            rpath_dirs.add(os.path.dirname(r.candidate))
    for d in sorted(rpath_dirs):
        if d in existing_rpaths:
            continue
        ops.append(FixOp("add_rpath", (d,)))

    # Strategy 2 — for broken absolute or relative refs, rewrite each one.
    for r in broken:
        if r.kind == "rpath":
            continue
        if not r.candidate:
            log.error("no donor %s found for %s (kind=%s)\n%s\n — leaving broken",
                      random.choice(ORGANS), r.raw, r.kind, CRASH_LANDING)
            continue
        ops.append(FixOp("change", (r.raw, r.candidate)))
    return ops


# ---------------------------------------------------------------------------
# application

def apply_ops(binary: str, ops: list[FixOp], backup_dir: str = "/tmp",
              make_backup: bool = True) -> None:
    """Backup, run install_name_tool, re-sign on arm64.

    `make_backup=False` skips the .bak copy. On failure we cannot restore,
    so we re-raise with a clear note — caller opted out of the safety net.
    """
    if not ops:
        log.info("no operations to apply — patient is in perfect health")
        return

    backup: str | None = None
    if make_backup:
        backup = os.path.join(backup_dir, os.path.basename(binary) + ".unborkity.bak")
        log.info("scrubbing in. backup of the patient -> %s", backup)
        shutil.copy2(binary, backup)
    else:
        log.info("scrubbing in. -n set: no backup, no take-backsies")
    log.info("I'm a doctor, not a binary hacker. this might sting a bit...")

    for op in ops:
        cmd = op.cmd(binary)
        organ = random.choice(ORGANS)
        if op.op == "change":
            old, new = op.args
            log.info("transplanting %s: removing old %s [%s], suturing in new one [%s]",
                     organ, organ, old, new)
        elif op.op == "add_rpath":
            log.info("grafting a new %s onto the patient (rpath = %s)", organ, op.args[0])
        log.info("scalpel: %s", " ".join(cmd))

        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            log.error("the %s rejected! install_name_tool rc=%d: %s",
                      organ, proc.returncode, proc.stderr.strip())
            if backup is not None:
                log.error("reversing the procedure — restoring from backup")
                shutil.copy2(backup, binary)
                raise UnborkityError(
                    f"install_name_tool rejected the {op.op}: {proc.stderr.strip()}"
                )
            raise UnborkityError(
                f"install_name_tool rejected the {op.op}: {proc.stderr.strip()} "
                f"(no backup taken; binary may be in a partially-modified state)"
            )

    if platform.machine() == "arm64":
        log.info("re-stamping the patient's wristband (ad-hoc codesign for arm64)")
        proc = subprocess.run(
            ["codesign", "-f", "-s", "-", binary],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise UnborkityError(f"codesign failed: {proc.stderr.strip()}")
    log.info("patient is up and walking. closing.")


# ---------------------------------------------------------------------------
# CLI

@dataclasses.dataclass
class ScanResult:
    binary: str
    status: str             # "ok" | "borked" | "skipped"
    broken_refs: list[str]  # raw refs that don't resolve
    note: str = ""          # reason for "skipped"


def is_mach_o(path: str) -> bool:
    """Cheap check — read first 4 bytes and look for any Mach-O magic."""
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
    except OSError:
        return False
    # 32/64-bit, both endiannesses, plus universal/fat
    return magic in (
        b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",  # MH_MAGIC_64 / be
        b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce",  # MH_MAGIC    / be
        b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",  # FAT_MAGIC
    )


def scan(binaries: Iterable[str]) -> Iterator[ScanResult]:
    """Triage a list of binaries — fast, no on-disk candidate hunting.

    Yields one ScanResult per binary as it's examined. Streaming so that
    large lists (4k+ binaries on a Homebrew prefix) start producing output
    immediately instead of waiting for the whole sweep to finish.
    """
    for path in binaries:
        if not os.path.exists(path):
            yield ScanResult(path, "skipped", [], "not found")
            continue
        real = os.path.realpath(path)
        if not os.path.isfile(real):
            yield ScanResult(path, "skipped", [], "not a regular file")
            continue
        if not is_mach_o(real):
            yield ScanResult(path, "skipped", [], "not a Mach-O binary")
            continue
        try:
            refs = diagnose(real, find_candidates=False)
        except (subprocess.CalledProcessError, UnborkityError) as e:
            yield ScanResult(path, "skipped", [], f"otool failed: {e}")
            continue
        broken = [r.raw for r in refs if r.is_broken]
        yield ScanResult(
            binary=path,
            status="borked" if broken else "ok",
            broken_refs=broken,
        )


def render_scan(results: Iterable[ScanResult], color: bool = True,
                borked_only: bool = False, minimal: bool = False) -> int:
    """Stream one line per binary as it arrives: STATUS path [: broken refs].

    Prints to stdout live (so a 4k-binary sweep starts producing output
    immediately). Returns the number of borked binaries found.

    `minimal=True` drops the per-binary broken-ref list (status only).
    """
    def paint(s: str, code: str) -> str:
        return f"\033[{code}m{s}\033[0m" if color else s

    n_borked = n_ok = n_skip = 0
    for r in results:
        if r.status == "ok":
            n_ok += 1
        elif r.status == "borked":
            n_borked += 1
        else:
            n_skip += 1

        if borked_only and r.status != "borked":
            continue
        if r.status == "ok":
            tag = paint("  ok  ", "32")
            print(f"{tag} {r.binary}", flush=True)
        elif r.status == "borked":
            tag = paint("BORKED", "31;1")
            if minimal:
                print(f"{tag} {r.binary}", flush=True)
            else:
                refs = ", ".join(r.broken_refs)
                print(f"{tag} {r.binary}  ({len(r.broken_refs)} broken: {refs})",
                      flush=True)
        else:  # skipped
            tag = paint(" skip ", "33")
            print(f"{tag} {r.binary}  ({r.note})", flush=True)

    n_total = n_ok + n_borked + n_skip
    print()
    print(f"summary: {n_ok} ok, {n_borked} borked, {n_skip} skipped"
          f"  (of {n_total} examined)")
    return n_borked


def render_report(binary: str, refs: list[LibRef], ops: list[FixOp]) -> str:
    """Diagnosis + plan tail. Per-ref table is emitted inline by diagnose()."""
    lines: list[str] = [""]  # blank line separating table from diagnosis
    if not ops:
        broken = [r for r in refs if r.is_broken]
        if not broken:
            lines.append("diagnosis: clean bill of health, nothing to fix")
        else:
            lines.append("diagnosis: borked, but I couldn't find any donor organs")
    else:
        lines.append(_c(random.choice(PLAN_HEADERS), _C_PLAN))
        for op in ops:
            tool = _c("install_name_tool", _C_PLAN)
            tail = f" -{op.op} {' '.join(op.args)} "
            lines.append(f"  {tool}{tail}{_c('<bin>', _C_PLAN_BIN)}")
    return "\n".join(lines)


def suggest_alternatives(binary: str) -> list[str]:
    tips: list[str] = []
    pkg = brew_package_for(binary)
    if pkg:
        tips.append(f"  - try `brew reinstall {pkg}` first (often fixes the root cause)")
    elif under_homebrew(binary):
        tips.append("  - this lives under a Homebrew prefix; `brew reinstall <pkg>` is usually the right fix")
    tips.append('  - or set DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib:/usr/local/lib" to '
                "resolve missing dylibs at runtime without modifying the binary")
    return tips


def main(argv: list[str] | None = None) -> int:
    # Python 3.14+ colorizes argparse help/usage on a TTY by default. We
    # gate it on the same `-c` opt-in our diagnostic output uses — peek
    # at argv before parsing since argparse handles -h before our main
    # body ever runs.
    parser_kwargs: dict = {
        "description": "diagnose & repair borked dylib refs in a macOS binary",
    }
    if sys.version_info >= (3, 14):
        peek = argv if argv is not None else sys.argv[1:]
        parser_kwargs["color"] = ("-c" in peek) or ("--color" in peek)
    ap = argparse.ArgumentParser(**parser_kwargs)
    ap.add_argument("binary", nargs="+", metavar="binary-file",
                    help="path(s) to executable(s) or dylib(s); shells expand globs like /usr/local/bin/*")
    ap.add_argument("-t", "--test", action="store_true",
                    help="triage mode: scan 1+ binaries and report borked-ness only (no fix)")
    ap.add_argument("-b", "--borked", action="store_true",
                    help="with --test: only print rows for borked binaries (summary still shown)")
    ap.add_argument("-w", "--write", action="store_true",
                    help="apply repairs (default: dry run); only valid for one binary at a time")
    ap.add_argument("-f", "--force", action="store_true",
                    help="with -w: override read-only by `chmod u+w` (if owner) or "
                         "re-exec under sudo (if not). chmod is reverted after surgery.")
    ap.add_argument("-m", "--minimal-out", action="store_true",
                    help="quiet mode: with -t, only borked-or-not (no broken-ref list); "
                         "without -t, silent run that prints just one final status line")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    ap.add_argument("-c", "--color", action="store_true",
                    help="colorize output (default: off; auto-suppressed if stdout is not a TTY)")
    ap.add_argument("-s", "--skip-suggestions", action="store_true",
                    help="skip the easier-fix suggestions")
    ap.add_argument("-n", "--no-backup", action="store_true",
                    help="with -w: skip the .bak file. faster, no take-backsies on failure")
    ap.add_argument("-d", "--deep", action="store_true",
                    help="after surface diagnose, walk the dylib graph and report "
                         "cascading borks in transitively-loaded libs")
    ap.add_argument("-e", "--ecosystem-only", action="store_true",
                    help="reject donors from a different ecosystem than the patient "
                         "(brew vs conda vs macports). avoids ABI roulette.")
    args = ap.parse_args(argv)

    _set_color(args.color and sys.stdout.isatty())
    _set_eco_only(args.ecosystem_only)

    # Bail before anything else if otool / install_name_tool / codesign
    # aren't on PATH — every code path below assumes they exist.
    _preflight_tools()

    if args.minimal_out:
        # silence the chatty doctor lines from apply_ops
        log_level = logging.WARNING
    else:
        log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s %(message)s")

    # --test: triage mode for many binaries, return non-zero if any are borked.
    if args.test:
        if args.write:
            ap.error("--test and --write are mutually exclusive")
        color = args.color and sys.stdout.isatty()
        n_borked = render_scan(scan(args.binary), color=color,
                               borked_only=args.borked,
                               minimal=args.minimal_out)
        return 1 if n_borked else 0

    if args.borked:
        ap.error("--borked only meaningful with --test")

    if args.force and not args.write:
        ap.error("--force only meaningful with --write")

    if args.no_backup and not args.write:
        ap.error("--no-backup only meaningful with --write")

    if len(args.binary) > 1:
        ap.error("multiple binaries only supported with --test; "
                 "drop -w / pass one path for full diagnose")

    binary = os.path.realpath(args.binary[0])
    if not os.path.isfile(binary):
        raise UnborkityError(f"not a file: {args.binary[0]}")

    # Loud, immediate sign of life — the disk hunt downstream can take a while.
    if not args.minimal_out:
        print(f"{_c('[unborkity]', _C_TAG)} admitting patient: "
              f"{_c(binary, _C_PATH_HEADER)}", flush=True)
        if not os.access(binary, os.W_OK):
            try:
                owner_uid = os.stat(binary).st_uid
            except OSError:
                owner_uid = -1
            is_owner = owner_uid == os.geteuid()
            if args.force and args.write:
                hint = ("-f set: will `chmod u+w` then revert"
                        if is_owner else
                        "-f set: will re-exec under sudo")
            elif is_owner:
                hint = (f"either do `chmod u+w {binary}` or use the -f "
                        "flag to force yourself on the patient")
            else:
                hint = ("different owner; either use `sudo` or pass the -f "
                        "flag to force yourself on the patient")
            print(f"  warning: no write permission on {binary} -- {hint}", flush=True)

    refs = diagnose(binary, progress=not args.minimal_out)
    ops = plan_fixes(binary, refs)

    if not args.minimal_out:
        print(render_report(binary, refs, ops))
        if args.deep:
            walk = diagnose_deep(binary)
            print(render_deep(walk, root=binary))
        if not args.skip_suggestions:
            tips = suggest_alternatives(binary)
            if tips:
                print()
                print(_c("alternatives worth trying first:", _C_ALT))
                for t in tips:
                    print(_c(t, _C_ALT))

    if not ops:
        if args.minimal_out:
            print(f"ok: {binary}")
        return 0

    if not args.write:
        if args.minimal_out:
            print(f"borked (dry run): {binary}")
        else:
            print('\n(dry run — re-run with -w to apply)')
        return 0

    restore_mode: int | None = None
    if not os.access(binary, os.W_OK):
        if not args.force:
            raise UnborkityError(
                f"no write permission on {binary} — pass -f to force, or use sudo"
            )
        try:
            st = os.stat(binary)
        except OSError as e:
            raise UnborkityError(f"stat failed on {binary}: {e}") from e
        if st.st_uid == os.geteuid():
            log.info("forcing write bit on %s (chmod u+w)", binary)
            try:
                os.chmod(binary, st.st_mode | 0o200)
            except OSError as e:
                raise UnborkityError(f"chmod failed: {e}") from e
            restore_mode = st.st_mode
        else:
            if os.environ.get("UNBORKITY_SUDO_REEXEC") == "1":
                raise UnborkityError(
                    f"sudo re-exec did not yield write access on {binary}"
                )
            sudo = shutil.which("sudo")
            if not sudo:
                raise UnborkityError(f"sudo not on PATH and we don't own {binary}")
            log.info("not the owner of %s — re-executing under sudo", binary)
            new_env = {**os.environ, "UNBORKITY_SUDO_REEXEC": "1"}
            os.execvpe(sudo, [sudo, sys.executable, sys.argv[0], *sys.argv[1:]],
                       new_env)

    try:
        apply_ops(binary, ops, make_backup=not args.no_backup)
    finally:
        if restore_mode is not None:
            try:
                os.chmod(binary, restore_mode)
                log.info("restored original mode on %s (0o%o)",
                         binary, restore_mode & 0o7777)
            except OSError as e:
                log.warning("could not restore mode on %s: %s", binary, e)

    if not args.minimal_out:
        print("\npost-op checkup...")
    refs2 = diagnose(binary)
    still_broken = [r for r in refs2 if r.is_broken]
    if still_broken:
        if args.minimal_out:
            print(f"still borked: {binary}")
        msg = (f"{len(still_broken)} refs still on the operating table: "
               + ", ".join(r.raw for r in still_broken))
        raise UnborkityError(msg)
    if args.minimal_out:
        print(f"repaired: {binary}")
    else:
        print("patient discharged. all refs resolve.")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except UnborkityError as e:
        _print_disaster(f"ERROR: {e}")
        rc = 1
    except KeyboardInterrupt:
        sys.stdout.write("\ninterrupted\n")
        rc = 130
    except Exception as e:
        _print_disaster(f"ERROR: unhandled {type(e).__name__}: {e}")
        rc = 1
    raise SystemExit(rc)
