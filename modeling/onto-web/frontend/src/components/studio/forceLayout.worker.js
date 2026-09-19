// 力导布局 Worker：d3-force 在后台线程跑固定迭代，主线程只收位置快照。
// 大本体（上千节点）布局计算不阻塞 UI（方案 7.1 的 Worker 力导路径）。
import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
} from 'd3-force';

let simulation = null;

self.onmessage = (event) => {
  const { nodes, edges } = event.data;
  if (simulation) simulation.stop();
  const simNodes = nodes.map((n) => ({ ...n }));
  const links = edges.map((e) => ({ source: e.source, target: e.target }));
  simulation = forceSimulation(simNodes)
    .force('link', forceLink(links).id((d) => d.id).distance(130).strength(0.5))
    .force('charge', forceManyBody().strength(-300).distanceMax(900))
    .force('collide', forceCollide().radius((d) => (d.kind === 'object' ? 72 : 64)))
    .force('center', forceCenter(0, 0))
    .force('x', forceX(0).strength(0.02))
    .force('y', forceY(0).strength(0.02))
    .stop();

  const total = Math.min(400, 150 + Math.floor(simNodes.length / 2));
  for (let i = 0; i < total; i += 1) {
    simulation.tick();
    if (i % 25 === 24) post(simNodes, false);
  }
  post(simNodes, true);
};

function post(nodes, done) {
  self.postMessage({
    done,
    positions: nodes.map((n) => ({ id: n.id, x: n.x, y: n.y })),
  });
}
