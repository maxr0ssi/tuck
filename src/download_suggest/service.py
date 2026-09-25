"""Private localhost service. Only explicit, authenticated approvals move files."""
from __future__ import annotations

import errno
import fcntl
import hmac
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import secrets
import subprocess
import signal
import sqlite3
import stat
import threading
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingTCPServer

from .classifier import Suggester, FOLDER_GUIDANCE, FILENAME_GUIDANCE

PARTIAL = ('.part', '.partial', '.download', '.crdownload', '.tmp')


def fingerprint(path):
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Only regular files are supported.')
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def matches(path, expected):
    try:
        return fingerprint(path) == expected
    except (OSError, ValueError):
        return False


def same_content_identity(path, expected):
    # Creating/removing a hard link changes ctime, but not the content identity.
    try:
        return fingerprint(path)[:4] == expected[:4]
    except (OSError, ValueError):
        return False


def eligible(path):
    return not path.name.startswith('.') and not path.name.lower().endswith(PARTIAL) and not path.is_symlink() and path.is_file()


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
            raise
    finally:
        os.close(fd)


class Conflict(ValueError):
    pass


class Service:
    def __init__(self, config, state_dir):
        self.config = config
        self.root = Path(config['watch_dir'])
        self.folders = {f['id']: f for f in config['folders']}
        self.state_dir = Path(state_dir)
        self.lock = threading.RLock()
        self.audit_status = 'ok'
        try:
            version = importlib.metadata.version('tinyjev')
        except importlib.metadata.PackageNotFoundError:
            version = 'unavailable'
        def source_hash(name):
            try:
                return hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            except OSError:
                return 'unavailable'
        self.audit_context = {
            'model_name': config.get('model', {}).get('name'),
            'model_enabled': config.get('model', {}).get('enabled', False),
            'instructions': config.get('instructions', ''),
            'folder_prompt': config.get('instructions', '') + FOLDER_GUIDANCE,
            'filename_prompt': FILENAME_GUIDANCE,
            'classifier_sha256': source_hash('classifier.py'),
            'worker_sha256': source_hash('model_worker.py'), 'tinyjev_version': version,
            'config_sha256': hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
        }
        self.stop = threading.Event()
        self.paused = False
        self.manual = threading.Event()
        self.busy = set()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='suggest')
        self.model = Suggester(config, self.state_dir)
        self.db = sqlite3.connect(self.state_dir / 'state.sqlite3', check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS seen(path TEXT PRIMARY KEY, fingerprint TEXT);
            CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS journal(id TEXT PRIMARY KEY, data TEXT NOT NULL);''')
        self.db.commit()
        self.backfill_audit()
        self.reconcile()
        if not self.db.execute("SELECT 1 FROM meta WHERE key='initialized'").fetchone():
            for path in self.files():
                try:
                    self.mark_seen(path, fingerprint(path))
                except (OSError, ValueError):
                    continue
            self.db.execute("INSERT INTO meta VALUES ('initialized','1')")
            self.db.commit()

    def audit(self, event, item_id, **fields):
        """Local append-only evidence; failures cannot change a file-operation result."""
        try:
            entry = {'schema_version': 1, 'event': event, 'event_id': secrets.token_hex(12),
                     'timestamp': time.time(), 'occurred_at': None if fields.get('reconstructed') else time.time(),
                     'item_id': item_id, 'context': self.audit_context, **fields}
            line = (json.dumps(entry, ensure_ascii=True) + '\n').encode('utf-8')
            with self.lock:
                descriptor = os.open(self.state_dir / 'recommendations.jsonl',
                                     os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, 'ab') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(line)
                    stream.flush()
                    os.fsync(stream.fileno())
            return True
        except Exception as exc:
            # Sticky: a later successful write does not conceal earlier missing events.
            self.audit_status = f'error: recommendation history may be incomplete ({type(exc).__name__})'
            return False

    @staticmethod
    def recommendation_snapshot(item):
        return {key: value for key, value in item.items() if key not in ('source_fp', 'applied_fp')}

    def backfill_audit(self):
        if self.db.execute("SELECT 1 FROM meta WHERE key='audit_enabled_v1'").fetchone():
            return
        complete = True
        for raw, in self.db.execute('SELECT data FROM items').fetchall():
            item = json.loads(raw)
            fields = {'reconstructed': True, 'context_historical': False}
            complete = self.audit('recommendation', item['id'], **fields,
                event_id='backfill:recommendation:' + item['id'], occurred_at=item.get('created_at'),
                recommendation=self.recommendation_snapshot(item)) and complete
            if item['status'] != 'pending':
                applied = item.get('applied_path')
                choices = [f for f in self.folders.values() if applied and Path(f['path']) == Path(applied).parent]
                chosen = choices[0] if len(choices) == 1 else None
                action = {'applied': 'apply', 'undone': 'undo', 'left': 'leave'}.get(item['status'], 'unknown')
                decision = {'action': action, 'requested_name': Path(applied).name if applied else None,
                            'requested_folder_id': chosen['id'] if chosen else None,
                            'requested_folder_label': chosen['label'] if chosen else None,
                            'name_changed': Path(applied).name != item['suggested_name'] if applied else None,
                            'folder_changed': chosen['id'] != item['folder_id'] if chosen else None}
                complete = self.audit('decision_result', item['id'], **fields,
                    event_id='backfill:decision:' + item['id'], decision=decision,
                    outcome='reconstructed', status=item['status'], applied_path=applied,
                    choice_inferred_from_path=bool(applied), folder_ambiguous=bool(applied and not chosen)) and complete
        if complete:
            self.db.execute("INSERT INTO meta VALUES ('audit_enabled_v1','1')")
            self.db.commit()

    def files(self):
        try:
            return [p for p in self.root.iterdir() if eligible(p)]
        except OSError:
            return []

    def mark_seen(self, path, fp):
        self.db.execute('INSERT OR REPLACE INTO seen VALUES (?,?)', (str(path), json.dumps(fp)))

    def save(self, item):
        self.db.execute('INSERT OR REPLACE INTO items VALUES (?,?)', (item['id'], json.dumps(item)))
        self.db.commit()

    def get(self, item_id):
        row = self.db.execute('SELECT data FROM items WHERE id=?', (item_id,)).fetchone()
        if not row:
            raise KeyError('Item not found.')
        return json.loads(row[0])

    def reconcile(self):
        for item_id, raw in self.db.execute('SELECT id,data FROM journal').fetchall():
            job = json.loads(raw)
            item = self.get(item_id)
            src, dst = Path(job['src']), Path(job['dst'])
            staged = Path(job['staged']) if job.get('staged') else None
            destination_ok = job.get('destination_fp') and matches(dst, job['destination_fp'])
            if destination_ok and not src.exists() and not (staged and staged.exists()):
                item['status'] = job['final_status']
                if item['status'] == 'applied':
                    item['applied_path'], item['applied_fp'] = str(dst), fingerprint(dst)
                else:
                    self.mark_seen(dst, fingerprint(dst))
            elif matches(src, job['source_fp']) and not dst.exists() and not (job.get('copy_staged') and os.path.lexists(job['copy_staged'])):
                item['status'] = job['initial_status']
            else:
                # ponytail: ambiguous crash recovery keeps both paths; manual review beats guessing ownership.
                item['status'] = 'interrupted'
                item['reason'] = 'Interrupted move: review both locations; no recovery deletion was performed.'
                item['recovery_paths'] = [str(src), str(dst)] + ([str(staged)] if staged else []) + ([job['copy_staged']] if job.get('copy_staged') else [])
            self.save(item)
            self.audit('recovery', item_id, status=item['status'], recovery_paths=item.get('recovery_paths', []))
            self.db.execute('DELETE FROM journal WHERE id=?', (item_id,))
            self.db.commit()

    def state(self):
        # WAL readers can refresh while a move holds the write-side lock.
        with closing(sqlite3.connect(self.state_dir / 'state.sqlite3')) as reader:
            items = [json.loads(r[0]) for r in reader.execute('SELECT data FROM items')]
        for index, item in enumerate(items):
            if item['status'] != 'pending' or not self.lock.acquire(blocking=False):
                continue
            try:
                # Re-read after acquiring the lock; a move may have just finished.
                item = self.get(item['id'])
                if item['status'] == 'pending' and not matches(Path(item['source_path']), item['source_fp']):
                    item['status'] = 'unavailable'
                    item['reason'] = 'The original file was deleted, moved, or changed outside this review. Nothing was moved by this action.'
                    self.save(item)
                    self.audit('source_unavailable', item['id'], status=item['status'])
                items[index] = item
            finally:
                self.lock.release()
        items.sort(key=lambda i: i['created_at'], reverse=True)
        public = lambda i: {k: v for k, v in i.items() if k not in ('source_fp', 'applied_fp')}
        return {'pending': [public(i) for i in items if i['status'] == 'pending'],
                'recent': [public(i) for i in items if i['status'] == 'interrupted'] +
                          [public(i) for i in sorted(items, key=lambda i: i.get('decided_at', i['created_at']), reverse=True)
                           if i['status'] not in ('pending', 'interrupted')][:100],
                'folders': list(self.folders.values()), 'watching': not self.paused,
                'model_status': self.model.status, 'audit_status': self.audit_status}

    def propose(self, path, fp, started, use_model=True):
        try:
            result = {'suggested_name': path.name, 'folder_id': None,
                      'reason': 'No confident destination; leave in Downloads.', 'method': 'fallback'}
            if use_model:
                try:
                    result.update(self.model.suggest(path, deadline=started + self.config.get('suggestion_deadline_ms', 1500) / 1000))
                except Exception:
                    result['reason'] = 'Suggestion unavailable; leave in Downloads.'
            with self.lock:
                if self.stop.is_set() or not matches(path, fp):
                    return
                folder = self.folders.get(result.get('folder_id'))
                item = {'id': secrets.token_hex(12), 'source_name': path.name, 'source_path': str(path),
                        'source_fp': fp, 'suggested_name': result.get('suggested_name', path.name),
                        'folder_id': folder['id'] if folder else None,
                        'folder_label': folder['label'] if folder else 'Leave in Downloads',
                        'reason': result.get('reason', ''), 'method': result.get('method', 'fallback'),
                        'latency_ms': round((time.monotonic() - started) * 1000),
                        'status': 'pending', 'created_at': time.time()}
                self.mark_seen(path, fp)
                self.save(item)
                self.audit('recommendation', item['id'], recommendation=self.recommendation_snapshot(item))
        finally:
            with self.lock:
                self.busy.discard(str(path))

    def watch(self):
        stable = {}
        while not self.stop.wait(self.config.get('poll_interval_ms', 100) / 1000):
            if self.paused:
                continue
            manual = self.manual.is_set()
            if manual:
                self.manual.clear()
            present = set()
            for path in self.files():
                key = str(path)
                present.add(key)
                try:
                    fp = fingerprint(path)
                except (OSError, ValueError):
                    continue
                with self.lock:
                    if manual:
                        # Do not create duplicate proposals when a scan is clicked twice.
                        known = any(json.loads(r[0]).get('source_fp') == fp for r in self.db.execute('SELECT data FROM items'))
                        if not known:
                            self.db.execute('DELETE FROM seen WHERE path=?', (key,))
                            self.db.commit()
                    seen = self.db.execute('SELECT fingerprint FROM seen WHERE path=?', (key,)).fetchone()
                    if key in self.busy or (seen and json.loads(seen[0]) == fp):
                        continue
                now = time.monotonic()
                prior = stable.get(key)
                if not prior or prior[0] != fp:
                    stable[key] = (fp, now)
                    continue
                if now - prior[1] < self.config.get('settle_ms', 400) / 1000:
                    continue
                with self.lock:
                    self.busy.add(key)
                    available = len(self.busy) <= 4
                if available:
                    self.pool.submit(self.propose, path, fp, prior[1])
                else:
                    self.propose(path, fp, prior[1], use_model=False)
                stable.pop(key, None)
            stable = {k: v for k, v in stable.items() if k in present}

    def move(self, item, src, dst, expected, final_status):
        if not matches(src, expected):
            raise Conflict('The source changed or disappeared. Rescan before moving it.')
        if os.path.lexists(dst):
            raise Conflict('A file already exists at that destination.')
        if not dst.parent.is_dir() or dst.parent.resolve() != dst.parent:
            raise Conflict('Destination folder changed or is unavailable.')
        job = {'src': str(src), 'dst': str(dst), 'source_fp': expected,
               'initial_status': item['status'], 'final_status': final_status}
        def journal():
            self.db.execute('INSERT OR REPLACE INTO journal VALUES (?,?)', (item['id'], json.dumps(job)))
            self.db.commit()
        journal()  # Durable intent precedes any filesystem mutation.
        created = False
        try:
            try:
                os.link(src, dst, follow_symlinks=False)
                created = True
                if not same_content_identity(src, expected) or not same_content_identity(dst, expected):
                    raise Conflict('The source changed during the move.')
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                # ditto preserves macOS resource forks, ACLs, and quarantine metadata.
                copy_dir = Path(tempfile.mkdtemp(prefix='.download-suggest-', dir=dst.parent))
                copy_staged = copy_dir / src.name
                job['copy_staged'] = str(copy_staged)
                journal()
                subprocess.run(['/usr/bin/ditto', '--rsrc', '--extattr', '--qtn', '--acl', str(src), str(copy_staged)],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if not matches(src, expected):
                    raise Conflict('The source changed during the copy; the copy was kept for review.')
                copied_fp = fingerprint(copy_staged)
                # Different filesystems can round timestamps; ditto completion plus
                # unchanged source and copied size are the cross-volume checks.
                if copied_fp[2] != expected[2]:
                    raise Conflict('Copied file metadata differs; both files were kept for review.')
                os.link(copy_staged, dst, follow_symlinks=False)
                created = True
                os.unlink(copy_staged)
                copy_dir.rmdir()
            destination_fp = fingerprint(dst)
            expected_destination = copied_fp if job.get('copy_staged') else expected
            if destination_fp[:4] != expected_destination[:4]:
                raise Conflict('The destination changed during the move; the original was kept.')
            sync_directory(dst.parent)
            job['destination_fp'] = destination_fp
            journal()
            if not same_content_identity(src, expected):
                raise Conflict('The source changed before removal; both files were kept.')
            # Move the source into an exclusively owned directory before unlinking:
            # a concurrent replacement at its original name must never be deleted.
            staging_dir = Path(tempfile.mkdtemp(prefix='.download-suggest-', dir=src.parent))
            staged = staging_dir / src.name
            job['staged'] = str(staged)
            journal()
            os.rename(src, staged)
            sync_directory(src.parent)
            if not same_content_identity(staged, expected):
                raise Conflict('The source changed during the move; all remaining files were kept for review.')
            if not same_content_identity(dst, job['destination_fp']):
                raise Conflict('The destination changed during the move; the staged original was kept for review.')
            os.unlink(staged)
            staging_dir.rmdir()
            sync_directory(src.parent)
            item['status'] = final_status
            item['decided_at'] = time.time()
            if final_status == 'applied':
                item['applied_path'], item['applied_fp'] = str(dst), fingerprint(dst)
            else:
                self.mark_seen(dst, fingerprint(dst))
            self.save(item)
            self.db.execute('DELETE FROM journal WHERE id=?', (item['id'],))
            self.db.commit()
        except Exception:
            if created or job.get('copy_staged'):
                item['status'] = 'interrupted'
                item['reason'] = 'Move interrupted; review both locations. No files were overwritten.'
                item['recovery_paths'] = [str(src), str(dst)] + ([job['staged']] if job.get('staged') else []) + ([job['copy_staged']] if job.get('copy_staged') else [])
                self.save(item)
            else:
                self.db.execute('DELETE FROM journal WHERE id=?', (item['id'],))
                self.db.commit()
            raise

    def action(self, item_id, action, body):
        with self.lock:
            try:
                item = self.get(item_id)
            except KeyError:
                item = {}
            folder_id = body.get('folder_id')
            chosen = self.folders.get(folder_id) if isinstance(folder_id, str) else None
            decision = {'action': action, 'requested_name': body.get('name'),
                        'requested_folder_id': folder_id,
                        'requested_folder_label': chosen['label'] if chosen else None,
                        'name_changed': body.get('name') != item.get('suggested_name') if action == 'apply' else None,
                        'folder_changed': folder_id != item.get('folder_id') if action == 'apply' else None}
            attempt_id = secrets.token_hex(12)
            self.audit('decision_attempt', item_id, attempt_id=attempt_id, decision=decision)
            try:
                result = self._action(item_id, action, body)
            except Exception as exc:
                self.audit('decision_result', item_id, attempt_id=attempt_id, decision=decision,
                           outcome='conflict' if isinstance(exc, (Conflict, FileExistsError)) else 'failed',
                           error_type=type(exc).__name__, error=str(exc))
                raise
            try:
                current = self.get(item_id)
            except Exception as exc:
                self.audit_status = f'error: successful action could not be recorded ({type(exc).__name__})'
                return result
            self.audit('decision_result', item_id, attempt_id=attempt_id, decision=decision,
                       outcome='success', status=current['status'], applied_path=current.get('applied_path'))
            return result

    def _action(self, item_id, action, body):
        # Serialize approved moves; state reads use a separate WAL connection.
        with self.lock:
            item = self.get(item_id)
            if action == 'leave':
                if item['status'] != 'pending':
                    raise Conflict('This item is no longer pending.')
                item['status'] = 'left'
                item['decided_at'] = time.time()
                self.save(item)
            elif action == 'apply':
                if item['status'] != 'pending':
                    raise Conflict('This item is no longer pending.')
                folder = self.folders.get(body.get('folder_id'))
                name = body.get('name')
                if not folder or not isinstance(name, str) or not name or name in ('.', '..') or name.startswith('.') or any(c in name for c in '/\\\0:') or any(ord(c) < 32 for c in name):
                    raise ValueError('Choose an allowed folder and a valid filename.')
                if Path(name).suffix.lower() != Path(item['source_name']).suffix.lower():
                    raise ValueError('Keep the original file extension.')
                source = Path(item['source_path'])
                if source.parent != self.root:
                    raise ValueError('Source is outside the watched folder.')
                self.move(item, source, Path(folder['path']) / name, item['source_fp'], 'applied')
            elif action == 'undo':
                if item['status'] != 'applied':
                    raise Conflict('Only an applied move can be undone.')
                self.move(item, Path(item['applied_path']), Path(item['source_path']), item['applied_fp'], 'undone')
            else:
                raise KeyError('Unknown action.')
            return {'ok': True}


def run_service(config: dict, state_dir: Path, port: int = 0):
    state_dir = Path(state_dir)
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)
    # Keep SQLite, session credentials, and lock files private from creation.
    os.umask(0o077)
    instance_lock = open(state_dir / 'service.lock', 'a')
    try:
        fcntl.flock(instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        instance_lock.close()
        raise RuntimeError('The service is already running for this state directory.')
    service = Service(config, state_dir)
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Local paths and credentials never enter access logs.

        def respond(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def handle_request(self):
            if self.headers.get('Origin') is not None or not hmac.compare_digest(self.headers.get('Authorization', '').encode('utf-8'), ('Bearer ' + token).encode('ascii')):
                return self.respond(401, {'error': 'Unauthorized.'})
            try:
                if self.command == 'GET':
                    if self.path == '/v1/health':
                        return self.respond(200, {'ok': True})
                    if self.path == '/v1/state':
                        return self.respond(200, service.state())
                    raise KeyError('Endpoint not found.')
                size = int(self.headers.get('Content-Length', '0'))
                if size < 0 or size > 8192 or self.headers.get('Transfer-Encoding'):
                    return self.respond(413, {'error': 'Request body too large or unsupported.'})
                body = json.loads(self.rfile.read(size) or b'{}')
                if not isinstance(body, dict):
                    raise ValueError('Expected a JSON object.')
                if self.path == '/v1/pause':
                    if not isinstance(body.get('paused'), bool):
                        raise ValueError('paused must be a boolean.')
                    service.paused = body['paused']
                    return self.respond(200, {'ok': True})
                if self.path == '/v1/scan':
                    service.manual.set()
                    return self.respond(200, {'ok': True})
                pieces = self.path.split('/')
                if len(pieces) == 5 and pieces[:3] == ['', 'v1', 'items']:
                    return self.respond(200, service.action(pieces[3], pieces[4], body))
                raise KeyError('Endpoint not found.')
            except KeyError as exc:
                self.respond(404, {'error': str(exc).strip("'")})
            except (Conflict, FileExistsError) as exc:
                self.respond(409, {'error': str(exc)})
            except (ValueError, TypeError) as exc:
                self.respond(400, {'error': str(exc)})
            except (OSError, subprocess.SubprocessError):
                self.respond(409, {'error': 'File operation unavailable. Check the file and folder permissions.'})

        do_GET = handle_request
        do_POST = handle_request

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

    # HTTPServer performs reverse DNS during binding; loopback needs no lookup.
    server = ThreadingTCPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    session = state_dir / 'session.json'
    temp = state_dir / 'session.json.tmp'
    temp.write_text(json.dumps({'port': server.server_address[1], 'token': token, 'pid': os.getpid()}))
    os.chmod(temp, 0o600)
    temp.replace(session)
    watcher = threading.Thread(target=service.watch, daemon=True, name='downloads-watcher')
    watcher.start()
    def shutdown(*_):
        service.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    old_handlers = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            old_handlers[sig] = signal.signal(sig, shutdown)
    try:
        server.serve_forever(poll_interval=.1)
    finally:
        service.stop.set()
        watcher.join(timeout=2)
        service.pool.shutdown(wait=True)
        service.model.close()
        server.server_close()
        service.db.close()
        session.unlink(missing_ok=True)
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        instance_lock.close()
