export const PACKAGE_DATA_SOURCE_OPTIONS = [
  { value: 'nl2sql', label: 'NL2SQL HTTP/Template' },
  { value: 'neo4j', label: 'Neo4j' },
];

export const PACKAGE_DATA_SOURCE_HELP = '使用的数据源会影响 mapping/object-data-mappings.json 中实体 resource 的值。NL2SQL/MindsDB 的实体 resource 使用实体的来源 Sheet，Neo4j 的实体 resource 使用实体的 object key。为了知识包生成后可以直接使用，避免二次修改 mapping/object-data-mappings.json ，所以需要提前告知后续使用的数据源类型。';
