"""Feedback signals, votes and problem clusters in evaluation.sqlite.

Signal bodies and anything derived from them are encrypted; the plain columns are identifiers, states, counts and
fingerprints, so lists and thresholds work without decrypting user content. A signal body is cleared once its
retention ends; counts and clusters stay.
"""
import hashlib
import time
import uuid

from ..store import FEEDBACK_OWNER

# Information a cluster's signals carry, from least to most: only a thumbs-down, a reason or a detected error,
# an expected answer, a correct statement or a suggestion.
LEVELS = {'positive': 0, 'L1': 1, 'L2': 2, 'L3': 3}


def run_owner(ontology_id):
    """The owner key of a package's feedback runs; it belongs to no user."""
    return FEEDBACK_OWNER + hashlib.sha256(ontology_id.encode()).hexdigest()[:32]


class FeedbackStore:
    def __init__(self, store):
        self.store = store
        self.db = store.db

    def insert(self, signal_id, actor_key, columns, payload_hash, body, now, purge_after):
        """accepted or duplicate with the intake ID, or conflict when the same ID came with other content."""
        with self.store.transaction():
            row = self.db.execute('SELECT rowid,payload_hash FROM feedback_signals WHERE signal_id=?', (signal_id,)).fetchone()
            if row:
                return ('duplicate' if row['payload_hash'] == payload_hash else 'conflict'), f"in_{row['rowid']}"
            cursor = self.db.execute(
                'INSERT INTO feedback_signals(signal_id,actor_key,signal_type,signal_seq,detected_by,vote,run_key,ontology_id,'
                'package_version,processing_status,payload_hash,body,received,updated,purge_after) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (signal_id, actor_key, columns['signal_type'], columns['signal_seq'], columns.get('detected_by'), columns.get('vote'),
                 columns.get('run_key'), columns.get('ontology_id'), columns.get('package_version'), 'received', payload_hash,
                 self.store.encrypt(body), now, now, purge_after))
            return 'accepted', f'in_{cursor.lastrowid}'

    def received(self, limit):
        with self.store.lock:
            rows = self.db.execute("SELECT * FROM feedback_signals WHERE processing_status='received' ORDER BY received LIMIT ?",
                                   (limit,)).fetchall()
        return [dict(row) for row in rows]

    def body(self, row):
        return self.store.decrypt(row['body']) if row.get('body') else None

    def derived(self, row):
        return self.store.decrypt(row['derived']) if row.get('derived') else {}

    def mark(self, signal_id, now=None, derived=None, **columns):
        fields = dict(columns, updated=now or time.time())
        if derived is not None:
            fields['derived'] = self.store.encrypt(derived)
        with self.store.transaction():
            self.db.execute(f"UPDATE feedback_signals SET {','.join(k + '=?' for k in fields)} WHERE signal_id=?",
                            (*fields.values(), signal_id))

    def vote(self, run_key, actor_key, seq, vote, reason, signal_id, now):
        """Record the newest vote on one consumer run. Returns the signal it replaces, or False when this vote is older."""
        with self.store.transaction():
            row = self.db.execute('SELECT * FROM feedback_votes WHERE run_key=?', (run_key,)).fetchone()
            if row and row['latest_seq'] >= seq:
                return False
            self.db.execute('INSERT OR REPLACE INTO feedback_votes VALUES(?,?,?,?,?,?,?)',
                            (run_key, actor_key, seq, vote, reason, signal_id, now))
            return row['signal_id'] if row else None

    def cluster(self, cluster_id):
        with self.store.lock:
            row = self.db.execute('SELECT * FROM feedback_clusters WHERE cluster_id=?', (cluster_id,)).fetchone()
        return self.decoded(row)

    def decoded(self, row):
        if not row:
            return None
        value = dict(row)
        value['body'] = self.store.decrypt(value['body'])
        return value

    def by_fingerprint(self, ontology_id, version, kind, fingerprint):
        with self.store.lock:
            row = self.db.execute('SELECT * FROM feedback_clusters WHERE ontology_id=? AND package_version=? AND cluster_kind=? '
                                  'AND fingerprint=?', (ontology_id, version, kind, fingerprint)).fetchone()
        return self.decoded(row)

    def open_clusters(self, ontology_id, version, kind, limit=20):
        """Clusters a new signal may still join: not yet taken into a feedback run, newest first."""
        with self.store.lock:
            rows = self.db.execute("SELECT * FROM feedback_clusters WHERE ontology_id=? AND package_version=? AND cluster_kind=? "
                                   "AND status IN ('open','ready') ORDER BY latest_signal DESC LIMIT ?",
                                   (ontology_id, version, kind, limit)).fetchall()
        return [self.decoded(row) for row in rows]

    def create_cluster(self, ontology_id, version, kind, fingerprint, body, now):
        cluster_id = 'cl_' + uuid.uuid4().hex[:24]
        with self.store.transaction():
            self.db.execute('INSERT INTO feedback_clusters(cluster_id,ontology_id,package_version,cluster_kind,fingerprint,level,status,'
                            'body,first_signal,latest_signal,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                            (cluster_id, ontology_id, version, kind, fingerprint, 'positive', 'open', self.store.encrypt(body),
                             now, now, now))
        return cluster_id

    def members(self, cluster_id):
        with self.store.lock:
            rows = self.db.execute("SELECT * FROM feedback_signals WHERE cluster_id=? AND processing_status='clustered' "
                                   'ORDER BY received', (cluster_id,)).fetchall()
        return [dict(row) for row in rows]

    def update_cluster(self, cluster_id, body=None, **columns):
        fields = dict(columns, updated=time.time())
        if body is not None:
            fields['body'] = self.store.encrypt(body)
        with self.store.transaction():
            self.db.execute(f"UPDATE feedback_clusters SET {','.join(k + '=?' for k in fields)} WHERE cluster_id=?",
                            (*fields.values(), cluster_id))

    def clusters(self, where, values=()):
        with self.store.lock:
            rows = self.db.execute(f'SELECT * FROM feedback_clusters WHERE {where}', values).fetchall()
        return [self.decoded(row) for row in rows]

    def latest_version(self, ontology_id):
        """The package version feedback last arrived for; suggestions without a version join it."""
        with self.store.lock:
            row = self.db.execute("SELECT package_version FROM feedback_signals WHERE ontology_id=? AND package_version!='' "
                                  'ORDER BY received DESC LIMIT 1', (ontology_id,)).fetchone()
        return row['package_version'] if row else ''

    def release(self, run_id):
        """A deleted feedback run gives its clusters back, ready for the next run of their package and version.

        Returns the clusters to refresh."""
        touched = []
        for cluster in self.clusters('run_id=?', (run_id,)):
            base = cluster['fingerprint'].split(':', 1)[0]
            newer = self.by_fingerprint(cluster['ontology_id'], cluster['package_version'], cluster['cluster_kind'], base)
            with self.store.transaction():
                if newer and newer['cluster_id'] != cluster['cluster_id']:
                    # Feedback on the same question arrived meanwhile: one cluster again.
                    self.db.execute('UPDATE feedback_signals SET cluster_id=? WHERE cluster_id=?',
                                    (newer['cluster_id'], cluster['cluster_id']))
                    self.db.execute('DELETE FROM feedback_clusters WHERE cluster_id=?', (cluster['cluster_id'],))
                    touched.append(newer['cluster_id'])
                else:
                    self.db.execute("UPDATE feedback_clusters SET fingerprint=?,status='ready',run_id=NULL,updated=? "
                                    'WHERE cluster_id=?', (base, time.time(), cluster['cluster_id']))
                    touched.append(cluster['cluster_id'])
        return touched

    def prune(self, now=None):
        """Clear signal bodies whose retention ended; their counts and clusters stay. Returns how many were cleared."""
        now = now or time.time()
        with self.store.transaction():
            cursor = self.db.execute('UPDATE feedback_signals SET body=NULL,derived=NULL,updated=? '
                                     'WHERE purge_after<? AND body IS NOT NULL', (now, now))
            return cursor.rowcount

    def feedback_runs(self, ontology_id, statuses=None):
        """Feedback runs of a package, newest first."""
        owner = run_owner(ontology_id)
        where = "owner=? AND kind='runs'"
        values = [owner]
        if statuses:
            where += f" AND json_extract(body,'$.status') IN ({','.join('?' * len(statuses))})"
            values += list(statuses)
        with self.store.lock:
            rows = self.db.execute(f"SELECT id FROM records WHERE {where} ORDER BY json_extract(body,'$.created_at') DESC",
                                   values).fetchall()
        return [self.store.get(owner, row['id'], 'runs') for row in rows]

    def collecting(self):
        """Every feedback run still collecting, with its owner."""
        with self.store.lock:
            rows = self.db.execute("SELECT id,owner FROM records WHERE kind='runs' AND owner LIKE 'feedback-%' "
                                   "AND json_extract(body,'$.status')='collecting'").fetchall()
        return [(row['owner'], self.store.get(row['owner'], row['id'], 'runs')) for row in rows]

    def packages(self):
        """Packages with feedback runs: (package, run count, newest run ID)."""
        with self.store.lock:
            rows = self.db.execute("SELECT json_extract(body,'$.package_ref.ontology_id') AS package,count(*) AS runs,"
                                   "max(json_extract(body,'$.created_at')) AS latest FROM records WHERE kind='runs' "
                                   "AND owner LIKE 'feedback-%' GROUP BY package ORDER BY latest DESC").fetchall()
        return [(row['package'], row['runs']) for row in rows]

