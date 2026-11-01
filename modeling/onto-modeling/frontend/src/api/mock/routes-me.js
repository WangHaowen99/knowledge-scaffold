// mock：个人设置（A-05～A-11）。页面：me，顶栏头像；项目设置的模型来源也读这里的状态（db.me）
export function register(ctx) {
  const { on, json, db, F } = ctx;
  db.me = db.me || { modelSettings: F.modelSettings() };
  // A-07（从 index.js 搬来，F4 子任务 F 扩充）
  on('GET', '/me', ({ user }) => json(200, {
    ...user, last_login: { at: new Date().toISOString(), ip: '192.0.2.10' }, project_count: db.cards.length,
    usage_this_month: { output_tokens: 410000, input_tokens: 3200000, cost: { amount: 126, currency: 'CNY' } },
  }));
  // A-09
  on('GET', '/me/model-settings', () => json(200, db.me.modelSettings));
}
