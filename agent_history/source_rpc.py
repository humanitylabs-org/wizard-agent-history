"""Read-only source RPC over SSH; history SQLite files stay on their local disk.

No listening service, credentials, tunnels, or network-mounted SQLite databases.
The authenticated SSH owner launches this short-lived stdio process on the source.
"""
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys

from .store import canonical, unjson


class Row(dict):
    def __getitem__(self, key):
        return tuple(self.values())[key] if isinstance(key, int) else super().__getitem__(key)

    def __iter__(self):
        return super().__iter__()


class Result:
    def __init__(self, value):
        self.rows = [Row(zip(value['columns'], values)) for values in value['rows']]

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class RemoteSource:
    def __init__(self, config):
        host = config['ssh']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]*', host):
            raise ValueError('invalid SSH host alias')
        command = 'cd ' + shlex.quote(config['repo']) + ' && ' + shlex.join([
            'nice', '-n', '19', 'ionice', '-c3', config['python'], '-m',
            'agent_history', 'source-serve', '--source', config['path']])
        self.process = subprocess.Popen(['ssh', '-C', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
            '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3', host, command],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.info = self.rpc({'method': 'info'})

    def rpc(self, request):
        self.process.stdin.write(canonical(request) + b'\n')
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError('SSH source ended without a response')
        result = unjson(line)
        if 'error' in result:
            raise RuntimeError('read-only SSH source: ' + result['error'])
        return result

    def execute(self, sql, params=()):
        return Result(self.rpc({'method': 'query', 'sql': sql, 'params': params}))

    def close(self):
        self.process.stdin.close()
        self.process.stdout.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=10)


def descriptor(source):
    return canonical(source).decode() if isinstance(source, dict) else str(Path(source).expanduser().resolve())


def identity(source, connection=None):
    if isinstance(source, dict):
        if connection is not None:
            return connection.info['identity']
        c = RemoteSource(source)
        try:
            return c.info['identity']
        finally:
            c.close()
    stat = Path(source).expanduser().resolve().stat()
    return f'{stat.st_dev}:{stat.st_ino}'


def checksum(source):
    from .store import file_hash
    if isinstance(source, dict):
        c = RemoteSource(source)
        try:
            return c.rpc({'method': 'checksum'})['sha256']
        finally:
            c.close()
    return file_hash(source)


def serve(source):
    from .store import ro, file_hash
    path = Path(source).expanduser().resolve()
    connection = ro(path)
    try:
        for line in sys.stdin.buffer:
            try:
                request = unjson(line)
                method = request['method']
                if method == 'info':
                    result = {'identity': identity(path), 'data_version': connection.execute('PRAGMA data_version').fetchone()[0]}
                elif method == 'query':
                    sql = request['sql']
                    if not sql.lstrip().upper().startswith('SELECT '):
                        raise ValueError('only read-only SELECT statements are admitted')
                    cur = connection.execute(sql, request.get('params', []))
                    rows = cur.fetchall()  # Client issues ID-range LIMIT batches.
                    result = {'columns': [c[0] for c in cur.description], 'rows': [list(r) for r in rows]}
                elif method == 'checksum':
                    wal = Path(str(path) + '-wal')
                    if wal.exists() and wal.stat().st_size:
                        raise ValueError('checksum requires a checkpointed, stopped source')
                    before = path.stat()
                    sha = file_hash(path)
                    after = path.stat()
                    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns) or (wal.exists() and wal.stat().st_size):
                        raise ValueError('source changed during checksum')
                    result = {'sha256': sha}
                else:
                    raise ValueError('unknown source method')
            except Exception as e:
                result = {'error': type(e).__name__}
            sys.stdout.buffer.write(canonical(result) + b'\n')
            sys.stdout.buffer.flush()
    finally:
        connection.close()
