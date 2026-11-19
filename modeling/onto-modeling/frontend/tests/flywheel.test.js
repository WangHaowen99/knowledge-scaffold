// Synthetic flywheel states; no real project data.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  DELEGATION_OPTIONS, POLL_ACTIVE_MS, POLL_IDLE_MS, appendRecords, auditKey, canIgnore, canRollback, connection,
  delegationHint, delegationNotice, evaluateBlock, evaluationHint, evaluationLine, formatAt,
  holdHint, iteration, loginMissing, mode, nextPoll, nextVersion, optimizeBlock, optimizeFocus, publishBlock,
  recordsSignature, runKey, switchWarning,
  earlierChangesNote, publishFocus, rate, reasonText, reauthorizeNotice, sourceLine, statusOf, toggleNotice, transitions,
  feedbackNote, isFeedback, ROUND_LIMITS, roundsLabel, canSkipEvaluation, confirmsPrompt, graphPrompt,
} from '../src/lib/flywheel.js';

const run = (fields = {}) => ({
  id: 'FW-001', kind: 'evaluation', status: 'waiting_optimize', reason: 'automation_off', human_hold: true,
  release_id: 'release-a', version: '0.1.8', eval_run_id: 'a'.repeat(32), ...fields,
});
const state = (fields = {}) => ({
  available: true, can_write: true, assistant_busy: false, active_id: null, candidate_id: null,
  package: { release_id: 'release-a', version: '0.1.8' }, settings: { optimize: false, publish: false, max_rounds: 3 },
  runs: [], ...fields,
});

test('next version is predicted from the patch number only for numeric versions', () => {
  assert.equal(nextVersion('0.1.8'), '0.1.9');
  assert.equal(nextVersion('1.2'), '1.3');
  assert.equal(nextVersion('0.1.0-eval.1'), '');
  assert.equal(nextVersion(undefined), '');
});

test('snapshots and times are shown in the prototype format', () => {
  assert.equal(iteration({ commit: 'c'.repeat(40), iteration: 27 }), 'iteration-027');
  assert.equal(iteration(null), '尚未生成');
  assert.equal(formatAt(0), '—');
  assert.match(formatAt(1793000000), /^\d{2}-\d{2} \d{2}:\d{2}$/);
});

test('a queued automatic run is labelled apart from runs waiting for confirmation', () => {
  assert.deepEqual(statusOf(run()), ['待确认优化', 'amber']);
  assert.deepEqual(statusOf(run({ human_hold: false, reason: 'queued' })), ['等待自动执行', 'blue']);
  assert.equal(mode(run({ human_hold: false })), '等待自动执行');
  assert.equal(mode(run({ kind: 'rollback', status: 'restored' })), '人工回退');
  assert.equal(holdHint(run({ reason: 'round_limit' }), 3), '已连续自动处理 3 轮，本次等待人工确认后继续。');
});

test('reason texts carry the versions and the failing checks', () => {
  const stale = run({ status: 'blocked', reason: 'version_mismatch', version: '0.1.7' });
  assert.equal(reasonText(stale, { current: '0.1.8' }), '该通知基于 v0.1.7，当前项目基于 v0.1.8。请评测当前版本后再优化。');
  const failed = run({ status: 'blocked', reason: 'checks_failed', check_groups: { failing: ['规则检查', '数据绑定'] } });
  assert.equal(reasonText(failed), '未通过：规则检查、数据绑定。已保留优化结果，修复并重新检查后才能发布。');
  assert.equal(reasonText(run({ status: 'blocked', reason: 'unknown', note: '合成说明' })), '合成说明');
});

test('optimization is blocked by the same conditions as the backend', () => {
  assert.equal(optimizeBlock(state(), run()), '');
  assert.equal(optimizeBlock(state({ can_write: false }), run()), '只读评审角色不能执行评测优化');
  assert.match(optimizeBlock(state(), run({ release_id: 'release-old' })), /与当前建模项目版本 0.1.8 不一致/);
  assert.equal(optimizeBlock(state({ active_id: 'FW-002' }), run()), '项目正在执行任务，请等待本次任务完成');
  assert.equal(optimizeBlock(state({ candidate_id: 'FW-002' }), run()), '请先处理当前待发布候选内容');
  assert.equal(optimizeBlock(state({ assistant_busy: true }), run()), '建模助手正在运行，请等本轮结束后再优化');
  assert.equal(optimizeBlock(state(), run({ status: 'published' })), '本次建议已处理，可在运行记录中查看结果');
  assert.equal(optimizeBlock(state(), run({ status: 'blocked', reason: 'optimization_interrupted' })), '');
  assert.equal(publishBlock(state({ active_id: 'FW-001' }), run()), '');
  assert.equal(publishBlock(state({ active_id: 'FW-002' }), run()), '项目正在执行任务，请等待本次任务完成');
});

