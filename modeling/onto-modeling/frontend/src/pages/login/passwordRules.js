// 注册密码规则，与服务端同一套（契约 A-03）；服务端仍会再校验一次
const COMMON = ['password', '12345678', 'qwerty123', 'passw0rd!', 'aa123456', 'admin123', '11111111', 'password1'];

export function passwordRules(username, password) {
  const classes = [/[A-Z]/, /[a-z]/, /\d/, /[^A-Za-z0-9]/].filter((r) => r.test(password)).length;
  return {
    len: password.length >= 8,
    max: password.length > 0 && password.length <= 128,
    classes: classes >= 3,
    contains_username: !username || !password.toLowerCase().includes(username.toLowerCase()),
    common: !COMMON.includes(password.toLowerCase()),
    edge_space: password === password.trim(),
  };
}
