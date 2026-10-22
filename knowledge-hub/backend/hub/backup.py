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


def restore(source, destination, deletion_records):
    destination = Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise HubError('Restore requires an empty destination')
    if not Path(deletion_records).is_dir():
        raise HubError('Current Onto deletion records are required')
    shutil.copytree(source, destination, dirs_exist_ok=True)
    store = Store(destination)
    with store.lock():
        for record in Path(deletion_records).glob('*.json'):
            store.delete(record.stem)
        store.recover_all()
    return destination


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['backup', 'restore'])
    parser.add_argument('source')
    parser.add_argument('destination')
    parser.add_argument('--deletion-records')
    args = parser.parse_args()
    if args.operation == 'backup':
        backup(args.source, args.destination)
    elif not args.deletion_records:
        parser.error('restore requires --deletion-records from current Onto hub-tasks/cleanup')
    else:
        restore(args.source, args.destination, args.deletion_records)


if __name__ == '__main__':
    main()
