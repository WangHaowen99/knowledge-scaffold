// mock：工作台插件开发模式（ADR-0087、契约 F17）——插件对话、PD-01～PD-05、plugin_draft 卡、plugin_drafts.changed、安装通知、
// 草稿装的包的停用（PL-09）和撤回这一轮（CS-02、CS-05）。合成场景：门店补货项目的「命名检查」插件。
// 地址参数 ?mock=pm:<状态> 让主示例项目一打开就有一条绑定「命名检查」草稿的插件对话，从输入框的模式菜单「插件开发」进：
// - pm:draft      草稿写好、预检通过，待安装
// - pm:fail       平台预检 2 项没过；「让助手修复」后通过
// - pm:installed  已装 0.1.0 并启用到本项目
// - pm:outdated   装了 0.1.0 之后又改了脚本，待更新
// 不带时没有插件对话：选「插件开发」新建一条，发一句需求后插件助手写出草稿；之后安装、切回建模试用、再改再装都能走通。

const NAME = 'naming-check';
const LABEL = '命名检查';
const ASK = '做一个发布前的命名检查：对象名用名词，操作名用动词开头，同一个意思只用一个叫法';

const manifest = ({ name = NAME, label = LABEL, version = '0.1.0', core = '>=0.5', command = 'naming-check' } = {}) => `name: ${name}
label: ${label}
version: ${version}
category: capability
description: 发布前查对象和操作的叫法，没过挡发布
requires:
  core: "${core}"
provides:
  flow: flow.yaml
  commands: [commands/${command}.md]
  scripts: [scripts/check_names.py]
`;

const FLOW = (command = 'naming-check') => `# 命名检查：发布前查对象和操作的叫法，没过挡发布
probes:
  naming_checked: {files: [naming-check], glob: "report.md"}
  naming_pending: {pending: [naming-check], glob: "report.md"}
nodes:
  - id: naming-check.run
    title: 命名检查
    kind: agent
    placement: spine
    after: core.validate
    display_after: [core.validate]
    done: [naming_checked, "!naming_pending"]
    stale_on: [object, operation]
    blocks: [core.publish]
    produces: [naming-check/report.md]
    entry: {command: /${command}}
    actions:
      - {label: 命名检查, command: /${command}}
`;

const COMMAND = `# 命名检查

读本体里所有对象和操作的名字，按下面三条查：

1. 对象名用名词；
2. 操作名用动词开头；
3. 同一个意思只用一个叫法。

运行 \`scripts/check_names.py\`，把结果写进 \`naming-check/report.md\`。
`;

const SCRIPT = (allowSuffix) => `"""命名检查：对象名用名词，操作名用动词开头，同一个意思只用一个叫法。"""
import json
import sys

VERBS = ("生成", "计算", "核定", "提交", "审批", "检查", "更新", "汇总")
ALLOWED_SUFFIX = ("提示", "清单")


def starts_with_verb(name):
    return name.startswith(VERBS)


def check(operations):
    problems = []
    for name in operations:
${allowSuffix ? '        if not starts_with_verb(name) and not name.endswith(ALLOWED_SUFFIX):' : '        if not starts_with_verb(name):'}
            problems.append(f"操作「{name}」不是动词开头")
    return problems


if __name__ == "__main__":
    for line in check(json.load(sys.stdin)):
        print(line)
`;

// 合成的校验和（不是真的 sha256，只给文件列表和抽屉看）
function checksums(files) {
  const h = (t) => [...t].reduce((a, ch) => (a * 31 + ch.charCodeAt(0)) >>> 0, 7).toString(16).padStart(8, '0').repeat(8);
  return Object.keys(files).filter((p) => p !== 'checksums.sha256').sort().map((p) => `${h(files[p])}  ${p}`).join('\n') + '\n';
}
const withSums = (files) => ({ ...files, 'checksums.sha256': checksums(files) });

