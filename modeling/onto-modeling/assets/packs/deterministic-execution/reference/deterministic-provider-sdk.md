# 确定性 Handler 的受限 Provider SDK

生成 workspace_provider Handler 前必须阅读本说明。SDK 在真实确定性沙箱中提供，建模 Agent 的普通工作区不必能 import；不能因为本机导入失败而复制 SDK、硬编码数据或改用数据库驱动。

## 导入、调用与错误

```python
from shared_capabilities.ontology_script_sdk import DeterministicProviderClient

client = DeterministicProviderClient()
result = client.execute("count_search", {
    "element_class": "SyntheticItem",
    "element_type": "NODE",
    "filter_dict": {"category": {"eq": "alpha"}},
})
count = result["total"]
```

SyntheticItem/category/alpha 仅为合成示例，实际名称须取当前对象和数据映射。构造器只有可选关键字 timeout_seconds（默认30秒），不接收连接、工作区或凭据。execute(operation, arguments=None) 接受白名单操作名与JSON对象；没有 query、read、connect 或 execute_cypher 方法。不要读取环境变量或自行寻找 Socket。Parser 和 data_access=none 的 Handler 均不得调用此客户端。

失败抛出 SDK 的 DeterministicProviderCallError（RuntimeError 子类），code 表示稳定错误码，包括 provider_capability_unavailable、provider_arguments_invalid、provider_transport_error、provider_protocol_error、provider_timeout、provider_query_failed、provider_response_too_large。遵循确定性 CLI 骨架的非零退出约定；不能吞异常返回0、空数据或伪造成功。不能把 Provider 故障当作“无匹配”。不要把未经脱敏的异常详情写入输出。

## 返回结构：不要套用普通 Agent 图查询工具的包装

### 查询类别和真实数据绑定

element_class 必须对应 Provider 实际接受的类别。当前 Neo4j 后端对 NODE 按节点 label、对 EDGE 按关系 type 匹配；不要假设 SDK 自动把中文显示名、对象文件名或英文名翻译成物理类别。英文名也只是候选，最终以已确认的数据映射和实际数据源 schema 为准。生成前核对对象声明、映射资料、属性名及枚举值；映射不明确时请用户确认业务对应或数据管理员确认绑定，不让普通用户编写代码。

真实接入验收先检查数据源连通性、当前隔离范围和类型/字段绑定，再核对至少一项独立已知的非空合成数据结果及一个合法空结果。仅连接测试成功或一次返回0不足以证明绑定正确。schema 缺项也可能源于权限、范围、采样截断或整个数据集为空，应明确说明尚待核验，不能一律判定映射错误或自动换成另一类型。不要为通过验收修改数据标签、枚举、范围或硬编码预期数量。已确认绑定下合法无匹配仍按业务规则返回空结果；Provider异常或不完整结果不能冒充空结果。

Client 直接返回 Broker 的业务结果。除 schema 与详情查询外，通常是 GraphBackendResult 转为JSON后的对象：

```json
{"records": [], "total": 0, "truncated": false, "warnings": [], "payload": {}}
```

- count_search：完整计数在顶层 total；records 为空是正常的。不是 result.count，也不是 records 的长度。
- property_filter / sorted_search：当前页在 records。节点属性在每行 properties 内，例如 {"element_id":"synthetic-instance-1","properties":{"sample_id":"item_1","amount":100}}；实例ID与业务主键是不同概念。total 可能为 null 或仅代表当页数量，不能充当匹配总数。
- aggregate_search：通常 records[0].value 或 payload.result.value；total 表示返回记录数量，不是聚合值。空集按实际结果处理，不凭空把 null 改成0。
- get_nodes_schema / get_edges_schema / get_element_info：返回对应能力的对象，不承诺都有 records 包装；先核对需要的字段和类型。
- hop_search 等关系能力可能在 payload.result 中返回 nodes/edges；不要把关系查询结果当成属性行。

读取每个字段之前检查结构及类型；必要字段缺失应失败，不能用 get(..., 0) 掩盖协议不匹配。业务主键、物理属性和实例ID分别依据当前对象声明及真实数据绑定确定。

## 白名单与参数

下表是 execute 第二参数允许的全部键；不传 scope、workspace_id、ontology_id、连接或查询语言。标为“可省略”的字段可不传，其他参数显式提供；无筛选用 filter_dict={}，不裁剪属性用 return_properties=null。Broker按当前绑定注入隔离范围。

| operation | arguments |
|---|---|
| get_nodes_schema | element_types（字符串数组，可省略） |
| get_edges_schema | relation_types（字符串数组，可省略） |
| property_filter | element_class, element_type, filter_dict, return_properties, limit, offset |
| count_search | element_class, element_type, filter_dict |
| aggregate_search | element_class, element_type, target_property, agg, filter_dict |
| sorted_search | element_class, element_type, filter_dict, return_properties, sort_by, ascending, limit, offset |
| pattern_search | path_pattern, return_vars, limit, return_properties（可省略） |
| hop_search | entity_instance_id, hop_num, accurate_flag |
| shortest_path_search | start_label, start_props, end_label, end_props, max_depth, directed |
| list_logic_titles | entity_type（可省略） |
| get_logic_content | entity_instance_ids, logic_titles |
| get_element_info | element_instance_id, element_class（可省略）, element_type（可省略） |

常用类型：element_type 为 NODE 或 EDGE；element_class 为节点类别/关系类别；return_properties、sort_by 为字段名数组；ascending、directed、accurate_flag 为布尔值；limit/offset/hop_num/max_depth 为整数。agg 使用 SUM/AVG/MIN/MAX/COUNT。filter_dict 的精确匹配可用 {"字段":{"eq":"合成值"}}，也可用 {"字段":"合成值"} 表示等值；不支持 {"op":"eq","value":...} 包装。其他结构比较按平台能力使用，不能直接拼SQL或Cypher。复杂模式和逻辑内容接口应先用合成向量及真实绑定核实返回结构，不能编造字段。

## 分页、限额与完整性

Broker会收紧limit到绑定的max_result_rows（默认100、上限1000），不能假定请求1000就一定返回1000。查询有超时，单请求不超过256KiB、响应不超过4MiB。使用有界循环与明确的总行数/页数上限；超过上限或读取不完整必须报错，不返回局部统计。

需要逐条核验业务主键唯一性时，count_search不能替代属性读取。按已确认规则读取必要属性并检查空主键、重复主键；若用分页，offset按实际收到的行数递增。不能以“返回条数小于请求limit”判断结束，因为limit可能被收紧；要结合truncated和明确完整性检查。当前 Neo4j 分页的 truncated=true 表示本页达到返回上限，不保证下一页还有数据；例如总数恰好100且页大小100时，应允许继续读取 offset=100 的空页（truncated=false），再核对完整计数，不能因已读数量等于总数而直接失败。truncated=true且无新增行、同一实例重复出现或前后完整计数不一致应失败；需要稳定分页时使用带唯一业务主键排序的sorted_search。多次读取不承诺数据库快照，不能声称避免了所有并发变更。

## 建模验证与消费验证

Onto向量中的 provider_fixtures 用 operation、arguments、result 描述一次合成调用，result必须是上述Client实际返回形状，不能用普通工具包装。消费执行时从真实工作空间数据源读取，不得把fixture写进Handler。先在“确定性执行”面板保存、真实沙箱验证，再正常发布并在Consumer绑定数据源后调用。普通脚本local验证不等于Provider真实执行通过。
