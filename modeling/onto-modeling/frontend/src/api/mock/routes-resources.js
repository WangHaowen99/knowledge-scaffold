// mock · 3.6 资料区：R-01 列表、R-02 上传、R-04 删除、R-05 被谁引用、R-15 选出处的位置，
// 另有 F-04 原文件（资料区的路径）、F-05 表格预览，以及资料页发出的命令（S-20）的进度。
// R-03 改用途、R-06 原文预览仍登记在 routes-materials.js（资料分析也在用）；两边共用 ctx.materials 的资料状态，
// 合成资料本身在 resources-data.js。
// 地址参数：?mock=task:resources（一打开就有一个重读任务在跑）
import * as F from './fixtures.js';
import {
  CONVERTERS, DRAWIO, EMPTY_DRAWIO, HTML_REPORT, PURPOSES, PURPOSE_VALUES, UPLOAD_EXTS, UPLOAD_LIMIT,
  anchorCount, fileRecord, groupOf, kindOf, materialView, positionsOf, referencesOf, tableOf, textMarkdown, textState, webCommit,
} from './resources-data.js';

const ORIGIN = { page: 'resources', label: '资料区' };
const GROUPS = [['diagrams', '流程图'], ['excel', '表格'], ['others', '其他资料']];

export const taskSeeds = {
  resources: () => ({ origin: ORIGIN, title: '助手正在重读「06-商品品类表.xlsx」受影响的部分', percent: 40, step_label: '已重读 Sheet「品类」，还剩 2 个工作表' }),
};

// 上传时自动识别用途：表格按结构判断（数据表或术语表），其余交给助手
function detectPurpose(name, kind) {
  if (kind === 'xls' || kind === 'csv') return /术语|词汇|对照/.test(name) ? 'term' : 'schema';
  if (/场景说明|目标/.test(name)) return 'goal';
  return 'auto';
}

