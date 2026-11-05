// 把 REA 的原始结果裁成测试夹具：只留映射会读的部分，证据上下文、指纹、字节码这类大块去掉。
// 用法：node test/fixtures/trim.mjs <REA 输出 .json 或 .jsonl> <夹具 .json> [jsonl 里取第几行，从 0 起]
import { readFileSync, writeFileSync } from 'node:fs';

const [input, output, line] = process.argv.slice(2);
const text = readFileSync(input, 'utf8');
const evidence = JSON.parse(line === undefined ? text : text.split('\n').filter(Boolean)[Number(line)]);
const result = evidence.normalized_result ?? {};
const KEEP_SEMANTIC = new Set(['function', 'module', 'literal', 'call-site']);

const slimEvidence = (value) => (value ? { state: value.state, location: value.location } : value);

if (result.graph) {
  result.graph = {
    nodes: result.graph.nodes.map((node) => ({
      node_id: node.node_id,
      kind: node.kind,
      observations: (node.observations ?? []).slice(0, 1).map((item) => ({
        label: item.label, properties: item.properties, evidence: slimEvidence(item.evidence),
      })),
    })),
    edges: result.graph.edges.map((edge) => ({
      source_node_id: edge.source_node_id, target_node_id: edge.target_node_id, relation: edge.relation,
      evidence: { state: edge.evidence?.state },
    })),
  };
}
if (result.semantic_graph) {
  const semantic = result.semantic_graph;
  result.semantic_graph = {
    nodes: semantic.nodes
      .filter((node) => KEEP_SEMANTIC.has(node.kind) || /^object:write:/.test(node.identity?.role_key ?? ''))
      .map(({ node_id, kind, identity, label, properties }) => ({ node_id, kind, identity, label, properties })),
    relations: semantic.relations
      .filter((relation) => relation.relation === 'calls')
      .map(({ source_node_id, target_node_id, relation, resolution }) => ({ source_node_id, target_node_id, relation, resolution })),
    unknowns: semantic.unknowns.map(() => ({})),
  };
}
if (result.route_inventory) result.route_inventory = { routes: result.route_inventory.routes };
for (const code of result.code_objects ?? []) delete code.bytecode;
for (const method of result.methods ?? []) {
  if (method.body) method.body = { anchors: (method.body.anchors ?? []).filter((anchor) => anchor.opcode === 'ldstr') };
}
for (const key of ['field_accesses', 'member_refs']) delete result[key];

const trimmed = {
  subject: evidence.subject,
  provider: evidence.provider,
  operation: evidence.operation,
  limitations: evidence.limitations,
  normalized_result: result,
};
writeFileSync(output, `${JSON.stringify(trimmed)}\n`);
