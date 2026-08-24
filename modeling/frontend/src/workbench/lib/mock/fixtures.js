/* 演示数据。全部为合成数据，不含任何真实业务信息。
   后端接通后整个 lib/mock 目录删除，组件不需要改——组件只认 store 的形状。 */

export const project = {
  id: 'p-demo', name: '备件库存与采购问答',
  revision: '0.4.0-draft', version: null,
  counts: { flows: 3, objects: 12, scripts: 0, pending: 2 },
};

export const tree = [
  { g: null, items: [
    { path: 'manifest.yaml', st: 'ok' },
    { path: 'index.md', st: 'ok' },
  ]},
  { g: 'diagrams/', items: [
    { path: 'diagrams/入库校验.mmd', st: 'ok' },
    { path: 'diagrams/采购申请.mmd', st: 'wait', cap: '讨论中' },
    { path: 'diagrams/库存盘点.mmd', st: 'ok' },
  ]},
  { g: 'objects/', items: [
    { path: 'objects/备件.md', st: 'ok' },
    { path: 'objects/库存批次.md', st: 'ok' },
    { path: 'objects/采购申请单.md', st: 'ok' },
    { path: 'objects/供应商.md', st: 'draft', cap: '草稿' },
  ]},
  { g: 'operations/', items: [
    { path: 'operations/查询可用库存.md', st: 'ok' },
    { path: 'operations/生成采购建议.md', st: 'draft', cap: '草稿' },
  ]},
  { g: 'rules/ · metrics/', items: [
    { path: 'rules/安全库存判断.md', st: 'ok' },
    { path: 'rules/入库质检规则.md', st: 'ok' },
    { path: 'metrics/周转率.md', st: 'draft', cap: '草稿' },
  ]},
  { g: '.modeling/', items: [
    { path: '.modeling/goal.md', st: 'ok' },
    { path: '.modeling/plan.md', st: 'new' },
    { path: '.modeling/decisions/', st: 'ok', cap: '9' },
  ]},
];

export const materials = [
  { id: 'm1', name: '作业手册.pdf', v: 2, refs: 14, st: 'ok', meta: '42 页' },
  { id: 'm2', name: '库存字段说明表.xlsx', v: 1, refs: 23, st: 'ok', meta: '3 张表 / 27 字段' },
  { id: 'm3', name: '安全库存管理办法.docx', v: 3, refs: 19, st: 'ok' },
  { id: 'm4', name: '采购流程说明.md', v: 1, refs: 11, st: 'ok' },
  { id: 'm5', name: '供应商台账.xlsx', v: 1, refs: 11, st: 'ok', meta: '1 张表 / 12 字段' },
  { id: 'm6', name: '质检记录扫描件.pdf', v: 1, refs: 0, st: 'stale', meta: '无法解析' },
];
export const spokenEvidence = 4;

export const flow = {
  title: '采购申请', focusNode: 'D2',
  nodes: [
    { id: 'S', kind: 'terminal', text: '领用申请提交' },
    { id: 'A', kind: 'step', text: '校验备件编码' },
    { id: 'B', kind: 'step', text: '查询可用库存' },
    { id: 'D2', kind: 'decision', text: '可用量 < 安全库存？', tag: 'D2' },
    { id: 'E', kind: 'step', text: '生成采购建议', added: true, edgeLabel: '是' },
    { id: 'F', kind: 'terminal', text: '通知采购负责人' },
  ],
};

export const diff = [
  { path: 'diagrams/采购申请.mmd', stat: '+4 −1', lines: [
    ['hunk', '@@ 判断分支 D2 @@'],
    ['ctx',  '  C[查询可用库存] --> D2{可用量 < 安全库存?}'],
    ['del',  '- D2 -->|是| E[提示补货]'],
    ['add',  '+ D2 -->|是| E[生成采购建议]'],
    ['add',  '+ E --> F[通知采购负责人]'],
    ['add',  '+ %% 可用量口径见 rules/安全库存判断.md'],
  ]},
  { path: 'rules/安全库存判断.md', stat: '新增', lines: [
    ['add', '+ ## 可用量口径'],
    ['add', '+ 可用量 = 现有库存 − 已占用 − 在途退货'],
    ['add', '+ 适用对象：库存批次'],
    ['add', '+ 证据：user-decision:d-014（2026-08-22 你的裁决）'],
    ['add', '+       material:库存字段说明表#pending_return'],
  ]},
];

