import { createServer, type Server } from 'node:http';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { config } from '../config.js';

const MAX_ROWS = 200;

export interface Nl2SqlRuntime {
  token?: string;
  defaultScene: string;
  generateSql(question: string, scene: string): Promise<string>;
  runSelect(sql: string, scene: string): Promise<{ rows: unknown[]; sql: string }>;
}

export function canonicalizeSqlTableIdentifiers(sql: string, tableNames: string[]): string {
  const exactByFolded = new Map<string, string>();
  const ambiguous = new Set<string>();
  for (const tableName of tableNames) {
    const folded = tableName.toLocaleLowerCase('en-US');
    const existing = exactByFolded.get(folded);
    if (existing && existing !== tableName) ambiguous.add(folded);
    else exactByFolded.set(folded, tableName);
  }
  return sql.replace(
    /\b(from|join)\s+((?:`?[A-Za-z0-9_$]+`?\.)?)(`?)([A-Za-z0-9_$]+)\3/gi,
    (matched, keyword: string, qualifier: string, _quote: string, tableName: string) => {
      const folded = tableName.toLocaleLowerCase('en-US');
      const exact = ambiguous.has(folded) ? undefined : exactByFolded.get(folded);
      if (!exact) return matched;
      return `${keyword} ${qualifier}\`${exact}\``;
    },
  );
}

const validScene = (scene: string) => /^[a-zA-Z0-9_]{1,60}$/.test(scene);

async function readJson(req: import('node:http').IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  for await (const chunk of req) chunks.push(Buffer.from(chunk));
  return JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}') as Record<string, unknown>;
}

/** Consumer 的 nl2sql_http / nl2sql_template 共用这一条 HTTP 接口。 */
export function createNl2SqlServer(runtime: Nl2SqlRuntime): Server {
  return createServer(async (req, res) => {
    const path = req.url ?? '';
    const send = (status: number, payload: unknown) => {
      res.statusCode = status;
      res.setHeader('Content-Type', 'application/json; charset=utf-8');
      res.end(JSON.stringify(payload));
    };
    if (path === '/health') return send(200, { status: 'ok' });
    if (req.method !== 'POST' || !['/api/v1/query', '/api/v1/sql'].includes(path)) {
      return send(404, { detail: `未实现 ${req.method} ${path}` });
    }
    if (runtime.token && req.headers.authorization !== `Bearer ${runtime.token}`) {
      return send(401, { detail: '无效 token' });
    }

    let body: Record<string, unknown>;
    try {
      body = await readJson(req);
    } catch {
      return send(400, { detail: '请求体不是合法 JSON' });
    }
    const scene = String(body.scene || runtime.defaultScene);
    if (!validScene(scene)) return send(200, { success: false, error: `scene 非法：${scene}` });

    try {
      const sql = path === '/api/v1/sql'
        ? String(body.sql ?? '')
        : await runtime.generateSql(String(body.question ?? ''), scene);
      const result = await runtime.runSelect(sql, scene);
      return send(200, {
        success: true,
        data: result.rows,
        row_count: result.rows.length,
        sql: result.sql,
      });
    } catch (error) {
      return send(200, { success: false, error: error instanceof Error ? error.message : String(error) });
    }
  });
}

