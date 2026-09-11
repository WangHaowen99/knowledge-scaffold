/* 冒烟：把每个场景 SSR 渲染一遍，抓组件未定义 / 渲染期异常。不参与生产构建。 */
globalThis.window = globalThis.window || { location: { hash: '' }, addEventListener() {}, removeEventListener() {} };

import { readFileSync } from 'node:fs';
import { renderToString } from 'react-dom/server';
import App from './App.jsx';
import LeftPane from './left/LeftPane.jsx';
import { initialUploadOptions } from './pages/CreatePage.jsx';
import { documentParagraphs, evidenceHighlight, evidenceLocationOffset } from './center/DocView.jsx';
import EvidenceLinks from './center/EvidenceLinks.jsx';
import { DiagnosisProgress, EvaluationSetupPanel, evaluationFocusRequest, evaluationScopeLabel, OptimizationProgress, RunsPanel } from './center/EvaluationView.jsx';
import {
  defaultExpandedEvaluationIds,
  EvaluationResultsList,
  toggleEvaluationResultExpansion,
} from '../EvaluationResultCard.jsx';
import FileView from './center/FileView.jsx';
import DiffView from './center/DiffView.jsx';
import { fitMermaidZoom } from './center/FlowView.jsx';
import { blockingGateIndex, stepCounts } from './shell/PhaseRail.jsx';
import GateView from './center/GateView.jsx';
import ImpactView from './center/ImpactView.jsx';
import { Block } from './right/blocks/Blocks.jsx';
import ActivityLine, { executionStartedAt, visibleRunActivity } from './right/ActivityLine.jsx';
import Composer, { shouldSendComposerMessage } from './right/Composer.jsx';
import { MaterialPurposeBatchCard, messageText } from './right/RightPane.jsx';
import RightPane from './right/RightPane.jsx';
import {
  nextReadyMaterialPurposeReview,
  packageUploadFollowUp,
  pendingMaterialPurposeReviews,
  store,
  actions,
  resolveBootstrapFocus,
  SCENES,
  SNAPSHOT_NEUTRAL_TOOLS,
  studioFocusFromLocation,
  shouldUseStaleRunTermination,
} from './lib/appStore.js';
import ProcessDrawer, {
  humanizeTraceSummary,
  newestTraceFirst,
  restoredVersionMessage,
} from './shell/ProcessDrawer.jsx';
import { userFacingText } from './lib/userFacingText.js';
import {
  evidenceLabel,
  evidenceHref,
  evidenceReferenceFromHref,
  linkEvidenceReferences,
  readableEvidenceAnchor,
} from './lib/evidence.js';
import {
  applyStudioFocus,
  clearStudioSearch,
  createStudioSession,
  filterStudioGraph,
  projectStudioLargeGraph,
  restoreStudioHistory,
  shouldSubmitStudioSearch,
  stageStudioSearch,
  studioGraphChain,
  studioGraphEdgeStyle,
  studioGraphNeighborhood,
  studioGraphScrollSurface,
  studioHistoryFromLocation,
  studioHistorySnapshot,
  studioHistoryUrl,
  studioKindCounts,
  studioSearchDraft,
  studioTextMatchesQuery,
  studioSummaryVisible,
  studioVisibleElementId,
  submitStudioSearch,
} from './studio/studio-model.js';
import {
  buildStudioRoutePresentation,
  separateStudioReciprocalRoutes,
  studioLabelPosition,
  studioRoundedPath,
  studioRouteHandles,
  studioRouteForNodePositions,
  studioRoutePoints,
} from './studio/studio-graph-geometry.js';
import {
  estimateStudioTextWidth,
  fallbackStudioLayout,
  studioBackendPositionsUsable,
  studioLayerBound,
  studioLayoutOptions,
  studioNodeSize,
} from './studio/studio-graph-layout.js';
import { applyHorizontalPacking } from './studio/webgl/studio-force-layout.worker.js';
import {
  studioGraphNodeAttributes,
  STUDIO_NODE_CLICK_DISTANCE,
  STUDIO_NODE_DRAG_THRESHOLD,
} from './studio/StudioGraph.jsx';
import {
  defaultFusionOutput,
  dispositionFor,
  availableFusionScopes,
  duplicateFusionPackageIds,
  fusionConceptGroups,
  fusionScopeCount,
  fusionScopeProblems,
  ledgerEntries,
  planStage,
  qualityRows,
  unresolvedEntries,
} from './fusion/fusion-model.js';
import {
  DecisionControl,
  FusionBuilder,
  FusionPlan,
  packageFilesFromSelection,
} from './fusion/FusionView.jsx';
import { StudioAccessBanner, StudioKindTabs, studioFieldEditable } from './studio/StudioView.jsx';
import {
  ambientNodeOffset,
  smoothGraphPosition,
  WebGLGraphRuntime,
} from './studio/webgl/StudioWebGLRuntime.js';
import {
  operationFlowMode,
  operationFlowScrollCanvas,
  operationFlowWheelZoom,
  operationFlowZoom,
  operationStepsDiagram,
  projectOperationFlowDiagram,
} from './studio/operation-flow/operationFlowModel.js';

let bad = 0;
try {
  const storeSource = readFileSync('src/workbench/lib/appStore.js', 'utf8');
  const appSource = readFileSync('src/workbench/App.jsx', 'utf8');
  const apiSource = readFileSync('src/workbench/studio/studio-api.js', 'utf8');
  const studioSource = readFileSync('src/workbench/studio/StudioView.jsx', 'utf8');
  if (!storeSource.includes('const initialStudioFocus = initialProjectId ? studioFocusFromLocation() || { ...DEFAULT_STUDIO_FOCUS } : null')
    || !storeSource.includes('prefetchStudioBootstrap(initialProjectId, { limit: 200 })')
    || !appSource.includes("store.get().focus.kind === 'studio'")
    || !appSource.includes('prefetchStudioBootstrap(projectId, { limit: 200 })')
    || !readFileSync('src/App.jsx', 'utf8').includes('window.location.assign(projectWorkbenchUrl(id))')
    || !apiSource.includes('const bootstrapRequests = new Map()')
    || !apiSource.includes("if (cache === 'reload') invalidateStudioBootstrap(projectId)")
    || !studioSource.includes("loadStudio({ cache: 'reload' })")) {
    throw new Error('直接进入 Studio 时没有优先加载或复用 bootstrap');
  }
  console.log('✓ Studio 快速入口优先预取并复用 bootstrap');
} catch (e) { console.log('✗ Studio 快速入口:', e.message); bad++; }

try {
  const smoothed = smoothGraphPosition({ x: 0, y: 0 }, { x: 100, y: 50 }, 150);
  const offsets = Array.from({ length: 120 }, (_, index) => (
    ambientNodeOffset('synthetic:node', 'synthetic:component', index / 10, 0.5)
  ));
  const maximumScreenX = Math.max(...offsets.map((offset) => Math.abs(offset.x * 0.5)));
  const maximumScreenY = Math.max(...offsets.map((offset) => Math.abs(offset.y * 0.5)));
  if (smoothed.x <= 55 || smoothed.x >= 70
    || smoothed.y <= 27 || smoothed.y >= 35
    || maximumScreenX > 3.2
    || maximumScreenY > 4.0) {
    throw new Error('节点展示插值或稳定后漂移超出安静动态范围');
  }
  console.log('✓ Studio 节点移动使用平滑插值与低幅慢速漂移');
} catch (e) { console.log('✗ Studio 节点移动参数:', e.message); bad++; }

try {
  const initial = { ...createStudioSession(), query: '旧查询', queryDraft: '  新查询  ', selectedElementId: 'rule:a' };
  const staged = stageStudioSearch(initial, '继续输入');
  const submitted = submitStudioSearch(initial);
  const cleared = clearStudioSearch(submitted);
  if (staged.query !== '旧查询'
    || studioSearchDraft(staged) !== '继续输入'
    || submitted.query !== '新查询'
    || submitted.queryDraft !== '新查询'
    || cleared.query !== ''
    || cleared.queryDraft !== ''
    || cleared.selectedElementId !== ''
    || !shouldSubmitStudioSearch({ key: 'Enter' })
    || shouldSubmitStudioSearch({ key: 'Enter', nativeEvent: { isComposing: true } })
    || shouldSubmitStudioSearch({ key: 'a' })
    || !studioTextMatchesQuery('合成库存盘点规则', '库房盘点')
    || !studioTextMatchesQuery('合成库存盘点规则', '库存盘点')
    || studioTextMatchesQuery('合成库存盘点规则', '库房')
    || studioTextMatchesQuery('合成库存盘点规则', '客户识别')) {
    throw new Error('搜索草稿在提交前影响了正式查询，或 Enter 组合输入判断错误');
  }
  console.log('✓ Studio 搜索仅在 Enter 或按钮提交后生效');
} catch (e) { console.log('✗ Studio 搜索提交:', e.message); bad++; }