export const changeGroups = [
  { id: 'g1', tier: 'caliber', title: '新增规则「安全库存判断」',
    meta: '可用量 = 现有库存 − 已占用 − 在途退货',
    evidence: 'user-decision:d-014', impact: 2 },
  { id: 'g2', tier: 'caliber', title: '修改指标「周转率」口径',
    meta: '粒度从 备件+仓库 改为 备件+仓库+批次',
    warning: '这会改变已有查询结果' },
  { id: 'g3',  tier: 'structural', title: '备件对象新增 3 个字段' },
  { id: 'g4',  tier: 'structural', title: '库存批次新增 5 个字段、删除 1 个' },
  { id: 'g5',  tier: 'structural', title: '采购申请单新增 2 个字段' },
  { id: 'g6',  tier: 'structural', title: '供应商对象补全描述与别名' },
  { id: 'g7',  tier: 'structural', title: '业务操作「查询可用库存」步骤补全' },
  { id: 'g8',  tier: 'structural', title: '业务操作「生成采购建议」步骤补全' },
  { id: 'g9',  tier: 'structural', title: '业务操作输入输出契约补全（3 处）' },
  { id: 'g10', tier: 'structural', title: '对象关系声明补全（4 处）' },
  { id: 'g11', tier: 'structural', title: '库存批次主键声明' },
  { id: 'g12', tier: 'structural', title: '备件 ↔ 库存批次 关系补全' },
  { id: 'g13', tier: 'structural', title: 'mapping 字段绑定补全（2 处）' },
  { id: 'g14', tier: 'structural', title: '业务操作证据引用补全（5 处）' },
  { id: 'g15', tier: 'index', title: 'objects/index.md 同步' },
  { id: 'g16', tier: 'index', title: 'operations/index.md 同步' },
  { id: 'g17', tier: 'index', title: 'rules/index.md 同步' },
];

export const TIERS = {
  caliber:    { label: '涉及业务口径', hint: '规则内容、指标口径、判断阈值', locked: true },
  structural: { label: '结构变更',     hint: '新增对象字段、业务操作步骤' },
  index:      { label: '索引同步',     hint: 'index.md、目录表格等无业务语义的更新' },
};

export const policy = [
  { tier: 'index',      mode: 'auto' },
  { tier: 'repair',     mode: 'auto', label: '结构性修复', hint: '补引用路径、修 frontmatter、同步命名' },
  { tier: 'structural', mode: 'ask' },
  { tier: 'caliber',    mode: 'ask', locked: true },
  { tier: 'script',     mode: 'ask', locked: true, label: '脚本生成与绑定', hint: '受控代码，进沙箱执行' },
];

export const impact = {
  intact: { n: 11, items: ['入库校验、库存盘点 2 条流程', '备件、采购申请单、供应商 3 个对象', '入库质检规则、周转率等 6 项'] },
  todo: { n: 4, decide: 2, items: [
    { t: '可用量口径', s: '与 v3 冲突 · 需裁决' },
    { t: '仓库分级', s: '引入新分支 · 需裁决' },
    { t: '库存批次字段说明', s: '可自动' },
    { t: '采购申请 D2 注释', s: '可自动' },
  ]},
  chains: [
    { src: '《安全库存管理办法 v3》§2.1', srcSub: '可用量不再扣减在途退货',
      dst: 'rules/安全库存判断.md', dstSub: '口径被直接覆盖 · 需裁决' },
    { src: '同上（间接）', srcSub: '经 rules/安全库存判断.md 传播',
      dst: 'objects/库存批次.md', dstSub: '字段 pending_return 说明失效 · 可自动' },
    { src: '《仓库分级表》全表', srcSub: '引入 A/B/C 三级仓，安全库存按级别不同',
      dst: 'operations/生成采购建议.md', dstSub: '新增分支未覆盖 · 需裁决' },
  ],
  hitPaths: ['diagrams/采购申请.mmd', 'objects/库存批次.md', 'rules/安全库存判断.md', 'operations/生成采购建议.md'],
};

export const conflicts = [
  { k: 'object:库存批次', kind: '定义', mine: '7 字段，绑定 Sheet:库存明细',
    theirs: '11 字段，绑定 Sheet:批次台账（多 4 个盘点字段）',
    options: ['合并字段（推荐）', '保留当前', '采用来源', '重命名共存'] },
  { k: 'rule:可用量口径', kind: '互斥', mine: '现有 − 已占用 − 在途退货', theirs: '现有 − 已占用',
    options: ['需人工裁决', '保留当前', '采用来源'] },
  { k: 'object:备件', kind: '别名', mine: '别名含「物料」', theirs: '别名含「物料」，指向不同对象',
    options: ['需人工裁决', '去掉来源别名', '加前缀区分'] },
  { k: 'op:查询可用库存', kind: '重叠', mine: '3 步，含占用扣减', theirs: '2 步，不含占用扣减',
    options: ['保留当前（推荐）', '采用来源', '重命名共存'] },
  { k: 'metric:周转率', kind: '口径', mine: '粒度 备件+仓库', theirs: '粒度 备件+仓库+批次',
    options: ['重命名共存（推荐）', '需人工裁决'] },
];

