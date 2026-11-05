export const MAX_RESOURCE_FILE_SIZE = 20 * 1024 * 1024;

export const RESOURCE_FILE_EXTENSIONS = new Set([
  '.csv',
  '.doc',
  '.docx',
  '.drawio',
  '.jpeg',
  '.jpg',
  '.md',
  '.pdf',
  '.png',
  '.txt',
  '.webp',
  '.xls',
  '.xlsm',
  '.xlsx',
]);

export const RESOURCE_FILE_ACCEPT = [...RESOURCE_FILE_EXTENSIONS].join(',');

function extension(name = '') {
  const index = name.lastIndexOf('.');
  return index > 0 ? name.slice(index).toLowerCase() : '';
}

export function validateResourceFile(file) {
  const name = file?.name || '未命名文件';
  if (!RESOURCE_FILE_EXTENSIONS.has(extension(name))) {
    return {
      valid: false,
      code: 'unsupported_resource_file_type',
      message: `不支持文件“${name}”的类型`,
    };
  }
  if (file.size > MAX_RESOURCE_FILE_SIZE) {
    return {
      valid: false,
      code: 'resource_file_too_large',
      message: `文件“${name}”超过 20 MB，无法上传`,
    };
  }
  return { valid: true, code: '', message: '' };
}

export function validateResourceFiles(files) {
  const accepted = [];
  const rejected = [];
  for (const file of [...(files || [])]) {
    const validation = validateResourceFile(file);
    if (validation.valid) accepted.push(file);
    else rejected.push({ file, ...validation });
  }
  return { accepted, rejected };
}

export function rejectedResourceMessage(rejected) {
  return rejected.map((item) => item.message).join('；');
}