test('rollback and ignore follow the run status', () => {
  const snapshot = { commit: 'c'.repeat(40), iteration: 3 };
  assert.equal(canRollback(run({ status: 'published', snapshot })), true);
  assert.equal(canRollback(run({ status: 'published' })), false);
  assert.equal(canRollback(run({ status: 'rolledback', snapshot })), false);
  assert.equal(canIgnore(run()), true);
  assert.equal(canIgnore(run({ status: 'blocked', reason: 'checks_failed' })), false);
  assert.equal(canIgnore(run({ status: 'blocked', reason: 'no_changes' })), true);
});

test('an evaluation can be skipped while waiting for it, and stopped waiting for while it runs', () => {
  assert.equal(canSkipEvaluation(run({ status: 'waiting_evaluation' })), true);
  assert.equal(canSkipEvaluation(run({ status: 'blocked', reason: 'evaluation_failed' })), true);
  // 评测中也能不再等待：评测任务被删除或迟迟没有结果时由此离开
  assert.equal(canSkipEvaluation(run({ status: 'evaluating' })), true);
  assert.equal(canSkipEvaluation(run({ status: 'published' })), false);
  assert.equal(canSkipEvaluation(run({ status: 'blocked', reason: 'checks_failed' })), false);
});

test('a graph deletion is confirmed only for the question on screen', () => {
  const asked = run({
    status: 'waiting_evaluation', graph_confirmation: '合成提示：已有 2 个节点', graph_confirmation_code: 'ab12',
  });
  const choice = graphPrompt(asked);
  assert.deepEqual(choice, { text: '合成提示：已有 2 个节点', code: 'ab12' });
  assert.equal(confirmsPrompt(choice, asked), true);
  // 数据又有变化，评测服务给了新的提示与确认码：之前的勾选不再算数
  const changed = { ...asked, graph_confirmation: '合成提示：现有 3 个节点', graph_confirmation_code: 'cd34' };
  assert.equal(confirmsPrompt(choice, changed), false);
  assert.equal(confirmsPrompt(choice, { ...asked, graph_confirmation_code: 'cd34' }), false);
  assert.equal(confirmsPrompt(null, asked), false);
  assert.equal(graphPrompt(run({ status: 'waiting_evaluation' })), null);
});

test('the cards focus on running, then waiting, then blocked items of the latest evaluation or version', () => {
  const runs = [run({ id: 'FW-003' }), run({ id: 'FW-002' }), run({ id: 'FW-001', status: 'published', changed: 2 })];
  assert.deepEqual(optimizeFocus(runs), { kind: 'waiting', run: runs[1], more: 1 });
  assert.equal(optimizeFocus([run({ status: 'optimizing' }), ...runs]).kind, 'running');
  assert.deepEqual(optimizeFocus([runs[2]]), { kind: 'idle', run: runs[2] });
  const stale = run({ id: 'FW-004', status: 'blocked', reason: 'version_mismatch' });
  assert.deepEqual(optimizeFocus([stale, runs[2]]), { kind: 'blocked', run: stale });
  assert.equal(optimizeFocus([runs[2], stale]).kind, 'idle');
  const blocked = run({ id: 'FW-004', status: 'blocked', reason: 'hub_failed', candidate: {} });
  assert.deepEqual(publishFocus([blocked, ...runs], 'release-a'), { kind: 'blocked', run: blocked });
  assert.deepEqual(publishFocus([blocked, ...runs], 'release-b'), { kind: 'idle', run: runs[2] });
  assert.deepEqual(publishFocus(runs, 'release-a'), { kind: 'idle', run: runs[2] });
  assert.equal(optimizeFocus([blocked]).kind, 'idle');
});