try {
  const source = readFileSync('src/workbench/studio/StudioGraph.jsx', 'utf8');
  if (/if \(!filtered\.nodes\.length\)\s*\{\s*return/s.test(source)
    || !source.includes('studio-graph-empty-overlay')
    || !source.includes('graphFullscreen && (')) {
    throw new Error('空搜索结果会卸载全屏图谱画布');
  }
  console.log('✓ Studio 全屏空搜索保留图谱画布');
} catch (e) { console.log('✗ Studio 全屏空搜索:', e.message); bad++; }

try {
  let contextLostCount = 0;
  let preventedCount = 0;
  const runtime = new WebGLGraphRuntime({ onContextLost: () => { contextLostCount += 1; } });
  const event = { preventDefault: () => { preventedCount += 1; } };
  runtime.handleContextLost(event);
  runtime.scene = {
    nodes: [{ id: 'cluster:synthetic', kind: 'cluster' }],
    edges: Array.from({ length: 80 }, (_, index) => ({ id: `edge:${index}` })),
  };
  runtime.forceStatus = 'settled';
  if (runtime.edgeLabelBudget(0.8) !== 10 || runtime.edgeLabelBudget(1) !== 16) {
    throw new Error('聚合图谱没有使用收敛后的边标签预算');
  }
  runtime.destroyed = true;
  runtime.handleContextLost(event);
  if (contextLostCount !== 1 || preventedCount !== 2) {
    throw new Error('主动销毁 WebGL 时仍被误判为渲染器不可用');
  }
  console.log('✓ Studio WebGL 主动销毁不触发不可用回退');
} catch (e) { console.log('✗ Studio WebGL 生命周期:', e.message); bad++; }

try {
  const graphSource = readFileSync('src/workbench/studio/webgl/StudioWebGLGraph.jsx', 'utf8');
  const runtimeSource = readFileSync('src/workbench/studio/webgl/StudioWebGLRuntime.js', 'utf8');
  if (!graphSource.includes('onScenePresented')
    || !graphSource.includes('!presented &&')
    || !runtimeSource.includes('notifyScenePresented()')
    || (runtimeSource.match(/window\.requestAnimationFrame/g) || []).length < 2) {
    throw new Error('WebGL 初始化遮罩在场景首帧呈现前被移除');
  }
  console.log('✓ Studio WebGL 场景首帧后移除初始化遮罩');
} catch (e) { console.log('✗ Studio WebGL 首帧遮罩:', e.message); bad++; }

try {
  const steps = operationStepsDiagram('operation:a', '操作 A', ['步骤一', '步骤二']);
  const projected = projectOperationFlowDiagram({
    id: 'process-a',
    title: '流程 A',
    nodes: [
      { id: 'start', label: '开始', kind: 'start' },
      { id: 'action', label: '执行', kind: 'action' },
      { id: 'long-action', label: '读取当前业务上下文并根据已经确认的规则计算适用范围，完成结果校验后继续执行后续步骤', kind: 'action' },
    ],
    edges: [
      { id: 'edge-a', from: 'start', to: 'action', kind: 'sequence' },
      { id: 'edge-b', from: 'action', to: 'long-action', kind: 'sequence' },
    ],
    layout: { direction: 'LR', positions: {} },
  }, ['action']);
  const scrollCanvas = operationFlowScrollCanvas({
    ...projected,
    nodes: projected.nodes.map((node, index) => ({ ...node, position: { x: -20, y: index * 680 - 40 } })),
  }, 56, { width: 1600, height: 900 });
  const flowPanelSource = readFileSync('src/workbench/studio/operation-flow/OperationFlowPanel.jsx', 'utf8');
  if (operationFlowMode({ defaultView: 'business-flow', diagrams: [{}], steps: ['步骤一'] }) !== 'business-flow'
    || operationFlowMode({ diagrams: [], steps: ['步骤一'] }) !== 'operation-steps'
    || steps.nodes.length !== 4
    || projected.nodes.find((node) => node.id === 'action')?.data.focused !== true
    || projected.nodes.find((node) => node.id === 'long-action')?.data.estimatedHeight <= 56
    || projected.nodes.find((node) => node.id === 'long-action')?.style.height !== undefined
    || scrollCanvas.height < 1400
    || scrollCanvas.width !== 1600
    || Math.min(...scrollCanvas.nodes.map((node) => node.position.x)) < 600
    || scrollCanvas.nodes.some((node) => node.position.x < 0 || node.position.y < 0)
    || operationFlowZoom(0.1) !== 0.5
    || operationFlowZoom(2.4) !== 2
    || operationFlowWheelZoom(1, -100) !== 1.25
    || operationFlowWheelZoom(1, 100) !== 0.75
    || !flowPanelSource.includes("addEventListener('wheel', handleFullscreenWheel, { passive: false })")
    || !flowPanelSource.includes("removeEventListener('wheel', handleFullscreenWheel)")
    || flowPanelSource.includes('onWheel={(event) =>')
    || projected.edges.some((edge) => edge.type !== 'step')
    || projected.edges[0]?.source !== 'start') {
    throw new Error('操作步骤、默认视图或业务流程高亮投影不一致');
  }
  console.log('✓ Studio 操作步骤与所在业务流程投影');
} catch (e) { console.log('✗ Studio 操作流程投影:', e.message); bad++; }

try {
  const graph = {
    nodes: [
      { id: 'operation:a', kind: 'operation', label: '操作 A' },
      { id: 'rule:a', kind: 'rule', label: '规则 A' },
      { id: 'metric:a', kind: 'metric', label: '指标 A' },
      { id: 'object:a', kind: 'object', label: '对象 A' },
    ],
    edges: [
      { id: 'e1', source: 'operation:a', target: 'rule:a', kind: 'uses_rule' },
      { id: 'e2', source: 'operation:a', target: 'metric:a', kind: 'uses_metric' },
      { id: 'e3', source: 'metric:a', target: 'object:a', kind: 'uses_object' },
    ],
  };
  const filtered = filterStudioGraph(graph, '', '', [], ['rule']);
  const neighborhood = studioGraphNeighborhood('metric:a', graph.edges);
  const metricChain = studioGraphChain('metric', graph.nodes, graph.edges);
  const byId = new Map(graph.nodes.map((node) => [node.id, node]));
  const metricStyle = studioGraphEdgeStyle(graph.edges[2], byId);
  const unknownStyle = studioGraphEdgeStyle({ kind: 'unknown_relation' }, byId);
  const objectNodes = filterStudioGraph(graph, 'object', '').nodes;
  const searched = filterStudioGraph(graph, '', '操作 A');
  const attributes = studioGraphNodeAttributes({ id: 'object:a', kind: 'object' }, {
    'object:a': { values: { fields: [{ key: 'field_a', cnName: '字段 A', type: 'string' }] } },
  });
  const scriptAttributes = studioGraphNodeAttributes({ id: 'script:a', kind: 'script' }, {
    'script:a': { values: { fields: [{ key: 'invalid' }] } },
  });
  const scrollSurface = studioGraphScrollSurface([
    { position: { x: -120, y: -60 }, width: 160, height: 72 },
    { position: { x: 1180, y: 940 }, width: 180, height: 80 },
  ], 1.5, { width: 1200, height: 800 });
  const decoratedSurface = studioGraphScrollSurface([
    { position: { x: 0, y: 0 }, width: 160, height: 72 },
  ], 2, { width: 800, height: 600 }, 80, [
    { x: 30, y: 1800, width: 120, height: 28 },
  ]);
  const largeSource = {
    nodes: Array.from({ length: 12 }, (_, index) => ({
      id: `object:synthetic_${index}`, kind: 'object', label: `合成对象 ${index}`, x: index * 40, y: index % 3 * 50,
    })),
    edges: Array.from({ length: 11 }, (_, index) => ({
      id: `edge:synthetic_${index}`, source: `object:synthetic_${index}`, target: `object:synthetic_${index + 1}`,
      kind: 'object_relation', label: '合成关系',
    })),
  };
  const clustered = projectStudioLargeGraph(largeSource, { threshold: 8, maxClusterSize: 4 });
  const firstClusterId = Object.keys(clustered.clusters)[0];
  const expanded = projectStudioLargeGraph(largeSource, {
    threshold: 8, maxClusterSize: 4, expandedClusterIds: [firstClusterId],
  });
  const searchedLarge = projectStudioLargeGraph(largeSource, { threshold: 8, maxClusterSize: 4, query: '合成' });
  const thresholdSource = {
    nodes: Array.from({ length: 500 }, (_, index) => ({
      id: `rule:threshold_${index}`,
      kind: 'rule',
      label: `合成阈值规则 ${index + 1}`,
    })),
    edges: Array.from({ length: 600 }, (_, index) => ({
      id: `edge:threshold_${index}`,
      source: `rule:threshold_${index % 500}`,
      target: `rule:threshold_${(index + 1) % 500}`,
      kind: 'uses_rule',
      label: '合成关系',
    })),
  };
  const thresholdClustered = projectStudioLargeGraph(thresholdSource);
  const unlinkedBelowThreshold = projectStudioLargeGraph({
    nodes: thresholdSource.nodes.slice(0, 499), edges: [],
  });
  const mediumSparse = projectStudioLargeGraph({
    nodes: thresholdSource.nodes.slice(0, 250),
    edges: Array.from({ length: 300 }, (_, index) => ({
      id: `edge:medium-sparse_${index}`,
      source: `rule:threshold_${index % 250}`,
      target: `rule:threshold_${(index + 1) % 250}`,
    })),
  });
  const lowDensity = projectStudioLargeGraph({
    nodes: thresholdSource.nodes.slice(0, 200),
    edges: Array.from({ length: 180 }, (_, index) => ({
      id: `edge:low-density_${index}`,
      source: `rule:threshold_${index % 200}`,
      target: `rule:threshold_${(index + 1) % 200}`,
    })),
  });
  const mediumDense = projectStudioLargeGraph({
    nodes: thresholdSource.nodes.slice(0, 200),
    edges: Array.from({ length: 400 }, (_, index) => ({
      id: `edge:medium-dense_${index}`,
      source: `rule:threshold_${index % 200}`,
      target: `rule:threshold_${(index + 1) % 200}`,
    })),
  });
  const largerSparse = projectStudioLargeGraph({
    nodes: thresholdSource.nodes.slice(0, 300),
    edges: Array.from({ length: 360 }, (_, index) => ({
      id: `edge:larger-sparse_${index}`,
      source: `rule:threshold_${index % 300}`,
      target: `rule:threshold_${(index + 1) % 300}`,
    })),
  });
  if (filtered.nodes.some((node) => node.kind === 'rule') || filtered.edges.some((edge) => edge.id === 'e1')
    || neighborhood.edges.size !== 2 || metricChain.edges.size !== 2
    || metricStyle.color !== '#5935a4' || metricStyle.dash
    || unknownStyle.color !== '#98a2b1'
    || searched.nodes.length !== 3 || searched.edges.length !== 2
    || searched.nodes.some((node) => node.id === 'object:a')
    || searched.edges.some((edge) => edge.id === 'e3')
    || attributes.length !== 1 || attributes[0].key !== 'field_a' || scriptAttributes.length !== 0
    || !studioSummaryVisible('object:a', '')
    || studioSummaryVisible('object:a', 'object:a')
    || !studioSummaryVisible('object:b', 'object:a')
    || studioSummaryVisible('operation:a', '', { activeKind: 'operation', selectedKind: 'operation' })
    || !studioSummaryVisible('operation:a', '', { activeKind: '', selectedKind: 'operation' })
    || scrollSurface.width <= 2000
    || scrollSurface.height <= 1500
    || scrollSurface.viewport.zoom !== 1.5
    || scrollSurface.viewport.x <= 0
    || decoratedSurface.height < 3800
    || !clustered.clustered || clustered.nodes.length >= largeSource.nodes.length
    || !clustered.nodes.every((node) => node.kind === 'cluster')
    || !clustered.edges.some((edge) => edge.source.startsWith('cluster:') && edge.target.startsWith('cluster:'))
    || !expanded.clusters[firstClusterId]?.expanded
    || expanded.nodes.filter((node) => node.kind !== 'cluster').length !== 4
    || searchedLarge.clustered || searchedLarge.nodes.length !== largeSource.nodes.length
    || !thresholdClustered.clustered || thresholdClustered.nodes.length > 30
    || unlinkedBelowThreshold.clustered
    || !mediumSparse.clustered
    || lowDensity.clustered
    || !mediumDense.clustered
    || !largerSparse.clustered
    || studioVisibleElementId('operation:a', objectNodes) !== ''
    || studioVisibleElementId('object:a', objectNodes) !== 'object:a'
    || STUDIO_NODE_CLICK_DISTANCE < 4
    || STUDIO_NODE_DRAG_THRESHOLD < STUDIO_NODE_CLICK_DISTANCE) {
    throw new Error('Studio 图谱过滤、链路或关系颜色语义不一致');
  }
  console.log('✓ Studio 图谱过滤、链路与关系颜色');
} catch (e) { console.log('✗ Studio 图谱派生状态:', e.message); bad++; }

try {
  const compactOptions = studioLayoutOptions(8);
  const largeOptions = studioLayoutOptions(49);
  const objectSize = studioNodeSize({ kind: 'object', id: 'object:a', label: '对象 A' }, 1);
  const operationSize = studioNodeSize({ kind: 'operation', id: 'operation:a', label: '操作 A' }, 1);
  const ruleSize = studioNodeSize({ kind: 'rule', id: 'rule:a', label: '规则 A' }, 1);
  const metricSize = studioNodeSize({ kind: 'metric', id: 'metric:a', label: '指标 A' }, 1);
  const scriptSize = studioNodeSize({ kind: 'script', id: 'script:a', label: '脚本 A' }, 1);
  const longObjectSize = studioNodeSize({ kind: 'object', id: 'object:long', label: '需要完整说明的超长合成业务对象名称' }, 0);
  const longOperationSize = studioNodeSize({ kind: 'operation', id: 'operation:long', label: '需要完整说明的超长合成业务操作名称' }, 0);
  const longScriptSize = studioNodeSize({ kind: 'script', id: 'script:long', label: '需要完整说明的超长合成处理脚本名称' }, 0);
  const unlinkedRules = Array.from({ length: 33 }, (_, index) => ({
    id: `rule:synthetic_${index}`,
    kind: 'rule',
    label: `合成规则 ${index + 1}`,
    width: 114,
    height: 68,
    x: index * 180,
    y: 0,
    degree: 0,
  }));
  const seededRules = fallbackStudioLayout(unlinkedRules);
  applyHorizontalPacking(unlinkedRules, [], 1.8);
  const packedRows = new Set(unlinkedRules.map((node) => Math.round(node.y)));
  const seededRows = new Set(seededRules.map((node) => Math.round(node.position.y)));
  if (compactOptions['elk.layered.layering.coffmanGraham.layerBound'] !== undefined
    || compactOptions['elk.aspectRatio'] !== '1.8'
    || compactOptions['elk.spacing.componentComponent'] !== '80'
    || studioLayerBound(10) !== null
    || studioLayerBound(11) !== 4
    || studioLayerBound(49) !== 4
    || largeOptions['elk.layered.layering.strategy'] !== 'COFFMAN_GRAHAM'
    || largeOptions['elk.layered.layering.coffmanGraham.layerBound'] !== '4'
    || objectSize.width <= Math.max(operationSize.width, ruleSize.width, metricSize.width, scriptSize.width)
    || objectSize.width !== objectSize.height
    || objectSize.height >= 168
    || operationSize.width <= operationSize.height
    || operationSize.height >= 92
    || ruleSize.width <= ruleSize.height
    || ruleSize.height >= 88
    || metricSize.width <= metricSize.height
    || metricSize.height >= 82
    || scriptSize.width <= scriptSize.height
    || scriptSize.height >= 88
    || longObjectSize.width < 180 || longObjectSize.width !== longObjectSize.height
    || longOperationSize.width !== 180 || longOperationSize.height <= 72 || longOperationSize.height > 126
    || longScriptSize.width !== 160 || longScriptSize.height <= 68 || longScriptSize.height > 122
    || studioBackendPositionsUsable(unlinkedRules.map((node, index) => ({ ...node, x: index * 180, y: 0 })))
    || seededRows.size < 4
    || packedRows.size < 4
    || estimateStudioTextWidth('合成对象') <= estimateStudioTextWidth('abcd')) {
    throw new Error('横向分量装箱或大图动态分栏参数不一致');
  }
  console.log('✓ Studio 分量横向装箱与大图动态分栏');
} catch (e) { console.log('✗ Studio 自适应布局:', e.message); bad++; }

try {
  const routes = {
    a: [{ x: 0, y: 0 }, { x: 60, y: 0 }, { x: 60, y: 50 }, { x: 120, y: 50 }],
    b: [{ x: 0, y: 0 }, { x: 60, y: 0 }, { x: 60, y: -50 }, { x: 120, y: -50 }],
  };
  const presentation = buildStudioRoutePresentation(routes);
  const rounded = studioRoundedPath(routes.a);
  if (!presentation.sharedEdges.a.includes('b') || !presentation.labelAnchors.a || !presentation.labelAnchors.b
    || !rounded.includes(' Q ')) {
    throw new Error('共享路由没有生成分支标签锚点或圆角路径');
  }
  console.log('✓ Studio 共享路由标签与圆角边');
} catch (e) { console.log('✗ Studio 路由几何:', e.message); bad++; }

try {
  const routes = {
    'edge:a': [{ x: 100, y: 40 }, { x: 200, y: 40 }],
    'edge:b': [{ x: 200, y: 41 }, { x: 100, y: 41 }],
  };
  const presentation = buildStudioRoutePresentation(routes, {
    'edge:a': '合成关系 A',
    'edge:b': '合成关系 B',
  });
  const firstLabel = studioLabelPosition(routes['edge:a'], presentation.labelAnchors['edge:a']);
  const secondLabel = studioLabelPosition(routes['edge:b'], presentation.labelAnchors['edge:b']);
  const reverseHandles = studioRouteHandles(routes['edge:b'], {
    position: { x: 200, y: 0 }, width: 160,
  }, {
    position: { x: -60, y: 0 }, width: 160,
  });
  if (firstLabel.y !== 40 || secondLabel.y !== 41
    || reverseHandles.sourceHandle !== 'source-left'
    || reverseHandles.targetHandle !== 'target-right') {
    throw new Error('关系标签没有保持在所属线条，或反向关系端点错误');
  }
  console.log('✓ Studio 在线标签与反向关系端点');
} catch (e) { console.log('✗ Studio 标签与端点几何:', e.message); bad++; }

try {
  const moved = studioRouteForNodePositions(
    [{ x: 100, y: 40 }, { x: 220, y: 40 }],
    { position: { x: 0, y: 0 } },
    { position: { x: 220, y: 0 } },
    { x: 20, y: 30 },
    { x: 220, y: 0 },
  );
  const orthogonal = moved.slice(0, -1).every((point, index) => (
    point.x === moved[index + 1].x || point.y === moved[index + 1].y
  ));
  if (moved[0].x !== 120 || moved[0].y !== 70
    || moved[moved.length - 1].x !== 220 || moved[moved.length - 1].y !== 40
    || !orthogonal) {
    throw new Error('节点位移没有同步到关系端点，或变形后的关系不再正交');
  }
  console.log('✓ Studio 节点拖动实时带动正交关系路由');
} catch (e) { console.log('✗ Studio 拖动关系路由:', e.message); bad++; }

try {
  const reciprocal = separateStudioReciprocalRoutes({
    'edge:a': [{ x: 100, y: 45 }, { x: 220, y: 45 }],
    'edge:b': [{ x: 220, y: 45 }, { x: 100, y: 45 }],
  }, [
    { id: 'edge:a', source: 'object:a', target: 'object:b' },
    { id: 'edge:b', source: 'object:b', target: 'object:a' },
  ]);
  const renderedFirst = studioRoutePoints(reciprocal['edge:a'], 100, 45, 220, 45);
  const renderedSecond = studioRoutePoints(reciprocal['edge:b'], 220, 45, 100, 45);
  const labelRoute = [{ x: 0, y: 100 }, { x: 240, y: 100 }];
  const labels = buildStudioRoutePresentation({ label: labelRoute }, { label: '合成指标关系' }, [{
    id: 'operation:a', position: { x: 80, y: 72 }, width: 160, height: 58,
  }]);
  const labelPosition = studioLabelPosition(labelRoute, labels.labelAnchors.label);
  if (renderedFirst[0].y === renderedSecond[0].y
    || (labelPosition.x >= 72 && labelPosition.x <= 248
      && labelPosition.y >= 64 && labelPosition.y <= 138)) {
    throw new Error('互为反向的关系仍然共线，或关系标签仍被节点遮挡');
  }
  console.log('✓ Studio 互为反向关系分轨且标签避开节点');
} catch (e) { console.log('✗ Studio 反向关系与节点避让:', e.message); bad++; }

try {
  const routes = {
    label: [{ x: 0, y: 100 }, { x: 240, y: 100 }],
    crossing: [{ x: 120, y: 60 }, { x: 120, y: 140 }],
  };
  const presentation = buildStudioRoutePresentation(routes, { label: '合成操作关系' });
  const position = studioLabelPosition(routes.label, presentation.labelAnchors.label);
  if (position.x >= 75 && position.x <= 165 && position.y >= 55 && position.y <= 145) {
    throw new Error('关系标签仍然落在其他操作关系线的穿越区域');
  }
  console.log('✓ Studio 关系标签沿自身路径换位并避开其他关系线');
} catch (e) { console.log('✗ Studio 标签与关系线避让:', e.message); bad++; }

try {
  const routes = {
    own: [{ x: 0, y: 100 }, { x: 240, y: 100 }],
    nearby: [{ x: 0, y: 124 }, { x: 240, y: 124 }],
  };
  const presentation = buildStudioRoutePresentation(routes, { own: '合成关系标签' });
  const position = studioLabelPosition(routes.own, presentation.labelAnchors.own);
  const ownDistance = Math.abs(position.y - 100);
  const nearbyDistance = Math.abs(position.y - 124);
  if (ownDistance + 4 > nearbyDistance || ownDistance > 26) {
    throw new Error('关系标签没有保持最近于所属线，或仍使用过远偏移轨道');
  }
  console.log('✓ Studio 标签保持最近于所属关系线');
} catch (e) { console.log('✗ Studio 标签归属约束:', e.message); bad++; }

try {
  const route = [{ x: 100, y: 100 }, { x: 240, y: 100 }];
  const presentation = buildStudioRoutePresentation({ relation: route }, {
    relation: '较长的合成关系标签文本',
  }, [
    { id: 'object:a', position: { x: 0, y: 60 }, width: 100, height: 80 },
    { id: 'object:b', position: { x: 240, y: 60 }, width: 100, height: 80 },
  ]);
  const position = studioLabelPosition(route, presentation.labelAnchors.relation);
  if (Math.abs(position.y - 100) > 38) {
    throw new Error('两端节点之间的关系标签偏离自身关系线过远');
  }
  console.log('✓ Studio 端点间长标签保持在自身关系线近距离轨道');
} catch (e) { console.log('✗ Studio 标签距离约束:', e.message); bad++; }

try {
  const counts = studioKindCounts([
    { kind: 'object' }, { kind: 'object' }, { kind: 'operation' }, { kind: 'rule' }, { kind: 'metric' },
  ]);
  const html = renderToString(<StudioKindTabs value="operation" kinds={[
    { kind: 'object', label: '对象' },
    { kind: 'operation', label: '操作' },
    { kind: 'rule', label: '规则' },
    { kind: 'metric', label: '指标' },
    { kind: 'script', label: '脚本' },
  ]} elements={[]} counts={counts} />);
  if (!html.includes('按知识类型筛选') || !html.includes('<span>全部</span>')
    || !html.includes('<span>对象</span>') || !html.includes('<span>操作</span>')
    || !html.includes('<span>规则</span>') || !html.includes('<span>指标</span>')
    || !html.includes('<span>脚本</span>') || (html.match(/role="tab"/g) || []).length !== 6
    || !html.includes('<span>对象</span><small>2</small>')
    || !html.includes('<span>全部</span><small>5</small>')
    || !html.includes('aria-selected="true"')) {
    throw new Error('Studio 类型没有以并列切换项完整展示');
  }
  console.log('✓ Studio 知识类型并列切换');
} catch (e) { console.log('✗ Studio 类型切换:', e.message); bad++; }

try {
  const html = renderToString(<Composer />);
  if (!shouldSendComposerMessage({ key: 'Enter' })
    || shouldSendComposerMessage({ key: 'Enter', shiftKey: true })
    || shouldSendComposerMessage({ key: 'Enter', isComposing: true })
    || shouldSendComposerMessage({ key: 'Enter', nativeEvent: { isComposing: true } })
    || !html.includes('aria-keyshortcuts="Enter"')) {
    throw new Error('输入框没有正确区分发送、换行和输入法组合状态');
  }
  console.log('✓ 输入框 Enter 发送且保留 Shift+Enter 换行');
} catch (e) { console.log('✗ 输入框键盘发送:', e.message); bad++; }

try {
  const editable = { capabilities: { update: true } };
  const readonlyLease = { capabilities: { update: false } };
  if (!studioFieldEditable(editable, { key: 'description' })
    || studioFieldEditable(editable, { key: 'evidence', readOnly: true })
    || studioFieldEditable(readonlyLease, { key: 'description' })) {
    throw new Error('Studio 字段没有同时服从字段约束和服务端更新权限');
  }
  console.log('✓ Studio 租约只读时不渲染字段编辑器');
} catch (e) { console.log('✗ Studio 字段权限:', e.message); bad++; }

try {
  const html = renderToString(<StudioAccessBanner access={{
    role: 'modeler',
    lease: { mode: 'review', owner: 'alice' },
  }} />);
  if (!html.includes('当前有待确认修改') || !html.includes('确认或退回后才能继续编辑')
    || html.includes('首次编辑会接管')) {
    throw new Error('冻结候选仍被显示为可接管租约');
  }
  console.log('✓ Studio 冻结候选优先提示处理确认');
} catch (e) { console.log('✗ Studio 冻结候选提示:', e.message); bad++; }

try {
  const html = renderToString(<EvaluationSetupPanel projectId="project-evaluation" onSaved={() => {}} />);
  if (!html.includes('wb-eval-data-source') || !html.includes('mwr') || !html.includes('加载中')) {
    throw new Error('评测准备页没有挂载共享数据源全链路');
  }
  console.log('✓ 评测准备页挂载共享数据源全链路');
} catch (e) { console.log('✗ 评测数据源配置:', e.message); bad++; }

try {
  const request = evaluationFocusRequest({
    kind: 'evaluation', target: '正式评测', view: 'runs', roundId: 'round-demo',
  });
  if (request?.view !== 'runs' || request.roundId !== 'round-demo'
    || evaluationFocusRequest({ kind: 'evaluation', target: '正式评测' }) !== null) {
    throw new Error('评测焦点没有准确指向运行结果和指定轮次');
  }
  console.log('✓ 评测焦点可打开指定轮次的运行进度');
} catch (e) { console.log('✗ 评测运行焦点:', e.message); bad++; }

try {
  // #25：诊断与优化面板曾经互为唯一入口，形成导航死锁；焦点必须能直接落到这两个视图
  const diagnose = evaluationFocusRequest({ kind: 'evaluation', view: 'diagnose', roundId: 'round-demo' });
  const optimize = evaluationFocusRequest({ kind: 'evaluation', view: 'optimize', roundId: '' });
  if (diagnose?.view !== 'diagnose' || optimize?.view !== 'optimize'
    || evaluationFocusRequest({ kind: 'evaluation', view: 'setup' }) !== null) {
    throw new Error('评测焦点没有放行失败诊断和优化建议视图');
  }
  console.log('✓ 评测焦点可直达失败诊断与优化建议');
} catch (e) { console.log('✗ 评测诊断优化焦点:', e.message); bad++; }

try {
  const html = renderToString(<DiagnosisProgress task={{
    operation: 'diagnose', startedAt: new Date(Date.now() - 65_000).toISOString(),
  }} />);
  if (!html.includes('正在分析失败原因') || !html.includes('正在读取失败结果、执行轨迹和独立证据')
    || !html.includes('已运行') || !html.includes('1 分') || !html.includes('role="status"')) {
    throw new Error('诊断长任务没有立即显示可恢复的进度反馈');
  }
  console.log('✓ 失败诊断长任务显示进度反馈');
} catch (e) { console.log('✗ 失败诊断进度反馈:', e.message); bad++; }

try {
  const proposal = renderToString(<OptimizationProgress operation="propose" task={{
    operation: 'propose', startedAt: new Date(Date.now() - 65_000).toISOString(),
  }} />);
  const apply = renderToString(<OptimizationProgress operation="apply" task={{
    operation: 'apply', startedAt: new Date(Date.now() - 65_000).toISOString(),
  }} />);
  const targeted = renderToString(<OptimizationProgress operation="apply" task={{ node: { output: {
    taskType: 'optimization_apply', protocol: 'targeted_incremental', selectedCount: 5,
    changeSetCount: 1, targetResourceKeys: ['script:calculate', 'operation:calculate'],
    startedAt: new Date(Date.now() - 65_000).toISOString(),
  } } }} />);
  if (!proposal.includes('正在生成优化建议') || !proposal.includes('已运行')
    || !apply.includes('正在生成知识包候选') || !apply.includes('确定性校验')
    || !targeted.includes('5 条建议合并为 1 个变更组') || !targeted.includes('涉及 2 个资源')) {
    throw new Error('优化长任务没有显示可恢复的进度反馈');
  }
  console.log('✓ 优化长任务显示进度反馈');
} catch (e) { console.log('✗ 优化任务进度反馈:', e.message); bad++; }

try {
  const sourceScope = { kind: 'fusion_source', sourceId: 'internal-source-a', sourceName: '线路治理方案' };
  const html = renderToString(<RunsPanel rounds={[{
    id: 'round-source', status: 'done', packageVersion: '0.9.0', createdAt: '2026-08-23T10:00:00Z',
    scope: sourceScope,
  }]} current={{ round: {
    id: 'round-source', status: 'done', progressTotal: 1, progressDone: 1, passCount: 1,
    scope: sourceScope,
  }, results: [] }} onOpen={() => {}} />);
  if (evaluationScopeLabel({ kind: 'current' }) !== '正式融合包'
    || !html.includes('来源回归 · 线路治理方案')
    || html.includes('internal-source-a')) {
    throw new Error('来源回归轮次没有可读归属或泄露内部标识');
  }
  console.log('✓ 来源回归轮次显示业务归属');
} catch (e) { console.log('✗ 来源回归归属:', e.message); bad++; }

try {
  const results = [{
    id: 'result-pass', verdict: 'pass', question: '已通过的问题',
    finalAnswer: '这条默认不展开。', reason: '符合要求。',
  }, {
    id: 'result-fail', verdict: 'fail', question: '需要检查的问题',
    groundTruth: '应包含汇总表。', reason: '> 判定依据\n\n缺少关键字段。',
    finalAnswer: '## 整体结论\n\n| 装置 | 负荷 |\n| --- | ---: |\n| 装置 A | 82% |',
  }];
  const defaults = defaultExpandedEvaluationIds(results);
  const toggled = toggleEvaluationResultExpansion(defaults, 'result-fail');
  const html = renderToString(<EvaluationResultsList results={results} showTrace={false} />);
  if (!defaults.has('result-fail') || defaults.has('result-pass') || toggled.has('result-fail')
    || !html.includes('展开全部') || !html.includes('收起全部')
    || !html.includes('aria-expanded="true"') || !html.includes('aria-expanded="false"')
    || !html.includes('<h2>整体结论</h2>') || !html.includes('<table>')
    || html.includes('这条默认不展开。') || html.includes('人工改判')) {
    throw new Error('评测回答没有按 Markdown 渲染，或展开控制与默认状态不正确');
  }
  console.log('✓ 评测回答 Markdown 与批量展开控制');
} catch (e) { console.log('✗ 评测回答展示:', e.message); bad++; }

try {
  if (fitMermaidZoom(8_000, 1_040) !== 0.25
    || fitMermaidZoom(1_000, 640) !== 0.64
    || fitMermaidZoom(480, 960) !== 1.8
    || fitMermaidZoom(0, 960) !== 1
    // 小图按宽度能放到 6 倍，但视口高度只够 1.5 倍，取较紧的一档
    || fitMermaidZoom(160, 960, 400, 600) !== 1.5
    // 长图高度装不下也不缩小：视口纵向可滚动，压扁了反而看不清
    || fitMermaidZoom(160, 960, 900, 600) !== 1
    // 高度未知时退回只看宽度，行为与改动前一致
    || fitMermaidZoom(1_000, 640, 0, 0) !== 0.64) {
    throw new Error('流程图自动适配没有按宽度贴合、按高度限制放大');
  }
  console.log('✓ Mermaid 按宽度贴合视口，高度只限制放大不造成缩小');
} catch (e) { console.log('✗ Mermaid 自动适配:', e.message); bad++; }

try {
  const steps = [
    { phase: 'intake', label: '资料与目标准备', gate: null },
    { phase: 'goal', label: '目标确认', gate: 'materials_ready' },
    { phase: 'process', label: '流程建模', gate: 'goal_confirmed' },
    { phase: 'knowledge', label: '知识建模', gate: 'process_confirmed' },
  ];
  const gateById = new Map([
    ['materials_ready', { id: 'materials_ready', pass: true, blockers: [] }],
    ['goal_confirmed', { id: 'goal_confirmed', pass: false, blockers: ['确认当前建模目标'] }],
    ['process_confirmed', { id: 'process_confirmed', pass: false, blockers: ['形成并确认至少 1 条业务流程'] }],
  ]);
  // 当前停在"目标确认"：只有紧接其后的那道闸算挡路，更远的那道不该也亮起来
  const atGoal = (index) => (index < 1 ? 'passed' : index > 1 ? 'future' : 'current');
  if (blockingGateIndex(steps, gateById, atGoal) !== 2) {
    throw new Error('挡路的闸没有取当前步之后的第一道');
  }
  // 还站在第一步时，后面那道没通过的闸只是"还没轮到"，不该提前点亮
  const atIntake = (index) => (index === 0 ? 'current' : 'future');
  if (blockingGateIndex(steps, gateById, atIntake) !== -1) {
    throw new Error('尚未走到的闸被提前标成了挡路');
  }
  // 门禁还没拉回来时不能凭空点亮任何一道
  if (blockingGateIndex(steps, new Map(), atGoal) !== -1) {
    throw new Error('没有门禁数据时仍然标了挡路的闸');
  }
  const files = [
    'diagrams/采购申请.mmd', 'diagrams/入库校验.mmd',
    'objects/备件.md', 'objects/index.md', 'rules/安全库存判断.md',
    'scripts/reorder.py',
  ];
  if (stepCounts('process', files, []) !== '2 条流程'
    || stepCounts('knowledge', files, []) !== '2 项知识'
    || stepCounts('validation', files, []) !== '1 个脚本'
    || stepCounts('intake', files, [{ id: 'm1' }]) !== '1 份资料'
    || stepCounts('release', files, []) !== '') {
    throw new Error('阶段产出计数没有按目录投影，或把 index.md 算成了知识条目');
  }
  console.log('✓ 阶段轨只点亮一道闸，产出计数按目录投影');
} catch (e) { console.log('✗ 阶段轨门禁与计数:', e.message); bad++; }

try {
  const evidence = 'material:doc-evidence#§3.2';
  const materials = [{ id: 'doc-evidence', filename: '作业手册.docx' }];
  const linked = linkEvidenceReferences(`> 依据：${evidence}`, materials);
  const href = linked.match(/\((#evidence:[^)]+)\)/u)?.[1];
  const contextual = linkEvidenceReferences(
    '资料：material:doc-evidence#前言（合成资料说明，提供业务背景）',
    materials,
  );
  const strictHref = evidenceHref('material:doc-evidence#章节(补充)');
  const paragraphs = documentParagraphs('概述\n\n§3.2 安全库存应扣除已占用数量。\n\n附录');
  const highlight = evidenceHighlight(paragraphs[1], { anchor: '§3.2', offset: 4 });
  if (!linked.includes('《作业手册.docx》 · §3.2') || evidenceReferenceFromHref(href) !== evidence
    || evidenceLabel(evidence, materials).includes('doc-evidence') || highlight?.start !== 0 || highlight?.end !== 4
    || !contextual.includes('《作业手册.docx》 · 前言')
    || !contextual.endsWith('（合成资料说明，提供业务背景）')
    || contextual.includes('· 前言（') || strictHref.includes('(') || strictHref.includes(')')
    || evidenceReferenceFromHref(strictHref) !== 'material:doc-evidence#章节(补充)') {
    throw new Error('证据引用没有转换成可读链接或正文锚点');
  }
  if (readableEvidenceAnchor('offset=12500') !== '正文第 12,500 字附近'
    || readableEvidenceAnchor('table=CrudeOilYard') !== '表：CrudeOilYard'
    || !evidenceLabel('material:doc-evidence#offset=12500', materials).includes('正文第 12,500 字附近')) {
    throw new Error('字符偏移量没有转换成专家可理解的原文位置');
  }
  const longParagraph = documentParagraphs(`文档开头\n${'甲'.repeat(40_000)}\n文档结尾`)[0];
  const deepHighlight = evidenceHighlight(longParagraph, { anchor: 'offset=34536', offset: 0 });
  if (evidenceLocationOffset({ anchor: 'offset=34536', offset: 0 }) !== 34_536
    || deepHighlight?.start !== 34_536 || deepHighlight.end <= deepHighlight.start) {
    throw new Error('超长文档的深位置被错误地退回到文档开头');
  }
  store.set({ materials, projectId: 'evidence-project' });
  const html = renderToString(<EvidenceLinks references={[evidence]} />);
  if (!html.includes('查看《作业手册.docx》 · §3.2中的原文') || html.includes('doc-evidence')) {
    throw new Error('证据入口暴露了内部资料标识');
  }
  console.log('✓ 证据引用使用可读入口并定位正文锚点');
} catch (e) { console.log('✗ 证据正向定位:', e.message); bad++; }

try {
  const focused = applyStudioFocus(createStudioSession(), {
    kind: 'studio', view: 'entry', elementId: 'rule:safety-stock',
  });
  const filtered = filterStudioGraph({
    nodes: [
      { id: 'object:inventory', kind: 'object', label: '库存' },
      { id: 'rule:safety-stock', kind: 'rule', label: '安全库存判断' },
    ],
    edges: [{ id: 'edge:1', source: 'rule:safety-stock', target: 'object:inventory' }],
  }, 'rule', '安全', [
    { id: 'rule:safety-stock', title: '安全库存判断', description: '判断是否需要补货' },
  ]);
  if (focused.view !== 'entry' || focused.selectedElementId !== 'rule:safety-stock'
    || filtered.nodes.length !== 1 || filtered.edges.length !== 0) {
    throw new Error('Studio 焦点或图筛选没有保持同一元素状态');
  }
  console.log('✓ Studio 焦点与图表共享状态');
} catch (e) { console.log('✗ Studio 焦点与图表共享状态:', e.message); bad++; }

try {
  const session = {
    ...createStudioSession(),
    view: 'entry',
    returnView: 'table',
    selectedElementId: 'rule:safety-stock',
    query: '库存',
    kind: 'rule',
    graphViewport: { x: 18, y: -9, zoom: 1.2 },
    tableScrollTop: 420,
    expandedClusterIds: ['cluster:rule:1'],
  };
  const snapshot = studioHistorySnapshot(session);
  const url = studioHistoryUrl(
    { href: 'http://localhost/workbench.html?project=p1#/workbench' },
    session,
  );
  const fromUrl = studioHistoryFromLocation({ href: `http://localhost${url}` });
  const routeFocus = studioFocusFromLocation({ href: `http://localhost${url}` });
  const restored = restoreStudioHistory(createStudioSession(), snapshot);
  const legacyKind = restoreStudioHistory(createStudioSession(), { kind: 'concept' });
  if (restored.view !== 'entry' || restored.returnView !== 'table'
    || restored.graphViewport.zoom !== 1.2 || restored.tableScrollTop !== 420
    || restored.expandedClusterIds[0] !== 'cluster:rule:1'
    || fromUrl.selectedElementId !== 'rule:safety-stock' || fromUrl.query !== '库存'
    || routeFocus.view !== 'entry' || routeFocus.elementId !== 'rule:safety-stock'
    || !url.includes('studioView=entry') || !url.includes('studioKind=rule')
    || legacyKind.kind !== '') {
    throw new Error('Studio 浏览器历史没有完整保存导航现场');
  }
  console.log('✓ Studio 浏览器历史恢复视图、筛选与相机现场');
} catch (e) { console.log('✗ Studio 浏览器历史现场:', e.message); bad++; }

for (const s of [...SCENES, { id: 'materials' }]) {
  actions.setScene(s.id);
  try {
    const html = renderToString(<App />);
    if (html.length < 500) { console.log(`✗ ${s.id}: 输出过短 ${html.length}`); bad++; }
    else console.log(`✓ ${s.id.padEnd(10)} ${String(html.length).padStart(6)} 字符`);
  } catch (e) {
    console.log(`✗ ${s.id}: ${e.message}`); bad++;
  }
}
// 确认卡 / 策略 / 资料请求这些块只在特定场景出现，单独验一遍关键交互状态
actions.setScene('confirm');
actions.toggleTier('structural'); actions.setReviewMode('sampled'); actions.setPolicy('structural', 'auto');
try { renderToString(<App />); console.log('✓ confirm 交互态'); } catch (e) { console.log('✗ confirm 交互态:', e.message); bad++; }

actions.setScene('materials'); actions.setAnchor(2); actions.selectMaterial('m6');
try { renderToString(<App />); console.log('✓ 资料解析失败态'); } catch (e) { console.log('✗ 资料解析失败态:', e.message); bad++; }

store.set({ drawerOpen: true });
try { renderToString(<App />); console.log('✓ 过程抽屉'); } catch (e) { console.log('✗ 过程抽屉:', e.message); bad++; }

globalThis.window.location.hash = '#/workbench';
store.set({
  demo: false,
  drawerOpen: false,
  messages: [{
    id: 'upload-smoke', role: 'system', blocks: [{ type: 'upload', files: [
      { id: 'f1', name: '流程.docx', status: 'parsing' },
      { id: 'f2', name: '字典.xlsx', status: 'uploading' },
    ] }],
  }],
});
try {
  const html = renderToString(<App />);
  if (!html.includes('正在上传 1/2') || !html.includes('已上传，后台解析中') || !html.includes('正在上传')) {
    throw new Error('上传与解析状态文案不完整');
  }
  console.log('✓ 上传与后台解析分态');
} catch (e) { console.log('✗ 上传与后台解析分态:', e.message); bad++; }

store.set({
  messages: [{
    id: 'upload-batch', role: 'system', blocks: [{ type: 'upload', files: [
      { id: 'batch-1', documentId: 'doc-batch-1', name: '第一份.docx', status: 'parsing' },
      { id: 'batch-2', documentId: 'doc-batch-2', name: '第二份.docx', status: 'parsing' },
    ] }],
  }],
  uploads: [
    { id: 'batch-1', documentId: 'doc-batch-1', name: '第一份.docx', status: 'parsing' },
    { id: 'batch-2', documentId: 'doc-batch-2', name: '第二份.docx', status: 'parsing' },
  ],
  lastSeq: 0,
  replayUntilSeq: 0,
});
actions.handleEvent({
  eventType: 'message', seq: 1, role: 'system', blocks: [{
    type: 'uploadResult', parsed: 1, failed: 0,
    details: [{ documentId: 'doc-batch-1', name: '第一份.docx', status: 'parsed' }],
  }],
});
try {
  let html = renderToString(<App />);
  if (!html.includes('第二份.docx') || html.includes('第一份.docx') && !html.includes('解析成功 1/1')
    || store.get().uploads.some((item) => item.documentId === 'doc-batch-1')
    || !store.get().uploads.some((item) => item.documentId === 'doc-batch-2')) {
    throw new Error('首份完成时误删了同批仍在解析的资料');
  }
  actions.handleEvent({
    eventType: 'message', seq: 2, role: 'system', blocks: [{
      type: 'uploadResult', parsed: 1, failed: 0,
      details: [{ documentId: 'doc-batch-2', name: '第二份.docx', status: 'parsed' }],
    }],
  });
  html = renderToString(<App />);
  if (store.get().messages.some((message) => (message.blocks || []).some((block) => block.type === 'upload'))
    || store.get().uploads.length || (html.match(/解析成功 1\/1/g) || []).length !== 2) {
    throw new Error('批量资料终态没有逐份替换为服务端结果');
  }
  console.log('✓ 批量上传逐份完成不吞掉其他文件');
} catch (e) { console.log('✗ 批量上传逐份完成:', e.message); bad++; }

store.set({ messages: [], uploads: [], lastSeq: 0, replayUntilSeq: 4 });
for (const event of [
  { eventType: 'message', seq: 1, role: 'system', blocks: [{ type: 'upload', files: [{ id: 'doc-refresh', documentId: 'doc-refresh', name: '刷新资料.docx', status: 'parsing' }] }] },
  { eventType: 'message', seq: 2, role: 'system', blocks: [{ type: 'uploadResult', parsed: 1, failed: 0, details: [{ documentId: 'doc-refresh', name: '刷新资料.docx', status: 'parsed' }] }] },
]) actions.handleEvent(event);
actions.handleEvent({ eventType: 'message', seq: 2, role: 'system', blocks: [{ type: 'uploadResult', parsed: 1, failed: 0, details: [{ documentId: 'doc-refresh', name: '刷新资料.docx', status: 'parsed' }] }] });
try {
  const uploadBlocks = store.get().messages.flatMap((message) => message.blocks || []).filter((block) => block.type === 'upload');
  const results = store.get().messages.flatMap((message) => message.blocks || []).filter((block) => block.type === 'uploadResult');
  if (uploadBlocks.length || results.length !== 1) throw new Error('刷新回放后解析卡重复或仍残留解析中卡片');
  console.log('✓ 刷新回放资料解析结果不产生双卡');
} catch (e) { console.log('✗ 刷新回放资料结果:', e.message); bad++; }

store.set({
  focus: { kind: 'file', target: 'index.md' },
  files: ['index.md'],
  candidateDiff: [],
  lastSeq: 0,
  replayUntilSeq: 2,
});
actions.handleEvent({
  eventType: 'focus',
  seq: 1,
  target: { kind: 'file', target: '已经删除的历史文件.md' },
});
try {
  if (store.get().focus.target !== 'index.md') throw new Error('刷新时历史焦点抢占了当前工作修订');
  actions.agentFocus({ kind: 'file', target: '当前修订不存在.md' });
  if (store.get().focus.target !== 'index.md') throw new Error('实时焦点跳向了当前修订不存在的文件');
  store.set({
    demo: false,
    focus: { kind: 'studio', target: '知识包 Studio', view: 'graph' },
    follow: false,
    pendingFocus: { kind: 'doc', target: 'rd_internal_123' },
    materials: [{ id: 'rd_internal_123', filename: '加氢裂化工艺规程.docx' }],
  });
  const html = renderToString(<App />);
  if (!html.includes('加氢裂化工艺规程.docx') || html.includes('rd_internal_123')) {
    throw new Error('资料焦点向用户暴露了内部标识');
  }
  console.log('✓ 历史与失效焦点不抢占当前中栏');
} catch (e) { console.log('✗ 失效焦点保护:', e.message); bad++; }

store.set({
  activity: null,
  activityOpen: false,
});
actions.handleEvent({
  eventType: 'progress',
  activityId: 'full-read-smoke',
  text: '正在全文精读《工艺规程.docx》',
  detail: '8/12 个处理检查点完成 · 全文覆盖 67%',
  counters: { progress: 67, current: 8, total: 12, unit: '个处理检查点', elapsedSeconds: 45 },
});
try {
  const html = renderToString(<ActivityLine />);
  if (!html.includes('role="progressbar"') || !html.includes('aria-valuenow="67"')
    || !html.includes('8/12 个处理检查点') || !html.includes('全文覆盖 67%')
    || html.includes('已 45 秒') || !html.includes('aria-live="polite"')
    || !html.includes('title="正在全文精读《工艺规程.docx》"')) {
    throw new Error('全文精读进度缺少真实计数、覆盖率或可访问性信息');
  }
  console.log('✓ Agent 长任务展示真实进度');
} catch (e) { console.log('✗ Agent 长任务进度:', e.message); bad++; }

store.set({
  activity: { text: '正在建立完整阅读计划', progress: {}, startedAt: Date.now() },
  activityOpen: true,
});
try {
  const html = renderToString(<ActivityLine />);
  if (!html.includes('indeterminate') || html.includes('aria-valuenow="0"')
    || !html.includes('<button') || !html.includes('查看全部处理记录')) {
    throw new Error('未知进度被误显示成确定的 0%');
  }
  console.log('✓ 未知进度不伪造百分比');
} catch (e) { console.log('✗ Agent 未知进度:', e.message); bad++; }

store.set({
  run: { id: 'long-running', status: 'running' },
  interaction: null,
  messages: [],
  activity: { text: '正在核对影响范围', progress: {}, startedAt: Date.now() - 1_000 },
});
try {
  const html = renderToString(<ActivityLine />);
  if (!html.includes('停止任务') || !html.includes('wb-act-stop')) {
    throw new Error('运行开始后没有立即在活动行提供停止出口');
  }
  console.log('✓ Agent 运行开始后立即提供停止出口');
} catch (e) { console.log('✗ Agent 长任务停止出口:', e.message); bad++; }

store.set({
  run: null,
  activity: { text: '正在建立完整阅读计划', progress: {}, startedAt: Date.now() },
  activityOpen: true,
});

try {
  const html = renderToString(<App />);
  if (!html.includes('class="wb-scroll"') || !html.includes('正在建立完整阅读计划')) {
    throw new Error('活动进度没有留在唯一消息流的可跟随尾部');
  }
  console.log('✓ 消息流尾部包含可跟随活动进度');
} catch (e) { console.log('✗ 消息流活动尾部:', e.message); bad++; }

try {
  const genuine = { text: '正在核对知识条目', progress: { current: 2, total: 5 }, startedAt: 1 };
  const actual = visibleRunActivity({ activity: genuine, run: { status: 'running' }, interaction: null, streaming: true });
  const queued = visibleRunActivity({ activity: null, run: { id: 'queued', status: 'queued' }, interaction: null, streaming: false }, 100);
  const waiting = visibleRunActivity({ activity: genuine, run: { status: 'waiting' }, interaction: null, streaming: false });
  const blocked = visibleRunActivity({ activity: null, run: { status: 'waiting' }, interaction: { id: 'question' }, streaming: false });
  const resumedAt = new Date('2026-08-23T08:00:00.000Z');
  const resumed = visibleRunActivity({
    activity: null,
    run: {
      id: 'resumed', status: 'running', createdAt: '2026-08-23T07:00:00.000Z', updatedAt: resumedAt.toISOString(),
    },
    interaction: null,
    streaming: false,
  }, resumedAt.getTime() + 5_000);
  const staleProgress = visibleRunActivity({
    activity: {
      text: '等待前的旧步骤', progress: {}, startedAt: resumedAt.getTime() - 44 * 60_000,
      observedAt: resumedAt.getTime() - 44 * 60_000,
    },
    run: {
      id: 'resumed', status: 'running', createdAt: '2026-08-23T07:00:00.000Z', updatedAt: resumedAt.toISOString(),
    },
    interaction: null,
    streaming: false,
  }, resumedAt.getTime() + 5_000);
  const interjection = visibleRunActivity({
    activity: null,
    run: { id: 'interjection', status: 'queued' },
    interaction: { id: 'question' },
    streaming: false,
  }, 100);
  const traceFallback = visibleRunActivity({
    activity: null,
    run: { id: 'trace-run', status: 'running', updatedAt: resumedAt.toISOString() },
    interaction: null,
    streaming: true,
    trace: [{
      id: 'trace-evaluation', seq: 9, runId: 'trace-run', phase: 'started',
      summary: '正在运行评测', createdAt: new Date(resumedAt.getTime() + 2_000).toISOString(),
    }],
  }, resumedAt.getTime() + 5_000);
  const postToolPlanning = visibleRunActivity({
    activity: {
      id: 'after-tool:write-rule', phase: 'planning', text: '正在根据处理结果判断下一步',
      detail: '当前操作已经结束，正在规划下一项可执行工作',
      observedAt: resumedAt.getTime() + 3_000,
    },
    run: { id: 'trace-run', status: 'running', updatedAt: resumedAt.toISOString() },
    interaction: null,
    streaming: false,
    trace: [{
      id: 'trace-rule', seq: 10, runId: 'trace-run', phase: 'completed',
      summary: '已创建规则《合成规则》及其索引',
      createdAt: new Date(resumedAt.getTime() + 2_000).toISOString(),
    }],
  }, resumedAt.getTime() + 5_000);
  if (actual !== genuine || queued?.text !== '任务已提交，等待开始'
    || Object.hasOwn(queued?.progress || {}, 'percent') || waiting !== null || blocked !== null
    || interjection?.text !== '任务已提交，等待开始'
    || resumed?.startedAt !== resumedAt.getTime()
    || staleProgress?.text !== '助手正在处理当前步骤' || staleProgress?.startedAt !== resumedAt.getTime()
    || traceFallback?.text !== '正在运行评测' || !traceFallback?.detail.includes('运行轨迹正在更新')
    || postToolPlanning?.text !== '已创建规则《合成规则》及其索引'
    || queued?.startedAt !== undefined
    || executionStartedAt({ updatedAt: 'not-a-time' }, resumedAt.getTime()) !== undefined) {
    throw new Error('active run 兜底覆盖了真实进度、伪造百分比或越过等待边界');
  }
  console.log('✓ 流内活动以唯一 run 状态补足连续性');
} catch (e) { console.log('✗ 流内活动派生:', e.message); bad++; }

store.set({
  activity: {
    id: 'restored-live-activity',
    text: '正在根据处理结果判断下一步',
    detail: '当前操作已经结束，正在规划下一项可执行工作',
    progress: {},
    observedAt: Date.now(),
  },
  trace: [],
  lastSeq: 0,
  replayUntilSeq: 10_000,
});
actions.handleEvent({
  eventType: 'tool', seq: 23, runId: 'historic-run', toolCallId: 'historic-tool',
  name: 'read_file', phase: 'completed', summary: '历史文件读取完成',
  createdAt: new Date(Date.now() - 60_000).toISOString(),
});
try {
  if (store.get().activity?.id !== 'restored-live-activity'
    || store.get().trace.length !== 1
    || store.get().trace[0]?.phase !== 'completed') {
    throw new Error('历史工具终态清空了服务端恢复的最新活动，或没有补入运行轨迹');
  }
  console.log('✓ 历史轨迹回放不覆盖当前运行活动');
} catch (e) { console.log('✗ 历史轨迹活动隔离:', e.message); bad++; }

// 写包工具完成后必须重新拉取快照，否则文件树和产物计数会长期停在旧值。
// 服务端 core-service.ts 的 DOMAIN_TOOL_NAMES 会演进，这里断言判定按「只读工具黑名单」实现，
// 而不是把写包工具逐个列进白名单——后者漏一个就退化成静默陈旧。
try {
  const storeSource = readFileSync('src/workbench/lib/appStore.js', 'utf8');
  if (!storeSource.includes('!SNAPSHOT_NEUTRAL_TOOLS.has(event.name)')) {
    throw new Error('快照刷新仍按写包工具白名单判定，服务端新增 domain 工具时会漏刷');
  }
  for (const tool of [
    'sync_schema_objects', 'stage_schema_semantic_changes', 'confirm_schema_semantics',
    'create_object', 'patch_object', 'create_rule', 'patch_rule',
    'create_metric', 'patch_metric', 'create_operation', 'patch_operation',
    'patch_object_source_binding', 'delete_domain_resource',
  ]) {
    if (SNAPSHOT_NEUTRAL_TOOLS.has(tool)) throw new Error(`写包工具 ${tool} 被当成只读工具，不会触发快照刷新`);
  }
  for (const tool of ['read_file', 'list_dir', 'get_project_state', 'grep_package']) {
    if (!SNAPSHOT_NEUTRAL_TOOLS.has(tool)) throw new Error(`只读工具 ${tool} 会触发无谓的快照刷新`);
  }
  console.log('✓ 写包工具完成后触发快照刷新，只读工具不触发');
} catch (e) { console.log('✗ 快照刷新触发范围:', e.message); bad++; }

store.set({
  demo: false,
  loading: false,
  project: { name: '持续运行项目' },
  activity: null,
  activityOpen: false,
  run: {
    id: 'continuous-run', status: 'waiting',
    createdAt: new Date(Date.now() - 44 * 60_000).toISOString(),
    updatedAt: new Date(Date.now() - 44 * 60_000).toISOString(),
  },
  interaction: null,
  messages: [{
    id: 'continuous-stream', role: 'assistant', streaming: true,
    blocks: [{ type: 'text', text: '正在持续生成知识内容' }],
  }],
  userMessages: [],
  error: '',
  lastSeq: 0,
  replayUntilSeq: 0,
});
actions.handleEvent({
  eventType: 'status', seq: 1, runId: 'continuous-run', status: 'running', text: '助手正在建模',
  createdAt: new Date(Date.now() - 5_000).toISOString(),
});
try {
  const activityHtml = renderToString(<ActivityLine />);
  const streamingHtml = renderToString(<App />);
  const activityCount = (streamingHtml.match(/class="wb-act"/g) || []).length;
  if (!activityHtml.includes('助手正在生成回复') || !activityHtml.includes('indeterminate')
    || activityHtml.includes('aria-valuenow') || /已 (?:4|5|6) 秒/u.test(activityHtml)
    || activityHtml.includes('分钟') || activityHtml.includes('运行时长')
    || activityCount !== 1 || !streamingHtml.includes('正在持续生成知识内容')
    || !streamingHtml.includes('wb-stream-cursor') || !streamingHtml.includes('<textarea')
    || streamingHtml.includes('aria-label="发送" disabled')
    || streamingHtml.indexOf('正在持续生成知识内容') > streamingHtml.indexOf('class="wb-act"')) {
    throw new Error('流式输出期间进度不连续、遮挡正文、伪造数值或锁住输入框');
  }
  actions.handleEvent({ eventType: 'message_done', seq: 2, messageId: 'continuous-stream' });
  const stillRunning = renderToString(<ActivityLine />);
  if (!stillRunning.includes('助手正在处理当前步骤')) throw new Error('文本输出结束后 active run 进度提前消失');
  actions.handleEvent({
    eventType: 'status', seq: 3, runId: 'continuous-run', status: 'completed', text: '本轮完成',
    createdAt: new Date().toISOString(),
  });
  if (renderToString(<ActivityLine />).includes('wb-act')) throw new Error('run 完成后流内进度没有清理');
  store.set({
    run: { id: 'unknown-start', status: 'running', createdAt: new Date(Date.now() - 44 * 60_000).toISOString() },
    activity: null,
    messages: [],
  });
  const unknownStart = renderToString(<ActivityLine />);
  if (unknownStart.includes('class="el"') || unknownStart.includes('已 ')) {
    throw new Error('本次执行起点不可信时仍沿用 run 生命周期显示耗时');
  }
  store.set({ run: { id: 'unknown-start', status: 'completed' }, activity: null });
  store.set({
    run: { id: 'tool-terminal', status: 'running', updatedAt: new Date().toISOString() },
    activity: { id: 'read', text: '正在读取资料', progress: { percent: 92 }, observedAt: Date.now() },
  });
  actions.handleEvent({
    eventType: 'tool', seq: 4, name: 'read_documents_fully', phase: 'completed', summary: '全文读取完成',
  });
  if (store.get().activity !== null || renderToString(<ActivityLine />).includes('92%')) {
    throw new Error('工具完成后仍保留旧百分比活动');
  }
  console.log('✓ active run 全程保留流内进度且输入框可用');
} catch (e) { console.log('✗ active run 进度连续性:', e.message); bad++; }

try {
  const html = renderToString(<ImpactView />);
  if (!html.includes('影响数据暂不可用') || html.includes('11') || html.includes('4 项需要处理')) {
    throw new Error('真实影响数据缺失时仍回退到了演示数字');
  }
  console.log('✓ 真实影响分析不伪造演示数字');
} catch (e) { console.log('✗ 真实影响分析缺失态:', e.message); bad++; }

try {
  const html = renderToString(<ImpactView data={{
    scopeResolved: false,
    intact: { n: 0, items: [] },
    todo: { n: 41, decide: 0, items: Array.from({ length: 41 }, (_, i) => ({ t: `item-${i}`, s: '待核对' })) },
    chains: [],
  }} />);
  const text = html.replaceAll('<!-- -->', '');
  if (!text.includes('正在界定影响范围') || !text.includes('41 项现有业务内容待核对')
    || html.includes('wb-stat good') || html.includes('item-40') || html.includes('wb-split')) {
    throw new Error('未决范围仍被渲染成全量受影响清单');
  }
  console.log('✓ 新增资料影响未决时不制造全包红色清单');
} catch (e) { console.log('✗ 新增资料影响未决态:', e.message); bad++; }

try {
  const html = renderToString(<ImpactView data={{
    intact: { n: 0, items: [] },
    todo: { n: 1, decide: 0, items: [] },
    chains: [{
      src: '《管理办法》', srcSub: '口径变化', srcDocumentId: 'doc-policy',
      dst: 'rules/limit.md', dstSub: '需复核', dstPath: 'rules/limit.md',
    }],
  }} />);
  if (!html.includes('查看资料原文') || !html.includes('查看知识内容') || !html.includes('<button')) {
    throw new Error('影响链没有可操作的资料和知识出口');
  }
  console.log('✓ 影响链可跳转到资料与知识内容');
} catch (e) { console.log('✗ 影响链操作:', e.message); bad++; }

store.set({
  demo: false,
  loading: false,
  leftTab: 'pkg',
  files: ['rules/limit.md', 'objects/warehouse.md'],
  focus: {
    kind: 'impact', target: '影响分析',
    data: {
      intact: { n: 1, items: ['objects/warehouse.md'] },
      todo: { n: 1, decide: 0, items: [{ t: 'rules/limit.md', s: '需复核' }] },
      chains: [],
    },
  },
});
try {
  const html = renderToString(<App />);
  if (!/wb-row[^\"]*hit/.test(html) || !/wb-row[^\"]*dim/.test(html)
    || !html.includes('受新资料影响，需复核')) {
    throw new Error('真实影响清单没有驱动左栏热图');
  }
  console.log('✓ 真实影响清单同步驱动目录热图');
} catch (e) { console.log('✗ 真实目录影响热图:', e.message); bad++; }

try {
  const text = userFacingText('<details><summary>目标草案全文</summary>\n# 建模目标\n</details>');
  if (/<\/?(?:details|summary)>/i.test(text) || !text.includes('**目标草案全文**') || !text.includes('# 建模目标')) {
    throw new Error('模型输出的折叠标签仍会作为原始 HTML 暴露');
  }
  console.log('✓ 模型折叠内容降级为可读 Markdown');
} catch (e) { console.log('✗ 模型折叠内容:', e.message); bad++; }

try {
  const text = userFacingText('资料返回 scopeResolved=false，现在用 direct_resources 提交范围。');
  if (text.includes('scopeResolved') || text.includes('direct_resources')
    || !text.includes('影响范围尚未界定') || !text.includes('直接影响范围')) {
    throw new Error('增量影响内部字段仍暴露在用户正文');
  }
  console.log('✓ 增量影响内部字段降级为业务语言');
} catch (e) { console.log('✗ 增量影响内部字段:', e.message); bad++; }

try {
  const html = renderToString(<Block block={{ type: 'upload', files: [{
    id: 'schema-upload', documentId: 'doc-schema', name: '装置台账.xlsx', kind: 'excel',
    status: 'parsing', purpose: 'schema', readingMode: 'full',
  }] }} />);
  if (!html.includes('用途') || !html.includes('数据 Schema') || !html.includes('全文精读')
    || !html.includes('checked')) {
    throw new Error('上传卡没有同时提供资料用途和独立的全文精读设置');
  }
  console.log('✓ 上传卡允许指定用途与全文精读');
} catch (e) { console.log('✗ 上传资料设置:', e.message); bad++; }

try {
  const html = renderToString(<Block block={{
    type: 'ask',
    interaction: {
      id: 'content-fix-smoke', kind: 'clarification', status: 'pending',
      payload: {
        question: '知识内容是否需要调整？',
        options: [{
          label: '内容需要修改', description: '说明需要修改的位置和内容', value: 'content_fix',
          requiresText: true, textPrompt: '请描述具体需要修改的地方',
        }],
      },
    },
  }} />);
  if (!html.includes('内容需要修改') || !html.includes('aria-expanded="false"')
    || !html.includes('wb-ask-manual')) {
    throw new Error('需要补充文字的选项没有保留待展开状态');
  }
  console.log('✓ 问题卡支持选项后继续补充文字');
} catch (e) { console.log('✗ 问题卡补充文字:', e.message); bad++; }

try {
  const html = renderToString(<Block block={{
    type: 'confirm',
    interaction: {
      id: 'impact-confirm-smoke', kind: 'confirmation', status: 'pending',
      payload: {
        lastSubmissionError: '候选写入失败，请先处理脚本问题',
        changeGroups: [{
          id: 'rules', tier: 'caliber', title: '业务规则确认',
          impact: ['rules/limit.md', 'diagrams/process.mmd', 'objects/item.md'],
        }],
      },
    },
  }} />);
  const text = html.replace(/<!-- -->/g, '').replace(/<[^>]+>/g, '');
  if (!text.includes('涉及 rules/limit.md、diagrams/process.mmd 等 3 处')
    || !text.includes('刚才没有接受成功') || !text.includes('候选写入失败，请先处理脚本问题')) {
    throw new Error('确认卡没有显示真实变更范围或原位失败原因');
  }
  console.log('✓ 确认卡展示真实变更范围和原位失败原因');
} catch (e) { console.log('✗ 确认卡变更范围:', e.message); bad++; }

store.set({
  activity: { text: '正在读取资料《示例资料》', startedAt: Date.now(), sub: [] },
  run: { id: 'run-smoke', status: 'running' },
});
actions.handleEvent({
  eventType: 'interaction', id: 'confirm-smoke', kind: 'confirmation', status: 'pending',
  payload: { summary: '确认目标', changeGroups: [{ id: 'goal', tier: 'caliber', title: '确认目标' }] },
});
try {
  const html = renderToString(<App />);
  if (html.includes('助手运行中') || html.includes('正在读取资料《示例资料》') || store.get().activity) {
    throw new Error('等待确认时仍显示助手运行活动');
  }
  if (!html.includes('退回调整')) throw new Error('确认卡缺少退回调整入口');
  console.log('✓ 确认等待态清理活动提示');
} catch (e) { console.log('✗ 确认等待态清理活动提示:', e.message); bad++; }

store.set({
  messages: [], interaction: null, reviewedChangeGroups: [],
  lastSeq: 0, replayUntilSeq: 2,
});
actions.handleEvent({
  eventType: 'interaction', seq: 1,
  id: 'cancelled-run-decision', kind: 'business_decision', status: 'pending',
  payload: { question: '是否继续历史方案？' },
});
try {
  const replayed = store.get().messages[0]?.blocks?.[0]?.interaction;
  if (store.get().interaction || replayed?.status !== 'superseded') {
    throw new Error('历史回放恢复了已失效运行的旧决定卡');
  }
  const html = renderToString(<App />);
  if (!html.includes('已关闭') || html.includes('确认提交')) {
    throw new Error('失效决定卡仍显示为可操作状态');
  }
  console.log('✓ 历史回放不恢复已取消运行的旧决定卡');
} catch (e) { console.log('✗ 历史失效决定卡:', e.message); bad++; }

store.set({ messages: [], interaction: null, lastSeq: 0, replayUntilSeq: 0 });
actions.handleEvent({
  eventType: 'interaction', seq: 1,
  id: 'live-cancel-decision', kind: 'business_decision', status: 'pending',
  payload: { question: '是否继续当前方案？' },
});
actions.handleEvent({
  eventType: 'interaction', seq: 2,
  id: 'live-cancel-decision', kind: 'business_decision', status: 'cancelled',
  answer: { action: 'run_cancelled' },
});
try {
  const closed = store.get().messages[0]?.blocks?.[0]?.interaction;
  if (store.get().interaction || closed?.status !== 'cancelled') {
    throw new Error('取消运行后待处理决定卡没有同步关闭');
  }
  console.log('✓ 取消运行同步关闭待处理决定卡');
} catch (e) { console.log('✗ 取消运行关闭决定卡:', e.message); bad++; }

store.set({
  demo: false,
  run: { id: 'cancel-tool-run', status: 'running' },
  interaction: null,
  activity: { text: '正在原子生成待确认修改', progress: {}, observedAt: Date.now() },
  trace: [{
    id: 'tool:cancel-tool-run:call-stage-change-set', seq: 1,
    runId: 'cancel-tool-run', toolCallId: 'call-stage-change-set',
    name: 'stage_change_set', phase: 'started', summary: '正在原子生成待确认修改',
    createdAt: new Date().toISOString(),
  }],
  lastSeq: 0,
  replayUntilSeq: 0,
});
actions.handleEvent({
  eventType: 'tool', seq: 2, runId: 'cancel-tool-run',
  toolCallId: 'call-stage-change-set', name: 'stage_change_set',
  phase: 'cancelled', summary: '运行已停止，当前操作已取消',
});
actions.handleEvent({
  eventType: 'status', seq: 3, runId: 'cancel-tool-run', status: 'cancelled',
  text: '已停止，已完成的候选内容保留',
});
actions.handleEvent({
  eventType: 'progress', seq: 4, runId: 'cancel-tool-run',
  text: '正在原子生成待确认修改', detail: '迟到的旧进度', counters: {},
});
actions.handleEvent({
  eventType: 'status', seq: 5, runId: 'cancel-tool-run', status: 'running',
  text: '迟到的运行状态',
});
try {
  const html = renderToString(<App />);
  const drawer = renderToString(<ProcessDrawer onClose={() => {}} />);
  if (store.get().run?.status !== 'cancelled' || store.get().activity
    || store.get().trace[0]?.phase !== 'cancelled'
    || html.includes('助手运行中') || !drawer.includes('已取消')
    || !drawer.includes('运行已停止，当前操作已取消')) {
    throw new Error('取消后工具活动、顶栏或迟到事件没有收敛到同一终态');
  }
  console.log('✓ 取消运行同步结算工具活动并拒绝迟到运行事件');
} catch (e) { console.log('✗ 取消运行工具终态:', e.message); bad++; }

try {
  const html = renderToString(<Block block={{
    type: 'release',
    interaction: {
      id: 'release-smoke', kind: 'release_confirmation', status: 'pending',
      payload: {
        question: '是否确认用例覆盖并正式发布 1.0.0？', version: '1.0.0', testcaseCount: 5,
        questions: ['真实问题一', '真实问题二'],
      },
    },
  }} />);
  if (!html.includes('正式发布确认') || !html.includes('条真实用例已全部通过')
    || !html.includes('确认覆盖并发布') || !html.includes('继续补充用例')) {
    throw new Error('发布确认卡缺少覆盖范围、版本或互斥出口');
  }
  console.log('✓ 正式发布使用专用人工确认卡');
} catch (e) { console.log('✗ 正式发布确认卡:', e.message); bad++; }

store.set({
  demo: false,
  loading: false,
  leftTab: 'pkg',
  files: ['index.md', 'diagrams/sales/product_qa_flow.mmd', 'objects/Customer.md'],
  selectedPath: 'diagrams/sales/product_qa_flow.mmd',
  focus: { kind: 'flow', target: 'diagrams/sales/product_qa_flow.mmd' },
});
try {
  const html = renderToString(<LeftPane />);
  if (!html.includes('aria-label="知识包文件目录"') || !html.includes('diagrams')
    || !html.includes('sales') || !html.includes('product_qa_flow.mmd')
    || (html.match(/aria-expanded="true"/g) || []).length < 2) {
    throw new Error('当前文件的多级父目录没有自动展开');
  }
  console.log('✓ 真实知识包使用可展开的多级目录树');
} catch (e) { console.log('✗ 多级目录树:', e.message); bad++; }

store.set({
  candidateDiff: [{
    path: 'diagrams/product_qa_flow.mmd',
    before: '',
    after: 'flowchart TD\nSTART[客户经理发起查询] --> END[返回产品知识条目]',
    additions: 2,
    deletions: 0,
  }],
  focus: { kind: 'diff', target: 'diagrams/product_qa_flow.mmd' },
  materials: [],
});
try {
  const html = renderToString(<DiffView />);
  if (!html.includes('流程图查看方式') || !html.includes('图形') || !html.includes('源码差异')
    || !html.includes('客户经理发起查询') || !html.includes('返回产品知识条目')) {
    throw new Error('Mermaid 候选仍然只显示源码差异');
  }
  console.log('✓ Mermaid 候选默认展示待确认图形');
} catch (e) { console.log('✗ Mermaid 候选图形化:', e.message); bad++; }

store.set({
  messages: [],
  interaction: {
    id: 'restored-confirm-smoke', kind: 'confirmation', status: 'pending',
    payload: {
      atomicSelection: true,
      changeGroups: [{ id: 'flow', tier: 'caliber', title: '业务流程确认' }],
    },
  },
});
try {
  const html = renderToString(<RightPane />);
  if (!html.includes('确认待修改内容') || !html.includes('业务流程确认')
    || !html.includes('接受已选') || !html.includes('disabled=""')) {
    throw new Error('刷新恢复的待确认状态没有直接渲染确认卡');
  }
  console.log('✓ 刷新时直接恢复待确认卡');
} catch (e) { console.log('✗ 待确认卡恢复:', e.message); bad++; }

store.set({
  demo: false,
  project: null,
  loading: true,
  files: [],
  messages: [],
  interaction: null,
  focus: { kind: 'empty', target: '' },
  selectedPath: '',
});
try {
  const html = renderToString(<App />);
  if (html.includes('备件库存与采购问答') || html.includes('采购申请.mmd')) {
    throw new Error('真实项目加载态泄漏演示项目内容');
  }
  if (!html.includes('正在加载项目')) throw new Error('真实项目加载态缺少明确提示');
  console.log('✓ 真实项目加载态不混入演示内容');
} catch (e) { console.log('✗ 真实项目加载态:', e.message); bad++; }

try {
  const streamFocus = { kind: 'diff', target: '.modeling/goal.md' };
  const kept = resolveBootstrapFocus({
    current: streamFocus,
    candidateFiles: [{ path: '.modeling/other.md' }],
    interaction: { kind: 'confirmation' },
    initialPath: 'index.md',
  });
  const recovered = resolveBootstrapFocus({
    current: { kind: 'empty', target: '' },
    candidateFiles: [{ path: '.modeling/goal.md' }],
    interaction: { kind: 'confirmation' },
    initialPath: 'index.md',
  });
  const activeRecovered = resolveBootstrapFocus({
    current: { kind: 'empty', target: '' },
    agentFocus: { kind: 'flow', target: 'diagrams/current.mmd', node: 'D3' },
    initialPath: 'index.md',
  });
  const defaultStudio = resolveBootstrapFocus({ current: { kind: 'empty', target: '' }, initialPath: 'index.md' });
  const retainedStudio = resolveBootstrapFocus({
    current: { kind: 'studio', target: '知识包 Studio', view: 'graph' },
    agentFocus: { kind: 'flow', target: 'diagrams/current.mmd', node: 'D3' },
    initialPath: 'index.md',
  });
  if (kept !== streamFocus || recovered.kind !== 'diff' || recovered.target !== '.modeling/goal.md'
    || activeRecovered.kind !== 'flow' || activeRecovered.node !== 'D3'
    || defaultStudio.kind !== 'studio' || defaultStudio.view !== 'graph'
    || retainedStudio.kind !== 'studio' || retainedStudio.view !== 'graph') {
    throw new Error('启动快照覆盖了 Agent 焦点');
  }
  console.log('✓ 启动竞态保持 Agent 焦点');
} catch (e) { console.log('✗ 启动焦点:', e.message); bad++; }

store.set({
  demo: false,
  loading: false,
  files: [],
  messages: [],
  interaction: null,
  project: { name: '合成项目' },
  focus: { kind: 'diff', target: '.modeling/goal.md' },
  materials: [{ id: 'doc-synthetic', filename: '合成资料说明.docx' }],
  candidateDiff: [{
    path: '.modeling/goal.md', before: '',
    after: '# 目标\n\n资料：`material:doc-synthetic#前言`（合成资料说明，提供业务背景）',
    additions: 3, deletions: 0,
  }],
});
try {
  const html = renderToString(<App />);
  if (!html.includes('goal.md') || !html.includes('<h1>目标</h1>')
    || !html.includes('《合成资料说明.docx》 · 前言') || !html.includes('（合成资料说明，提供业务背景）')
    || html.includes('material:doc-synthetic') || html.includes('#evidence:')
    || html.includes('[《') || html.includes('# 目标')) {
    throw new Error('合成 goal.md 候选没有使用渲染后的 Markdown 内容');
  }
  console.log('✓ 合成 goal.md 候选默认展示渲染后的 Markdown');
} catch (e) { console.log('✗ 合成候选标题:', e.message); bad++; }

try {
  const appCss = readFileSync('src/workbench/app.css', 'utf8');
  const renderedRule = appCss.match(/\.wb-diff-markdown \.wb-markdown\s*\{([^}]*)\}/u)?.[1] || '';
  const sourceRule = appCss.match(/\.wb-diff-markdown \.wb-flow-source \.l\s*\{([^}]*)\}/u)?.[1] || '';
  const sourceShellRule = appCss.match(/\.wb-diff-markdown \.wb-flow-source\s*\{([^}]*)\}/u)?.[1] || '';
  const inlineEvidenceRule = appCss.match(/\.wb-markdown \.wb-evidence-link\s*\{([^}]*)\}/u)?.[1] || '';
  const inlineEvidenceIconRule = appCss.match(/\.wb-markdown \.wb-evidence-link svg\s*\{([^}]*)\}/u)?.[1] || '';
  if (!/width:\s*100%/u.test(renderedRule) || !/max-width:\s*none/u.test(renderedRule)
    || !/white-space:\s*pre-wrap/u.test(sourceRule)
    || !/overflow-wrap:\s*anywhere/u.test(sourceRule)
    || !/word-break:\s*break-word/u.test(sourceRule)
    || !/overflow-x:\s*hidden/u.test(sourceShellRule)
    || !/background:\s*var\(--c-surface-2\)/u.test(sourceShellRule)
    || !/display:\s*inline/u.test(inlineEvidenceRule)
    || !/line-height:\s*inherit/u.test(inlineEvidenceRule)
    || !/vertical-align:\s*baseline/u.test(inlineEvidenceRule)
    || !/vertical-align:\s*-0\.125em/u.test(inlineEvidenceIconRule)) {
    throw new Error('Markdown 候选宽度、源码布局或证据文字基线不正确');
  }
  console.log('✓ Markdown 候选使用完整宽度，源码自动换行，证据与说明文字基线对齐');
} catch (e) { console.log('✗ Markdown 候选源码布局:', e.message); bad++; }

