const WEAK_PASSWORDS = new Set([
  '123456',
  '12345678',
  'admin',
  'admin123',
  'password',
  'password123',
  'qwerty',
  'qwerty123',
]);

export const PASSWORD_RULE_LABELS = Object.freeze({
  min_length: '至少 8 个字符',
  max_length: '不超过 128 个字符',
  character_classes: '至少包含小写字母、大写字母、数字、特殊字符中的三类',
  contains_username: '不包含用户名',
  surrounding_whitespace: '首尾不含空白字符',
  common_password: '不能是常见弱密码',
  same_as_old: '不能与当前密码相同',
});

function countCharacterClasses(value) {
  return [
    /\p{Ll}/u.test(value),
    /\p{Lu}/u.test(value),
    /\p{N}/u.test(value),
    /[^\p{L}\p{N}]/u.test(value),
  ].filter(Boolean).length;
}

export function evaluatePasswordRules(
  password,
  { username = '', oldPassword = '', includeSameAsOld = false } = {},
) {
  const value = String(password || '');
  const normalizedUser = String(username || '').trim().toLocaleLowerCase();
  const normalizedValue = value.toLocaleLowerCase();
  const rules = [
    { id: 'min_length', label: PASSWORD_RULE_LABELS.min_length, met: value.length >= 8 },
    { id: 'max_length', label: PASSWORD_RULE_LABELS.max_length, met: value.length <= 128 },
    { id: 'character_classes', label: PASSWORD_RULE_LABELS.character_classes, met: countCharacterClasses(value) >= 3 },
    {
      id: 'contains_username',
      label: PASSWORD_RULE_LABELS.contains_username,
      met: !normalizedUser || !normalizedValue.includes(normalizedUser),
    },
    { id: 'surrounding_whitespace', label: PASSWORD_RULE_LABELS.surrounding_whitespace, met: value === value.trim() },
    { id: 'common_password', label: PASSWORD_RULE_LABELS.common_password, met: !WEAK_PASSWORDS.has(normalizedValue) },
  ];
  if (includeSameAsOld) {
    rules.push({
      id: 'same_as_old',
      label: PASSWORD_RULE_LABELS.same_as_old,
      met: !oldPassword || value !== oldPassword,
    });
  }
  return rules;
}

export function applyServerPasswordViolations(rules, violations) {
  const rejected = new Set(Array.isArray(violations) ? violations.map(String) : []);
  return (Array.isArray(rules) ? rules : []).map((rule) => ({
    ...rule,
    met: rule.met && !rejected.has(rule.id),
    serverRejected: rejected.has(rule.id),
  }));
}

export function passwordViolationLabels(violations) {
  return (Array.isArray(violations) ? violations : [])
    .map((violation) => PASSWORD_RULE_LABELS[violation] || `服务端规则：${violation}`);
}

export function passwordFormState({
  password = '',
  confirmation = '',
  username = '',
  oldPassword = '',
  includeSameAsOld = false,
  serverViolations = [],
} = {}) {
  const rules = applyServerPasswordViolations(
    evaluatePasswordRules(password, { username, oldPassword, includeSameAsOld }),
    serverViolations,
  );
  const matches = Boolean(password) && Boolean(confirmation) && password === confirmation;
  return {
    rules,
    matches,
    canSubmit: Boolean(password) && Boolean(confirmation) && matches && rules.every((rule) => rule.met),
  };
}
