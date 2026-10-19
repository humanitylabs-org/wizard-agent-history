"""Version-one storage. Only this module knows the portable history schema."""
from __future__ import annotations

import base64
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from compression import zstd

VERSION = 1
BATCH = 400
HUMAN_SOURCES = {'webui', 'api_server', 'telegram', 'cli'}
PAYLOAD_FIELDS = ('reasoning', 'reasoning_content', 'reasoning_details',
                  'codex_reasoning_items', 'codex_message_items', 'api_content', 'tool_calls')


def json_default(x):
    if isinstance(x, bytes):
        return {'$bytes': base64.b64encode(x).decode('ascii')}
    raise TypeError(type(x).__name__)


def canonical(x):
    return json.dumps(x, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      default=json_default).encode('utf-8')


def unjson(data):
    def hook(x):
        if set(x) == {'$bytes'}:
            return base64.b64decode(x['$bytes'])
        return x
    return json.loads(data, object_hook=hook)


def strip_opaque(value, signature=False):
    if isinstance(value, dict):
        return {k: strip_opaque(v, signature) for k, v in value.items()
                if k != 'encrypted_content' and not (signature and k == 'signature')}
    if isinstance(value, list):
        return [strip_opaque(x, signature) for x in value]
    return value


def sanitize(row):
    row = dict(row)
    for field in PAYLOAD_FIELDS:
        value = row.get(field)
        if not isinstance(value, str) or not value:
            continue
        try:
            obj = json.loads(value)
        except ValueError:
            # Unknown opaque formats must fail closed, not leak an encrypted blob.
            if field in ('codex_reasoning_items', 'reasoning_details') and (
                    'encrypted_content' in value or '"signature"' in value):
                raise ValueError('unparseable provider metadata; manual format review required')
            continue
        cleaned = strip_opaque(obj, signature=field == 'reasoning_details')
        if cleaned != obj:
            row[field] = canonical(cleaned).decode()
    return row


def row_hash(row):
    # Flags are mutable visibility state, separately reconciled; all other columns
    # (including IDs, UIDs and display identities) are bound to the content proof.
    return hashlib.sha256(canonical({k: v for k, v in row.items()
                                    if k not in ('active', 'compacted')})).hexdigest()


def accumulate(proofs, row, digest):
    sid = row['session_id']
    count, h = proofs.setdefault(sid, [0, hashlib.sha256()])
    proofs[sid][0] = count + 1
    h.update(str(row['id']).encode() + b':' + digest.encode() + b'\n')


def finished(proofs):
    return {sid: {'count': count, 'sha256': h.hexdigest()} for sid, (count, h) in proofs.items()}