store.set({
  demo: false,
  loading: false,
  project: { name: '流式项目' },
  messages: [],
  lastSeq: 0,
});
actions.handleEvent({ eventType: 'message_delta', seq: 1, messageId: 'stream-smoke', role: 'assistant', delta: '正在' });
actions.handleEvent({ eventType: 'message_delta', seq: 2, messageId: 'stream-smoke', role: 'assistant', delta: '实时输出' });
try {
  const streaming = renderToString(<App />);
  if (!streaming.includes('正在实时输出') || !streaming.includes('wb-stream-cursor')) {
    throw new Error('增量文本或输出光标缺失');
  }
  actions.handleEvent({ eventType: 'message_done', seq: 3, messageId: 'stream-smoke', role: 'assistant' });
  const completed = renderToString(<App />);
  if (!completed.includes('正在实时输出') || completed.includes('wb-stream-cursor')) {
    throw new Error('流式完成状态不正确');
  }
  console.log('✓ Agent 增量消息实时合并');
} catch (e) { console.log('✗ Agent 增量消息:', e.message); bad++; }

store.set({ messages: [], lastSeq: 0 });
actions.handleEvent({
  eventType: 'reasoning_delta', seq: 1, messageId: 'reasoning-smoke', role: 'assistant',
  delta: '先核对资料结构。',
});
try {
  const streaming = renderToString(<App />);
  if (!streaming.includes('正在思考') || !streaming.includes('先核对资料结构')) {
    throw new Error('思考过程没有在输出时展开');
  }
  actions.handleEvent({ eventType: 'reasoning_done', seq: 2, messageId: 'reasoning-smoke', role: 'assistant' });
  const completed = renderToString(<App />);
  if (!completed.includes('思考过程') || !completed.includes('先核对资料结构') || completed.includes('正在思考')) {
    throw new Error('思考过程完成状态不正确');
  }
  console.log('✓ LLM 思考过程独立折叠展示');
} catch (e) { console.log('✗ LLM 思考过程:', e.message); bad++; }

