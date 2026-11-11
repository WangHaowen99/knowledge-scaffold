// 资料上传的类型白名单与大小上限（契约 1.8）：资料区、新建项目、工作台上传卡、对话附件共用这一份。
// 规则以后端为准：C-01 下发 material_types 与 limits.material_upload_bytes；下面的清单只在还没取到配置时兜底，
// 后端测试 test_resource_upload 核对它和后端 uploads.MATERIAL_TYPES 一致。
export const RESOURCE_TYPES = ['.md', '.markdown', '.txt', '.doc', '.docx', '.xls', '.xlsx', '.xlsm', '.csv', '.pdf', '.drawio', '.html', '.htm', '.png', '.jpg', '.jpeg'];
export const MAX_RESOURCE_BYTES = 20 * 1024 * 1024;
// 图纸、图片类：提示转成 PDF 或 drawio
const DRAWING_TYPES = ['.dwg', '.dxf', '.vsd', '.vsdx', '.png', '.jpg', '.jpeg', '.svg', '.gif', '.bmp', '.webp'];

export function extOf(name) {
  const text = String(name || '');
  const index = text.lastIndexOf('.');
  return index > 0 ? text.slice(index).toLowerCase() : '';
}

export function uploadRules(config) {
  const types = config?.material_types?.length ? config.material_types.map((x) => String(x).toLowerCase()) : RESOURCE_TYPES;
  const maxBytes = config?.limits?.material_upload_bytes || MAX_RESOURCE_BYTES;
  return { types, maxBytes, accept: types.join(',') };
}

export const DEFAULT_RULES = uploadRules();

// 选完文件先在本地挡掉格式不对、太大的；返回错误文案，能传返回 null
export function checkResourceFile(name, size, rules = DEFAULT_RULES) {
  const ext = extOf(name);
  if (!rules.types.includes(ext)) {
    return DRAWING_TYPES.includes(ext) ? '不支持这种格式，请转成 PDF 或 drawio 后再上传' : '不支持这种格式，请转成 PDF、Word 或 Markdown 后再上传';
  }
  if (size > rules.maxBytes) return `超过 ${Math.round(rules.maxBytes / 1048576)} MB，请拆分或压缩后再上传`;
  return null;
}

// 一批文件拆成能传的和挡下的（挡下的带原因），能传的照常传
export function splitResourceFiles(files, rules = DEFAULT_RULES) {
  const accepted = [];
  const rejected = [];
  for (const file of [...(files || [])]) {
    const error = checkResourceFile(file?.name, file?.size || 0, rules);
    if (error) rejected.push({ file, error });
    else accepted.push(file);
  }
  return { accepted, rejected };
}

// 旧版二进制 Office：能上传，但转不出文本，分析和预览都用不了，传完提示另存（不拦截）
export function legacyOfficeHint(name) {
  const ext = extOf(name);
  if (ext !== '.doc' && ext !== '.xls') return '';
  return `${name} 是旧版格式，另存为 ${ext === '.doc' ? '.docx' : '.xlsx'} 再上传才能分析和预览`;
}
