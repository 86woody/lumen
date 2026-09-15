"""Hash the executable acceptance inputs, excluding generated outputs and secrets."""
import hashlib
import os
from pathlib import Path


def snapshot(root):
    root = Path(root).resolve()
    paths = [root / name for name in ('.agents/plans/Lumen.md', 'pyproject.toml', 'README.md', 'LICENSE')]
    excluded = {'archive', '.git', '__pycache__', '.env', 'credentials'}
    for directory in ('src', 'tests', 'tools', 'evals', 'acceptance', 'native'):
        for parent, directories, filenames in os.walk(root / directory, followlinks=False):
            directories[:] = sorted(d for d in directories if d.casefold() not in excluded)
            for directory_name in directories:
                directory_path = Path(parent) / directory_name
                if directory_path.is_symlink() or getattr(directory_path, 'is_junction', lambda: False)():
                    raise ValueError('Linked acceptance directories are not supported')
            for name in sorted(filenames):
                if Path(name).suffix in {'.py', '.ps1', '.json', '.toml', '.go'} and not name.startswith('.env'):
                    paths.append(Path(parent) / name)
    result = {}
    for path in sorted(paths):
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or any(p.casefold() in excluded for p in resolved.relative_to(root).parts):
            raise ValueError('Acceptance input escapes permitted tree')
        result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result