store.set({
  messages: [{
    id: 'accepted-smoke', role: 'user',
    blocks: [{ type: 'answer', interactionId: 'goal-smoke', answer: { approved: true, selected_groups: ['goal-rev4'] } }],
  }],
});
try {
  const html = renderToString(<App />);
  if (!html.includes('已接受所选修改') || html.includes('goal-rev4')) {
    throw new Error('确认结果仍暴露内部标识');
  }
  console.log('✓ 确认结果使用用户可读文案');
} catch (e) { console.log('✗ 确认结果文案:', e.message); bad++; }

store.set({
  messages: [{
    id: 'option-smoke', role: 'user',
    blocks: [{ type: 'answer', interactionId: 'ask-smoke', answer: { value: 'later', displayText: '稍后补充资料' } }],
  }],
});
try {
  const html = renderToString(<App />);
  if (!html.includes('稍后补充资料') || /<!-- -->later/.test(html)) {
    throw new Error('选项答复仍显示内部值');
  }
  console.log('✓ 问题选项使用用户可读标签');
} catch (e) { console.log('✗ 问题选项文案:', e.message); bad++; }

store.set({
  focus: { kind: 'flow', target: 'diagrams/真实流程.mmd' },
  selectedPath: 'diagrams/真实流程.mmd',
  fileContents: { 'diagrams/真实流程.mmd': 'flowchart TD\nA[开始<br/><b>入口</b>] --> B[完成]\n%% material:doc-real#流程' },
  materials: [{ id: 'doc-real', filename: '真实规程.docx' }],
  messages: [],
});
try {
  const html = renderToString(<App />);
  if (!html.includes('图形') || !html.includes('源码') || !html.includes('流程图查看方式')) {
    throw new Error('真实流程图缺少图形/源码切换');
  }
  if (!html.includes('开始<br/>入口') || html.includes('&lt;br')) {
    throw new Error('流程节点没有把 Mermaid 换行标记渲染为可读换行');
  }
  console.log('✓ 真实流程图提供图形与源码视图');
} catch (e) { console.log('✗ 流程图查看方式:', e.message); bad++; }

