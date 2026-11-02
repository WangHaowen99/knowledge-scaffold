// mock 数据 · 知识沉淀页（K-01～K-08）：门店补货单编制的项目层 12 条、零售连锁行业包 24 条（全部合成）。
// 名称、Key、资料取自 scenario.js；第 1 条「陈列上限按到货周期区分」覆盖行业包的「陈列上限按品类取」，和冲突检查、提名回流讲的是同一件事。
import { entryRef } from './scenario.js';

const at = (mo, d, h = 10, mi = 0) => {
  const x = new Date();
  x.setMonth(mo - 1, d);
  x.setHours(h, mi, 0, 0);
  if (x.getTime() > Date.now()) x.setFullYear(x.getFullYear() - 1);
  return x.toISOString();
};

export const INDUSTRY_PACK = { name: 'industry-retail', label: '零售连锁' };
export const LOADING_RULE = '行业条目按「概念」匹配：项目对象名和别名命中行业概念表时才加载；按对象划定范围的条目，每次最多取最近修改的 10 条。';

const GLOBAL = { kind: 'global' };
const on = (...keys) => ({ kind: 'entries', entries: keys.map(entryRef) });
const conv = (label, mo, d, h, mi) => ({ kind: 'conversation', label, at: at(mo, d, h, mi) });
const mat = (label, anchor) => ({ kind: 'material', label, anchor });
const PACK_SRC = { kind: 'pack', label: '零售连锁行业包' };

// 项目层：k(id, 类型, 标题, 范围, 置信度, 来源, 详情)
const p = (id, type, title, scope, confidence, source, extra = {}) => ({
  id, title, layer: 'project', pack: null, type, scope, confidence, source, overrides_industry: false, readonly: false,
  revision: `r1${id.slice(-2)}`, updated_at: source.at || at(10, 27), ...extra,
});
const i = (id, type, title, scope, confidence = 'high', extra = {}) => ({
  id, title, layer: 'industry', pack: INDUSTRY_PACK.name, type, scope, confidence, source: PACK_SRC, overrides_industry: false, readonly: true,
  revision: `ri${id.slice(-2)}`, updated_at: at(9, 20), ...extra,
});

const CONV_DISPLAY = 'b2d94c11e0a7'; // 「陈列量核定返工」

