// AskUserQuestion 的传输契约仍以问题文本为键，控件状态按题目位置隔离。
export function isCustomAnswerOption(option) {
  const label = typeof option?.label === 'string' ? option.label.trim() : '';
  const description = typeof option?.description === 'string' ? option.description.trim() : '';
  return /^(other|其他回答|自定义回答)$/i.test(label) ||
    /(?:点|选).*\bother\b.*(?:输入|填写|回答|文本)/i.test(label) ||
    /^占位(?:项|示例|选项|答案)(?:[一二三四\d]+)?$/.test(label) ||
    /^(?:占位(?:示例|选项|答案|项)|仅(?:为|作)?占位)/.test(description);
}

export function buildQuestionSubmission(questions, selected = {}, custom = {}) {
  const entries = [];
  const missing = [];
  const seen = new Set();
  if (!Array.isArray(questions) || !questions.length) {
    return { answers: {}, missing, invalid: true, error: '缺少有效的问题列表' };
  }
  for (const [index, question] of questions.entries()) {
    let error;
    if (!question || typeof question.question !== 'string' || !question.question.trim()) {
      error = `第 ${index + 1} 题缺少有效的问题文本`;
    } else if (seen.has(question.question)) {
      error = `第 ${index + 1} 题的问题文本重复`;
    } else if (question.options != null && (!Array.isArray(question.options) ||
      question.options.some((option) => typeof option?.label !== 'string' || !option.label.trim()))) {
      error = `第 ${index + 1} 题的选项必须是带有非空 label 的对象列表`;
    }
    if (error) return { answers: {}, missing: [], invalid: true, error };
    seen.add(question.question);
    const labels = new Set((question.options || [])
      .filter((option) => !isCustomAnswerOption(option)).map((option) => option.label));
    const choices = [...new Set(selected[index] || [])].filter((label) => labels.has(label));
    const text = (custom[index] || '').trim();
    const answer = question.multiSelect
      ? [...choices, ...(text ? [text] : [])].join(',')
      : text || choices[0] || '';
    if (answer) entries.push([question.question, answer]);
    else missing.push(index);
  }
  return { answers: Object.fromEntries(entries), missing, invalid: false };
}
