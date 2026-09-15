"""Prepare a local alpha reproduction kit; never publishes or infers acceptance."""
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile
from verification_inputs import snapshot


def check(condition, message):
    if not condition:
        raise SystemExit(message)

root = Path(__file__).resolve().parents[1]
wheel = root / 'dist/lumen_memory-0.1.0a1-py3-none-any.whl'
installation = json.loads((root / 'artifacts/local/clean-install.json').read_text())
if not installation.get('verification_inputs_unchanged') or installation.get('verification_inputs') != snapshot(root):
    raise SystemExit('Acceptance inputs missing or changed; run clean-install verification again')
gate = json.loads((root / 'artifacts/local/clean-install-release-1.json').read_text())
sha = lambda data: hashlib.sha256(data).hexdigest()
for filename, key in (('release-1.json', 'manifest_digest'), ('clients.lock.json', 'clients_digest')):
    data = json.loads((root / 'evals' / filename).read_text())
    encoded = json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    check(sha(encoded) == gate[key], 'Release inputs changed after verification: ' + filename)
check(installation['sha256'] == sha(wheel.read_bytes()), 'Installation evidence is for another wheel')
check(len(installation['commands']) == 3 and all(c['exit_code'] == 0 for c in installation['commands'][:2]), 'Installation checks failed')
check(gate['local']['passed'] and gate['runtime_unchanged'], 'Component verification failed')
for name, expected in gate['identity']['files'].items():
    check(sha((root / 'src/lumen' / name).read_bytes()) == expected, 'Runtime changed after verification')
files = {}
files['.agents/plans/Lumen.md'] = (root / '.agents/plans/Lumen.md').read_bytes()
for name in ('README.md', 'LICENSE', 'pyproject.toml', 'CHECKPOINT.md', '.gitattributes', '.gitignore'):
    files[name] = (root / name).read_bytes()
for directory in ('src', 'tests', 'tools', 'evals', 'docs', 'acceptance'):
    for path in sorted((root / directory).rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts and (path.suffix in {'.py', '.json', '.jsonl', '.md', '.toml', '.ps1', '.go'}
                or (path.is_relative_to(root / 'docs/validation') and path.suffix == '.stderr')):
            files[path.relative_to(root).as_posix()] = path.read_bytes()
files['dist/' + wheel.name] = wheel.read_bytes()
for path in sorted((root / 'artifacts/local/wheels').glob('*.whl')):
    files[path.relative_to(root).as_posix()] = path.read_bytes()
tested_wheel = root / 'artifacts/local/client-tested-wheel' / wheel.name
if tested_wheel.exists():
    files[tested_wheel.relative_to(root).as_posix()] = tested_wheel.read_bytes()
for name in ('clean-install.json', 'clean-install-release-1.json'):
    files['artifacts/local/' + name] = (root / 'artifacts/local' / name).read_bytes()
manifest = {'schema': 1, 'status': 'alpha; mandatory release acceptance incomplete',
    'publication': False, 'release_1_passed': gate['passed'], 'release_2_passed': False,
    'code_revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
    'runtime_identity': gate['identity'], 'additional_cost_usd': 0,
    'files': {name: {'sha256': sha(data), 'bytes': len(data)} for name, data in sorted(files.items())}}
manifest_bytes = (json.dumps(manifest, indent=2) + '\n').encode()
target = root / 'dist/lumen-alpha-reproduction.zip'
with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
    for name, data in sorted(files.items()):
        archive.writestr(name, data)
    archive.writestr('release-manifest.json', manifest_bytes)
with zipfile.ZipFile(target) as archive:
    check(archive.testzip() is None, 'Invalid reproduction ZIP')
    for name, metadata in manifest['files'].items():
        check(sha(archive.read(name)) == metadata['sha256'], 'Packaged file hash mismatch: ' + name)
(root / 'dist/release-manifest.json').write_bytes(manifest_bytes)
print(json.dumps({'path': str(target), 'sha256': sha(target.read_bytes()), 'files': len(files),
                  'release_complete': False}))
