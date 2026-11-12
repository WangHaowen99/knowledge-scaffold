// 删除项目时一并删除 Hub 包（develop !590 的删除确认，ADR-0098）：文字与判定照 develop App.jsx 的 removeProject。

export const DELETE_TEXT = '进行中的建模会话和自主构建将终止。工作区文件、对话和版本历史将永久删除，无法恢复。';
export const HUB_CHECKBOX = '同时删除本项目发布的知识包';
export const HUB_CONFIRM_TITLE = '确认删除项目及知识包';
export const HUB_CONFIRM_TEXT = '请确保消费和评测不再使用这些知识包。将物理删除全部正式版本、评测快照及共享资料，'
  + '使全部用户授权失效，包括其他服务发布到同一本体的版本。可能导致依赖任务失败，无法恢复。'
  + '已安装或下载的副本不会删除；系统未自动核实下游使用情况。';
export const HUB_USAGE_CHECKBOX = '已确认消费和评测不再使用这些知识包';
export const HUB_DELETE_BUTTON = '删除项目及知识包';

// 预览没取到（没登录统一账号、Hub 不可用）时按未发布处理，带上原因
export const previewFailed = (message) => ({ published: false, error: message });

// 勾选框下面的说明：没发布过 / 预览失败的原因
export const unpublishedNote = (preview) => (preview && !preview.published ? preview.error || '本项目尚未发布知识包' : '');

// Hub 删除进度：上次删到一半（删除中）或远端已删、本地还没删
export function progressNote(preview) {
  if (!preview?.state) return '';
  return `Hub 删除进度：${preview.state === 'deleted' ? '知识包已删除，待完成项目删除' : '结果待确认，请勾选后重试'}`;
}

export const packageLabel = (preview) => `${preview?.name || ''}（${preview?.ontology_id || ''}）`;
