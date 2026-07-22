export const DATA_SOURCE_TOUR_SCENE = 'data-source-config';
export const ONTOLOGY_ID_SYNC_BUTTON_LABEL = '同步知识包id';

export const DATA_SOURCE_TOUR_TARGETS = {
  provider: 'data-source-provider',
  url: 'data-source-url',
  scene: 'data-source-scene',
  token: 'data-source-token',
  neo4jUri: 'data-source-neo4j-uri',
  neo4jDatabase: 'data-source-neo4j-database',
  neo4jUser: 'data-source-neo4j-user',
  neo4jPassword: 'data-source-neo4j-password',
  workspaceProperty: 'data-source-workspace-property',
  workspaceValue: 'data-source-workspace-value',
  ontologyProperty: 'data-source-ontology-property',
  ontologyValue: 'data-source-ontology-value',
};

const step = (target, title, body) => ({
  sel: `[data-tour="${target}"]`,
  title,
  body,
});

const NL2SQL_TEMPLATE_STEPS = [
  step(
    DATA_SOURCE_TOUR_TARGETS.provider,
    'Provider · NL2SQL Template',
    '适合知识包中带有 SQL 模板的场景：优先匹配模板，未命中时再请求 NL2SQL 服务。',
  ),
  step(
    DATA_SOURCE_TOUR_TARGETS.url,
    '数据服务 URL',
    '填写 NL2SQL 服务根地址，例如 http://nl2sql:5600。不要追加 /api/v1/query，系统会自动拼接查询路径。',
  ),
  step(
    DATA_SOURCE_TOUR_TARGETS.scene,
    'Scene',
    '填写 NL2SQL 服务识别的数据源名称，通常就是目标数据库名。留空时系统会使用当前项目自动创建的评测库名称。',
  ),
  step(
    DATA_SOURCE_TOUR_TARGETS.token,
    'Token',
    'NL2SQL Template 的 Token 为必填项，填写服务要求的 Bearer Token。密钥只用于本次连接测试并同步到 consumer，modeling 不会保存明文。',
  ),
];

const NL2SQL_HTTP_STEPS = [
  step(
    DATA_SOURCE_TOUR_TARGETS.provider,
    'Provider · NL2SQL HTTP',
    '适合直接调用标准 NL2SQL HTTP 查询服务的场景，不经过知识包 SQL 模板匹配。',
  ),
  step(
    DATA_SOURCE_TOUR_TARGETS.url,
    '数据服务 URL',
    '填写 NL2SQL 服务根地址，例如 http://nl2sql:5600。不要追加 /api/v1/query，系统会自动拼接查询路径。',
  ),
  step(
    DATA_SOURCE_TOUR_TARGETS.scene,
    'Scene',
    '填写服务端用于选择数据库或业务场景的名称。留空时系统会使用当前项目自动创建的评测库名称。',
  ),
  step(
    DATA_SOURCE_TOUR_TARGETS.token,
    'Token',
    '如果服务启用了 Bearer 鉴权，请填写访问 Token；服务未启用鉴权时 Token 可留空。明文不会保存在 modeling 中。',
  ),
];

const NEO4J_STEPS = [
  step(
    DATA_SOURCE_TOUR_TARGETS.provider,
    'Provider · Neo4j',
    '图数据库Neo4j的数据源，数据格式为知识图谱，适合直接查询实体实例、关系及完整路径。连接测试会从 modeling 服务访问数据库，请确保网络地址对 modeling 可达。',
  ),
  step(
    DATA_SOURCE_TOUR_TARGETS.neo4jUri,
    'Neo4j URL',
    '填写 Bolt 连接地址，例如 bolt://{ip}:7687。',
  ),
  step(
    DATA_SOURCE_TOUR_TARGETS.neo4jDatabase,
    'Database',
    '填写业务图数据所在的数据库名；使用默认数据库时填写 neo4j。',
  ),
  step(
    DATA_SOURCE_TOUR_TARGETS.neo4jUser,
    'User',
    '填写拥有目标 Database 查询权限的 Neo4j 用户名，例如 neo4j。',
  ),
  step(
    DATA_SOURCE_TOUR_TARGETS.neo4jPassword,
    'Password',
    '填写该用户的当前密码。每次保存并测试连接时都需要重新填写；密码只用于测试和同步，modeling 不保存明文。',
  ),
  step(
    DATA_SOURCE_TOUR_TARGETS.workspaceProperty,
    'Workspace 隔离字段',
    '填写在neo4j中区分工作空间的properties属性名，建议默认为 d2o_workspace_id。',
  ),
  step(
    DATA_SOURCE_TOUR_TARGETS.workspaceValue,
    'Workspace 对应值',
    '填写在neo4j中区分工作空间的隔离值，防止不同工作空间的数据干扰，该值同时会作为 consumer 工作空间 ID。',
  ),
  step(
    DATA_SOURCE_TOUR_TARGETS.ontologyProperty,
    'Ontology 隔离字段',
    '填写在neo4j中区分同一工作空间下不同版本知识包的properties属性名，建议默认为 d2o_ontology_id。',
  ),
  step(
    DATA_SOURCE_TOUR_TARGETS.ontologyValue,
    'Ontology 对应值',
    `填写在neo4j中区分同一工作空间下不同版本知识包的隔离值。可点击输入框右侧“${ONTOLOGY_ID_SYNC_BUTTON_LABEL}”，从最新知识包 manifest.yaml 自动回填。`,
  ),
];

export function dataSourceTourSteps(provider) {
  if (provider === 'neo4j') return NEO4J_STEPS;
  if (provider === 'nl2sql_http') return NL2SQL_HTTP_STEPS;
  return NL2SQL_TEMPLATE_STEPS;
}
