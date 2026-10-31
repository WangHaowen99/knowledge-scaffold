// 构建进度提醒（lib/buildNotifier.js）：何时提醒、提醒哪种、是否弹系统通知、设置页的可用性提示。
// 跑法：cd modeling/onto-modeling/frontend && npm test
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  alertFaviconHref,
  createBuildAlertTracker,
  flashFrame,
  notificationAvailability,
  planAlert,
} from '../src/lib/buildNotifier.js';

const state = (value, ts) => ({ type: 'session_state', state: value, ts });

// 依次喂入事件，返回每个事件对应的提醒（无提醒为 null）
function feed(tracker, events, options) {
  return events.map((event) => tracker.next(event, options));
}

test('一轮跑完就提醒已完成，不论耗时长短', () => {
  const tracker = createBuildAlertTracker();
  const alerts = feed(tracker, [
    { type: 'user_message', ts: 100 },
    state('running', 101),
    state('idle', 104),
  ]);
  assert.deepEqual(alerts, [null, null, 'done']);
});

test('运行中追加消息不影响结束时的提醒', () => {
  const tracker = createBuildAlertTracker();
  feed(tracker, [
    { type: 'user_message', ts: 100 },
    state('running', 101),
    { type: 'user_message', ts: 150 },
  ]);
  assert.equal(tracker.next(state('idle', 170)), 'done');
});

test('历史事件只用来还原状态，不触发提醒', () => {
  const tracker = createBuildAlertTracker();
  // 构建中途刷新页面：本轮开始在历史里，结束是实时事件
  const history = feed(
    tracker,
    [{ type: 'user_message', ts: 100 }, state('running', 101)],
    { history: true },
  );
  assert.deepEqual(history, [null, null]);
  assert.equal(tracker.next(state('idle', 130)), 'done');

  const replay = createBuildAlertTracker();
  const alerts = feed(
    replay,
    [{ type: 'user_message', ts: 100 }, state('running', 101), state('idle', 500)],
    { history: true },
  );
  assert.deepEqual(alerts, [null, null, null]);
});

test('用户中断的一轮不提醒已完成，下一轮照常提醒', () => {
  const tracker = createBuildAlertTracker();
  const alerts = feed(tracker, [
    { type: 'user_message', ts: 100 },
    state('running', 101),
    { type: 'session_interrupted', ts: 300 },
    state('idle', 301),
    { type: 'user_message', ts: 400 },
    state('running', 401),
    state('idle', 500),
  ]);
  assert.deepEqual(alerts, [null, null, null, null, null, null, 'done']);
});

test('排队中取消、没真正跑过的一轮不提醒', () => {
  const tracker = createBuildAlertTracker();
  feed(tracker, [{ type: 'user_message', ts: 100 }, state('queued', 101)]);
  assert.equal(tracker.next(state('idle', 300)), null);
});

test('历史被截断看不到状态事件时，按服务端报告的运行态判断', () => {
  const tracker = createBuildAlertTracker();
  tracker.seed('running');
  assert.equal(tracker.next(state('idle', 500)), 'done');

  const idle = createBuildAlertTracker();
  idle.seed('idle');
  assert.equal(idle.next(state('idle', 500)), null);
});

test('提问与权限确认分开提醒，同一请求只提醒一次', () => {
  const tracker = createBuildAlertTracker();
  const question = { type: 'permission_request', request_id: 'r1', tool_name: 'AskUserQuestion' };
  const permission = { type: 'permission_request', request_id: 'r2', tool_name: 'Bash' };
  assert.equal(tracker.next(question), 'question');
  assert.equal(tracker.next(question), null);
  assert.equal(tracker.next(permission), 'permission');
});

test('历史里的提问不提醒，重连后再次收到也不提醒', () => {
  const tracker = createBuildAlertTracker();
  const question = { type: 'permission_request', request_id: 'r1', tool_name: 'AskUserQuestion' };
  assert.equal(tracker.next(question, { history: true }), null);
  assert.equal(tracker.next(question), null);
});

test('构建出错提醒，服务重启遗留的 exit code 143 和历史里的错误不提醒', () => {
  const tracker = createBuildAlertTracker();
  feed(tracker, [{ type: 'user_message', ts: 100 }, state('running', 101)]);
  assert.equal(tracker.next({ type: 'session_error', error: '模型调用失败' }), 'error');
  // 出错后状态为 error，不会再提醒已完成
  assert.equal(tracker.next(state('error', 300)), null);

  assert.equal(tracker.next({ type: 'session_error', error: 'Command failed with exit code 143' }), null);
  assert.equal(tracker.next({ type: 'session_error', error: '模型调用失败' }, { history: true }), null);
});

