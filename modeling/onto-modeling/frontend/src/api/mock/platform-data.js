// 平台视角的合成数据（运维中心、用户管理共用）：账号与别人的项目。
// 只是元数据（名称、所有者、阶段、占用），不含项目内容（契约 1.4 ProjectMetaView）。
// 运维总览、会话池、用户列表、用户名下的项目、审计、回收站都从这里取名字，保证几页说的是同一批人和项目。
import { MAIN_PID, projectCards } from './fixtures.js';

// 23 个账号（总览「共 23 个账号」）；online 表示当前在线（总览「在线用户 9」）
export const PLATFORM_USERS = [
  { username: 'zhangsan', display_name: '张工', role: 'admin', online: true },
  { username: 'wanggong', display_name: '王工', role: 'user', online: true },
  { username: 'ops01', display_name: '周运维', role: 'ops', online: true },
  { username: 'liming', display_name: '李明', role: 'user', online: true },
  { username: 'chenjing', display_name: '陈静', role: 'user', online: true },
  { username: 'zhaolei', display_name: '赵磊', role: 'user', online: true },
  { username: 'sunyu', display_name: '孙宇', role: 'user', online: true },
  { username: 'zhoumin', display_name: '周敏', role: 'user', online: true },
  { username: 'wuhao', display_name: '吴昊', role: 'user', online: true },
  { username: 'modeler01', display_name: '建模者', role: 'user', online: false },
  { username: 'zhengyi', display_name: '郑一', role: 'user', online: false },
  { username: 'fengxue', display_name: '冯雪', role: 'user', online: false },
  { username: 'heqing', display_name: '何青', role: 'user', online: false },
  { username: 'luoyang', display_name: '罗阳', role: 'user', online: false },
  { username: 'tangli', display_name: '唐丽', role: 'user', online: false },
  { username: 'hanbo', display_name: '韩波', role: 'user', online: false },
  { username: 'caoxin', display_name: '曹欣', role: 'user', online: false },
  { username: 'xuning', display_name: '徐宁', role: 'user', online: false },
  { username: 'majun', display_name: '马骏', role: 'user', online: false },
  { username: 'linfang', display_name: '林芳', role: 'user', online: false },
  { username: 'guotao', display_name: '郭涛', role: 'user', online: false },
  { username: 'admin02', display_name: '系统管理员', role: 'admin', online: false },
  { username: 'ops02', display_name: '值班运维', role: 'ops', online: false },
];

export const userLabel = (username) => PLATFORM_USERS.find((u) => u.username === username)?.display_name || username;

// 别人的项目（合成场景，与 zhangsan 自己的 6 个项目一起构成平台上的一部分项目）
const OTHERS = [
  { id: 'parcel-sort-6c1f2a90', name: '快递分拣异常处理', owner: 'wanggong', build_mode: 'L4', stage: '建模（按段）', disk_bytes: 412e6, days: 0 },
  { id: 'clinic-queue-9e3b7d21', name: '社区诊所排队分诊', owner: 'liming', build_mode: 'L2', stage: '校验与走查', disk_bytes: 286e6, days: 0 },
  { id: 'hotel-room-2d8a4c55', name: '酒店客房清洁排程', owner: 'chenjing', build_mode: 'L2', stage: '资料分析', disk_bytes: 198e6, days: 1 },
  { id: 'gym-member-7f04b6e3', name: '健身房会员续费提醒', owner: 'zhaolei', build_mode: 'L2', stage: '建模（按段）', disk_bytes: 154e6, days: 2 },
  { id: 'canteen-menu-4a9c1e08', name: '食堂菜单与备餐', owner: 'sunyu', build_mode: 'L2', stage: '发布', disk_bytes: 121e6, days: 3 },
  { id: 'park-lot-8b2e5f71', name: '停车场车位调度', owner: 'zhoumin', build_mode: 'L4', stage: '校验与走查', disk_bytes: 98e6, days: 5 },
  { id: 'school-bus-3e7d9a46', name: '校车线路排班', owner: 'wuhao', build_mode: 'L2', stage: '资料分析', disk_bytes: 76e6, days: 9 },
];

const DAY = 24 * 3600 * 1000;

/** 平台上的项目元数据视图（ProjectMetaView 加占用与最近活动），按磁盘占用倒序 */
export function platformProjects(now = Date.now()) {
  const mine = projectCards().map((c, i) => ({
    id: c.id, name: c.name, owner: c.owner, build_mode: c.build_mode, archived: c.archived,
    stage: c.id === MAIN_PID ? '建模（按段）' : c.status.includes('publishable') ? '发布' : '建模（按段）',
    disk_bytes: [524e6, 368e6, 233e6, 88e6, 41e6, 12e6][i] || 10e6, last_activity_at: c.last_activity_at,
  }));
  const others = OTHERS.map((p) => ({
    id: p.id, name: p.name, owner: p.owner, build_mode: p.build_mode, archived: false, stage: p.stage,
    disk_bytes: p.disk_bytes, last_activity_at: new Date(now - p.days * DAY - 3600 * 1000).toISOString(),
  }));
  return [...mine, ...others].sort((a, b) => b.disk_bytes - a.disk_bytes);
}

// 平台总数（总览的统计卡）：列表只是其中一部分
export const PLATFORM_TOTALS = { accounts: PLATFORM_USERS.length, online: PLATFORM_USERS.filter((u) => u.online).length, projects: 38, active: 24, archived: 6 };
