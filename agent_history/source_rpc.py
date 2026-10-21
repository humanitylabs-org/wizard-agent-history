"""Read-only source RPC over SSH; history SQLite files stay on their local disk.

No listening service, credentials, tunnels, or network-mounted SQLite databases.
The authenticated SSH owner launches this short-lived stdio process on the source.
"""
import contextlib
import hashlib
import os
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


# Set by the CLI when the History process itself is the far end of a bridge:
# its original stdout carries RPC requests, its stdin carries responses.
STDIO = None


class RemoteSource:
    def __init__(self, config):
        if 'stdio' in config:
            if STDIO is None:
                raise ValueError('stdio source requires the bridged CLI')
            self.process = None
            self.reader, self.writer = STDIO
            self.info = self.rpc({'method': 'info'})
            return
        host = config['ssh']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]*', host):
            raise ValueError('invalid SSH host alias')
        command = 'cd ' + shlex.quote(config['repo']) + ' && ' + shlex.join([
            'nice', '-n', '19', 'ionice', '-c3', config['python'], '-m',
            'agent_history', 'source-serve', '--source', config['path']])
        self.process = subprocess.Popen(['ssh', '-C', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
            '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3', host, command],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.reader, self.writer = self.process.stdout, self.process.stdin
        self.info = self.rpc({'method': 'info'})

    def rpc(self, request):
        self.writer.write(canonical(request) + b'\n')
        self.writer.flush()
        line = self.reader.readline()
        if not line:
            raise RuntimeError('SSH source ended without a response')
        result = unjson(line)
        if 'error' in result:
            raise RuntimeError('read-only SSH source: ' + result['error'])
        return result

    def execute(self, sql, params=()):
        return Result(self.rpc({'method': 'query', 'sql': sql, 'params': params}))

    def close(self):
        if self.process is None:
            return  # Bridged stdio is shared by every connection in this process.
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


def source_token(source, connection):
    if isinstance(source, dict):
        return connection.rpc({'method': 'info'})['token']
    st = Path(source).expanduser().resolve().stat()
    return [f'{st.st_dev}:{st.st_ino}', st.st_size, st.st_mtime_ns, st.st_ctime_ns,
            connection.execute('PRAGMA data_version').fetchone()[0]]


def local_attestation(source, connection):
    path = Path(source).expanduser().resolve()
    wal = Path(str(path) + '-wal')
    if wal.exists() and wal.stat().st_size:
        raise ValueError('attestation requires a checkpointed, stopped source')
    before = source_token(path, connection)
    # Hash an open descriptor, never repeatedly resolve a potentially replaced path.
    with path.open('rb') as f:
        st = os.fstat(f.fileno())
        if [f'{st.st_dev}:{st.st_ino}', st.st_size, st.st_mtime_ns, st.st_ctime_ns] != before[:4]:
            raise ValueError('source replaced before checksum')
        digest = hashlib.file_digest(f, 'sha256').hexdigest()
    if source_token(path, connection) != before or (wal.exists() and wal.stat().st_size):
        raise ValueError('source changed during checksum')
    return {'sha256': digest, 'identity': before[0], 'token': before}


@contextlib.contextmanager
def attestation(source):
    from .store import ro
    with contextlib.closing(ro(source)) as connection:
        proof = (connection.rpc({'method': 'checksum'}) if isinstance(source, dict) else
                 local_attestation(source, connection))
        yield proof
        if source_token(source, connection) != proof['token']:
            raise ValueError('attested source changed before acceptance')


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
                    token = source_token(path, connection)
                    result = {'identity': token[0], 'data_version': token[-1], 'token': token}
                elif method == 'query':
                    sql = request['sql']
                    if not sql.lstrip().upper().startswith('SELECT '):
                        raise ValueError('only read-only SELECT statements are admitted')
                    cur = connection.execute(sql, request.get('params', []))
                    rows = cur.fetchall()  # Client issues ID-range LIMIT batches.
                    result = {'columns': [c[0] for c in cur.description], 'rows': [list(r) for r in rows]}
                elif method == 'checksum':
                    result = local_attestation(path, connection)
                else:
                    raise ValueError('unknown source method')
            except Exception as e:
                result = {'error': type(e).__name__}
            sys.stdout.buffer.write(canonical(result) + b'\n')
            sys.stdout.buffer.flush()
    finally:
        connection.close()
