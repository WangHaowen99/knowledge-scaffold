// mock · 3.9 版本与标签（V-01～V-08）、读历史版本文件（F-06）
//
// 版本列表 = versions-data.js 的静态历史 + 项目流里出现过的 workspace.changed（Studio 保存、流程图保存、助手一轮合入、
// 自主构建按段回退、本页的保存与回退……），所以别的页做的修改会自动出现在这里。新版本号和已有的冲突时顺延。
// 历史文件不存快照：当前内容取 F-02（Studio 分组的条目文件，经 ctx.self 转调一次），
// 再按 versions-data.js 里每条改动的 undo 往回推。回退把恢复后的内容记在 overlay 里（Studio 自己的条目数据不跟着变，见报告）。
// 主线版本号：打开本页或发布页时，项目卡的 version_label / head 对齐到历史的最新版本（F1 的卡片还停在 v0.12）。
//
// mock 参数：stage:l4（自主构建运行 #3 还在跑，历史到 v0.16，回退返回 409 autobuild_running）；
//           scope:running / scope:pending（回退返回 423 write_scope_locked，对比右侧只读）
import { diffLines, entryPath, pathLabel, undoneText } from '../../pages/versions/versionsModel.js';
import * as D from './versions-data.js';
import { releasesOf } from './publish-data.js';
import { ENTRIES, entryRef } from './scenario.js';

const zip = () => new Blob(['PK\u0005\u0006'.padEnd(22, '\u0000')]);
const majorOf = (label) => Number(/^v(\d+)\./.exec(label || '')?.[1] || 0);
const VERB = { studio: 'Studio 编辑', web: '网页编辑', assistant: '助手修改', mcp: 'MCP 写入', autobuild: '自主构建', import: '导入', plugin: '能力变更', revert: '回退' };

