"""Configuration is local data, never part of the source repository."""
from __future__ import annotations
import json
import os
import re
import tempfile
from pathlib import Path


def default_state_dir() -> Path:
    return Path.home() / 'Library/Application Support/Tuck'


def private_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def checked_path(value: str) -> Path:
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError('Paths must be nonempty strings.')
    path = Path(value).expanduser()
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Use absolute paths or paths starting with ~/.')
    if any(parent.is_symlink() for parent in [path, *path.parents]):
        raise ValueError('Symbolic links are not supported as filing destinations.')
    return path


def load_config(path: Path) -> dict:
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or data.get('version', 1) != 1:
        raise ValueError('Unsupported configuration version.')
    watch = checked_path(data.get('watch_dir', '~/Downloads'))
    if not watch.is_dir():
        raise ValueError('The watched folder must exist.')
    folders = data.get('folders')
    if not isinstance(folders, list) or not 1 <= len(folders) <= 200:
        raise ValueError('Configure between 1 and 200 destination folders.')
    ids = set()
    paths = set()
    labels = set()
    for folder in folders:
        if not isinstance(folder, dict):
            raise ValueError('Each destination must be an object.')
        key = folder.get('id', '')
        if not isinstance(key, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', key) or key == 'leave' or key in ids:
            raise ValueError('Destination IDs must be unique simple identifiers.')
        dest = checked_path(folder.get('path', ''))
        if dest == watch or dest in paths or not dest.is_dir():
            raise ValueError('Destinations must be unique folders other than the watched folder.')
        label = folder.get('label', key)
        description = folder.get('description', label)
        if not isinstance(label, str) or not 1 <= len(label) <= 200 or not isinstance(description, str) or len(description) > 600:
            raise ValueError('Destination descriptions must be short text.')
        if label.casefold() in labels:
            raise ValueError("Destination labels must be unique. Give each folder a distinct label.")
        labels.add(label.casefold())
        folder.update(path=str(dest), label=label, description=description)
        ids.add(key)
        paths.add(dest)
    rules = data.get('rules', [])
    if not isinstance(rules, list) or len(rules) > 200:
        raise ValueError('Rules must be a list of at most 200 entries.')
    for rule in rules:
        if not isinstance(rule, dict) or rule.get('folder_id') not in ids:
            raise ValueError('Every rule must reference an allowed destination.')
        words = rule.get('keywords', [])
        if not isinstance(words, list) or not words or any(not isinstance(w, str) or not w.strip() or len(w) > 100 for w in words):
            raise ValueError('Rules need nonempty keyword lists.')
    model = data.get('model', {})
    if not isinstance(model, dict) or model.get('engine', 'tinyjev') != 'tinyjev':
        raise ValueError('The supported model engine is tinyjev.')
    if not isinstance(model.get('enabled', True), bool):
        raise ValueError('model.enabled must be true or false.')
    if model.get('name', 'TinyJev-0.6B') != 'TinyJev-0.6B':
        raise ValueError('This release supports TinyJev-0.6B.')
    timeout = model.get('timeout_ms', 850)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 50 <= timeout <= 1500:
        raise ValueError('model.timeout_ms must be between 50 and 1500.')
    for key, default, low, high in [('poll_interval_ms',150,50,1000),('settle_ms',400,200,5000),('suggestion_deadline_ms',1500,200,10000)]:
        value = data.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int,float)) or not low <= value <= high:
            raise ValueError(f'{key} is outside the supported range.')
        data[key] = value
    instructions = data.get('instructions', 'Suggest the most relevant available folder for human review.')
    if not isinstance(instructions, str) or len(instructions) > 2000:
        raise ValueError('Instructions must be at most 2000 characters.')
    data.update(watch_dir=str(watch), folders=folders, rules=rules, model={**model,'enabled':model.get('enabled',True),'name':'TinyJev-0.6B'}, instructions=instructions)
    return data


def discover_folders(root: Path, depth: int = 3) -> list[dict]:
    """Directories only: never expose filenames or traverse project internals."""
    root = checked_path(str(root))
    folders = []
    excluded = {'Codex', 'node_modules', 'vendor', 'build', 'dist', 'venv', 'env', 'Library', '__pycache__'}
    def visit(parent: Path, level: int) -> None:
        if level > depth or len(folders) >= 160:
            return
        for child in sorted(parent.iterdir(), key=lambda p:p.name.casefold()):
            if child.name.startswith('.') or child.name in excluded or child.is_symlink() or not child.is_dir():
                continue
            relative = str(child.relative_to(root))
            key = re.sub(r'[^a-z0-9]+', '-', relative.casefold()).strip('-')[:65] or 'folder'
            key = f'{key}-{len(folders)+1}'
            folders.append({'id':key,'label':relative.replace('/', ' / '),'path':str(child),'description':relative.replace('/', ' — ')})
            if not (child/'.git').exists() and len(folders)<160:
                visit(child,level+1)
            if len(folders)>=160:
                break
    visit(root,1)
    return folders or [{'id':'documents','label':'Documents','path':str(root),'description':'Documents to keep'}]
