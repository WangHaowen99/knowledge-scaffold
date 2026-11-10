// mock：插件上传与审核（PL-13～PL-18）、普通用户装自己的包（PL-22～PL-26），以及没挂界面的管理员审核申请（AD-08～AD-10）。
// 页面：plugin-upload。PL-13～PL-18 只有 admin 能调（ctx.forbid）。状态放在 db.packUploads、db.packRepo、db.adminRequests 上。
// 地址参数 ?mock=upload:<状态>，几个状态用 + 连起来（如 upload:checking+exists）：
// - 缺省            两个上传：industry-retail-1.1.0.zip 检查通过（13 通过 · 1 警告），demo-trace-ext-0.1.0.zip 是租户包、4 项失败
// - upload:checking 再加一个正在检查的 sys-wms-generic-0.5.0.zip（一直停在检查中，不会跑完）
// - upload:empty    还没有上传过
// - upload:exists   包仓库里已经有内容不同的 industry-retail 1.1.0：发布它返回 409 pack_version_exists
// 发布 409 也可以自然造出来：再上传一个同名同版本的包（如 industry-retail-1.1.0.zip），检查通过后先发布一个，再发布另一个。
// 新上传的包约 2.4 秒后检查完：文件名以 demo- 开头的当作租户「示例租户」上传、判 4 项失败，其余检查通过；
// 文件名含 nomanifest 返回 400 pack_upload_invalid（缺 manifest.yaml）。
// 别的分组的 mock（PL-04 申请平台安装、EV-03 数据源登记申请、K-08 知识提名）可以用 addAdminRequest(ctx, 申请) 推进审核申请。
import {
  failedChecks, manifestOf, parseFilename, passedChecks, reportMarkdown, seedRequests, seedUploads, summaryOf,
} from './pack-uploads-data.js';

const ADMIN = ['admin'];
const CHECK_MS = 2400;
const LIMIT = 200 * 1024 * 1024;
const PRIVATE_LIMIT = 20 * 1024 * 1024;

// PL-14 的一项（PackUpload）；full 时加 manifest、checks、timeline（PL-15）。同后端 _upload_view
export function uploadView(rec, full = false) {
  const view = {
    id: rec.id, filename: rec.filename, size: rec.size, uploaded_at: rec.uploaded_at, uploaded_by: rec.uploaded_by,
    tenant_label: rec.tenant_label, scope: rec.scope || null, state: rec.state, summary: rec.summary, manifest_ok: rec.manifest_ok, checksums_ok: rec.checksums_ok,
    name: rec.name, version: rec.version,
  };
  return full ? { ...view, manifest: rec.manifest, checks: rec.checks, timeline: rec.timeline } : view;
}

/** 静态检查跑完：按文件名判结果（mock 不解包） */
export function finishCheck(rec, now = Date.now()) {
  const { name, version } = parseFilename(rec.filename);
  const checks = rec.tenant_label || (rec.scope === 'private' && /^demo-/i.test(rec.filename)) ? failedChecks(name) : passedChecks(name, version, { files: Math.max(12, Math.round(rec.size / 2048)) });
  const summary = summaryOf(checks);
  const kind = name.startsWith('industry-') || name.startsWith('sys-') ? 'knowledge' : 'capability';
  Object.assign(rec, {
    state: summary.fail ? 'failed' : 'passed', summary, checks, name, version,
    manifest_ok: true, checksums_ok: true,
    manifest: manifestOf(name, version, kind, { size: rec.size * 3, files: Math.max(12, Math.round(rec.size / 2048)), contributes: kind === 'knowledge' ? { templates: 2 } : { commands: 1, scripts: 1 } }),
  });
  rec.timeline.push({ at: new Date(now).toISOString(), actor: 'system', text: summary.fail ? '静态检查未通过' : '静态检查通过', state: rec.state });
  return rec;
}

const requestsOf = (db) => {
  db.adminRequests = db.adminRequests || seedRequests();
  return db.adminRequests;
};

/** 别的分组推一条待处理的申请进来（管理员收到 request_pending 通知） */
export function addAdminRequest(ctx, req) {
  const item = { id: `req-${ctx.uid()}`, state: 'pending', requested_at: new Date().toISOString(), payload: {}, ...req };
  requestsOf(ctx.db).push(item);
  ctx.emitUser('notification', {
    id: `n-${item.id}`, kind: 'request_pending', title: '有新的申请待处理', body: item.summary, project_id: item.project_id || null,
    project_name: item.payload?.project_name || null, action: null, created_at: item.requested_at, read: false,
  });
  return item;
}

