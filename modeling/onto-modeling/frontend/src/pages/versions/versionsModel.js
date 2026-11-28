// 版本历史与对比（设计稿 versions）的纯逻辑：来源标记、落地选中哪个版本、逐行对比、回退说明、标记校验
import { isLocked } from '../../state/readonly.js';

// 来源：契约 VersionSource → [界面叫法, 设计稿 mr-src 的配色类]；回退没有设计稿配色，用产品补的 r
export const SOURCES = [
  ['assistant', '助手', 'a'],
  ['web', '网页', 'w'],
  ['studio', 'Studio', 's'],
  ['mcp', 'MCP', 'm'],
  ['autobuild', '自主构建', 'l4'],
  ['import', '导入', 'i'],
  ['plugin', '插件', 'p'],
  ['revert', '回退', 'r'],
];
const SRC = Object.fromEntries(SOURCES.map(([k, t, c]) => [k, { t, c }]));

export const srcClass = (source) => SRC[source]?.c || 'w';
export const srcLabel = (v) => v?.source_label || SRC[v?.source]?.t || v?.source || '';

// 「全部来源」筛选的选项（V-01 source）
export const SOURCE_FILTERS = [{ value: '', label: '全部来源' }, ...SOURCES.map(([k, t]) => ({ value: k, label: t }))];

