#!/usr/bin/env python3
"""Install this checkout as a per-user macOS login service."""
import os
from pathlib import Path
import plistlib
import subprocess
import sys


def main():
    if sys.platform != 'darwin':
        raise SystemExit('This installer requires macOS')
    root = Path(__file__).resolve().parents[1]
    home = Path.home()
    state = home / '.local/share/agent-stats'
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    error_log = state / 'server.stderr.log'
    error_log.touch(mode=0o600, exist_ok=True)
    codex = Path(os.environ.get('CODEX_HOME', str(home / '.codex'))).expanduser().resolve()
    label = 'local.agent-stats'
    domain = 'gui/%d' % os.getuid()
    agents = home / 'Library/LaunchAgents'
    agents.mkdir(parents=True, exist_ok=True)
    target = agents / (label + '.plist')
    config = {
        'Label': label,
        'ProgramArguments': [sys.executable, '-u', '-m', 'agent_stats', '--db', str(state / 'stats.sqlite3'),
                             'serve', '--source', str(codex / 'sessions'), '--source', str(codex / 'archived_sessions')],
        'WorkingDirectory': str(root),
        'EnvironmentVariables': {'PYTHONDONTWRITEBYTECODE': '1'},
        'RunAtLoad': True,
        'KeepAlive': True,
        'ThrottleInterval': 30,
        'ProcessType': 'Background',
        'StandardOutPath': '/dev/null',
        'StandardErrorPath': str(error_log),
    }
    if target.exists():
        with target.open('rb') as stream:
            if plistlib.load(stream).get('Label') != label:
                raise SystemExit('Existing plist has a different label; refusing to overwrite')
    pending = target.with_suffix('.plist.pending')
    with pending.open('wb') as stream:
        plistlib.dump(config, stream)
    pending.chmod(0o644)
    subprocess.run(['/usr/bin/plutil', '-lint', str(pending)], check=True)
    pending.replace(target)
    # Re-installing updates the same service instead of creating another process.
    if subprocess.run(['/bin/launchctl', 'print', domain + '/' + label], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        subprocess.run(['/bin/launchctl', 'bootout', domain + '/' + label], check=True)
    subprocess.run(['/bin/launchctl', 'enable', domain + '/' + label], check=True)
    subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(target)], check=True)
    print('Installed and started ' + domain + '/' + label)
    print('http://127.0.0.1:8765/')


if __name__ == '__main__':
    main()
