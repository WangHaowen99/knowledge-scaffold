function isRecord(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function fileLabel(file, index) {
  if (typeof file === 'string') { return file; }
  if (isRecord(file) && file.path) { return String(file.path); }
  return `条目 ${index + 1}`;
}

function summarizeFileSection(section) {
  const files = Array.isArray(section?.files) ? section.files : [];
  const totalFiles = Number.isFinite(Number(section?.total_files)) ?
    Number(section.total_files) :
    files.length;
  const binaryFiles = files.filter((file) => isRecord(file) && file.binary === true).length;
  const truncatedFiles = files.filter((file) => isRecord(file) && file.truncated === true).length;
  return {
    totalFiles,
    previewedFiles: files.length,
    textFiles: files.length - binaryFiles,
    binaryFiles,
    truncatedFiles,
    isTruncated: section?.truncated === true || totalFiles > files.length,
    paths: files.map(fileLabel),
  };
}

function warningMessages(value) {
  if (Array.isArray(value)) { return value.flatMap(warningMessages); }
  if (isRecord(value)) { return Object.values(value).flatMap(warningMessages); }
  const message = String(value ?? '').trim();
  return message ? [message] : [];
}

function summarizeWarnings(value) {
  if (!value) { return []; }
  if (Array.isArray(value)) { return warningMessages(value).map((message) => ({ category: 'general', message })); }
  if (!isRecord(value)) { return warningMessages(value).map((message) => ({ category: 'general', message })); }
  return Object.entries(value).flatMap(([category, messages]) => (
    warningMessages(messages).map((message) => ({ category, message }))
  ));
}

export function summarizeImportPreview(preview) {
  if (preview == null || preview === '') { return { kind: 'empty' }; }
  if (typeof preview === 'string') { return { kind: 'text', text: preview }; }

  if (Array.isArray(preview)) {
    const files = preview;
    const fileRecords = files.filter((file) => isRecord(file));
    const binaryFiles = fileRecords.filter((file) => file.binary === true).length;
    const truncatedFiles = fileRecords.filter((file) => file.truncated === true).length;
    return {
      kind: 'array',
      totalFiles: files.length,
      previewedFiles: files.length,
      textFiles: fileRecords.length - binaryFiles,
      binaryFiles,
      truncatedFiles,
      isTruncated: truncatedFiles > 0,
      paths: files.map(fileLabel),
    };
  }

  if (!isRecord(preview)) { return { kind: 'empty' }; }
  const source = preview.source && isRecord(preview.source) ? summarizeFileSection(preview.source) : null;
  const result = preview.result && isRecord(preview.result) ? summarizeFileSection(preview.result) : null;
  const warnings = summarizeWarnings(preview.warnings);
  const nested = Object.prototype.hasOwnProperty.call(preview, 'preview') ?
    summarizeImportPreview(preview.preview) :
    null;
  if (!source && !result && !warnings.length && (!nested || nested.kind === 'empty')) { return { kind: 'empty' }; }
  return {
    kind: 'structured', source, result, warnings, nested
  };
}

export function previewValueForDetails(value) {
  if (typeof value === 'string') { return value; }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}
