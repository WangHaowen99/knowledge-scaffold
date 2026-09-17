/* 每个场景一段完整会话。之前所有场景共用一份 base messages，
   导致增量、融合、门禁场景里都挂着建模场景的 D2 问题卡，会话读起来是串的。 */

const plan = { type: 'plan', items: [
  { t: '分析 6 份资料，记录证据引用', st: 'done' },
  { t: '确认建模目标与范围', st: 'done' },
  { t: '确认「入库校验」「库存盘点」流程', st: 'done' },
  { t: '确认「采购申请」流程的判断口径', st: 'now' },
  { t: '从 3 条流程抽取业务操作与对象' },
  { t: '收尾待确认项 → 进入验证' },
]};

export const CONVERSATIONS = {
  modeling: [
    { id: 'a1', role: 'assistant', at: '2026-08-24 14:02', blocks: [plan] },
    { id: 'a2', role: 'assistant', at: '2026-08-24 14:03',
      focus: { kind: 'flow', target: 'diagrams/采购申请.mmd', node: 'D2' }, focusLabel: '看 D2 判断分支',
      blocks: [{ type: 'text',
      text: '采购申请流程草拟完成，其余节点都能在资料里找到依据。只有 D2 的判断阈值有两处互相矛盾的说法，需要你裁决。' }] },
    { id: 'a3', role: 'assistant', at: '2026-08-24 14:03',
      focus: { kind: 'flow', target: 'diagrams/采购申请.mmd', node: 'D2' }, focusLabel: '看 D2 判断分支',
      blocks: [{ type: 'ask', idx: '1 / 2',
      q: 'D2「可用量低于安全库存」中的可用量怎么算？',
      options: [
        { t: '现有库存 − 已占用', s: '依据：作业手册 §3.2「可用即未被工单占用的部分」' },
        { t: '现有库存 − 已占用 − 在途退货', s: '依据：库存字段说明表，含 pending_return 列' },
        { t: '两者都对，按仓库类型区分', s: '会再问你一个后续问题' },
      ],
      chips: ['为什么这两处会冲突？', '给我看原文', '先跳过，稍后再说'] }] },
    { id: 'a4', role: 'assistant', at: '2026-08-24 14:06', blocks: [{ type: 'material' }] },
  ],

  confirm: [
    { id: 'c1', role: 'assistant', at: '2026-08-24 15:18', blocks: [{ type: 'text',
      text: '按你的口径改完了。3 条流程的业务操作和对象也一并抽好了。' }] },
    { id: 'c2', role: 'assistant', at: '2026-08-24 15:20', blocks: [{ type: 'text',
      text: '17 个变更组里，只有 2 个涉及业务口径需要你判断，其余是结构和索引。' }] },
    { id: 'c3', role: 'assistant', at: '2026-08-24 15:20',
      focus: { kind: 'diff', target: 'rules/安全库存判断.md' }, focusLabel: '看这批 diff',
      blocks: [{ type: 'confirm' }] },
    { id: 'c4', role: 'assistant', at: '2026-08-24 15:20', blocks: [{ type: 'policy' }] },
  ],

  materials: [
    { id: 'd1', role: 'user', at: '2026-08-24 16:01', blocks: [{ type: 'text',
      text: '可用量为什么要扣在途退货？依据是哪来的' }] },
    { id: 'd2', role: 'assistant', at: '2026-08-24 16:02',
      focus: { kind: 'doc', target: '作业手册.pdf' }, focusLabel: '定位到 §3.2',
      blocks: [{ type: 'text',
      text: '依据不在资料里，是你三个月前的裁决。《作业手册》§3.2 原文只说「扣除已占用」，没提在途退货——我已经把中栏定位到那一段，右边能看到引用它的 3 条知识。' }] },
    { id: 'd3', role: 'assistant', at: '2026-08-24 16:02', blocks: [{ type: 'text',
      text: '要改口径的话，走一次增量更稳妥：我会先算出受影响范围再动手。' }] },
  ],

  increment: [
    { id: 'i1', role: 'user', at: '2026-08-25 09:30', blocks: [{ type: 'text',
      text: '上传了两份新文件，管理办法改版了' }] },
    { id: 'i2', role: 'assistant', at: '2026-08-25 09:31',
      focus: { kind: 'impact', target: '影响分析' }, focusLabel: '看影响范围',
      blocks: [{ type: 'text',
      text: '两份新资料读完了。11 项保持有效，我不会去动它们。4 项受影响，其中 2 项要你裁决。' }] },
    { id: 'i3', role: 'assistant', at: '2026-08-25 09:31', blocks: [{ type: 'ask', idx: '1 / 2',
      q: '可用量是否扣减在途退货？现行是扣减（你 2026-08-22 的裁决），新资料《管理办法 v3》§2.1 说不扣减。',
      options: [
        { t: '采用新资料，v3 生效', s: '失效 1 条规则 + 3 处引用，生成变更方案' },
        { t: '保留现行口径', s: '记为「已知与 v3 不一致」，写入 decisions 留痕' },
        { t: '按生效日期分段', s: '会追问生效日期字段来源' },
      ],
      chips: ['看那次裁决', '给我看 v3 原文'] }] },
    { id: 'i4', role: 'assistant', at: '2026-08-25 09:31', blocks: [{ type: 'preview',
      title: '增量方案预览 · 待裁决后生成',
      text: '新增 1 规则 · 修改 3 文件 · 失效 0 已发布用例 · 需重跑 4 例',
      actions: [{ t: '生成待确认的修改', disabled: true }, { t: '只看不改' }] }] },
  ],

  merge: [
    { id: 'g1', role: 'user', at: '2026-08-26 10:10', blocks: [{ type: 'text',
      text: '把库存盘点包 0.9.0 并进来' }] },
    { id: 'g2', role: 'assistant', at: '2026-08-26 10:12',
      focus: { kind: 'conflict', target: '冲突清单' }, focusLabel: '看冲突清单',
      blocks: [{ type: 'text',
      text: '来源包解析完成，28 条知识条目里 23 条能直接合并，5 条冲突。互斥规则和别名撞车必须你来定，我不会替你选。' }] },
    { id: 'g3', role: 'assistant', at: '2026-08-26 10:12', blocks: [{ type: 'ask', idx: '1 / 2',
      q: '「可用量口径」两个包结论相反。这不是合并问题，是业务口径问题。',
      options: [
        { t: '以当前包为准', s: '来源包相关业务操作会按当前口径改写（2 处）' },
        { t: '以来源包为准', s: '会失效当前 4 处引用，等价于一次增量变更' },
        { t: '按仓库类型分段共存', s: '需要你说明哪类仓库用哪个口径' },
      ],
      chips: ['两边的证据分别是什么？'] }] },
    { id: 'g4', role: 'assistant', at: '2026-08-26 10:13', blocks: [{ type: 'preview',
      title: '融合执行',
      text: '裁决 2/5 完成 · 全部裁决后生成待确认的修改并重算引用解析',
      actions: [{ t: '生成融合后的待确认修改', disabled: true }, { t: '导出冲突清单' }, { t: '放弃融合', danger: true }] }] },
  ],

  gates: [
    { id: 't1', role: 'assistant', at: '2026-08-27 11:04', blocks: [{ type: 'text',
      text: '13 条用例跑完了，11 通过、1 部分通过、1 失败。' }] },
    { id: 't2', role: 'assistant', at: '2026-08-27 11:05',
      focus: { kind: 'gates', target: '正式发布门禁' }, focusLabel: '看门禁',
      blocks: [{ type: 'text',
      text: '两个用例没过，都指向同一个根因：仓库分级引入后，安全库存阈值应该按仓库级别取，但 rules/安全库存判断.md 还是单一阈值。修这一处应该能同时解决 #7 和 #12。' }] },
    { id: 't3', role: 'assistant', at: '2026-08-27 11:05', blocks: [{ type: 'preview',
      title: '修复方案 · 待你同意',
      text: '改 rules/安全库存判断.md 1 处 · 改 operations/生成采购建议.md 1 处。预期修复 #7 #12；已通过用例风险：#3 #9（同样引用该规则，将重跑）。',
      actions: [{ t: '生成待确认的修改', primary: true }, { t: '先看诊断详情' }, { t: '我自己改' }] }] },
    { id: 't4', role: 'assistant', at: '2026-08-27 11:06', blocks: [{ type: 'text',
      text: '另外「库存盘点」只有 1 条用例。v1 没有静态校验器，用例覆盖就是唯一的质量抓手——这一条我不能替你确认，需要项目负责人过一遍，或者先补几条再发布。' }] },
  ],
};