store.set({
  demo: false,
  fileContents: {
    'operations/check.md': ['---', `resource: ${['opera', 'tion'].join('')}`, 'key: check', 'title: 核对',
      '---', '# 业务目标', '', '核对资料。', '', '> 依据：material:doc-check#核对章节'].join('\n'),
  },
  materials: [{ id: 'doc-check', filename: '核对规程.docx' }],
});
try {
  const html = renderToString(<FileView path="operations/check.md" />);
  if (!html.includes('渲染') || !html.includes('源码') || !html.includes('文件查看方式')) {
    throw new Error('Markdown 文件缺少渲染/源码切换');
  }
  if (!html.includes('业务目标') || html.includes('resource: operation')) {
    throw new Error('Markdown 默认渲染视图没有正确隐藏 frontmatter');
  }
  if (!html.includes('查看《核对规程.docx》 · 核对章节中的原文') || html.includes('material:doc-check')) {
    throw new Error('Markdown 证据没有渲染成可读的原文入口');
  }
  console.log('✓ 真实 Markdown 提供渲染与源码视图');
} catch (e) { console.log('✗ Markdown 查看方式:', e.message); bad++; }

store.set({
  demo: false,
  fileContents: {},
  fileErrors: { 'objects/member_profile.md': '当前文件暂时读取失败，项目其他区域仍可正常使用' },
});
try {
  const html = renderToString(<FileView path="objects/member_profile.md" />);
  if (!html.includes('当前文件暂时不可用') || !html.includes('重试') || !html.includes('建模助手')) {
    throw new Error('文件读取失败没有提供就地恢复入口');
  }
  console.log('✓ 文件读取失败提供就地重试和助手提示');
} catch (e) { console.log('✗ 文件读取恢复入口:', e.message); bad++; }

