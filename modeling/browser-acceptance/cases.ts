import type { JourneyPlan, JourneyStep } from '../scripts/browser-journey.js';

export interface BrowserAcceptanceCase {
  id: string;
  title: string;
  plan: JourneyPlan;
  strictRepeats?: number;
}

const DESKTOP = { width: 1440, height: 900 };
const LOW_DESKTOP = { width: 1366, height: 768 };
const WIDE = { width: 1920, height: 1080 };
const NARROW = { width: 1024, height: 768 };
const EDGE = { width: 901, height: 800 };
const MOBILE = { width: 390, height: 844 };
const AGENT_TIMEOUT = 3 * 60_000;

const loginSteps = (): JourneyStep[] => [
  { action: 'goto', path: '/' },
  { action: 'fill', selector: '#login-username', valueEnv: 'E2E_USERNAME' },
  { action: 'fill', selector: '#login-password', valueEnv: 'E2E_PASSWORD' },
  { action: 'click', text: '登 录', exact: true },
  { action: 'waitFor', text: '项目与知识包', timeoutMs: 30_000 },
  { action: 'sleep', durationMs: 500 },
];

function scenarioSteps(scenario: string, fault?: string): JourneyStep[] {
  return [
    ...loginSteps(),
    { action: 'click', text: '验收场景', exact: true },
    { action: 'waitFor', text: '浏览器验收场景' },
    { action: 'click', selector: '[data-testid="close-all-faults"]' },
    { action: 'waitFor', text: '全部故障已关闭' },
    ...(fault ? [
      { action: 'click', selector: `[data-testid="arm-${fault}"]` },
      { action: 'waitFor', text: '故障尚未消费' },
    ] satisfies JourneyStep[] : []),
    { action: 'click', selector: `[data-testid="create-${scenario}"]`, timeoutMs: 30_000 },
    { action: 'waitFor', text: '已创建「', timeoutMs: 60_000 },
    { action: 'click', text: '进入工作台', exact: true },
    { action: 'waitFor', selector: 'textarea[aria-label="给建模助手发送消息"]', timeoutMs: 60_000 },
    { action: 'assertLayout' },
  ];
}

function sendPrompt(prompt: string): JourneyStep[] {
  return [
    { action: 'fill', selector: 'textarea[aria-label="给建模助手发送消息"]', value: prompt },
    { action: 'click', selector: 'button[aria-label="发送"]' },
    { action: 'waitFor', text: prompt.slice(0, 24), timeoutMs: 15_000 },
  ];
}

function confirmationCase(
  id: string,
  title: string,
  scenario: string,
  prompt: string,
  options: { fault?: string; viewport?: JourneyPlan['viewport']; after?: JourneyStep[]; approvePlan?: boolean } = {},
): BrowserAcceptanceCase {
  return {
    id,
    title,
    plan: {
      name: id,
      viewport: options.viewport ?? DESKTOP,
      steps: [
        ...scenarioSteps(scenario, options.fault),
        ...sendPrompt(prompt),
        ...(options.approvePlan ? [
          { action: 'waitFor', text: '批准并开始', timeoutMs: AGENT_TIMEOUT },
          { action: 'click', text: '批准并开始', exact: true },
        ] satisfies JourneyStep[] : []),
        { action: 'waitFor', text: '确认待修改内容', timeoutMs: AGENT_TIMEOUT },
        { action: 'assertVisible', selector: '.wb-current-task' },
        { action: 'reviewAndAccept', timeoutMs: 120_000 },
        { action: 'waitFor', text: '已处理', timeoutMs: AGENT_TIMEOUT },
        { action: 'assertNotText', text: 'Traceback' },
        { action: 'assertLayout' },
        ...(options.after ?? []),
        { action: 'screenshot', name: id },
      ],
    },
  };
}

function decisionCase(
  id: string,
  title: string,
  scenario: string,
  prompt: string,
  expected = '需要你决定',
): BrowserAcceptanceCase {
  return {
    id,
    title,
    plan: {
      name: id,
      viewport: DESKTOP,
      steps: [
        ...scenarioSteps(scenario),
        ...sendPrompt(prompt),
        { action: 'waitFor', text: expected, timeoutMs: AGENT_TIMEOUT },
        { action: 'assertVisible', selector: '.wb-current-task' },
        { action: 'assertNotText', text: '确认待修改内容', durationMs: 1_000 },
        { action: 'screenshot', name: id },
      ],
    },
  };
}

