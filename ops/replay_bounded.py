"""Bounded, resumable acceptance for an existing full-size rehearsal.

Reuses the History, trimmed copy and frozen query list that ops.full_scale
already produced; never re-syncs, re-seals or re-trims. Every search runs in a
worker process with a per-call timeout and every result is checkpointed, so a
deadline leaves a partial, honest receipt instead of nothing.

Writes aggregates to <test_root>/replay-acceptance.json; per-query detail stays
in evidence-private/replay.jsonl (hashed query ids only).
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time

from agent_history.store import batches, canonical, file_hash, ro

WORKER = r'''
import json, os, sys, time
from datetime import datetime, timezone
cfg = json.loads(sys.argv[1])
sys.path.insert(0, cfg['hermes_checkout'])
from hermes_state import SessionDB
from tools.session_search_tool import session_search, _parse_iso_bound
from agent_history.store import History
baseline = SessionDB(db_path=cfg['source'], read_only=True)
trimmed = SessionDB(db_path=cfg['trimmed'], read_only=True)
h = History(cfg['root'], cfg.get('agent', 'hermes'), writable=False)
print(json.dumps({'ready': True}), flush=True)
for line in sys.stdin:
    args = json.loads(line)
    exact = {k: v for k, v in args.items() if k in (
        'query', 'role_filter', 'limit', 'session_id', 'around_message_id',
        'window', 'sort', 'detail', 'after', 'before', 'exclude_session_ids')}
    for field in ('after', 'before'):
        if exact.get(field):
            exact[field] = datetime.fromtimestamp(_parse_iso_bound(exact[field]), timezone.utc).isoformat()
    out = {}
    print(json.dumps({'stage': 'baseline'}), flush=True)
    t = time.monotonic()
    prior = json.loads(session_search(db=baseline, **exact))
    out['baseline_seconds'] = round(time.monotonic() - t, 3)
    print(json.dumps({'stage': 'trimmed', 'baseline_seconds': out['baseline_seconds']}), flush=True)
    t = time.monotonic()
    after = json.loads(session_search(db=trimmed, **exact))
    out['trimmed_seconds'] = round(time.monotonic() - t, 3)
    if prior.get('success') is False or after.get('success') is False:
        out['tool_error'] = True
        print(json.dumps(out), flush=True)
        continue
    old = {r['session_id'] for r in prior.get('results', [])}
    live = {r['session_id'] for r in after.get('results', [])}
    missing = old - live
    recovered = set()
    if missing:
        print(json.dumps({'stage': 'history'}), flush=True)
        t = time.monotonic()
        try:
            recovered = {x['session_id'] for x in h.search(args['query'], deep=True, limit=1000)}
        except ValueError:
            out['archive_query_error'] = True
        out['history_seconds'] = round(time.monotonic() - t, 3)
    out.update(baseline_sessions=len(old), trimmed_sessions=len(live), live_misses=len(missing),
               recovered=len(missing & recovered), misses=len(missing - recovered),
               additional_live_sessions=len(live - old))
    print(json.dumps(out), flush=True)
'''


class Worker:
    def __init__(self, cfg, env):
        self.cfg, self.env = cfg, env
        self.proc = None

    def start(self, timeout):
        self.proc = subprocess.Popen([sys.executable, '-c', WORKER, json.dumps(self.cfg)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=self.env)
        if self._read(timeout) is None:
            self.stop()
            raise RuntimeError('replay worker did not become ready')

    def _read(self, timeout):
        ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not ready:
            return None
        line = self.proc.stdout.readline()
        return json.loads(line) if line else None

    def ask(self, args, timeout):
        self.proc.stdin.write(json.dumps(args) + '\n')
        self.proc.stdin.flush()
        end, stage = time.monotonic() + timeout, {}
        while True:
            msg = self._read(max(0.0, end - time.monotonic()))
            if msg is None:
                return dict(stage, timeout=True, timed_out_in=stage.pop('stage', 'start') if stage else 'start')
            if 'stage' in msg:
                stage = msg
                continue
            return msg

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=30)
        self.proc = None


def atomic_json(path, data):
    temp = path.with_suffix('.partial')
    temp.write_bytes(canonical(data) + b'\n')
    temp.replace(path)


def run(cfg, wall_seconds, call_timeout, sample, replay_only=False):
    root = Path(cfg['test_root']).resolve()
    private = root / 'evidence-private'
    receipt_path = root / ('replay-smoke.json' if replay_only else 'replay-acceptance.json')
    rehearsal = json.loads((root / 'acceptance.json').read_text())
    trimmed = root / 'trimmed.db'
    source = Path(cfg['source']).resolve()
    receipt = {'status': 'running', 'started_at': time.time(), 'wall_seconds': wall_seconds,
               'call_timeout_seconds': call_timeout,
               'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()}

    def save():
        atomic_json(receipt_path, receipt)

    # 1. The artifacts are exactly the ones the rehearsal produced.
    expected_src = rehearsal['source_checksum']['result']['sha256']
    expected_out = rehearsal['trim_new_file']['result']['output_sha256']
    receipt['artifacts'] = {} if replay_only else {'source_sha256_ok': file_hash(source) == expected_src,
                            'trimmed_sha256_ok': file_hash(trimmed) == expected_out}
    save()
    if receipt['artifacts'] and not all(receipt['artifacts'].values()):
        receipt['status'] = 'artifact_mismatch'
        save()
        return 1

    # 2. Long old human threads display identically through stock paging.
    sys.path.insert(0, cfg['hermes_checkout'])
    os.environ.update(HERMES_HOME=str(root / 'stock-home'), HOME=str(root / 'isolated-home'))
    from hermes_state import SessionDB
    from ops.full_scale import projection_hash
    with contextlib.closing(ro(source)) as src:
        sids = [r[0] for r in src.execute(
            "SELECT m.session_id,count(*) n FROM messages m JOIN sessions s ON s.id=m.session_id "
            "WHERE s.source IN ('cli','api_server','webui','telegram') AND m.timestamp<? "
            "GROUP BY m.session_id ORDER BY n DESC LIMIT 5", (time.time() - 30 * 86400,))]
    base_db, trim_db = SessionDB(db_path=str(source), read_only=True), SessionDB(db_path=str(trimmed), read_only=True)
    try:
        threads = []
        for sid in sids:
            a, b = projection_hash(base_db, sid), projection_hash(trim_db, sid)
            threads.append({'rows': a['rows'], 'identical': a == b})
    finally:
        base_db.close(); trim_db.close()
    receipt['long_thread_display'] = {'threads': len(threads), 'all_identical': all(t['identical'] for t in threads),
                                      'rows': [t['rows'] for t in threads]}
    save()

    # 3. Search replay, resumable, sampled deterministically by query hash.
    env = dict(os.environ, HERMES_HOME=str(root / 'stock-home'), HOME=str(root / 'isolated-home'),
               PYTHONPATH=str(Path.cwd()))
    queries = json.loads((private / 'queries.json').read_text())
    keyed = sorted(((hashlib.sha256(canonical(a)).hexdigest(), a) for a in queries), key=lambda x: x[0])
    keyed = [(k, a) for k, a in keyed if a.get('profile') in (None, '', 'default')]
    skipped_profiles = len(queries) - len(keyed)
    if sample:
        keyed = keyed[:sample]
    log = private / ('replay-smoke.jsonl' if replay_only else 'replay.jsonl')
    done = {}
    if log.exists():
        for line in log.read_text().splitlines():
            r = json.loads(line)
            done[r['query_sha256']] = r
    worker = Worker(dict(cfg, source=str(source), trimmed=str(trimmed)), env)
    worker.start(timeout=900)
    deadline = time.monotonic() + wall_seconds  # replay budget only
    try:
        with log.open('a') as out:
            for key, args in keyed:
                if key in done:
                    continue
                if time.monotonic() > deadline:
                    break
                result = worker.ask(args, call_timeout)
                if result.get('timeout'):
                    worker.stop()
                    worker.start(timeout=900)
                result['query_sha256'] = key
                out.write(json.dumps(result) + '\n')
                out.flush()
                done[key] = result
                rs = list(done.values())
                receipt['replay'] = {
                    'planned': len(keyed), 'completed': len(rs), 'skipped_other_profiles': skipped_profiles,
                    'timeouts': sum(bool(r.get('timeout')) for r in rs),
                    'timeouts_in_original_db': sum(r.get('timed_out_in') == 'baseline' for r in rs),
                    'timeouts_in_trimmed_db': sum(r.get('timed_out_in') == 'trimmed' for r in rs),
                    'timeouts_in_history': sum(r.get('timed_out_in') == 'history' for r in rs),
                    'tool_errors': sum(bool(r.get('tool_error')) for r in rs),
                    'queries_with_live_misses': sum(r.get('live_misses', 0) > 0 for r in rs),
                    'queries_with_misses_after_history': sum(r.get('misses', 0) > 0 for r in rs),
                    'additional_live_sessions': sum(r.get('additional_live_sessions', 0) for r in rs),
                    'baseline_seconds_median': sorted(r['baseline_seconds'] for r in rs if 'baseline_seconds' in r)[
                        len([r for r in rs if 'baseline_seconds' in r]) // 2] if any('baseline_seconds' in r for r in rs) else None,
                    'baseline_seconds_max': max((r['baseline_seconds'] for r in rs if 'baseline_seconds' in r), default=None),
                    'trimmed_seconds_max': max((r['trimmed_seconds'] for r in rs if 'trimmed_seconds' in r), default=None),
                }
                save()
    finally:
        worker.stop()
    if not replay_only:
        # 4. Every active row identical (provider replay context included).
        with contextlib.closing(ro(source)) as src, contextlib.closing(ro(trimmed)) as dst:
            digests, counts = [hashlib.sha256(), hashlib.sha256()], [0, 0]
            for i, c in enumerate((src, dst)):
                for batch in batches(c):
                    for row in batch:
                        if row['active']:
                            digests[i].update(canonical(dict(row)) + b'\n')
                            counts[i] += 1
            receipt['active_rows'] = {'rows': counts[0], 'identical': counts[0] == counts[1] and
                                      digests[0].digest() == digests[1].digest()}
            save()

        # 5. Structural integrity of the trimmed copy (read-only quick_check).
        with contextlib.closing(ro(trimmed)) as c:
            receipt['quick_check'] = c.execute('PRAGMA quick_check').fetchone()[0]
            save()

    rp = receipt.get('replay', {})
    clean = (receipt.get('active_rows', {}).get('identical') and receipt.get('quick_check') == 'ok'
             and receipt['long_thread_display']['all_identical']
             and rp.get('completed') == rp.get('planned')
             and not (rp.get('timeouts_in_trimmed_db') or rp.get('timeouts_in_history') or rp.get('timeouts', 0) - rp.get('timeouts_in_original_db', 0))
             and not rp.get('tool_errors') and not rp.get('queries_with_misses_after_history'))
    receipt['status'] = 'passed' if clean else ('partial' if rp.get('completed', 0) < rp.get('planned', 0) else 'gaps')
    receipt['finished_at'] = time.time()
    save()
    print(json.dumps({'status': receipt['status'], 'receipt': str(receipt_path)}), flush=True)
    return 0 if clean else 2


if __name__ == '__main__':
    os.umask(0o077)
    p = argparse.ArgumentParser()
    p.add_argument('--config', required=True)
    p.add_argument('--wall-seconds', type=int, default=4 * 3600, help='replay budget; checkpoints survive it')
    p.add_argument('--call-timeout', type=int, default=180)
    p.add_argument('--sample', type=int, default=0, help='0 = all frozen queries')
    p.add_argument('--replay-only', action='store_true', help='smoke: skip full-file hashing/integrity')
    a = p.parse_args()
    raise SystemExit(run(json.loads(Path(a.config).read_text()), a.wall_seconds, a.call_timeout, a.sample, a.replay_only))
