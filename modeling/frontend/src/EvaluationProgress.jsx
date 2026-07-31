import { useEffect, useState } from 'react';

import { evaluationProgress } from './evaluationProgress.js';
import TaskProgressCard from './TaskProgressCard.jsx';

export default function EvaluationProgress({ round, history }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (round?.status !== 'running') return undefined;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [round?.id, round?.status]);

  if (round?.status !== 'running') return null;
  return <TaskProgressCard progress={evaluationProgress(round, history, now)} />;
}