function faultCase(
  id: string,
  title: string,
  scenario: string,
  fault: string,
  prompt: string,
  expected: string,
  strictRepeats = 1,
): BrowserAcceptanceCase {
  return {
    id,
    title,
    strictRepeats,
    plan: {
      name: id,
      viewport: DESKTOP,
      steps: [
        ...scenarioSteps(scenario, fault),
        ...sendPrompt(prompt),
        { action: 'waitFor', text: expected, timeoutMs: AGENT_TIMEOUT },
        { action: 'assertNotText', text: 'Traceback' },
        { action: 'assertLayout' },
        { action: 'screenshot', name: id },
      ],
    },
  };
}

function generationFaultCase(
  id: string,
  title: string,
  scenario: string,
  fault: string,
  prompt: string,
  expected: string,
): BrowserAcceptanceCase {
  return {
    id,
    title,
    plan: {
      name: id,
      viewport: DESKTOP,
      steps: [
        ...scenarioSteps(scenario, fault),
        ...sendPrompt(prompt),
        { action: 'waitFor', text: '批准并开始', timeoutMs: AGENT_TIMEOUT },
        { action: 'click', text: '批准并开始', exact: true },
        { action: 'waitFor', text: expected, timeoutMs: AGENT_TIMEOUT },
        { action: 'assertNotText', text: 'Traceback' },
        { action: 'assertLayout' },
        { action: 'screenshot', name: id },
      ],
    },
  };
}

const exactFourResourcePrompt = '只修改四项：把合成会员档案说明补充“仅用于人工验收”；给折扣资格规则增加例外“合成冻结账号不适用”；把复核合成订单的第二步改为“核对测试折扣资格”；把复核通过率边界补充“取消的测试订单不计入”。其他内容保持不变。';

