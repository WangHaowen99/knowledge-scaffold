"""Offline consistent backup/restore. Runtime credentials are never backed up here."""
import argparse
from pathlib import Path
import shutil
import sqlite3
import fcntl

from .core import Failure
from .store import Store


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['backup','restore'])
    parser.add_argument('--data-dir',required=True,type=Path)
    parser.add_argument('--snapshot',required=True,type=Path)
    parser.add_argument('--deletion-records',type=Path)
    args=parser.parse_args()
    if args.action=='backup':
        store=Store(args.data_dir)
        try: store.backup(args.snapshot)
        finally: store.db.close()
        return
    if not args.deletion_records:
        parser.error('restore requires the latest database as --deletion-records')
    if args.data_dir.exists() and any(args.data_dir.iterdir()):
        raise Failure('restore_not_empty','恢复目录必须为空',409)
    with sqlite3.connect('file:'+str(args.deletion_records.resolve())+'?mode=ro',uri=True) as current:
        deleted=current.execute('SELECT id,deleted FROM tombstones').fetchall()
    args.data_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=args.data_dir/'evaluation.sqlite'
    with sqlite3.connect('file:'+str(args.snapshot.resolve())+'?mode=ro',uri=True) as backup:
        with sqlite3.connect(target) as restored:
            backup.backup(restored)
            restored.executemany('DELETE FROM records WHERE id=?',[(rid,) for rid,_ in deleted])
            restored.executemany('INSERT OR REPLACE INTO tombstones VALUES(?,?)',deleted)
            restored.commit()
            if restored.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise Failure('restore_corrupt','恢复数据校验失败',409)
    store=Store(args.data_dir)
    try:
        store.recover()
        store.prune_orphans()
    finally: store.db.close()


if __name__=='__main__': main()
