/**
 * 轻量 NL2SQL 服务：兼容 consumer 的 nl2sql_template / nl2sql_http provider 契约。
 *   POST /api/v1/query  {question, scene?}  → LLM 生成 SELECT → 查 MySQL → {success,data,row_count,sql}
 *   POST /api/v1/sql    {sql, scene?}       → 直接执行（仅 SELECT/WITH）
 *   GET  /health
 * 约定：scene = MySQL 数据库名（如 example_domain 或 modeling 评测库 mw_proj_*）。
 * 鉴权：设了 NL2SQL_TOKEN 时校验 Authorization: Bearer <token>。
 * 业务错误一律 200 + {success:false,error}（consumer 只对 5xx 重试，避免重试风暴）。
 * 启动：tsx --env-file=.env scripts/nl2sql-service.ts
 *   （需 .env 含 MODELING_LLM_BASE_URL/MODEL/API_KEY 与 EVAL_DB_URL）
 */
import { createServer } from 'node:http';
import { config } from '../src/config.js';

const PORT = Number(process.env.NL2SQL_PORT ?? 5600);
const TOKEN = process.env.NL2SQL_TOKEN ?? '';
const DEFAULT_SCENE = process.env.NL2SQL_DEFAULT_SCENE ?? 'example_domain';
const MAX_ROWS = 200;

if (!config.evalDbUrl) { console.error('缺 EVAL_DB_URL'); process.exit(1); }
if (!config.llm.baseUrl || config.llm.baseUrl === 'faux' || !config.llm.model) {
  console.error('缺真实 LLM 配置（MODELING_LLM_BASE_URL/MODEL）'); process.exit(1);
}

// ---- MySQL ----
let poolPromise: Promise<import('mysql2/promise').Pool> | null = null;
async function pool() {
  if (!poolPromise) {
    poolPromise = import('mysql2/promise').then((m) =>
      m.createPool({ uri: config.evalDbUrl, connectionLimit: 5, multipleStatements: false }));
  }
  return poolPromise;
}

const okScene = (s: string) => /^[a-zA-Z0-9_]{1,60}$/.test(s);

// schema 快照（60s 缓存）：表 + 列 + 每表 2 行样例，供 LLM 生成 SQL
const schemaCache = new Map<string, { at: number; text: string }>();
async function schemaText(scene: string): Promise<string> {
  const hit = schemaCache.get(scene);
  if (hit && Date.now() - hit.at < 60_000) return hit.text;
  const p = await pool();
  const [cols] = await p.query(
    `SELECT TABLE_NAME t, COLUMN_NAME c FROM information_schema.columns
     WHERE table_schema = ? ORDER BY TABLE_NAME, ORDINAL_POSITION`, [scene]) as any;
  const byTable = new Map<string, string[]>();
  for (const r of cols) {
    if (!byTable.has(r.t)) byTable.set(r.t, []);
    byTable.get(r.t)!.push(r.c);
  }
  if (!byTable.size) throw new Error(`scene（数据库）不存在或为空：${scene}`);
  const parts: string[] = [];
  for (const [t, cs] of byTable) {
    parts.push(`${t}(${cs.join(', ')})`);
    try {
      const [rows] = await p.query(`SELECT * FROM \`${scene}\`.\`${t}\` LIMIT 2`) as any;
      if (rows.length) parts.push(`  样例: ${JSON.stringify(rows).slice(0, 400)}`);
    } catch { /* 样例失败不阻断 */ }
  }
  const text = parts.join('\n').slice(0, 60_000);
  schemaCache.set(scene, { at: Date.now(), text });
  return text;
}

