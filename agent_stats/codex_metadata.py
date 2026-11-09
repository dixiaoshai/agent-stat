"""Read display names beside a rollout source without changing Codex state."""
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path


def metadata_home(path):
    """Use the rollout's own home, never the importing machine's global index."""
    path = Path(path).resolve()
    for parent in path.parents:
        if parent.name in ('sessions', 'archived_sessions'):
            return parent.parent
    # Also support exported sources with a colocated name index.
    return path.parent


def read_session_titles(home):
    home = Path(home)
    titles, warnings = {}, []
    index = home / 'session_index.jsonl'
    if index.is_file():
        try:
            with index.open(encoding='utf-8') as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            raise ValueError()
                        identity, title = row.get('id'), row.get('thread_name')
                        if isinstance(identity, str) and isinstance(title, str) and title.strip():
                            # This is an append-only index; later entries carry renames.
                            titles[identity] = title.strip()
                    except (ValueError, TypeError):
                        warnings.append(str(index) + ': 会话名称索引含无效行，已跳过')
        except (OSError, UnicodeError):
            warnings.append(str(index) + ': 无法读取会话名称索引')

    # Codex versions its state database. Prefer the newest schema, and fall back
    # gracefully if a source predates the optional name column or is unreadable.
    databases = sorted(
        (p for p in home.glob('state_*.sqlite') if p.stem[6:].isdigit()),
        key=lambda p: int(p.stem[6:]), reverse=True,
    )
    for path in databases:
        try:
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
                columns = {row[1] for row in db.execute('PRAGMA table_info(threads)')}
                if not {'id', 'name'} <= columns:
                    continue
                # threads.title can contain a first prompt; the app displays name.
                for identity, name in db.execute('SELECT id, name FROM threads'):
                    if isinstance(identity, str) and isinstance(name, str) and name.strip():
                        titles[identity] = name.strip()
            break
        except (OSError, sqlite3.Error):
            warnings.append(str(path) + ': 无法读取 Codex 会话名称，已回退名称索引')
    return titles, list(dict.fromkeys(warnings))


def read_project_metadata(home):
    """Read project labels and roots from the rollout source's Codex state."""
    databases = sorted(
        (p for p in Path(home).glob('state_*.sqlite') if p.stem[6:].isdigit()),
        key=lambda p: int(p.stem[6:]), reverse=True,
    )
    for path in databases:
        try:
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
                project_columns = {row[1] for row in db.execute('PRAGMA table_info(projects)')}
                root_columns = {row[1] for row in db.execute('PRAGMA table_info(project_roots)')}
                if not {'id', 'name'} <= project_columns or not {'project_id', 'path', 'position'} <= root_columns:
                    continue
                roots = []
                for root, position, name in db.execute(
                    'SELECT r.path, r.position, p.name FROM project_roots r '
                    'JOIN projects p ON p.id = r.project_id'
                ):
                    if (isinstance(root, str) and os.path.isabs(root) and isinstance(position, int)
                            and isinstance(name, str) and name.strip()):
                        roots.append((os.path.normpath(root), position, name.strip()))
                thread_columns = {row[1] for row in db.execute('PRAGMA table_info(threads)')}
                thread_names = {}
                if {'id', 'project_id'} <= thread_columns:
                    for identity, name in db.execute(
                        'SELECT t.id, p.name FROM threads t JOIN projects p ON p.id = t.project_id'
                    ):
                        if isinstance(identity, str) and isinstance(name, str) and name.strip():
                            thread_names[identity] = name.strip()
                return thread_names, roots
        except (OSError, sqlite3.Error):
            # Session metadata reports unreadable state; an older schema may work.
            continue
    return {}, []


def _git_common_dir(directory):
    """Resolve a checkout's .git marker, including linked worktrees."""
    marker = Path(directory) / '.git'
    try:
        if marker.is_dir():
            gitdir = marker.resolve()
        elif marker.is_file():
            with marker.open(encoding='utf-8') as stream:
                line = stream.readline(4096).strip()
            if not line.startswith('gitdir: '):
                return None
            gitdir = (marker.parent / line[8:]).resolve()
        else:
            return None
        common = gitdir / 'commondir'
        if common.is_file():
            with common.open(encoding='utf-8') as stream:
                gitdir = (gitdir / stream.readline(4096).strip()).resolve()
        return gitdir
    except (OSError, UnicodeError, RuntimeError):
        return None


def project_name(identity, cwd, metadata):
    """Prefer Codex's explicit thread association, then the closest root."""
    thread_names, roots = metadata
    if identity in thread_names:
        return thread_names[identity]
    if not roots or not isinstance(cwd, str) or not os.path.isabs(cwd):
        return None
    cwd = os.path.normpath(cwd)
    candidates = []
    for root, position, name in roots:
        try:
            if os.path.commonpath((root, cwd)) == root:
                candidates.append((len(Path(root).parts), position, name))
        except ValueError:
            continue
    if candidates:
        depth = max(row[0] for row in candidates)
        priority = min(row[1] for row in candidates if row[0] == depth)
        names = {row[2] for row in candidates if row[0] == depth and row[1] == priority}
        return next(iter(names)) if len(names) == 1 else None

    # Linked worktrees live outside the project root; compare Git's common dir.
    try:
        checkout = next((p for p in (Path(cwd), *Path(cwd).parents) if (p / '.git').exists()), None)
    except OSError:
        return None
    common = _git_common_dir(checkout) if checkout else None
    if common is None:
        return None
    matches = [(position, name) for root, position, name in roots if _git_common_dir(root) == common]
    if not matches:
        return None
    priority = min(position for position, _ in matches)
    names = {name for position, name in matches if position == priority}
    return next(iter(names)) if len(names) == 1 else None
