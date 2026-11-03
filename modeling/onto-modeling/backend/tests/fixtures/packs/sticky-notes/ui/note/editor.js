// 合成样例编辑器：只经窄接口（界面接口 1.0）和平台通信，拿不到登录态，也发不了请求。
// 先往父窗口握手，平台转交一个 MessagePort，之后的请求都走它（页面一旦导航走，端口就失效）。
(function () {
  var UI_API = '1.0';
  var pending = {};
  var seq = 0;
  var current = null;
  var port = null;
  var queue = [];

  function call(method, params) {
    seq += 1;
    var id = 'r' + seq;
    return new Promise(function (resolve, reject) {
      pending[id] = { resolve: resolve, reject: reject };
      var msg = { onto_ui: UI_API, id: id, method: method, params: params || {} };
      if (port) port.postMessage(msg); else queue.push(msg);
    });
  }

  function onPortMessage(event) {
    var msg = event.data || {};
    if (msg.event === 'artifact.changed') { load(); return; }
    if (msg.event === 'readonly.changed') { show('state', msg.data && msg.data.readonly ? '只读' : ''); return; }
    var waiter = msg.id && pending[msg.id];
    if (!waiter) return;
    delete pending[msg.id];
    if (msg.ok) waiter.resolve(msg.result); else waiter.reject(msg.error || {});
  }

  window.addEventListener('message', function (event) {
    if (event.source !== window.parent || port) return;
    var msg = event.data || {};
    if (msg.onto_ui == null || msg.event !== 'connected' || !event.ports || !event.ports[0]) return;
    port = event.ports[0];
    port.onmessage = onPortMessage;
    queue.splice(0).forEach(function (m) { port.postMessage(m); });
  });
  window.parent.postMessage({ onto_ui: UI_API, connect: true }, '*');

  function show(id, text) { document.getElementById(id).textContent = text; }

  // 自检：读平台的 localStorage、直接调接口，两样都应该做不到
  function probe() {
    var storage;
    try { storage = 'storage:' + Object.keys(window.localStorage).length; } catch (err) { storage = 'storage:blocked'; }
    var el = document.getElementById('probe');
    el.dataset.probe = storage;
    try {
      fetch('/api/v1/projects').then(function () { el.dataset.fetch = 'fetch:allowed'; })
        .catch(function () { el.dataset.fetch = 'fetch:blocked'; });
    } catch (err) { el.dataset.fetch = 'fetch:blocked'; }
    show('probe', storage);
  }

  function load() {
    return call('artifact.read', {}).then(function (artifact) {
      current = artifact;
      show('title', artifact.title);
      document.getElementById('body').value = (artifact.sections && artifact.sections['正文']) || '';
      show('state', artifact.locked ? '只读' : '');
    }).catch(function (error) { show('state', error.code || 'error'); });
  }

  document.getElementById('save').addEventListener('click', function () {
    if (!current) return;
    call('artifact.save', {
      base_revision: current.revision,
      sections: { '正文': document.getElementById('body').value },
      summary: '在便签编辑器里修改',
    }).then(function (result) {
      current = result.artifact;
      show('state', 'saved ' + (result.version_label || ''));
    }).catch(function (error) { show('state', error.code || 'error'); });
  });

  document.getElementById('tidy').addEventListener('click', function () {
    call('chat.command', { command: '/sticky-notes-tidy', text: '整理便签' })
      .then(function () { show('state', 'sent'); })
      .catch(function (error) { show('state', error.code || 'error'); });
  });

  // 给端到端测试用：在 iframe 里直接发窄接口调用
  window.stickyNotes = { call: call };

  call('context.get', {}).then(function () { return call('theme.get', {}); }).then(function (theme) {
    Object.keys(theme.vars || {}).forEach(function (name) { document.body.style.setProperty(name, theme.vars[name]); });
  }).catch(function () {});
  probe();
  load();
}());