store.set({
  demo: false,
  fileContents: {
    'manifest.yaml': ['id: refinery', 'name: 加氢裂化知识包', 'version: 1.0.0', 'status: released', 'resources:', '  objects: 2', '  operations: 2'].join('\n'),
  },
  materials: [],
});
try {
  const html = renderToString(<FileView path="manifest.yaml" />);
  if (!html.includes('YAML 结构化内容') || !html.includes('加氢裂化知识包')
    || !html.includes('v1.0.0') || !html.includes('已发布') || !html.includes('知识内容')) {
    throw new Error('YAML 没有按业务字段渲染结构化视图');
  }
  if (!html.includes('渲染') || !html.includes('源码')) throw new Error('YAML 缺少渲染/源码切换');
  console.log('✓ YAML 提供结构化业务视图');
} catch (e) { console.log('✗ YAML 结构化视图:', e.message); bad++; }

store.set({ projectId: 'pj eval/1' });
try {
  const html = renderToString(<GateView data={{ gates: [{
    id: 'evaluation', st: 'fail', t: '评测门禁', d: '需要真实用例',
    actions: [{ label: '前往评测界面', route: 'evaluation', primary: true }],
  }] }} />);
  if (!html.includes('<button') || html.includes('href="/?project=')) {
    throw new Error('评测门禁动作仍会跳入旧工作流');
  }
  actions.openEvaluation();
  const workbench = renderToString(<App />);
  if (store.get().focus.kind !== 'evaluation' || !workbench.includes('正式评测') || !workbench.includes('正在读取评测事实')
      || !workbench.includes('打开正式评测')) {
    throw new Error('评测门禁没有在新工作台中打开评测视图');
  }
  console.log('✓ 评测门禁在新工作台内打开评测视图');
} catch (e) { console.log('✗ 评测门禁内部导航:', e.message); bad++; }

