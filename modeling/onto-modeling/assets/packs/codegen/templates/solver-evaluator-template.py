"""
关联operation：无

功能描述：求解评估器（声明式）：只声明取数映射与评估参数，不含计算代码，取数和路线指标由 Consumer 计算。

详细逻辑：
    本文件只能有本段 docstring、from __future__ import annotations 和下列名字的字面量赋值；
    不能写函数、类、其它 import、变量引用或运算，Consumer 只解析、从不执行本文件。
    由 /ontology-solver-evaluator 生成；修改后运行
    python3 .claude/scripts/solver_evaluator_contract.py ontology/scripts/<本文件>.py 自检。

输入参数：无

输出：
    无运行时输出；Consumer 按 TABLE_SOURCES 从图谱取数，结合下列参数计算各候选方案的路线指标。
"""

from __future__ import annotations

# ---- 加工单元 ----
# 填写对象实例的 ID 取值（不是属性名），须与 TABLE_SOURCES 映射出的 ID 列取值逐字一致。

# 一次加工单元在 hydrogenation_product_yield 表 RefineryUnitID 列中的取值
ATMOS_UNIT_ID = "<待填写：一次加工单元的 RefineryUnitID>"
# 求解输入可选的单元编号 → 该单元的 RefineryUnitID；每个可选单元一项，求解面板据此生成选项。
# 键写用户提问时对该单元的称呼（业务文档里的单元名称，不超过 32 个字符），不要写 ID：Consumer 的 Agent
# 按键匹配用户说法，求解结果文字也原样显示这个键；值才是 RefineryUnitID
DIESEL_UNIT_IDS = {
    "<待填写：用户对该单元的称呼>": "<待填写：对应单元的 RefineryUnitID>",
}
# 选定单元缺少收率或能耗数据时，改用这个单元的收率与能耗数据（RefineryUnitID 取值）；
# 加工单元表不回退：选定单元在 refinery_unit 表里没有行时评估失败
DEFAULT_DIESEL_UNIT_ID = "<待填写：回退单元的 RefineryUnitID>"
# 二次加工单元在 vgo_ht_unit 表 VGOUnitID 列中的取值
VGO_UNIT_ID = "<待填写：二次加工单元的 VGOUnitID>"

# ---- 收率列 ----
# 逻辑列名，不是对象属性：保持默认即可，TABLE_SOURCES 中收率表的同名逻辑列映射到收率属性。
# 改名时须同步改 TABLE_SOURCES，且不能与表的固定列重名。
YIELD_ATMOS = "yield_atmos"
YIELD_DIESEL_WITH_VGO = "yield_diesel_with_vgo"
YIELD_DIESEL_WITHOUT_VGO = "yield_diesel_without_vgo"
YIELD_VGO_WITH_VGO = "yield_vgo_with_vgo"
YIELD_VGO_WITHOUT_VGO = "yield_vgo_without_vgo"

# ---- 物料流向 ----
# 填写收率表 ProductName 列的取值，可以为空列表；列出的产品名在收率表里必须有对应行。
# ATMOS_DIESEL_FEED、ATMOS_HC_FEED 与 SOLVER_CONTEXT.route_stream_products 三组互不重叠，
# 同一产品只能出现在其中一组。
# 一次加工产出中进入可选单元的产品
ATMOS_DIESEL_FEED = ["<待填写：进入可选单元的产品名>"]
# 一次加工产出中进入二次加工单元的产品
ATMOS_HC_FEED = ["<待填写：进入二次加工单元的产品名>"]
# 可选单元产出中继续进入二次加工单元的中间产品
VGO_INTERMEDIATE_NAMES = ["<待填写：继续进入二次加工单元的中间产品名>"]

# ---- 能力与修正系数 ----
# 能力项名 → 日处理能力（正数）；SOLVER_CONTEXT.unit_capacity_keys 引用这里的能力项名
RULE_DAILY_CAPACITIES = {
    "<待填写：一次加工单元能力项名>": "<待填写：日处理能力，正数>",
    "<待填写：可选单元能力项名>": "<待填写：日处理能力，正数>",
    "<待填写：二次加工单元能力项名>": "<待填写：日处理能力，正数>",
}
# 来源名称 → 修正系数（正数）。来源名称与 crude_oil_yard 表 CrudeOilYard Name 列取值一致，
# 比较时忽略空白与全半角差异。每个来源恰好包含：每个修正前缀（见
# SOLVER_CONTEXT.correction_factor_prefix_by_unit）的 <前缀>_load_a、<前缀>_product_b，
# 以及 vgo_load_a、vgo_product_b，不多不少。
CORRECTION_FACTORS = {
    "<待填写：来源名称>": {
        "<待填写：修正前缀>_load_a": "<待填写：正数>",
        "<待填写：修正前缀>_product_b": "<待填写：正数>",
        "vgo_load_a": "<待填写：正数>",
        "vgo_product_b": "<待填写：正数>",
    },
}