function filesV1({ name = NAME, label = LABEL, core = '>=0.5', command = 'naming-check', allowSuffix = false } = {}) {
  return withSums({
    'manifest.yaml': manifest({ name, label, core, command }),
    'flow.yaml': FLOW(command),
    [`commands/${command}.md`]: COMMAND,
    'scripts/check_names.py': SCRIPT(allowSuffix),
  });
}

// 平台预检 14 项（mock 里固定）：没过的项按草稿的 failures 换掉
const CHECKS = [
  ['manifest', '清单能读'], ['name', '包名合法'], ['version', '版本号合法'], ['category', '分类和内容一致'], ['flow', '流程声明合法'],
  ['anchor', '锚点节点存在'], ['commands', '命令文件齐全'], ['scripts', '脚本只写允许的目录'], ['secrets', '不含密钥和连接串'],
  ['size', '文件大小在限制内'], ['checksums', '校验和一致'], ['private_name', '名字没被别的包占用'], ['deps', '依赖的版本平台上有'], ['dir_name', '目录名和包名一致'],
];
const FAILURES = [
  { id: 'private_name', label: '命令和平台上的包重名', detail: '/naming-check 已被平台上的另一个包使用' },
  { id: 'deps', label: '依赖的版本平台上没有', detail: '要求 core >=0.6，平台上最高 0.5' },
];

function precheck(d, full) {
  const failed = new Map((d.failures || []).map((f) => [f.id, f]));
  const items = CHECKS.map(([id, label]) => (failed.has(id) ? { ...failed.get(id), result: 'fail' } : { id, label, result: 'pass', detail: '' }));
  const fail = items.filter((i) => i.result === 'fail');
  return { state: fail.length ? 'failed' : 'passed', summary: { pass: items.length - fail.length, warn: 0, fail: fail.length }, items: full ? items : fail };
}

// 行级差异（最长公共子序列），只留改动和前后各一行，最多 40 行
function lineDiff(a, b) {
  const x = a.replace(/\n$/, '').split('\n');
  const y = b.replace(/\n$/, '').split('\n');
  const dp = Array.from({ length: x.length + 1 }, () => new Array(y.length + 1).fill(0));
  for (let i = x.length - 1; i >= 0; i -= 1) for (let j = y.length - 1; j >= 0; j -= 1) dp[i][j] = x[i] === y[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out = [];
  let i = 0;
  let j = 0;
  while (i < x.length && j < y.length) {
    if (x[i] === y[j]) { out.push({ op: ' ', text: x[i] }); i += 1; j += 1; } else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push({ op: '-', text: x[i] }); i += 1; } else { out.push({ op: '+', text: y[j] }); j += 1; }
  }
  while (i < x.length) { out.push({ op: '-', text: x[i] }); i += 1; }
  while (j < y.length) { out.push({ op: '+', text: y[j] }); j += 1; }
  const changed = (k) => out[k] && out[k].op !== ' ';
  return out.filter((l, k) => changed(k) || changed(k - 1) || changed(k + 1)).slice(0, 40);
}

function installView(d) {
  if (!d.enabled) return { state: 'not_installed', enabled_version: null, changes: [], diff: null };
  const paths = new Set([...Object.keys(d.installedFiles), ...Object.keys(d.files)]);
  paths.delete('checksums.sha256');
  const changes = [...paths].sort().flatMap((path) => {
    const [a, b] = [d.installedFiles[path], d.files[path]];
    if (a === b) return [];
    return [{ path, status: a == null ? 'added' : b == null ? 'deleted' : 'modified' }];
  });
  const first = changes.find((c) => c.status === 'modified');
  return {
    state: changes.length ? 'outdated' : 'installed',
    enabled_version: d.enabled,
    changes,
    diff: first ? { path: first.path, lines: lineDiff(d.installedFiles[first.path], d.files[first.path]) } : null,
  };
}

