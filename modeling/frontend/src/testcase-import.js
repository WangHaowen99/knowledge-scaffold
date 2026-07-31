const MAX_IMPORT_CASES = 500;

const text = (value) => (value == null ? '' : String(value).trim());

export function testcaseImportTemplateJson() {
  return `${JSON.stringify({
    cases: [
      { question: '示例业务问题', groundTruth: '可选：填写标准答案' },
      { question: '仅用于真实执行的问题', groundTruth: '' },
    ],
  }, null, 2)}\n`;
}

export function parseTestcaseImport(raw) {
  let value;
  try {
    value = JSON.parse(String(raw ?? ''));
  } catch {
    throw new Error('用例文件不是有效的 JSON');
  }
  const rows = Array.isArray(value) ? value : value?.cases;
  if (!Array.isArray(rows)) throw new Error('用例文件必须是数组或包含 cases 数组');
  if (!rows.length) throw new Error('用例文件中没有可导入内容');
  if (rows.length > MAX_IMPORT_CASES) throw new Error(`一次最多导入 ${MAX_IMPORT_CASES} 条真实用例`);

  const seen = new Set();
  return rows.map((row, index) => {
    if (!row || typeof row !== 'object' || Array.isArray(row)) {
      throw new Error(`第 ${index + 1} 条用例格式不正确`);
    }
    const question = text(row.question ?? row.prompt);
    if (!question) throw new Error(`第 ${index + 1} 条缺少问题`);
    const key = question.replace(/\s+/g, ' ').toLocaleLowerCase('zh-CN');
    if (seen.has(key)) throw new Error(`用例文件包含重复问题：第 ${index + 1} 条`);
    seen.add(key);
    return {
      question,
      groundTruth: text(row.groundTruth ?? row.ground_truth ?? row.answer ?? row.referenceAnswer),
    };
  });
}