store.set({
  demo: false,
  project: { id: 'pj-readable', name: '可读版本项目' },
  projectId: 'pj-readable',
  packageVersion: '1.0.0',
  packageStatus: 'released',
  revision: 'deadbeefcafebabedeadbeefcafebabedeadbeef',
  workflow: { phase: 'release', phaseLabel: '发布交付', execution: 'completed', revision: 108 },
  phases: [
    { phase: 'intake', label: '资料与目标准备', gate: null, linear: true },
    { phase: 'goal', label: '目标确认', gate: 'materials_ready', linear: true },
    { phase: 'release', label: '发布交付', gate: 'releasable', linear: true },
  ],
  progress: null,
  files: [],
  focus: { kind: 'empty', target: '' },
  messages: [],
  drawerOpen: true,
  trace: [
    { id: 21, seq: 21, createdAt: '2026-08-24T08:10:00Z', at: '16:10:00', name: 'get_project_state', phase: 'completed', summary: '当前项目状态：评测 / running' },
    { id: 23, seq: 23, createdAt: '2026-08-24T08:11:00Z', at: '16:11:00', name: 'transition_project_state', phase: 'completed', summary: '最新轨迹记录' },
    { id: 22, seq: 22, createdAt: '2026-08-24T08:11:00Z', at: '16:11:00', name: 'update_plan', phase: 'completed', summary: '同秒较早记录' },
  ],
});
try {
  const html = renderToString(<App />);
  const orderedTrace = newestTraceFirst(store.get().trace);
  const message = restoredVersionMessage('1.0.0', {});
  const leakedText = "评测轮次 rd_64bea577-467，基于修订 588743e；request_release；evaluation:running；评测库查询失败：Unknown database 'mv_shadow_pj_796c0f3e_156'；结果 5/5，版本 v1.0.0。";
  const agentText = userFacingText(leakedText);
  const capabilityClaim = userFacingText('脚本契约需要读取领域资源，但本会话工具集中没有该读取能力，请确认平台是否开放。');
  const agentHtml = renderToString(<Block block={{ type: 'text', text: leakedText }} />);
  const quotaText = userFacingText("[181001] model call failed: RateLimitError: Error code: 429 - {'code':'insufficient_quota','message':'Allocated quota exceeded'}");
  if (!html.includes('v1.0.0 · 已发布') || !html.includes('发布交付')) {
    throw new Error('主界面没有优先显示语义版本和业务阶段');
  }
  if (/deadbeef|request_release|run-|cp-013|工具事件|工具边界/.test(html)) {
    throw new Error('主界面仍暴露运行时实现标识');
  }
  if (capabilityClaim !== '当前步骤需要系统继续核对，现有进展已保留。') {
    throw new Error('主界面仍暴露平台内部能力缺口');
  }
  if (message !== '已从版本历史恢复到v1.0.0，无需重复评测。'
    || /request_|preflight|API|checkpoint|running/.test(message)) {
    throw new Error('版本恢复没有形成确定性业务提示');
  }
  if (humanizeTraceSummary('当前项目状态：评测 / running') !== '当前阶段：评测'
    || humanizeTraceSummary('处理完成', 'task_tool') !== '一项子任务已完成，主任务继续处理中'
    || humanizeTraceSummary('处理完成', 'read_document') !== '当前步骤已完成'
    || !html.includes('当前阶段：评测')) {
    throw new Error('历史过程摘要没有翻译成业务语言');
  }
  if (orderedTrace.map((item) => item.seq).join(',') !== '23,22,21'
    || !html.includes('最新轨迹记录') || !html.includes('同秒较早记录')
    || html.indexOf('最新轨迹记录') > html.indexOf('同秒较早记录')
    || html.indexOf('同秒较早记录') > html.indexOf('当前阶段：评测')) {
    throw new Error('运行轨迹没有按生成时间和事件序号倒序展示');
  }
  if (/rd_64bea577|588743e|request_release|evaluation:running|mv_shadow_pj|Unknown database/.test(agentText + agentHtml)
    || !agentText.includes('本次评测') || !agentText.includes('当前版本') || !agentHtml.includes('本次评测')
    || !agentText.includes('5/5') || !agentText.includes('v1.0.0')) {
    throw new Error('Agent 消息没有隔离运行时实现，或误伤了业务值');
  }
  if (/RateLimitError|429|insufficient_quota|Allocated quota/i.test(quotaText)
    || !quotaText.includes('候选和进度已保留')) {
    throw new Error('模型额度错误没有翻译成可恢复的业务提示');
  }
  const userLiteral = messageText({ type: 'text', text: '请按我写的 doc_user_literal 原样理解' }, false);
  const assistantSanitized = messageText({
    type: 'text', text: '已检查 doc_agent_leak 与 material:doc_agent_leak#offset=12',
  });
  if (assistantSanitized.includes('doc_agent_leak') || !userLiteral.includes('doc_user_literal')) {
    throw new Error('助手历史消息仍暴露资料编号，或页面误改了用户自己的输入');
  }
  console.log('✓ 主流程不暴露运行时实现');
} catch (e) { console.log('✗ 主流程实现隔离:', e.message); bad++; }

store.set({ messages: [], trace: [], lastSeq: 0, replayUntilSeq: 0, activity: null });
actions.handleEvent({
  eventType: 'tool', seq: 1, runId: 'run-recovery', toolCallId: 'call-change-set',
  name: 'stage_change_set', phase: 'repairing', code: 'CHANGE_SET_RESULT_UNKNOWN',
  summary: 'Change Set 已提交，但返回助手的响应丢失；重试前必须先查询当前回执',
  createdAt: '2026-01-01T00:00:00Z',
});
actions.handleEvent({
  eventType: 'tool', seq: 2, runId: 'run-recovery', toolCallId: 'call-receipt',
  name: 'get_change_set_receipt', phase: 'completed', summary: '已找到原写入回执',
  createdAt: '2026-01-01T00:00:01Z',
});
try {
  const recovery = store.get().messages.find((message) => message.id === 'tool-alert-run-recovery-call-change-set');
  const trace = store.get().trace.find((item) => item.toolCallId === 'call-change-set');
  if (recovery || trace?.phase !== 'repairing' || !trace.summary.includes('响应丢失')) {
    throw new Error('自动恢复过程没有仅保留在活动记录');
  }
  console.log('✓ 工具自动恢复过程不占用主对话');
} catch (e) { console.log('✗ 工具自动恢复记录:', e.message); bad++; }

store.set({ messages: [], trace: [], lastSeq: 0, replayUntilSeq: 0, activity: null });
actions.handleEvent({
  eventType: 'tool', seq: 1, runId: 'run-optional-tool', toolCallId: 'call-eval-db',
  name: 'query_eval_db', phase: 'rejected', code: 'EVAL_DB_TOOL_UNAVAILABLE',
  problemVisibility: 'activity', summary: '可选评测库未配置，已跳过本次查询',
  createdAt: '2026-01-01T00:00:00Z',
});
try {
  const trace = store.get().trace.find((item) => item.toolCallId === 'call-eval-db');
  if (store.get().messages.length !== 0 || trace?.phase !== 'rejected') {
    throw new Error('可选工具不可用仍生成了主对话问题卡，或没有保留活动轨迹');
  }
  console.log('✓ 可选工具不可用只保留活动轨迹');
} catch (e) { console.log('✗ 可选工具不可用展示:', e.message); bad++; }

const traceOnlyProblem = {
  eventType: 'tool', seq: 1, runId: 'run-trace-only-problem', toolCallId: 'call-plan-validation',
  name: 'run_script_test', phase: 'failed', code: 'SCRIPT_CONTRACT_NOT_FOUND',
  issueId: 'issue-plan-validation', issueStatus: 'awaiting_user',
  summary: '脚本契约不存在：plan_validation', createdAt: '2026-01-01T00:00:00Z',
};
try {
  for (const replayUntilSeq of [0, 1]) {
    store.set({
      demo: false, messages: [], trace: [], lastSeq: 0, replayUntilSeq,
      activity: null, run: null,
    });
    actions.handleEvent(traceOnlyProblem);
    const trace = store.get().trace.find((item) => item.toolCallId === 'call-plan-validation');
    if (store.get().messages.length !== 0
      || trace?.phase !== 'failed'
      || !trace.summary.includes('脚本契约不存在')) {
      throw new Error('普通工具失败仍进入聊天记录，或没有保留在过程记录');
    }
  }
  const processHtml = renderToString(<ProcessDrawer onClose={() => {}} />);
  if (!processHtml.includes('脚本契约不存在') || processHtml.includes('当前问题需要处理')) {
    throw new Error('过程抽屉没有展示工具失败，或仍渲染聊天红卡文案');
  }
  console.log('✓ 工具失败只出现在过程记录');
} catch (e) { console.log('✗ 工具失败展示位置:', e.message); bad++; }

store.set({ messages: [], trace: [], lastSeq: 0, replayUntilSeq: 0, activity: null });
actions.handleEvent({
  eventType: 'tool', seq: 1, runId: 'run-issue', toolCallId: 'call-managed-write',
  name: 'write_file', phase: 'repairing', code: 'MANAGED_FILE_REQUIRES_DOMAIN_TOOL',
  issueId: 'issue-binding', issueStatus: 'repairing', summary: '对象数据映射已阻止，正在自动修正',
  createdAt: '2026-01-01T00:00:00Z',
});
actions.handleEvent({
  eventType: 'tool', seq: 2, runId: 'run-issue', toolCallId: 'call-managed-write-again',
  name: 'write_file', phase: 'repairing', code: 'MANAGED_FILE_REQUIRES_DOMAIN_TOOL',
  issueId: 'issue-binding', issueStatus: 'repairing', summary: '正在读取对象和来源表',
  createdAt: '2026-01-01T00:00:01Z',
});
try {
  const issues = store.get().messages.filter((message) => message.id === 'tool-issue-run-issue-issue-binding');
  if (issues.length !== 0) {
    throw new Error('自动修正中的技术问题仍占据主对话');
  }
  actions.handleEvent({
    eventType: 'tool', seq: 3, runId: 'run-issue', toolCallId: 'call-binding',
    name: 'patch_object_source_binding', phase: 'waiting', code: 'RECOVERY_EXHAUSTED',
    issueId: 'issue-binding', issueStatus: 'awaiting_user', problemVisibility: 'recovery',
    repairStopped: true, summary: '自动处理仍有未完成项，请选择如何处理当前成果。',
    recoveryActions: [{
      id: 'retry_from_latest', label: '基于最新内容继续', description: '只处理未完成部分。',
    }, {
      id: 'keep_current_progress', label: '保留当前成果，结束本轮', description: '保留已有修改。',
    }],
    createdAt: '2026-01-01T00:00:02Z',
  });
  const recoveryIssue = store.get().messages.find((message) => message.id === 'tool-issue-run-issue-issue-binding');
  const html = renderToString(<Block block={recoveryIssue?.blocks?.[0]} />);
  if (recoveryIssue?.blocks?.[0]?.type !== 'recovery'
    || !html.includes('请选择后续处理')
    || !html.includes('基于最新内容继续')
    || !html.includes('保留当前成果，结束本轮')
    || html.includes('card-stale')
    || html.includes('当前问题需要处理')) {
    throw new Error('自动恢复到达上限后没有形成客户可操作的非红色选择卡');
  }
  actions.handleEvent({
    eventType: 'tool', seq: 4, runId: 'run-issue', toolCallId: 'call-binding-resolved',
    name: 'patch_object_source_binding', phase: 'completed', issueStatus: 'resolved',
    resolvedIssueIds: ['issue-binding'], summary: '对象与数据表绑定完成',
    createdAt: '2026-01-01T00:00:03Z',
  });
  if (store.get().messages.some((message) => message.id === 'tool-issue-run-issue-issue-binding')) {
    throw new Error('已解决问题仍占据主对话');
  }
  console.log('✓ 自动恢复隐藏且耗尽后提供客户处理出口');
} catch (e) { console.log('✗ 工具问题生命周期:', e.message); bad++; }

store.set({ trace: [], lastSeq: 0, replayUntilSeq: 0, activity: null, drawerOpen: true });
actions.handleEvent({
  eventType: 'tool', seq: 1, runId: 'run-trace', toolCallId: 'call-read',
  traceGroupId: 'paged:read_document:fixture',
  name: 'read_document', phase: 'started', summary: '正在读取资料', createdAt: '2026-01-01T00:00:00Z',
});
actions.handleEvent({
  eventType: 'tool', seq: 2, runId: 'run-trace', toolCallId: 'call-read',
  traceGroupId: 'paged:read_document:fixture',
  name: 'read_document', phase: 'completed', summary: '资料读取完成', createdAt: '2026-01-01T00:00:01Z',
});
actions.handleEvent({
  eventType: 'tool', seq: 3, runId: 'run-trace', toolCallId: 'call-read-next',
  traceGroupId: 'paged:read_document:fixture',
  name: 'read_document', phase: 'started', summary: '正在读取资料', createdAt: '2026-01-01T00:00:02Z',
});
actions.handleEvent({
  eventType: 'tool', seq: 4, runId: 'run-trace', toolCallId: 'call-read-next',
  traceGroupId: 'paged:read_document:fixture',
  name: 'read_document', phase: 'completed', summary: '资料读取完成', createdAt: '2026-01-01T00:00:03Z',
});
try {
  const [entry] = store.get().trace;
  if (store.get().trace.length !== 1 || entry?.phase !== 'completed'
    || entry?.summary !== '资料读取完成' || !entry?.at.endsWith(':00')
    || entry?.createdAt !== '2026-01-01T00:00:00Z' || entry?.seq !== 1
    || entry?.segmentCount !== 2
    || !renderToString(<App />).includes('2 个连续分段')) {
    throw new Error('同一分页动作没有合并为一条处理记录');
  }
  console.log('✓ 连续分页读取合并为单条处理记录');
} catch (e) { console.log('✗ 工具处理记录合并:', e.message); bad++; }

