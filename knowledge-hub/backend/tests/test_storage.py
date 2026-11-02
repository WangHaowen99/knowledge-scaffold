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
        self.pid = 'ontology_demo'
        self.subject = 'operator_a'

    def tearDown(self):
        self.temp.cleanup()

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
        self.sync(1, 'synthetic old')
        result = self.sync(2, 'synthetic new')
        self.assertEqual(result['generation'], 2)
        self.assertEqual(self.sync(2, 'synthetic new'), result)
        with self.assertRaises(HubError):
            self.sync(1, 'synthetic old')
        with self.assertRaises(HubError):
            self.sync(2, 'synthetic conflict')
        root = (self.store.root / 'packages' / self.pid)
        self.assertEqual((root / 'resources-current/resource/others/demo.txt').read_text(), 'synthetic new')
        self.assertNotIn(b'resources', self.store.git(root, 'ls-files'))
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

    def test_clear_blocks_late_upload_and_restore_preserves_empty_generation(self):
        self.sync(1, 'synthetic current')
        cleared = self.store.clear_resources(self.pid, 2)
        self.assertEqual(self.store.clear_resources(self.pid, 2), cleared)
        with self.assertRaises(HubError):
            self.sync(1, 'synthetic late')
        with self.assertRaises(HubError):
            self.sync(2, 'synthetic conflict')
        snapshot = backup(self.store.root, self.root / 'backup')
        restored = restore(snapshot, self.root / 'restored', self.store.root / 'packages')
        value = json.loads((restored / 'packages' / self.pid / 'resources-current/receipt.json').read_text())
        self.assertTrue(value['empty'])
        self.assertEqual(value['generation'], 2)

    def test_backup_restore_preserves_release_and_latest_materials(self):
        self.sync(1, 'synthetic current')
        backup(self.store.root, self.root / 'backup')
        deletions = self.root / 'deletions'
        deletions.mkdir()
        restore(self.root / 'backup', self.root / 'restored', deletions)
        restored = Store(self.root / 'restored')
        self.assertEqual(((restored.root / 'packages' / self.pid) / 'resources-current/resource/others/demo.txt').read_text(), 'synthetic current')


if __name__ == '__main__':
    unittest.main()