export const gates = [
  { id: 'g-script', st: 'pass', t: '脚本沙箱测试全通过', d: '3 个脚本、11 个用例，11:04 在修订 r-88c1 上运行' },
  { id: 'g-cand',   st: 'pass', t: '无待确认的修改', d: '当前内容都已确认' },
  { id: 'g-eval',   st: 'fail', t: '全部评测用例通过', d: '13 例已执行完成，11 通过、1 部分通过、1 失败',
    blockers: ['用例 #7「跨仓库可用量汇总」— partial，Judge：漏算在途', '用例 #12「安全库存预警阈值」— fail，Judge：阈值取错仓库级别'],
    actions: ['看失败诊断', '让助手修', '人工改判'] },
  { id: 'g-cover',  st: 'fail', t: '用例覆盖已确认', d: '项目负责人尚未确认当前 13 条用例覆盖了核心业务问题',
    blockers: ['「库存盘点」流程目前只有 1 条用例', '3 处计算逻辑以规则文字描述实现，没有对应用例'],
    actions: ['逐条复核覆盖', '补用例'] },
  { id: 'g-ver',    st: 'na',   t: '版本号确认', d: '助手建议 1.3.0（新增能力，向后兼容）· 待项目负责人确认' },
];

export const docSample = {
  title: '作业手册.pdf', meta: 'v2 · 42 页',
  blocks: [
    { h: '§3.2 可用量与占用' },
    { p: [['仓库现有库存并不等于可以领用的数量。'],
          ['可用量指扣除已被工单占用之后、当前可以立即发放的部分', 'hl'],
          ['。已占用包含已审批未领取和在途调拨两种情况。']] },
    { p: [['盘点期间库存冻结，冻结期内'],
          ['所有可用量按盘点开始时刻的快照计算', 'hl'],
          ['，不随实时出入库变动。']] },
    { h: '§3.3 安全库存' },
    { p: [['安全库存由各仓库根据历史月均消耗量与供货周期自行设定', 'hl'],
          ['，报备后生效，原则上每季度复核一次。']] },
  ],
  referrers: [
    { path: 'rules/安全库存判断.md', why: '可用量 = 现有库存 − 已占用 − 在途退货' },
    { path: 'objects/库存批次.md', why: '字段 available_qty 的业务含义' },
    { path: 'operations/查询可用库存.md', why: '步骤 2：扣减已占用' },
  ],
};

/* 每帧带一个 focus：这就是「助手说看这个，图已经在那儿」的数据来源。 */
export const activityFrames = [
  { text: '正在读《安全库存管理办法 v3》', progress: [3, 6],
    focus: { kind: 'flow', target: 'diagrams/采购申请.mmd', node: 'D2' },
    sub: ['14:06:41 · 搜索「安全库存 供货周期」，命中 9 段', '14:06:28 · 读《作业手册》第 18–22 页', '14:06:04 · 3 个资料分析并行中（2 完成）'] },
  { text: '正在核对「查询可用库存」这一步的取数口径', progress: [4, 6],
    focus: { kind: 'flow', target: 'diagrams/采购申请.mmd', node: 'B' },
    sub: ['14:07:02 · describe_table(库存明细)，11 列', '14:06:55 · who_references(objects/库存批次.md)，3 处'] },
  { text: '正在补全「生成采购建议」节点', progress: [5, 6],
    focus: { kind: 'flow', target: 'diagrams/采购申请.mmd', node: 'E' },
    sub: ['14:07:20 · read_document(采购流程说明.md)', '14:07:11 · grep_package("安全库存")'] },
  { text: '正在整理需要你确认的问题', progress: [6, 6],
    focus: { kind: 'flow', target: 'diagrams/采购申请.mmd', node: 'D2' },
    sub: ['14:07:31 · 更新项目上下文', '14:07:18 · 读《采购流程说明》'] },
];

export const createFiles = [
  { name: '作业手册.pdf', st: 'ok', meta: '解析完成 · 42 页' },
  { name: '库存字段说明表.xlsx', st: 'ok', meta: '解析完成 · 3 张表 / 27 字段' },
  { name: '安全库存管理办法.docx', st: 'ok', meta: '解析完成' },
  { name: '采购流程说明.md', st: 'ok', meta: '解析完成' },
  { name: '供应商台账.xlsx', st: 'ok', meta: '解析完成 · 1 张表 / 12 字段' },
  { name: '质检记录扫描件.pdf', st: 'parsing', pct: 64, meta: '解析中' },
];