def ro(path):
    c = sqlite3.connect(Path(path).expanduser().resolve().as_uri() + '?mode=ro',
                        uri=True, timeout=0.5, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA query_only=ON')
    c.execute('PRAGMA busy_timeout=500')
    return c


def batches(c, columns='*', high=None):
    """No yield with a live cursor: each fetch completes its short read snapshot."""
    if high is None:
        high = c.execute('SELECT coalesce(max(id),0) FROM messages').fetchone()[0]
    last = 0
    while last < high:
        rows = c.execute(f'SELECT {columns} FROM messages WHERE id>? AND id<=? '
                         'ORDER BY id LIMIT ?', (last, high, BATCH)).fetchall()
        if not rows:
            break
        last = rows[-1]['id']
        yield rows


def month_of(ts):
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime('%Y-%m')


def texts(row):
    conv = (row.get('content') or '') if row['role'] in ('user', 'assistant') else ''
    deep = '\n'.join(str(row.get(k) or '') for k in PAYLOAD_FIELDS)
    if row['role'] == 'tool':
        deep += '\n' + (row.get('content') or '')
    deep += '\n' + (row.get('tool_name') or '')
    return conv, deep


def pack(row):
    raw = canonical(row)
    compressed = zstd.compress(raw, level=3) if len(raw) >= 512 else raw
    return (compressed, 'zstd', len(raw)) if len(compressed) < len(raw) else (raw, 'raw', len(raw))


def unpack(row):
    return unjson(zstd.decompress(row['payload']) if row['codec'] == 'zstd' else row['payload'])


CATALOG = '''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,payload BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS system_prompts(hash TEXT PRIMARY KEY,prompt TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS months(month TEXT PRIMARY KEY,sealed INTEGER NOT NULL DEFAULT 0,
 sha256 TEXT,verified_at REAL);
CREATE TABLE IF NOT EXISTS inventory(id INTEGER PRIMARY KEY,month TEXT NOT NULL,
 session_id TEXT NOT NULL,digest TEXT NOT NULL,active INTEGER,compacted INTEGER,
 seen INTEGER NOT NULL,deleted INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS inventory_session ON inventory(session_id,id);
CREATE TABLE IF NOT EXISTS proofs(session_id TEXT PRIMARY KEY,row_count INTEGER NOT NULL,
 content_hash TEXT NOT NULL,verified_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS retired(id INTEGER PRIMARY KEY,expected_digest TEXT);
'''
MONTH = '''
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,session_id TEXT NOT NULL,
 timestamp REAL NOT NULL,role TEXT NOT NULL,digest TEXT NOT NULL,
 payload BLOB NOT NULL,codec TEXT NOT NULL,raw_bytes INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS session_rows ON messages(session_id,id);
CREATE VIRTUAL TABLE IF NOT EXISTS conversation USING fts5(text,content='');
CREATE VIRTUAL TABLE IF NOT EXISTS deep USING fts5(text,content='');
'''


class History:
    """One trusted reader/source identity per instance. No cross-agent MCP grants."""
    def __init__(self, root, agent='hermes', writable=True):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', agent):
            raise ValueError('invalid agent id')
        self.agent = agent
        self.dir = Path(root).expanduser().resolve() / agent
        self.writable = writable
        self.opened = {}
        if writable:
            self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.lockfile = open(self.dir / '.writer.lock', 'a')
            import fcntl
            fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.cat = sqlite3.connect(self.dir / 'catalog.db')
            self.cat.row_factory = sqlite3.Row
            self.cat.executescript(CATALOG)
            version = self.get('version')
            if version and int(version) != VERSION:
                raise ValueError('unsupported catalog version')
            self.set('version', VERSION)
            self.cat.commit()
        else:
            self.cat = ro(self.dir / 'catalog.db')

    def close(self):
        for c in self.opened.values():
            c.close()
        self.cat.close()
        if self.writable:
            self.lockfile.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def get(self, key, default=None):
        row = self.cat.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    def set(self, key, value):
        self.cat.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, str(value)))

    def is_sealed(self, month):
        r = self.cat.execute('SELECT sealed FROM months WHERE month=?', (month,)).fetchone()
        return bool(r and r[0])

    def db(self, month, write=False):
        key = (month, write)
        if key not in self.opened:
            path = self.dir / (month + '.db')
            if write:
                if not self.writable or self.is_sealed(month):
                    raise ValueError('sealed month is immutable')
                c = sqlite3.connect(path)
                c.row_factory = sqlite3.Row
                c.executescript(MONTH)
                self.cat.execute('INSERT OR IGNORE INTO months(month) VALUES (?)', (month,))
            else:
                c = ro(path)
            self.opened[key] = c
        return self.opened[key]

    def stored(self, inv):
        row = self.db(inv['month']).execute('SELECT * FROM messages WHERE id=?', (inv['id'],)).fetchone()
        if row is None:
            raise ValueError('history row missing')
        data = unpack(row)
        if row_hash(data) != inv['digest'] or row['digest'] != inv['digest']:
            raise ValueError('history content hash mismatch')
        data.update(active=inv['active'], compacted=inv['compacted'])
        return data

    def erase(self, inv):
        if self.is_sealed(inv['month']):
            # Logical deletion immediately hides sealed data. Physical deletion
            # requires an explicit owner-approved reseal + encrypted-backup policy.
            return
        c = self.db(inv['month'], True)
        old = c.execute('SELECT * FROM messages WHERE id=?', (inv['id'],)).fetchone()
        if old:
            for table, text in zip(('conversation', 'deep'), texts(unpack(old))):
                c.execute(f'INSERT INTO {table}({table},rowid,text) VALUES (\'delete\',?,?)',
                          (inv['id'], text))
            c.execute('DELETE FROM messages WHERE id=?', (inv['id'],))

    def put(self, row, generation):
        row = sanitize(row)
        digest = row_hash(row)
        old = self.cat.execute('SELECT * FROM inventory WHERE id=?', (row['id'],)).fetchone()
        retired = self.cat.execute('SELECT expected_digest FROM retired WHERE id=?', (row['id'],)).fetchone()
        if retired:
            if retired[0] != digest:
                raise ValueError('retired row changed; explicit reconciliation required')
            self.cat.execute('UPDATE inventory SET seen=?,active=?,compacted=? WHERE id=?',
                             (generation, row.get('active', 1), row.get('compacted', 0), row['id']))
            return False
        month = month_of(row['timestamp'])
        changed = not old or old['digest'] != digest or old['deleted']
        if changed:
            if self.is_sealed(month) or (old and self.is_sealed(old['month'])):
                raise ValueError('late insert/edit in sealed month; explicit reseal required')
            if old and old['month'] != month:
                self.erase(old)
            c = self.db(month, True)
            # A previous crash may have committed payloads but not inventory.
            # Delete by actual month-local bytes before replacing its FTS entries.
            existing = c.execute('SELECT * FROM messages WHERE id=?', (row['id'],)).fetchone()
            if existing:
                for table, text in zip(('conversation', 'deep'), texts(unpack(existing))):
                    c.execute(f"INSERT INTO {table}({table},rowid,text) VALUES ('delete',?,?)",
                              (row['id'], text))
            payload, codec, raw_bytes = pack(row)
            c.execute('INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?)',
                      (row['id'], row['session_id'], row['timestamp'], row['role'], digest,
                       payload, codec, raw_bytes))
            for table, text in zip(('conversation', 'deep'), texts(row)):
                c.execute(f'INSERT INTO {table}(rowid,text) VALUES (?,?)', (row['id'], text))
        self.cat.execute('INSERT OR REPLACE INTO inventory VALUES (?,?,?,?,?,?,?,0)',
                         (row['id'], month, row['session_id'], digest, row.get('active', 1),
                          row.get('compacted', 0), generation))
        return changed

    def commit(self):
        # Payload before manifest: an interruption is retried idempotently using
        # month-local stored bytes, never trusted solely from the watermark.
        for (month, write), c in self.opened.items():
            if write:
                c.commit()
        self.cat.commit()

    def sync(self, source, reconcile=False):
        start = time.monotonic()
        with contextlib.closing(ro(source)) as src:
            # Pin source identity to SQLite schema + explicit path; replacing the
            # source is only admitted through accept-trim, not a guessed watermark.
            source_path = str(Path(source).expanduser().resolve())
            if self.get('source') not in (None, source_path):
                raise ValueError('catalog belongs to a different source')
            self.set('source', source_path)
            st = Path(source_path).stat()
            identity = f'{st.st_dev}:{st.st_ino}'
            if self.get('source_identity') not in (None, identity):
                raise ValueError('source file replaced; explicit rebind required')
            self.set('source_identity', identity)
            generation = int(self.get('generation', 0)) + 1
            self.set('generation', generation)
            self.cat.commit()  # Never reuse a partially completed generation.
            high = src.execute('SELECT coalesce(max(id),0) FROM messages').fetchone()[0]
            watermark = int(self.get('watermark', 0))
            self.cat.execute('DELETE FROM proofs')
            self.cat.execute('DELETE FROM sessions')
            # Metadata is small and is refreshed even when there are no new messages.
            for s in src.execute('SELECT * FROM sessions').fetchall():
                self.cat.execute('INSERT INTO sessions VALUES (?,?)', (s['id'], canonical(dict(s))))
            if src.execute("SELECT 1 FROM sqlite_master WHERE name='system_prompts'").fetchone():
                for p in src.execute('SELECT hash,prompt FROM system_prompts').fetchall():
                    self.cat.execute('INSERT OR REPLACE INTO system_prompts VALUES (?,?)', tuple(p))
            changed = scanned = 0
            # Every pass scans IDs/flags only on old rows. A full payload pass is
            # mandatory before sealing and verification; no long live read txn.
            for batch in batches(src, 'id,active,compacted', high):
                ids = [r['id'] for r in batch]
                wanted = ids if reconcile else [i for i in ids if i > watermark]
                if wanted:
                    marks = ','.join('?' for _ in wanted)
                    full = src.execute(f'SELECT * FROM messages WHERE id IN ({marks}) ORDER BY id', wanted).fetchall()
                    for row in full:
                        changed += self.put(row, generation)
                self.cat.executemany('UPDATE inventory SET active=?,compacted=?,seen=? WHERE id=?',
                                     [(r['active'], r['compacted'], generation, r['id']) for r in batch])
                scanned += len(batch)
                self.commit()
            # Missing rows are owner deletions, except an explicitly accepted trim.
            last = 0
            deleted = 0
            while True:
                missing = self.cat.execute('SELECT * FROM inventory WHERE id>? AND id<=? AND seen<>? '
                    'AND deleted=0 AND id NOT IN (SELECT id FROM retired) ORDER BY id LIMIT ?',
                    (last, max(high, watermark), generation, BATCH)).fetchall()
                if not missing:
                    break
                for inv in missing:
                    self.erase(inv)
                    self.cat.execute('UPDATE inventory SET deleted=1 WHERE id=?', (inv['id'],))
                    deleted += 1
                last = missing[-1]['id']
                self.commit()
            self.set('watermark', max(high, watermark))
            self.set('generation', generation)
            self.set('reconciled', int(reconcile))
            self.commit()
        return dict(scanned=scanned, changed=changed, deleted=deleted,
                    seconds=round(time.monotonic() - start, 3))

    def verify(self, source):
        start = time.monotonic()
        proofs = {}
        with contextlib.closing(ro(source)) as src:
            for batch in batches(src):
                for raw in batch:
                    row = sanitize(raw)
                    inv = self.cat.execute('SELECT * FROM inventory WHERE id=? AND deleted=0', (row['id'],)).fetchone()
                    if inv is None:
                        raise ValueError('source row has no History copy')
                    saved = self.stored(inv)
                    retired = self.cat.execute('SELECT expected_digest FROM retired WHERE id=?', (row['id'],)).fetchone()
                    expected = retired[0] if retired else inv['digest']
                    if row_hash(row) != expected:
                        raise ValueError('source content differs from History')
                    if (row['active'], row['compacted']) != (saved['active'], saved['compacted']):
                        raise ValueError('source flags changed; sync again')
                    accumulate(proofs, row, row_hash(row))
            count = sum(p[0] for p in proofs.values())
            expected = self.cat.execute('SELECT count(*) FROM inventory WHERE deleted=0 '
                       'AND id NOT IN (SELECT id FROM retired WHERE expected_digest IS NULL)').fetchone()[0]
            if count != expected:
                raise ValueError('row count mismatch; reconcile deletions before verification')
        result = finished(proofs)
        self.cat.execute('DELETE FROM proofs')
        self.cat.executemany('INSERT INTO proofs VALUES (?,?,?,?)',
                             [(sid, p['count'], p['sha256'], time.time()) for sid, p in result.items()])
        self.cat.commit()
        return dict(rows=count, sessions=len(result), seconds=round(time.monotonic() - start, 3))

    def seal(self, source, month):
        if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', month) or month >= month_of(time.time()):
            raise ValueError('only past calendar months can be sealed')
        if self.is_sealed(month):
            return {'month': month, 'already_sealed': True}
        self.sync(source, reconcile=True)
        self.verify(source)
        c = self.db(month, True)
        c.execute("INSERT INTO conversation(conversation) VALUES ('integrity-check')")
        c.execute("INSERT INTO deep(deep) VALUES ('integrity-check')")
        c.commit()
        c.execute('VACUUM')
        if c.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('month integrity failed')
        c.close()
        del self.opened[(month, True)]
        digest = file_hash(self.dir / (month + '.db'))
        self.cat.execute('UPDATE months SET sealed=1,sha256=?,verified_at=? WHERE month=?',
                         (digest, time.time(), month))
        self.cat.commit()
        return {'month': month, 'sha256': digest}

    def search(self, query, deep=False, after=None, before=None, limit=20):
        if not isinstance(query, str) or not query.strip():
            return []
        limit = max(1, min(int(limit), 1000))
        after, before = bound(after), bound(before)
        hits = []
        for m in self.cat.execute('SELECT month FROM months ORDER BY month DESC').fetchall():
            month = m[0]
            c = self.db(month)
            ids = set()
            # Match query is FTS5 syntax, not SQL. Parameterization is mandatory.
            for table in (('conversation', 'deep') if deep else ('conversation',)):
                try:
                    ids.update(r[0] for r in c.execute(
                        f'SELECT rowid FROM {table} WHERE {table} MATCH ?', (query,)).fetchall())
                except sqlite3.OperationalError as e:
                    raise ValueError('invalid FTS query') from e
            # Collapse to one newest message per session, globally after all months.
            for id in ids:
                inv = self.cat.execute('SELECT * FROM inventory WHERE id=? AND deleted=0 '
                                       'AND (active=1 OR compacted=1)', (id,)).fetchone()
                if inv is None:
                    continue
                r = c.execute('SELECT timestamp,session_id FROM messages WHERE id=?', (id,)).fetchone()
                if after is not None and r['timestamp'] < after:
                    continue
                if before is not None and r['timestamp'] >= before:
                    continue
                hits.append((r['timestamp'], id, month, r['session_id']))
        result, seen = [], set()
        for ts, id, month, sid in sorted(hits, reverse=True):
            if sid in seen:
                continue
            seen.add(sid)
            inv = self.cat.execute('SELECT * FROM inventory WHERE id=?', (id,)).fetchone()
            row = self.stored(inv)
            s = self.cat.execute('SELECT payload FROM sessions WHERE id=?', (sid,)).fetchone()
            meta = unjson(s[0]) if s else {}
            text = '\n'.join(texts(row)) if deep else texts(row)[0]
            # Snippet generation has no second uncompressed canonical text copy.
            needle = re.sub(r'[^\w ]', '', query).split()
            pos = next((text.lower().find(n.lower()) for n in needle if n.lower() in text.lower()), 0)
            result.append(dict(agent=self.agent, month=month, session_id=sid,
                title=meta.get('title'), message_id=id, timestamp=ts,
                snippet=text[max(0, pos - 100):max(0, pos - 100) + 600]))
            if len(result) >= limit:
                break
        return result

    def read(self, session_id, after_id=0, limit=100):
        limit = max(1, min(int(limit), 500))
        invs = self.cat.execute('SELECT * FROM inventory WHERE session_id=? AND id>? AND deleted=0 '
                               'ORDER BY id LIMIT ?', (session_id, int(after_id), limit + 1)).fetchall()
        rows = []
        for inv in invs[:limit]:
            row = self.stored(inv)
            rows.append(dict(agent=self.agent, month=inv['month'], **row))
        return {'messages': rows, 'has_more': len(invs) > limit,
                'next_after_id': rows[-1]['id'] if rows else after_id}

    def export(self, output=None):
        output = Path(output) if output else self.dir / 'conversation.jsonl'
        temp = output.with_name(output.name + '.partial')
        count, seen = 0, set()
        sessions = {r['id']: unjson(r['payload']) for r in self.cat.execute('SELECT * FROM sessions')}
        with open(temp, 'xb') as f:
            for month, in self.cat.execute('SELECT month FROM months ORDER BY month').fetchall():
                c = self.db(month)
                last = 0
                while True:
                    rows = c.execute("SELECT * FROM messages WHERE id>? AND role IN ('user','assistant') ORDER BY id LIMIT ?",
                                     (last, BATCH)).fetchall()
                    if not rows:
                        break
                    last = rows[-1]['id']
                    for stored in rows:
                        inv = self.cat.execute('SELECT * FROM inventory WHERE id=? AND deleted=0 '
                                               'AND (active=1 OR compacted=1)', (stored['id'],)).fetchone()
                        if not inv:
                            continue
                        row = unpack(stored)
                        meta = sessions.get(row['session_id'], {})
                        if meta.get('source') not in HUMAN_SOURCES or meta.get('parent_session_id') and meta.get('source') == 'subagent':
                            continue
                        text = row.get('content')
                        if not text or row.get('_compressed_summary') or text.lstrip().startswith(('[CONTEXT COMPACTION', '[CONTEXT SUMMARY]:')):
                            continue
                        if row.get('display_kind') in ('internal', 'machine', 'system'):
                            continue
                        if row['role'] == 'assistant' and row.get('tool_calls') not in (None, '', '[]', 'null'):
                            continue
                        identity = row.get('message_uid') or row.get('display_identity') or hashlib.sha256(
                            canonical([row['role'], row['timestamp'], text])).hexdigest()
                        key = (row['session_id'], identity)
                        if key in seen:
                            continue
                        seen.add(key)
                        f.write(canonical(dict(agent=self.agent, month=month, session_id=row['session_id'],
                            message_id=row['id'], timestamp=row['timestamp'], role=row['role'], content=text)) + b'\n')
                        count += 1
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, output)
        return {'rows': count, 'bytes': output.stat().st_size}

    def stats(self):
        months = []
        for m in self.cat.execute('SELECT * FROM months ORDER BY month').fetchall():
            c = self.db(m['month'])
            row = c.execute('SELECT count(*),coalesce(sum(raw_bytes),0),coalesce(sum(length(payload)),0) FROM messages').fetchone()
            months.append(dict(month=m['month'], sealed=bool(m['sealed']), rows=row[0],
                               raw_bytes=row[1], payload_bytes=row[2], bytes=(self.dir / (m['month'] + '.db')).stat().st_size))
        return dict(months=months, rows=sum(m['rows'] for m in months),
                    sessions=self.cat.execute('SELECT count(*) FROM sessions').fetchone()[0],
                    total_bytes=sum(p.stat().st_size for p in self.dir.iterdir() if p.is_file()))


def bound(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def file_hash(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()
