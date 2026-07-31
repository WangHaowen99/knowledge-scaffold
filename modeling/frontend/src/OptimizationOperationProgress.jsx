import { useEffect, useState } from 'react';

import { optimizationOperationProgress } from './optimizationOperationProgress.js';
import TaskProgressCard from './TaskProgressCard.jsx';

export default function OptimizationOperationProgress({ task }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [task?.operation, task?.startedAt]);

  return <TaskProgressCard progress={optimizationOperationProgress(task, now)} />;
}