const bump = (v) => v.replace(/(\d+)$/, (n) => String(Number(n) + 1));
function nextVersion(d) {
  let v = d.version;
  while (d.versions.includes(v)) v = bump(v);
  return v;
}

function draftView(d, full = true) {
  const paths = Object.keys(d.files).sort();
  return {
    name: d.name, label: d.label, description: '发布前查对象和操作的叫法，没过挡发布', kind: 'capability', category: 'capability', version: d.version, manifest_ok: true,
    file_count: paths.length,
    ...(full ? { files: paths.map((path) => ({ path, size: new TextEncoder().encode(d.files[path]).length })) } : {}),
    updated_at: d.updated_at, conversation_id: d.cid,
    contents: {
      nodes: [{ id: 'naming-check.run', title: '命名检查', kind: 'agent', placement: 'spine', section: 'required', after: 'core.validate', after_title: '校验', before: null, before_title: null, blocks_publish: true }],
      commands: [{ name: d.command, label: d.command === 'naming-check' ? '命名检查' : '检查叫法' }], agents: [], skills: [], scripts: ['check_names.py'], knowledge: null,
    },
    precheck: precheck(d, full),
    install: { ...installView(d), installed_versions: [...d.versions], next_version: nextVersion(d) },
  };
}

const todayAt = (h, m) => { const t = new Date(); t.setHours(h, m, 0, 0); return t.toISOString(); };

