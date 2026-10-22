"""All archives and identities below are synthetic and created in temporary directories."""
import io
import json
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from hub.storage import Store, HubError, digest
from hub.backup import backup, restore


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'hub')
        self.payload = {'source_digest': 'a' * 64}
        self.pid = 'project-alpha'
        self.subject = 'operator_a'

    def tearDown(self):
        self.temp.cleanup()

    def reserve(self, rid='request-a', **kwargs):
        with self.store.lock():
            return self.store.reserve(self.pid, rid, {**self.payload, **kwargs}, self.subject)

    def archive(self, receipt, name='package.zip'):
        path = self.root / name
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr(f'{self.pid}/manifest.yaml',
                       f"kind: ontology-package\nschema_version: '0.1'\nid: {self.pid}\n"
                       f"version: {receipt['version']}\nname: knowledge_package_demo\ndescription: synthetic\n")
            for item in ('objects/index.md', 'operations/index.md'):
                z.writestr(f'{self.pid}/{item}', '# Synthetic index')
        return path

    def publish(self, rid='request-a', **kwargs):
        receipt = self.reserve(rid, **kwargs)
        path = self.archive(receipt)
        with path.open('rb') as f, self.store.lock():
            self.store.upload(self.pid, rid, self.subject, f, digest(path))
            return self.store.commit(self.pid, rid, self.subject)

    def test_idempotent_restart_and_immutable_download(self):
        receipt = self.publish()
        self.store = Store(self.root / 'hub')
        self.assertEqual(self.reserve()['release_id'], receipt['release_id'])
        self.assertEqual(self.store.commit(self.pid, 'request-a', self.subject), receipt)
        found, path = self.store.release(self.pid, receipt['release_id'])
        self.assertEqual(digest(path), found['package_digest'])
        self.assertEqual(self.store.listing(self.pid)['total'], 1)
        with self.assertRaises(HubError):
            self.reserve(source_digest='b' * 64)

    def test_expired_request_never_reuses_a_version(self):
        first = self.reserve()
        self.store.pending_ttl = -1
        with self.assertRaises(HubError) as raised:
            self.reserve()
        self.assertEqual(raised.exception.status, 410)
        self.store.pending_ttl = 7 * 86400
        second = self.reserve('request-b')
        self.assertNotEqual(first['version'], second['version'])

    def test_more_than_50_and_concurrent_reservations(self):
        def reserve(i):
            return self.reserve(f'request-{i}')['version']
        with ThreadPoolExecutor(max_workers=8) as pool:
            versions = list(pool.map(reserve, range(60)))
        self.assertEqual(len(set(versions)), 60)
        self.assertIn('0.1.60', versions)
        self.assertEqual(self.store.listing(self.pid)['total'], 0)

    def test_missing_content_and_subject_are_rejected(self):
        self.reserve()
        with self.assertRaises(HubError):
            self.store.commit(self.pid, 'request-a', self.subject)
        with self.assertRaises(HubError):
            self.store.request(self.pid, 'request-a', 'operator_b')
        self.assertEqual(self.store.listing(self.pid)['total'], 0)

    def test_snapshots_do_not_enter_release_list(self):
        receipt = self.publish(kind='evaluation_snapshot')
        self.assertEqual(self.store.listing(self.pid)['total'], 0)
        self.assertTrue(self.store.release(self.pid, receipt['release_id'])[1].exists())

    def test_git_failure_recovery(self):
        receipt = self.reserve()
        path = self.archive(receipt)
        with path.open('rb') as f:
            self.store.upload(self.pid, 'request-a', self.subject, f, digest(path))
        original = self.store.git
        def fail_commit(root, *args):
            if args[0] == 'commit':
                raise HubError('Synthetic journal failure', 503)
            return original(root, *args)
        with patch.object(self.store, 'git', fail_commit):
            with self.assertRaises(HubError):
                self.store.commit(self.pid, 'request-a', self.subject)
        self.store = Store(self.root / 'hub')
        self.store.recover_all()
        self.assertEqual(self.store.listing(self.pid)['total'], 1)

    def test_pending_receipt_commit_crash_is_recoverable(self):
        receipt = self.reserve()
        path = self.archive(receipt)
        with path.open('rb') as f:
            self.store.upload(self.pid, 'request-a', self.subject, f, digest(path))
        with patch('hub.storage.os.replace', side_effect=OSError('synthetic crash')):
            with self.assertRaises(OSError):
                self.store.commit(self.pid, 'request-a', self.subject)
        self.assertEqual(self.store.commit(self.pid, 'request-a', self.subject)['status'], 'committed')

    def resource_archive(self, content=None):
        path = self.root / 'resources.zip'
        with zipfile.ZipFile(path, 'w') as z:
            if content is not None:
                z.writestr('resource/others/demo.txt', content)
            z.writestr('resource/materials.json', '{}')
        return path

    def sync(self, generation, content=None):
        path = self.resource_archive(content)
        with path.open('rb') as f, self.store.lock():
            return self.store.resources(self.pid, generation, f, digest(path))

    def test_latest_resources_replace_clear_and_no_git_history(self):
        self.publish()
        self.sync(1, 'synthetic old')
        result = self.sync(2, 'synthetic new')
        self.assertEqual(result['generation'], 2)
        self.assertEqual(self.sync(2, 'synthetic new'), result)
        with self.assertRaises(HubError):
            self.sync(1, 'synthetic old')
        with self.assertRaises(HubError):
            self.sync(2, 'synthetic conflict')
        root = self.store.project(self.pid)
        self.assertEqual((root / 'resources-current/resource/others/demo.txt').read_text(), 'synthetic new')
        self.assertNotIn(b'resources', self.store.git(root, 'log', '--all', '--name-only', '--format='))
        self.sync(3)
        self.assertFalse((root / 'resources-current/resource/others/demo.txt').exists())
        self.assertFalse(list(root.glob('.resources-*')))

    def test_traversal_link_duplicate_and_limits(self):
        for names in (['../escape'], ['/absolute'], ['resource/others/a', 'resource/others/a'],
                      ['resource/others/a', 'resource/others/a/b']):
            path = self.root / 'bad.zip'
            with zipfile.ZipFile(path, 'w') as z:
                for name in names:
                    z.writestr(name, 'synthetic')
            with self.assertRaises(HubError):
                self.store.inspect_zip(path)
        path = self.root / 'link.zip'
        with zipfile.ZipFile(path, 'w') as z:
            info = zipfile.ZipInfo('resource/others/link')
            info.external_attr = 0o120777 << 16
            z.writestr(info, 'target')
        with self.assertRaises(HubError):
            self.store.inspect_zip(path)
        self.store.max_bytes = 3
        with self.assertRaises(HubError):
            self.store.copy_upload(io.BytesIO(b'1234'), self.root / 'upload')

    def test_delete_blocks_late_commit_and_restore_replays_tombstones(self):
        self.publish()
        self.sync(1, 'synthetic current')
        destination = backup(self.store.root, self.root / 'backup')
        self.store.delete(self.pid)
        self.store.delete(self.pid)
        with self.assertRaises(HubError):
            self.store.commit(self.pid, 'request-a', self.subject)
        restore(destination, self.root / 'restored', self.store.root / 'cleanup')
        restored = Store(self.root / 'restored')
        with self.assertRaises(HubError):
            restored.listing(self.pid)
        self.assertFalse((restored.root / 'projects' / self.pid).exists())

    def test_backup_restore_preserves_release_and_latest_materials(self):
        receipt = self.publish()
        self.sync(1, 'synthetic current')
        backup(self.store.root, self.root / 'backup')
        deletions = self.root / 'deletions'
        deletions.mkdir()
        restore(self.root / 'backup', self.root / 'restored', deletions)
        restored = Store(self.root / 'restored')
        self.assertEqual(restored.release(self.pid, receipt['release_id'])[0], receipt)
        self.assertEqual((restored.project(self.pid) / 'resources-current/resource/others/demo.txt').read_text(), 'synthetic current')


if __name__ == '__main__':
    unittest.main()
