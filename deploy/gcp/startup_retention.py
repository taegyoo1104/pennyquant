"""Prepare a stopped Paper deployment for startup without trusting old reports.

The caller holds the lifecycle lock and has made MySQL available.
Only a live CLEAR/DISABLED probe skips maintenance. Every other result needs a
verified backup and the existing protected deletion path before startup proceeds.
This host-side helper intentionally uses only the Python standard library.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import tempfile
import time
from typing import NamedTuple
import uuid


ROOT = Path('/opt/sessionquant')
BACKUP_STATUS = Path('/etc/sessionquant/backup-status')
SUMMARY = Path('/var/lib/sessionquant/health/startup-retention-last.json')
STATUS = Path('/etc/sessionquant/startup-retention-status')
SEAL = Path('/etc/sessionquant/nightly-seal.json')
COMPOSE = ['docker', 'compose', '-f', 'deploy/gcp/compose.yaml']
PROBE_STATES = {'CLEAR', 'NEEDS_CLEANUP', 'UNKNOWN', 'DISABLED'}
PROBE_REASONS = {
    'POLICY_DISABLED', 'NO_ELIGIBLE_ROWS', 'ELIGIBLE_ROWS', 'TIME_LIMIT',
    'PROTECTION_ROW_LIMIT', 'ROW_LIMIT', 'ROWS_CHANGED', 'READ_ERROR', 'ENGINE_LEASE_UNAVAILABLE',
}


class CommandResult(NamedTuple):
    returncode: int
    stdout: str = ''
    truncated: bool = False
    timed_out: bool = False


class PreparationError(Exception):
    """A fixed, content-free reason that must prevent startup."""


def utcnow():
    return datetime.now(timezone.utc)


def _kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _interrupt_group(process):
    # An operator interrupt must give backup/retention shell traps time to
    # remove temporary archives/one-off containers and record failure.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        _kill_group(process)
        process.wait(timeout=5)


def run_command(command, *, timeout, output_limit=65536, live=False):
    """Drain output with a fixed memory bound and kill descendants on timeout.

    Backup/retention also use GNU timeout so their shell traps get a grace period.
    The host deadline is a final bound if that wrapper or Docker stops responding.
    Probe output is bounded and never placed in the public summary. Existing
    backup/cleanup commands may stream their stage progress to the caller.
    """
    process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                               stdout=None if live else subprocess.PIPE,
                               stderr=None if live else subprocess.DEVNULL,
                               start_new_session=True)
    selector = selectors.DefaultSelector()
    output = bytearray()
    truncated = timed_out = False
    deadline = time.monotonic() + timeout
    try:
        if process.stdout is not None:
            selector.register(process.stdout, selectors.EVENT_READ)
        while selector.get_map() or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _kill_group(process)
                break
            for key, _ in selector.select(min(remaining, 0.2)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                room = output_limit - len(output)
                output.extend(chunk[:room])
                truncated = truncated or len(chunk) > room
        process.wait(timeout=5)
        return CommandResult(process.returncode, output.decode('utf-8', errors='replace'),
                             truncated, timed_out)
    finally:
        selector.close()
        if process.stdout is not None:
            process.stdout.close()
        if process.poll() is None:
            _interrupt_group(process)


def probe():
    """A failed/incomplete live read is UNKNOWN; never infer an empty backlog."""
    container = 'sessionquant-startup-retention-' + str(uuid.uuid4())
    try:
        result = run_command(COMPOSE + [
            'run', '--rm', '--no-deps', '-T', '--name', container, 'app',
            'sh', '-c', 'exec python -m sessionquant.database.retention_probe '
            '--config "${SQ_SESSION_CONFIG:-config/sessionquant.realtime-paper.example.yaml}"',
        ], timeout=45)
        if result.timed_out:
            return 'UNKNOWN', 'PROBE_TIMEOUT'
        if result.returncode:
            return 'UNKNOWN', 'PROBE_ERROR'
        if result.truncated:
            return 'UNKNOWN', 'PROBE_OUTPUT_LIMIT'
        try:
            payload = json.loads(result.stdout)
            if (not isinstance(payload, dict) or payload.get('read_only') is not True
                    or payload.get('status') not in PROBE_STATES):
                return 'UNKNOWN', 'PROBE_INVALID'
            reason = payload.get('reason')
            return payload['status'], reason if reason in PROBE_REASONS else 'LIVE_PROBE'
        except (ValueError, TypeError):
            return 'UNKNOWN', 'PROBE_INVALID'
    except InterruptedError:
        raise
    except (OSError, subprocess.SubprocessError):
        return 'UNKNOWN', 'PROBE_ERROR'
    finally:
        # Killing the Docker client alone leaves a compose one-off running.
        # --rm also covers normal completion; this exact name covers timeout/exit.
        try:
            run_command(['docker', 'rm', '-f', container], timeout=10, output_limit=1024)
        except InterruptedError:
            raise
        except (OSError, subprocess.SubprocessError):
            pass


def read_receipt():
    try:
        with BACKUP_STATUS.open('rb') as stream:
            value = stream.read(4097)
    except FileNotFoundError:
        return 'MISSING'
    if len(value) > 4096:
        raise PreparationError('BACKUP_STATUS_TOO_LARGE')
    # Match shell $(cat ...) exactly, including malformed whitespace in an old
    # receipt. Only the newly uploaded receipt may authorize deletion.
    return value.decode('utf-8', errors='replace').rstrip('\n')


def receipt_time(receipt, now):
    match = re.fullmatch(
        r'UPLOADED (\S+) (sessionquant-[0-9]{8}T[0-9]{6}Z-[a-zA-Z0-9-]+\.tar\.gz)', receipt)
    if match is None:
        return None
    try:
        stamp = datetime.fromisoformat(match[1].replace('Z', '+00:00'))
        if stamp.tzinfo is not None and timedelta(0) <= now - stamp <= timedelta(hours=1):
            return stamp
    except (ValueError, OverflowError):
        pass
    return None


def _atomic_write(path, content, *, owner, mode):
    # Parent directories are provisioned by deployment. Health lives outside
    # the sealed app snapshot, so a read-only CLEAR preserves that proof.
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fchown(stream.fileno(), owner, owner)
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def persist(summary):
    try:
        SUMMARY.parent.mkdir(mode=0o755)
    except FileExistsError:
        pass
    else:
        # The lifecycle scripts deliberately use umask 077. The app's read-only
        # health mount still needs directory traversal after first provisioning.
        SUMMARY.parent.chmod(0o755)
    _atomic_write(SUMMARY, json.dumps(summary, sort_keys=True) + '\n', owner=1000, mode=0o644)
    fields = ' '.join(f'{key}={str(summary[key]).lower() if isinstance(summary[key], bool) else summary[key]}'
                      for key in ('checked_at', 'before', 'after', 'cleanup_attempted', 'backup_reused', 'reason'))
    _atomic_write(STATUS, summary['status'] + ' ' + fields + '\n', owner=0, mode=0o600)


def prepare(backup_before=None):
    """Return (exit code, summary); this never starts the application itself."""
    summary = {'version': 1, 'checked_at': '', 'before': 'UNKNOWN', 'after': 'UNKNOWN',
               'cleanup_attempted': False, 'backup_reused': False,
               'status': 'FAILED', 'reason': 'NOT_FINISHED'}
    exit_code = 1
    try:
        state = run_command(COMPOSE + ['ps', '--status', 'running', '--services', 'app'], timeout=15)
        if state.returncode or state.timed_out or state.truncated:
            raise PreparationError('APP_STATUS_ERROR')
        if state.stdout.strip():
            raise PreparationError('APP_RUNNING')

        print('시작 전 오래된 자료 정리 대상을 확인합니다 (읽기 전용).', flush=True)
        before, reason = probe()
        summary.update(before=before, after=before, reason=reason)
        if before in {'CLEAR', 'DISABLED'}:
            summary['status'] = before
            exit_code = 0
        else:
            previous = read_receipt()
            if (backup_before is not None and previous != backup_before
                    and receipt_time(previous, utcnow()) is not None):
                summary['backup_reused'] = True
                previous = backup_before
                print('이번 업데이트의 새 백업 업로드를 확인했습니다.', flush=True)
            else:
                print(f'자료 정리 확인: {before}. 정리 전 새 백업을 시작합니다 (최대 90분).', flush=True)
                backup_started = utcnow().replace(microsecond=0)
                backup = run_command(['timeout', '--kill-after=30', '5400', 'bash',
                                      'deploy/gcp/backup.sh'], timeout=5440, live=True)
                if backup.returncode or backup.timed_out:
                    raise PreparationError('BACKUP_FAILED')
                current = read_receipt()
                uploaded = receipt_time(current, utcnow())
                if current == previous or uploaded is None or uploaded < backup_started:
                    raise PreparationError('BACKUP_NOT_VERIFIED')

            if SEAL.exists():
                writable = run_command(['python3', 'deploy/gcp/nightly_checkpoint.py', 'writable'], timeout=45)
                if writable.returncode or writable.timed_out:
                    raise PreparationError('UNSEAL_FAILED')

            # Preserve the real pre-backup receipt. retention.sh independently
            # verifies that today's UPLOADED receipt changed before any deletion.
            summary['cleanup_attempted'] = True
            summary['after'] = 'UNKNOWN'
            print('백업 확인 완료. 같은 DB 연결·잠금을 유지하며 보호된 자료를 최대 10분 정리합니다.', flush=True)
            cleanup = run_command(['timeout', '--kill-after=15', '690', 'bash',
                                   'deploy/gcp/retention_after_update.sh', previous], timeout=710, live=True)
            if cleanup.returncode or cleanup.timed_out:
                raise PreparationError('CLEANUP_FAILED')

            print('자료 정리 후 남은 대상을 다시 확인합니다 (읽기 전용).', flush=True)
            after, reason = probe()
            summary.update(after=after, reason=reason,
                           status='PENDING' if after == 'NEEDS_CLEANUP' else after)
            exit_code = 0
    except PreparationError as exc:
        summary.update(status='FAILED', reason=str(exc))
    except (InterruptedError, KeyboardInterrupt):
        summary.update(status='FAILED', reason='INTERRUPTED')
    except (OSError, subprocess.SubprocessError):
        summary.update(status='FAILED', reason='COMMAND_ERROR')
    finally:
        summary['checked_at'] = utcnow().isoformat()
        persist(summary)
    return exit_code, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description='시작 전 자료 정리 확인·백업·보호된 정리')
    parser.add_argument('--backup-before', help='업데이트 백업 직전에 실제로 읽은 백업 상태')
    args = parser.parse_args(argv)

    def interrupted(_signum, _frame):
        # selectors catches InterruptedError to retry an interrupted syscall.
        # Use a control-flow exception so an operator stop is not swallowed.
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        result, summary = prepare(args.backup_before)
    except OSError:
        print('시작 전 자료 정리 상태를 저장하지 못했습니다. 프로그램 시작을 중단합니다.')
        return 1
    print(f"시작 전 자료 정리: {summary['status']} ({summary['reason']})")
    return result


if __name__ == '__main__':
    raise SystemExit(main())
