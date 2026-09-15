"""Real on-disk permitted source fixtures, without network or model services."""
import hashlib
import json
from pathlib import Path
from lumen.workspace import Workspace


def source_workspace(root, workspace_id='w'):
    root = Path(root)
    (root / 'p').mkdir(parents=True)
    for name in ('contract.txt', 'a.txt', 'b.txt'):
        (root / 'p' / name).write_bytes(b'fixture')
    manifest = Workspace.initialize(root, [{'id':'p', 'root':'p', 'kind':'project',
        'purpose':'fixture', 'owners':['owner'], 'depends_on':[]}])
    manifest['id'] = workspace_id
    (root / '.lumen/workspace.json').write_text(json.dumps(manifest))
    return Workspace(root, root.parent / 'source-home')


def citation(name='contract.txt'):
    return {'kind':'file', 'project':'p', 'path':name, 'revision':'fixture',
            'sha256':hashlib.sha256(b'fixture').hexdigest(), 'anchor':'fixture'}
