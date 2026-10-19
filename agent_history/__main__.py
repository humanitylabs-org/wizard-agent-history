"""Dependency-free CLI and newline-delimited JSON-RPC MCP transport."""
import argparse
import json
import os
from pathlib import Path
import sys

from .store import History, canonical
from .trim import accept_trim, trim_copy


def mcp(history):
    tools = [
        {'name': 'sessions_search', 'description': 'Search this authorized agent history. Conversation by default; deep adds readable reasoning, calls and tool output. FTS5 query syntax. Time bounds apply to message UTC timestamps.',
         'inputSchema': {'type': 'object', 'properties': {'query': {'type': 'string'}, 'deep': {'type': 'boolean', 'default': False},
             'after': {'type': 'string'}, 'before': {'type': 'string'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 1000}},
             'required': ['query'], 'additionalProperties': False}},
        {'name': 'session_read', 'description': 'Read preserved rows, including readable reasoning and tools; page by message ID.',
         'inputSchema': {'type': 'object', 'properties': {'session_id': {'type': 'string'},
             'after_id': {'type': 'integer', 'default': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 500}},
             'required': ['session_id'], 'additionalProperties': False}}]
    for line in sys.stdin:
        request = None
        try:
            request = json.loads(line)
            if 'id' not in request:
                continue
            method = request.get('method')
            if method == 'initialize':
                result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                          'serverInfo': {'name': 'wizard-agent-history', 'version': '0.1.0'}}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': tools}
            elif method == 'tools/call':
                p = request['params']
                fn = {'sessions_search': history.search, 'session_read': history.read}.get(p['name'])
                if fn is None:
                    raise ValueError('unknown tool')
                payload = fn(**p.get('arguments', {}))
                result = {'content': [{'type': 'text', 'text': canonical(payload).decode()}], 'isError': False}
            else:
                print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'error': {'code': -32601, 'message': 'method not found'}}), flush=True)
                continue
            print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
        except Exception as e:
            # Do not echo source records or malformed argument bodies in error logs.
            print(json.dumps({'jsonrpc': '2.0', 'id': request.get('id') if isinstance(request, dict) else None,
                              'error': {'code': -32602, 'message': str(e)}}), flush=True)


def main(argv=None):
    os.umask(0o077)
    p = argparse.ArgumentParser(prog='agent-history')
    p.add_argument('--config', default=os.getenv('AGENT_HISTORY_CONFIG'))
    p.add_argument('--root', default=os.getenv('AGENT_HISTORY_ROOT'))
    p.add_argument('--agent', default=None)
    sub = p.add_subparsers(dest='cmd', required=True)
    for name in ('sync', 'verify', 'seal', 'trim-copy', 'accept-trim'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--source')
        if name == 'sync':
            cmd.add_argument('--reconcile', action='store_true')
        if name == 'seal':
            cmd.add_argument('month')
        if name == 'trim-copy':
            cmd.add_argument('--output', required=True)
            cmd.add_argument('--live-days', type=int)
        if name == 'accept-trim':
            cmd.add_argument('--ledger', required=True)
    s = sub.add_parser('search')
    s.add_argument('query')
    s.add_argument('--deep', action='store_true')
    s.add_argument('--after')
    s.add_argument('--before')
    s.add_argument('--limit', type=int, default=20)
    s = sub.add_parser('read')
    s.add_argument('session_id')
    s.add_argument('--after-id', type=int, default=0)
    s.add_argument('--limit', type=int, default=100)
    s = sub.add_parser('export')
    s.add_argument('--output')
    sub.add_parser('stats')
    sub.add_parser('mcp')
    args = p.parse_args(argv)
    cfg = json.loads(Path(args.config).expanduser().read_text()) if args.config else {}
    root = args.root or cfg.get('root')
    if not root:
        p.error('--root or config.root is required (no implicit live paths)')
    source = getattr(args, 'source', None) or cfg.get('source')
    if args.cmd in ('sync', 'verify', 'seal', 'trim-copy', 'accept-trim') and not source:
        p.error('--source or config.source is required')
    writable = args.cmd not in ('search', 'read', 'stats', 'mcp')
    with History(root, args.agent or cfg.get('agent', 'hermes'), writable=writable) as h:
        if args.cmd == 'sync':
            result = h.sync(source, args.reconcile)
        elif args.cmd == 'verify':
            result = h.verify(source)
        elif args.cmd == 'seal':
            result = h.seal(source, args.month)
        elif args.cmd == 'trim-copy':
            result = trim_copy(h, source, args.output, args.live_days or cfg.get('live_days', 30))
        elif args.cmd == 'accept-trim':
            result = accept_trim(h, source, args.ledger)
        elif args.cmd == 'search':
            result = h.search(args.query, args.deep, args.after, args.before, args.limit)
        elif args.cmd == 'read':
            result = h.read(args.session_id, args.after_id, args.limit)
        elif args.cmd == 'export':
            result = h.export(args.output)
        elif args.cmd == 'stats':
            result = h.stats()
        else:
            mcp(h)
            return
    print(canonical(result).decode())


if __name__ == '__main__':
    main()
