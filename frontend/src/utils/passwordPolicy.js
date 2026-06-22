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

const countCharacterClasses = (value) => [
  /[a-z]/.test(value),
  /[A-Z]/.test(value),
  /\d/.test(value),
  /[^A-Za-z0-9]/.test(value),
].filter(Boolean).length;

export const evaluatePasswordRules = (
  password,
  {
    username = '',
    oldPassword = '',
    includeSameAsOld = false,
  } = {},
) => {
  const value = password || '';
  const normalizedUser = (username || '').trim().toLowerCase();
  const normalizedValue = value.toLowerCase();
  const classes = countCharacterClasses(value);
  const rules = [
    { id: 'min_length', label: '至少 8 个字符', met: value.length >= 8 },
    { id: 'max_length', label: '不超过 128 个字符', met: value.length <= 128 },
    { id: 'character_classes', label: '至少包含三类字符', met: classes >= 3 },
    { id: 'contains_username', label: '不包含用户名', met: !normalizedUser || !normalizedValue.includes(normalizedUser) },
    { id: 'surrounding_whitespace', label: '首尾不含空格', met: value === value.trim() },
    { id: 'common_password', label: '不能是常见弱密码', met: !WEAK_PASSWORDS.has(normalizedValue) },
  ];
  if (includeSameAsOld) {
    rules.push({
      id: 'same_as_old',
      label: '不能与当前密码相同',
      met: !oldPassword || value !== oldPassword,
    });
  }
  return rules;
};