const pad = (x) => String(x).padStart(2, '0');
// 10-31 15:21
export function fmtStamp(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// 条目 / 产物的说法：对象「陈列量标准」、场景「场景 3」
const KIND_LABEL = { object: '对象', operation: '操作', rule: '规则', metric: '指标', script: '脚本', flowchart: '流程图' };
const ARTIFACT_LABEL = { 'trace.scenario': '场景', 'trace.mock': '模拟数据', 'diagram.flowchart': '流程图', 'codegen.solver_contract': '求解契约' };
export function targetLabel(t) {
  if (!t) return '';
  if (typeof t.label === 'string' && t.label) return t.label; // 后端按元模型、产物登记算好的业务名（F4b-paths）
  if (t.type) return `${ARTIFACT_LABEL[t.type] || '产物'}「${t.title || t.key}」`;
  return `${KIND_LABEL[t.kind] || '条目'}「${t.name || t.key}」`;
}

// 条目在知识包里的默认路径（元数据的目录约定：ontology/<类型>/<名称>.md，脚本在 scripts/）
export function entryPath(ref) {
  if (!ref?.kind || !KIND_LABEL[ref.kind] || ref.kind === 'flowchart') return null;
  return ref.kind === 'script' ? `scripts/${ref.name}` : `ontology/${ref.kind}/${ref.name}.md`;
}

// 路径的说法：ontology/object/陈列量标准.md → 对象「陈列量标准」；资料按资料名，蓝图、资料分析报告按固定叫法；
// 认不出的不出文件名（F4b-paths）
export function pathLabel(path) {
  const p = path || '';
  const idx = /^ontology\/(object|operation|rule|metric)\/index\.md$/.exec(p);
  if (idx) return `${KIND_LABEL[idx[1]]}清单`;
  const m = /^ontology\/(object|operation|rule|metric)\/(.+)\.md$/.exec(p);
  if (m) return `${KIND_LABEL[m[1]]}「${m[2]}」`;
  const s = /^scripts\/(.+)$/.exec(p);
  if (s) return `脚本「${s[1]}」`;
  if (p === 'research/blueprint.json') return '建模蓝图';
  if (/^research\/.+\.md$/.test(p)) return '资料分析报告';
  if (/^reports\/.+\.md$/.test(p)) return '检查报告';
  if (/^resource\/(?!text\/|systems\/|materials\.json$)/.test(p)) return `资料「${p.split('/').pop().replace(/\.[^.]+$/, '')}」`;
  return '其他文件';
}

// 某个版本的文件（V-03 抽屉）：按业务类型分组、按业务名列，不出目录和文件名（F4b-paths）；
// 认不出的文件不列，下载的 zip 里照有
const FILE_GROUPS = [
  ['object', '对象', /^ontology\/object\/(.+)\.md$/],
  ['operation', '操作', /^ontology\/operation\/(.+)\.md$/],
  ['rule', '规则', /^ontology\/rule\/(.+)\.md$/],
  ['metric', '指标', /^ontology\/metric\/(.+)\.md$/],
  ['script', '脚本', /^scripts\/(.+)$/],
  ['material', '资料', /^resource\/(?!text\/|systems\/)(?:[^/]+\/)*([^/]+?)(?:\.[^./]+)?$/],
  ['research', '资料分析', /^research\/(blueprint\.json|.+\.md)$/],
];
export function fileGroups(files = []) {
  const groups = FILE_GROUPS.map(([key, label]) => ({ key, label, items: [] }));
  (files || []).forEach((f) => {
    const i = FILE_GROUPS.findIndex(([, , re]) => re.test(f.path) && f.path !== 'resource/materials.json');
    if (i < 0) return;
    const hit = FILE_GROUPS[i][2].exec(f.path)[1];
    const name = FILE_GROUPS[i][0] === 'research' ? pathLabel(f.path) : hit;
    groups[i].items.push({ ...f, name });
  });
  return groups.filter((g) => g.items.length);
}

export const CHANGE_TEXT = { add: '新增', mod: '修改', del: '删除' };
export const changeText = (c) => c.detail || CHANGE_TEXT[c.change] || c.change;

// 「复核」一列：需复核 / 高置信 · 已接受 / 空
export function reviewView(review) {
  if (review === 'needs_review') return { st: 'needs_review' };
  if (review === 'accepted') return { chip: ['高置信 · 已接受', 'blue'] };
  return null;
}

// 「v0.11 改了什么」的副标题和来源说明
export function versionSub(v) {
  if (!v) return '';
  if (v.run) return `自主构建运行 #${v.run.run_no}${v.run.slice_name ? ` · ${v.run.slice_name}` : ''}`;
  return [srcLabel(v), fmtStamp(v.at)].filter(Boolean).join(' · ');
}

// 外壳顶栏「知识包 vX」：版本列表（V-01）知道主线最新版本，元数据落后时跟上
export function syncMeta(meta, list) {
  if (!meta || !list?.current_version_label) return meta;
  if (meta.version_label === list.current_version_label && (!list.head || meta.head === list.head)) return meta;
  return { ...meta, version_label: list.current_version_label, ...(list.head ? { head: list.head } : {}) };
}

// 列表按版本号从新到旧
export const byNewest = (items) => [...(items || [])].sort((a, b) => b.n - a.n);

// 对比左侧取哪个版本：选中的是当前版本时，和它的上一个版本比（看这个版本改了什么）
export function compareFromOf(selected, items) {
  if (!selected) return null;
  if (!selected.current) return selected;
  return byNewest(items).find((v) => v.n < selected.n) || selected;
}

// 某个版本的上一版（first-parent 顺序里紧挨着的旧版本）；第一版没有
export const previousOf = (v, items) => (v ? byNewest(items).find((x) => x.n < v.n) || null : null);
// 选中一个版本时对比什么：看这一版改了什么（上一版 → 这一版；这一版是当前时右侧是可编辑的当前文件）
export function selfRange(v, items) {
  const prev = previousOf(v, items);
  return { from: prev?.commit || null, to: v.current ? 'WORKTREE' : v.commit };
}
// 详情里「对比」的范围下拉：这一版改了什么 / 这一版到现在又变了什么；跳转带来的其他范围原样列一项
export function rangeChoices(v, items, cmp) {
  if (!v) return [];
  const prev = previousOf(v, items);
  const name = (x) => (x ? `迭代 ${x.n}` : '');
  const self = selfRange(v, items);
  const out = [];
  if (prev) out.push({ value: 'self', label: `${name(prev)} → ${v.current ? '当前' : name(v)}`, ...self });
  if (!v.current) out.push({ value: 'now', label: `${name(v)} → 当前`, from: v.commit, to: 'WORKTREE' });
  const hit = out.find((o) => o.from === cmp.from && o.to === (cmp.to || 'WORKTREE'));
  if (!hit && cmp.from) {
    const from = (items || []).find((x) => x.commit === cmp.from);
    const to = cmp.to && cmp.to !== 'WORKTREE' ? (items || []).find((x) => x.commit === cmp.to) : null;
    out.unshift({ value: 'other', label: `${name(from) || '其他版本'} → ${to ? name(to) : '当前'}`, from: cmp.from, to: cmp.to || 'WORKTREE' });
  }
  return out;
}
export const rangeValue = (choices, cmp) => (choices.find((o) => o.from === cmp.from && o.to === (cmp.to || 'WORKTREE'))?.value || '');
// 「改了什么」里认不出的文件（报告、流程记录等）只说个数
export const otherChanges = (changes = []) => changes.filter((c) => c.target?.label === '').length;
export const knownChanges = (changes = []) => changes.filter((c) => c.target?.label !== '');

/**
 * 从别的页跳过来时选中哪个版本：
 * - ?v=v0.14&run=run-3&slice=s2（待处理、自主构建报告、运行矩阵的「查看改动」）：先按版本号找，对不上运行和分段时改按运行 + 分段找；
 * - ?path=&from=<commit>&to=WORKTREE（原文编辑器的「刷新后对比」）：按提交找。
 * 返回 { commit, note }；找不到时 commit 为当前版本，note 说明原因。
 */
export function pickLanding(items, query = {}) {
  const list = byNewest(items);
  const head = list.find((v) => v.current) || list[0] || null;
  if (!head) return { commit: null, note: '' };
  const { v, run, slice, from, path } = query;
  const sameRun = (x) => x.run && (!run || x.run.run_id === run) && (!slice || x.run.slice_id === slice);
  if (path) {
    if (!from) return { commit: head.commit, note: '' };
    const hit = list.find((x) => x.commit === from || (from.length >= 6 && x.commit.startsWith(from)));
    if (hit) return { commit: hit.commit, note: '' };
    return { commit: head.commit, note: '没有找到这个版本，已改为对比上一版' };
  }
  if (!v && !run) return { commit: head.commit, note: '' };
  const byLabel = v ? list.find((x) => x.version_label === v) : null;
  // 版本没带运行信息（P1 的 V-01 run 恒为 null）时只能信版本号
  if (byLabel && (!run || !byLabel.run || sameRun(byLabel))) return { commit: byLabel.commit, note: '' };
  const bySlice = run ? list.find(sameRun) : null;
  if (bySlice) return { commit: bySlice.commit, note: '' };
  if (byLabel) return { commit: byLabel.commit, note: `${byLabel.version_label} 不是这次运行生成的，这部分可能还没做完` };
  return { commit: head.commit, note: `没有找到版本 ${v || ''}，可能已被回退或还没有生成` };
}

// 这一段的名字（落地说明用）：从版本列表里找运行 + 分段
export function sliceNameOf(items, run, slice) {
  const hit = (items || []).find((x) => x.run && x.run.run_id === run && x.run.slice_id === slice);
  return hit?.run?.slice_name || '';
}

// 对比对象的选项：所选版本改过的条目（有文件的才能对比），加上跳转带来的路径
export function compareOptions(changes, extraPath) {
  const out = [];
  const seen = new Set();
  (changes || []).forEach((c) => {
    const p = entryPath(c.target);
    if (!p || seen.has(p)) return;
    seen.add(p);
    out.push({ value: p, label: targetLabel(c.target), entry: c.target });
  });
  if (extraPath && !seen.has(extraPath)) out.unshift({ value: extraPath, label: pathLabel(extraPath), entry: null });
  return out;
}

// 缺省对比哪个：已经在对比的保持不变（换版本时对象不跟着换，同设计稿）> 这个版本改过的第一个对象 > 第一个条目
export function defaultCompare(options, keep) {
  if (keep) return keep;
  const obj = options.find((o) => o.entry?.kind === 'object');
  return (obj || options[0])?.value || null;
}

// ---------- 逐行对比 ----------
export const splitLines = (s) => (s == null || s === '' ? [] : String(s).split('\n'));

/**
 * 按行对比：去掉相同的前后缀，中间做最长公共子序列。
 * 返回 left / right（{n, t, k}，k='d' 左侧删掉或改掉的行，'a' 右侧新增或改后的行）和 hunks（同契约 V-04）。
 */
export function diffLines(a, b) {
  const A = splitLines(a);
  const B = splitLines(b);
  let pre = 0;
  while (pre < A.length && pre < B.length && A[pre] === B[pre]) pre += 1;
  let suf = 0;
  while (suf < A.length - pre && suf < B.length - pre && A[A.length - 1 - suf] === B[B.length - 1 - suf]) suf += 1;
  const a2 = A.slice(pre, A.length - suf);
  const b2 = B.slice(pre, B.length - suf);
  // ops：'=' 两边都有、'-' 只在左边、'+' 只在右边
  const ops = [];
  if (a2.length && b2.length && a2.length * b2.length <= 4000000) {
    const n = a2.length;
    const m = b2.length;
    const dp = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
    for (let i = n - 1; i >= 0; i -= 1) {
      for (let j = m - 1; j >= 0; j -= 1) {
        dp[i][j] = a2[i] === b2[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      if (a2[i] === b2[j]) { ops.push('='); i += 1; j += 1; } else if (dp[i + 1][j] >= dp[i][j + 1]) { ops.push('-'); i += 1; } else { ops.push('+'); j += 1; }
    }
    while (i < n) { ops.push('-'); i += 1; }
    while (j < m) { ops.push('+'); j += 1; }
  } else {
    a2.forEach(() => ops.push('-'));
    b2.forEach(() => ops.push('+'));
  }
  const all = [...Array(pre).fill('='), ...ops, ...Array(suf).fill('=')];
  const left = [];
  const right = [];
  const hunks = [];
  let li = 0;
  let ri = 0;
  let cur = null;
  all.forEach((op) => {
    if (op === '=') {
      left.push({ n: li + 1, t: A[li], k: '' });
      right.push({ n: ri + 1, t: B[ri], k: '' });
      li += 1;
      ri += 1;
      cur = null;
      return;
    }
    if (!cur) {
      cur = { left_start: li + 1, left_lines: 0, right_start: ri + 1, right_lines: 0 };
      hunks.push(cur);
    }
    if (op === '-') { left.push({ n: li + 1, t: A[li], k: 'd' }); li += 1; cur.left_lines += 1; } else { right.push({ n: ri + 1, t: B[ri], k: 'a' }); ri += 1; cur.right_lines += 1; }
  });
  return { left: left.sort((x, y) => x.n - y.n), right: right.sort((x, y) => x.n - y.n), hunks };
}

// 右侧（当前文件）里标绿的行号
export const addedLines = (a, b) => diffLines(a, b).right.filter((x) => x.k === 'a').map((x) => x.n);

// 「未保存 N 处」：编辑后的内容和打开时的当前文件相比，有几处改动
export const editBlocks = (orig, cur) => (orig === cur ? 0 : diffLines(orig, cur).hunks.length);

// ---------- 回退 ----------
// v0.12、v0.13；超过 3 个写成 v0.15～v0.19
export function undoneText(labels) {
  const l = labels || [];
  if (!l.length) return '';
  if (l.length <= 3) return l.join('、');
  return `${l[0]}～${l[l.length - 1]}`;
}

// 回退确认框的说明（develop 原文）：按业务文件计数，项目设置与运行状态不随回退
export function revertConfirmText(changes) {
  const n = (key) => (changes?.[key] || []).length;
  return `将新增 ${n('added')}、修改 ${n('modified')}、删除 ${n('deleted')} 个业务文件；项目设置和运行状态不会回退。`;
}

// 预览说不能回退时的提示：各原因用「；」连起来
export function revertBlockedText(preview) {
  const reasons = Array.isArray(preview?.blockers) ? preview.blockers.map((b) => b?.message).filter(Boolean) : [];
  return reasons.join('；') || '当前项目暂不能回退';
}

// 版本右侧 ⋯ 的菜单（标记、回退在行内按钮上）
// 时间线一行的 ⋯：查看文件、标记、回退（当前版本不能回退到自己，回退前会预览确认）
// 没有写权限（can_tag=false，develop 只读评审）时不给标记入口
export function versionMenu(v) {
  return [
    { key: 'files', label: '查看这一版的全部文件', icon: 'eye' },
    ...(v.can_tag === false ? [] : [{ key: 'tag', label: '标记', icon: 'tag' }]),
    '-',
    {
      key: 'revert', label: '回退到这一版', icon: 'undo', disabled: !!v.current || v.can_revert === false,
      title: v.current ? '已是当前版本' : undefined,
    },
  ];
}

// 对比右侧是否只读：写入范围锁住了这个文件（条目按路径对上也算）
export function pathLocked(lock, path) {
  if (!lock || !path) return false;
  if (isLocked(lock, { path })) return true;
  return (lock.entries || []).some((e) => entryPath(e) === path && isLocked(lock, e));
}
