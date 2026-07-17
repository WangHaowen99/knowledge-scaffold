import { getEvalDbUrl } from '../settings/repo.js';
import type { DataDictTable } from '../types.js';

// Excel → 评测 MySQL 落库（design.md 决策 5 第三段）：
// 每个项目一个独立 schema（mw_proj_<id>），自动 DDL + 灌数。
// 外部 NL2SQL / MindsDB 服务指向该库后，consumer 的知识包数据源即可查到评测数据。
// best-effort：EVAL_DB_URL 未配置或不可达时返回说明而不阻断评测。
const TYPE_MAP: Record<string, string> = {
  number: 'DOUBLE', boolean: 'TINYINT(1)', date: 'DATETIME', string: 'VARCHAR(512)', unknown: 'VARCHAR(512)',
};

const ident = (s: string) => '`' + s.replace(/`/g, '') + '`';

export function evalSchemaName(projectId: string): string {
  return `mw_proj_${projectId.replace(/[^a-zA-Z0-9_]/g, '_')}`.slice(0, 60);
}

export async function provisionEvalDb(projectId: string, tables: DataDictTable[]): Promise<{ ok: boolean; schema?: string; detail: string }> {
  const evalDbUrl = getEvalDbUrl();
  if (!evalDbUrl) return { ok: false, detail: '未配置评测库地址（配置中心或 EVAL_DB_URL），跳过落库' };
  const withRows = tables.filter((t) => t.rows?.length);
  if (!withRows.length) return { ok: false, detail: '数据字典无全量行（旧项目需重新上传 Excel），跳过落库' };

  let mysql: typeof import('mysql2/promise');
  try {
    mysql = await import('mysql2/promise');
  } catch {
    return { ok: false, detail: 'mysql2 未安装，跳过落库' };
  }

  const schema = evalSchemaName(projectId);
  let conn: import('mysql2/promise').Connection | null = null;
  try {
    conn = await mysql.createConnection(evalDbUrl);
    await conn.query(`DROP DATABASE IF EXISTS ${ident(schema)}`);
    await conn.query(`CREATE DATABASE ${ident(schema)} CHARACTER SET utf8mb4`);
    await conn.query(`USE ${ident(schema)}`);
    // 布尔列的中文/英文枚举值 → 0/1（'是' 直插 TINYINT 会报错）
    const BOOL_MAP: Record<string, string> = { '是': '1', '否': '0', 'true': '1', 'false': '0', 'yes': '1', 'no': '0', 'y': '1', 'n': '0' };
    const coerce = (v: string, type: string) => {
      if (v === '') return null;
      if (type === 'boolean') return BOOL_MAP[v.toLowerCase()] ?? BOOL_MAP[v] ?? v;
      return v;
    };
    let rowTotal = 0;
    let okTables = 0;
    const failed: string[] = [];
    for (const t of withRows) {
      // 按表隔离：单表失败只跳过该表，不拖垮整个评测库
      try {
        const cols = t.fields.map((f) => `${ident(f.name)} ${TYPE_MAP[f.inferredType] ?? 'VARCHAR(512)'} COMMENT ${JSON.stringify(f.note ?? '')}`);
        await conn.query(`CREATE TABLE ${ident(t.name)} (${cols.join(', ')})`);
        const rows = t.rows!;
        for (let i = 0; i < rows.length; i += 200) {
          const chunk = rows.slice(i, i + 200);
          const placeholders = chunk.map(() => `(${t.fields.map(() => '?').join(',')})`).join(',');
          const values = chunk.flatMap((r) => t.fields.map((f, c) => coerce(String(r[c] ?? ''), f.inferredType)));
          await conn.query(`INSERT INTO ${ident(t.name)} VALUES ${placeholders}`, values);
        }
        rowTotal += rows.length;
        okTables++;
      } catch (e) {
        failed.push(`${t.name}（${e instanceof Error ? e.message.slice(0, 80) : e}）`);
      }
    }
    const failNote = failed.length ? `；失败 ${failed.length} 表：${failed.join('、')}` : '';
    return { ok: okTables > 0, schema, detail: `评测库就绪：schema=${schema}，${okTables} 张表 / ${rowTotal} 行${failNote}` };
  } catch (e) {
    return { ok: false, schema, detail: `评测库落库失败：${e instanceof Error ? e.message : e}` };
  } finally {
    await conn?.end().catch(() => {});
  }
}
