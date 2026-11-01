// mock：插件目录、详情、兼容检查、申请与平台安装（PL-01～PL-04、PL-19）。页面：plugins、plugin-detail、create（第 3 步）
// PL-05～PL-11（按项目启用、升级、回滚、停用）在 routes-settings.js。
export function register(ctx) {
  const { on, json, F } = ctx;
  // PL-01（从 index.js 搬来的最小版本，F4 子任务 D 扩充）
  on('GET', '/packs', ({ query }) => {
    const cats = (query.get('category') || '').split(',').filter(Boolean);
    const items = F.packs().filter((p) => !cats.length || cats.includes(p.category));
    return json(200, { items, counts: { all: items.length }, recommendations: [] });
  });
}