test('an unverified solver revision waits for its validation and can be retried', () => {
  const blocked = run({ id: 'FW-004', status: 'blocked', reason: 'solver_unverified', candidate: {} });
  assert.deepEqual(publishFocus([blocked], 'release-a'), { kind: 'blocked', run: blocked });
  assert.match(reasonText(blocked), /飞轮不代为确认，未创建新版本/);
  const publishing = { ...blocked, status: 'publishing', reason: '' };
  assert.deepEqual(transitions(state({ runs: [publishing] }), state({ runs: [blocked] })),
    [['error', '求解能力尚未验证，未创建新版本。']]);
});

test('records load in batches and drop entries a later batch repeats', () => {
  const page = (...ids) => ids.map((id) => run({ id }));
  // 第二批读取前新到了一条，旧记录被往后推了一位：重复的 FW-018 去掉，不缺不重
  const merged = appendRecords(page('FW-020', 'FW-019', 'FW-018'), page('FW-018', 'FW-017'), runKey);
  assert.deepEqual(merged.map(runKey), ['FW-020', 'FW-019', 'FW-018', 'FW-017']);
  const change = { at: 1, action: '后台授权', description: '合成说明' };
  assert.equal(appendRecords([change], [{ ...change }], auditKey).length, 1);
});

test('the records list reloads only when the polled records changed', () => {
  const before = state({ total: 1, runs: [run()], audit: [{ at: 1 }] });
  assert.equal(recordsSignature(before, 'runs'), recordsSignature(state({ total: 1, runs: [run()], audit: [{ at: 1 }] }), 'runs'));
  assert.notEqual(recordsSignature(before, 'runs'), recordsSignature({ ...before, runs: [run({ status: 'optimizing' })] }, 'runs'));
  assert.notEqual(recordsSignature(before, 'settings'), recordsSignature({ ...before, audit: [{ at: 2 }, { at: 1 }] }, 'settings'));
  assert.equal(recordsSignature(null, 'runs'), '');
});

test('polling speeds up while the dialog is open or a step is running', () => {
  assert.equal(nextPoll(state(), false), POLL_IDLE_MS);
  assert.equal(nextPoll(state(), true), POLL_ACTIVE_MS);
  assert.equal(nextPoll(state({ active_id: 'FW-001' }), false), POLL_ACTIVE_MS);
  assert.equal(nextPoll(state({ runs: [run({ human_hold: false })] }), false), POLL_ACTIVE_MS);
  // 评测在评测服务里要跑很久，弹窗关着时不密集轮询；即将自动发起的评测仍要尽快推进
  assert.equal(nextPoll(state({ active_id: 'FW-001', runs: [run({ status: 'evaluating' })] }), false), POLL_IDLE_MS);
  assert.equal(nextPoll(state({ runs: [run({ status: 'waiting_evaluation', human_hold: false })] }), false), POLL_ACTIVE_MS);
});

test('connection status explains how evaluation results arrive', () => {
  assert.deepEqual(connection(state({ evaluation: { configured: false } })), { color: 'gray', text: '未配置评测服务' });
  assert.equal(connection(state({ evaluation: { configured: true, reachable: false, notifications: true } })).text, '评测服务暂不可用');
  assert.equal(connection(state({ evaluation: { configured: true, reachable: true, notifications: true } })).text, '评测通知已连接');
});

test('a switch says something only when its background authorization has a problem', () => {
  const settings = { optimize: true, publish: true, max_rounds: 3 };
  const active = { status: 'active', expires_at: 1796000000, message: '' };
  // 一切正常、或开关关着：不显示任何说明
  assert.equal(switchWarning(state({ settings, delegation: { evaluation: active, hub: active, login: 'active' } }), 'optimize'), '');
  assert.equal(switchWarning(state({ settings, delegation: { evaluation: active, hub: active, login: 'active' } }), 'publish'), '');
  assert.equal(switchWarning(state({ delegation: { evaluation: { status: 'issue', message: '合成原因' } } }), 'optimize'), '');
  assert.match(switchWarning(state({ settings, delegation: { evaluation: { status: 'issue', message: '合成原因' } } }), 'optimize'),
    /需保持页面打开.*合成原因/);
  assert.match(switchWarning(state({ settings, delegation: { hub: { status: 'pending' } } }), 'publish'), /后台授权未就绪/);
  assert.match(switchWarning(state({ settings, delegation: { evaluation: { status: 'unlinked' } } }), 'optimize'), /尚未发布到 Hub/);
});