store.set({ trace: [], lastSeq: 0, replayUntilSeq: 0 });
actions.handleEvent({
  eventType: 'tool', seq: 1, runId: 'run-legacy-trace', toolCallId: 'legacy-page-1',
  name: 'read_document', phase: 'completed', summary: '正在读取资料《历史资料》', createdAt: '2026-01-01T00:00:00Z',
});
actions.handleEvent({
  eventType: 'tool', seq: 2, runId: 'run-legacy-trace', toolCallId: 'legacy-page-2',
  name: 'read_document', phase: 'completed', summary: '正在读取资料《历史资料》', createdAt: '2026-01-01T00:00:01Z',
});
try {
  const [entry] = store.get().trace;
  if (store.get().trace.length !== 1 || entry?.segmentCount !== 2) {
    throw new Error('旧分页轨迹回放后仍逐页重复展示');
  }
  console.log('✓ 历史分页轨迹按资料聚合');
} catch (e) { console.log('✗ 历史分页轨迹聚合:', e.message); bad++; }

try {
  const awaiting = {
    status: 'awaiting_decisions', unresolvedCount: 2,
    ledger: [
      { sourceId: 'source-a', kind: 'object', sourceKey: '设备', decision: null },
      { sourceId: 'source-a', kind: 'rule', sourceKey: '安全规则', decision: 'preserve' },
      { sourceId: 'source-b', kind: 'diagram', sourceKey: '开工流程', decision: null },
    ],
  };
  const unresolved = unresolvedEntries(awaiting);
  const pendingQuality = qualityRows(awaiting);
  const disposition = dispositionFor(unresolved[0]);
  const canonical = dispositionFor(unresolved[0], 'canonicalize', {
    targetSourceId: 'source-b', targetKey: '设备主数据', reason: '字段和口径一致',
  });
  const excluded = dispositionFor(unresolved[0], 'exclude', { reason: '不属于本次范围' });
  const compiled = ledgerEntries({ ledger: { resources: [{
    sourceRecordId: 'source-a', sourceId: 'compiler-a', kind: 'object', sourceKey: '设备',
    fusedKey: 'source-a--设备', decision: 'preserve',
  }] } });
  const actualQuality = qualityRows({ quality: { gates: [{
    id: 'reference-integrity', passed: true, detail: '结构化引用全部闭合',
  }] } });
  const output = defaultFusionOutput({ projectId: 'pj-fusion', projectName: '炼化', packageVersion: '1.0.0' });
  const source = {
    packageId: 'shared-package', scopes: ['objects', 'operations', 'scripts', 'mappings'],
    counts: { objects: 2, operations: 1, scripts: 1 },
    scopeDependencies: [{ fromScope: 'operations', from: 'operations/run', requiresScope: 'scripts', requires: 'scripts/run' }],
  };
  const scopes = availableFusionScopes(source);
  const problems = fusionScopeProblems(source, ['objects', 'operations']);
  const groups = fusionConceptGroups({ concepts: [{ id: 'common-a', layer: 'common' }, { id: 'parameter-a', layer: 'parameterized' }] });
  const duplicates = duplicateFusionPackageIds([{ packageId: 'shared-package' }, { packageId: 'shared-package' }]);
  const followUp = packageUploadFollowUp([
    { name: '来源一', version: '1.0.0' }, { name: '来源二', version: '2.0.0' },
  ]);
  if (unresolved.length !== 2 || disposition.action !== 'preserve' || disposition.key !== '设备'
    || canonical.action !== 'canonicalize' || canonical.targetSourceId !== 'source-b' || canonical.reason !== '字段和口径一致'
    || excluded.action !== 'exclude' || excluded.reason !== '不属于本次范围'
    || scopes.join(',') !== 'objects,operations,scripts'
    || fusionScopeCount(source, 'objects') !== 2 || problems.length !== 1
    || groups.common.length !== 1 || groups.parameterized.length !== 1
    || !duplicates.has('shared-package') || !/一次导入了 2 份/u.test(followUp)
    || compiled[0].sourceId !== 'source-a' || compiled[0].fusedKey !== 'source-a--设备'
    || pendingQuality[0].passed !== false || pendingQuality[1].passed !== null
    || actualQuality[0].label !== '结构化引用闭合' || !actualQuality[0].passed
    || planStage(awaiting).label !== '还有 2 项待确定'
    || output.name !== '炼化融合知识包' || output.version !== '1.0.0') {
    throw new Error('账本、未裁决门禁或编译后质量结果映射不正确');
  }
  console.log('✓ 多来源融合账本与真实质量门禁');
} catch (e) { console.log('✗ 多来源融合模型:', e.message); bad++; }

try {
  const sources = [
    { id: 'source-a', packageId: 'shared-package', name: '安全生产包', version: '1.0.0', fileCount: 8,
      scopes: ['objects', 'operations', 'scripts'], counts: { objects: 2, operations: 1, scripts: 1 },
      scopeDependencies: [{ fromScope: 'operations', from: 'operations/run', requiresScope: 'scripts', requires: 'scripts/run' }] },
    { id: 'source-b', packageId: 'shared-package', name: '设备主数据包', version: '1.0.0', fileCount: 6,
      scopes: ['objects', 'rules'], counts: { objects: 1, rules: 1 }, scopeDependencies: [] },
  ];
  const builder = renderToString(<FusionBuilder
    sources={sources}
    selected={sources.map((source) => source.id)}
    setSelected={() => {}}
    sourceScopes={{ 'source-a': ['objects', 'operations'], 'source-b': ['objects', 'rules'] }}
    setSourceScopes={() => {}}
    output={{ id: 'fusion-ui', name: '融合结果', description: '融合说明', version: '1.0.0' }}
    setOutput={() => {}}
    plans={[]}
    baselineReady
    busy=""
    error=""
    onCreate={() => {}}
    onOpenPlan={() => {}}
  />);
  const emptyBuilder = renderToString(<FusionBuilder
    sources={[]}
    selected={[]}
    setSelected={() => {}}
    sourceScopes={{}}
    setSourceScopes={() => {}}
    output={{ id: 'fusion-ui', name: '融合结果', description: '融合说明', version: '1.0.0' }}
    setOutput={() => {}}
    plans={[]}
    baselineReady
    busy=""
    error=""
    onCreate={() => {}}
    onOpenPlan={() => {}}
  />);
  let liveFiles = [{ name: 'source-a.zip' }, { name: 'notes.txt' }, { name: 'SOURCE-B.ZIP' }];
  const liveSelection = { [Symbol.iterator]: () => liveFiles[Symbol.iterator]() };
  const selectedPackages = packageFilesFromSelection(liveSelection);
  liveFiles = [];
  const decision = renderToString(<DecisionControl
    entry={{ sourceId: 'source-a', kind: 'object', sourceKey: '设备', decision: null, included: true }}
    entries={[
      { sourceId: 'source-a', kind: 'object', sourceKey: '设备', decision: null, included: true },
      { sourceId: 'source-b', kind: 'object', sourceKey: '设备主数据', decision: 'preserve', included: true },
    ]}
    sourceMap={new Map(sources.map((source) => [source.id, source]))}
    mutable
    busy={false}
    onResolve={() => {}}
  />);
  const builderText = builder.replaceAll('<!-- -->', '');
  if (!builderText.includes('纳入范围') || !builderText.includes('脚本 0/1') || builderText.includes('流程图')
    || !builderText.includes('相同 package id') || !builderText.includes('当前范围会留下 1 处依赖缺口')
    || !emptyBuilder.includes('选择多个来源知识包') || !emptyBuilder.includes('multiple=""')
    || !emptyBuilder.includes('.zip,application/zip') || !emptyBuilder.includes('fusion-package-upload')
    || selectedPackages.map((file) => file.name).join(',') !== 'source-a.zip,SOURCE-B.ZIP'
    || !decision.includes('独立保留') || !decision.includes('归并为同一项') || !decision.includes('不纳入结果')
    || />[^<]*(source-a|exclude_scope)[^<]*</u.test(builder + decision)) {
    throw new Error('范围选择或三种互斥业务去向没有形成可用界面');
  }
  console.log('✓ 多来源融合范围与三种业务去向');
} catch (e) { console.log('✗ 多来源融合裁决界面:', e.message); bad++; }

try {
  const sources = [
    { id: 'source-a', name: '分支容量治理', version: '1.0.0' },
    { id: 'source-b', name: '单辐射治理', version: '1.0.0' },
  ];
  const plan = {
    id: 'plan-business', status: 'awaiting_decisions', unresolvedCount: 1,
    output: { name: '融合知识包' }, sources: sources.map((source) => ({ ...source, sourceId: source.id })),
    concepts: [{
      id: 'common.substation', title: '变电站', layer: 'common', decision: 'preserve_views',
      rationale: '两个来源的对象粒度不同，保留场景视图。',
      members: [
        { sourceId: 'source-a', kind: 'object', key: 'Substation', relation: 'overlap' },
        { sourceId: 'source-b', kind: 'object', key: 'substation_ledger', relation: 'broader' },
      ],
    }],
    ledger: [
      { sourceId: 'source-a', kind: 'object', sourceKey: 'Substation', decision: 'preserve', included: true },
      { sourceId: 'source-b', kind: 'object', sourceKey: 'substation_ledger', decision: null, included: true },
    ],
  };
  const html = renderToString(<FusionPlan plan={plan} plans={[plan]} sources={sources} busy="" error=""
    failedQuality={null} onResolve={() => {}} onCompile={() => {}} onSelectPlan={() => {}}
    onNew={() => {}} onRetry={() => {}} />);
  const conceptText = html.replaceAll('<!-- -->', '');
  if (!conceptText.includes('跨包业务概念') || !conceptText.includes('公共层 1 组') || !conceptText.includes('变电站')
    || !conceptText.includes('保留场景视图') || !conceptText.includes('范围更宽')) {
    throw new Error('跨包概念没有按业务层级和来源关系展示');
  }
  console.log('✓ 多来源融合概念业务卡');
} catch (e) { console.log('✗ 多来源融合概念业务卡:', e.message); bad++; }

store.set({
  demo: false,
  project: { id: 'pj-fusion-ui', name: '融合验收项目' },
  projectId: 'pj-fusion-ui',
  packageVersion: '1.0.0',
  revision: 'revision-for-fusion',
  focus: { kind: 'conflict', target: '多来源融合' },
  drawerOpen: false,
});
try {
  const html = renderToString(<App />);
  if (!html.includes('多来源融合') || !html.includes('正在读取来源包和融合方案')
    || !html.includes('>融合<') || /fusion_[a-z0-9]|awaiting_decisions|compile_package|\/agent\/fusion-plans/.test(html)) {
    throw new Error('融合入口不可见、加载态缺失或暴露了内部实现');
  }
  console.log('✓ 多来源融合有可见入口且不暴露内部实现');
} catch (e) { console.log('✗ 多来源融合入口:', e.message); bad++; }

globalThis.window.location.hash = '#/create';
try {
  const options = initialUploadOptions([
    { name: '目标.md', purpose: 'goal', readingMode: 'full' },
    { name: '对象.xlsx', purpose: 'schema', readingMode: 'on_demand' },
  ]);
  if (options.followUp !== true || options.directivesByName['目标.md']?.readingMode !== 'full'
    || options.directivesByName['对象.xlsx']?.purpose !== 'schema') {
    throw new Error('初始多文件没有共享批次并保留逐份用途');
  }
  renderToString(<App />);
  console.log('✓ 创建页多文件单批上传');
} catch (e) { console.log('✗ 创建页:', e.message); bad++; }

try {
  const reviewMaterials = [
    { id: 'confirmed', parseStatus: 'parsed', purposeReview: { status: 'confirmed', suggestedBy: 'user' } },
    { id: 'analyzing', parseStatus: 'parsed', purposeReview: { status: 'pending', suggestedBy: 'system' } },
    { id: 'ready-a', parseStatus: 'parsed', purposeReview: { status: 'pending', suggestedBy: 'agent' } },
    { id: 'ready-b', parseStatus: 'parsed', purposeReview: { status: 'pending', suggestedBy: 'agent' } },
  ];
  if (pendingMaterialPurposeReviews(reviewMaterials).length !== 3
    || nextReadyMaterialPurposeReview(reviewMaterials)?.id !== 'ready-a'
    || nextReadyMaterialPurposeReview(reviewMaterials, 'ready-a')?.id !== 'ready-b') {
    throw new Error('多资料用途确认没有区分分析中、待确认和下一份');
  }
  console.log('✓ 多资料用途逐份确认顺序');
} catch (e) { console.log('✗ 多资料用途确认:', e.message); bad++; }

try {
  const html = renderToString(<MaterialPurposeBatchCard materials={[
    {
      id: 'purpose-a', filename: 'synthetic-goal.md', parseStatus: 'parsed',
      purposeReview: { status: 'pending', suggestedBy: 'agent', suggestedPurpose: 'goal', sheetSuggestions: [] },
    },
    {
      id: 'purpose-b', filename: 'synthetic-data.xlsx', parseStatus: 'parsed',
      purposeReview: {
        status: 'pending', suggestedBy: 'agent', suggestedPurpose: 'schema',
        sheetSuggestions: [{ sheet: 'internal_sheet_alpha', semanticRole: 'schema', importDisposition: 'object' }],
      },
    },
  ]} />);
  if (!html.includes('确认资料用途') || !html.includes('synthetic-goal.md')
    || !html.includes('建模目标依据') || !html.includes('synthetic-data.xlsx')
    || !html.includes('数据 Schema') || !html.includes('一键确认全部')
    || html.includes('internal_sheet_alpha')) {
    throw new Error('批量确认卡没有仅按文件展示助手建议用途');
  }
  console.log('✓ 助手提供文件级资料用途批量确认卡');
} catch (e) { console.log('✗ 资料用途批量确认卡:', e.message); bad++; }


try {
  if (!shouldUseStaleRunTermination({ status: 'stale_run' })
    || !shouldUseStaleRunTermination({ status: 'plan_unreadable' })
    || shouldUseStaleRunTermination({ status: 'resume_failed' })
    || shouldUseStaleRunTermination(null)) {
    throw new Error('恢复终止门控没有只覆盖 stale_run/plan_unreadable');
  }
  console.log('✓ stale-run 终止门控仅覆盖 stale_run/plan_unreadable');
} catch (e) { console.log('✗ 恢复终止门控:', e.message); bad++; }

try {
  // #30：计划卡的批准状态必须取自 store.changePlan，而不是消息里的历史快照
  const planSource = readFileSync('src/workbench/right/blocks/Blocks.jsx', 'utf8');
  if (!/const livePlan = useStore\(store, \(s\) => s\.changePlan\)/.test(planSource)
    || !/livePlan && livePlan\.id === b\.plan\?\.id \? livePlan : b\.plan/.test(planSource)) {
    throw new Error('计划卡仍在读消息里的历史快照');
  }
  if (!/disabled=\{Boolean\(busy\) \|\| agentBusy\}/.test(planSource)) {
    throw new Error('助手运行中时批准按钮没有禁用');
  }
  if (!/继续执行/.test(planSource)) throw new Error('缺少开工指令补发出口');
  const storeSource = readFileSync('src/workbench/lib/appStore.js', 'utf8');
  if (!/error\.planSettled = true/.test(storeSource)) {
    throw new Error('等待超时没有标记“决定已生效”');
  }
  if (!/决定已经生效，只是助手当时仍在收尾/.test(storeSource)) {
    throw new Error('超时后仍在谎报“计划仍保持原状态”');
  }
  if (!/async resumeChangePlan\(\)/.test(storeSource)) throw new Error('缺少 resumeChangePlan 补发动作');
  console.log('✓ 计划批准竞态：状态取权威值、忙时禁用、超时可补发');
} catch (e) { console.log('✗ 计划批准竞态:', e.message); bad++; }

console.log(bad ? `\n${bad} 处失败` : '\n全部通过');
process.exit(bad ? 1 : 0);