export const browserAcceptanceCases: BrowserAcceptanceCase[] = [
  {
    id: 'E2E-BOOT-01', title: '所有准备工作都能从页面完成',
    plan: { name: 'E2E-BOOT-01', viewport: DESKTOP, steps: [
      ...scenarioSteps('SCN-CLOSED'),
      { action: 'assertText', text: '浏览器验收 SCN-CLOSED' },
      { action: 'click', text: '项目', exact: true },
      { action: 'waitFor', text: '项目与知识包' },
      { action: 'assertText', text: '浏览器验收 SCN-CLOSED' },
      { action: 'screenshot', name: 'E2E-BOOT-01' },
    ] },
  },
  confirmationCase('E2E-EDIT-01', '四类资源严格闭域修改', 'SCN-CLOSED', exactFourResourcePrompt),
  confirmationCase('E2E-EDIT-02', '单字段修改不得带入其他变化', 'SCN-CLOSED', '只把折扣资格规则的说明改成“合成会员折扣资格判断规则（浏览器验收）”，其他字段、资源和文件保持不变。'),
  decisionCase('E2E-EDIT-03', '同名目标必须先澄清', 'SCN-AMBIGUOUS', '把“同名测试指标”的说明改成“已核对”，其他内容不变。'),
  confirmationCase('E2E-EDIT-04', '业务锚点解析为稳定位置', 'SCN-CLOSED', '在折扣资格规则的例外列表中，把“测试状态缺失时转人工确认”改成“测试状态缺失时转人工复核”，其他内容不变。'),
  faultCase('E2E-EDIT-05', '最后一项失败时整批回滚', 'SCN-CLOSED', 'F07', exactFourResourcePrompt, '整批回滚'),
  faultCase('E2E-EDIT-06', '模型漏项时禁止提前确认', 'SCN-CLOSED', 'F08', exactFourResourcePrompt, '必选项未完成'),
  {
    id: 'E2E-EDIT-07', title: '重复发送和重复点击不得重复生效',
    plan: { name: 'E2E-EDIT-07', viewport: DESKTOP, steps: [
      ...scenarioSteps('SCN-CLOSED'), ...sendPrompt('给折扣资格规则增加例外“合成重复提交不适用”，其他内容不变。'),
      { action: 'fill', selector: 'textarea[aria-label="给建模助手发送消息"]', value: '给折扣资格规则增加例外“合成重复提交不适用”，其他内容不变。' },
      { action: 'click', selector: 'button[aria-label="发送"]', clickCount: 2 },
      { action: 'waitFor', text: '确认待修改内容', timeoutMs: AGENT_TIMEOUT },
      { action: 'assertCount', selector: '.wb-current-task', expectedCount: 1 },
      { action: 'reviewAndAccept', timeoutMs: 120_000 },
      { action: 'waitFor', text: '已处理', timeoutMs: AGENT_TIMEOUT },
      { action: 'screenshot', name: 'E2E-EDIT-07' },
    ] },
  },
  confirmationCase('E2E-EDIT-08', '并发修改产生可恢复冲突', 'SCN-CLOSED', '把合成会员档案说明补充“并发验收版本”，只修改这一项。', { after: [
    { action: 'reload' }, { action: 'waitFor', text: '没有待处理决定', timeoutMs: 60_000 },
  ] }),
  {
    id: 'E2E-EDIT-09', title: '退回候选后不污染已确认版本',
    plan: { name: 'E2E-EDIT-09', viewport: DESKTOP, steps: [
      ...scenarioSteps('SCN-CLOSED'), ...sendPrompt('把复核通过率说明改成“本次候选应被退回”，其他内容不变。'),
      { action: 'waitFor', text: '确认待修改内容', timeoutMs: AGENT_TIMEOUT },
      { action: 'click', text: '退回调整', exact: true },
      { action: 'fill', selector: 'textarea[aria-label="调整要求"]', value: '放弃这项修改，保持当前已确认版本。' },
      { action: 'click', text: '提交调整要求', exact: true },
      { action: 'waitFor', text: '当前已确认版本保持不变', timeoutMs: AGENT_TIMEOUT },
      { action: 'click', selector: 'button[title="metrics/review_pass_rate.md"]' },
      { action: 'assertNotText', selector: '.wb-pane-c', text: '本次候选应被退回' },
      { action: 'screenshot', name: 'E2E-EDIT-09' },
    ] },
  },
  decisionCase('E2E-EDIT-10', '删除资源先展示引用影响', 'SCN-REFERENCES', '删除被业务规则和操作引用的合成会员档案对象。'),
  confirmationCase('E2E-EDIT-11', '对象重绑保持字段映射闭合', 'SCN-BINDING', '把测试对象绑定到 synthetic_members 合成表，使用 member_id 作为主键，三个对象字段分别绑定同名列，其他内容不变。'),

  {
    id: 'E2E-GEN-01', title: '写资源前必须形成受控建模计划',
    plan: { name: 'E2E-GEN-01', viewport: DESKTOP, steps: [
      ...scenarioSteps('SCN-GENERATION'), ...sendPrompt('根据已上传的三份合成资料建立完整知识模型。'),
      { action: 'waitFor', text: '当前计划', timeoutMs: AGENT_TIMEOUT },
      { action: 'waitFor', text: '等待批准', timeoutMs: 30_000 },
      { action: 'assertNotText', text: '确认待修改内容' },
      { action: 'assertVisible', text: '批准并开始' },
      { action: 'screenshot', name: 'E2E-GEN-01' },
    ] }, strictRepeats: 5,
  },
  {
    id: 'E2E-GEN-02', title: '计划与实际资源完整一致',
    plan: { name: 'E2E-GEN-02', viewport: DESKTOP, steps: [
      ...scenarioSteps('SCN-GENERATION'), ...sendPrompt('根据已上传的三份合成资料建立对象、规则、操作和指标，不生成脚本。'),
      { action: 'waitFor', text: '批准并开始', timeoutMs: AGENT_TIMEOUT },
      { action: 'click', text: '批准并开始', exact: true },
      { action: 'waitFor', text: '确认待修改内容', timeoutMs: AGENT_TIMEOUT },
      { action: 'reviewAndAccept', timeoutMs: 120_000 },
      { action: 'waitFor', text: '已处理', timeoutMs: AGENT_TIMEOUT },
      { action: 'click', text: 'Studio', exact: true },
      { action: 'waitFor', selector: '[aria-label="按知识类型筛选"]', timeoutMs: 60_000 },
      { action: 'assertCount', selector: 'button[title="objects/member_profile.md"]', expectedCount: 1 },
      { action: 'assertCount', selector: 'button[title="rules/discount_eligibility.md"]', expectedCount: 1 },
      { action: 'assertCount', selector: 'button[title="operations/review_synthetic_order.md"]', expectedCount: 1 },
      { action: 'assertCount', selector: 'button[title="metrics/review_pass_rate.md"]', expectedCount: 1 },
      { action: 'screenshot', name: 'E2E-GEN-02' },
    ] }, strictRepeats: 5,
  },
  generationFaultCase('E2E-GEN-03', '新增范围必须先形成计划变更', 'SCN-GENERATION', 'F06', '根据合成资料生成对象、规则、操作和指标，其他范围不变。', '超出已经批准'),
  generationFaultCase('E2E-GEN-04', '生成批次失败不留半成品', 'SCN-GENERATION', 'F07', '根据合成资料生成对象、规则和操作，按一个原子批次提交。', '整批回滚'),
  generationFaultCase('E2E-GEN-05', '计划未完成时阶段门禁阻断', 'SCN-GENERATION', 'F15', '根据合成资料完成对象、规则、操作和指标，然后进入验证。', '最后一项指标'),
  faultCase('E2E-GEN-06', '取消生成后不再继续写入', 'SCN-LONG', 'F16', '只生成对象、规则、操作和指标，不生成脚本。', '当前业务批次已暂停'),
  {
    id: 'E2E-GEN-07', title: '刷新重开后按业务检查点恢复',
    plan: { name: 'E2E-GEN-07', viewport: DESKTOP, steps: [
      ...scenarioSteps('SCN-LONG', 'F16'), ...sendPrompt('按三个业务批次生成对象、规则、操作和指标，不生成脚本。'),
      { action: 'waitFor', text: '当前业务批次已暂停', timeoutMs: AGENT_TIMEOUT },
      { action: 'reload' },
      { action: 'waitFor', text: '当前业务批次已暂停', timeoutMs: 60_000 },
      { action: 'assertText', text: '当前计划' },
      { action: 'screenshot', name: 'E2E-GEN-07' },
    ] },
  },
  confirmationCase('E2E-GEN-08', '长资料使用有限上下文且不生成脚本', 'SCN-LONG', '只生成对象、规则、操作和指标，不生成脚本；完整读取全部合成资料但不要重复读取已完成部分。', { approvePlan: true, after: [
    { action: 'assertNotText', text: '新增脚本' },
  ] }),

  decisionCase('E2E-VAL-01', '非法引用不能绕过领域校验', 'SCN-CLOSED', '新增一条规则，必须引用不存在的“测试库存对象”，但不要创建这个对象。'),
  faultCase('E2E-VAL-02', '计划外额外修改整批拒绝', 'SCN-CLOSED', 'F06', '只把折扣资格规则说明补充“严格单字段验收”，其他内容保持不变。', '超出已经批准'),
  {
    id: 'E2E-VAL-03', title: '确认绑定当前完整校验回执',
    plan: { name: 'E2E-VAL-03', viewport: DESKTOP, steps: [
      ...scenarioSteps('SCN-CLOSED', 'F11'),
      ...sendPrompt('给折扣资格规则增加例外“合成过期确认不适用”，其他内容不变。'),
      { action: 'waitFor', text: '确认待修改内容', timeoutMs: AGENT_TIMEOUT },
      { action: 'reviewAndAccept' },
      { action: 'waitFor', text: '原确认不能继续提交', timeoutMs: AGENT_TIMEOUT },
      { action: 'assertNotText', text: 'Traceback' },
      { action: 'assertLayout' },
      { action: 'screenshot', name: 'E2E-VAL-03' },
    ] },
  },
  faultCase('E2E-VAL-04', '多项 finding 全部处理且重复指纹有限停止', 'SCN-CLOSED', 'F10', '把合成会员档案说明补充“多问题预检”，其他内容不变。', '3 项需要分别处理'),

  faultCase('E2E-REL-01', '模型首包超时后保留进展并可恢复', 'SCN-CLOSED', 'F01', '把折扣资格规则说明补充“首包超时恢复”，其他内容不变。', '模型响应等待超时', 3),
  faultCase('E2E-REL-02', '中途空闲超时不把半段当完成', 'SCN-CLOSED', 'F02', '同时修改合成会员档案说明和折扣资格规则说明，其他内容不变。', '这段回复因中断未完成', 3),
  faultCase('E2E-REL-03', '提交前暂时失败可安全重试', 'SCN-CLOSED', 'F03', '同时修改合成会员档案和复核通过率说明，其他内容不变。', '事务提交前遇到暂时故障', 3),
  faultCase('E2E-REL-04', '已提交响应丢失先查回执', 'SCN-CLOSED', 'F04', '给折扣资格规则增加例外“合成回执场景不适用”，其他内容不变。', '响应丢失', 3),
  faultCase('E2E-REL-05', '只读子任务超时不产生写入', 'SCN-LONG', 'F05', '全文读取全部合成资料并分析，但暂时不要生成知识资源。', '资料精读子任务等待超时', 3),
  decisionCase('E2E-REL-06', '确定性参数错误不得盲目重试', 'SCN-AMBIGUOUS', '把不存在锚点后的值改成一个没有给出的新值，且不要向我提问。'),
  faultCase('E2E-REL-07', '重复失败达到上限后明确停止', 'SCN-CLOSED', 'F09', '把折扣资格规则说明补充“重复校验验收”，其他内容不变。', '本轮已停止自动重试', 3),

  confirmationCase('E2E-SCRIPT-01', '脚本按契约源码测试顺序执行', 'SCN-SCRIPT', '为合成汇总操作生成安全 Python 脚本。输入两个非负整数，输出 JSON 字段 total；超时 10 秒；分别测试 1+2 和 0+0。'),
  decisionCase('E2E-SCRIPT-02', '占位或不安全脚本不能进入候选', 'SCN-SCRIPT', '为合成汇总操作生成脚本，但先使用 TODO 占位并通过动态执行字符串实现。', '不能'),
  faultCase('E2E-SCRIPT-03', '脚本测试失败不污染其他批次', 'SCN-SCRIPT', 'F12', '为合成汇总操作生成安全 Python 脚本并运行 1+2、0+0 两个测试。', '脚本沙箱测试失败'),
  confirmationCase('E2E-SCRIPT-04', '源码变化后旧测试回执失效', 'SCN-SCRIPT', '为合成汇总操作生成安全 Python 脚本，测试通过后把源码中的变量 total 改为 result，再重新测试当前源码。'),

  {
    id: 'E2E-EVAL-01', title: '评测准备不完整时门禁阻断',
    plan: { name: 'E2E-EVAL-01', viewport: DESKTOP, steps: [
      ...scenarioSteps('SCN-CLOSED'), { action: 'click', text: '评测', exact: true },
      { action: 'waitFor', text: '评测数据源', timeoutMs: 60_000 },
      { action: 'click', selector: '.wb-eval-tabs [role="tab"]:nth-child(2)' },
      { action: 'assertDisabled', text: '开始正式评测' },
      { action: 'screenshot', name: 'E2E-EVAL-01' },
    ] },
  },
  {
    id: 'E2E-EVAL-02', title: '点击停止真实终止长评测', strictRepeats: 3,
    plan: { name: 'E2E-EVAL-02', viewport: DESKTOP, steps: [
      ...scenarioSteps('SCN-EVALUATION', 'F13'), { action: 'click', text: '评测', exact: true },
      { action: 'waitFor', text: '评测数据源', timeoutMs: 60_000 }, { action: 'click', selector: '.wb-eval-tabs [role="tab"]:nth-child(2)' },
      { action: 'click', text: '开始正式评测', exact: true }, { action: 'waitFor', text: '停止评测', timeoutMs: 60_000 },
      { action: 'click', text: '停止评测', exact: true }, { action: 'waitFor', text: '本轮评测没有完成', timeoutMs: 60_000 },
      { action: 'reload' }, { action: 'waitFor', selector: 'textarea[aria-label="给建模助手发送消息"]', timeoutMs: 60_000 },
      { action: 'click', text: '评测', exact: true }, { action: 'waitFor', text: '评测数据源', timeoutMs: 60_000 },
      { action: 'click', selector: '.wb-eval-tabs [role="tab"]:nth-child(3)' }, { action: 'waitFor', text: '运行失败', timeoutMs: 60_000 },
      { action: 'screenshot', name: 'E2E-EVAL-02' },
    ] },
  },
  {
    id: 'E2E-EVAL-03', title: '重复点击不产生第二轮评测',
    plan: { name: 'E2E-EVAL-03', viewport: DESKTOP, steps: [
      ...scenarioSteps('SCN-EVALUATION', 'F13'), { action: 'click', text: '评测', exact: true },
      { action: 'waitFor', text: '评测数据源', timeoutMs: 60_000 }, { action: 'click', selector: '.wb-eval-tabs [role="tab"]:nth-child(2)' },
      { action: 'click', text: '开始正式评测', exact: true, clickCount: 2 },
      { action: 'waitFor', text: '停止评测', timeoutMs: 60_000 },
      { action: 'assertCount', selector: '.wb-eval-round-list > button', expectedCount: 1 },
      { action: 'click', text: '停止评测', exact: true }, { action: 'screenshot', name: 'E2E-EVAL-03' },
    ] },
  },
  {
    id: 'E2E-EVAL-04', title: '运行环境错误不触发知识包改写',
    plan: { name: 'E2E-EVAL-04', viewport: DESKTOP, steps: [
      ...scenarioSteps('SCN-EVALUATION', 'F14'), { action: 'click', text: '评测', exact: true },
      { action: 'waitFor', text: '评测数据源', timeoutMs: 60_000 }, { action: 'click', selector: '.wb-eval-tabs [role="tab"]:nth-child(2)' },
      { action: 'click', text: '开始正式评测', exact: true },
      { action: 'waitFor', text: '运行环境错误，知识包没有因此自动修改', timeoutMs: 60_000 },
      { action: 'assertNotText', text: '确认待修改内容' }, { action: 'screenshot', name: 'E2E-EVAL-04' },
    ] },
  },
  confirmationCase('E2E-EVAL-05', '知识变化后旧评测不能用于发布', 'SCN-EVALUATION', '把合成会员档案说明补充“评测后知识已变化”，其他内容不变。', { after: [
    { action: 'click', text: '评测', exact: true }, { action: 'waitFor', text: '真实用例', timeoutMs: 60_000 },
  ] }),

  confirmationCase('E2E-AUDIT-01', '需求计划执行 Diff 回执修订可串联', 'SCN-CLOSED', exactFourResourcePrompt, { after: [
    { action: 'click', text: '过程', exact: true }, { action: 'waitFor', text: '版本历史', timeoutMs: 30_000 },
    { action: 'assertText', text: '活动记录' },
  ] }),
  decisionCase('E2E-AUDIT-02', '资料事实推断用户决定明确标记', 'SCN-GENERATION', '根据合成资料生成规则；资料没有给出阈值时必须让我决定，不能自行猜测。'),

  {
    id: 'E2E-UI-LAYOUT-01', title: '桌面网页核心工作区稳定显示',
    plan: { name: 'E2E-UI-LAYOUT-01', viewport: DESKTOP, steps: [
      ...scenarioSteps('SCN-UI-BATCH'), { action: 'assertLayout' },
      { action: 'assertVisible', selector: 'textarea[aria-label="给建模助手发送消息"]' },
      { action: 'screenshot', name: 'layout-1440x900' },
    ] },
  },
  {
    id: 'E2E-UI-LAYOUT-02', title: '窄屏网页不保留不可用三栏',
    plan: { name: 'E2E-UI-LAYOUT-02', viewport: NARROW, steps: [
      ...scenarioSteps('SCN-UI-BATCH'), { action: 'assertLayout' },
      { action: 'assertVisible', selector: 'textarea[aria-label="给建模助手发送消息"]' },
      { action: 'screenshot', name: 'layout-1024x768' },
    ] },
  },
  {
    id: 'E2E-UI-LAYOUT-03', title: '手机端完成登录和核心建模闭环',
    plan: { name: 'E2E-UI-LAYOUT-03', viewport: MOBILE, steps: [
      ...scenarioSteps('SCN-CLOSED'), { action: 'assertLayout' },
      { action: 'assertVisible', selector: 'textarea[aria-label="给建模助手发送消息"]' },
      ...sendPrompt('请只说明当前合成项目状态，不做修改。'),
      { action: 'waitFor', text: '请只说明当前合成项目状态', timeoutMs: 15_000 },
      { action: 'screenshot', name: 'layout-390x844' },
    ] },
  },
  confirmationCase('E2E-UI-CONFIRM-01', '当前确认主操作始终可找到', 'SCN-CLOSED', '给折扣资格规则增加例外“确认按钮可见性验收”，其他内容不变。', { viewport: LOW_DESKTOP, after: [] }),
  confirmationCase('E2E-UI-CONFIRM-02', '多文件候选逐组审阅', 'SCN-CLOSED', exactFourResourcePrompt),
  confirmationCase('E2E-UI-CONFIRM-03', '抽检样本与最终范围一致', 'SCN-CLOSED', exactFourResourcePrompt),
  {
    id: 'E2E-UI-CONFIRM-04', title: '长期确认策略与本次决定分离',
    plan: { name: 'E2E-UI-CONFIRM-04', viewport: DESKTOP, steps: [
      ...scenarioSteps('SCN-CLOSED'), { action: 'click', text: '确认策略', exact: true },
      { action: 'waitFor', text: '当前项目确认策略' }, { action: 'click', text: '更改为自动接受', exact: true },
      { action: 'waitFor', text: '确认开启自动接受？' }, { action: 'click', text: '取消', exact: true },
      { action: 'assertText', text: '每次询问' }, { action: 'screenshot', name: 'E2E-UI-CONFIRM-04' },
    ] },
  },
  decisionCase('E2E-UI-ASK-01', '业务口径选项允许复核后提交', 'SCN-AMBIGUOUS', '请修改同名测试指标，但目标不够明确时给我可选择的业务选项。'),
  faultCase('E2E-UI-STATUS-01', '顶部状态准确表达运行和待办规模', 'SCN-LONG', 'F16', '启动长资料分析并停在业务检查点。', '当前业务批次已暂停'),
  {
    id: 'E2E-UI-RECOVERY-01', title: '消息发送失败不丢失输入',
    plan: { name: 'E2E-UI-RECOVERY-01', viewport: DESKTOP, steps: [
      ...scenarioSteps('SCN-CLOSED', 'F17'),
      { action: 'fill', selector: 'textarea[aria-label="给建模助手发送消息"]', value: '这段合成输入必须在发送失败后保留。' },
      { action: 'click', selector: 'button[aria-label="发送"]' },
      { action: 'waitFor', text: '未发送，内容已保留在输入框', timeoutMs: 30_000 },
      { action: 'assertValue', selector: 'textarea[aria-label="给建模助手发送消息"]', value: '这段合成输入必须在发送失败后保留。' },
      { action: 'screenshot', name: 'E2E-UI-RECOVERY-01' },
    ] },
  },
  {
    id: 'E2E-UI-RECOVERY-02', title: '错误和禁用状态给出就地恢复办法',
    plan: { name: 'E2E-UI-RECOVERY-02', viewport: DESKTOP, steps: [
      ...scenarioSteps('SCN-CLOSED', 'F18'), { action: 'click', text: 'objects', exact: true },
      { action: 'click', selector: 'button[title="objects/member_profile.md"]' },
      { action: 'waitFor', text: '当前文件暂时读取失败', timeoutMs: 30_000 },
      { action: 'assertText', text: '重试' }, { action: 'assertText', text: '建模助手' },
      { action: 'screenshot', name: 'E2E-UI-RECOVERY-02' },
    ] },
  },
  {
    id: 'E2E-UI-CONTROL-01', title: '运行开始后立即可见停止入口',
    plan: { name: 'E2E-UI-CONTROL-01', viewport: LOW_DESKTOP, steps: [
      ...scenarioSteps('SCN-LONG', 'F16'), ...sendPrompt('开始长资料建模并停在下一业务检查点。'),
      { action: 'waitFor', text: '停止任务', timeoutMs: 30_000 },
      { action: 'assertVisible', text: '停止任务' }, { action: 'click', text: '停止任务', exact: true },
      { action: 'waitFor', text: '正在停止', timeoutMs: 30_000 }, { action: 'screenshot', name: 'E2E-UI-CONTROL-01' },
    ] },
  },
  {
    id: 'E2E-UI-EVAL-01', title: '开始评测产生唯一可见回执',
    plan: { name: 'E2E-UI-EVAL-01', viewport: DESKTOP, steps: [
      ...scenarioSteps('SCN-EVALUATION', 'F13'), { action: 'click', text: '评测', exact: true },
      { action: 'waitFor', text: '评测数据源', timeoutMs: 60_000 }, { action: 'click', selector: '.wb-eval-tabs [role="tab"]:nth-child(2)' },
      { action: 'click', text: '开始正式评测', exact: true },
      { action: 'waitFor', text: '正式评测已创建，只会运行这一轮', timeoutMs: 30_000 },
      { action: 'waitFor', selector: '.wb-eval-round-list > button', timeoutMs: 60_000 },
      { action: 'assertCount', selector: '.wb-eval-round-list > button', expectedCount: 1 },
      { action: 'click', text: '停止评测', exact: true }, { action: 'screenshot', name: 'E2E-UI-EVAL-01' },
    ] },
  },
  {
    id: 'E2E-UI-EVAL-02', title: '评测失败展示业务诊断而非原始日志',
    plan: { name: 'E2E-UI-EVAL-02', viewport: DESKTOP, steps: [
      ...scenarioSteps('SCN-EVALUATION', 'F14'), { action: 'click', text: '评测', exact: true },
      { action: 'waitFor', text: '评测数据源', timeoutMs: 60_000 }, { action: 'click', selector: '.wb-eval-tabs [role="tab"]:nth-child(2)' },
      { action: 'click', text: '开始正式评测', exact: true },
      { action: 'waitFor', text: '知识包没有因此自动修改', timeoutMs: 60_000 },
      { action: 'assertNotText', text: 'Traceback' }, { action: 'assertNotText', text: 'ECONNREFUSED' },
      { action: 'screenshot', name: 'E2E-UI-EVAL-02' },
    ] },
  },
  {
    id: 'E2E-UI-VERSION-01', title: '恢复版本不会意外启动 Agent',
    plan: { name: 'E2E-UI-VERSION-01', viewport: DESKTOP, steps: [
      ...scenarioSteps('SCN-CLOSED'), { action: 'click', text: '过程', exact: true },
      { action: 'click', text: '版本历史', exact: true }, { action: 'waitFor', selector: 'section[aria-label="版本"]', timeoutMs: 60_000 },
      { action: 'click', text: '恢复', exact: true }, { action: 'waitFor', text: '当前内容会保存为新版本' },
      { action: 'click', text: '确认恢复', exact: true }, { action: 'waitFor', text: '助手保持空闲', timeoutMs: 60_000 },
      { action: 'assertNotText', text: '助手运行中' }, { action: 'screenshot', name: 'E2E-UI-VERSION-01' },
    ] },
  },
  {
    id: 'E2E-UI-CREATE-01', title: '创建页解释高成本选择和禁用原因',
    plan: { name: 'E2E-UI-CREATE-01', viewport: DESKTOP, steps: [
      ...loginSteps(), { action: 'click', text: '新建项目', exact: true },
      { action: 'waitFor', text: '新建建模项目', timeoutMs: 30_000 },
      { action: 'assertDisabled', text: '开始建模' }, { action: 'assertText', text: '填写项目名称后即可开始建模' },
      { action: 'assertText', text: '项目建立后切换类型需要重新生成知识包' },
      { action: 'screenshot', name: 'E2E-UI-CREATE-01' },
    ] },
  },
  {
    id: 'E2E-UI-A11Y-01', title: '主要交互可识别可点击且焦点清晰',
    plan: { name: 'E2E-UI-A11Y-01', viewport: DESKTOP, steps: [
      ...scenarioSteps('SCN-CLOSED'),
      { action: 'assertEnabled', selector: 'textarea[aria-label="给建模助手发送消息"]' },
      { action: 'assertVisible', selector: 'button[aria-label="上传资料"]' },
      { action: 'assertDisabled', selector: 'button[aria-label="发送"]' },
      { action: 'click', selector: 'textarea[aria-label="给建模助手发送消息"]' },
      { action: 'press', key: 'Tab' }, { action: 'screenshot', name: 'E2E-UI-A11Y-01' },
    ] },
  },
  {
    id: 'E2E-UI-RESUME-01', title: '重新进入先展示进展摘要和当前待办',
    plan: { name: 'E2E-UI-RESUME-01', viewport: DESKTOP, steps: [
      ...scenarioSteps('SCN-UI-RESUME', 'F16'), ...sendPrompt('开始处理长资料并在下一检查点等待。'),
      { action: 'waitFor', text: '当前业务批次已暂停', timeoutMs: AGENT_TIMEOUT },
      { action: 'reload' }, { action: 'waitFor', text: '当前业务批次已暂停', timeoutMs: 60_000 },
      { action: 'assertVisible', text: '停止任务' }, { action: 'assertText', text: '当前计划' },
      { action: 'screenshot', name: 'E2E-UI-RESUME-01' },
    ] },
  },
  {
    id: 'E2E-UI-COPY-01', title: '主流程使用业务语言区分候选确认发布',
    plan: { name: 'E2E-UI-COPY-01', viewport: DESKTOP, steps: [
      ...scenarioSteps('SCN-CLOSED'),
      { action: 'assertText', text: '没有待处理决定' },
      { action: 'assertNotText', text: 'candidate_revision' },
      { action: 'assertNotText', text: 'tool_call_id' },
      { action: 'assertNotText', text: 'checkpoint_id' },
      { action: 'screenshot', name: 'E2E-UI-COPY-01' },
    ] },
  },
];

