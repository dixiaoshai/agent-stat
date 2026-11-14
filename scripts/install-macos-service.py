#!/usr/bin/env python3
"""Install the stats dashboard (and Trace follower) as per-user login services.

The dashboard runs in archive-only mode: it never reads Codex logs. New sessions
arrive through `agent-stats follow`, which talks to the local agent-trace API.
When Trace assets exist in ~/.local/share/agent-trace, this installs the three
services together; otherwise it only installs the dashboard and explains what is
still missing.
"""
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path


def install(label, config, target, mode=0o600):
    domain = 'gui/%d' % os.getuid()
    pending = target.with_suffix('.plist.pending')
    with pending.open('wb') as stream:
        plistlib.dump(config, stream)
    pending.chmod(mode)
    subprocess.run(['/usr/bin/plutil', '-lint', str(pending)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pending.replace(target)
    def loaded():
        return subprocess.run(['/bin/launchctl', 'print', domain + '/' + label],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

    if loaded():
        subprocess.run(['/bin/launchctl', 'bootout', domain + '/' + label], check=True)
        # bootout can return before the job is fully gone; bootstrap would then
        # fail with a transient I/O error.
        for _ in range(50):
            if not loaded():
                break
            time.sleep(0.1)
    subprocess.run(['/bin/launchctl', 'enable', domain + '/' + label], check=True)
    for attempt in range(20):
        result = subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(target)],
                                capture_output=True, text=True)
        if result.returncode == 0:
            break
        if loaded():
            break
        time.sleep(0.25)
    else:
        raise SystemExit('launchctl bootstrap failed for %s: %s' % (label, result.stderr.strip()))
    print('Installed and started ' + domain + '/' + label)


def main():
    if sys.platform != 'darwin':
        raise SystemExit('This installer requires macOS')
    root = Path(__file__).resolve().parents[1]
    home = Path.home()
    state = home / '.local/share/agent-stats'
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    agents = home / 'Library/LaunchAgents'
    agents.mkdir(parents=True, exist_ok=True)
    trace_state = home / '.local/share/agent-trace'

    for name in ('server.stderr.log', 'follow.log', 'follow.stderr.log'):
        (state / name).touch(mode=0o600, exist_ok=True)

    dashboard = {
        'Label': 'local.agent-stats',
        'ProgramArguments': [sys.executable, '-u', '-m', 'agent_stats', '--db', str(state / 'stats.sqlite3'),
                             'serve', '--archive-only', '--port', '8765'],
        'WorkingDirectory': str(root),
        'EnvironmentVariables': {'PYTHONDONTWRITEBYTECODE': '1'},
        'RunAtLoad': True,
        'KeepAlive': True,
        'ThrottleInterval': 30,
        'ProcessType': 'Background',
        'StandardOutPath': '/dev/null',
        'StandardErrorPath': str(state / 'server.stderr.log'),
    }
    target = agents / 'local.agent-stats.plist'
    if target.exists():
        with target.open('rb') as stream:
            if plistlib.load(stream).get('Label') != 'local.agent-stats':
                raise SystemExit('Existing plist has a different label; refusing to overwrite')
    install('local.agent-stats', dashboard, target)

    service_config = trace_state / 'service.json'
    follow_config = trace_state / 'stats.follow.json'
    token_file = trace_state / 'stats.token'
    node = shutil.which('node')
    if not (service_config.is_file() and follow_config.is_file() and token_file.is_file() and node):
        print('Installed the archive-only dashboard only:')
        print('  dashboard: http://127.0.0.1:8765/  (reads only already-received data)')
        print('  missing Trace assets under %s or node in PATH;' % trace_state)
        print('  the dashboard will stay empty/stale until agent-trace serve and follow run.')
        return

    token = token_file.read_text(encoding='utf-8').strip()
    follow = {
        'Label': 'local.agent-stats-follow',
        'ProgramArguments': [sys.executable, '-u', '-m', 'agent_stats', 'follow',
                             '--config', str(follow_config)],
        'WorkingDirectory': str(root),
        'EnvironmentVariables': {'PYTHONDONTWRITEBYTECODE': '1', 'AGENT_TRACE_STATS_TOKEN': token},
        'RunAtLoad': True,
        'KeepAlive': True,
        'ThrottleInterval': 30,
        'ProcessType': 'Background',
        'StandardOutPath': str(state / 'follow.log'),
        'StandardErrorPath': str(state / 'follow.stderr.log'),
    }
    trace = {
        'Label': 'local.agent-trace',
        'ProgramArguments': [node, '--experimental-strip-types', str(root.parent / 'agent-trace/src/cli.ts'),
                             'serve', '--config', str(service_config)],
        'WorkingDirectory': str(trace_state),
        'EnvironmentVariables': {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'},
        'RunAtLoad': True,
        'KeepAlive': True,
        'ThrottleInterval': 30,
        'ProcessType': 'Background',
        'StandardOutPath': str(trace_state / 'trace.log'),
        'StandardErrorPath': str(trace_state / 'trace.err'),
    }
    install('local.agent-trace', trace, agents / 'local.agent-trace.plist')
    install('local.agent-stats-follow', follow, agents / 'local.agent-stats-follow.plist')
    print('Dashboard (archive-only): http://127.0.0.1:8765/')
    print('Follow worker log: %s' % (state / 'follow.log'))
    print('Trace service log: %s' % (trace_state / 'trace.log'))


if __name__ == '__main__':
    main()
