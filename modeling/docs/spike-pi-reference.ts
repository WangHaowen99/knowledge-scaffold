/**
 * pi spike —— 一次性验证脚本，验完即弃。
 * 回答设计文档 §9 的四个关键假设：
 *   Q1 能否注册自定义工具（读数据字典 / 写包文件）
 *   Q2 agent 状态能否序列化存盘、之后恢复继续（访谈挂起/恢复的命根子）
 *   Q3 OpenAI-compatible 端点接入方式
 *   Q4 人机交互中断的干净建模方式
 *
 * 主体用 faux provider（无需真实 key）编排一个「读数据 → 访谈挂起 → 存盘 →
 * 恢复 → 基于答案写包 → 结束」的完整场景，模拟真实建模节点里的挂起/恢复。
 */
import { writeFileSync, readFileSync } from 'node:fs';
import { Type } from 'typebox';
import { Agent } from '@earendil-works/pi-agent-core';
import { createFauxCore, fauxAssistantMessage, fauxToolCall } from '@earendil-works/pi-ai/providers/faux';

const log = (tag: string, msg: string) => console.log(`  [${tag}] ${msg}`);
const STATE_FILE = '/tmp/pi-spike-state.json';

// ---- 副作用探针：验证工具真的被执行 ----
const sideEffects: string[] = [];
const dataDict = {
  STRUCTURE_LIST: ['ID', 'ParentPart', 'ChildPart', 'Num', 'Effective', 'Comments'],
};

// ---- Q1：自定义工具 ----
const readDataDict = {
  name: 'read_data_dict',
  label: '读取数据字典',
  description: '返回某张表的字段列表',
  parameters: Type.Object({ table: Type.String() }),
  async execute(_id: string, params: { table: string }) {
    sideEffects.push(`read_data_dict(${params.table})`);
    const fields = (dataDict as Record<string, string[]>)[params.table] ?? [];
    return { content: [{ type: 'text' as const, text: fields.join(', ') }], details: { fields } };
  },
};

// ---- Q4：访谈工具 —— terminate:true 让 agent 自然停在问题处 ----
const askUser = {
  name: 'ask_user',
  label: '向专家提问',
  description: '提出一批选择题，等待专家作答',
  parameters: Type.Object({
    questions: Type.Array(Type.Object({ q: Type.String(), options: Type.Array(Type.String()) })),
  }),
  async execute(_id: string, params: { questions: { q: string; options: string[] }[] }) {
    sideEffects.push(`ask_user(${params.questions.length}题)`);
    // 关键：不挂起 Promise，而是把问题作为工具结果记入 transcript 并 terminate。
    // agent 停下后，state.messages 就是可序列化的完整快照。
    return {
      content: [{ type: 'text' as const, text: '[已向专家发送问题，等待作答]' }],
      details: { pending_questions: params.questions },
      terminate: true,
    };
  },
};

const writePackageFile = {
  name: 'write_package_file',
  label: '写入知识包文件',
  description: '把内容写入知识包某文件',
  parameters: Type.Object({ path: Type.String(), content: Type.String() }),
  async execute(_id: string, params: { path: string; content: string }) {
    sideEffects.push(`write_package_file(${params.path})`);
    return { content: [{ type: 'text' as const, text: `已写入 ${params.path}` }], details: {} };
  },
};

const TOOLS = [readDataDict, askUser, writePackageFile];

// ---- faux LLM 编排：预设每一步模型响应 ----
function buildFaux() {
  const faux = createFauxCore({ provider: 'faux', models: [{ id: 'faux-1' }] });
  return { faux, model: faux.getModel(), streamFn: faux.streamSimple };
}

function makeAgent(model: any, streamFn: any, messages: any[] = []) {
  return new Agent({
    streamFn,
    initialState: {
      model,
      systemPrompt: '你是知识包建模访谈 agent。',
      tools: TOOLS as any,
      messages,
    },
  });
}