test('the delegation lifetime offers the agreed choices and no limit shows no date', () => {
  assert.deepEqual(DELEGATION_OPTIONS.map((item) => item.value), [1, 3, 7, 30, 0]);
  assert.equal(DELEGATION_OPTIONS.at(-1).label, '无限制');
  assert.match(delegationHint(7), /最多继续自动执行 7 天/);
  assert.match(delegationHint(0), /不会自动到期/);
  assert.equal(delegationNotice({ delegation: {} }, 3), '后台授权有效期已改为 3 天。');
  assert.match(delegationNotice({ delegation: { hub: { status: 'issue', message: '合成原因' } } }, 0),
    /已改为 无限制，但后台授权未就绪.*合成原因/);
});

test('toggling tells the user what happened to the background authorization', () => {
  const view = (evaluation) => ({ delegation: { evaluation } });
  assert.match(toggleNotice(view({ status: 'active' }), 'optimize', true, false), /已登记后台授权/);
  assert.match(toggleNotice(view({ status: 'issue', message: '合成原因' }), 'optimize', true, false), /未就绪.*合成原因/);
  assert.match(toggleNotice(view({ status: 'off' }), 'optimize', false, false), /销毁后台授权/);
  const unlinked = toggleNotice(view({ status: 'unlinked' }), 'optimize', true, false);
  assert.match(unlinked, /尚未发布到 Hub.*发布首个正式版本后自动登记/);
  assert.doesNotMatch(unlinked, /请稍后重试/);
  assert.match(toggleNotice(view({ status: 'off' }), 'optimize', false, true), /当前已启动的步骤继续执行/);
});

test('transitions between syncs become notices; the first read stays quiet', () => {
  const before = state({ runs: [run({ id: 'FW-001', status: 'optimizing' }), run({ id: 'FW-000', status: 'publishing' })] });
  const after = state({
    runs: [run({ id: 'FW-002' }), run({ id: 'FW-001', status: 'waiting_publish', publish_hold: true }),
      run({ id: 'FW-000', status: 'published', target_version: '0.1.9' })],
  });
  assert.deepEqual(transitions(null, after), []);
  assert.deepEqual(transitions(before, after), [
    ['info', '已收到评测建议，等待你确认继续。'],
    ['success', '优化完成；请确认是否发布到 Hub。'],
    ['success', 'v0.1.9 已发布到 Hub，完整运行记录已保存。'],
  ]);
  assert.deepEqual(transitions(after, after), []);
});

test('a published version waits for its evaluation, and the records say how it went', () => {
  const waiting = run({ status: 'waiting_evaluation', published_release_id: 'release-b', target_version: '0.1.9',
    evaluation: { status: 'waiting' } });
  assert.deepEqual(statusOf(waiting), ['待确认评测', 'amber']);
  assert.deepEqual(statusOf({ ...waiting, human_hold: false }), ['等待自动评测', 'blue']);
  assert.equal(mode(waiting), '待人工确认');
  assert.equal(mode(run({ status: 'passed' })), '无需优化');
  assert.deepEqual(statusOf(run({ status: 'expired', reason: 'version_mismatch' })), ['已过期', 'gray']);
  assert.equal(mode(run({ status: 'expired', reason: 'version_mismatch' })), '无需处理');
  // 开关关着时“待确认评测”标签已说明一切，不再重复
  assert.equal(evaluationHint(waiting, state()), '');
  assert.equal(evaluationHint({ ...waiting, human_hold: false }, state()), '');
  const on = state({ settings: { optimize: false, publish: true, max_rounds: 3 } });
  assert.match(evaluationHint(waiting, on), /没有可用的后台评测登录/);
  assert.match(evaluationHint({ ...waiting, error: '合成原因' }, on), /自动评测未开始：合成原因/);
  // 图数据需要确认删除时说明原因
  assert.equal(evaluationHint({ ...waiting, error: '合成原因', graph_confirmation: '合成图数据说明' }, on),
    '合成图数据说明。确认评测时一并确认删除。');
  assert.equal(evaluationLine(waiting), '');
  assert.equal(evaluationLine({ ...waiting, evaluation: { status: 'running', mode: 'auto' } }), '已自动发起新版本评测');
  assert.equal(evaluationLine({ ...waiting, evaluation: { status: 'running', mode: 'manual' } }), '已确认发起新版本评测');
  assert.equal(evaluationLine({ ...waiting, evaluation: { status: 'done', passed: false, score: 67 } }), '新版本评测 67%，转入下一轮');
  // 评测得分按结果概览的严格通过率显示，与评测页面一样是整数百分比；此前存下的一位小数也按整数显示
  assert.equal(rate(75), '75%');
  assert.equal(rate(0), '0%');
  assert.equal(rate(66.7), '67%');
  assert.equal(rate(null), '—');
  assert.equal(evaluationLine({ ...waiting, evaluation: { status: 'done', passed: true, score: 100 } }), '新版本评测全部通过');
  assert.equal(evaluationLine({ ...waiting, evaluation: { status: 'skipped' } }), '已跳过新版本评测');
  assert.equal(sourceLine(run({ source_id: 'FW-001', trigger: 'flywheel' })), 'FW-001 发布后自动评测');
  assert.equal(sourceLine(run({ source_id: 'FW-001', trigger: 'evaluation_page' })), 'FW-001 发布后在评测页面评测');
  assert.equal(sourceLine(run({ source_id: 'FW-001', kind: 'rollback' })), '');
});

