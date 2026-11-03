// mock 数据 · 场景走查页（插件产物 trace.scenario、trace.mock，全部合成）：
// 23 个场景（通过 19 / 失败 1 / 过期 3），示例数据 6 组。分段、操作、资料取自 scenario.js；
// 场景 3、4、6 的 key 与 Studio、增量更新里引用的一致（s-03、s-04、s-06），s-21 是工作台本轮卡里新增的场景。
import { ANCHORS } from './fixtures.js';
import { entryRef } from './scenario.js';

const at = (mo, d, h = 10, mi = 0) => {
  const x = new Date();
  x.setMonth(mo - 1, d);
  x.setHours(h, mi, 0, 0);
  if (x.getTime() > Date.now()) x.setFullYear(x.getFullYear() - 1);
  return x.toISOString();
};

const F01 = 'file:resource/others/01-场景说明.docx';
const F02 = 'file:resource/others/02-门店补货规则.docx';
const F03A = 'file:resource/others/03-样例补货单-A门店.docx';
const F03B = 'file:resource/others/03-样例补货单-B.docx';
const F04 = 'file:resource/others/04-商品陈列说明.pdf';
const F07 = 'file:resource/others/07-门店C特殊需求.pdf';
const F09 = 'file:resource/others/09-修订通知.docx';

// 出处锚点：和 fixtures、Studio、自主构建用到的同一处资料用同一个锚点
export const SC_ANCHORS = {
  a3: `${F03A}#p:3`, a5: ANCHORS.ea, a6: `${F03A}#p:6`, b2: `${F03B}#p:2`, b4: ANCHORS.eb,
  s2: `${F01}#p:2`, r2: `${F02}#p:2`, r5: `${F02}#p:5`, r73: `${F02}#p:7.3`, r75: `${F02}#p:7.5`, r8: `${F02}#p:8`, r81: `${F02}#p:8.1`,
  r92: ANCHORS.e92, r114: ANCHORS.e114, show3: `${F04}#p:3`, c1: `${F07}#p:1`, n2: `${F09}#p:2`, n63: `${F09}#p:6.3`,
};
const A = SC_ANCHORS;

function res(anchor, source_label, location_label, marked) {
  let text = '';
  const highlights = [];
  marked.split(/(<mark>.*?<\/mark>)/).forEach((part) => {
    const m = part.match(/^<mark>(.*)<\/mark>$/);
    if (m) { highlights.push({ start: text.length, end: text.length + m[1].length }); text += m[1]; } else text += part;
  });
  return { anchor, scheme: 'file', resolved: true, source_label, location_label, text, highlights, confidence: null, snapshot_id: null, page: 'resources' };
}

// EV-29 悬浮原文：index.js 的 /anchors/resolve 查 F.ANCHOR_RESOLUTIONS，别的分组已经登记过的不覆盖
export const SC_RESOLUTIONS = [
  res(A.a3, '03-样例补货单-A门店.docx', '第 3 节 安全库存', '牛奶同时列在乳制品和冷藏两类，安全库存分别是 120 箱和 140 箱，<mark>取较严的 120 箱</mark>。'),
  res(A.a6, '03-样例补货单-A门店.docx', '第 6 节', '检查项目：安全库存、陈列量、临期、<mark>冷链温度记录</mark>。'),
  res(A.b2, '03-样例补货单-B.docx', '第 2 节', 'B 门店本次<mark>没有特殊需求</mark>，各品类按标准执行。'),
  res(A.s2, '01-场景说明.docx', '§2 受理范围', '受理范围：<mark>乳制品、烘焙、冷藏三类商品</mark>；日均销量超过 200 箱的门店转区域统一补货。'),
  res(A.r2, '02-门店补货规则.docx', '§2 必填项', '补货申请必须写明门店、商品、<mark>到货周期</mark>和申请量。'),
  res(A.r5, '02-门店补货规则.docx', '§5 按类取标准', '安全库存<mark>按商品品类取标准</mark>，品类表里没有的按同类商品估算。'),
  res(A.r73, '02-门店补货规则.docx', '§7.3 陈列量', '陈列量按适用标准规定的<mark>上限</mark>执行，单位：箱。'),
  res(A.r75, '02-门店补货规则.docx', '§7.5 促销备货', '促销备货<mark>可在安全库存表中注明</mark>。'),
  res(A.r8, '02-门店补货规则.docx', '§8 临期检查', '临期抽检按「先到先出」核对，<mark>同一到货批次、同一品类算一批</mark>。'),
  res(A.r81, '02-门店补货规则.docx', '§8.1 临期分级', '临期商品按<mark>四级临期分级</mark>评定。'),
  res(A.show3, '04-商品陈列说明.pdf', '第 3 节', '保质期剩余<mark>不超过 3 天</mark>的商品当天下架。'),
  res(A.c1, '07-门店C特殊需求.pdf', '第 1 页（扫描件）', '门店 C 对酸奶有特殊陈列需求，<mark>原件只有扫描件</mark>。'),
  res(A.n2, '09-修订通知.docx', '§2', '乳制品区域配额上限，<mark>按区域统一配额 ≤150 箱</mark> 执行（日均销量 ≥80 箱时适用）。'),
  res(A.n63, '09-修订通知.docx', '§6.3', '新增 6.3：<mark>门店指定标准时以门店为准</mark>，临时加量另行审批。'),
];