async function main() {
  console.log('\n=== pi spike：建模节点挂起/恢复验证 ===\n');
  const { faux, model, streamFn } = buildFaux();

  // 阶段 1：读数据 → 访谈挂起
  console.log('阶段 1（首轮 run）：读数据字典 → 发起访谈 → 挂起');
  faux.setResponses([
    fauxAssistantMessage([fauxToolCall('read_data_dict', { table: 'STRUCTURE_LIST' })]),
    fauxAssistantMessage([
      fauxToolCall('ask_user', {
        questions: [{ q: 'STRUCTURE_LIST 的 Effective 字段语义是？', options: ['批次区间', '生效日期', '版本号'] }],
      }),
    ]),
  ]);

  const agent1 = makeAgent(model, streamFn);
  await agent1.prompt('开始对 STRUCTURE_LIST 表建模');
  await agent1.waitForIdle();

  const snapshot = agent1.state.messages;
  log('Q1', `工具已执行：${sideEffects.join(' → ')}`);
  log('Q4', `agent 停在访谈处，transcript 含 ${snapshot.length} 条消息（末条含 pending 问题）`);

  // 阶段 2：序列化存盘 → 销毁 agent（模拟关浏览器 / 进程重启）
  writeFileSync(STATE_FILE, JSON.stringify(snapshot, null, 2));
  const bytes = readFileSync(STATE_FILE).length;
  log('Q2', `state.messages 已 JSON 序列化存盘（${bytes} 字节），销毁 agent 实例`);

  // 阶段 3：从盘恢复 → 注入专家答案 → 继续
  console.log('\n阶段 2（恢复 run）：从盘加载 → 注入专家答案 → 继续建模');
  const restored = JSON.parse(readFileSync(STATE_FILE, 'utf8'));
  faux.setResponses([
    fauxAssistantMessage([
      fauxToolCall('write_package_file', {
        path: 'objects/STRUCTURE_LIST.md',
        content: '# STRUCTURE_LIST\nEffective: 批次区间',
      }),
    ]),
    fauxAssistantMessage('已根据专家答复完成 STRUCTURE_LIST 对象建模。'),
  ]);

  const agent2 = makeAgent(model, streamFn, restored);
  const before = agent2.state.messages.length;
  // 专家答案作为一条 user 消息注入，继续对话
  await agent2.prompt('专家答复：Effective 字段的语义是「批次区间」');
  await agent2.waitForIdle();

  const finalMsgs = agent2.state.messages;
  const wrotePackage = sideEffects.some((s) => s.startsWith('write_package_file'));
  const answerInContext = JSON.stringify(finalMsgs).includes('批次区间');

  log('Q2', `恢复成功：加载 ${before} 条历史消息，续跑后增长到 ${finalMsgs.length} 条`);
  log('Q4', `恢复后基于专家答案继续：${wrotePackage ? '已写入 objects/STRUCTURE_LIST.md ✓' : '未写入 ✗'}`);
  log('ctx', `专家答案进入上下文并影响后续：${answerInContext ? '是 ✓' : '否 ✗'}`);

  // 汇总
  console.log('\n=== 结论 ===');
  console.log(`Q1 自定义工具注册与执行：${sideEffects.length >= 3 ? 'PASS ✓' : 'FAIL ✗'}`);
  console.log(`Q2 状态序列化 / 恢复继续：${before > 0 && finalMsgs.length > before ? 'PASS ✓' : 'FAIL ✗'}`);
  console.log(`Q4 访谈挂起（terminate 模式）：${wrotePackage && answerInContext ? 'PASS ✓' : 'FAIL ✗'}`);
  console.log('Q3 OpenAI-compatible：见 spike-notes.md（用 createProvider + baseUrl，需真实 key，未在本脚本联网）');
}

main().catch((e) => {
  console.error('\nSPIKE ERROR:', e?.stack || e);
  process.exit(1);
});