test('the publication card follows the evaluation of the new version', () => {
  const evaluating = run({ id: 'FW-002', status: 'evaluating', published_release_id: 'release-b' });
  assert.equal(publishFocus([evaluating], 'release-b').kind, 'running');
  const waiting = run({ id: 'FW-002', status: 'waiting_evaluation', published_release_id: 'release-b' });
  assert.deepEqual(publishFocus([waiting], 'release-b'), { kind: 'evaluate', run: waiting });
  // 评测阻断属于新发布的版本，而不是优化所基于的旧版本
  const failed = run({ id: 'FW-002', status: 'blocked', reason: 'evaluation_failed', release_id: 'release-a',
    published_release_id: 'release-b' });
  assert.deepEqual(publishFocus([failed], 'release-b'), { kind: 'blocked', run: failed });
  assert.equal(publishFocus([failed], 'release-a').kind, 'idle');
  assert.equal(evaluateBlock(state(), waiting), '');
  assert.equal(evaluateBlock(state(), failed), '');
  assert.equal(evaluateBlock(state({ can_write: false }), waiting), '只读评审角色不能发起评测');
  assert.equal(evaluateBlock(state({ active_id: 'FW-003' }), waiting), '项目正在执行任务，请等待本次任务完成');
  assert.equal(evaluateBlock(state(), run({ status: 'published' })), '该运行没有待评测的版本');
  assert.equal(canRollback({ ...waiting, snapshot: { iteration: 2 } }), true);
  const passed = run({ id: 'FW-003', status: 'passed' });
  assert.deepEqual(optimizeFocus([passed, run({ id: 'FW-002', status: 'published', changed: 1 })]), { kind: 'passed', run: passed });
});

test('the publication switch says whether evaluations start while the user is away', () => {
  const on = { optimize: true, publish: true, max_rounds: 3, delegation_days: 7 };
  const hub = { status: 'active', expires_at: 1796000000 };
  const missing = state({ settings: on, delegation: { hub, login: 'missing', evaluation: { status: 'active' } } });
  assert.equal(loginMissing(missing), true);
  assert.match(switchWarning(missing, 'publish'), /评测服务未保存后台评测登录/);
  const ready = state({ settings: on, delegation: { hub, login: 'active' } });
  assert.equal(loginMissing(ready), false);
  assert.equal(switchWarning(ready, 'publish'), '');
  const unlinked = state({ settings: on, delegation: { hub: { status: 'unlinked' }, login: 'missing' } });
  assert.equal(loginMissing(unlinked), false);
  assert.match(switchWarning(unlinked, 'publish'), /尚未发布到 Hub/);
  assert.match(toggleNotice({ delegation: { hub, login: 'missing' } }, 'publish', true, false),
    /自动确认发布与发起评测已开启。评测服务未保存后台评测登录/);
  assert.match(toggleNotice({ delegation: { hub, login: 'active' } }, 'publish', true, false), /自动确认发布与发起评测已开启，已登记后台授权/);
  assert.match(reauthorizeNotice({ delegation: { login: 'active' } }), /已重新保存后台评测登录/);
  assert.match(reauthorizeNotice({ delegation: { login: 'missing', evaluation: { message: '合成原因' } } }), /仍未就绪：合成原因/);
});