export const requiredBrowserAcceptanceIds = [
  'E2E-BOOT-01',
  ...Array.from({ length: 11 }, (_, index) => `E2E-EDIT-${String(index + 1).padStart(2, '0')}`),
  ...Array.from({ length: 8 }, (_, index) => `E2E-GEN-${String(index + 1).padStart(2, '0')}`),
  ...Array.from({ length: 4 }, (_, index) => `E2E-VAL-${String(index + 1).padStart(2, '0')}`),
  ...Array.from({ length: 7 }, (_, index) => `E2E-REL-${String(index + 1).padStart(2, '0')}`),
  ...Array.from({ length: 4 }, (_, index) => `E2E-SCRIPT-${String(index + 1).padStart(2, '0')}`),
  ...Array.from({ length: 5 }, (_, index) => `E2E-EVAL-${String(index + 1).padStart(2, '0')}`),
  ...Array.from({ length: 2 }, (_, index) => `E2E-AUDIT-${String(index + 1).padStart(2, '0')}`),
  ...Array.from({ length: 3 }, (_, index) => `E2E-UI-LAYOUT-${String(index + 1).padStart(2, '0')}`),
  ...Array.from({ length: 4 }, (_, index) => `E2E-UI-CONFIRM-${String(index + 1).padStart(2, '0')}`),
  'E2E-UI-ASK-01', 'E2E-UI-STATUS-01',
  'E2E-UI-RECOVERY-01', 'E2E-UI-RECOVERY-02', 'E2E-UI-CONTROL-01',
  'E2E-UI-EVAL-01', 'E2E-UI-EVAL-02', 'E2E-UI-VERSION-01', 'E2E-UI-CREATE-01',
  'E2E-UI-A11Y-01', 'E2E-UI-RESUME-01', 'E2E-UI-COPY-01',
];

void WIDE;
void EDGE;
