// 数据 Schema 的 Sheet 范围（ADR-0096）：R-16 预检每个 Sheet 的表头，R-03 连同用途一起保存。
// 状态放在流程对象里、不放 React 闭包：确认、重新检查、切项目都靠请求版本号挡住过期的响应，
// 不依赖组件有没有重渲染。

export const SCHEMA_TABLE_TYPES = ['xlsx', 'xlsm', 'csv'];

export function isSchemaTable(name) {
  const ext = String(name || '').split('.').pop().toLowerCase();
  return SCHEMA_TABLE_TYPES.includes(ext);
}

// 列表与详情里的范围摘要（R-01 Material.schema_scope）
export function scopeChip(scope) {
  if (!scope || scope.pending) return null;
  if (scope.error) return { tone: 'amber', text: 'Sheet 范围待确认' };
  return { tone: 'line', text: `数据表 ${scope.included_sheets?.length || 0} · 参考 ${scope.reference_sheets?.length || 0}` };
}

// 预检没有任何问题：不用弹窗，直接按现算的范围保存
export function cleanScope(scope) {
  return !scope?.error && !scope?.pending && (scope?.sheets || []).length > 0
    && scope.sheets.every((sheet) => !(sheet.issues || []).length);
}

const usable = (sheet) => sheet.schema_selectable !== false && sheet.selectable !== false;

export function sheetScopeFlow({ api, set, onSaved = () => {}, onError = () => {} }) {
  let pid = null;
  let version = 0;
  let checking = false;
  let saving = false;
  let editor = null;
  let saveSeq = 0; // 哪一次保存拥有 saving 标记：作废后旧保存的收尾不能清掉新保存的
  const emit = () => set({ editor, checking, saving });
  const current = (p, v) => p === pid && v === version;
  const invalidate = () => { version += 1; saveSeq += 1; checking = false; saving = false; emit(); };

  const save = async (p, m, readingMode, selection) => {
    const r = await api.setResourcePurpose(p, {
      path: m.path, purpose: 'schema', reading_mode: readingMode || 'on_demand',
      ...(selection ? { schema_selection: selection } : {}),
    });
    return r;
  };

  // 预检；saveIfClean 时没问题就直接保存（改用途为数据 Schema 的路径），有问题或出错才打开弹窗
  const check = async (m, { saveIfClean = false, readingMode } = {}) => {
    if (saving) return;
    const p = pid;
    const v = ++version;
    checking = true;
    emit();
    const mode = readingMode || m.reading_mode || 'on_demand';
    try {
      const scope = await api.resourceSchemaPreview(p, m.path);
      if (!current(p, v)) return;
      if (saveIfClean && (scope.pending || cleanScope(scope))) {
        // 还没转出文本时先只改用途，范围按结构现算，转好后在详情里能看
        const r = await save(p, m, mode, scope.pending ? null : { mode: 'auto' });
        if (!current(p, v)) return;
        checking = false;
        emit();
        onSaved(r, scope);
        return;
      }
      editor = { pid: p, material: m, readingMode: mode, scope, selected: (scope.included_sheets || []).filter((name) => {
        const sheet = (scope.sheets || []).find((s) => s.name === name);
        return sheet && usable(sheet);
      }) };
    } catch (e) {
      if (!current(p, v)) return;
      // 改用途时预检失败（格式不对、编码不对）：报错，用途不变；弹窗里重新检查失败：错误留在弹窗里，挡住确认
      if (saveIfClean) onError(e);
      else editor = { pid: p, material: m, readingMode: mode, selected: [], scope: { sheets: [], error: e.message || String(e) } };
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
    choose: (m, readingMode) => check(m, { saveIfClean: true, readingMode }),
    edit: (m) => check(m),
    recheck() {
      if (editor) return check(editor.material, { readingMode: editor.readingMode });
      return undefined;
    },
    toggle(name) {
      if (!editor || checking || saving) return;
      const sheet = (editor.scope.sheets || []).find((s) => s.name === name);
      if (!sheet || !usable(sheet)) return;
      const on = editor.selected.includes(name);
      // 保持文件里的 Sheet 顺序
      const next = new Set(on ? editor.selected.filter((x) => x !== name) : [...editor.selected, name]);
      editor = { ...editor, selected: editor.scope.sheets.map((s) => s.name).filter((x) => next.has(x)) };
      emit();
    },
    cancel() {
      editor = null;
      invalidate();
    },
    canSave: () => !!(editor && !checking && !saving && editor.scope.revision && editor.pid === pid),
    async confirm() {
      if (!editor || checking || saving || !editor.scope.revision || editor.pid !== pid) return;
      const target = editor;
      const mine = ++saveSeq;
      version += 1;
      const v = version;
      const p = pid;
      saving = true;
      emit();
      try {
        const r = await save(p, target.material, target.readingMode, {
          mode: 'manual', included_sheets: target.selected, revision: target.scope.revision,
        });
        if (!current(p, v)) return;
        editor = null;
        onSaved(r, target.scope);
      } catch (e) {
        if (!current(p, v)) return;
        // 保存失败（文件变了、表头有错）：要先重新检查才能再确认
        editor = { ...target, scope: { ...target.scope, error: e.message || String(e), revision: null } };
      } finally {
        if (saveSeq === mine) saving = false;
        emit();
      }
    },
    state: () => ({ editor, checking, saving }),
  };
}
