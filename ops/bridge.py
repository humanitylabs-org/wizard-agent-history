"""Run a History command on the archive host against a source on this host.

The source host starts the connection, so the archive host needs no way back in:

    python3 -m ops.bridge --source ~/.hermes/state.db -- ssh thales 'cd REPO && python3.14 -m agent_history --config CFG sync'

A local read-only ``source-serve`` answers the remote command's RPC requests:
remote stdout -> local server stdin, local server stdout -> remote stdin. The
remote config must use ``"source": {"stdio": ...}``. Exit status is the remote's.
"""
import argparse
import os
import subprocess
import sys


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--source', required=True)
    p.add_argument('--python', default=sys.executable)
    p.add_argument('remote', nargs=argparse.REMAINDER)
    a = p.parse_args(argv)
    remote = a.remote[1:] if a.remote[:1] == ['--'] else a.remote
    if not remote:
        p.error('remote command required after --')
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, PYTHONPATH=here)
    server = subprocess.Popen([a.python, '-m', 'agent_history', 'source-serve', '--source', a.source],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
    try:
        client = subprocess.Popen(remote, stdin=server.stdout, stdout=server.stdin)
        server.stdout.close()
        server.stdin.close()
        code = client.wait()
    finally:
        if server.poll() is None:
            server.terminate()
        server.wait(timeout=30)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
