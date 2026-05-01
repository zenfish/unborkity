"""
County General ER — unborkity test suite.

Each test admits a patient, performs a procedure, and signs the chart.
If the patient codes, the malpractice attorneys (pytest assertions) are
already in the parking lot.

Triage tag legend:
    is_broken=True   -> patient is borked, needs surgery
    is_broken=False  -> patient is healthy, please discharge
    status=skipped   -> not a person, do not admit (it was a sandwich)
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

import unborkity


# ---------------------------------------------------------------------------
# pure-function unit tests
# (the medical school exam — does the intern know which organ is which?)

def test_classify():
    """The intern names every dylib reference. Get any wrong, repeat the year."""
    cases = {
        "@rpath/libfoo.dylib":               "rpath",
        "@loader_path/libfoo.dylib":         "loader",
        "@executable_path/libfoo.dylib":     "executable",
        "/usr/lib/libSystem.B.dylib":        "system",   # the heart, you cannot cut into it
        "/System/Library/x":                 "system",
        "/opt/homebrew/lib/libfftw3.dylib":  "absolute",
        "libhackrf.0.dylib":                 "relative", # naked in the hallway, no path on
    }
    for raw, expected in cases.items():
        got = unborkity.classify(raw)
        assert got == expected, f"intern called {raw!r} a {got!r}, should be {expected!r}"


def test_otool_parse(broken_bin: Path):
    """Patient X-ray. We expect to see one cracked rib (@rpath) and one libSystem."""
    refs = unborkity.run_otool(str(broken_bin))
    assert "@rpath/libgreet.dylib" in refs, "the obviously-borked rib didn't show up on the X-ray"
    assert any(r.startswith("/usr/lib/") for r in refs), "patient appears to have no libSystem; are they human?"


def test_get_rpaths_empty(broken_bin: Path):
    """Fixture is built without LC_RPATH on purpose — patient has no spinal column."""
    assert unborkity.get_rpaths(str(broken_bin)) == [], \
        "the patient grew an LC_RPATH overnight; this is medically impossible"


# ---------------------------------------------------------------------------
# diagnose — the attending makes rounds

def test_diagnose_marks_rpath_broken(broken_bin: Path):
    """One rpath patient is in distress. libSystem is a janitor, leave them alone."""
    refs = unborkity.diagnose(str(broken_bin))
    by_kind = {r.kind: r for r in refs}
    assert "rpath" in by_kind, "where did the rpath patient go?"
    assert by_kind["rpath"].is_broken or by_kind["rpath"].resolved is not None
    # libSystem is a system lib — legally we cannot operate on it.
    sys_refs = [r for r in refs if r.kind == "system"]
    assert sys_refs, "no system libs at all? did we admit a vending machine?"
    assert not any(r.is_broken for r in sys_refs), \
        "you do NOT diagnose libSystem as broken; that is how lawsuits start"


# ---------------------------------------------------------------------------
# planning — the surgical team draws on the patient with a Sharpie

def _force_walk_only(monkeypatch):
    """Disable mdfind. Spotlight is unreliable; we walk the wards by hand."""
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if cmd and cmd[0] == "mdfind":
            class R:
                stdout = ""  # mdfind insists it found nothing
            return R()
        return real_run(cmd, *a, **kw)
    monkeypatch.setattr(unborkity.subprocess, "run", fake_run)


def test_plan_proposes_add_rpath(broken_bin: Path, stash_dir: Path, monkeypatch):
    """One donor in the stash, one patient on the table. Match made."""
    monkeypatch.setattr(unborkity, "SEARCH_PATHS", [str(stash_dir)])
    _force_walk_only(monkeypatch)

    refs = unborkity.diagnose(str(broken_bin))
    ops = unborkity.plan_fixes(str(broken_bin), refs)
    add_rpath_ops = [o for o in ops if o.op == "add_rpath"]
    assert len(add_rpath_ops) == 1, \
        f"expected exactly one rpath graft, surgeon scheduled {len(add_rpath_ops)}"
    assert add_rpath_ops[0].args == (str(stash_dir),), \
        "scrub nurse handed the surgeon the wrong rpath"


# ---------------------------------------------------------------------------
# end-to-end repair — the actual surgery
# (success criterion: the patient says hello after waking up)

def test_repair_makes_binary_runnable(broken_bin: Path, stash_dir: Path, monkeypatch, tmp_path):
    # admission interview: confirm the patient is, in fact, dying.
    pre = subprocess.run([str(broken_bin)], capture_output=True, text=True)
    assert pre.returncode != 0, "patient walked in on their own two feet; admit refused"
    assert "Library not loaded" in pre.stderr or "image not found" in pre.stderr.lower() \
        or "no LC_RPATH" in pre.stderr, \
        f"patient is unconscious for some other reason: {pre.stderr!r}"

    # wheel in the donor cart
    monkeypatch.setattr(unborkity, "SEARCH_PATHS", [str(stash_dir)])
    _force_walk_only(monkeypatch)

    refs = unborkity.diagnose(str(broken_bin))
    ops = unborkity.plan_fixes(str(broken_bin), refs)
    assert ops, "surgeon stood over the patient and announced 'looks fine to me'"

    unborkity.apply_ops(str(broken_bin), ops, backup_dir=str(tmp_path))

    # cowardly insurance copy in the morgue, just in case
    assert (tmp_path / (broken_bin.name + ".unborkity.bak")).is_file(), \
        "backup is missing; if this surgery fails the lawyers will eat well"

    # post-op vitals
    post = subprocess.run([str(broken_bin), "unborkity"], capture_output=True, text=True)
    assert post.returncode == 0, f"patient flatlined after operation: {post.stderr!r}"
    assert "hello, unborkity, from libgreet" in post.stdout, \
        "patient woke up but cannot speak; possible aphasia from the rpath transplant"


# ---------------------------------------------------------------------------
# triage — the waiting room scan (-t/--test mode)

def test_scan_classifies_broken_ok_skipped(broken_bin: Path, tmp_path: Path):
    """Four arrivals at the ER: a patient, a jogger, a sandwich, and a no-show."""
    sandwich = tmp_path / "notes.txt"
    sandwich.write_text("turkey on rye, no mayo")  # not a binary, nurse, please

    results = list(unborkity.scan([
        str(broken_bin),    # the patient — definitely borked
        "/bin/ls",          # the jogger — annoyingly healthy, came for a checkup
        str(sandwich),      # the sandwich — should not be in triage
        "/no/such/path",    # the no-show — never showed up to their appointment
    ]))
    by_status = {r.binary: r.status for r in results}

    assert by_status[str(broken_bin)] == "borked", "we missed the patient with the broken rib"
    assert by_status["/bin/ls"] == "ok", "we accidentally diagnosed the jogger; refund their copay"
    assert by_status[str(sandwich)] == "skipped", "the sandwich is on a gurney, get the sandwich off the gurney"
    assert by_status["/no/such/path"] == "skipped", "we admitted a ghost"

    borked = [r for r in results if r.status == "borked"][0]
    assert any("@rpath/libgreet.dylib" in ref for ref in borked.broken_refs), \
        "we know the patient is borked but we cannot locate the wound"


def test_cli_test_flag_exits_nonzero_on_bork(broken_bin: Path, tmp_path: Path):
    """`-t` flag is the head nurse: if anyone in the room is dying, sound the alarm."""
    script = Path(__file__).parent.parent / "unborkity.py"

    # mixed waiting room
    proc = subprocess.run(
        [sys.executable, str(script), "-t", str(broken_bin), "/bin/ls"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 1, \
        f"head nurse failed to sound the alarm despite a bleeding patient:\n{proc.stdout}"
    assert "BORKED" in proc.stdout, "no BORKED tag — did the patient walk out?"
    assert "  ok  " in proc.stdout, "the jogger went uncharted"

    # all clear in the waiting room
    proc = subprocess.run(
        [sys.executable, str(script), "-t", "/bin/ls", "/bin/cat"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"head nurse panicked over two perfectly fine joggers:\n{proc.stdout}"
    assert "BORKED" not in proc.stdout, "head nurse hallucinated a borked patient on a slow night"


def test_cli_borked_filter_hides_healthy(broken_bin: Path):
    """`-b` is the head nurse with selective hearing: only the screamers get charted."""
    script = Path(__file__).parent.parent / "unborkity.py"
    proc = subprocess.run(
        [sys.executable, str(script), "-t", "-b",
         str(broken_bin), "/bin/ls", "/bin/cat"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 1, "alarm did not sound despite a bleeding patient on the list"
    assert "BORKED" in proc.stdout, "borked row vanished — nurse charted nobody"
    assert "/bin/ls" not in proc.stdout, "healthy jogger leaked into the borked-only chart"
    assert "/bin/cat" not in proc.stdout, "second jogger also leaked"
    assert "summary:" in proc.stdout, "summary line dropped; counts of healthy patients lost"


def test_cli_borked_requires_test(broken_bin: Path):
    """`-b` without `-t` is a nurse demanding triage results before triage. Reject."""
    script = Path(__file__).parent.parent / "unborkity.py"
    proc = subprocess.run(
        [sys.executable, str(script), "-b", str(broken_bin)],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0, "argparse let -b through without -t"
    assert "only meaningful with --test" in proc.stderr


def test_cli_test_and_write_mutually_exclusive(broken_bin: Path):
    """You cannot triage and operate in the same gesture. Pick a lane."""
    script = Path(__file__).parent.parent / "unborkity.py"
    proc = subprocess.run(
        [sys.executable, str(script), "-t", "-w", str(broken_bin)],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0, "argparse let the surgeon-triage-nurse hybrid into the building"
    assert "mutually exclusive" in proc.stderr


def test_cli_force_requires_write(broken_bin: Path):
    """`-f` without `-w` is consent paperwork without surgery scheduled. Reject."""
    script = Path(__file__).parent.parent / "unborkity.py"
    proc = subprocess.run(
        [sys.executable, str(script), "-f", str(broken_bin)],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0, "argparse let -f through without -w"
    assert "only meaningful with --write" in proc.stderr


def test_force_chmods_and_reverts(broken_bin: Path, stash_dir: Path,
                                  monkeypatch, tmp_path):
    """`-f` on owner-but-read-only file: chmod u+w, operate, revert mode."""
    monkeypatch.setattr(unborkity, "SEARCH_PATHS", [str(stash_dir)])

    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if cmd and cmd[0] == "mdfind":
            class R:
                stdout = ""
            return R()
        return real_run(cmd, *a, **kw)
    monkeypatch.setattr(unborkity.subprocess, "run", fake_run)

    # strip write bit; we own it
    original_mode = broken_bin.stat().st_mode
    os.chmod(broken_bin, original_mode & ~0o222)
    assert not os.access(broken_bin, os.W_OK), "test fixture refused to go read-only"

    rc = unborkity.main([
        "-w", "-f", "--skip-suggestions", str(broken_bin),
    ])
    assert rc == 0, f"force-write surgery failed (rc={rc})"

    # mode restored
    assert broken_bin.stat().st_mode == original_mode & ~0o222, \
        "force mode forgot to revert chmod after surgery"
    # patient walks
    post = subprocess.run([str(broken_bin), "force"], capture_output=True, text=True)
    assert post.returncode == 0, f"patient flatlined after forced surgery: {post.stderr!r}"


def test_preflight_missing_tool_raises(monkeypatch):
    """No otool on PATH = clinic locked. Tell user how to install Xcode CLT."""
    real_which = unborkity.shutil.which

    def fake_which(name, *a, **kw):
        if name == "otool":
            return None
        return real_which(name, *a, **kw)
    monkeypatch.setattr(unborkity.shutil, "which", fake_which)

    with pytest.raises(unborkity.UnborkityError) as ei:
        unborkity._preflight_tools()
    msg = str(ei.value)
    assert "otool" in msg, "preflight didn't name the missing tool"
    assert "xcode-select --install" in msg, \
        "preflight didn't hand the user the install hint"


def test_preflight_all_present_silent():
    """All three tools present = preflight is a no-op. (Real env on dev box.)"""
    unborkity._preflight_tools()  # must not raise


def test_cli_dry_run_makes_no_changes(broken_bin: Path):
    """Bedside manner test: look at the patient, do not touch the patient."""
    before = broken_bin.read_bytes()
    script = Path(__file__).parent.parent / "unborkity.py"
    proc = subprocess.run(
        [sys.executable, str(script), str(broken_bin), "--skip-suggestions"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "dry run" in proc.stdout, "tool forgot to announce 'just looking, don't worry'"
    assert broken_bin.read_bytes() == before, \
        "the surgeon went poking around when they were told just to consult"


# ---------------------------------------------------------------------------
# new-feature unit tests

def test_ecosystem_classifier():
    """The triage nurse must tell brew from conda from macports at a glance."""
    cases = {
        "/opt/homebrew/lib/libfoo.dylib":                     "brew-arm",
        "/opt/homebrew/Cellar/grpc/1.73/lib/x.dylib":         "brew-arm",
        "/usr/local/Cellar/openssl/3.0/lib/x.dylib":          "brew-x86",
        "/usr/local/lib/libfoo.dylib":                        "brew-x86",
        "/opt/local/lib/libfoo.dylib":                        "macports",
        "/Users/zen/radioconda/lib/libabsl.dylib":            "conda",
        "/Users/x/miniconda3/lib/libfoo.dylib":               "conda",
        "/usr/lib/libSystem.B.dylib":                         "system",
        "/Applications/Foo.app/Contents/Frameworks/X.dylib":  "apps",
        "/var/tmp/whatever.dylib":                            "other",
    }
    for path, expected in cases.items():
        got = unborkity._ecosystem(path)
        assert got == expected, f"nurse called {path!r} a {got!r}, expected {expected!r}"


def test_cli_no_backup_skips_bak(broken_bin: Path, stash_dir: Path,
                                 monkeypatch):
    """`-n` skips the .bak file. Surgeon goes commando, no insurance copy."""
    monkeypatch.setattr(unborkity, "SEARCH_PATHS", [str(stash_dir)])
    _force_walk_only(monkeypatch)

    # purge any prior /tmp backup left by other tests so we can prove
    # this run did not write one.
    sys_bak = Path("/tmp") / (broken_bin.name + ".unborkity.bak")
    if sys_bak.exists():
        sys_bak.unlink()

    rc = unborkity.main([
        "-w", "-n", "--skip-suggestions", str(broken_bin),
    ])
    assert rc == 0
    assert not sys_bak.exists(), \
        "no-backup mode left a .bak in /tmp; commando promise broken"


def test_cli_no_backup_requires_write(broken_bin: Path):
    """`-n` without `-w` is a surgeon refusing the gown but no operation booked."""
    script = Path(__file__).parent.parent / "unborkity.py"
    proc = subprocess.run(
        [sys.executable, str(script), "-n", str(broken_bin)],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "only meaningful with --write" in proc.stderr


def test_diagnose_deep_smoke(broken_bin: Path):
    """`diagnose_deep` walks the dylib graph without exploding on the fixture."""
    walk = unborkity.diagnose_deep(str(broken_bin))
    paths = [p for p, _, _ in walk]
    assert str(broken_bin) in paths or os.path.realpath(str(broken_bin)) in paths, \
        "deep walk forgot to visit the patient itself"
    # the fixture has unresolved @rpath/libgreet, so cascading walk stops there;
    # whatever it reaches should not loop.
    assert len(paths) == len(set(paths)), "deep walk visited same node twice (cycle bug)"