// ---- LLM 生成 SQL（直连 OpenAI-compatible，非 agent，快）----
async function genSql(question: string, scene: string): Promise<string> {
  const schema = await schemaText(scene);
  const res = await fetch(`${config.llm.baseUrl}/chat/completions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${config.llm.apiKey ?? ''}` },
    body: JSON.stringify({
      model: config.llm.model,
      temperature: 0,
      messages: [
        { role: 'system', content: [
          '你是 NL2SQL 引擎。根据 MySQL 库结构，把问题转成一条 SELECT 语句。',
          '规则：只输出 SQL 本身（不要解释、不要 markdown 围栏）；只允许 SELECT；',
          '所有列均为 TEXT 类型，数值比较/聚合时用 CAST(x AS DECIMAL(18,4))；',
          `表都在数据库 ${scene} 中，表名不必带库名前缀；结果行数不超过 ${MAX_ROWS}。`,
        ].join('\n') },
        { role: 'user', content: `库结构：\n${schema}\n\n问题：${question}\n\nSQL：` },
      ],
    }),
  });
  const data = (await res.json().catch(() => ({}))) as any;
  if (!res.ok) throw new Error(`LLM ${res.status}: ${JSON.stringify(data).slice(0, 200)}`);
  let sql = String(data.choices?.[0]?.message?.content ?? '').trim();
  sql = sql.replace(/^```(?:sql)?\s*/i, '').replace(/```\s*$/, '').trim();
  if (!sql) throw new Error('LLM 未产出 SQL');
  return sql;
}

async function runSelect(sql: string, scene: string): Promise<{ rows: any[]; sql: string }> {
  const clean = sql.replace(/;\s*$/, '').trim();
  if (!/^(select|with)\b/i.test(clean)) throw new Error('仅允许 SELECT 查询');
  if (clean.includes(';')) throw new Error('不允许多语句');
  const limited = /\blimit\s+\d+/i.test(clean) ? clean : `${clean} LIMIT ${MAX_ROWS}`;
  const p = await pool();
  const conn = await p.getConnection();
  try {
    await conn.query(`USE \`${scene}\``);
    const [rows] = await conn.query({ sql: limited, timeout: 30_000 }) as any;
    return { rows: Array.isArray(rows) ? rows.slice(0, MAX_ROWS) : [], sql: limited };
  } finally {
    conn.release();
  }
}

const srv = createServer(async (req, res) => {
  const url = req.url ?? '';
  const send = (code: number, obj: unknown) => {
    res.statusCode = code;
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify(obj));
  };
  if (url === '/health') return send(200, { status: 'ok' });
  if (req.method !== 'POST' || !(url === '/api/v1/query' || url === '/api/v1/sql')) {
    return send(404, { detail: `未实现 ${req.method} ${url}` });
  }
  if (TOKEN && req.headers.authorization !== `Bearer ${TOKEN}`) {
    return send(401, { detail: '无效 token' });
  }
  let body: any = {};
  try {
    const chunks: Buffer[] = [];
    for await (const c of req) chunks.push(c as Buffer);
    body = JSON.parse(Buffer.concat(chunks).toString() || '{}');
  } catch { return send(400, { detail: '请求体不是合法 JSON' }); }

  const scene = String(body.scene || DEFAULT_SCENE);
  if (!okScene(scene)) return send(200, { success: false, error: `scene 非法：${scene}` });

  try {
    const sql = url === '/api/v1/sql' ? String(body.sql ?? '') : await genSql(String(body.question ?? ''), scene);
    const { rows, sql: ranSql } = await runSelect(sql, scene);
    console.log(`[nl2sql] scene=${scene} rows=${rows.length} :: ${(body.question ?? body.sql ?? '').toString().slice(0, 60)} => ${ranSql.slice(0, 120)}`);
    return send(200, { success: true, data: rows, row_count: rows.length, sql: ranSql });
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    console.log(`[nl2sql] ERROR scene=${scene} :: ${(body.question ?? body.sql ?? '').toString().slice(0, 60)} => ${msg.slice(0, 160)}`);
    return send(200, { success: false, error: msg });
  }
});

srv.listen(PORT, () => console.log(`NL2SQL 服务已启动 :${PORT}（默认 scene=${DEFAULT_SCENE}）`));
