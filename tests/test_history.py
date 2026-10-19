"""All fixtures are synthetic. No owner state is read by the test suite."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from agent_history.store import History, canonical, file_hash, month_of, ro, sanitize, row_hash, unpack
from agent_history.trim import accept_trim, guard_copy, trim_copy

OLD = 1577836800.0  # synthetic January 2020
SCHEMA = '''
CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT, started_at REAL, ended_at REAL);
CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
 role TEXT, content TEXT, timestamp REAL, active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0,
 tool_calls TEXT, tool_name TEXT, reasoning TEXT, reasoning_content TEXT, reasoning_details TEXT,
 codex_reasoning_items TEXT, codex_message_items TEXT, api_content TEXT, display_metadata TEXT,
 display_identity BLOB, display_order INTEGER, message_uid TEXT, _compressed_summary INTEGER DEFAULT 0);
CREATE TABLE state_meta(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE system_prompts(hash TEXT PRIMARY KEY,prompt TEXT);
CREATE VIEW messages_fts_src AS SELECT id,content,tool_name,tool_calls FROM messages;
CREATE VIRTUAL TABLE messages_fts USING fts5(content,tool_name,tool_calls,content='messages_fts_src',content_rowid='id');
CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
 INSERT INTO messages_fts(rowid,content,tool_name,tool_calls) VALUES(new.id,new.content,new.tool_name,new.tool_calls); END;
CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
 INSERT INTO messages_fts(messages_fts,rowid,content,tool_name,tool_calls) VALUES('delete',old.id,old.content,old.tool_name,old.tool_calls); END;
CREATE TRIGGER messages_fts_update AFTER UPDATE OF content,tool_name,tool_calls ON messages BEGIN
 INSERT INTO messages_fts(messages_fts,rowid,content,tool_name,tool_calls) VALUES('delete',old.id,old.content,old.tool_name,old.tool_calls);
 INSERT INTO messages_fts(rowid,content,tool_name,tool_calls) VALUES(new.id,new.content,new.tool_name,new.tool_calls); END;
CREATE VIEW messages_fts_trigram_src AS SELECT id,content FROM messages WHERE role<>'tool';
CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(content,content='messages_fts_trigram_src',content_rowid='id',tokenize='trigram');
'''


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.source = self.base / 'synthetic.db'
        self.conn = sqlite3.connect(self.source)
        self.conn.executescript(SCHEMA)
        self.conn.execute('INSERT INTO sessions VALUES (?,?,?,?,?)', ('a', 'cli', 'Synthetic garden', OLD, None))
        self.conn.execute('INSERT INTO sessions VALUES (?,?,?,?,?)', ('b', 'subagent', 'Synthetic helper', OLD, None))
        self.conn.commit()
        self.h = History(self.base / 'history')

    def tearDown(self):
        self.h.close()
        self.conn.close()
        self.tmp.cleanup()

    def add(self, role='user', content='Synthetic violet garden', **kwargs):
        data = dict(session_id='a', role=role, content=content, timestamp=OLD,
                    active=1, compacted=0)
        data.update(kwargs)
        cur = self.conn.execute(f'INSERT INTO messages({",".join(data)}) VALUES ({",".join("?" for _ in data)})', tuple(data.values()))
        self.conn.commit()
        return cur.lastrowid

    def sync(self):
        return self.h.sync(self.source)

    def verified(self):
        self.h.sync(self.source, reconcile=True)
        return self.h.verify(self.source)

    def test_readonly_source(self):
        self.add()
        before = file_hash(self.source)
        self.sync()
        self.assertEqual(before, file_hash(self.source))
        c = ro(self.source)
        with self.assertRaises(sqlite3.OperationalError):
            c.execute('DELETE FROM messages')
        c.close()

    def test_incremental_and_metadata(self):
        self.add()
        self.assertEqual(self.sync()['changed'], 1)
        self.assertEqual(self.sync()['changed'], 0)
        self.conn.execute("UPDATE sessions SET title='Renamed synthetic title'")
        self.conn.commit()
        self.add(role='assistant', content='Tulips grow')
        self.assertEqual(self.sync()['changed'], 1)
        self.assertEqual(self.h.search('violet')[0]['title'], 'Renamed synthetic title')

    def test_mutable_flags(self):
        i = self.add()
        self.sync()
        self.conn.execute('UPDATE messages SET active=0,compacted=1 WHERE id=?', (i,))
        self.conn.commit()
        self.sync()
        self.assertEqual(self.h.read('a')['messages'][0]['active'], 0)
        self.assertEqual(len(self.h.search('violet')), 1)

    def test_privacy_deletion_even_highest_id(self):
        i = self.add()
        self.sync()
        self.conn.execute('DELETE FROM messages WHERE id=?', (i,))
        self.conn.commit()
        self.assertEqual(self.sync()['deleted'], 1)
        self.assertEqual(self.h.search('violet'), [])
        self.assertEqual(self.h.read('a')['messages'], [])
        self.assertEqual(self.h.db('2020-01').execute('SELECT count(*) FROM messages').fetchone()[0], 0)

    def test_edits_full_reconciliation(self):
        i = self.add()
        self.sync()
        self.conn.execute("UPDATE messages SET content='Synthetic changed cactus' WHERE id=?", (i,))
        self.conn.commit()
        with self.assertRaises(ValueError):
            self.h.verify(self.source)
        self.verified()
        self.assertEqual(self.h.search('violet'), [])
        self.assertEqual(len(self.h.search('cactus')), 1)

    def test_opaque_exclusion_readable_retained(self):
        self.add(role='assistant', codex_reasoning_items=json.dumps([{'encrypted_content': 'SYNTHETICOPAQUE', 'summary': [{'text': 'Readable analysis'}]}]),
                 reasoning_details=json.dumps([{'text': 'Readable details', 'signature': 'SYNTHETICSIGNATURE'}]))
        self.sync()
        raw = canonical(self.h.read('a')).decode()
        self.assertNotIn('SYNTHETICOPAQUE', raw)
        self.assertNotIn('SYNTHETICSIGNATURE', raw)
        self.assertIn('Readable analysis', raw)
        self.assertEqual(len(self.h.search('analysis', deep=True)), 1)
        self.assertEqual(self.h.search('analysis'), [])

    def test_compression_bytes(self):
        self.add(content='Synthetic repetition ' * 1000, display_identity=b'\x00\xff')
        self.sync()
        stats = self.h.stats()['months'][0]
        self.assertLess(stats['payload_bytes'], stats['raw_bytes'])
        self.assertEqual(self.h.read('a')['messages'][0]['display_identity'], b'\x00\xff')

    def test_multi_month_time_filter(self):
        self.add()
        self.add(session_id='b', timestamp=1580515200)
        self.sync()
        self.assertEqual(len(self.h.search('violet')), 2)
        self.assertEqual(len(self.h.search('violet', after='2020-02-01')), 1)
        self.assertEqual(len(self.h.search('violet', before='2020-02-01')), 1)

    def test_pagination(self):
        for _ in range(4):
            self.add()
        self.sync()
        first = self.h.read('a', limit=2)
        self.assertTrue(first['has_more'])
        second = self.h.read('a', first['next_after_id'], 2)
        self.assertFalse(second['has_more'])
        self.assertEqual(len(second['messages']), 2)

    def test_seal_deletion_and_flags(self):
        i = self.add()
        self.add(content='Synthetic retained fern')
        self.sync()
        result = self.h.seal(self.source, '2020-01')
        digest = result['sha256']
        self.conn.execute('DELETE FROM messages WHERE id=?', (i,))
        self.conn.execute('UPDATE messages SET active=0,compacted=1')
        self.conn.commit()
        self.sync()
        self.assertEqual(self.h.search('violet'), [])
        self.assertEqual(self.h.read('a')['messages'][0]['compacted'], 1)
        self.assertEqual(file_hash(self.h.dir / '2020-01.db'), digest)

    def test_seal_reconciles_before_immutability(self):
        self.add()
        self.add(content='Synthetic deleted orchid')
        self.sync()
        self.conn.execute('DELETE FROM messages WHERE id=2')
        self.conn.commit()
        self.h.seal(self.source, '2020-01')
        self.assertEqual(self.h.db('2020-01').execute('SELECT count(*) FROM messages').fetchone()[0], 1)
        with self.assertRaises(ValueError):
            self.h.seal(self.source, month_of(time.time()))

    def test_refuse_sealed_late_edit(self):
        self.add()
        self.sync()
        self.h.seal(self.source, '2020-01')
        self.conn.execute("UPDATE messages SET content='Synthetic changed'")
        self.conn.commit()
        with self.assertRaises(ValueError):
            self.h.sync(self.source, reconcile=True)

    def test_export_human_final_only_and_dedupe(self):
        self.add(message_uid='synthetic-user', _compressed_summary=0)
        self.add(message_uid='synthetic-user', active=0, compacted=1)
        self.add(role='assistant', content='Synthetic final answer')
        self.add(role='assistant', content='Synthetic commentary', tool_calls='[{"name":"fixture"}]')
        self.add(session_id='b', content='Synthetic helper message')
        self.add(content='Synthetic summary', _compressed_summary=1)
        self.sync()
        self.assertEqual(self.h.export()['rows'], 2)
        rows = [json.loads(line) for line in (self.h.dir / 'conversation.jsonl').read_text().splitlines()]
        self.assertEqual([r['role'] for r in rows], ['user', 'assistant'])

    def test_trim_rules_and_fts(self):
        active = self.add(role='tool', content='Synthetic active tool', reasoning='keep')
        self.add(role='tool', content='Synthetic archived tool', active=0, compacted=1)
        prose = self.add(role='assistant', content='Synthetic old prose', active=0, compacted=1,
                         reasoning='Preserved in history', tool_calls='[{"name":"fixture"}]', display_identity=b'abc', display_order=3)
        recent = self.add(role='tool', content='Synthetic recent tool', active=0, compacted=1, timestamp=time.time())
        self.verified()
        output = self.base / 'trimmed.db'
        result = trim_copy(self.h, self.source, output)
        self.assertEqual(result['dropped_rows'], 1)
        self.assertEqual(result['stripped_rows'], 1)
        c = ro(output)
        self.assertEqual(c.execute('SELECT reasoning FROM messages WHERE id=?', (active,)).fetchone()[0], 'keep')
        self.assertIsNone(c.execute('SELECT reasoning FROM messages WHERE id=?', (prose,)).fetchone()[0])
        self.assertEqual(c.execute('SELECT display_identity FROM messages WHERE id=?', (prose,)).fetchone()[0], b'abc')
        self.assertTrue(c.execute('SELECT 1 FROM messages WHERE id=?', (recent,)).fetchone())
        self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(c.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'prose'").fetchone()[0], 1)
        c.close()

    def test_refuse_without_proof(self):
        self.add()
        self.sync()
        with self.assertRaises(ValueError):
            trim_copy(self.h, self.source, self.base / 'trimmed.db')

    def test_refuse_stale_source_proof(self):
        self.add(active=0, compacted=1, reasoning='Synthetic old')
        self.verified()
        self.add(content='Synthetic new unverified row')
        output = self.base / 'trimmed.db'
        with self.assertRaises(ValueError):
            trim_copy(self.h, self.source, output)
        self.assertFalse(output.exists())

    def test_refuse_corrupt_archived_payload(self):
        self.add(active=0, compacted=1, reasoning='Synthetic old')
        self.verified()
        c = self.h.db('2020-01', True)
        c.execute("UPDATE messages SET payload=?,codec='raw'", (b'{}',))
        c.commit()
        with self.assertRaises((ValueError, KeyError)):
            trim_copy(self.h, self.source, self.base / 'trimmed.db')

    def test_accept_trim_does_not_propagate_intentional_deletes(self):
        self.add(role='tool', active=0, compacted=1, content='Synthetic toolretained')
        self.add(role='assistant', active=0, compacted=1, reasoning='Synthetic reasonretained')
        self.add()
        self.verified()
        output = self.base / 'trimmed.db'
        result = trim_copy(self.h, self.source, output)
        accept_trim(self.h, output, result['ledger'])
        self.h.sync(output, reconcile=True)
        self.assertEqual(len(self.h.search('toolretained', deep=True)), 1)
        self.assertEqual(len(self.h.search('reasonretained', deep=True)), 1)
        self.h.verify(output)

    def test_live_guard_and_existing_output(self):
        with self.assertRaises(ValueError):
            guard_copy(Path.home() / '.hermes/state.db', self.base / 'out.db')
        with self.assertRaises(ValueError):
            guard_copy(self.source, self.source)

    def test_crash_between_payload_and_catalog(self):
        self.add()
        self.sync()
        # Simulate payload commit surviving a lost catalog transaction.
        self.h.cat.execute('DELETE FROM inventory')
        self.h.set('watermark', 0)
        self.h.cat.commit()
        self.sync()
        c = self.h.db('2020-01', True)
        c.execute("INSERT INTO conversation(conversation) VALUES ('integrity-check')")
        self.assertEqual(len(self.h.search('violet')), 1)

    def test_mcp_stdio(self):
        self.add()
        self.sync()
        requests = [dict(jsonrpc='2.0', id=1, method='initialize'),
                    dict(jsonrpc='2.0', id=2, method='tools/list'),
                    dict(jsonrpc='2.0', id=3, method='tools/call', params={'name':'sessions_search','arguments':{'query':'violet'}})]
        r = subprocess.run([sys.executable, '-m', 'agent_history', '--root', str(self.base / 'history'), 'mcp'],
                           input=''.join(json.dumps(x)+'\n' for x in requests), text=True, capture_output=True, check=True)
        out = [json.loads(x) for x in r.stdout.splitlines()]
        self.assertEqual(len(out), 3)
        self.assertEqual(len(out[1]['result']['tools']), 2)
        self.assertFalse(out[2]['result']['isError'])


if __name__ == '__main__':
    unittest.main()
