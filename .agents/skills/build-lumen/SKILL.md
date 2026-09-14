---
name: build-lumen
description: Build Lumen exactly as the 2026-09-14 package refresh did - inventory, offline tests, the wheel from the pinned setuptools, reinstall into .venv, fixture and poison benches, clean install from a fresh venv, reproduction kit, release manifest and byte-exact evidence copies. Use for "build lumen", "rebuild the wheel", "refresh the package evidence". Not for running the demo daemon, the client acceptance drivers or the Codex/Copilot pairs.
---

# /build-lumen

One command, run from anywhere inside the repo:

    powershell -File .claude/skills/build-lumen/build.ps1

It resolves the repo root from its own location and stops at the first step
whose exit code is not the one that step is supposed to give. Nothing here
touches the network: the build wheel comes from `artifacts/local/wheels`, tests
run under `tools/network_guard`, and pip runs `--isolated --no-index`.

## Steps, in this order (the order matters)

1. `python tools/inventory.py` rewrites `evals/dependencies.lock.json`. It must run
   before verify-install because that file is one of the acceptance inputs the
   clean-install report snapshots; changing it afterwards invalidates the report.
2. `python tools/offline-tests.py`: the unittest suite with IP networking denied, exit 0.
3. `python -m pip --isolated wheel --no-index --find-links artifacts/local/wheels
   --no-deps --wheel-dir dist .` produces `dist/lumen_memory-0.1.0a1-py3-none-any.whl`.
4. Reinstall that wheel into `.venv` (`--force-reinstall`, same version number).
5. `.venv/Scripts/lumen bench --suite fixture` and `--suite poison`, reports in
   `artifacts/local/<suite>.json`. Exit 1 by design (phase gates); the script
   requires `component_tests_passed` and `runtime_unchanged` in each report.
6. `python tools/verify-install.py`: a fresh temp venv, install, import location,
   release manifest gate. Exit 1 by design; install and import must exit 0,
   `verification_inputs_unchanged` must be true, `local.passed` must be true.
7. `python tools/package-release.py` rechecks the evidence against the exact wheel
   and the runtime hashes, then writes `dist/lumen-alpha-reproduction.zip` and
   `dist/release-manifest.json` (the only `dist/` file that is committed).
8. Copies `artifacts/local/clean-install*.json` byte-exact into `docs/validation/`
   as `alpha-install[-release-1]-<today>[-N].json` and `current-alpha-clean-install*.json`.
   `-SkipEvidenceCopy` leaves step 8 out for a throwaway build.

## Gate

The script ends with `BUILD OK` and the wheel SHA256, runtime identity, kit file
count and code revision. Falsifiable: the SHA256 it prints equals the one in
`dist/release-manifest.json`, and `git status` shows only the manifest, the
dependency lock and the evidence copies changed.

## After a build

Update the digests in the "Latest status" block of `CHECKPOINT.md` (wheel, runtime
identity, hook.py) and commit the manifest with the evidence copies on the feature
branch. Do not edit `tools/`, `evals/` or `src/` between steps 6 and 7.
Digests hash worktree bytes: a `src/lumen/*.py` file that flips between LF and
CRLF changes the runtime identity with no git diff. Check `git ls-files --eol src`
when the identity moves without a source commit.

## Not covered

`dist/lumen-hook.exe` is a retired Go launcher kept as evidence; the launcher is
now `src/lumen/hook.py` and needs no build. Client drivers, pilot runs and
`bench --suite cross-agent` are separate acceptance work, not part of the build.
