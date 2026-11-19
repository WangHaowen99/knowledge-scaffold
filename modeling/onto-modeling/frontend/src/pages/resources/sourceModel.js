// 数据源（ADR-0103，契约 3.19a DS-01～DS-04）的纯逻辑：连接表单的初值、校验、拆成 config / secrets，
// 服务端错误标到字段，测试连接的结果，数据源资料上能做什么。页面（SourceDrawer.jsx、ResourcesPage.jsx）只负责画。

export const isSource = (m) => m?.kind === 'source';

// 资料行、预览里文本版状态的叫法：数据源的「转换」就是采集
export const SOURCE_STATE_TEXT = {
  converting: '采集中',
  waiting: '等待采集',
  failed: '采集失败',
};

// 资料名的写法同上传文件名（后端按文件名规则再校验一次）：不能带路径分隔符、不能以点开头
const BAD_NAME = /[\\/:*?"<>|]|^\.|\.\./;
export const nameError = (name) => {
  const s = String(name || '').trim();
  if (!s) return 'required';
  if (BAD_NAME.test(s) || s.length > 80) return 'invalid';
  return null;
};

const isNumeric = (type) => type === 'port' || type === 'number';

/**
 * 表单初值：接入时用字段的 default；修改时用资料里的 config（没有的键再用 default）。
 * secret 一律留空：修改时空着就是不改。
 */
export function initialValues(fields = [], config = null) {
  const out = {};
  fields.forEach((f) => {
    if (f.type === 'secret') { out[f.key] = ''; return; }
    const v = config && config[f.key] != null ? config[f.key] : f.default;
    out[f.key] = v == null ? '' : String(v);
  });
  return out;
}

/**
 * 本地校验，返回 {字段键: 'required'|'invalid'}（名称的键是 name）。
 * 修改时（editing）secret 可以空着；数字类要是整数，端口在 1～65535。
 */
export function validateSource({ name, fields = [], values = {}, editing = false }) {
  const errors = {};
  if (!editing) {
    const n = nameError(name);
    if (n) errors.name = n;
  }
  fields.forEach((f) => {
    const raw = String(values[f.key] ?? '').trim();
    if (!raw) {
      if (f.required && !(editing && f.type === 'secret')) errors[f.key] = 'required';
      return;
    }
    if (isNumeric(f.type)) {
      const n = Number(raw);
      if (!Number.isInteger(n) || (f.type === 'port' && (n < 1 || n > 65535))) errors[f.key] = 'invalid';
    }
    if (f.type === 'select' && !(f.options || []).some((o) => String(o.value) === raw)) errors[f.key] = 'invalid';
  });
  return errors;
}

/**
 * 表单值 → 请求体的 config 与 secrets（契约 DS-02、DS-03）：
 * config 放全部非密钥字段（数字类转成整数，空着的数字类不放）；secrets 只放填了的密钥字段。
 */
export function splitValues(fields = [], values = {}) {
  const config = {};
  const secrets = {};
  fields.forEach((f) => {
    const raw = String(values[f.key] ?? '').trim();
    if (f.type === 'secret') { if (raw) secrets[f.key] = String(values[f.key]); return; }
    if (isNumeric(f.type)) { if (raw) config[f.key] = Number(raw); return; }
    config[f.key] = raw;
  });
  return { config, secrets };
}

// 字段错误的说法：越短越好，标在字段下面
export const FIELD_ERROR_TEXT = { required: '必填', invalid: '格式不对', exists: '已有同名资料' };

/**
 * 服务端错误 → 标到字段：invalid_argument 带 details.fields；file_exists 标到名称。
 * 标不到字段的返回 null，由页面整条显示。
 */
export function fieldErrorsFrom(err) {
  if (!err) return null;
  if (err.code === 'file_exists') return { name: 'exists' };
  const fields = err.code === 'invalid_argument' ? err.details?.fields : null;
  if (fields && typeof fields === 'object' && Object.keys(fields).length) {
    return Object.fromEntries(Object.entries(fields).map(([k, v]) => [k, v === 'required' ? 'required' : 'invalid']));
  }
  return null;
}

/**
 * 测试连接的结果（DS-04）→ 画法：整体结论加逐项检查。
 * 连不上、账号能写也是 200（ok=false），原因在 error 或没通过的那一项里。
 */
export function testView(result) {
  if (!result) return null;
  const checks = (result.checks || []).map((c) => ({ name: c.name, ok: !!c.ok, detail: c.detail || '' }));
  return {
    ok: !!result.ok,
    title: result.ok ? '连接成功' : '连接失败',
    error: result.error ? (result.error.message || result.error.code) : null,
    checks,
  };
}

// 连接器下拉（DS-01）：网关不可用、没有连接器时「接入数据源」入口不出现
export const connectorsReady = (data) => data?.gateway?.status === 'ok' && (data.items || []).length > 0;
export const connectorOptions = (items = []) => items.map((c) => ({ value: c.id, label: c.label }));

/**
 * 数据源资料上能做什么：连接器不在网关里（available=false）时不能重新采集、不能测试，只显示「连接器不可用」。
 */
export function sourceActions(m) {
  if (!isSource(m)) return null;
  const available = m.source?.connector?.available !== false;
  return { available, recollect: available, test: available, edit: true, state: available ? null : '连接器不可用' };
}

/**
 * 修改连接时抽屉要的东西：连接器（只读）、字段、初值。资料里没有 source（旧数据）时返回 null。
 */
export function editTarget(m) {
  if (!isSource(m) || !m.source) return null;
  const fields = m.source.fields || [];
  return {
    path: m.path,
    name: m.title || m.name,
    connector: m.source.connector,
    fields,
    values: initialValues(fields, m.source.config || {}),
    savedSecrets: m.source.secret_fields || [],
  };
}

// 修改时密钥字段的占位：已存过的写「已保存」，空着就是不改
export const secretPlaceholder = (field, savedSecrets = []) => (savedSecrets.includes(field.key) ? '已保存' : field.placeholder || '');
