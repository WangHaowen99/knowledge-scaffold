import assert from 'node:assert/strict';
import test from 'node:test';
import { createBody } from '../src/pages/create/createBody.js';
import { passwordRules } from '../src/pages/login/passwordRules.js';
import { activityText, projectLook } from '../src/pages/projects/projectCard.js';
import { layoutThumb } from '../src/pages/workbench/thumb.js';
import { mergeFlow, trackNodes } from '../src/state/flow.js';

const NOW = new Date(2026, 10, 1, 15, 30).getTime();
const at = (d, h, m) => new Date(2026, 10, d, h, m).toISOString();

test('项目卡片时间：运行中、归档、几分钟前、几小时前、昨天、几天前', () => {
  assert.equal(activityText({ autobuild: { state: 'running' } }, NOW), '运行中');
  assert.equal(activityText({ archived: true, archived_at: at(1, 9, 0).replace('-11-01', '-10-12') }, NOW), '归档于 10-12');
  assert.equal(activityText({ last_activity_at: new Date(NOW - 5 * 60000).toISOString() }, NOW), '5 分钟前');
  assert.equal(activityText({ last_activity_at: new Date(NOW - 65 * 60000).toISOString() }, NOW), '1 小时前');
  assert.equal(activityText({ last_activity_at: new Date(2026, 9, 31, 18, 20).toISOString() }, NOW), '昨天 18:20');
  assert.equal(activityText({ last_activity_at: new Date(2026, 9, 29, 15, 30).toISOString() }, NOW), '3 天前');
});

test('卡片图标', () => {
  assert.equal(projectLook({ archived: true, package_name: 'x' }).icon, 'archive');
  assert.deepEqual(projectLook({ package_name: 'a' }), projectLook({ package_name: 'a' }));
});

test('密码规则：长度、三类字符、不含用户名、常见密码、首尾空格', () => {
  const ok = passwordRules('zhangsan', 'Mango#2026');
  assert.ok(Object.values(ok).every(Boolean));
  const bad = passwordRules('zhangsan', ' Zhangsan2026');
  assert.equal(bad.contains_username, false);
  assert.equal(bad.edge_space, false);
  assert.equal(passwordRules('u', 'password').common, false);
  assert.equal(passwordRules('u', 'abcdefgh').classes, false);
});

test('新建项目请求体：只带上传完成的资料，示例项目带 example', () => {
  const body = createBody({
    name: ' 甲 ', goal: '目标', starts: ['upload', 'example'],
    files: [{ upload: { upload_id: 'u1' }, purpose: 'schema' }, { name: '上传中' }],
    industries: ['industry-retail'], systems: [], caps: { trace: true, web: false }, engine: 'claude', buildMode: 'L4',
  });
  assert.equal(body.name, '甲');
  assert.deepEqual(body.materials, [{ upload_id: 'u1', purpose: 'schema' }]);
  assert.deepEqual(body.industry_packs, [{ name: 'industry-retail' }]);
  assert.equal(body.build_mode, 'L4');
  assert.deepEqual(body.example, { id: 'seed_example' });
});

const node = (id, placement, display_state = 'not_ready') => ({ id, title: id, placement, display_state, state: display_state });
test('主线进度条按边排序，只放主线、步骤、关卡', () => {
  const flow = {
    nodes: [node('a', 'spine'), node('gate', 'gate'), node('b', 'step'), node('pub', 'spine'), node('opt', 'branch')],
    edges: [{ from: 'a', to: 'b' }, { from: 'b', to: 'gate' }, { from: 'gate', to: 'pub' }],
  };
  assert.deepEqual(trackNodes(flow).map((n) => n.id), ['a', 'b', 'gate', 'pub']);
});

test('项目流的流程增量合进快照', () => {
  const flow = { flow_hash: 'h1', nodes: [node('a', 'spine')], edges: [], slices: [{ id: 's1', stages: { object: 'ready', rule: 'draft' } }] };
  const next = mergeFlow(flow, { flow_hash: 'h2', nodes: [{ id: 'a', state: 'running' }], slices: [{ id: 's1', stages: { object: 'confirmed' } }] });
  assert.equal(next.flow_hash, 'h2');
  assert.equal(next.nodes[0].state, 'running');
  assert.deepEqual(next.slices[0].stages, { object: 'confirmed', rule: 'draft' });
});

test('流程图缩略：按层排，判断后的两个分支上下分开', () => {
  const nodes = [
    { id: 'n1', type: 'start', label: '受理申请' }, { id: 'n2', type: 'step', label: '读取申请量' },
    { id: 'n3', type: 'decision', label: '严于标准?' }, { id: 'n4', type: 'step', label: '按门店执行' }, { id: 'n5', type: 'step', label: '生成审批提示' },
  ];
  const edges = [{ from: 'n1', to: 'n2' }, { from: 'n2', to: 'n3' }, { from: 'n3', to: 'n4' }, { from: 'n3', to: 'n5' }];
  const { pos } = layoutThumb(nodes, edges);
  assert.equal(pos.get('n1').x, 16);
  assert.ok(pos.get('n2').x > pos.get('n1').x + pos.get('n1').w);
  assert.equal(pos.get('n4').x, pos.get('n5').x);
  assert.ok(pos.get('n4').cy < pos.get('n5').cy);
  assert.equal(pos.get('n3').cy, 75);
});