test('本轮以失败的 result 结束（如模型调用失败）时提醒出错，而不是已完成', () => {
  const tracker = createBuildAlertTracker();
  const alerts = feed(tracker, [
    { type: 'user_message', ts: 100 },
    state('running', 101),
    { type: 'result', subtype: 'success', is_error: true },
    state('idle', 110),
  ]);
  assert.deepEqual(alerts, [null, null, null, 'error']);

  const maxTurns = createBuildAlertTracker();
  feed(maxTurns, [{ type: 'user_message', ts: 100 }, state('running', 101)]);
  maxTurns.next({ type: 'result', subtype: 'error_max_turns', is_error: false });
  assert.equal(maxTurns.next(state('idle', 300)), 'error');
});

test('失败只看本轮最后一个 result，下一轮成功照常提醒已完成', () => {
  const tracker = createBuildAlertTracker();
  const alerts = feed(tracker, [
    { type: 'user_message', ts: 100 },
    state('running', 101),
    { type: 'result', subtype: 'success', is_error: true },
    state('idle', 110),
    { type: 'user_message', ts: 200 },
    state('running', 201),
    { type: 'result', subtype: 'success', is_error: false },
    state('idle', 300),
  ]);
  assert.deepEqual(alerts.slice(-1), ['done']);
});

test('闪烁的提醒态：标题加同色圆点与前缀，图标换成提醒色', () => {
  const base = '知识工程建模工作台';
  const cases = [
    ['done', '🟢【已完成】', '#2f9e44'],
    ['question', '🟠【待确认】', '#f08c00'],
    ['permission', '🟠【待确认】', '#f08c00'],
    ['error', '🔴【出错】', '#e03131'],
  ];
  for (const [kind, prefix, color] of cases) {
    const frame = flashFrame(kind, true, base);
    assert.equal(frame.title, prefix + base);
    assert.equal(frame.favicon, alertFaviconHref(color));
  }
});

test('闪烁的原样态：标题与图标都还原', () => {
  assert.deepEqual(flashFrame('error', false, '知识工程建模工作台'), {
    title: '知识工程建模工作台',
    favicon: null,
  });
});

test('提醒图标是带提醒色底的 SVG，可直接作 favicon 地址', () => {
  const href = alertFaviconHref('#e03131');
  assert.match(href, /^data:image\/svg\+xml,/);
  const svg = decodeURIComponent(href.slice('data:image/svg+xml,'.length));
  assert.match(svg, /<rect [^>]*fill="#e03131"/);
  assert.match(svg, />知<\/text><\/svg>$/);
});

test('人在页面上时不弹通知、标题不闪', () => {
  const granted = notificationAvailability({ secure: true, supported: true, permission: 'granted' });
  assert.deepEqual(planAlert({ present: true, enabled: true, availability: granted }), {
    flash: false,
    notify: false,
  });
});

test('人不在页面上：开关开着且已授权才弹系统通知，标题总是闪烁', () => {
  const granted = notificationAvailability({ secure: true, supported: true, permission: 'granted' });
  const insecure = notificationAvailability({ secure: false, supported: true, permission: 'granted' });
  const denied = notificationAvailability({ secure: true, supported: true, permission: 'denied' });
  assert.deepEqual(planAlert({ present: false, enabled: true, availability: granted }), {
    flash: true,
    notify: true,
  });
  assert.deepEqual(planAlert({ present: false, enabled: false, availability: granted }), {
    flash: true,
    notify: false,
  });
  assert.equal(planAlert({ present: false, enabled: true, availability: insecure }).notify, false);
  assert.equal(planAlert({ present: false, enabled: true, availability: denied }).notify, false);
});

test('设置页按 HTTP → 不支持 → 已拒绝 → 未申请 → 已授权 的顺序提示', () => {
  const check = (env) => notificationAvailability(env);
  // HTTP 访问时浏览器连 Notification 都可能不提供，仍提示 HTTP
  assert.deepEqual(check({ secure: false, supported: false }), {
    code: 'insecure',
    text: '当前为 HTTP 访问，无法弹出系统通知，只有标签页标题和图标闪烁。如需系统通知，请联系管理员改用 HTTPS 访问',
  });
  assert.deepEqual(check({ secure: true, supported: false }), {
    code: 'unsupported',
    text: '当前浏览器不支持系统通知',
  });
  assert.deepEqual(check({ secure: true, supported: true, permission: 'denied' }), {
    code: 'denied',
    text: '浏览器已禁止本站通知，需要在浏览器设置里开启',
  });
  assert.deepEqual(check({ secure: true, supported: true, permission: 'default' }), {
    code: 'default',
    text: '首次发起构建时会请求通知权限',
  });
  assert.deepEqual(check({ secure: true, supported: true, permission: 'granted' }), {
    code: 'granted',
    text: '',
  });
});
