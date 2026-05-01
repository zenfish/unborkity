# unborkity
Diagnose and repair macOS binaries that fail with the classic -
```
dyld: Library not loaded: libfoo.dylib
  Referenced from: /usr/local/bin/some_program
  Reason: image not found
Abort trap: 6
```

I've had an old unpublished thing for years, thought I'd let claude-code beat it into shape for release. FWIW!

When you get the above error (e.g. the executable is *borked*), you can use otool to figure out the issue, then try to
remember the syntax on how to fix it. This automates all that.

## Usage

```sh
# diagnose (dry run)
unborkity.py /usr/local/bin/hackrf_info

# apply repairs
unborkity.py -w /usr/local/bin/hackrf_info

# check homebrew bin en-masse
unborkity.py -t /opt/homebrew/bin/*

```

The tool

- parses `otool -L` and `otool -l` to find every dylib reference and existing `LC_RPATH` entries;
- classifies each reference as `absolute`, `rpath`, `loader`, `executable`, `relative`, or `system`;
- distinguishes between *what dyld would currently load* (the `resolved` field) and *what we found on disk that could fix it* (the `candidate` field);
- prefers a single `install_name_tool -add_rpath` for batches of broken `@rpath/...` refs, and falls back to `-change` per reference for absolute / relative refs;
- backs the binary up to `/tmp/<name>.unborkity.bak` before any write;
- re-signs ad-hoc on Apple Silicon (`codesign -f -s -`) so the kernel will still load the binary after modification — the original tool didn't, which silently breaks every fix on arm64.

## Requirements

unborkity is **pure Python stdlib** — no `pip install` for normal use. It does
shell out to a few macOS CLI tools that must be on your `PATH`:

| Tool                | Provided by                  | Used for                                  |
| ------------------- | ---------------------------- | ----------------------------------------- |
| `otool`             | Xcode Command Line Tools     | reading dylib refs and `LC_RPATH` entries |
| `install_name_tool` | Xcode Command Line Tools     | rewriting refs / adding rpaths            |
| `codesign`          | Xcode Command Line Tools     | re-signing ad-hoc on Apple Silicon        |
| `mdfind`            | macOS (built-in, optional)   | fast donor-library search via Spotlight   |

If any of the three required tools are missing, unborkity refuses to start
and prints the install hint:

```sh
xcode-select --install
```

`mdfind` is optional — when absent the search falls back to walking
`/opt/homebrew` and `/usr/local`.

**Python**: any `python3` ≥ 3.9. No third-party packages.

**Tests**: `pip install pytest`. The C fixture build uses `make` + `clang`
(part of Xcode CLT). Run from the repo root:

```sh
pytest tests/
```

The test suite invokes `unborkity.py` via `sys.executable`, so it works on
whichever interpreter you launch pytest with (3.9 through 3.14 currently).

## Try the easy fixes first

Before reaching for binary surgery, try these — they fix the cause, not the symptom.

1. **Reinstall the package.** If the binary lives under a Homebrew prefix
   (`/opt/homebrew/...` or `/usr/local/...`), the broken reference usually
   means a dependency was upgraded out from under it.

   ```sh
   brew reinstall hackrf      # whatever package owns the bin
   brew doctor                # surfaces other broken links
   ```

2. **Use `DYLD_FALLBACK_LIBRARY_PATH`.** Tell the dynamic loader where to
   look at runtime — no binary modification at all.

   ```sh
   export DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib:/usr/local/lib"
   ```

   Caveat: macOS strips `DYLD_*` vars when launching anything in `/usr/bin`,
   `/bin`, or any SIP-protected location, but it works for Homebrew bins.

3. **Add an rpath.** A single command, instead of rewriting every reference:

   ```sh
   install_name_tool -add_rpath /opt/homebrew/lib /usr/local/bin/hackrf_info
   codesign -f -s - /usr/local/bin/hackrf_info     # required on Apple Silicon
   ```

If none of those apply, run `unborkity`.

## Test fixture

`tests/fixtures/` builds a minimal C library and binary that reproduces the
exact `dyld: Library not loaded` failure mode:

```sh
cd tests/fixtures && make
./build/bin/mygreet               # fails: "no LC_RPATH's found"
```

The dylib has install name `@rpath/libgreet.dylib`, the binary is linked against
it without any `LC_RPATH` entries, mirroring the typical Homebrew bork.

```sh
pytest tests/                     # runs the full suite
```