export function register(ctx) {
  const { on, json, err, db, sc, F, cardOf, hub, projKey, emitProj, scopeOf, stageOf, uid } = ctx;
  const st = db.vstore || (db.vstore = {
    statics: new Map(), tags: new Map(), deleted: new Set(), meta: new Map(), processed: new Set(),
    base: new Map(), snaps: new Map(), overlay: new Map(),
  });
  const isMain = (pid) => pid === F.MAIN_PID;
  const running = (pid) => isMain(pid) && stageOf?.(pid) === 'l4' && sc.run !== 'ended';

  // ---------- 当前文件：Studio 分组的 F-02（null = 没有这个文件，undefined = 读不到） ----------
  async function studioFile(pid, path, init) {
    if (ctx.readFile) return ctx.readFile(pid, path);
    if (ctx.self) {
      const res = await ctx.self(`/api/v1/projects/${encodeURIComponent(pid)}/files/content?path=${encodeURIComponent(path)}`, { method: 'GET', headers: init?.headers || {} });
      if (res.ok) {
        const f = await res.json();
        return { content: f.content, revision: f.revision };
      }
      return res.status === 404 ? null : undefined;
    }
    const f = db.files?.get(path);
    return f ? { content: f.content, revision: f.revision || 'r1' } : null;
  }
  async function cur(pid, path, init) {
    const key = `${pid}:${path}`;
    const f = await studioFile(pid, path, init);
    if (st.overlay.has(key)) return { content: st.overlay.get(key), revision: f?.revision || null };
    return f;
  }

  // ---------- 版本历史 ----------
  const touched = (d) => [...new Set([...(d.paths || []), ...(d.entries || []).map(entryPath)].filter(Boolean))];
  function autoSummary(d) {
    const names = [...(d.entries || []).map((e) => e.name), ...(d.artifacts || []).map((a) => a.title)];
    const what = names.length ? names.slice(0, 3).join('、') : (d.paths || []).map((p) => p.split('/').pop()).slice(0, 2).join('、') || '知识包';
    return `${VERB[d.source] || '修改'}：${what}`;
  }
  const autoChanges = (d) => [...(d.entries || []), ...(d.artifacts || [])].map((t) => ({ target: t, change: 'mod', detail: '修改', review: null }));

  async function history(pid, init) {
    const card = cardOf(pid);
    if (!card) return null;
    if (!st.statics.has(pid)) st.statics.set(pid, isMain(pid) ? D.mainHistory({ running: running(pid) }) : D.genericHistory(card));
    const out = st.statics.get(pid).map((v) => ({ ...v, dynamic: false }));
    const known = new Set(out.map((v) => v.commit));
    const fresh = [];
    hub.history(projKey(pid)).forEach((e) => {
      const d = e.data || {};
      if (e.type !== 'workspace.changed' || !d.commit || known.has(d.commit)) return;
      known.add(d.commit);
      const prev = out[out.length - 1];
      const m = /^v(\d+)\.(\d+)$/.exec(d.version_label || '');
      let n = m ? Number(m[2]) : 0;
      let label = d.version_label;
      if (!m || n <= prev.n) { n = prev.n + 1; label = `v${majorOf(prev.version_label)}.${n}`; }
      const meta = st.meta.get(d.commit) || {};
      const v = {
        commit: d.commit, n, version_label: label, at: e.ts || new Date().toISOString(), source: d.source,
        source_label: D.SOURCE_LABEL[d.source] || d.source, author: d.by || 'zhangsan', summary: meta.summary || autoSummary(d),
        changes: meta.changes || autoChanges(d), tags: [], changeset_id: d.changeset_id || null, run: null, needs_review: 0,
        trailer: { source: D.TRAILER_SOURCE[d.source] || d.source, author: d.by || 'zhangsan', ...(d.changeset_id ? { changeset: d.changeset_id } : {}) },
        packs_from: null, dynamic: true, paths: touched(d), mine: !!meta.mine,
      };
      out.push(v);
      if (!st.processed.has(`${pid}:${v.commit}`)) { st.processed.add(`${pid}:${v.commit}`); fresh.push(v); }
    });
    // 新出现的版本：别处的修改让回退留下的 overlay 失效；已经还原过历史的文件，记下这个版本之后的内容
    for (const v of fresh) {
      for (const p of v.paths) {
        const key = `${pid}:${p}`;
        if (!v.mine) st.overlay.delete(key);
        if (!st.base.has(key)) continue;
        const f = await cur(pid, p, init);
        if (f !== undefined) st.snaps.set(key, [...(st.snaps.get(key) || []), { n: v.n, content: f?.content ?? null }]);
      }
    }
    const rel = releasesOf(db, pid, card);
    const userTags = st.tags.get(pid) || [];
    out.forEach((v) => {
      v.tags = [...v.tags, ...userTags.filter((t) => t.commit === v.commit)].filter((t) => !st.deleted.has(`${pid}:${t.name}`));
      v.release = rel.find((r) => r.commit === v.commit)?.version || null;
      v.current = false;
    });
    const head = out[out.length - 1];
    head.current = true;
    if (card.version_label !== head.version_label || card.head !== head.commit) {
      Object.assign(card, { version_label: head.version_label, head: head.commit });
      emitProj(pid, 'project.updated', { fields: ['version_label'], meta: F.projectMeta(card) });
    }
    return out;
  }

  const publicVersion = ({ changes, dynamic, paths, packs_from, mine, ...v }) => v;
  function resolve(list, c) {
    if (!c || !list) return null;
    const h = /^HEAD(?:~(\d+))?$/.exec(c);
    if (h) return list[list.length - 1 - Number(h[1] || 0)] || null;
    if (!/^[0-9a-f]{6,40}$/i.test(c)) return null;
    return list.find((v) => v.commit === c || v.commit.startsWith(c) || c.startsWith(v.commit)) || null;
  }

  // ---------- 某个文件在第 n 个版本时的内容 ----------
  async function baseOf(pid, list, path, init) {
    const key = `${pid}:${path}`;
    if (!st.base.has(key)) {
      const f = await cur(pid, path, init);
      if (f === undefined) return undefined;
      const statics = st.statics.get(pid) || [];
      st.base.set(key, { n: statics.length ? statics[statics.length - 1].n : 0, content: f?.content ?? null });
    }
    return st.base.get(key);
  }
  async function contentAt(pid, list, path, n, init) {
    const base = await baseOf(pid, list, path, init);
    if (base === undefined) return undefined;
    const snaps = (st.snaps.get(`${pid}:${path}`) || []).filter((s) => s.n <= n);
    if (snaps.length) return snaps[snaps.length - 1].content;
    if (n >= base.n) return base.content;
    let c = base.content;
    list.filter((v) => !v.dynamic && v.n > n && v.n <= base.n).sort((a, b) => b.n - a.n).forEach((v) => {
      v.changes.forEach((x) => { if (entryPath(x.target) === path) c = D.undoChange(c, x); });
    });
    return c;
  }
  const entryByPath = (path) => ENTRIES.find((x) => entryPath(x) === path);
  const pathOfEntryId = (id) => {
    const x = ENTRIES.find((e) => e.id === id);
    return x ? entryPath(x) : null;
  };

  async function revertPreview(pid, list, target, init) {
    const after = list.filter((v) => v.n > target.n);
    const paths = [...new Set(after.flatMap((v) => (v.dynamic ? v.paths : v.changes.map((c) => entryPath(c.target)).filter(Boolean))))];
    const files = (await Promise.all(paths.map(async (path) => {
      const [at, now] = await Promise.all([contentAt(pid, list, path, target.n, init), cur(pid, path, init)]);
      if (at === undefined || now === undefined || at === (now?.content ?? null)) return null;
      return { path, change: at == null ? 'delete' : 'restore' };
    }))).filter(Boolean);
    const scenes = new Map();
    after.forEach((v) => v.changes.forEach((c) => {
      if (c.target?.type === 'trace.scenario' && !scenes.has(c.target.key)) scenes.set(c.target.key, c.change === 'add' ? 'delete' : 'restore');
    }));
    scenes.forEach((change, key) => files.push({ path: `ext/trace/scenarios/${key}.yaml`, change }));
    const head = list[list.length - 1];
    return {
      target: { commit: target.commit, version_label: target.version_label },
      next_version_label: `v${majorOf(head.version_label)}.${head.n + 1}`,
      files,
      undone_versions: after.map((v) => v.version_label),
      packs_lock_changes: after.filter((v) => v.source === 'plugin' && v.trailer?.packs).map((v) => {
        const [pack, to] = v.trailer.packs.split('@');
        return { pack, from: to, to: v.packs_from };
      }),
    };
  }

  // 写入一个新版本（本页回退、发布页改数据来源共用）：更新项目卡、发 workspace.changed
  async function commitWrite(pid, { source, summary, changes = [], entries = [], paths = [], by = 'zhangsan', mine = false }, init) {
    const list = await history(pid, init);
    const head = list[list.length - 1];
    const version_label = `v${majorOf(head.version_label)}.${head.n + 1}`;
    const commit = D.hex8(`${pid}:${source}:${Date.now()}:${uid()}`);
    st.meta.set(commit, { summary, changes, mine });
    const card = cardOf(pid);
    if (card) Object.assign(card, { version_label, head: commit });
    emitProj(pid, 'workspace.changed', { commit, version_label, source, changeset_id: null, by, entries, artifacts: [], paths });
    if (card) emitProj(pid, 'project.updated', { fields: ['version_label'], meta: F.projectMeta(card) });
    await history(pid, init);
    return { commit, version_label };
  }
  st.history = history;
  st.resolve = resolve;
  st.commitWrite = commitWrite;

  // ---------- V-01 列表 ----------
  on('GET', '/projects/([^/]+)/versions', async ({ m, query, init }) => {
    const list = await history(m[1], init);
    if (!list) return err(404, 'project_not_found', '项目不存在');
    const sources = (query.get('source') || '').split(',').filter(Boolean);
    let items = [...list].reverse();
    if (sources.length) items = items.filter((v) => sources.includes(v.source));
    const limit = Math.min(200, Math.max(1, Number(query.get('limit')) || 50));
    const offset = Number(query.get('cursor')) || 0;
    const head = list[list.length - 1];
    return json(200, {
      items: items.slice(offset, offset + limit).map(publicVersion),
      next_cursor: offset + limit < items.length ? String(offset + limit) : null,
      total: items.length, head: head.commit, current_version_label: head.version_label,
    });
  });

  // ---------- V-04 对比（先于 V-02 登记：/versions/diff 也能匹配 /versions/{commit}） ----------
  on('GET', '/projects/([^/]+)/versions/diff', async ({ m, query, init }) => {
    const pid = m[1];
    const list = await history(pid, init);
    if (!list) return err(404, 'project_not_found', '项目不存在');
    const from = resolve(list, query.get('from'));
    if (!from) return err(400, 'invalid_commit', '版本号不合法', null);
    const path = query.get('path') || pathOfEntryId(query.get('entry'));
    if (!path) return err(400, 'invalid_argument', '缺少 path 或 entry', { fields: { path: 'required' } });
    const toQ = query.get('to') || 'WORKTREE';
    const left = await contentAt(pid, list, path, from.n, init);
    let right;
    let rightRevision = null;
    let to = { commit: null, version_label: null };
    if (toQ === 'WORKTREE') {
      const f = await cur(pid, path, init);
      right = f === undefined ? undefined : f?.content ?? null;
      rightRevision = f?.content != null ? f.revision : null;
    } else {
      const tv = resolve(list, toQ);
      if (!tv) return err(400, 'invalid_commit', '版本号不合法', null);
      right = await contentAt(pid, list, path, tv.n, init);
      to = { commit: tv.commit, version_label: tv.version_label };
    }
    if (left == null && right == null) return err(404, 'file_not_found', '文件不存在', { path });
    return json(200, {
      from: { commit: from.commit, version_label: from.version_label }, to, path,
      left: left ?? '', right: right ?? '', right_revision: rightRevision, hunks: diffLines(left || '', right || '').hunks,
    });
  });

  // ---------- V-05 回退预览、V-06 回退 ----------
  on('GET', '/projects/([^/]+)/versions/revert-preview', async ({ m, query, init }) => {
    const list = await history(m[1], init);
    if (!list) return err(404, 'project_not_found', '项目不存在');
    const target = resolve(list, query.get('commit'));
    if (!target) return err(400, 'invalid_commit', '版本号不合法', null);
    if (target.current) return err(400, 'nothing_to_revert', '当前内容与目标版本一致', null);
    return json(200, await revertPreview(m[1], list, target, init));
  });
  on('POST', '/projects/([^/]+)/versions/revert', async ({ m, body, user, init }) => {
    const pid = m[1];
    const lock = scopeOf(pid);
    if (lock.state === 'assistant_running' || lock.state === 'pending_confirm') return err(423, 'write_scope_locked', '有未确认的助手修改', lock);
    if (running(pid) || lock.state === 'autobuild_slice') return err(409, 'autobuild_running', '自主构建运行中', { run_id: 'run-3' });
    const list = await history(pid, init);
    if (!list) return err(404, 'project_not_found', '项目不存在');
    const target = resolve(list, body.commit);
    if (!target) return err(400, 'invalid_commit', '版本号不合法', null);
    if (target.current) return err(400, 'nothing_to_revert', '当前内容与目标版本一致', null);
    const p = await revertPreview(pid, list, target, init);
    const fileChanges = p.files.filter((f) => !/^ext\//.test(f.path));
    for (const f of fileChanges) st.overlay.set(`${pid}:${f.path}`, await contentAt(pid, list, f.path, target.n, init));
    const refs = fileChanges.map((f) => entryByPath(f.path)).filter(Boolean).map((x) => entryRef(x.key));
    const r = await commitWrite(pid, {
      source: 'revert', mine: true, by: user?.username || 'zhangsan', entries: refs, paths: fileChanges.map((f) => f.path),
      summary: `回退到 ${target.version_label}${p.undone_versions.length ? `：撤回 ${undoneText(p.undone_versions)} 的改动` : ''}`,
      changes: fileChanges.map((f) => ({
        target: entryByPath(f.path) ? entryRef(entryByPath(f.path).key) : { type: 'file', key: f.path, title: pathLabel(f.path), label: pathLabel(f.path) === '其他文件' ? '' : pathLabel(f.path) },
        change: f.change === 'delete' ? 'del' : 'mod', detail: f.change === 'delete' ? '删除' : `恢复到 ${target.version_label}`, review: null,
      })),
    }, init);
    return json(200, { ...r, reverted_to: { commit: target.commit, version_label: target.version_label } });
  });

  // ---------- V-07 / V-08 标记 ----------
  on('POST', '/projects/([^/]+)/versions/tags', async ({ m, body, init }) => {
    const pid = m[1];
    const label = String(body.label || '').trim();
    if (!label || label.length > 120) return err(400, 'invalid_argument', '标记名称须为 1–120 个字符', { fields: { label: 'length' } });
    const list = await history(pid, init);
    const v = resolve(list, body.commit);
    if (!v) return err(400, 'invalid_commit', '版本号不合法', null);
    const tag = { name: `mark-${Math.floor(Date.now() / 1000)}-${uid().padStart(4, '0').slice(-4)}`, label, commit: v.commit };
    st.tags.set(pid, [...(st.tags.get(pid) || []), tag]);
    return json(201, { name: tag.name, label, commit: v.commit, version_label: v.version_label });
  });
  on('DELETE', '/projects/([^/]+)/versions/tags/([^/]+)', async ({ m, init }) => {
    const pid = m[1];
    const name = decodeURIComponent(m[2]);
    if (!/^mark-/.test(name)) return err(404, 'tag_not_found', '只能删用户标记', null);
    const list = await history(pid, init);
    if (!(list || []).some((v) => v.tags.some((t) => t.name === name))) return err(404, 'tag_not_found', '标记不存在', null);
    st.deleted.add(`${pid}:${name}`);
    return new Response(null, { status: 204 });
  });

  // ---------- V-03 某版本的文件 ----------
  on('GET', '/projects/([^/]+)/versions/([^/]+)/files', async ({ m, query, init }) => {
    const pid = m[1];
    const list = await history(pid, init);
    const v = resolve(list, decodeURIComponent(m[2]));
    if (!v) return err(400, 'invalid_commit', '版本号不合法', null);
    if (query.get('download')) {
      const card = cardOf(pid);
      return new Response(zip(), { status: 200, headers: { 'Content-Type': 'application/zip', 'Content-Disposition': `attachment; filename="${encodeURIComponent(`${card?.package_name || 'package'}-${v.version_label}-ontology.zip`)}"` } });
    }
    const prefix = query.has('prefix') ? query.get('prefix') : 'ontology/';
    const added = D.addedAt(list);
    const items = isMain(pid)
      ? ENTRIES.filter((x) => !added.has(x.id) || added.get(x.id) <= v.n).map((x) => entryPath(x)).filter((p) => p && p.startsWith(prefix))
        .sort((a, b) => a.localeCompare(b, 'zh')).map((path, i) => ({ path, size: 380 + ((path.length * 53 + i * 17) % 1400) }))
      : [];
    return json(200, { items });
  });

  // ---------- V-02 详情 ----------
  on('GET', '/projects/([^/]+)/versions/([^/]+)', async ({ m, init }) => {
    const list = await history(m[1], init);
    if (!list) return err(404, 'project_not_found', '项目不存在');
    const v = resolve(list, decodeURIComponent(m[2]));
    if (!v) return err(400, 'invalid_commit', '版本号不合法', null);
    return json(200, { ...publicVersion(v), changes: v.changes.map(({ undo, ...c }) => c), run: v.run });
  });

  // ---------- F-06 读历史版本文件 ----------
  on('GET', '/projects/([^/]+)/files/at', async ({ m, query, init }) => {
    const pid = m[1];
    const path = query.get('path') || '';
    if (/(^|\/)\.(git|claude|onto|ontology-state)(\/|$)/.test(path) || path.includes('..')) return err(403, 'path_forbidden', '路径越界', { path });
    const list = await history(pid, init);
    const v = resolve(list, query.get('commit'));
    if (!v) return err(400, 'invalid_commit', '版本号不合法', null);
    const content = await contentAt(pid, list, path, v.n, init);
    if (content == null) return err(404, 'file_not_found', '文件不存在', { path });
    return json(200, { path, commit: v.commit, version_label: v.version_label, content, truncated: false });
  });
}
