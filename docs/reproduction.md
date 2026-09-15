# Local alpha reproduction

`dist/lumen-alpha-reproduction.zip` contains the wheel, current specification,
source, tests, manifests, documentation, evaluation adapters and pinned build
wheel. It excludes model clients, private workspace data and benchmark conversation
bodies. `release-manifest.json` records exact file hashes and incomplete acceptance.

Extract into a new directory. With Python 3.12+ and Git available:

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install --no-index --no-deps dist/lumen_memory-0.1.0a1-py3-none-any.whl
python tools/verify-install.py
```

The verifier creates another clean environment, checks that imports resolve to the
installed wheel, checks packaged license/documentation, blocks Python IP networking,
and runs the manifest gate. Its current expected exit is **1**: local tests pass,
but mandatory release acceptance remains incomplete. Inspect
`artifacts/local/clean-install.json` and `clean-install-release-1.json` for individual
commands, exit codes, identities and pending requirements. Never interpret exit 1
as product acceptance.

Build without network using the bundled build wheel:

```powershell
python -m pip --isolated wheel --no-index --find-links artifacts/local/wheels --no-deps --wheel-dir dist .
python tools/verify-install.py
python tools/package-release.py
```

The final command requires successful component verification of the exact wheel
and unchanged acceptance inputs: specification, package metadata, runtime, tests,
tools and evaluation configuration. Additions and removals also invalidate the
evidence. Older reports lacking this snapshot require re-verification. Packaging
checks remain active with Python optimization enabled. The kit is marked alpha
and is not published.
For benchmark reproduction, see `validation/reference-2026-09-11.md`; the public
dataset download is separate and requires network. No inference is enabled.
