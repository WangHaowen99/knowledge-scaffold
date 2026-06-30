const NODE_NAMES = {
  turn_stream: "本体消费智能体",
  router: "路由分析",
  simple_worker: "简单执行",
  dispatcher: "任务分发",
  worker: "任务执行",
  consolidator: "结果整合",
  postprocessor: "结果处理",
  clarification: "澄清确认",
};

/**
 * display name 
 * @param {string} nodeLabel - The node identifier (e.g., "parse_query")
 * @param {string} displayName - display name
 * @returns {string} - The display name to show
 */
export function getNodeDisplayName(nodeLabel, displayName = null) {
  // 优先使用 displayName
  if (displayName != null && displayName !== '') {
    return displayName;
  }

  if (nodeLabel && NODE_NAMES[nodeLabel]) {
    return NODE_NAMES[nodeLabel];
  }
  return nodeLabel || 'step';
}

/**
 * @param {string} status -  "running", "completed", "finished"
 * @returns {string}
 */
export function getStatusDisplayText(status) {
  const statusMap = {
    running: '运行中',
    streaming: '流式传输中',
    completed: '已完成',
    finished: '已完成',
    awaiting: '等待用户输入',
    awaiting_input: '等待用户输入',
    pending: '等待中',
    error: '错误',
    failed: '失败',
    aborted: '已中止',
    start: '开始',
  };

  return statusMap[status] || status || '未知';
}
