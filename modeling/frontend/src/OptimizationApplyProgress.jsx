import { useEffect, useState } from 'react';
import { optimizationProgress } from './optimizationProgress.js';
import TaskProgressCard from './TaskProgressCard.jsx';

export default function OptimizationApplyProgress({ node }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (node?.status !== 'running') return undefined;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [node?.id, node?.status]);

  if (node?.status !== 'running') return null;
  const progress = optimizationProgress(node, now);
  if (!progress) return null;

  return <TaskProgressCard progress={progress} />;
}