test('the evaluation loop reports publication, evaluation and the end of the loop', () => {
  const publishing = run({ id: 'FW-001', status: 'publishing', target_version: '0.1.9' });
  const queued = { ...publishing, status: 'waiting_evaluation', human_hold: false };
  const held = { ...publishing, status: 'waiting_evaluation', human_hold: true };
  const evaluating = { ...publishing, status: 'evaluating', evaluation: { status: 'running', mode: 'auto' } };
  const done = (passed) => ({ ...publishing, status: 'published', evaluation: { status: 'done', passed } });
  const views = (...runs) => state({ runs });
  assert.deepEqual(transitions(views(publishing), views(evaluating)), [['success', 'v0.1.9 已发布到 Hub，已自动发起新版本评测。']]);
  assert.deepEqual(transitions(views(publishing), views(held)), [['success', 'v0.1.9 已发布到 Hub；确认后开始评测新版本。']]);
  assert.deepEqual(transitions(views(publishing), views(queued)), [['success', 'v0.1.9 已发布到 Hub，即将自动发起评测。']]);
  assert.deepEqual(transitions(views(queued), views(evaluating)), [['info', '已自动发起新版本评测。']]);
  // 全部通过：只由发布它的运行提示一次
  const passed = run({ id: 'FW-002', status: 'passed', source_id: 'FW-001', trigger: 'flywheel' });
  assert.deepEqual(transitions(views(evaluating), views(passed, done(true))),
    [['success', 'v0.1.9 通过评测集全部用例，数据飞轮循环结束。']]);
  // 仍有用例未通过：由新的运行说明下一轮
  const next = run({ id: 'FW-002', status: 'optimizing', source_id: 'FW-001', trigger: 'flywheel', mode: 'auto' });
  assert.deepEqual(transitions(views(evaluating), views(next, done(false))), [['success', '新版本仍有用例未通过，已自动开始下一轮优化。']]);
  const stopped = { ...evaluating, status: 'blocked', reason: 'evaluation_failed' };
  assert.deepEqual(transitions(views(evaluating), views(stopped)), [['error', '新版本评测未完成，可确认后重新评测。']]);
  assert.deepEqual(transitions(views(), views(run({ status: 'passed' }))), [['success', '评测集全部通过，无需继续优化。']]);
});

test('a run closed by an all-pass result says so', () => {
  const before = state({ runs: [run({ id: 'FW-001', status: 'blocked', reason: 'no_changes' })] });
  const after = state({ runs: [run({ id: 'FW-001', status: 'passed', reason: '' })] });
  assert.deepEqual(transitions(before, after), [['success', 'FW-001 最新评测结果全部通过，无需继续处理。']]);
});

test('a candidate carrying earlier unpublished edits says so', () => {
  const view = state({ package: { release_id: 'release-a', version: '0.1.8' } });
  assert.match(earlierChangesNote(run({ status: 'waiting_publish', earlier_changes: true }), view),
    /上次发布 v0\.1\.8 之后.*一并发布/);
  assert.equal(earlierChangesNote(run({ status: 'waiting_publish', earlier_changes: false }), view), '');
  assert.equal(earlierChangesNote(run({ status: 'waiting_publish', earlier_changes: null }), view), '');
});

test('user feedback runs may concern an earlier version and end at publication', () => {
  const feedback = run({ origin: 'consumer_feedback', release_id: 'release-old', version: '0.1.6' });
  assert.equal(isFeedback(feedback), true);
  assert.equal(isFeedback(run()), false);
  // 旧版本的反馈可以优化：先核对当前版本，仍存在的问题才修改
  assert.equal(optimizeBlock(state(), feedback), '');
  assert.match(feedbackNote(feedback, state()), /反馈针对 v0.1.6，优化时先核对当前版本 v0.1.8/);
  assert.equal(feedbackNote({ ...feedback, release_id: 'release-a' }, state()), '');
  assert.deepEqual(statusOf({ ...feedback, status: 'ignored', reason: 'already_resolved' }), ['最新版本已解决', 'green']);
  assert.equal(evaluationLine({ ...feedback, status: 'published' }), '用户反馈，发布后不评测');
  // 候选内容基于优化开始时的版本：反馈针对旧版本也照常归到当前版本的发布卡片
  const blocked = { ...feedback, status: 'blocked', reason: 'hub_failed', base_release_id: 'release-a' };
  assert.equal(publishFocus([blocked], 'release-a').kind, 'blocked');
});

test('the round limit offers no limit as well', () => {
  assert.deepEqual(ROUND_LIMITS.map(roundsLabel), ['最多 1 轮', '最多 3 轮', '最多 5 轮', '无限次']);
});
