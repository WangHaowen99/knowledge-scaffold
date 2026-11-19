// 公共本体页：插件自己的页面，在不同源的 iframe 里跑，只经窄接口（界面接口 1.1）和平台通信，拿不到登录态、发不了请求。
// 握手：往父窗口发一次 {onto_ui, connect: true}，平台转交一个 MessagePort，之后的请求、回应、推送都走它。
// 用到的方法：context.get、theme.get、upload.list / put / delete、artifact.list、chat.command、command.tasks、
// file.download、studio.open；推送：artifact.changed、upload.changed、command.progress、readonly.changed。
// 页面里的文字都来自平台返回的数据，一律用 textContent 写，不拼 HTML（图标是写死的 SVG）。
(function () {
  'use strict';

  var UI_API = '1.1';
  var SLOT = 'inputs';
  var COMMAND = '/common-ontology-extract';
  var RESULT_DIR = 'result';
  var UPLOAD_MAX_BYTES = 50 * 1024 * 1024;
  var MIN_DEFAULT = 2;
  var LIST_PAGES_MAX = 20;
  var ACTIVE = ['queued', 'running', 'waiting_user'];
  var TYPES = {
    status: 'common-ontology.status', real: 'common-ontology.real', term: 'common-ontology.term',
    template: 'common-ontology.template', mapping: 'common-ontology.mapping'
  };
  var KIND = { object: ['对象', 'blue'], operation: ['操作', 'indigo'], rule: ['规则', 'amber'] };
  var CLASS = { real: ['实类', 'teal'], virtual: ['虚类', 'line'], none: ['没抽出', 'line'] };
  var ICONS = {
    layers: '<path d="M12 2 2 7l10 5 10-5z"/><path d="M2 17l10 5 10-5M2 12l10 5 10-5"/>',
    users: '<circle cx="9" cy="8" r="3.5"/><path d="M2 20a7 7 0 0 1 14 0M16 4.5a3.5 3.5 0 0 1 0 7M18 13.5a6 6 0 0 1 4 6.5"/>',
    download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>',
    upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12"/>',
    play: '<path d="M7 4.5v15l12.5-7.5z"/>',
    trash: '<path d="M3 6h18M8 6V4h8v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>',
    chev: '<path d="m9 18 6-6-6-6"/>',
    chevd: '<path d="m6 9 6 6 6-6"/>',
    cube: '<path d="M21 16V8l-9-5-9 5v8l9 5 9-5z"/><path d="M3.3 7 12 12l8.7-5M12 22V12"/>',
    book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5z"/><path d="M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5"/>',
    sliders: '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
    fork: '<circle cx="6" cy="5" r="2.5"/><circle cx="18" cy="5" r="2.5"/><circle cx="12" cy="19" r="2.5"/><path d="M6 7.5v2a3 3 0 0 0 3 3h6a3 3 0 0 0 3-3v-2M12 12.5v4"/>'
  };
  var TABS = [
    { id: 'real', label: '实类', icon: 'cube' },
    { id: 'term', label: '术语表', icon: 'book' },
    { id: 'template', label: '规则模板', icon: 'sliders' },
    { id: 'mapping', label: '继承映射', icon: 'fork' }
  ];

  // ---------------------------------------------------------------- 窄接口

  var port = null;
  var queue = [];
  var pending = {};
  var seq = 0;

  function call(method, params, transfer) {
    seq += 1;
    var id = 'c' + seq;
    return new Promise(function (resolve, reject) {
      pending[id] = { resolve: resolve, reject: reject };
      var msg = { onto_ui: UI_API, id: id, method: method, params: params || {} };
      if (port) port.postMessage(msg, transfer || []); else queue.push([msg, transfer || []]);
    });
  }

  function onPortMessage(event) {
    var msg = event.data || {};
    if (msg.event) { onEvent(msg.event, msg.data || {}); return; }
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
    queue.splice(0).forEach(function (item) { port.postMessage(item[0], item[1]); });
  });
  window.parent.postMessage({ onto_ui: UI_API, connect: true }, '*');

  // ---------------------------------------------------------------- 状态

  var state = {
    uploads: [], status: null, real: [], term: [], template: [], mapping: [], totals: {},
    tab: 'real', realKind: 'all', mapClass: 'all', mapPack: 'all',
    min: null, running: false, taskId: null, startedAt: 0, progress: false, uploading: 0, readonly: false,
    loaded: false, arming: null
  };

  // ---------------------------------------------------------------- DOM 小件

  function $(id) { return document.getElementById(id); }

  function icon(name) {
    var span = document.createElement('span');
    span.className = 'ic';
    span.innerHTML = '<svg class="i" viewBox="0 0 24 24" aria-hidden="true">' + (ICONS[name] || '') + '</svg>';
    return span;
  }

  function h(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (key) {
      var value = attrs[key];
      if (value == null || value === false) return;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key.slice(0, 2) === 'on') node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? '' : value);
    });
    (children || []).forEach(function (child) {
      if (child == null || child === false) return;
      node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    });
    return node;
  }

  function chip(text, tone) { return h('span', { class: 'chip ' + (tone || ''), text: text }); }

  function fill(node, children) {
    while (node.firstChild) node.removeChild(node.firstChild);
    children.forEach(function (child) { if (child) node.appendChild(child); });
  }

  function emptyRow(cols, text) {
    return h('tr', { class: 'empty' }, [h('td', { colspan: cols, text: text || '暂无' })]);
  }

  function moreRow(cols, shown, total) {
    return total > shown ? h('tr', { class: 'more' }, [h('td', { colspan: cols, text: '还有 ' + (total - shown) + ' 项未显示' })]) : null;
  }

  function hydrateIcons(root) {
    Array.prototype.forEach.call(root.querySelectorAll('[data-icon]'), function (node) {
      if (!node.firstChild) node.appendChild(icon(node.getAttribute('data-icon')).firstChild);
    });
  }

  var toastTimer = null;
  function toast(text, isError) {
    var node = $('toast');
    node.textContent = text;
    node.className = 'show' + (isError ? ' err' : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { node.className = ''; }, isError ? 5000 : 2500);
  }

  function errorText(error) {
    return (error && error.message) || (error && error.code) || '操作失败';
  }

  function timeText(value) {
    if (value == null || value === '') return '';
    var date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
    if (isNaN(date.getTime())) return String(value);
    var pad = function (n) { return (n < 10 ? '0' : '') + n; };
    return pad(date.getMonth() + 1) + '-' + pad(date.getDate()) + ' ' + pad(date.getHours()) + ':' + pad(date.getMinutes());
  }

  function list(value) { return Array.isArray(value) ? value : []; }

  // [[条目]] → 条目：页面上只显示名字
  function plainLinks(text) { return String(text || '').replace(/\[\[([^\]|]+)(?:\|[^\]]*)?\]\]/g, '$1'); }

  // 按上传先后排（平台给的是新的在前）；映射、来源都按这个顺序
  function packOrder(packId) {
    for (var i = 0; i < state.uploads.length; i += 1) if (state.uploads[i].id === packId) return i;
    return state.uploads.length;
  }

  function byTime(a, b) { return String(a.uploaded_at || '').localeCompare(String(b.uploaded_at || '')); }

  // ---------------------------------------------------------------- 读取

  // AR-02 一页最多 50 条：有下一页就带游标接着取；平台不认游标时取回来的是同一页，没有新条目就停
  function listAll(type) {
    var items = [];
    var seen = {};
    var total = 0;
    function page(cursor, count) {
      var params = { type: type };
      if (cursor) params.cursor = cursor;
      return call('artifact.list', params).then(function (res) {
        res = res || {};
        total = typeof res.total === 'number' ? res.total : total;
        var fresh = list(res.items).filter(function (item) { return !seen[item.key]; });
        fresh.forEach(function (item) { seen[item.key] = true; items.push(item); });
        if (res.next_cursor && fresh.length && count < LIST_PAGES_MAX) return page(res.next_cursor, count + 1);
        return { items: items, total: Math.max(total, items.length) };
      });
    }
    return page(null, 1).catch(function () { return { items: [], total: 0 }; });
  }

  function loadUploads() {
    return call('upload.list', { slot: SLOT }).then(function (res) {
      state.uploads = list(res && res.items).slice().sort(byTime);
    }).catch(function (error) { toast(errorText(error), true); }).then(render);
  }

  var resultTimer = null;
  function loadResults() {
    return Promise.all(Object.keys(TYPES).map(function (name) {
      return listAll(TYPES[name]).then(function (res) { return [name, res]; });
    })).then(function (pairs) {
      pairs.forEach(function (pair) {
        var name = pair[0], res = pair[1];
        if (name === 'status') state.status = res.items[0] ? res.items[0].fields || {} : null;
        else { state[name] = res.items; state.totals[name] = res.total; }
      });
      if (state.min == null && state.status && state.status.min) state.min = state.status.min;
      // 平台不推命令进度时的兜底：开始抽取之后核对结果变了，说明这一轮已经合入
      if (state.running && !state.progress && state.status && Date.parse(state.status.checked_at) > state.startedAt - 5000) {
        state.running = false;
      }
      state.loaded = true;
      render();
    });
  }

  function scheduleResults() {
    clearTimeout(resultTimer);
    resultTimer = setTimeout(loadResults, 300);
  }

  function loadTasks() {
    return call('command.tasks', {}).then(function (res) {
      state.progress = true;
      var active = list(res && res.items).filter(function (task) { return ACTIVE.indexOf(task.state) >= 0; });
      if (active.length) { state.running = true; state.taskId = active[0].task_id; render(); }
    }).catch(function () { /* 平台没有这个方法时不显示恢复的进行中 */ });
  }

  function onEvent(name, data) {
    if (name === 'upload.changed' && (!data.slot || data.slot === SLOT)) loadUploads();
    else if (name === 'artifact.changed') scheduleResults();
    else if (name === 'readonly.changed') { state.readonly = !!data.readonly; render(); }
    else if (name === 'command.progress') {
      state.progress = true;
      if (ACTIVE.indexOf(data.state) >= 0) {
        state.running = true;
        state.taskId = data.task_id || state.taskId;
      } else if (!state.taskId || data.task_id === state.taskId) {
        state.running = false;
        state.taskId = null;
        if (data.state === 'failed') toast('抽取没有完成', true);
        scheduleResults();
      }
      render();
    }
  }

  // ---------------------------------------------------------------- 操作

  function packCount() { return state.uploads.length; }

  function currentMin() {
    var max = Math.max(MIN_DEFAULT, packCount());
    var value = state.min || MIN_DEFAULT;
    return Math.min(Math.max(MIN_DEFAULT, value), max);
  }

  function extract() {
    var min = currentMin();
    state.running = true;
    state.startedAt = Date.now();
    render();
    call('chat.command', { command: COMMAND, args: { min: min } }).then(function (res) {
      state.taskId = res && res.task && res.task.task_id || null;
      if (res && res.task && ACTIVE.indexOf(res.task.state) < 0 && res.task.state) state.running = false;
      render();
    }).catch(function (error) {
      state.running = false;
      render();
      toast(errorText(error), true);
    });
  }

  function download() {
    call('file.download', { path: RESULT_DIR }).catch(function (error) { toast(errorText(error), true); });
  }

  function readBuffer(file) {
    if (file.arrayBuffer) return file.arrayBuffer();
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () { resolve(reader.result); };
      reader.onerror = function () { reject(reader.error); };
      reader.readAsArrayBuffer(file);
    });
  }

  function uploadFiles(files) {
    var queueFiles = Array.prototype.slice.call(files || []);
    if (!queueFiles.length || state.readonly) return;
    var next = function () {
      var file = queueFiles.shift();
      if (!file) return loadUploads();
      if (file.size > UPLOAD_MAX_BYTES) { toast(file.name + '：超过 50 MB', true); return next(); }
      state.uploading += 1;
      render();
      return readBuffer(file).then(function (buffer) {
        return call('upload.put', { slot: SLOT, name: file.name, data: buffer }, [buffer]);
      }).then(function (item) {
        var name = item && item.package && item.package.name;
        toast('已上传 ' + (name || file.name));
      }).catch(function (error) {
        toast(file.name + '：' + errorText(error), true);
      }).then(function () {
        state.uploading -= 1;
        return next();
      });
    };
    next();
  }

  function removeUpload(item) {
    if (state.arming !== item.id) {
      state.arming = item.id;
      render();
      setTimeout(function () { if (state.arming === item.id) { state.arming = null; render(); } }, 3000);
      return;
    }
    state.arming = null;
    call('upload.delete', { slot: SLOT, id: item.id }).then(loadUploads).catch(function (error) {
      toast(errorText(error), true);
      render();
    });
  }

  function openEntry(fields) {
    if (!fields || !fields.entry || !KIND[fields.kind]) return;
    call('studio.open', { kind: fields.kind, key: fields.entry }).catch(function (error) { toast(errorText(error), true); });
  }

  // ---------------------------------------------------------------- 渲染

  function renderHeader() {
    var count = packCount();
    var select = $('min');
    var max = Math.max(MIN_DEFAULT, count);
    var min = currentMin();
    var options = [];
    for (var n = MIN_DEFAULT; n <= max; n += 1) {
      options.push(h('option', { value: String(n), text: '至少 ' + n + ' 个包共有', selected: n === min }));
    }
    fill(select, options);
    select.value = String(min);
    select.disabled = state.running;
    $('min-wrap').className = 'sel' + (state.running ? ' off' : '');

    var button = $('extract');
    var blocked = state.running || state.readonly || state.uploading > 0 || count < MIN_DEFAULT || count < min;
    button.disabled = blocked;
    button.className = 'btn primary' + (state.running ? ' busy' : '');
    button.title = count < MIN_DEFAULT ? '至少上传 2 个知识包' : '';
    fill($('extract-ic'), [state.running ? h('span', { class: 'spin' }) : icon('play').firstChild]);
    $('extract-label').textContent = state.running ? '抽取中' : '抽取公共本体';
    $('download').disabled = !state.status;
  }

  function renderUploads() {
    $('upload-count').textContent = packCount() ? String(packCount()) : '';
    var uploadButton = $('upload');
    uploadButton.disabled = state.readonly || state.uploading > 0;
    fill($('upload-label'), [document.createTextNode(state.uploading ? '上传中' : '上传 ZIP')]);
    var rows = state.uploads.map(function (item) {
      var pkg = item.package || {};
      var counts = item.counts || {};
      var armed = state.arming === item.id;
      var del = h('button', {
        class: armed ? 'btn sm danger' : 'btn icon', type: 'button', 'aria-label': '删除',
        title: armed ? '' : '删除', disabled: state.readonly || state.running,
        onclick: function () { removeUpload(item); }
      }, [armed ? '删除' : icon('trash')]);
      return h('tr', {}, [
        h('td', { class: 'b', text: pkg.name || item.filename || item.id, title: pkg.id || '' }),
        h('td', { class: 'muted num', text: pkg.version ? 'v' + pkg.version : '' }),
        h('td', { class: 'num', text: String(counts.objects || 0) }),
        h('td', { class: 'num', text: String(counts.operations || 0) }),
        h('td', { class: 'num', text: String(counts.rules || 0) }),
        h('td', { class: 'muted num', text: timeText(item.uploaded_at) }),
        h('td', { class: 'act' }, [del])
      ]);
    });
    fill($('upload-rows'), rows.length ? rows : [emptyRow(7)]);
  }

  function tabCount(id) {
    if (id === 'mapping') return mappingRows().length;
    return state.totals[id] != null ? state.totals[id] : state[id].length;
  }

  function renderTabs() {
    fill($('tabs'), TABS.map(function (tab) {
      return h('button', {
        class: 'tab' + (state.tab === tab.id ? ' on' : ''), type: 'button', role: 'tab',
        'aria-selected': state.tab === tab.id ? 'true' : 'false',
        onclick: function () { state.tab = tab.id; render(); }
      }, [icon(tab.icon), tab.label, h('span', { class: 'n', text: String(tabCount(tab.id)) })]);
    }));
    var status = state.status;
    fill($('check-state'), [status && status.state === 'failed'
      ? h('span', { class: 'chip red', title: list(status.problems).slice(0, 5).join('\n'), text: '核对没通过 ' + (status.errors || '') })
      : null]);
  }

  function seg(options, value, onPick) {
    return h('div', { class: 'seg', role: 'group' }, options.map(function (option) {
      return h('button', {
        type: 'button', class: option[0] === value ? 'on' : '', text: option[1],
        onclick: function () { onPick(option[0]); }
      });
    }));
  }

  function table(cols, head, rows) {
    return h('div', { class: 'tbl-wrap' }, [h('table', { class: 'tbl' }, [
      h('colgroup', {}, cols.map(function (cls) { return h('col', { class: cls || null }); })),
      h('thead', {}, [h('tr', {}, head.map(function (title) { return h('th', { text: title }); }))]),
      h('tbody', {}, rows)
    ])]);
  }

  function renderReal() {
    var kinds = Object.keys(KIND);
    var items = state.real.filter(function (item) {
      return state.realKind === 'all' || (item.fields || {}).kind === state.realKind;
    }).sort(function (a, b) {
      var x = a.fields || {}, y = b.fields || {};
      return (kinds.indexOf(x.kind) - kinds.indexOf(y.kind)) || ((y.source_count || 0) - (x.source_count || 0))
        || String(x.title).localeCompare(String(y.title), 'zh');
    });
    var rows = items.map(function (item) {
      var f = item.fields || {};
      var kind = KIND[f.kind] || [f.kind || '', ''];
      return h('tr', {
        class: 'go', tabindex: '0', title: '在 Studio 打开',
        onclick: function () { openEntry(f); },
        onkeydown: function (event) { if (event.key === 'Enter') openEntry(f); }
      }, [
        h('td', { class: 'b', text: f.title || item.title }),
        h('td', {}, [chip(kind[0], 'line')]),
        h('td', { text: f.summary || '—' }),
        h('td', { class: 'muted', text: list(f.sources).join('、') }),
        h('td', { class: 'end' }, [icon('chev')])
      ]);
    });
    if (!rows.length) rows.push(emptyRow(5));
    var more = state.realKind === 'all' ? moreRow(5, state.real.length, state.totals.real || 0) : null;
    if (more) rows.push(more);
    return [
      h('div', { class: 'bar' }, [seg([['all', '全部'], ['object', '对象'], ['operation', '操作'], ['rule', '规则']], state.realKind,
        function (value) { state.realKind = value; render(); })]),
      table(['w-name', 'w-kind', null, null, 'w-go'], ['名称', '类型', '内容', '来源知识包', ''], rows)
    ];
  }

  function renderTerms() {
    var rows = state.term.map(function (item) {
      var f = item.fields || {};
      return h('tr', {}, [
        h('td', { class: 'b', text: f.title || item.title }),
        h('td', { text: f.definition || '' }),
        h('td', {}, list(f.names).map(function (name) {
          return h('span', { class: 'ln' }, [h('span', { class: 'pk', text: name.pack_name || name.pack || '' }), name.name || '']);
        }))
      ]);
    });
    if (!rows.length) rows.push(emptyRow(3));
    var more = moreRow(3, state.term.length, state.totals.term || 0);
    if (more) rows.push(more);
    return [table(['w-pack', null, null], ['术语', '定义', '各包叫法'], rows)];
  }

  function renderTemplates() {
    var rows = state.template.map(function (item) {
      var f = item.fields || {};
      return h('tr', {}, [
        h('td', { class: 'b' }, [f.title || item.title, h('span', { class: 'sub', text: plainLinks(f.template) })]),
        h('td', {}, [h('div', { class: 'chips' }, list(f.params).map(function (param) {
          var node = chip(param.name || '', 'line');
          if (param.meaning) node.title = param.meaning;
          return node;
        }))]),
        h('td', {}, list(f.instances).map(function (inst) {
          return h('span', { class: 'ln' }, [
            h('span', { class: 'pk', text: inst.pack_name || inst.pack || '' }),
            (inst.rule || '') + (inst.values_text ? '（' + inst.values_text + '）' : '')
          ]);
        }))
      ]);
    });
    if (!rows.length) rows.push(emptyRow(3));
    var more = moreRow(3, state.template.length, state.totals.template || 0);
    if (more) rows.push(more);
    return [table([null, 'w-pack', null], ['模板', '参数', '各包实例'], rows)];
  }

  function mappingRows() {
    var out = [];
    state.mapping.forEach(function (item) {
      var f = item.fields || {};
      list(f.rows).forEach(function (row) {
        out.push({ pack: f.pack || item.key, packName: f.title || item.title, row: row });
      });
    });
    return out;
  }

  function renderMapping() {
    var rows = mappingRows().filter(function (entry) {
      return (state.mapClass === 'all' || entry.row['class'] === state.mapClass)
        && (state.mapPack === 'all' || entry.pack === state.mapPack);
    });
    var order = { real: 0, virtual: 1, none: 2 };
    var kindOrder = { object: 0, operation: 1, rule: 2 };
    var groups = {};
    var keys = [];
    rows.forEach(function (entry) {
      var row = entry.row;
      var key = row['class'] === 'none' ? 'none' : row['class'] + '|' + row.kind + '|' + row.common;
      if (!groups[key]) { groups[key] = { key: key, cls: row['class'], kind: row.kind, name: row.common, items: [] }; keys.push(key); }
      groups[key].items.push(entry);
    });
    keys.forEach(function (key) {
      groups[key].items.sort(function (a, b) { return packOrder(a.pack) - packOrder(b.pack); });
    });
    keys.sort(function (a, b) {
      var x = groups[a], y = groups[b];
      return (order[x.cls] - order[y.cls]) || ((kindOrder[x.kind] || 0) - (kindOrder[y.kind] || 0))
        || String(x.name).localeCompare(String(y.name), 'zh');
    });
    var body = [];
    keys.forEach(function (key) {
      var group = groups[key];
      var head = group.cls === 'none'
        ? [h('span', { text: '没抽出' })]
        : [h('span', { text: group.name }), chip((KIND[group.kind] || [''])[0], 'line'), chip(CLASS[group.cls][0], CLASS[group.cls][1])];
      head.push(h('span', { class: 'cnt', text: String(group.items.length) }));
      body.push(h('tr', { class: 'grp' }, [h('td', { colspan: 3 }, [h('div', { class: 'row' }, head)])]));
      group.items.forEach(function (entry) {
        var row = entry.row;
        var detail = group.cls === 'none' ? (row.note || row.extra || '—') : (row.extra || '—');
        body.push(h('tr', {}, [
          h('td', { class: 'muted', text: entry.packName }),
          h('td', {}, group.cls === 'none' ? [row.entry + ' ', chip((KIND[row.kind] || [''])[0], 'line')] : [row.entry]),
          h('td', { class: detail === '—' ? 'muted' : '', text: detail })
        ]));
      });
    });
    if (!body.length) body.push(emptyRow(3));
    var packs = [['all', '全部知识包']].concat(state.mapping.slice().sort(function (a, b) {
      return packOrder((a.fields || {}).pack || a.key) - packOrder((b.fields || {}).pack || b.key);
    }).map(function (item) {
      var f = item.fields || {};
      return [f.pack || item.key, f.title || item.title];
    }));
    var packSelect = h('select', { 'aria-label': '知识包', onchange: function (event) { state.mapPack = event.target.value; render(); } },
      packs.map(function (pack) { return h('option', { value: pack[0], text: pack[1], selected: pack[0] === state.mapPack }); }));
    return [
      h('div', { class: 'bar' }, [
        seg([['all', '全部'], ['real', '实类'], ['virtual', '虚类'], ['none', '没抽出']], state.mapClass,
          function (value) { state.mapClass = value; render(); }),
        h('label', { class: 'sel' }, [packSelect, h('span', { class: 'sel-chev' }, [icon('chevd')])])
      ]),
      table(['w-pack', 'w-pack', null], ['知识包', '条目', '多出的部分'], body)
    ];
  }

  function renderPanel() {
    var views = { real: renderReal, term: renderTerms, template: renderTemplates, mapping: renderMapping };
    fill($('panel'), views[state.tab]());
  }

  function render() {
    renderHeader();
    renderUploads();
    renderTabs();
    renderPanel();
  }

  // ---------------------------------------------------------------- 启动

  function applyTheme(theme) {
    var vars = (theme && theme.vars) || {};
    Object.keys(vars).forEach(function (name) {
      if (/^--[a-z0-9-]+$/.test(name)) document.documentElement.style.setProperty(name, String(vars[name]));
    });
  }

  function bind() {
    hydrateIcons(document);
    $('min').addEventListener('change', function (event) { state.min = parseInt(event.target.value, 10) || MIN_DEFAULT; render(); });
    $('extract').addEventListener('click', extract);
    $('download').addEventListener('click', download);
    $('upload').addEventListener('click', function () { $('file').click(); });
    $('file').addEventListener('change', function (event) {
      var files = event.target.files;
      uploadFiles(files);
      event.target.value = '';
    });
    var card = $('inputs');
    card.addEventListener('dragover', function (event) {
      if (state.readonly) return;
      event.preventDefault();
      card.classList.add('drop');
    });
    card.addEventListener('dragleave', function () { card.classList.remove('drop'); });
    card.addEventListener('drop', function (event) {
      event.preventDefault();
      card.classList.remove('drop');
      uploadFiles(event.dataTransfer && event.dataTransfer.files);
    });
  }

  bind();
  render();
  call('context.get', {}).then(function (ctx) {
    state.readonly = !!(ctx && ctx.readonly);
  }).catch(function () {});
  call('theme.get', {}).then(applyTheme).catch(function () {});
  loadUploads();
  loadResults();
  loadTasks();
}());
