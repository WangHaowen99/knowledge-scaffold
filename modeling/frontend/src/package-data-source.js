export const PACKAGE_DATA_SOURCE_OPTIONS = [
  { value: 'nl2sql', label: 'NL2SQL HTTP/Template' },
  { value: 'neo4j', label: 'Neo4j' },
];

// 正文只说「为什么现在要选、选错什么后果」；实现细节收进 DETAIL，由界面折叠展示。
// 原文直接把 mapping 文件路径和字段名摆给业务专家看，那是写给实施工程师的。
export const PACKAGE_DATA_SOURCE_HELP = '请选择知识包最终要连接的数据库类型。'
  + '生成知识包时会按这个类型写好数据对应关系，选错需要重新生成才能使用，所以要提前确定。';

export const PACKAGE_DATA_SOURCE_DETAIL = '技术细节：数据源类型决定 mapping/object-data-mappings.json 中实体 resource 的取值——'
  + 'NL2SQL/MindsDB 用实体的来源 Sheet，Neo4j 用实体的 object key。';
