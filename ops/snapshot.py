"""Create one consistent online-backup snapshot; source is strictly read-only."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--source', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--min-free-gib', type=int, default=80)
    args = p.parse_args()
    os.umask(0o077)
    source, output = Path(args.source).resolve(), Path(args.output).resolve()
    if output.exists() or output == source:
        raise ValueError('snapshot destination must be new')
    def safe():
        now = datetime.now(timezone.utc)
        if now.hour >= 21 or now.hour == 0 and now.minute < 30:
            raise RuntimeError('nightly-backup exclusion window')
        if shutil.disk_usage(output.parent).free < args.min_free_gib * 1024**3:
            raise RuntimeError('disk free-space floor reached')
    safe()
    if shutil.disk_usage(output.parent).free - source.stat().st_size < args.min_free_gib * 1024**3:
        raise RuntimeError('snapshot would violate free-space floor')
    start = time.monotonic()
    src = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=.5)
    src.execute('PRAGMA query_only=ON')
    src.execute('PRAGMA busy_timeout=500')
    dst = sqlite3.connect(output)
    last = [0.0]
    def progress(status, remaining, total):
        if time.monotonic() - last[0] > 5:
            safe()
            last[0] = time.monotonic()
    # Pin one read snapshot for this explicit full-backup operation only. Without
    # it, frequent source writes restart sqlite3_backup from page zero indefinitely.
    # Hourly sync never uses this long transaction.
    src.execute('BEGIN')
    src.execute('SELECT rootpage FROM sqlite_master LIMIT 1').fetchone()
    try:
        src.backup(dst, pages=2048, progress=progress, sleep=.05)
        dst.execute('PRAGMA journal_mode=DELETE')
    finally:
        dst.close()
        src.close()
    print(json.dumps({'bytes': output.stat().st_size, 'seconds': round(time.monotonic()-start, 3)}))


if __name__ == '__main__':
    main()