// s(key, 标题, 分段, 操作, 出处, 出处名, 输入类型, 上次推演, 结果, 输入, 预期, 另外的字段)
const s = (key, title, slice, op, anchor, label, input, run, result, inputText, expect, extra = {}) => ({
  key, title, slice, operation: entryRef(op), source: anchor, source_label: label, input_kind: input,
  last_run_at: run, result, input_text: inputText, expect_text: expect, ...extra,
});
const T = (d, h, mi) => at(10, d, h, mi);

export function scenarios() {
  return [
    s('s-21', '门店申请严于标准，按门店执行', 's5', 'overage_and_hint', A.a5, '样例补货单 A §5', 'custom', T(31, 15, 21), 'pass',
      '门店：A 门店；商品：牛奶；品类标准上限 120 箱；门店申请 96 箱', '按门店执行 96 箱，不生成审批提示'),
    s('s-22', '门店申请超出标准，生成审批提示', 's5', 'overage_and_hint', A.b4, '样例补货单 B §4', 'custom', T(31, 15, 21), 'pass',
      '门店：B 门店；商品：酸奶；品类标准上限 110 箱；门店申请 130 箱', '超量清单列出酸奶 130 / 110 箱，生成审批提示交区域督导'),
    s('s-03', '场景 3 门店更严（陈列量）', 's3', 'display_qty_check', A.a5, '样例补货单 A §5', 'mock', T(30, 18, 2), 'stale',
      '查示例数据：A 门店、牛奶、每日到货', '陈列上限取门店申请 96 箱', { stale_source: '09 修订通知', stale_reason: '09 修订通知 §2 改了乳制品陈列上限，示例数据要按新口径重新生成' }),
    s('s-04', '场景 4 乳制品隔日到货 ≤150 箱', 's3', 'display_qty_check', A.n2, '09-修订通知 §2', 'custom', null, 'stale',
      '商品：牛奶；到货周期：隔日；日均销量 85 箱', '陈列上限取区域配额 150 箱', { stale_source: '09 修订通知', stale_reason: '09 修订通知 §2 新增的场景，还没推演过' }),
    s('s-06', '场景 6 临时加量缺 6.3 新分支', 's3', 'display_qty_check', A.n63, '09-修订通知 §6.3', 'custom', T(30, 18, 2), 'stale',
      '门店指定适用标准 2024 版；临时加量 20 箱', '按门店指定的标准取值，临时加量生成审批提示', { stale_source: '09 修订通知', stale_reason: '09 修订通知新增 6.3「门店指定标准时以门店为准」，操作里还没有这个分支' }),
    s('s-05', '安全库存多类出现取最严值', 's2', 'safety_stock_calc', A.a3, '样例补货单 A §3', 'mock', T(31, 13, 40), 'fail',
      '查示例数据：牛奶同时属于乳制品、冷藏两类', '安全库存取两类中较严的 120 箱', { expected: '≤120 箱', actual: '140 箱', fail_reason: '第 3 节和第 5 节取值没有取严' }),
    s('s-01', '商品不在受理范围，拒绝受理', 's1', 'intake_checks', A.s2, '01-场景说明 §2', 'custom', T(29, 16, 10), 'pass',
      '门店：A 门店；商品：洗衣液（日化）', '拒绝受理，提示商品不在受理范围'),
    s('s-02', '日均销量超出 200 箱，受理校验拦截', 's1', 'intake_checks', A.s2, '01-场景说明 §2', 'custom', T(29, 16, 10), 'pass',
      '门店：A 门店；商品：牛奶；日均销量 230 箱', '受理校验拦截，转区域统一补货'),
    s('s-07', '门店没有特殊需求，全部按标准', 's1', 'intake_checks', A.b2, '样例补货单 B §2', 'mock', T(29, 16, 10), 'pass',
      '查示例数据：B 门店，没有特殊需求', '各品类按标准上限生成补货单'),
    s('s-08', '门店 C 特殊需求只有扫描件，转人工确认', 's1', 'intake_checks', A.c1, '07-门店C特殊需求 §1', 'custom', T(29, 16, 10), 'pass',
      '门店：C 门店；特殊需求：只有扫描件', '提示上传文本版，暂按标准执行'),
    s('s-09', '必填项缺到货周期，追问后受理', 's1', 'intake_checks', A.r2, '补货规则 §2', 'custom', T(29, 16, 10), 'pass',
      '门店：A 门店；商品：酸奶；没有写到货周期', '追问到货周期，补齐后受理'),
    s('s-10', '安全库存按类取标准', 's2', 'safety_stock_calc', A.r5, '补货规则 §5', 'mock', T(31, 13, 40), 'pass',
      '查示例数据：酸奶，乳制品一类', '安全库存取乳制品标准 90 箱'),
    s('s-11', '促销备货单列安全库存', 's2', 'safety_stock_calc', A.r75, '补货规则 §7.5', 'custom', T(31, 13, 40), 'pass',
      '商品：牛奶；促销备货 30 箱', '安全库存表里单列促销备货 30 箱'),
    s('s-12', '酸奶隔日到货，安全库存按两天计', 's2', 'safety_stock_calc', A.a3, '样例补货单 A §3', 'mock', T(31, 13, 40), 'pass',
      '查示例数据：酸奶，隔日到货，日均销量 40 箱', '安全库存按两天销量计 80 箱'),
    s('s-13', '牛奶每日到货，陈列上限 120 箱', 's3', 'display_qty_check', A.r73, '补货规则 §7.3', 'mock', T(30, 18, 2), 'pass',
      '查示例数据：牛奶，每日到货', '陈列上限 120 箱'),
    s('s-14', '区域统一配额 ≤100 箱，按配额执行', 's3', 'display_qty_check', A.b4, '样例补货单 B §4', 'custom', T(30, 18, 2), 'pass',
      '门店：B 门店；商品：牛奶；区域统一配额 100 箱', '陈列上限按区域配额 100 箱'),
    s('s-15', '保质期剩余 ≤3 天下架', 's4', 'expiry_check', A.show3, '商品陈列说明 §3', 'custom', T(30, 11, 20), 'pass',
      '商品：酸奶；保质期剩余 2 天', '当天下架，不计入陈列量'),
    s('s-16', '临期商品按四级分级', 's4', 'expiry_check', A.r81, '补货规则 §8.1', 'custom', T(30, 11, 20), 'pass',
      '商品：面包；保质期剩余三分之一', '评为二级临期'),
    s('s-17', '检查项目包含冷链温度记录', 's4', 'expiry_check', A.a6, '样例补货单 A §6', 'mock', T(30, 11, 20), 'pass',
      '查示例数据：A 门店本批到货的检查项目', '检查项目里有冷链温度记录'),
    s('s-18', '同一到货批次、同一品类算一批', 's4', 'expiry_check', A.r8, '补货规则 §8', 'custom', T(30, 11, 20), 'pass',
      '同一到货批次里有牛奶、酸奶两种乳制品', '按一批抽检'),
    s('s-19', '临时加量须审批，审批前不得下单', 's5', 'overage_and_hint', A.r114, '补货规则 §11.4', 'custom', T(31, 15, 21), 'pass',
      '门店：A 门店；临时加量 30 箱', '生成审批提示，审批通过前不生成补货单'),
    s('s-20', '超量清单逐条列出标准值与申请值', 's5', 'overage_and_hint', A.r92, '补货规则 §9.2', 'custom', T(31, 15, 21), 'pass',
      '门店：B 门店；两个商品超出标准', '超量清单两行，各写标准值与申请值'),
    s('s-23', '门店申请等于标准，不算超量', 's5', 'overage_and_hint', A.a5, '样例补货单 A §5', 'custom', T(31, 15, 21), 'pass',
      '门店：A 门店；商品：牛奶；门店申请 120 箱', '不进超量清单'),
  ];
}