function productionRuntime(): Nl2SqlRuntime {
  const evalDbUrl = process.env.EVAL_DB_URL || config.evalDbUrl;
  if (!evalDbUrl) throw new Error('缺少 EVAL_DB_URL');
  if (!config.llm.baseUrl || config.llm.baseUrl === 'faux' || !config.llm.model) {
    throw new Error('缺少真实 MODELING_LLM_BASE_URL / MODELING_LLM_MODEL');
  }

  let poolPromise: Promise<import('mysql2/promise').Pool> | undefined;
  const pool = () => {
    if (!poolPromise) {
      poolPromise = import('mysql2/promise').then((mysql) =>
        mysql.createPool({ uri: evalDbUrl, connectionLimit: 5, multipleStatements: false }));
    }
    return poolPromise;
  };
  const schemaCache = new Map<string, { at: number; text: string; tableNames: string[] }>();

  const schemaInfo = async (scene: string): Promise<{ text: string; tableNames: string[] }> => {
    const cached = schemaCache.get(scene);
    if (cached && Date.now() - cached.at < 60_000) {
      return { text: cached.text, tableNames: cached.tableNames };
    }
    const db = await pool();
    const [columns] = await db.query(
      `SELECT TABLE_NAME AS t, COLUMN_NAME AS c, COLUMN_TYPE AS ct
       FROM information_schema.columns WHERE table_schema = ?
       ORDER BY TABLE_NAME, ORDINAL_POSITION`, [scene]) as any;
    const byTable = new Map<string, string[]>();
    for (const row of columns) {
      if (!byTable.has(row.t)) byTable.set(row.t, []);
      byTable.get(row.t)!.push(`${row.c}:${row.ct}`);
    }
    if (!byTable.size) throw new Error(`scene（数据库）不存在或为空：${scene}`);
    const parts: string[] = [];
    for (const [table, fields] of byTable) {
      parts.push(`${table}(${fields.join(', ')})`);
      const safeTable = table.replace(/`/g, '');
      const [rows] = await db.query(`SELECT * FROM \`${scene}\`.\`${safeTable}\` LIMIT 2`) as any;
      if (rows.length) parts.push(`  样例: ${JSON.stringify(rows).slice(0, 500)}`);
    }
    const text = parts.join('\n').slice(0, 60_000);
    const tableNames = [...byTable.keys()];
    schemaCache.set(scene, { at: Date.now(), text, tableNames });
    return { text, tableNames };
  };

  const generateSql = async (question: string, scene: string): Promise<string> => {
    const { text: schema } = await schemaInfo(scene);
    const response = await fetch(`${config.llm.baseUrl.replace(/\/+$/, '')}/chat/completions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${config.llm.apiKey ?? ''}` },
      body: JSON.stringify({
        model: config.llm.model,
        temperature: 0,
        messages: [
          { role: 'system', content: [
            '你是 NL2SQL 引擎。根据 MySQL 库结构，把问题转换成一条只读 SELECT 语句。',
            '只输出 SQL 本身，不要解释，不要 Markdown 围栏，不得生成修改数据或结构的语句。',
            'MySQL 表名区分大小写；必须逐字使用库结构中给出的真实表名，并用反引号包裹表名。',
            `表位于数据库 ${scene}；字段类型以库结构为准；结果最多 ${MAX_ROWS} 行。`,
          ].join('\n') },
          { role: 'user', content: `库结构：\n${schema}\n\n问题：${question}\n\nSQL：` },
        ],
      }),
    });
    const data = await response.json().catch(() => ({})) as any;
    if (!response.ok) throw new Error(`LLM ${response.status}: ${JSON.stringify(data).slice(0, 200)}`);
    const sql = String(data.choices?.[0]?.message?.content ?? '')
      .trim().replace(/^```(?:sql)?\s*/i, '').replace(/```\s*$/, '').trim();
    if (!sql) throw new Error('LLM 未产出 SQL');
    return sql;
  };

  const runSelect = async (sql: string, scene: string): Promise<{ rows: unknown[]; sql: string }> => {
    const { tableNames } = await schemaInfo(scene);
    const clean = canonicalizeSqlTableIdentifiers(sql, tableNames).replace(/;\s*$/, '').trim();
    if (!/^(select|with)\b/i.test(clean)) throw new Error('仅允许 SELECT 查询');
    if (clean.includes(';')) throw new Error('不允许多语句');
    if (/\b(insert|update|delete|drop|alter|create|truncate|replace|grant|revoke|call|load)\b/i.test(clean)) {
      throw new Error('查询包含禁止的写入或结构变更关键字');
    }
    const limited = /\blimit\s+\d+/i.test(clean) ? clean : `${clean} LIMIT ${MAX_ROWS}`;
    const db = await pool();
    const connection = await db.getConnection();
    try {
      await connection.query(`USE \`${scene}\``);
      const [rows] = await connection.query({ sql: limited, timeout: 30_000 }) as any;
      return { rows: Array.isArray(rows) ? rows.slice(0, MAX_ROWS) : [], sql: limited };
    } finally {
      connection.release();
    }
  };

  return {
    token: process.env.NL2SQL_TOKEN ?? '',
    defaultScene: process.env.NL2SQL_DEFAULT_SCENE ?? 'default',
    generateSql,
    runSelect,
  };
}

export function startNl2SqlServerFromEnv(): Server {
  const host = process.env.NL2SQL_HOST ?? '127.0.0.1';
  const port = Number(process.env.NL2SQL_PORT ?? 5600);
  const server = createNl2SqlServer(productionRuntime());
  server.listen(port, host, () => console.log(`[nl2sql] listening on ${host}:${port}`));
  return server;
}

const invokedAsMain = !!process.argv[1]
  && import.meta.url === pathToFileURL(resolve(process.argv[1])).href;
if (invokedAsMain) {
  try {
    startNl2SqlServerFromEnv();
  } catch (error) {
    console.error(`[nl2sql] ${error instanceof Error ? error.message : error}`);
    process.exitCode = 1;
  }
}
