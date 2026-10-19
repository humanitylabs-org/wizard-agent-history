"""Durable-worker entry: install the isolated stock lock, then run acceptance."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--config', required=True)
    args = p.parse_args()
    config = Path(args.config).resolve()
    cfg = json.loads(config.read_text())
    root = Path(cfg['test_root']).resolve()
    os.umask(0o077)
    env = os.environ.copy()
    for name, relative in [('UV_PROJECT_ENVIRONMENT', 'stock-venv'), ('UV_CACHE_DIR', 'uv-cache'),
                           ('TMPDIR', 'tmp'), ('HERMES_HOME', 'stock-home'), ('HOME', 'isolated-home')]:
        path = root / relative
        path.mkdir(parents=True, exist_ok=True)
        env[name] = str(path)
    env['UV_PYTHON_DOWNLOADS'] = 'never'
    private = root / 'evidence-private'
    private.mkdir(mode=0o700, exist_ok=True)
    with open(private / 'environment-install.log', 'w') as log:
        result = subprocess.run([cfg['uv'], 'sync', '--frozen', '--python', sys.executable,
                                 '--extra', 'messaging', '--project', cfg['hermes_checkout']],
                                env=env, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        print(json.dumps({'status': 'blocked', 'phase': 'isolated_stock_environment', 'exit_code': result.returncode}), flush=True)
        return result.returncode
    python = root / 'stock-venv/bin/python'
    # Import actual stock modules before the multi-hour data pass, not at its end.
    env['PYTHONPATH'] = cfg['hermes_checkout']
    with open(private / 'stock-import.log', 'w') as log:
        result = subprocess.run([str(python), '-c', 'from hermes_state import SessionDB; from tools.session_search_tool import session_search; print("stock imports ok")'],
                                env=env, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        print(json.dumps({'status': 'blocked', 'phase': 'stock_import', 'exit_code': result.returncode}), flush=True)
        return result.returncode
    env.pop('PYTHONPATH', None)
    return subprocess.run([str(python), '-m', 'ops.full_scale', '--config', str(config)], env=env).returncode


if __name__ == '__main__':
    raise SystemExit(main())