export function projectEntries() {
  return [
    p('kn-p01', 'correction', '陈列上限按到货周期区分，不是按品类', on('display_qty_std'), 'high', conv('对话', 10, 28, 14, 20), {
      overrides_industry: true,
      detail: {
        background_md: '牛奶、酸奶在每日到货和隔日到货两种周期下陈列上限不同，按品类统一取值会让隔日到货的补货单被误判为超量。',
        rule_md: '取陈列上限时先看 [[补货单]] 的到货周期，再查 [[陈列量标准]] 对应行；到货周期缺失时追问，不默认每日到货。',
        source_quote: { text: '不对，应该是按到货周期分，同一个牛奶隔日到货上限 96 箱，每日到货 120 箱。', conversation_id: CONV_DISPLAY, message_id: 'm-7d21', at: at(10, 28, 14, 20), by: '张工' },
        overrides: { pack: INDUSTRY_PACK.name, entry_id: 'kn-i04', entry_title: '陈列上限取值', note: '行业条目写的是「按品类取」' },
        used_by: [entryRef('display_qty_std'), entryRef('display_qty_check'), { type: 'trace.scenario', key: 's-03', title: '场景 3 门店更严' }],
      },
    }),
    p('kn-p02', 'term', '「要货方」就是门店，两个词在补货单里通用', GLOBAL, 'high', conv('对话', 10, 26, 16, 5), {
      detail: {
        background_md: '样例补货单里「要货方」和「门店」混用，抽取时容易拆成两个对象。',
        rule_md: '把「要货方」当作 [[门店]] 的别名，不单独建对象。',
        source_quote: { text: '要货方就是门店，我们单子上两种写法都有。', conversation_id: '9e0f3a7c6d21', message_id: 'm-5a10', at: at(10, 26, 16, 5), by: '张工' },
        used_by: [entryRef('store'), entryRef('replenish_order')],
      },
    }),
    p('kn-p03', 'domain_rule', '临时加量必须区域审批通过后才能下单', on('approval_hint'), 'medium', mat('02-门店补货规则.docx §11.4', 'file:resource/others/02-门店补货规则.docx#p:11.4'), {
      updated_at: at(10, 30, 11, 2),
      detail: {
        background_md: '补货规则 §11.4 写了临时加量要提交区域审批，样例补货单 B 里有一单没等审批就下了。',
        rule_md: '申请量超出标准的临时加量，先生成 [[审批提示]] 交区域督导；审批通过前不生成补货单。',
        source_quote: { text: '对临时加量的商品，店长应生成提示并提交区域审批，审批通过前不得下单。', at: at(10, 30, 11, 2), by: '资料原文' },
        used_by: [entryRef('approval_hint'), entryRef('overage_and_hint'), entryRef('r11_temp_increase')],
      },
    }),
    p('kn-p04', 'correction', '临期商品按四级分级评定，不是区域三级制', on('replenish_order'), 'medium', conv('对话', 10, 29, 10, 40), {
      detail: {
        background_md: '区域三级制只在个别试点门店用过，补货规则 §8.1 统一写的是四级。',
        rule_md: '临期分级一律按四级评定，[[临期损耗标准]] 里的三级口径只作参考。',
        source_quote: { text: '三级那个是试点，正式的都按四级来。', conversation_id: '9e0f3a7c6d21', message_id: 'm-6c02', at: at(10, 29, 10, 40), by: '张工' },
        used_by: [entryRef('expiry_loss_std'), entryRef('expiry_check')],
      },
    }),
    p('kn-p05', 'convention', '促销备货在安全库存表里单列', on('safety_stock_std'), 'medium', conv('对话', 10, 27, 15, 12)),
    p('kn-p06', 'term', '「先到先出」指同一品类按到货批次先后出库', GLOBAL, 'high', mat('04-商品陈列说明.pdf 第 3 节', 'file:resource/others/04-商品陈列说明.pdf#p:3')),
    p('kn-p07', 'domain_rule', '门店申请超出标准时逐条列进超量清单', on('overage_list'), 'high', mat('02-门店补货规则.docx §9.2', 'file:resource/others/02-门店补货规则.docx#p:9.2')),
    p('kn-p08', 'correction', '冷链温度记录属于检查项目，不能省略', on('check_item'), 'medium', conv('对话', 10, 30, 9, 15)),
    p('kn-p09', 'convention', '区域统一配额按 100 箱执行，优先于品类标准', on('display_qty_std'), 'medium', mat('03-样例补货单-B.docx 第 4 节', 'file:resource/others/03-样例补货单-B.docx#p:4')),
    p('kn-p10', 'term', '「到货周期」只取每日、隔日、每周三种', GLOBAL, 'high', conv('对话', 10, 25, 17, 30)),
    p('kn-p11', 'domain_rule', '同一到货批次、同一品类算一批', on('expiry_loss_std'), 'high', mat('02-门店补货规则.docx §8', 'file:resource/others/02-门店补货规则.docx#p:8')),
    p('kn-p12', 'convention', '补货单号按门店编号加日期生成', on('replenish_order'), 'low', conv('对话', 10, 24, 11, 0)),
  ];
}

