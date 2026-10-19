"""Full-size rehearsal runner. Logs aggregates only; private query bytes stay local.

Run only through the durable heavy lane, on a copied DB and isolated checkout.
The JSON config supplies every owner/host-specific path. Checkpoints survive failure.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import traceback

from agent_history.store import History, batches, canonical, file_hash, month_of, ro
from agent_history.trim import trim_copy


def atomic_json(path, data):
    temp = path.with_suffix('.partial')
    temp.write_bytes(canonical(data) + b'\n')
    temp.replace(path)


def query_calls(value):
    if isinstance(value, list):
        for x in value:
            yield from query_calls(x)
    elif isinstance(value, dict):
        fn = value.get('function', value)
        name = fn.get('name', value.get('recipient_name', ''))
        if name.split('.')[-1] in ('session_search', 'sessions_search'):
            args = fn.get('arguments', value.get('parameters', {}))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            if isinstance(args, dict) and isinstance(args.get('query'), str) and args['query'].strip():
                yield args
        for k, child in value.items():
            if isinstance(child, (dict, list)):
                yield from query_calls(child)
            elif k in ('arguments', 'parameters') and isinstance(child, str):
                try:
                    yield from query_calls(json.loads(child))
                except ValueError:
                    pass


def search_ids(db, args):
    # Reproduce stock search's source/role visibility at the retrieval layer,
    # with exhaustive pagination rather than top-K BM25 rank claims.
    options = dict(query=args['query'], role_filter=args.get('role_filter') or ['user', 'assistant'],
                   exclude_sources=['kanban', 'subagent', 'tool'], sort='recent',
                   fields=('id', 'session_id'), limit=1000)
    if isinstance(options['role_filter'], str):
        options['role_filter'] = [options['role_filter']]
    ids = set()
    for offset in range(0, 10000000, 1000):
        rows = db.search_messages(**options, offset=offset)
        ids.update(r['session_id'] for r in rows)
        if len(rows) < 1000:
            return ids
    raise RuntimeError('search result paging bound exceeded')


def projection_hash(db, sid):
    """Actual stock display paging, not an imitation SQL projection."""
    digest = hashlib.sha256()
    count = 0
    for offset in range(0, 10000000, 500):
        rows = db.get_messages(sid, include_compacted=True, include_ancestors=True, limit=500, offset=offset)
        for row in rows:
            if row.get('role') in ('user', 'assistant'):
                digest.update(canonical([row.get('role'), row.get('content'), row.get('timestamp'),
                                         row.get('display_kind'), row.get('display_metadata')]) + b'\n')
                count += 1
        if len(rows) < 500:
            return {'rows': count, 'sha256': digest.hexdigest()}
    raise RuntimeError('display paging bound exceeded')


def run(cfg):
    root = Path(cfg['test_root']).resolve()
    root.mkdir(parents=True, exist_ok=True)
    private = root / 'evidence-private'
    private.mkdir(mode=0o700, exist_ok=True)
    evidence = root / 'acceptance.json'
    results = {'status': 'running', 'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
               'python': sys.version.split()[0], 'sqlite': sqlite3.sqlite_version, 'started_at': time.time()}
    if evidence.exists():
        raise ValueError('existing acceptance receipt; use a new intended run, do not duplicate work')
    def phase(name, fn):
        start = time.monotonic()
        print(json.dumps({'phase': name, 'state': 'started'}), flush=True)
        result = fn()
        results[name] = {'seconds': round(time.monotonic() - start, 3), 'result': result}
        atomic_json(evidence, results)
        print(json.dumps({'phase': name, 'state': 'complete', 'seconds': results[name]['seconds']}), flush=True)
        return result
    source = Path(cfg['source']).resolve()
    output = root / 'trimmed.db'
    try:
        phase('synthetic', lambda: subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-q'], check=True).returncode)
        phase('source_checksum', lambda: {'sha256': file_hash(source), 'bytes': source.stat().st_size})
        with History(cfg['root'], cfg.get('agent', 'hermes')) as h:
            phase('initial_sync', lambda: h.sync(source))
            phase('incremental_sync', lambda: h.sync(source))
            phase('verification', lambda: h.verify(source))
            phase('conversation_export', h.export)
            phase('history_sizes', h.stats)
            # One conservative seal operation includes a complete pre-seal reconcile
            # and proof. Do not seal the current month or invent completion records.
            past = [r[0] for r in h.cat.execute('SELECT month FROM months ORDER BY month') if r[0] < month_of(time.time())]
            phase('sealing', lambda: [h.seal(source, month) for month in past])
            phase('trim_new_file', lambda: trim_copy(h, source, output, cfg.get('live_days', 30)))
            phase('sealed_sizes', h.stats)
            # Portable read-only stock runtime check. No gateway, provider, or real
            # credentials; HERMES_HOME and HOME are isolated before importing stock.
            os.environ['HERMES_HOME'] = str(root / 'stock-home')
            os.environ['HOME'] = str(root / 'isolated-home')
            Path(os.environ['HERMES_HOME']).mkdir(exist_ok=True)
            Path(os.environ['HOME']).mkdir(exist_ok=True)
            sys.path.insert(0, cfg['hermes_checkout'])
            import logging
            logging.basicConfig(filename=private / 'stock-runtime.log', level=logging.WARNING, force=True)
            from hermes_state import SessionDB
            from tools.session_search_tool import session_search, _parse_iso_bound
            from datetime import datetime, timezone
            checkout = Path(cfg['hermes_checkout'])
            results['hermes_revision'] = cfg['hermes_revision']
            results['hermes_snapshot_commit'] = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD'], text=True).strip()
            dirty = subprocess.check_output(['git', '-C', str(checkout), 'status', '--porcelain', '--untracked-files=no'], text=True).strip()
            if dirty:
                raise RuntimeError('stock source has tracked modifications')
            def stock_checks():
                baseline = SessionDB(db_path=source, read_only=True)
                trimmed = SessionDB(db_path=output, read_only=True)
                try:
                    with contextlib.closing(ro(source)) as src, contextlib.closing(ro(output)) as dst:
                        # Every active provider replay row remains exactly identical,
                        # including encrypted context; not just its readable projection.
                        a, b = hashlib.sha256(), hashlib.sha256()
                        counts = [0, 0]
                        for index, (c, digest) in enumerate(((src, a), (dst, b))):
                            for batch in batches(c):
                                for row in batch:
                                    if row['active']:
                                        digest.update(canonical(dict(row)) + b'\n')
                                        counts[index] += 1
                        if a.digest() != b.digest() or counts[0] != counts[1]:
                            raise AssertionError('active replay rows changed')
                        selected = src.execute("SELECT m.session_id,count(*) n FROM messages m JOIN sessions s ON s.id=m.session_id "
                            "WHERE s.source IN ('cli','api_server','webui','telegram') AND m.timestamp<? "
                            "GROUP BY m.session_id ORDER BY n DESC LIMIT 1", (time.time()-30*86400,)).fetchone()
                        if selected is None:
                            raise RuntimeError('no old human thread available')
                        before = projection_hash(baseline, selected[0])
                        after = projection_hash(trimmed, selected[0])
                        if before != after:
                            raise AssertionError('long-thread display prose changed')
                        args_by_key = {}
                        query_calls_seen = 0
                        for batch in batches(src, 'id,timestamp,tool_calls'):
                            for row in batch:
                                if row['timestamp'] < time.time() - 30*86400 or not row['tool_calls']:
                                    continue
                                try:
                                    calls = json.loads(row['tool_calls'])
                                except ValueError:
                                    continue
                                for args in query_calls(calls):
                                    query_calls_seen += 1
                                    args_by_key[canonical(args)] = args
                        atomic_json(private / 'queries.json', list(args_by_key.values()))
                        replay = []
                        for key, args in args_by_key.items():
                            tag = hashlib.sha256(key).hexdigest()
                            exact = {k: v for k, v in args.items() if k in (
                                'query', 'role_filter', 'limit', 'session_id', 'around_message_id',
                                'window', 'sort', 'detail', 'after', 'before', 'exclude_session_ids')}
                            # Never resolve another profile against a real operator HOME.
                            if args.get('profile') not in (None, '', 'default'):
                                replay.append(dict(query_sha256=tag, misses=0, skipped_profile=True))
                                continue
                            for field in ('after', 'before'):
                                if exact.get(field):
                                    ts = _parse_iso_bound(exact[field])
                                    exact[field] = datetime.fromtimestamp(ts, timezone.utc).isoformat()
                            prior_tool = json.loads(session_search(db=baseline, **exact))
                            after_tool = json.loads(session_search(db=trimmed, **exact))
                            if prior_tool.get('success') is False or after_tool.get('success') is False:
                                replay.append(dict(query_sha256=tag, misses=0, tool_error=True))
                                continue
                            old = {r['session_id'] for r in prior_tool.get('results', [])}
                            live = {r['session_id'] for r in after_tool.get('results', [])}
                            missing = old - live
                            recovered = set()
                            archive_error = False
                            if missing:
                                # Deep is used only for tool-output discoveries;
                                # stock default user/assistant content stays live.
                                try:
                                    recovered = {x['session_id'] for x in h.search(args['query'], deep=True, limit=1000)}
                                except ValueError:
                                    archive_error = True
                            replay.append(dict(query_sha256=tag, baseline_sessions=len(old), trimmed_sessions=len(live),
                                               live_misses=len(missing), recovered=len(missing & recovered),
                                               misses=len(missing-recovered), archive_query_error=archive_error,
                                               additional_live_sessions=len(live-old)))
                        atomic_json(private / 'replay.json', replay)
                        return dict(active_rows=counts[0], active_sha256=a.hexdigest(), long_thread_prose=before,
                                    query_calls=query_calls_seen, unique_queries=len(replay),
                                    queries_with_misses=sum(r['misses'] > 0 for r in replay),
                                    total_session_misses=sum(r['misses'] for r in replay),
                                    additional_live_sessions=sum(r.get('additional_live_sessions', 0) for r in replay),
                                    skipped_profiles=sum(bool(r.get('skipped_profile')) for r in replay),
                                    tool_errors=sum(bool(r.get('tool_error')) for r in replay),
                                    replay_scope='actual stock session_search arguments, frozen relative bounds, exact top-K session sets plus History recovery; other profiles explicitly skipped')
                finally:
                    baseline.close()
                    trimmed.close()
            phase('stock_open_display_replay', stock_checks)
        # A new-file rebuild already compacts. Time VACUUM separately on the test
        # artifact only, then repeat index consistency (does not affect live state).
        def vacuum():
            with contextlib.closing(sqlite3.connect(output)) as c, c:
                c.execute('VACUUM')
                if c.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise AssertionError('post-vacuum integrity')
                names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE sql LIKE 'CREATE VIRTUAL TABLE%'")]
                for name in names:
                    if not name.startswith('messages_fts'):
                        raise ValueError('unexpected FTS table')
                    c.execute(f"INSERT INTO {name}({name},rank) VALUES ('integrity-check',1)")
            digest = file_hash(output)
            with contextlib.closing(sqlite3.connect(output.with_name(output.name + '.trim-ledger.db'))) as ledger, ledger:
                ledger.execute("UPDATE meta SET value=? WHERE key='output_sha256'", (digest,))
            return {'bytes': output.stat().st_size, 'fts_indexes': len(names), 'sha256': digest}
        phase('vacuum_and_integrity', vacuum)
        replay_result = results['stock_open_display_replay']['result']
        results['status'] = 'passed' if not (replay_result['queries_with_misses'] or replay_result['tool_errors'] or replay_result['skipped_profiles']) else 'replay_gaps'
        results['finished_at'] = time.time()
        atomic_json(evidence, results)
        print(json.dumps({'status': results['status'], 'receipt': str(evidence)}), flush=True)
    except BaseException as e:
        results['status'] = 'failed'
        results['error_type'] = type(e).__name__
        # Traceback is private because dependency messages may mention owner IDs.
        (private / 'failure.txt').write_text(traceback.format_exc())
        atomic_json(evidence, results)
        print(json.dumps({'status': 'failed', 'phase_error_type': type(e).__name__}), flush=True)
        raise SystemExit(1)


if __name__ == '__main__':
    os.umask(0o077)
    p = argparse.ArgumentParser()
    p.add_argument('--config', required=True)
    args = p.parse_args()
    run(json.loads(Path(args.config).read_text()))
