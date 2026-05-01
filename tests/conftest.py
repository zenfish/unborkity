"""Shared fixtures: build the test binary once, hand each test a fresh copy."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"
BUILD_DIR = FIXTURE_DIR / "build"

# make the project root importable so `import unborkity` works
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(scope="session")
def fixture_build() -> Path:
    """Run `make` once per session; return the build dir."""
    proc = subprocess.run(
        ["make", "-C", str(FIXTURE_DIR)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        pytest.fail(f"fixture build failed:\n{proc.stdout}\n{proc.stderr}")
    return BUILD_DIR


@pytest.fixture
def broken_bin(tmp_path: Path, fixture_build: Path) -> Path:
    """Fresh copy of the broken binary in a tmp dir — tests can mutate freely."""
    src = fixture_build / "bin" / "mygreet"
    dst = tmp_path / "mygreet"
    shutil.copy2(src, dst)
    # codesign the copy too (otherwise macOS may reject it)
    subprocess.run(["codesign", "-f", "-s", "-", str(dst)], check=True, capture_output=True)
    return dst


@pytest.fixture
def stash_dir(fixture_build: Path) -> Path:
    """Directory holding a working copy of libgreet.dylib (used as an rpath candidate)."""
    return fixture_build / "stash"