# ---- 求解上下文 ----
SOLVER_CONTEXT = {
    # 两个路线选项的显示标签，取值互不相同；只作标签，不改变路线语义
    "option_routes": {"OPTION_0": "<待填写：选项 0 标签>", "OPTION_1": "<待填写：选项 1 标签>"},
    # 一次加工产出中由路线决策分配去向的产品（ProductName 取值，非空且不重复）
    "route_stream_products": ["<待填写：受路线决策分配的产品名>"],
    # 可选：一次加工其余产品 → 收率表未给出 LogicTankID 时使用的储罐 ID；没有则写 {}
    "atmospheric_product_tanks": {},
    # 有库存上下限约束的两个储罐（logic_tank 表 LogicTankID 取值，互不相同）
    "special_tanks": {
        "diesel": "<待填写：可选单元产品储罐 ID>",
        "vgo": "<待填写：二次加工产品储罐 ID>",
    },
    # 各单元使用 RULE_DAILY_CAPACITIES 中的哪一项能力
    "unit_capacity_keys": {
        "atmospheric": "<待填写：一次加工单元能力项名>",
        # 键与 DIESEL_UNIT_IDS 的键一一对应
        "diesel_by_unit": {"<待填写：用户对该单元的称呼>": "<待填写：可选单元能力项名>"},
        "vgo": "<待填写：二次加工单元能力项名>",
    },
    # 单元称呼 → 修正系数前缀；键与 DIESEL_UNIT_IDS 的键一一对应
    "correction_factor_prefix_by_unit": {"<待填写：用户对该单元的称呼>": "<待填写：修正前缀>"},
    # 生产工况：Consumer 求解引擎只支持 NO_JET 这一个工况，固定填写，不要改成业务文档里的工况名称
    "production_condition": "NO_JET",
    # 收率属性的书写方式：percent 为百分数（30 表示 30%），fraction 为小数（0.3 表示 30%）；
    # 按对象文档或样例数据确认，确认不了就问用户，不要猜
    "yield_unit": "<待填写：percent 或 fraction>",
}

# ---- 取数映射 ----
# 逻辑表 → {"object": 本体对象 key（ontology/object/ 下的文件名，即图谱 Label），
#           "fields": {逻辑列: 对象属性英文名}}。
# 7 张表都要声明；逻辑列只能用下面列出的名字，同一属性不能映射两次。
# 标「可选」的列在本体中没有对应属性时删除该行，Consumer 按 0 计；映射了的列在用到的行上取到空值时评估失败，
# 不会按 0 计。ID 列（*ID）对应的属性须为字符串类型。
# 本对象没有、但能经另一个对象查到的列，写成一跳关联（不要为此给对象补造属性）：
#     "逻辑列": {"lookup": 关联对象 key, "match": {本对象属性: 关联对象属性}, "field": 关联对象属性}
# Consumer 按 match 的全部属性等值匹配（不做类型转换）：匹配不到的行不参与评估，匹配到多条记录时评估失败。
TABLE_SOURCES = {
    "crude_oil_yard": {
        "object": "<待填写：来源对象 key>",
        "fields": {
            "OilYardID": "<待填写：来源 ID 属性>",
            "CrudeOilYard Name": "<待填写：来源名称属性>",
        },
    },
    "refinery_unit": {
        "object": "<待填写：加工单元对象 key>",
        "fields": {
            "RefineryUnitID": "<待填写：单元 ID 属性>",
            "price_per_unit": "<待填写：单位加工成本属性>",  # 可选
        },
    },
    "vgo_ht_unit": {
        "object": "<待填写：二次加工单元对象 key>",
        "fields": {
            "VGOUnitID": "<待填写：单元 ID 属性>",
            "price_per_unit": "<待填写：单位加工成本属性>",  # 可选
        },
    },
    "hydrogenation_product_yield": {
        "object": "<待填写：加工收率对象 key>",
        "fields": {
            "OilYardID": "<待填写：来源 ID 属性>",
            "RefineryUnitID": "<待填写：单元 ID 属性>",
            "ProductName": "<待填写：产品名属性>",
            "LogicTankID": "<待填写：储罐 ID 属性>",  # 可选
            "price_per_unit": "<待填写：产品单价属性>",  # 可选
            "yield_atmos": "<待填写：一次加工收率属性>",
            "yield_diesel_with_vgo": "<待填写：选该路线时的收率属性>",
            "yield_diesel_without_vgo": "<待填写：不选该路线时的收率属性>",
        },
    },
    "vgo_product_yield": {
        "object": "<待填写：二次加工收率对象 key>",
        "fields": {
            "OilYardID": "<待填写：来源 ID 属性>",
            "VGOUnitID": "<待填写：单元 ID 属性>",  # 必填；本对象没有时写成一跳关联
            "ProductName": "<待填写：产品名属性>",
            "LogicTankID": "<待填写：储罐 ID 属性>",  # 可选
            "price_per_unit": "<待填写：产品单价属性>",  # 可选
            "yield_vgo_with_vgo": "<待填写：选该路线时的收率属性>",
            "yield_vgo_without_vgo": "<待填写：不选该路线时的收率属性>",
        },
    },
    "logic_tank": {
        "object": "<待填写：储罐对象 key>",
        "fields": {
            "LogicTankID": "<待填写：储罐 ID 属性>",
            # AvailableCapacity_Tons 与 SafetyStockThrd_Tons 至少映射一个
            "AvailableCapacity_Tons": "<待填写：可用容量属性>",
            "CurrentCapacity_Tons": "<待填写：当前库存属性>",  # 可选
            "LowSafetyThrd_Tons": "<待填写：安全库存下限属性>",  # 可选
            "SafetyStockThrd_Tons": "<待填写：安全库存上限属性>",
        },
    },
    "energy_consume": {
        "object": "<待填写：能耗对象 key>",
        "fields": {
            # RefineryUnitID 与 VGOUnitID 至少映射一个
            "RefineryUnitID": "<待填写：加工单元 ID 属性>",
            "VGOUnitID": "<待填写：二次加工单元 ID 属性>",
            "UnitConsume_Value": "<待填写：单位能耗属性>",
            "price_per_unit": "<待填写：能源单价属性>",  # 可选
        },
    },
}
