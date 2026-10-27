#!/usr/bin/env python3
"""Run the PostgreSQL exporter inside worker and atomically publish node_exporter text files."""
import argparse
from pathlib import Path
import subprocess
import time


def atomic(path, content):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(content)
    temporary.chmod(0o644)
    temporary.replace(path)


def status_text(success, started):
    return ('# HELP signal_foundry_collector_success signal_foundry_collector_success\n'
            '# TYPE signal_foundry_collector_success gauge\n'
            f'signal_foundry_collector_success {float(success):.6f}\n'
            '# HELP signal_foundry_collector_timestamp_seconds signal_foundry_collector_timestamp_seconds\n'
            '# TYPE signal_foundry_collector_timestamp_seconds gauge\n'
            f'signal_foundry_collector_timestamp_seconds {time.time():.6f}\n'
            '# HELP signal_foundry_collector_duration_seconds signal_foundry_collector_duration_seconds\n'
            '# TYPE signal_foundry_collector_duration_seconds gauge\n'
            f'signal_foundry_collector_duration_seconds {time.monotonic()-started:.6f}\n')


def collect(project, output, timeout=90):
    started = time.monotonic()
    output = Path(output)
    error = ''
    try:
        result = subprocess.run([
            'docker', 'compose', '-f', 'compose.yaml', '-f', 'compose.release.yaml',
            'exec', '-T', 'worker', 'python', 'monitoring.py'],
            cwd=project, capture_output=True, text=True, timeout=timeout, check=True)
        if not result.stdout.startswith('# HELP signal_foundry_'):
            raise RuntimeError('Invalid metrics payload')
        atomic(output / 'signal-foundry.prom', result.stdout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, RuntimeError) as exc:
        error = type(exc).__name__
    atomic(output / 'signal-foundry-status.prom', status_text(not error, started))
    if error:
        raise RuntimeError('Signal Foundry metrics failed: ' + error)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', default='/opt/signal-foundry')
    parser.add_argument('--output', default='/var/lib/infra-node-exporter', type=Path)
    parser.add_argument('--timeout', type=int, default=90)
    args = parser.parse_args()
    try:
        collect(args.project, args.output, args.timeout)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == '__main__':
    main()
