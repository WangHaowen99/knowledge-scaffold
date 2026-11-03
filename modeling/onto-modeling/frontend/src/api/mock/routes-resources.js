// mock · 3.6 资料区：R-01 列表、R-02 上传、R-04 删除、R-05 被谁引用、R-11 / R-12 外部反馈，
// 另有 F-04 原文件（资料区的路径）、F-05 表格预览，以及资料页发出的命令（S-20）的进度。
// R-03 改用途、R-06 原文预览仍登记在 routes-materials.js（资料分析也在用）；两边共用 ctx.materials 的资料状态，
// 合成资料本身在 resources-data.js。
// 地址参数：?mock=feedback:off（没启用反馈演进，没有外部反馈分组）、?mock=task:resources（一打开就有一个重读任务在跑）
import {
  CONVERTERS, DRAWIO, EMPTY_DRAWIO, HTML_REPORT, PURPOSES, PURPOSE_VALUES, UPLOAD_EXTS, UPLOAD_LIMIT,
  anchorCount, feedbackSeed, fileRecord, groupOf, kindOf, materialView, referencesOf, tableOf, textMarkdown, textState, webCommit,
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
  const { on, json, err, db, sc, uid, convsOf, emitConv, emitProj, putTask } = ctx;
  db.files = db.files || new Map();
  Object.entries(DRAWIO).forEach(([path, content]) => {
    if (!db.files.has(path)) db.files.set(path, { content, revision: 'r-dio1' });
  });
  const feedbackOn = sc?.feedback !== 'off';
  const fbStore = new Map();
  const revisionTasks = new Map(); // batch_id → CommandTask（同一批修订资料只发起一次增量更新）

  const state = (pid) => ctx.materials.state(pid);
  const mapItemOf = (s, path) => s.map.items.find((x) => x.path === path);
  const view = (s, f) => {
    const m = materialView(f, mapItemOf(s, f.path));
    return f.kind === 'drawio' && db.files.get(f.path) ? { ...m, revision: db.files.get(f.path).revision } : m;
  };
  // 发布过才有外部反馈：还没传资料、读资料中、蓝图待确认的阶段没有
  const feedbackOf = (pid) => {
    if (!fbStore.has(pid)) {
      const stage = ctx.stageOf(pid);
      const early = !stage || /empty$/.test(stage) || stage === 'materials' || stage === 'blueprint';
      fbStore.set(pid, feedbackOn && !early ? feedbackSeed() : []);
    }
    return fbStore.get(pid);
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
      feedback_group: { enabled: feedbackOn, count: feedbackOn ? feedbackOf(pid).length : 0 },
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

  // R-11 外部反馈列表（反馈演进包没启用时为空，带 plugin_disabled）
  on('GET', '/projects/([^/]+)/feedback', ({ m, query }) => {
    if (!feedbackOn) return json(200, { items: [], next_cursor: null, plugin_disabled: true });
    const want = query.get('state');
    const items = feedbackOf(m[1]).filter((x) => !want || x.state === want);
    return json(200, { items, next_cursor: null });
  });

  // R-12 标记外部反馈状态
  on('PATCH', '/projects/([^/]+)/feedback/([^/]+)', ({ m, body }) => {
    const pid = m[1];
    const fb = feedbackOf(pid).find((x) => x.id === decodeURIComponent(m[2]));
    if (!fb) return err(404, 'not_found', '反馈不存在');
    if (!['open', 'handled', 'dismissed'].includes(body.state)) return err(400, 'invalid_argument', '状态不对', { fields: { state: 'invalid' } });
    fb.state = body.state;
    if (body.state !== 'handled') fb.handled_version_label = null;
    return json(200, fb);
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