export function register(ctx) {
  const {
    on, json, err, wait, db, hub, sc, uid, convKey, convsOf, flowOf, emitConv, emitProj, setSession, later, replyHooks, convSeeders,
  } = ctx;
  db.pluginDrafts = new Map(); // pid → Map(name → 草稿)
  const draftsOf = (pid) => {
    convsOf(pid);
    if (!db.pluginDrafts.has(pid)) db.pluginDrafts.set(pid, new Map());
    return db.pluginDrafts.get(pid);
  };
  const changed = (pid, name) => emitProj(pid, 'plugin_drafts.changed', { names: [name] });

  // 装上 / 停用：本项目流程里多一个（少一个）「命名检查」节点，flow_hash 变了，前端重取 FL-01
  function setFlowNode(pid, on_) {
    const f = flowOf(pid);
    const id = 'naming-check.run';
    const has = f.nodes.some((n) => n.id === id);
    if (on_ === has) return;
    const validate = f.nodes.find((n) => n.id === 'core.validate');
    const nextEdge = f.edges.find((e) => e.from === 'core.validate');
    if (on_) {
      f.nodes.splice(f.nodes.indexOf(validate) + 1, 0, {
        ...validate, id, title: LABEL, state: 'ready', display_state: 'pending', progress: { label: '3 处' }, version: `${NAME}@0.1.0`, note: null, blocks: ['core.publish'],
        actions: [{ kind: 'start', label: LABEL, target: `/${NAME}` }], plugin: { name: NAME, label: LABEL },
      });
      f.edges = [...f.edges.filter((e) => e !== nextEdge), { from: 'core.validate', to: id }, ...(nextEdge ? [{ from: id, to: nextEdge.to }] : [])];
    } else {
      const out = f.edges.find((e) => e.from === id);
      f.nodes = f.nodes.filter((n) => n.id !== id);
      f.edges = [...f.edges.filter((e) => e.from !== id && e.to !== id), ...(out ? [{ from: 'core.validate', to: out.to }] : [])];
    }
    f.flow_hash = `${f.flow_hash.slice(0, 6)}${uid()}`.slice(0, 10);
    emitProj(pid, 'flow.updated', { flow_hash: f.flow_hash, nodes: [], slices: [], suggestions_changed: true });
  }

  // ---------- 预置的插件对话（?mock=pm:...） ----------
  const PLUGIN_CID = 'p5e1a2b3c4d6';
  const SEEDS = ['draft', 'fail', 'installed', 'outdated'];
  convSeeders.push((pid, list) => {
    if (pid !== ctx.F.MAIN_PID || !SEEDS.includes(sc.pm)) return;
    const at = todayAt(14, 4);
    list.push({ id: PLUGIN_CID, title: LABEL, title_manual: false, created_at: todayAt(14, 0), updated_at: at, live: false, state: 'idle', kind: 'plugin', plugin: NAME, run_id: null });
    const fail = sc.pm === 'fail';
    const installed = ['installed', 'outdated'].includes(sc.pm);
    const v1 = filesV1(fail ? { core: '>=0.6' } : {});
    const d = {
      name: NAME, label: LABEL, version: '0.1.0', command: 'naming-check', cid: PLUGIN_CID, updated_at: at,
      files: sc.pm === 'outdated' ? filesV1({ allowSuffix: true }) : v1, failures: fail ? FAILURES : [],
      versions: installed ? ['0.1.0'] : [], enabled: installed ? '0.1.0' : null, installedFiles: installed ? v1 : null,
    };
    if (!db.pluginDrafts.has(pid)) db.pluginDrafts.set(pid, new Map());
    db.pluginDrafts.get(pid).set(NAME, d);
    if (installed) setTimeout(() => setFlowNode(pid, true), 0);
    db.changesets.set('cs-pm-1', { owner: 'plugin-dev', state: 'merged', pid, cid: PLUGIN_CID, name: NAME, before: null });
    let n = 0;
    const ev = (type, data, ts = at) => { n += 1; return { id: String(n), type, ts, data }; };
    const asst = (text) => ev('message.assistant', { message_id: `m-pm-${n + 1}`, text_md: text, anchors: [], entry_links: [], continued: false });
    const proc = (group, title, desc, ms, steps) => ev('progress', {
      group_id: group, title, desc, state: 'done', started_at: at, duration_ms: ms, collapsed: true,
      steps: steps.map(([text, meta], i) => ({ id: `s${i + 1}`, text, meta, state: 'done', duration_ms: null })),
    });
    const events = [
      ev('day.marker', { label: '今天' }),
      ev('message.user', { message_id: 'm-pm-u1', text: ASK, attachments: [], references: [], origin: null, delivery: 'immediate' }),
      asst('好。先看这个项目现在的叫法，再按开发说明写草稿。'),
      proc('pg-pm-1', '已完成 4 步', '读开发说明 · 读现有叫法 · 写草稿 · 本地检查', 134000, [
        ['读开发说明：能力包协议、挡发布的步骤怎么写', '6 秒'], ['读了 18 个对象、9 个操作的名字，有 3 处不统一', '21 秒'],
        ['写了 5 个文件：清单、流程声明、1 条命令、1 个脚本、校验和', '1 分 38 秒'], ['本地检查 14 项通过', '9 秒'],
      ]),
      asst(fail ? '草稿写好了，本地检查通过。平台预检有 2 项没过，见下面。'
        : '草稿写好了。启用后「校验」后面多一步「命名检查」，有不合规的叫法就挡住发布。按现在的叫法会查出 3 处。'),
      ev('card', { kind: 'plugin_draft', name: NAME, version: '0.1.0', changed_files: Object.keys(v1), changeset_id: 'cs-pm-1' }),
    ];
    if (installed) events.push(ev('notice', { kind: 'plugin_installed', text: `「${LABEL}」0.1.0 已装好，启用到本项目` }));
    if (sc.pm === 'outdated') {
      db.changesets.set('cs-pm-2', { owner: 'plugin-dev', state: 'merged', pid, cid: PLUGIN_CID, name: NAME, before: { files: v1, failures: [] } });
      events.push(
        ev('message.user', { message_id: 'm-pm-u2', text: '以「提示」「清单」结尾的操作名不用报。顺便把那 3 个名字改了', attachments: [], references: [], origin: null, delivery: 'immediate' }),
        proc('pg-pm-2', '已完成 2 步', '改规则 · 本地检查', 41000, [['改了 scripts/check_names.py：以「提示」「清单」结尾的不报', '32 秒'], ['本地检查 14 项通过', '9 秒']]),
        asst('规则改好了，再装时版本号自动升到 0.1.1。改名要动本体，插件开发里本体只读，切回建模后按检查结果改。'),
        ev('card', { kind: 'plugin_draft', name: NAME, version: '0.1.0', changed_files: ['scripts/check_names.py'], changeset_id: 'cs-pm-2' }),
      );
    }
    hub.seed(convKey(pid, PLUGIN_CID), 'conversation', events);
  });

  // ---------- 插件助手的一轮 ----------
  const convOf = (pid, cid) => convsOf(pid).find((c) => c.id === cid);
  replyHooks.push({
    match: (pid, cid) => convOf(pid, cid)?.kind === 'plugin',
    run(pid, cid, text) {
      const c = convOf(pid, cid);
      const drafts = draftsOf(pid);
      const d = c.plugin ? drafts.get(c.plugin) : null;
      const gid = `pg-${uid()}`;
      setSession(pid, cid, 'running');
      if (!d) emitConv(pid, cid, 'message.assistant', { message_id: `m-${uid()}`, text_md: '好。先看这个项目现在的叫法，再按开发说明写草稿。', anchors: [], entry_links: [], continued: false });
      emitConv(pid, cid, 'progress', { group_id: gid, title: '', desc: '', state: 'running', started_at: new Date().toISOString(), duration_ms: null, collapsed: false, steps: [{ id: 's1', text: d ? '改草稿' : '读开发说明', meta: '进行中', state: 'running', duration_ms: null }] });
      later(pid, cid, sc.reply === 'slow' ? 90000 : 1500, () => {
        const now = new Date().toISOString();
        let draft = d;
        let reply;
        let steps;
        const before = d ? { files: { ...d.files }, failures: [...(d.failures || [])] } : null;
        if (!draft) {
          // 第一轮：写出草稿，对话绑定这个包名；同一项目已有同名草稿时加序号
          let k = 1;
          while (drafts.has(k === 1 ? NAME : `${NAME}-${k}`)) k += 1;
          const name = k === 1 ? NAME : `${NAME}-${k}`;
          const label = k === 1 ? LABEL : `${LABEL} ${k}`;
          draft = { name, label, version: '0.1.0', command: 'naming-check', cid, updated_at: now, files: filesV1({ name, label }), failures: [], versions: [], enabled: null, installedFiles: null };
          drafts.set(name, draft);
          Object.assign(c, { plugin: name, updated_at: now, ...(c.title_manual ? {} : { title: label }) });
          emitProj(pid, 'conversation.updated', { conversation: c, deleted: false });
          steps = [['读开发说明：能力包协议、挡发布的步骤怎么写', '6 秒'], ['读了 18 个对象、9 个操作的名字，有 3 处不统一', '21 秒'], ['写了 5 个文件：清单、流程声明、1 条命令、1 个脚本、校验和', '1 分 38 秒'], ['本地检查 14 项通过', '9 秒']];
          reply = '草稿写好了。启用后「校验」后面多一步「命名检查」，有不合规的叫法就挡住发布。按现在的叫法会查出 3 处。';
        } else if (draft.failures?.length) {
          // 预检没过：改命令名、放宽依赖
          draft.command = 'check-naming';
          draft.files = filesV1({ name: draft.name, label: draft.label, command: 'check-naming', allowSuffix: draft.files['scripts/check_names.py']?.includes('ALLOWED_SUFFIX):') });
          draft.failures = [];
          steps = [['命令改名为 /check-naming', '12 秒'], ['依赖改成 core >=0.5', '4 秒'], ['本地检查 14 项通过', '9 秒']];
          reply = '改好了：命令改名为「检查叫法」，依赖改成 core >=0.5。本地检查通过。';
        } else if (!draft.files['scripts/check_names.py'].includes('ALLOWED_SUFFIX):')) {
          draft.files = withSums({ ...draft.files, 'scripts/check_names.py': SCRIPT(true) });
          steps = [['改了 scripts/check_names.py：以「提示」「清单」结尾的不报', '32 秒'], ['本地检查 14 项通过', '9 秒']];
          reply = draft.enabled ? `规则改好了，再装时版本号自动升到 ${nextVersion(draft)}。` : '规则改好了。';
        } else {
          draft.files = withSums({ ...draft.files, [`commands/${draft.command}.md`]: `${draft.files[`commands/${draft.command}.md`] || COMMAND}\n按「${text.slice(0, 20)}」补了一条说明。\n` });
          steps = [[`改了 commands/${draft.command}.md`, '18 秒'], ['本地检查 14 项通过', '9 秒']];
          reply = '改好了。';
        }
        draft.updated_at = now;
        const changedFiles = Object.keys(draft.files).filter((p) => !before || before.files[p] !== draft.files[p]);
        const cs = `cs-pm-${uid()}`;
        db.changesets.set(cs, { owner: 'plugin-dev', state: 'merged', pid, cid, name: draft.name, before });
        emitConv(pid, cid, 'progress.updated', {
          group_id: gid, title: `已完成 ${steps.length} 步`, desc: steps.map(([t]) => t.split('：')[0]).join(' · '), state: 'done', started_at: now, duration_ms: 60000, collapsed: true,
          steps: steps.map(([t, meta], i) => ({ id: `s${i + 1}`, text: t, meta, state: 'done', duration_ms: null })),
        });
        emitConv(pid, cid, 'message.assistant', { message_id: `m-${uid()}`, text_md: reply, anchors: [], entry_links: [], continued: false });
        emitConv(pid, cid, 'card', { kind: 'plugin_draft', name: draft.name, version: draft.version, changed_files: changedFiles, changeset_id: cs });
        emitConv(pid, cid, 'turn.completed', { turn_id: `t-${uid()}`, duration_ms: 60000, is_error: false, usage: { input_tokens: 12000, output_tokens: 900, cache_read_tokens: 0 }, cost: { amount: 0.21, currency: 'CNY' } });
        setSession(pid, cid, 'idle');
        changed(pid, draft.name);
      });
    },
  });

  // ---------- PD-01～PD-05 ----------
  const P = '/projects/([^/]+)/plugin-drafts';
  const notFound = () => err(404, 'not_found', '草稿不存在');
  on('GET', P, ({ m }) => json(200, { items: [...draftsOf(m[1]).values()].sort((a, b) => b.updated_at.localeCompare(a.updated_at)).map((d) => draftView(d, false)) }));
  on('GET', `${P}/([^/]+)`, ({ m }) => {
    const d = draftsOf(m[1]).get(decodeURIComponent(m[2]));
    return d ? json(200, draftView(d)) : notFound();
  });
  on('GET', `${P}/([^/]+)/files`, ({ m, query }) => {
    const d = draftsOf(m[1]).get(decodeURIComponent(m[2]));
    if (!d) return notFound();
    const path = query.get('path') || '';
    if (!(path in d.files)) return err(404, 'not_found', '文件不存在');
    const content = d.files[path];
    return json(200, { path, content, size: new TextEncoder().encode(content).length, binary: false });
  });
  on('PUT', `${P}/([^/]+)/files`, ({ m, body }) => {
    const [pid, name] = [m[1], decodeURIComponent(m[2])];
    const d = draftsOf(pid).get(name);
    if (!d) return notFound();
    if (!(body.path in d.files) || body.path === 'checksums.sha256') return err(400, 'invalid_argument', '这个文件不能在网页上改');
    if (convOf(pid, d.cid)?.state === 'running') return err(409, 'session_busy', '插件对话正在运行');
    d.files = withSums({ ...d.files, [body.path]: String(body.content ?? '') });
    d.updated_at = new Date().toISOString();
    changed(pid, name);
    return json(200, { path: body.path, size: new TextEncoder().encode(d.files[body.path]).length });
  });
  on('POST', `${P}/([^/]+)/install`, async ({ m }) => {
    const [pid, name] = [m[1], decodeURIComponent(m[2])];
    const d = draftsOf(pid).get(name);
    if (!d) return notFound();
    await wait(700);
    const pre = precheck(d, true);
    if (pre.state === 'failed') return err(422, 'static_check_failed', '静态检查有失败项，不能安装', { items: pre.items.filter((i) => i.result === 'fail') });
    // 版本号已在包仓库里：升补丁号写回草稿（平台提交）
    const version = nextVersion(d);
    if (version !== d.version) {
      d.version = version;
      d.files = withSums({ ...d.files, 'manifest.yaml': d.files['manifest.yaml'].replace(/^version: .*$/m, `version: ${version}`) });
      d.updated_at = new Date().toISOString();
    }
    d.versions.push(version);
    d.enabled = version;
    d.installedFiles = { ...d.files };
    setFlowNode(pid, true);
    emitConv(pid, d.cid, 'notice', { kind: 'plugin_installed', text: `「${d.label}」${version} 已装好，启用到本项目` });
    changed(pid, name);
    return json(200, { draft: draftView(d), version, upload_id: `up-${uid()}`, enabled: { version, effective: 'next_message' } });
  });
  // PL-09：草稿装的包在这里停用，其余交给项目设置那一组
  on('POST', '/projects/([^/]+)/packs/([^/]+)/disable', ({ m }) => {
    const [pid, name] = [m[1], decodeURIComponent(m[2])];
    const d = draftsOf(pid).get(name);
    if (!d) return undefined;
    if (!d.enabled) return err(409, 'pack_not_enabled', '本项目没有启用这个能力');
    d.enabled = null;
    d.installedFiles = null;
    setFlowNode(pid, false);
    changed(pid, name);
    return json(200, { pack: { name, label: d.label, category: 'capability', status: 'disabled', locked_version: null, pending: null }, effective: 'immediate' });
  });

  // ---------- 插件对话那一轮的变更集：CS-02、CS-05 ----------
  on('GET', '/projects/([^/]+)/changesets/([^/]+)', ({ m }) => {
    const cs = db.changesets.get(m[2]);
    if (cs?.owner !== 'plugin-dev') return undefined;
    return json(200, { id: m[2], source: 'assistant', build_mode: 'L2', purpose: '插件草稿', state: cs.state, base: 'a71e03c9', conversation_id: cs.cid, run_id: null, slice_id: null, created_at: null, updated_at: null, merged: null, entity_count: 0, groups: [], checks: { errors: 0, warnings: 0, findings: [] }, next_version_label: null });
  });
  on('POST', '/projects/([^/]+)/changesets/([^/]+)/discard', ({ m }) => {
    const [pid, id] = [m[1], m[2]];
    const cs = db.changesets.get(id);
    if (cs?.owner !== 'plugin-dev') return undefined;
    if (cs.state !== 'merged') return err(409, 'changeset_closed', '这一轮已经撤回过', { state: cs.state });
    const drafts = draftsOf(pid);
    const d = drafts.get(cs.name);
    cs.state = 'reverted';
    if (!cs.before) drafts.delete(cs.name);
    else if (d) Object.assign(d, { files: cs.before.files, failures: cs.before.failures, updated_at: new Date().toISOString() });
    emitConv(pid, cs.cid, 'notice', { kind: 'discarded', text: '已撤回这一轮', changeset_id: id, align: 'center' });
    changed(pid, cs.name);
    return json(200, { state: 'reverted', commit: 'c0ffee21', version_label: null });
  });
}
