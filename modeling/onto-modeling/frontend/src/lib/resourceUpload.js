// 资料上传的类型白名单与大小上限（契约 1.8）：资料区、新建项目、工作台上传卡、对话附件共用这一份，文字与 develop 一致。
// 规则以后端为准：C-01 下发 material_types 与 limits.material_upload_bytes；下面的清单只在还没取到配置时兜底，
// 后端测试 test_resource_upload 核对它和后端 uploads.MATERIAL_TYPES 一致。
export const MAX_RESOURCE_FILE_SIZE = 20 * 1024 * 1024;

export const RESOURCE_TYPES = ['.md', '.markdown', '.txt', '.doc', '.docx', '.xls', '.xlsx', '.xlsm', '.csv', '.pdf', '.drawio', '.html', '.htm', '.png', '.jpg', '.jpeg', '.webp'];

export const RESOURCE_FILE_EXTENSIONS = new Set(RESOURCE_TYPES);

export const RESOURCE_FILE_ACCEPT = RESOURCE_TYPES.join(',');

function extension(name = '') {
  const index = name.lastIndexOf('.');
  return index > 0 ? name.slice(index).toLowerCase() : '';
}

export function uploadRules(config) {
  const types = config?.material_types?.length ? config.material_types.map((x) => String(x).toLowerCase()) : RESOURCE_TYPES;
  const maxBytes = config?.limits?.material_upload_bytes || MAX_RESOURCE_FILE_SIZE;
  return { types: new Set(types), maxBytes, accept: types.join(',') };
}

const DEFAULT_RULES = uploadRules();

export function validateResourceFile(file, rules = DEFAULT_RULES) {
  const name = file?.name || '未命名文件';
  if (!rules.types.has(extension(name))) {
    return {
      valid: false,
      code: 'unsupported_resource_file_type',
      message: `不支持文件“${name}”的类型`,
    };
  }
  if (file.size > rules.maxBytes) {
    return {
      valid: false,
      code: 'resource_file_too_large',
      message: `文件“${name}”超过 ${Math.round(rules.maxBytes / 1048576)} MB，无法上传`,
    };
  }
  return { valid: true, code: '', message: '' };
}

export function validateResourceFiles(files, rules = DEFAULT_RULES) {
  const accepted = [];
  const rejected = [];
  for (const file of [...(files || [])]) {
    const validation = validateResourceFile(file, rules);
    if (validation.valid) accepted.push(file);
    else rejected.push({ file, ...validation });
  }
  return { accepted, rejected };
}

export function rejectedResourceMessage(rejected) {
  return rejected.map((item) => item.message).join('；');
}

// 旧版二进制 Office 格式：资料分析读不了、平台内也无法预览，上传后提示另存（不拦截上传）
export function legacyOfficeHint(fileName) {
  const ext = extension(String(fileName || '')).slice(1);
  if (ext !== 'doc' && ext !== 'xls') return '';
  const target = ext === 'doc' ? '.docx' : '.xlsx';
  return `${fileName} 是旧版格式，建议另存为 ${target} 后上传，否则无法分析和预览`;
}