export function industryEntries() {
  return [
    i('kn-i01', 'convention', '门店申请严于标准时按门店执行', GLOBAL),
    i('kn-i02', 'domain_rule', '同一商品在多个品类出现时取最严值', on('safety_stock_std')),
    i('kn-i03', 'term', '「箱」指标准外箱，补货单里按「≤数值 箱」书写', GLOBAL),
    i('kn-i04', 'domain_rule', '陈列上限按品类取', on('display_qty_std')),
    i('kn-i05', 'term', '「安全库存」指覆盖一个到货周期销量的最低库存', GLOBAL),
    i('kn-i06', 'convention', '补货单按品类分节，每节先写标准再写申请量', on('replenish_order')),
    i('kn-i07', 'domain_rule', '超出标准的申请量必须生成审批提示', on('approval_hint')),
    i('kn-i08', 'term', '「日均销量」按近 28 天销量计算', GLOBAL, 'medium'),
    i('kn-i09', 'convention', '新品首月按同类商品日均销量估算', on('goods_category'), 'medium'),
    i('kn-i10', 'domain_rule', '保质期剩余不足三分之一的商品不再补货', on('expiry_loss_std')),
    i('kn-i11', 'term', '「临期」指保质期剩余不足三分之一', GLOBAL),
    i('kn-i12', 'convention', '冷藏商品按每日到货安排', on('delivery_cycle'), 'medium'),
    i('kn-i13', 'domain_rule', '促销期间安全库存上浮 20%', on('safety_stock_std'), 'medium'),
    i('kn-i14', 'term', '「陈列量」指货架和端架上可见的商品数量', GLOBAL),
    i('kn-i15', 'convention', '门店等级高的优先满足补货申请', on('store'), 'medium'),
    i('kn-i16', 'domain_rule', '审批提示写明标准值、申请值和超出比例', on('approval_hint')),
    i('kn-i17', 'term', '「端架」指货架两端的促销陈列位', GLOBAL),
    i('kn-i18', 'convention', '补货台账按周归档', on('replenish_ledger'), 'medium'),
    i('kn-i19', 'domain_rule', '每批到货抽检冷链温度记录', on('check_item')),
    i('kn-i20', 'term', '「超量率」是超出标准的条目数除以条目总数', GLOBAL),
    i('kn-i21', 'convention', '节假日前一周补货量按日均销量 1.5 倍估算', on('replenish_order'), 'medium'),
    i('kn-i22', 'domain_rule', '区域配额与品类标准不一致时取严', on('display_qty_std')),
    i('kn-i23', 'term', '「到货周期」指相邻两次到货的间隔', GLOBAL),
    i('kn-i24', 'convention', '缺货登记次日补报', on('replenish_ledger'), 'low'),
  ];
}

// 没写详情的条目：按标题和范围给一段通用的背景与规则
export function defaultDetail(e) {
  const linked = e.scope.kind === 'entries' ? e.scope.entries.map((x) => `[[${x.name}]]`).join('、') : '';
  return {
    background_md: e.layer === 'industry' ? '零售连锁行业的通用做法，随行业包发布。' : '专家在建模时补充的口径。',
    rule_md: linked ? `${e.title}；涉及 ${linked}。` : `${e.title}。`,
    source_quote: null,
    overrides: null,
    used_by: e.scope.kind === 'entries' ? e.scope.entries.slice() : [],
  };
}

// K-06 最近一次检查（10-31 15:02）
export function lastCheck() {
  return {
    checked_at: at(10, 31, 15, 2), duration_ms: 4000,
    conflicts: [{
      id: 'kc-1', kind: 'scope_conflict', effective: 'project',
      project_entry: { id: 'kn-p01', title: '陈列上限按到货周期区分，不是按品类' },
      industry_entry: { id: 'kn-i04', title: '陈列上限按品类取' },
      note: '已按项目优先生效；如果是行业通用做法，建议提名回流',
    }],
    consistent_with_materials: 11, unsourced: 1, duplicates: 0, broken_refs: 0,
  };
}

// K-07 脱敏检查：customer_name 等检查项按契约编号，界面另起中文名
export function nominationPreview(e) {
  if (e.id === 'kn-p01') {
    const text = '取陈列上限时先看到货周期，同一商品每日到货与隔日到货的上限不同。示例门店的隔日到货补货单曾因按品类取值被误判超量。';
    const start = text.indexOf('示例门店');
    return {
      sanitized_text: text, highlights: [{ start, end: start + 4 }],
      checks: [
        { item: 'customer_name', found: 1, action: '「A 门店」已替换为「示例门店」' },
        { item: 'internal_id', found: 1, action: '补货单号 BH-2026-118 已移除' },
        { item: 'amount', found: 0, action: '' },
        { item: 'secret_or_pii', found: 0, action: '' },
      ],
    };
  }
  return {
    sanitized_text: `${e.title}。`, highlights: [],
    checks: ['customer_name', 'internal_id', 'amount', 'secret_or_pii'].map((item) => ({ item, found: 0, action: '' })),
  };
}
