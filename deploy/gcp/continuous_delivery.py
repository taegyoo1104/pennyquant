#!/usr/bin/env python3
"""Opt-in VM pull CD. Successful main CI only; no inbound SSH keys or trading startup."""
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from urllib.request import Request, urlopen

REPO = 'taegyoo1104/pennyquant'
ROOT = Path('/opt/sessionquant')
STATE = Path('/var/lib/sessionquant/cd')
COMPOSE = ['docker', 'compose', '-f', str(ROOT/'deploy/gcp/compose.yaml')]
UNITS = ('sessionquant-market-close.service', 'sessionquant-run-limit.service',
         'sessionquant-cost-guard.service', 'sessionquant-maintenance-catchup.service')


def run(args, *, timeout=60, **kwargs):
    return subprocess.run(args, cwd=ROOT, check=True, text=True, capture_output=True, timeout=timeout, **kwargs).stdout.strip()


def api(path):
    request = Request('https://api.github.com/repos/'+REPO+'/'+path,
                      headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'SessionQuant-CD'})
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def ci_passed(runs, sha):
    eligible = [r for r in runs if r.get('head_sha') == sha and r.get('event') == 'push'
                and r.get('head_branch') == 'main' and r.get('path') == '.github/workflows/ci.yml'
                and (r.get('repository') or {}).get('full_name') == REPO]
    if not eligible:
        return False
    latest = max(eligible, key=lambda r: (r['id'], r.get('run_attempt', 1)))
    return latest.get('status') == 'completed' and latest.get('conclusion') == 'success'


def record(status, **extra):
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    result = dict(status=status, checked_at=datetime.now(timezone.utc).isoformat(), **extra)
    temporary = STATE/'status.tmp'
    temporary.write_text(json.dumps(result)+'\n')
    temporary.replace(STATE/'status.json')
    print(json.dumps(result), flush=True)


def readiness():
    if any((Path('/etc/sessionquant')/name).exists() for name in ('maintenance', 'storage-blocked')):
        return 'MAINTENANCE_BLOCKED'
    for unit in UNITS:
        if subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0:
            return 'MAINTENANCE_RUNNING'
    # Probe locks without holding them while the existing update script acquires its own locks.
    for name in ('lifecycle', 'update'):
        with open('/run/sessionquant-'+name+'.lock', 'a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 'LIFECYCLE_BUSY'
    active = run(COMPOSE+['ps', '--status', 'running', '--services', 'app'])
    if active:
        command = run(['docker', 'inspect', 'sessionquant-app-1', '--format', '{{json .Config.Cmd}}'])
        if command != '["python","-m","sessionquant.inspection_dashboard"]':
            return 'TRADING_RUNTIME_ACTIVE'
    # Includes calendar, OFFLINE control, pending orders, positions, exit intents and engine DB lock.
    # Read-only guard fails closed; no application stop or backup has happened yet.
    try:
        run(COMPOSE+['run', '--rm', '--no-deps', '-T', 'app', 'python', '-', '--off-hours'],
            input=(ROOT/'deploy/gcp/update_guard.py').read_text(), timeout=60)
    except subprocess.CalledProcessError:
        return 'WINDOW_OR_LEDGER_NOT_READY'
    return None


def deliver():
    if not Path('/etc/sessionquant/cd-enabled').is_file():
        record('DISABLED')
        return
    # A failed/abandoned attempt requires explicit operator review even if main advances.
    if (STATE/'attempt.json').exists():
        record('BLOCKED', reason='PREVIOUS_ATTEMPT_REQUIRES_REVIEW')
        return
    sha = api('commits/main')['sha']
    if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40}', sha):
        raise ValueError('Invalid main revision')
    if run(['git', 'rev-parse', 'HEAD']) == sha:
        record('CURRENT', revision=sha)
        return
    runs = api(f'actions/workflows/ci.yml/runs?branch=main&event=push&head_sha={sha}&per_page=20')['workflow_runs']
    if not ci_passed(runs, sha):
        record('DEFERRED', revision=sha, reason='WAITING_FOR_MAIN_CI')
        return
    reason = readiness()
    if reason:
        record('DEFERRED', revision=sha, reason=reason)
        return
    run(['git', 'fetch', 'origin', 'refs/heads/main:refs/remotes/origin/main'], timeout=120)
    if run(['git', 'rev-parse', 'origin/main']) != sha:
        record('DEFERRED', reason='MAIN_MOVED')
        return
    run(['git', 'merge-base', '--is-ancestor', 'HEAD', sha])
    script = run(['git', 'show', sha+':deploy/gcp/update_preopen.sh'])
    # Mark before any operation that can stop app/create backup. Crash/reboot leaves the marker.
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (STATE/'attempt.json').open('x') as handle:
        json.dump({'revision': sha, 'started_at': datetime.now(timezone.utc).isoformat()}, handle)
        handle.flush()
        os.fsync(handle.fileno())
    record('DEPLOYING', revision=sha)
    try:
        with tempfile.NamedTemporaryFile(mode='w', prefix='sq-cd-', suffix='.sh') as target:
            target.write(script+'\n')
            target.flush()
            # The updater owns lifecycle locks and rechecks the calendar/ledger around builds/stops.
            with (STATE/'deploy.log').open('w') as output:
                subprocess.run(['bash', target.name, sha, '--off-hours'], cwd=ROOT, check=True,
                               stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
        if run(['git', 'rev-parse', 'HEAD']) != sha:
            raise ValueError('Deployed revision mismatch')
        command = run(['docker', 'inspect', 'sessionquant-app-1', '--format', '{{json .Config.Cmd}}'])
        if command != '["python","-m","sessionquant.inspection_dashboard"]':
            raise ValueError('Expected inspection dashboard')
        for port in ([8501, 8502] if Path('/etc/sessionquant/public-enabled').is_file() else [8501]):
            with urlopen(f'http://127.0.0.1:{port}/_stcore/health', timeout=5) as response:
                if response.status != 200 or response.read(16).strip() != b'ok':
                    raise ValueError('Dashboard health failed')
        record('COMPLETE', revision=sha)
        (STATE/'attempt.json').unlink()
    except Exception:
        record('FAILED', revision=sha, reason='REVIEW_DEPLOY_LOG')
        raise


def main():
    if os.geteuid() != 0 or Path('/etc/hostname').read_text().strip().split('.')[0] != 'sessionquant-paper':
        raise SystemExit('sessionquant-paper root only')
    os.umask(0o077)
    with open('/run/sessionquant-cd.lock', 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            deliver()
        except Exception as exc:
            # Never dump provider responses, environment values, or command stderr to public UI.
            if not (STATE/'attempt.json').exists():
                record('DEFERRED', reason='CHECK_FAILED', error_type=type(exc).__name__)
            raise SystemExit(1) from None


if __name__ == '__main__':
    main()
