const COMMON_TITLE = '从一个业务问题开始';

export function getChatDataSourceStatus(ontology) {
  if (!ontology) {
    return {
      kind: 'automatic',
      kicker: '知识问答已就绪',
      title: COMMON_TITLE,
      description: '系统会根据问题自动选择知识包，也可手动指定；随后结合对应知识、可用数据源和运行工具给出可追溯的回答。',
      actionLabel: '',
    };
  }

  const businessData = ontology.business_data;
  if (!businessData || typeof businessData.configured !== 'boolean' || typeof businessData.ready !== 'boolean') {
    return {
      kind: 'unknown',
      kicker: '知识包已就绪，数据源状态待确认',
      title: COMMON_TITLE,
      description: '仍可进行知识问答；可前往当前知识包查看数据源配置。',
      actionLabel: '查看数据源',
    };
  }

  if (businessData.ready) {
    return {
      kind: 'ready',
      kicker: '知识包与数据源已配置',
      title: COMMON_TITLE,
      description: '我会结合当前知识包、已配置的数据源和运行工具给出可追溯的回答。',
      actionLabel: '',
    };
  }

  if (businessData.configured) {
    return {
      kind: 'not-ready',
      kicker: '知识包已就绪，数据源配置待完善',
      title: COMMON_TITLE,
      description: '仍可进行知识问答；完善数据源配置后可查询业务数据。',
      actionLabel: '完善数据源配置',
    };
  }

  return {
    kind: 'not-configured',
    kicker: '知识包已就绪，数据源待配置',
    title: COMMON_TITLE,
    description: '仍可进行知识问答；配置数据源后可查询业务数据。',
    actionLabel: '配置数据源',
  };
}
