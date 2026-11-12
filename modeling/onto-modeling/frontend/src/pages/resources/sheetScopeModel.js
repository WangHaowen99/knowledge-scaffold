// 数据 Schema 的 Sheet 范围（ADR-0096，行为同 develop LeftPane 的 openSheetScope / saveSheetScope）：
// R-16 预检每个 Sheet 的表头，R-03 连同用途一起保存。状态放在流程对象里、不放 React 闭包：确认、重新检查、
// 切项目都靠请求版本号挡住过期的响应，不依赖组件有没有重渲染。

export const SCHEMA_TABLE_TYPES = ['xlsx', 'xlsm', 'csv'];

export function isSchemaTable(name) {
  const ext = String(name || '').split('.').pop().toLowerCase();
  return SCHEMA_TABLE_TYPES.includes(ext);
}

// 资料行上的范围链接（develop：数据表 N · 参考 M · 查看范围 / Sheet 范围待确认 · 查看范围）；还没转出文本版时不出
export function scopeLink(scope) {
  if (!scope || scope.pending) return null;
  return {
    danger: Boolean(scope.error),
    text: `${scope.error ? 'Sheet 范围待确认' : `数据表 ${scope.included_sheets?.length || 0} · 参考 ${scope.reference_sheets?.length || 0}`} · 查看范围`,
  };
}

const blocked = (sheet) => sheet.schema_selectable === false || sheet.selectable === false;

export function sheetScopeFlow({ api, set, onSaved = () => {} }) {
  let pid = null;
  let version = 0;
  let checking = false;
  let saving = false;
  let editor = null;
  let saveSeq = 0; // 哪一次保存拥有 saving 标记：作废后旧保存的收尾不能清掉新保存的
  const emit = () => set({ editor, checking, saving });
  const current = (p, v) => p === pid && v === version;
  const invalidate = () => { version += 1; saveSeq += 1; checking = false; saving = false; emit(); };
  const save = (p, m, readingMode, selection) => api.setResourcePurpose(p, {
    path: m.path, purpose: 'schema', reading_mode: readingMode || 'on_demand', schema_selection: selection,
  });

  // 预检；saveIfValid 时每个 Sheet 都没有问题就直接按建议范围保存（改用途为数据 Schema 的路径），否则打开弹窗
  const check = async (m, readingMode, saveIfValid = false, force = false) => {
    if (saving) return;
    const p = pid;
    const v = ++version;
    checking = true;
    emit();
    try {
      const scope = await api.resourceSchemaPreview(p, m.path, force);
      if (!current(p, v)) return;
      if (saveIfValid && !scope.error && scope.sheets.length > 0
          && scope.sheets.every((sheet) => !sheet.issues?.length)) {
        const r = await save(p, m, readingMode, {
          mode: 'manual', included_sheets: scope.included_sheets, file_sha256: scope.file_sha256,
        });
        if (!current(p, v)) return;
        onSaved(r, { quick: true, count: scope.included_sheets.length });
        return;
      }
      editor = { pid: p, material: m, readingMode, scope, selected: scope.included_sheets || [] };
    } catch (error) {
      if (!current(p, v)) return;
      editor = { pid: p, material: m, readingMode, selected: [], scope: { sheets: [], error: error.message || String(error) } };
    } finally {
      if (current(p, v)) { checking = false; emit(); }
    }
  };

  return {
    setProject(next) {
      if (next === pid) return;
      pid = next;
      editor = null;
      invalidate();
    },
    choose: (m, readingMode) => check(m, readingMode || m.reading_mode, true),
    // 「查看范围」：直接用资料列表里的范围打开，有错误的 Sheet 不预选
    open(m) {
      const scope = m.schema_scope;
      if (!scope) return;
      invalidate();
      const invalid = new Set((scope.sheets || []).filter(blocked).map((sheet) => sheet.name));
      editor = { pid, material: m, readingMode: m.reading_mode, scope,
        selected: (scope.included_sheets || []).filter((name) => !invalid.has(name)) };
      emit();
    },
    recheck() {
      if (editor) return check(editor.material, editor.readingMode, false, true);
      return undefined;
    },
    toggle(name) {
      if (!editor || checking || saving) return;
      const sheet = (editor.scope.sheets || []).find((s) => s.name === name);
      if (!sheet || blocked(sheet)) return;
      const next = new Set(editor.selected.includes(name) ? editor.selected.filter((x) => x !== name) : [...editor.selected, name]);
      editor = { ...editor, selected: editor.scope.sheets.map((s) => s.name).filter((x) => next.has(x)) };
      emit();
    },
    cancel() {
      editor = null;
      invalidate();
    },
    canSave: () => !!(editor && !checking && !saving && editor.scope.file_sha256 && editor.pid === pid),
    async confirm() {
      if (!editor || saving || checking || !editor.scope.file_sha256 || editor.pid !== pid) return;
      const target = editor;
      invalidate();
      const v = version;
      const p = pid;
      const mine = saveSeq;
      saving = true;
      emit();
      try {
        const r = await save(p, target.material, target.readingMode, {
          mode: 'manual', included_sheets: target.selected, file_sha256: target.scope.file_sha256,
        });
        if (!current(p, v)) return;
        editor = null;
        onSaved(r, { quick: false, count: target.selected.length });
      } catch (error) {
        if (!current(p, v)) return;
        editor = { ...target, scope: { ...target.scope, error: error.message || String(error), file_sha256: null } };
      } finally {
        if (saveSeq === mine) saving = false;
        emit();
      }
    },
    state: () => ({ editor, checking, saving }),
  };
}
