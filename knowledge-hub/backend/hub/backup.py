"""Offline backup/restore. Restore into an empty volume and replay deletion records."""
import argparse
import shutil
from pathlib import Path

from .storage import Store, HubError


def backup(source, destination):
    store = Store(source)
    destination = Path(destination)
    if destination.exists():
        raise HubError('Backup destination must not exist')
    with store.lock():
        store.recover_all()
        shutil.copytree(store.root, destination, ignore=shutil.ignore_patterns('.lock', '.write-*'))
    return destination


def restore(source, destination, current_metadata):
    destination = Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise HubError('Restore requires an empty destination')
    if not Path(current_metadata).is_dir():
        raise HubError('Current Hub package metadata directory is required')
    shutil.copytree(source, destination, dirs_exist_ok=True)
    store = Store(destination)
    with store.lock():
        from .storage import read_json, write_json
        # Replay current tombstones and ACLs so an older backup cannot restore revoked access.
        for record in Path(current_metadata).glob('*/metadata.json'):
            meta = read_json(record)
            target = store.root / 'packages' / record.parent.name / 'metadata.json'
            if target.exists() or meta.get('state') == 'deleted':
                write_json(target, meta)
        store.recover_all()
    return destination


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['backup', 'restore'])
    parser.add_argument('source')
    parser.add_argument('destination')
    parser.add_argument('--current-metadata')
    args = parser.parse_args()
    if args.operation == 'backup':
        backup(args.source, args.destination)
    elif not args.current_metadata:
        parser.error('restore requires --current-metadata from current Hub packages directory')
    else:
        restore(args.source, args.destination, args.current_metadata)


if __name__ == '__main__':
    main()
