import { DatabaseSync } from 'node:sqlite';
import { readFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { config } from '../config.js';

// 用 Node 内置 node:sqlite（零原生编译）。API 近似 better-sqlite3：
// exec / prepare().run|get|all。
const here = dirname(fileURLToPath(import.meta.url));

let _db: DatabaseSync | null = null;

export function getDb(): DatabaseSync {
  if (_db) return _db;
  mkdirSync(config.dataDir, { recursive: true });
  const db = new DatabaseSync(join(config.dataDir, 'modeling.db'));
  db.exec('PRAGMA journal_mode = WAL');
  db.exec('PRAGMA foreign_keys = ON');
  // schema.sql 随源码走：dev 从 src/db 读，编译后 build 脚本拷到 dist/db。
  const schema = readFileSync(join(here, 'schema.sql'), 'utf8');
  db.exec(schema);
  _db = db;
  return db;
}

export function nowIso(): string {
  return new Date().toISOString();
}