// 推演通过率的历史（版本 v0.6～v0.11），最后一个点是当前
export const PASS_TREND = [['v0.6', 61], ['v0.7', 66], ['v0.8', 70], ['v0.9', 74], ['v0.10', 79], ['v0.11', 78]];

// 示例数据（trace.mock）：每组是一张表，属性英文名就是业务系统表的列名
const md = (key, scenariosText, rows, columns, preview) => ({
  key, object: entryRef(key), scenarios: scenariosText, rows, mode: 'by_scenario', columns, preview,
});
export function mockSets() {
  return [
    md('goods_category', '场景 3、安全库存取最严', 6, ['category_code', 'goods_name', 'delivery_cycle', 'std_ref'], [
      ['C-DAIRY', '牛奶', '每日', '陈列标准-2024'], ['C-DAIRY', '酸奶', '隔日', '陈列标准-2024'], ['C-COLD', '牛奶', '每日', '冷藏标准-2022'],
      ['C-BAKE', '面包', '每日', '烘焙标准-2023'], ['C-BAKE', '吐司', '隔日', '烘焙标准-2023'], ['C-COLD', '鲜肉', '每日', '冷藏标准-2022'],
    ]),
    md('safety_stock_std', '安全库存取最严', 14, ['category_code', 'goods_name', 'safety_qty', 'std_ref'], [
      ['C-DAIRY', '牛奶', '120', '陈列标准-2024'], ['C-COLD', '牛奶', '140', '冷藏标准-2022'], ['C-DAIRY', '酸奶', '90', '陈列标准-2024'],
      ['C-BAKE', '面包', '60', '烘焙标准-2023'], ['C-BAKE', '吐司', '40', '烘焙标准-2023'],
    ]),
    md('display_qty_std', '场景 3', 8, ['goods_name', 'delivery_cycle', 'max_qty', 'std_ref'], [
      ['牛奶', '每日', '120', '陈列标准-2024'], ['牛奶', '隔日', '96', '陈列标准-2024'], ['酸奶', '每日', '110', '陈列标准-2024'],
      ['酸奶', '隔日', '90', '陈列标准-2024'], ['面包', '每日', '—', '烘焙标准-2023'],
    ]),
    md('store', '场景 3、无特殊需求', 3, ['store_code', 'store_name', 'store_level', 'region_code'], [
      ['S-001', 'A 门店', '一级', 'R-01'], ['S-002', 'B 门店', '二级', 'R-01'], ['S-003', 'C 门店', '二级', 'R-02'],
    ]),
    md('store_request', '场景 3', 4, ['store_code', 'goods_name', 'request_qty', 'request_note'], [
      ['S-001', '牛奶', '96', '每日到货'], ['S-001', '酸奶', '80', '隔日到货'], ['S-003', '酸奶', '60', '扫描件待确认'], ['S-002', '牛奶', '—', '没有特殊需求'],
    ]),
    md('replenish_order', '无特殊需求', 2, ['order_no', 'store_code', 'goods_name', 'order_qty'], [
      ['BH-0001', 'S-002', '牛奶', '100'], ['BH-0002', 'S-002', '酸奶', '80'],
    ]),
  ];
}

// 不落库、跳过生成的对象（数据来源写的是「无」）
export const SKIPPED = [
  { entry: entryRef('approval_hint'), reason: '数据来源写的是「无」' },
  { entry: entryRef('overage_list'), reason: '数据来源写的是「无」' },
];
