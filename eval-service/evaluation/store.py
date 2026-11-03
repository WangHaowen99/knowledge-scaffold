"""Serialized SQLite mutations and encrypted secrets; no user tokens on disk."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from cryptography.fernet import Fernet
from .core import Failure, canonical, digest


class Store:
    def __init__(self, root, encryption_key=''):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.cipher = Fernet(encryption_key.encode()) if encryption_key else None
        self.db = sqlite3.connect(self.root / 'evaluation.sqlite', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS records(
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL,
          revision INTEGER NOT NULL, body TEXT NOT NULL, updated REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS records_owner ON records(owner,kind,updated);
        CREATE TABLE IF NOT EXISTS requests(
          owner TEXT, operation TEXT, request_id TEXT, digest TEXT, record_id TEXT,
          PRIMARY KEY(owner,operation,request_id));
        CREATE TABLE IF NOT EXISTS tombstones(id TEXT PRIMARY KEY, deleted REAL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, body TEXT NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS artifacts(
          run_id TEXT NOT NULL, owner TEXT NOT NULL, name TEXT NOT NULL,
          revision INTEGER NOT NULL, body TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(run_id,name));
        CREATE TABLE IF NOT EXISTS notifications(
          id TEXT PRIMARY KEY, run_id TEXT NOT NULL, owner TEXT NOT NULL, sequence INTEGER NOT NULL,
          body TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL, next_attempt REAL NOT NULL,
          last_error TEXT, created REAL NOT NULL, delivered REAL);
        CREATE INDEX IF NOT EXISTS notifications_due ON notifications(status,next_attempt);
        CREATE INDEX IF NOT EXISTS notifications_run ON notifications(run_id,sequence);
        CREATE TABLE IF NOT EXISTS evidence(
          run_id TEXT NOT NULL, owner TEXT NOT NULL, case_index INTEGER NOT NULL,
          body TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(run_id,case_index));
        ''')
        self.db.commit()

    def setting(self, key):
        """Service-wide settings, not owned by any user."""
        with self.lock:
            row = self.db.execute('SELECT body FROM settings WHERE key=?', (key,)).fetchone()
            return json.loads(row['body']) if row else None

    def put_setting(self, key, body):
        with self.transaction():
            if body is None:
                self.db.execute('DELETE FROM settings WHERE key=?', (key,))
            else:
                self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?,?)', (key, canonical(body), time.time()))

    @contextmanager
    def transaction(self):
        with self.lock:
            try:
                self.db.execute('BEGIN IMMEDIATE')
                yield
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def encrypt(self, value):
        if not self.cipher:
            raise Failure('encryption_unconfigured', '请配置评测凭据加密密钥', 503, 'configuration')
        return self.cipher.encrypt(canonical(value).encode()).decode()

    def decrypt(self, value):
        if not self.cipher:
            raise Failure('encryption_unconfigured', '评测凭据加密密钥不可用', 503, 'configuration')
        return json.loads(self.cipher.decrypt(value.encode()))

    def lookup_request(self, owner, kind, request_id, identity):
        with self.lock:
            row = self.db.execute('SELECT * FROM requests WHERE owner=? AND operation=? AND request_id=?',
                                  (owner, kind, request_id)).fetchone()
            if row:
                if row['digest'] != digest(identity):
                    raise Failure('request_conflict', '同一请求标识对应不同输入', 409)
                return self.get(owner, row['record_id'], kind)
        return None

    def create(self, owner, kind, body, request_id=None, identity=None):
        with self.transaction():
            if request_id:
                row = self.db.execute('SELECT * FROM requests WHERE owner=? AND operation=? AND request_id=?',
                                      (owner, kind, request_id)).fetchone()
                if row:
                    if row['digest'] != digest(identity if identity is not None else body):
                        raise Failure('request_conflict', '同一请求标识对应不同输入', 409)
                    return self.get(owner, row['record_id'], kind)
            rid = uuid.uuid4().hex
            self.db.execute('INSERT INTO records VALUES(?,?,?,?,?,?)',
                            (rid, owner, kind, 1, canonical(body), time.time()))
            if request_id:
                self.db.execute('INSERT INTO requests VALUES(?,?,?,?,?)',
                                (owner, kind, request_id, digest(identity if identity is not None else body), rid))
            return dict(id=rid, revision=1, **body)

    def get(self, owner, rid, kind=None):
        with self.lock:
            row = self.db.execute('SELECT * FROM records WHERE id=? AND owner=?', (rid, owner)).fetchone()
            if not row or (kind and row['kind'] != kind):
                raise Failure('not_found', '记录不存在或无权访问', 404)
            return dict(id=row['id'], revision=row['revision'], **json.loads(row['body']))

    def save(self, owner, rid, body, expected=None):
        with self.transaction():
            old = self.get(owner, rid)
            if expected is not None and old['revision'] != expected:
                raise Failure('revision_conflict', '记录已变更，请刷新', 409)
            clean = {k: v for k, v in body.items() if k not in ('id', 'revision')}
            self.db.execute('UPDATE records SET body=?,revision=revision+1,updated=? WHERE id=? AND owner=?',
                            (canonical(clean), time.time(), rid, owner))
            return dict(id=rid, revision=old['revision']+1, **clean)

    def list(self, owner, kind, offset=0, limit=20):
        with self.lock:
            total = self.db.execute('SELECT count(*) FROM records WHERE owner=? AND kind=?', (owner,kind)).fetchone()[0]
            rows = self.db.execute('SELECT id FROM records WHERE owner=? AND kind=? ORDER BY updated DESC LIMIT ? OFFSET ?',
                                   (owner,kind,limit,offset)).fetchall()
            return dict(items=[self.get(owner,r['id']) for r in rows], total=total, offset=offset,
                        next_offset=offset+len(rows) if offset+len(rows)<total else None)

    def delete(self, owner, rid):
        with self.transaction():
            self.get(owner,rid)
            # Keep idempotency tombstones: a deleted request must never start again.
            self.db.execute('INSERT OR IGNORE INTO tombstones VALUES(?,?)',(rid,time.time()))
            self.db.execute('DELETE FROM records WHERE id=? AND owner=?',(rid,owner))
            self.db.execute('DELETE FROM artifacts WHERE run_id=?',(rid,))
            self.db.execute('DELETE FROM notifications WHERE run_id=?',(rid,))
            self.db.execute('DELETE FROM evidence WHERE run_id=?',(rid,))

    def prune_orphans(self):
        """Drop artifacts, notifications and evidence of runs that no longer exist, e.g. after a restore replayed deletions."""
        with self.transaction():
            for table in ('artifacts','notifications','evidence'):
                self.db.execute(f"DELETE FROM {table} WHERE run_id NOT IN (SELECT id FROM records WHERE kind='runs')")

    # Runs are grouped by the Hub package they evaluate; evaluation snapshots keep the package ID.
    PACKAGE="json_extract(body,'$.package_ref.ontology_id')"
    CREATED="json_extract(body,'$.created_at')"

    def package_groups(self, owner, offset=0, limit=20):
        """One entry per evaluated package, newest run first: (package, run count, latest run)."""
        with self.lock:
            where=f"FROM records WHERE owner=? AND kind='runs'"
            total=self.db.execute(f'SELECT count(DISTINCT {self.PACKAGE}) {where}',(owner,)).fetchone()[0]
            rows=self.db.execute(f'SELECT {self.PACKAGE} AS package,count(*) AS runs,max({self.CREATED}) AS latest {where} '
                                 'GROUP BY package ORDER BY latest DESC LIMIT ? OFFSET ?',(owner,limit,offset)).fetchall()
            items=[]
            for row in rows:
                latest=self.db.execute(f"SELECT id {where} AND {self.PACKAGE}=? ORDER BY {self.CREATED} DESC LIMIT 1",
                                       (owner,row['package'])).fetchone()
                items.append(dict(ontology_id=row['package'],run_count=row['runs'],latest=self.get(owner,latest['id'])))
            return dict(items=items,total=total,offset=offset,
                        next_offset=offset+len(rows) if offset+len(rows)<total else None)

    def package_runs(self, owner, package, offset=0, limit=20):
        with self.lock:
            where=f"FROM records WHERE owner=? AND kind='runs' AND {self.PACKAGE}=?"
            total=self.db.execute('SELECT count(*) '+where,(owner,package)).fetchone()[0]
            rows=self.db.execute(f'SELECT id {where} ORDER BY {self.CREATED} DESC LIMIT ? OFFSET ?',
                                 (owner,package,limit,offset)).fetchall()
            return dict(items=[self.get(owner,r['id']) for r in rows],total=total,offset=offset,
                        next_offset=offset+len(rows) if offset+len(rows)<total else None)

    def package_run_in_status(self, owner, package, statuses, exclude=''):
        """ID of another run of this package in one of the given statuses, if any."""
        with self.lock:
            marks=','.join('?'*len(statuses))
            row=self.db.execute(f"SELECT id FROM records WHERE owner=? AND kind='runs' AND {self.PACKAGE}=? AND id!=? "
                                f"AND json_extract(body,'$.status') IN ({marks}) LIMIT 1",(owner,package,exclude,*statuses)).fetchone()
            return row['id'] if row else None

    def package_run_rows(self, owner, package, limit=1000):
        """Small fields of a package's runs, newest first, without loading their cases."""
        with self.lock:
            rows=self.db.execute(f"SELECT id,json_extract(body,'$.status') AS status,json_extract(body,'$.base_ref') AS base_ref,"
                                 f"json_extract(body,'$.package_ref') AS package_ref,json_extract(body,'$.preparation_id') AS preparation_id,"
                                 f"{self.CREATED} AS created_at,json_extract(body,'$.finished_at') AS finished_at "
                                 f"FROM records WHERE owner=? AND kind='runs' AND {self.PACKAGE}=? ORDER BY {self.CREATED} DESC LIMIT ?",
                                 (owner,package,limit)).fetchall()
            return [dict(id=r['id'],status=r['status'],base_ref=json.loads(r['base_ref']) if r['base_ref'] else None,
                         package_ref=json.loads(r['package_ref']) if r['package_ref'] else None,preparation_id=r['preparation_id'],
                         created_at=r['created_at'],finished_at=r['finished_at']) for r in rows]

    def artifact(self, owner, run_id, name):
        with self.lock:
            row=self.db.execute('SELECT revision,body FROM artifacts WHERE run_id=? AND owner=? AND name=?',(run_id,owner,name)).fetchone()
            return (row['revision'],row['body']) if row else (None,None)

    def put_artifacts(self, owner, run_id, revision, values):
        """Write several artifacts of one revision atomically, so readers never see a mixed set."""
        with self.transaction():
            if not self.db.execute("SELECT 1 FROM records WHERE id=? AND owner=? AND kind='runs'",(run_id,owner)).fetchone():
                raise Failure('not_found','记录不存在或无权访问',404)
            for name,body in values.items():
                self.db.execute('INSERT OR REPLACE INTO artifacts VALUES(?,?,?,?,?,?)',(run_id,owner,name,revision,body,time.time()))

    def put_evidence(self, owner, run_id, case_index, value):
        """Bounded tool-call evidence of one settled case; a reconciled case replaces its earlier evidence."""
        with self.transaction():
            if not self.db.execute("SELECT 1 FROM records WHERE id=? AND owner=? AND kind='runs'",(run_id,owner)).fetchone():
                raise Failure('not_found','记录不存在或无权访问',404)
            self.db.execute('INSERT OR REPLACE INTO evidence VALUES(?,?,?,?,?)',
                            (run_id,owner,case_index,canonical(value),time.time()))

    def evidence(self, owner, run_id):
        """Evidence of a run by zero-based case index; cases settled before evidence was kept are absent."""
        with self.lock:
            rows=self.db.execute('SELECT case_index,body FROM evidence WHERE run_id=? AND owner=?',(run_id,owner)).fetchall()
            return {row['case_index']:json.loads(row['body']) for row in rows}

    def recover(self):
        with self.transaction():
            for row in self.db.execute("SELECT * FROM records WHERE kind='runs'").fetchall():
                body=json.loads(row['body'])
                if body.get('status') in ('queued','preparing','running','cancelling'):
                    body['status']='interrupted'
                    body['message']='服务已重启，请重新授权并对账恢复'
                    self.db.execute('UPDATE records SET body=?,revision=revision+1 WHERE id=?',(canonical(body),row['id']))

    def backup(self, target):
        target=Path(target)
        if target.exists():
            raise Failure('backup_exists','备份目标必须不存在',409)
        with self.lock:
            destination=sqlite3.connect(target)
            try:
                self.db.backup(destination)
            finally:
                destination.close()

    def prune_inputs(self, now=None):
        """Keep frozen/definition references; remove only unreferenced uploads older than 24h."""
        now=now or time.time()
        with self.transaction():
            referenced=set()
            for row in self.db.execute('SELECT body FROM records WHERE kind IN (?,?,?)',
                                       ('data-sources','preparations','runs')).fetchall():
                body=json.loads(row['body'])
                referenced.update(body.get('input_ids',[]))
                referenced.update(body.get('source',{}).get('input_ids',[]))
            removed=0
            for row in self.db.execute("SELECT id,body FROM records WHERE kind='inputs'").fetchall():
                if row['id'] not in referenced and json.loads(row['body']).get('created',now)<now-86400:
                    self.db.execute('DELETE FROM records WHERE id=?',(row['id'],))
                    removed+=1
            return removed
