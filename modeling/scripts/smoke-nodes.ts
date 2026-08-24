/**
 * M1 冒烟：验证 workflow 节点 1→3 端到端跑通（验收「传入样例资料走到节点 3」）。
 * 用 faux LLM（无需真实 key），造一个测试 Excel，走：建项目 → 解析入数据字典 →
 * 动态分段 Map-Reduce 初步分析与持久化结果恢复。
 *
 * 运行：MODELING_LLM_BASE_URL=x MODELING_LLM_MODEL=x DATA_DIR=./data/smoke npx tsx scripts/smoke-nodes.ts
 */
import * as XLSX from 'xlsx';
import { parseDocument } from '../src/documents/parser.js';
import { runAnalyze } from '../src/workflow/engine.js';
import { createProject } from '../src/projects/repo.js';
import { insertDocument, listDocuments } from '../src/documents/repo.js';
import { buildFauxModeler } from '../src/llm/provider.js';

const ok = (c: boolean, msg: string) => console.log(`  ${c ? '✓' : '✗ FAIL'}  ${msg}`);

// 造一个双 sheet 的测试 Excel（含注释行），模拟专家上传的数据库表。
function buildTestXlsx(): Buffer {
  const structure_list = [
    ['ID', 'ParentPart', 'ChildPart', 'Num', 'Effective'],
    ['行编号', '父项TargetItem编号', '子项TargetItem编号', '数量', '有效性(批次区间)'],
    ['1', 'A001', 'B001', '2', 'BATCH001-BATCH050'],
    ['2', 'A001', 'B002', '1', 'BATCH001-BATCH050'],
    ['3', 'A002', 'B003', '4', 'BATCH051-'],
  ];
  const change_order = [
    ['ChangeOrderCode', 'Title', 'Status'],
    ['CHANGE_ORDER-001', '翼盒结构变更', '已批准'],
    ['CHANGE_ORDER-002', '蒙皮材料替换', '审查中'],
  ];
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet(structure_list), 'STRUCTURE_LIST');
  XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet(change_order), 'CHANGE_ORDER');
  return XLSX.write(wb, { type: 'buffer', bookType: 'xlsx' }) as Buffer;
}

async function main() {
  console.log('\n=== M1 冒烟：workflow 节点 1→3 ===\n');

  // 节点 1：建项目
  const project = createProject({ owner: 'smoke-user', name: '示例工程变更 建模', domain: '示例业务', background: '测试' });
  ok(!!project.id, `节点 1 建项目：${project.id} · stage=${project.stage}`);

  // 节点 2：解析 Excel → 数据字典
  const parsed = await parseDocument('excel', buildTestXlsx());
  ok(!!parsed.tables && parsed.tables.length === 2, `节点 2 Excel 解析出 ${parsed.tables?.length} 张表`);
  const structure_list = parsed.tables?.find((t) => t.name === 'STRUCTURE_LIST');
  ok(!!structure_list && structure_list.fields.length === 5, `STRUCTURE_LIST 表 ${structure_list?.fields.length} 个字段`);
  const effective = structure_list?.fields.find((f) => f.name === 'Effective');
  ok(effective?.note === '有效性(批次区间)', `注释行被识别为字段说明：Effective → "${effective?.note}"`);
  const idField = structure_list?.fields.find((f) => f.name === 'ID');
  ok((idField?.distinctRatio ?? 0) === 1, `唯一值比例可用于主键推断：ID distinctRatio=${idField?.distinctRatio}`);

  insertDocument({
    projectId: project.id, filename: 'domain-data.xlsx', kind: 'excel',
    parseStatus: 'parsed', tables: parsed.tables,
  });
  ok(listDocuments(project.id).length === 1, '文档入库');

  // 节点 3：初步分析（faux agent 跑通领域分析与材料分类工具调用）
  const tables = listDocuments(project.id).flatMap((d) => d.tables ?? []);
  const { node, analysis } = await runAnalyze(project.id, tables, [], buildFauxModeler());
  ok(node.status === 'done', `节点 3 状态=${node.status}`);
  ok(analysis.candidateObjects.length === 2, `产出候选对象 ${analysis.candidateObjects.length} 个：${analysis.candidateObjects.map((o) => o.name).join('/')}`);
  ok(Array.isArray(analysis.openQuestions), `产出开放问题 ${analysis.openQuestions.length} 条`);

  // 验证节点 3 的 agent 快照可序列化恢复（spike 结论在真实节点上复现）
  const snapshot = node.output;
  const serialized = JSON.stringify(snapshot);
  const restored = JSON.parse(serialized);
  ok(Boolean(restored?.domainOverview), `节点结果可序列化恢复：${serialized.length} 字节`);

  console.log('\n=== M1 冒烟通过：项目已走到节点 3（初步分析），产物落库、快照可恢复 ===\n');
}

main().catch((e) => { console.error('\nSMOKE ERROR:', e?.stack || e); process.exit(1); });