export function register(ctx) {
  const { on, json, err, db, sc, uid, F, forbid } = ctx;
  const flags = new Set(String(sc.upload || '').split('+').filter(Boolean));
  if (!db.packUploads) {
    db.packUploads = seedUploads(Date.now(), { empty: flags.has('empty'), checking: flags.has('checking') });
    // 包仓库里已有的版本（name@version → 内容摘要）；upload:exists 时 industry-retail 1.1.0 已在仓库里且内容不同
    db.packRepo = new Map(F.packs().map((p) => [`${p.name}@${p.latest}`, `builtin-${p.name}`]));
    if (flags.has('exists')) db.packRepo.set('industry-retail@1.1.0', 'other-upload');
  }
  const find = (id) => db.packUploads.find((u) => u.id === decodeURIComponent(id));
  const notFound = () => err(404, 'not_found', '上传不存在');

  const receive = (user, form, { limit, scope = null }) => {
    const file = form?.get('file');
    if (!file || typeof file === 'string') return err(400, 'invalid_argument', '缺少文件', { fields: { file: 'required' } });
    if (file.size > limit) return err(413, 'payload_too_large', `插件包超过 ${limit / 1024 / 1024} MiB`, { limit_bytes: limit });
    if (!/\.zip$/i.test(file.name)) return err(400, 'pack_upload_invalid', '不是有效的 zip 包', { reason: 'not_zip' });
    if (/nomanifest/i.test(file.name)) return err(400, 'pack_upload_invalid', '包里缺少 manifest.yaml', { reason: 'manifest_missing' });
    const at = new Date().toISOString();
    const rec = {
      id: `up-${uid()}${Date.now().toString(16).slice(-7)}`, filename: file.name, size: file.size, uploaded_at: at, uploaded_by: user.username,
      tenant_label: scope ? null : form.get('tenant_label') || (/^demo-/i.test(file.name) ? '示例租户' : null), scope, state: 'checking', summary: { pass: 0, warn: 0, fail: 0 }, manifest_ok: false, checksums_ok: false,
      name: null, version: null, manifest: null, checks: [], timeline: [{ at, actor: user.username, text: '上传', state: 'checking' }],
    };
    db.packUploads.unshift(rec);
    setTimeout(() => finishCheck(rec), CHECK_MS)?.unref?.(); // 单测里不拖住进程
    return json(202, uploadView(rec));
  };

  const reportResponse = (rec) => {
    if (rec.state === 'checking') return err(404, 'not_found', '检查报告还没生成');
    return new Response(reportMarkdown(rec), {
      status: 200, headers: { 'Content-Type': 'text/markdown; charset=utf-8', 'Content-Disposition': `attachment; filename="${rec.id}-report.md"` },
    });
  };

  // PL-13 上传（multipart：file、tenant_label?）；静态检查在响应之后跑
  on('POST', '/admin/packs/uploads', ({ user, form }) => forbid(user, ADMIN) || receive(user, form, { limit: LIMIT }));

  // PL-14 上传列表（state 可逗号分隔；mock 不分页）
  on('GET', '/admin/packs/uploads', ({ user, query }) => forbid(user, ADMIN) || (() => {
    const states = (query.get('state') || '').split(',').filter(Boolean);
    const items = db.packUploads
      .filter((u) => !states.length || states.includes(u.state))
      .sort((a, b) => Date.parse(b.uploaded_at) - Date.parse(a.uploaded_at))
      .map((u) => uploadView(u));
    return json(200, { items, next_cursor: null });
  })());

  // PL-15 详情与静态检查
  on('GET', '/admin/packs/uploads/([^/]+)', ({ user, m }) => forbid(user, ADMIN) || (find(m[1]) ? json(200, uploadView(find(m[1]), true)) : notFound()));

  // PL-16 检查报告（text/markdown 附件）
  on('GET', '/admin/packs/uploads/([^/]+)/report', ({ user, m }) => forbid(user, ADMIN) || (() => {
    const rec = find(m[1]);
    return rec ? reportResponse(rec) : notFound();
  })());

  // PL-17 发布到通道：rollout、auto_breaker 原样存进通道记录（B3a 第 12 条）
  on('POST', '/admin/packs/uploads/([^/]+)/publish', ({ user, m, body }) => forbid(user, ADMIN) || (() => {
    const rec = find(m[1]);
    if (!rec) return notFound();
    if (rec.state === 'published' || rec.state === 'rejected') return err(409, 'pack_upload_state', '这个上传已经发布或退回', { state: rec.state });
    if (rec.state === 'failed' || rec.state === 'checking') {
      return err(422, 'static_check_failed', '静态检查有失败项，不能发布', { failed: rec.checks.filter((c) => c.result === 'fail').map((c) => c.label) });
    }
    if (!['beta', 'stable'].includes(body.channel)) return err(400, 'invalid_argument', 'channel 只能是 stable 或 beta', { fields: { channel: 'enum' } });
    const r = body.rollout || {};
    if (r.percent != null && !(Number.isInteger(r.percent) && r.percent >= 0 && r.percent <= 100)) {
      return err(400, 'invalid_argument', '灰度百分比要在 0～100 之间', { fields: { 'rollout.percent': 'range' } });
    }
    const key = `${rec.name}@${rec.version}`;
    if (db.packRepo.has(key) && db.packRepo.get(key) !== rec.id) {
      return err(409, 'pack_version_exists', `${rec.name} ${rec.version} 已在包仓库里且内容不同；版本目录不可修改，请换一个版本号`);
    }
    db.packRepo.set(key, rec.id);
    Object.assign(rec, { state: 'published', channel: body.channel, rollout: r, auto_breaker: !!body.auto_breaker });
    rec.timeline.push({ at: new Date().toISOString(), actor: user.username, text: `发布到 ${body.channel}`, state: 'published' });
    const label = F.packs().find((p) => p.name === rec.name)?.label || rec.name;
    return json(200, { pack: { name: rec.name, version: rec.version, label }, channel: body.channel, rollout: r });
  })());

  // PL-18 退回（reason 必填；不通知上传者，B3a 第 13 条）
  on('POST', '/admin/packs/uploads/([^/]+)/reject', ({ user, m, body }) => forbid(user, ADMIN) || (() => {
    const rec = find(m[1]);
    if (!rec) return notFound();
    const reason = String(body.reason || '').trim();
    if (!reason) return err(400, 'invalid_argument', 'reason 必填', { fields: { reason: 'required' } });
    if (rec.state === 'published' || rec.state === 'rejected') return err(409, 'pack_upload_state', '这个上传已经发布或退回', { state: rec.state });
    rec.state = 'rejected';
    rec.timeline.push({ at: new Date().toISOString(), actor: user.username, text: `退回：${reason}`, state: 'rejected' });
    return json(200, uploadView(rec));
  })());

  // ---------- PL-22～PL-26 自己的插件包（登录即可，只看得到自己的） ----------
  const mine = (user, id) => {
    const rec = find(id);
    return rec && rec.scope === 'private' && rec.uploaded_by === user.username ? rec : null;
  };
  on('POST', '/me/pack-uploads', ({ user, form }) => receive(user, form, { limit: PRIVATE_LIMIT, scope: 'private' }));
  on('GET', '/me/pack-uploads', ({ user }) => json(200, {
    items: db.packUploads.filter((u) => u.scope === 'private' && u.uploaded_by === user.username).map((u) => uploadView(u)),
    next_cursor: null,
  }));
  on('GET', '/me/pack-uploads/([^/]+)', ({ user, m }) => (mine(user, m[1]) ? json(200, uploadView(mine(user, m[1]), true)) : notFound()));
  on('GET', '/me/pack-uploads/([^/]+)/report', ({ user, m }) => (mine(user, m[1]) ? reportResponse(mine(user, m[1])) : notFound()));
  on('POST', '/me/pack-uploads/([^/]+)/install', ({ user, m }) => {
    const rec = mine(user, m[1]);
    if (!rec) return notFound();
    if (rec.state === 'failed' || rec.state === 'checking') {
      return err(422, 'static_check_failed', '静态检查有失败项，不能安装', { failed: rec.checks.filter((c) => c.result === 'fail').map((c) => c.label) });
    }
    if (rec.state === 'passed') {
      if (F.packs().some((p) => p.name === rec.name)) return err(409, 'pack_name_taken', `包名 ${rec.name} 已被占用，请换一个名字`);
      rec.state = 'installed';
      rec.timeline.push({ at: new Date().toISOString(), actor: user.username, text: '安装', state: 'installed' });
      const base = F.packs().find((p) => p.category === 'capability');
      db.privateCards = [...(db.privateCards || []), {
        ...base, name: rec.name, label: rec.name, latest: rec.version, version: rec.version, description: '', publisher: { kind: 'private', label: '仅自己可见' },
      }];
    }
    return json(200, uploadView(rec));
  });

  // ---------- AD-08～AD-10 管理员审核申请（插件安装页的「申请」页签） ----------
  const findReq = (id) => requestsOf(db).find((r) => r.id === decodeURIComponent(id));

  on('GET', '/admin/requests', ({ user, query }) => forbid(user, ADMIN) || (() => {
    const kinds = (query.get('kind') || '').split(',').filter(Boolean);
    const states = (query.get('state') || '').split(',').filter(Boolean);
    const items = requestsOf(db)
      .filter((r) => (!kinds.length || kinds.includes(r.kind)) && (!states.length || states.includes(r.state)))
      .sort((a, b) => Date.parse(b.requested_at) - Date.parse(a.requested_at));
    return json(200, { items, next_cursor: null });
  })());

  const decide = (action) => ({ user, m, body }) => forbid(user, ADMIN) || (() => {
    const r = findReq(m[1]);
    if (!r) return err(404, 'request_not_found', '申请不存在');
    if (r.state !== 'pending') return err(409, 'request_state', '这条申请已经处理过了', { state: r.state });
    if (action === 'reject' && !String(body.reason || '').trim()) return err(400, 'invalid_argument', '请写明退回原因', { fields: { reason: 'required' } });
    // 同内核：数据源申请通过要带凭据
    if (action === 'approve' && r.kind === 'source_registration' && !body.secret) return err(400, 'invalid_argument', '通过前要写入凭据', { fields: { secret: 'required' } });
    Object.assign(r, {
      state: action === 'approve' ? 'approved' : 'rejected', reviewed_by: user.username, reviewed_at: new Date().toISOString(),
      note: action === 'approve' ? String(body.note || '').trim() || null : String(body.reason).trim(),
    });
    return json(200, r);
  })();
  on('POST', '/admin/requests/([^/]+)/approve', decide('approve'));
  on('POST', '/admin/requests/([^/]+)/reject', decide('reject'));
}

