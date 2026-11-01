// 工作台对话里按 card_type 分出去的卡片：资料分析与蓝图（wb-materials、wb-blueprint）、自主构建（l4-run）
import { AUTOBUILD_CARDS } from './cards-autobuild.jsx';
import { MATERIAL_CARDS } from './cards-materials.jsx';

export const EXTRA_CARDS = { ...MATERIAL_CARDS, ...AUTOBUILD_CARDS };
