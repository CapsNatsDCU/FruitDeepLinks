#!/usr/bin/env python3
"""Manual, credential-safe TS/Python, TS/curl and HLS/FFmpeg comparison.

Run inside Fruit with PYTHONPATH=/app/bin. Secrets come from Fruit's configured
account pool; neither authenticated URLs nor provider error text are printed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path


METHODS = ('python_ts', 'curl_ts', 'ffmpeg_hls')
MAX_BYTES = 4 * 1024 * 1024


def capture(method, url, gate_fd, seconds, *, before_probe=lambda: None):
    import requests
    from xtream_curl import CurlStream
    from xtream_hls import HLSStream
    from xtream_transport import configure_session, media_chunks
    from server.services.xtream_quality import _probe_bytes

    started = time.monotonic()
    result = {'method': method, 'status': 'failed', 'http_status': None,
              'redirects': None, 'first_media_seconds': None, 'sample_bytes': 0}
    session = upstream = None
    sample = bytearray()
    try:
        if method == 'python_ts':
            session = configure_session(requests.Session())
            upstream = session.get(url, stream=True, timeout=(min(5, seconds), min(3, seconds)),
                                   headers={'Accept-Encoding': 'identity'}, allow_redirects=True)
            result['http_status'] = upstream.status_code
            result['redirects'] = len(upstream.history)
            result['headers_seconds'] = round(time.monotonic() - started, 3)
            upstream.raise_for_status()
            chunks = iter(media_chunks(upstream))
        elif method == 'curl_ts':
            upstream = CurlStream(url, min(3, seconds), gate_fd)
            chunks = iter(upstream.chunks())
        else:
            upstream = HLSStream(url, min(3, seconds), gate_fd)
            chunks = iter(upstream.chunks())
        first = next(chunks, b'')
        if not first or first[0] != 0x47:
            result['error'] = 'not_mpeg_ts'
        else:
            result['first_media_seconds'] = round(time.monotonic() - started, 3)
            sample.extend(first[:MAX_BYTES])
            until = min(started + seconds, time.monotonic() + 2)
            while len(sample) < MAX_BYTES and time.monotonic() < until:
                chunk = next(chunks, b'')
                if not chunk:
                    break
                sample.extend(chunk[:MAX_BYTES - len(sample)])
    except (requests.Timeout, TimeoutError):
        result['error'] = 'timeout'
    except requests.HTTPError:
        result['error'] = 'http_error'
    except requests.ConnectionError:
        result['error'] = 'connection_error'
    except Exception:
        result['error'] = 'transport_error'
    finally:
        for resource in (upstream, session):
            if resource is not None:
                resource.close()
    result['sample_bytes'] = len(sample)
    if sample and 'error' not in result:
        try:
            before_probe()
            result['video'] = _probe_bytes(bytes(sample))
            result['status'] = 'passed'
        except Exception:
            result['error'] = 'video_not_detected'
    result['elapsed_seconds'] = round(time.monotonic() - started, 3)
    return result


def worker(gate_fd, seconds):
    if os.getpgrp() != os.getpid():
        return 2
    # Includes local video analysis. The parent separately supervises this
    # group, so neither it nor a surviving media child can hold the gate forever.
    timer = threading.Timer(seconds + 9, lambda: os.killpg(os.getpgrp(), signal.SIGKILL))
    timer.daemon = True
    timer.start()
    try:
        arguments = json.load(sys.stdin)
        # Hard network deadline even for HTTP slow trickles or blocked C calls.
        network_timer = threading.Timer(seconds, lambda: os.killpg(os.getpgrp(), signal.SIGKILL))
        network_timer.daemon = True
        network_timer.start()
        try:
            # All provider transports close before this local ffprobe callback.
            result = capture(arguments['method'], arguments['url'], gate_fd, seconds,
                             before_probe=network_timer.cancel)
        finally:
            network_timer.cancel()
        print(json.dumps(result), flush=True)
        return 0
    except Exception:
        # Never print exceptions; provider clients may include credentials.
        return 2
    finally:
        timer.cancel()


def run_probe(method, config, gate_fd, seconds):
    from xtream_ingest import build_stream_url
    started = time.monotonic()
    child = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), '--worker', str(gate_fd), str(seconds)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        pass_fds=(gate_fd,), start_new_session=True,
    )
    try:
        output, _ = child.communicate(json.dumps({'method': method,
            'url': build_stream_url(config[0], config[1], 'm3u8' if method == 'ffmpeg_hls' else 'ts')}).encode(),
            timeout=seconds + 10)
        if child.returncode == 0:
            return json.loads(output)
        error = 'network_deadline' if child.returncode == -signal.SIGKILL else 'worker_error'
    except subprocess.TimeoutExpired:
        error = 'worker_deadline'
    finally:
        # Always stop every descendant before making the account reusable.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=2)
        for pipe in (child.stdin, child.stdout):
            if pipe and not pipe.closed:
                pipe.close()
    return {'method': method, 'status': 'failed', 'error': error,
            'http_status': None, 'redirects': None, 'first_media_seconds': None, 'sample_bytes': None,
            'elapsed_seconds': round(time.monotonic() - started, 3)}


def compare(pool, account_id, stream_id, *, host='configured', path_style='root', seconds=8):
    from xtream_accounts import safe_value
    from xtream_gate import AccountBusy, AccountDisabled
    account = next((account for account in pool.accounts if account.id == account_id), None)
    if account is None:
        raise ValueError('Configured account ID not found; use --list')
    state = next(row for row in pool.status()['accounts'] if row['id'] == account_id)
    if not state['enabled']:
        return {'status': 'skipped', 'reason': 'account_disabled', 'account_id': account_id}
    config = account.config
    if host == 'alternate':
        if not config.fallback_server_url:
            raise ValueError('This account has no configured alternate host')
        config = replace(config, server_url=config.fallback_server_url, fallback_server_url=None)
    if path_style == 'live':
        config = replace(config, server_url=config.server_url.rstrip('/') + '/live')
    try:
        # Acquire the original credential gate even when testing its alternate
        # host. Hold it across all three tests, just like a provider operation.
        with pool.gate.hold(account.config, wait_seconds=0) as fd:
            results = [run_probe(method, (config, stream_id), fd, seconds) for method in METHODS]
    except AccountDisabled:
        return {'status': 'skipped', 'reason': 'account_disabled', 'account_id': account_id}
    except AccountBusy:
        return {'status': 'skipped', 'reason': 'account_in_use', 'account_id': account_id}
    return safe_value({'status': 'complete', 'account_id': account_id, 'stream_id': stream_id,
                       'host': host, 'path_style': path_style, 'results': results}, pool.accounts)


def main(argv=None):
    if len(sys.argv) == 4 and sys.argv[1] == '--worker':
        return worker(int(sys.argv[2]), float(sys.argv[3]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--list', action='store_true', help='List configured account IDs and saved channels; no provider calls')
    parser.add_argument('--account', help='Configured account ID')
    channel = parser.add_mutually_exclusive_group()
    channel.add_argument('--stream-id', help='Provider stream ID, matching the working channel in the other app')
    channel.add_argument('--channel-id', type=int, help='Fruit saved persistent channel ID, resolved locally')
    parser.add_argument('--host', choices=('configured', 'alternate'), default='configured')
    parser.add_argument('--path-style', choices=('root', 'live'), default='root',
                        help='Default: Fruit root-path URLs; select live only to compare an observed /live/ URL')
    parser.add_argument('--seconds', type=int, choices=range(5, 31), default=8,
                        metavar='5–30', help='Equal hard media deadline for each method; default 8 seconds')
    parser.add_argument('--db', type=Path, help='Fruit database path; defaults to FRUIT_DB_PATH')
    args = parser.parse_args(argv)
    try:
        from db.connection import resolve_db_path
        from xtream_pool import XtreamPool
        from xtream_accounts import safe_value
        from server.services.xtream_persistent import get_channel, list_channels
        path = args.db or resolve_db_path()
        if not path.is_file():
            raise ValueError('Fruit database not found; run inside its container or provide --db')
        pool = XtreamPool(path)
        if args.list:
            accounts = [{'id': row['id'], 'enabled': row['enabled'], 'health': row['health'],
                         'busy': row['busy']} for row in pool.status()['accounts']]
            with pool.connection() as conn:
                channels = [{'channel_id': row['id'], 'stream_id': row['stream_id'],
                             'name': row['display_name']} for row in list_channels(conn, enabled_only=True)[:50]]
            print(json.dumps(safe_value({'accounts': accounts, 'first_50_saved_channels': channels}, pool.accounts), indent=2))
            return 0
        if not args.account or (args.stream_id is None and args.channel_id is None):
            raise ValueError('Provide --account and --stream-id or --channel-id; use --list to find IDs')
        stream_id = args.stream_id
        if args.channel_id is not None:
            with pool.connection() as conn:
                saved = get_channel(conn, args.channel_id)
            if not saved or not saved['enabled']:
                raise ValueError('Enabled saved channel not found')
            stream_id = str(saved['stream_id'])
        if not re.fullmatch(r'[0-9]+', stream_id or ''):
            raise ValueError('Provider stream ID must be numeric')
        result = compare(pool, args.account, stream_id, host=args.host,
                         path_style=args.path_style, seconds=args.seconds)
        print(json.dumps(result, indent=2), flush=True)
        return 0 if result['status'] == 'complete' and any(row['status'] == 'passed' for row in result['results']) else 1
    except ValueError as exc:
        print(json.dumps({'status': 'error', 'message': str(exc)}))
        return 2
    except KeyboardInterrupt:
        print(json.dumps({'status': 'cancelled'}))
        return 130
    except Exception:
        print(json.dumps({'status': 'error', 'message': 'Comparison setup failed; verify Fruit configuration and dependencies'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
