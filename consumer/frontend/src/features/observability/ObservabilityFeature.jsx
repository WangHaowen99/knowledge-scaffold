// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import { authFetch } from '../../shared/api/auth.js';

import {
  ObservabilityPageHeader,
  ObservabilityPanel,
  ObservabilityState,
} from './components.jsx';
import GlobalObservabilityView from './GlobalObservabilityView.jsx';
import { OBSERVABILITY_VIEWS, resolveObservabilityAccess } from './model.js';
import './observability.css';
import SessionPerformanceView from './SessionPerformanceView.jsx';

export default function ObservabilityFeature({
  view = OBSERVABILITY_VIEWS.SESSION,
  sessionId = '',
  user = null,
  canViewGlobal,
  can,
  workspaceId = '',
  workspaces = [],
  onBack,
  request = authFetch,
}) {
  let resolvedGlobalPermission;
  if (typeof canViewGlobal === 'boolean') {
    resolvedGlobalPermission = canViewGlobal;
  } else if (typeof can === 'function') {
    resolvedGlobalPermission = can('observability.global');
  }
  const access = resolveObservabilityAccess({
    view,
    sessionId,
    user,
    canViewGlobal: resolvedGlobalPermission,
  });

  if (!access.allowed) {
    let kind = 'empty';
    if (access.reason === 'forbidden') {
      kind = 'forbidden';
    } else if (access.reason === 'unauthenticated') {
      kind = 'error';
    }
    return (
      <div className="obs-feature">
        <ObservabilityPageHeader
          eyebrow="OBSERVABILITY"
          title="可观测性"
          description="查看会话和运行级性能数据。"
          onBack={onBack}
        />
        <ObservabilityPanel>
          <ObservabilityState
            kind={kind}
            title={access.title}
            detail={access.detail}
          />
        </ObservabilityPanel>
      </div>
    );
  }

  return (
    <div className="obs-feature">
      {access.view === OBSERVABILITY_VIEWS.GLOBAL ? (
        <GlobalObservabilityView
          workspaceId={workspaceId}
          workspaces={workspaces}
          onBack={onBack}
          request={request}
        />
      ) : (
        <SessionPerformanceView
          key={sessionId}
          sessionId={sessionId}
          onBack={onBack}
          request={request}
        />
      )}
    </div>
  );
}