export function register(ctx) {
  const { on, json, err, db, uid, convsOf, emitConv, emitProj, putTask } = ctx;
  db.files = db.files || new Map();
  Object.entries(DRAWIO).forEach(([path, content]) => {
    if (!db.files.has(path)) db.files.set(path, { content, revision: 'r-dio1' });
  });
  const revisionTasks = new Map(); // batch_id → CommandTask（同一批修订资料只发起一次增量更新）

  const state = (pid) => ctx.materials.state(pid);
  const mapItemOf = (s, path) => s.map.items.find((x) => x.path === path);
  // 表格的 Sheet：mock 里第一张是中文表头（提醒一条），其余是英文列名的数据表
  const mockSheets = (f) => Array.from({ length: f.sheets || 1 }, (_, i) => ({
    name: `Sheet${i + 1}`, kind: i ? 'table' : 'document', headers: i ? ['id', 'name'] : ['编号', '名称'], selectable: true, schema_selectable: true,
    issues: i ? [] : [{ code: 'non_identifier_header', severity: 'warning', message: '表头含中文或非英文标识符。系统会原样使用这些字段名；请核对是否与实际数据源列名一致，不要为通过识别而随意改名。' }],
  }));
  const mockScope = (f) => {
    const list = mockSheets(f);
    return { version: 1, mode: 'auto', file_sha256: `mock-${f.path}`, included_sheets: list.map((x) => x.name),
      reference_sheets: [], sheets: list, warnings: [] };
  };
  const schemaTable = (f) => f.purpose === 'schema' && /\.(xlsx|xlsm|csv)$/i.test(f.path);
  const view = (s, f) => {
    const m = { ...materialView(f, mapItemOf(s, f.path)), schema_scope: schemaTable(f) ? mockScope(f) : null, schema_candidate: null };
    return f.kind === 'drawio' && db.files.get(f.path) ? { ...m, revision: db.files.get(f.path).revision } : m;
  };
  const cidOf = (pid) => convsOf(pid)[0]?.id;
  const nowIso = () => new Date().toISOString();

  // R-01 资料列表
  on('GET', '/projects/([^/]+)/resources', ({ m }) => {
    const pid = m[1];
    const s = state(pid);
    const items = s.files.map((f) => view(s, f));
    return json(200, {
      groups: GROUPS.map(([key, label]) => ({ key, label, items: items.filter((x) => x.group === key) })),
      purposes: PURPOSES,
    });
  });

  // 修订资料：整批传完后往工作台对话发一条增量更新命令，进度经项目流推给资料区
  function revisionTask(pid, f, batchId) {
    if (batchId && revisionTasks.has(batchId)) return revisionTasks.get(batchId);
    const cid = cidOf(pid);
    const text = `「${f.name}」是修订资料，发起增量更新：先列变更点，确认后只改受影响的内容`;
    const t = {
      task_id: `task-${uid()}`, origin: ORIGIN, title: `助手正在识别「${f.name}」的变更点`, state: 'running', percent: 0, step_label: '对比修订前后的资料',
      conversation_id: cid, message_id: `m-${uid()}`, exec_id: null, started_at: nowIso(), finished_at: null, result: null,
    };
    if (batchId) revisionTasks.set(batchId, t);
    emitConv(pid, cid, 'message.user', { message_id: t.message_id, text, command: '/ontology-update', attachments: [], references: [], origin: { kind: 'page', ...ORIGIN, task_id: t.task_id }, delivery: 'immediate' });
    putTask?.(pid, t);
    setTimeout(() => putTask?.(pid, { ...t, percent: 50, step_label: '列出变更点' }, { transient: true }), 900);
    setTimeout(() => {
      f.revision_task = { conversation_id: cid, label: '识别出 3 处变更点，等你确认清单' };
      emitConv(pid, cid, 'message.assistant', { message_id: `m-${uid()}`, text_md: `「${f.name}」里识别出 3 处变更点，清单列在下面，确认后只改受影响的内容。`, anchors: [], entry_links: [], continued: false });
      putTask?.(pid, { ...t, state: 'done', percent: 100, step_label: '列出变更点', finished_at: nowIso(), result: { summary: '识别出 3 处变更点，等你确认清单', refs: [] } });
    }, 2700);
    return t;
  }

  // R-02 上传资料：同名直接替换；资料地图（资料分析）同步加一份
  on('POST', '/projects/([^/]+)/resources', ({ m, body, form }) => {
    const pid = m[1];
    let name;
    let size = 0;
    if (form) { const file = form.get('file'); name = file?.name; size = file?.size || 0; } else {
      const u = db.uploads.get(body.upload_id);
      if (!u) return err(410, 'upload_expired', '暂存文件已过期');
      name = u.filename; size = u.size;
    }
    if (!name) return err(400, 'invalid_argument', '缺少文件', { fields: { file: 'required' } });
    if (/[\\/]|^\.|\.\./.test(name)) return err(400, 'invalid_filename', '文件名不合法', { filename: name });
    const ext = (name.split('.').pop() || '').toLowerCase();
    if (!UPLOAD_EXTS.includes(ext)) return err(415, 'unsupported_media_type', '不支持这种格式', { accepted: UPLOAD_EXTS });
    if (size > UPLOAD_LIMIT) return err(413, 'payload_too_large', '文件超过上限', { limit_bytes: UPLOAD_LIMIT });
    const s = state(pid);
    const kind = kindOf(name);
    const existing = s.files.find((x) => x.name === name);
    const subdir = ['diagrams', 'excel', 'others'].includes(body.subdir) ? body.subdir : groupOf(kind);
    const path = existing?.path || `resource/${subdir}/${name}`;
    const given = PURPOSE_VALUES.includes(body.purpose) && body.purpose !== 'auto' ? body.purpose : null;
    const purpose = given || existing?.purpose || detectPurpose(name, kind);
    const purpose_auto = given ? false : existing ? existing.purpose_auto : purpose !== 'auto';
    const rec = fileRecord({
      path, name, kind, size, uploaded_at: nowIso(), purpose, purpose_auto, reading_mode: body.reading_mode || existing?.reading_mode,
      is_template: existing?.is_template, coverage: null, text_state: 'converting',
    });
    convertLater(pid, path);
    if (existing) Object.assign(existing, rec, { referenced: existing.referenced }); else s.files.push(rec);
    const f = existing || rec;
    if (kind === 'drawio') db.files.set(path, { content: db.files.get(path)?.content || EMPTY_DRAWIO, revision: `r-${uid()}` });
    ctx.materials.added(pid, { path, name, purpose, purpose_auto });
    const { commit, version_label } = webCommit(ctx, pid, [path]);
    let command_task = null;
    if (body.is_revision) {
      f.is_revision = true;
      command_task = revisionTask(pid, f, body.batch_id);
    }
    return json(201, { material: view(s, f), replaced: !!existing, commit, version_label, detected: { purpose, tables: [] }, command_task });
  });

  // 文本版（ADR-0073）：上传、重新转换后在「后台」转，1.2 秒后经项目流推 materials.text.updated（EP-25）
  function convertLater(pid, path) {
    setTimeout(() => {
      const f = state(pid).files.find((x) => x.path === path);
      if (!f || f.text?.state !== 'converting') return;
      f.text = textState(f.name, f.kind, { scanned: f.scanned, state: /坏|损坏/.test(f.name) ? 'failed' : 'converted' });
      if (f.text?.converter) f.text.converter = converterOf(f.name) || f.text.converter;
      emitProj(pid, 'materials.text.updated', { path, text: f.text });
    }, 1200);
  }
  const converterOf = (name) => {
    const ext = `.${(name.split('.').pop() || '').toLowerCase()}`;
    const id = defaults[ext] || CONVERTERS.find((c) => c.types.includes(ext))?.id;
    const c = CONVERTERS.find((x) => x.id === id);
    return c ? { id: c.id, label: c.label, version: c.version } : null;
  };

  // R-13 查看文本
  on('GET', '/projects/([^/]+)/resources/text', ({ m, query }) => {
    const path = query.get('path') || '';
    const f = state(m[1]).files.find((x) => x.path === path);
    if (!f) return err(404, 'file_not_found', '资料不存在', { path });
    if (!f.text) return err(409, 'not_convertible', '这份资料没有文本版', { path });
    const markdown = f.text.state === 'converted' ? textMarkdown(f) : null;
    return json(200, { path, text: f.text, markdown, truncated: false });
  });

  // R-16 Sheet 范围预检：mock 里第一张 Sheet 有一条提醒，改用途为数据 Schema 时会弹窗
  on('GET', '/projects/([^/]+)/resources/schema-preview', ({ m, query }) => {
    const path = query.get('path') || '';
    const f = state(m[1]).files.find((x) => x.path === path);
    if (!f) return err(404, 'file_not_found', '资料不存在', { path });
    if (!/\.(xlsx|xlsm|csv)$/i.test(path)) return err(415, 'unsupported_media_type', '数据 Schema 只支持 xlsx、xlsm、csv', { allowed: ['.xlsx', '.xlsm', '.csv'] });
    return json(200, mockScope(f));
  });

  // R-15 选出处的位置：逐行，q 按原文和位置筛，一页 limit 条（游标就是下标）；选中的锚点补进 EV-29 的解析表，悬停能看原文
  on('GET', '/projects/([^/]+)/resources/positions', ({ m, query }) => {
    const path = query.get('path') || '';
    const f = state(m[1]).files.find((x) => x.path === path);
    if (!f) return err(404, 'file_not_found', '资料不存在', { path });
    if (!f.text) return err(409, 'not_convertible', '这份资料没有文本', { path });
    const name = f.name.replace(/\.[^.]+$/, '');
    const all = positionsOf(f);
    if (!all) return json(200, { path, name, readable: false, items: [], next_cursor: null });
    const q = (query.get('q') || '').trim().toLowerCase();
    const hits = q ? all.filter((x) => x.text.toLowerCase().includes(q) || x.location.toLowerCase().includes(q)) : all;
    const start = Number(query.get('cursor') || 0);
    const end = start + Math.min(Number(query.get('limit') || 200), 1000);
    const items = hits.slice(start, end);
    items.forEach((x) => {
      if (F.ANCHOR_RESOLUTIONS && !F.ANCHOR_RESOLUTIONS[x.anchor]) {
        F.ANCHOR_RESOLUTIONS[x.anchor] = {
          anchor: x.anchor, scheme: 'file', resolved: true, source_label: name, location_label: x.location, material: { name, path },
          text: x.text, highlights: [{ start: 0, end: x.text.length }], confidence: null, snapshot_id: null, page: 'resources',
        };
      }
    });
    return json(200, { path, name, readable: true, items, next_cursor: end < hits.length ? String(end) : null });
  });

  // R-14 重新转换（失败后的重试也是它）
  on('POST', '/projects/([^/]+)/resources/convert', ({ m, body }) => {
    const pid = m[1];
    const s = state(pid);
    const f = s.files.find((x) => x.path === body?.path);
    if (!f) return err(404, 'file_not_found', '资料不存在', { path: body?.path });
    if (!f.text || f.text.state === 'native' || !converterOf(f.name)) return err(409, 'not_convertible', '这份资料不能转成文本', { path: f.path });
    f.text = { ...f.text, state: 'converting', reason: null };
    emitProj(pid, 'materials.text.updated', { path: f.path, text: f.text });
    convertLater(pid, f.path);
    return json(202, { material: view(s, f) });
  });

  // PL-20 转换器与默认、PL-21 管理员设默认
  const defaults = {};
  const convertersView = () => {
    const types = [...new Set(CONVERTERS.flatMap((c) => c.types))].sort();
    const rows = types.map((t) => {
      const ids = CONVERTERS.filter((c) => c.types.includes(t)).map((c) => c.id);
      return { type: t, label: { '.pdf': 'PDF', '.docx': 'Word', '.xlsx': 'Excel', '.xlsm': 'Excel（启用宏）' }[t] || t, default: defaults[t] || ids[0], chosen_by_admin: !!defaults[t], converters: ids };
    });
    return {
      gateway: { status: 'ok', detail: null },
      items: CONVERTERS.map((c) => ({ ...c, loaded: true, default_for: rows.filter((r) => r.default === c.id).map((r) => r.type) })),
      types: rows,
    };
  };
  on('GET', '/converters', () => json(200, convertersView()));
  on('PUT', '/admin/converters/defaults', ({ body }) => {
    if (!CONVERTERS.some((c) => c.id === body?.converter && c.types.includes(body?.type))) return err(400, 'invalid_argument', '这种文件没有这个转换器', { fields: { converter: 'enum' } });
    defaults[body.type] = body.converter;
    return json(200, convertersView());
  });

  // R-04 删除资料：保存为新版本；引用它的出处变成「来源无法核验」
  on('DELETE', '/projects/([^/]+)/resources', ({ m, query }) => {
    const pid = m[1];
    const path = query.get('path') || '';
    if (path === 'resource/materials.json' || path.startsWith('resource/systems/') || path.includes('..')) return err(403, 'path_forbidden', '这个路径不能删', { path });
    const s = state(pid);
    const f = s.files.find((x) => x.path === path);
    if (!f) return err(404, 'file_not_found', '资料不存在', { path });
    const broken = anchorCount(referencesOf(f));
    s.files.splice(s.files.indexOf(f), 1);
    db.files.delete(path);
    ctx.materials.removed(pid, path);
    const { commit, version_label } = webCommit(ctx, pid, [path]);
    return json(200, { deleted: path, commit, version_label, broken_references: broken });
  });

  // R-05 资料被谁引用（还没建模的阶段没有引用）
  on('GET', '/projects/([^/]+)/resources/references', ({ m, query }) => {
    const path = query.get('path') || '';
    const f = state(m[1]).files.find((x) => x.path === path);
    if (!f) return err(404, 'file_not_found', '资料不存在', { path });
    const items = referencesOf(f);
    return json(200, { path, total: items.length, items });
  });

  // F-04 原文件：只管资料区的路径，别的交给后面登记的
  on('GET', '/projects/([^/]+)/files/raw', ({ m, query }) => {
    const path = query.get('path') || '';
    if (!path.startsWith('resource/')) return undefined;
    const f = state(m[1]).files.find((x) => x.path === path);
    if (!f) return err(404, 'file_not_found', '文件不存在', { path });
    let content = `（合成示例）${f.name}`;
    let type = 'application/octet-stream';
    if (f.kind === 'drawio') { content = db.files.get(path)?.content || EMPTY_DRAWIO; type = 'application/xml'; }
    if (f.kind === 'html') { content = HTML_REPORT; type = 'text/html; charset=utf-8'; }
    if (f.kind === 'csv') { content = tableOf(f)[0].rows.map((r) => r.join(',')).join('\n'); type = 'text/csv; charset=utf-8'; }
    const headers = { 'Content-Type': type };
    if (f.kind === 'html') Object.assign(headers, { 'Content-Security-Policy': "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'", 'X-Content-Type-Options': 'nosniff' });
    if (['1', 'true'].includes(query.get('download'))) headers['Content-Disposition'] = `attachment; filename="${encodeURIComponent(f.name)}"`;
    return new Response(content, { status: 200, headers });
  });

  // F-05 表格预览（xlsx 多个工作表，CSV 一个）
  on('GET', '/projects/([^/]+)/files/table-preview', ({ m, query }) => {
    const path = query.get('path') || '';
    const f = state(m[1]).files.find((x) => x.path === path);
    if (!f) return path.startsWith('resource/') ? err(404, 'file_not_found', '文件不存在', { path }) : undefined;
    if (f.kind !== 'xls' && f.kind !== 'csv') return err(415, 'unsupported_media_type', '只能预览 xlsx、xlsm、csv');
    const want = query.get('sheet');
    const sheets = tableOf(f).filter((x) => !want || x.name === want);
    return json(200, { format: f.kind === 'csv' ? 'csv' : 'xlsx', sheets, sheet_truncated: false });
  });

  // 资料页发的命令：让助手把没读完的资料读完（S-20），跑完资料地图和清单里标为读完
  ctx.commandHooks.push({
    match: (body) => body.origin?.page === 'resources' && body.command === '/ontology-research' && !!body.args?.path,
    title: (body) => `助手正在读完「${String(body.args.path).split('/').pop()}」`,
    steps: ['定位还没读的部分', '读取并抽取条目', '更新资料地图'],
    finish: (pid, body) => {
      const s = state(pid);
      const f = s.files.find((x) => x.path === body.args.path);
      const it = mapItemOf(s, body.args.path);
      if (f && f.coverage) f.coverage = { read_pct: 100, label: f.sections || '全文' };
      if (it) Object.assign(it, { read_pct: 100, status: 'done', position: null, blocked: null });
      emitProj(pid, 'materials.updated', { items: it ? [it] : [], read_pct: s.map.read_pct, problems_count: 0 });
    },
  });
}
