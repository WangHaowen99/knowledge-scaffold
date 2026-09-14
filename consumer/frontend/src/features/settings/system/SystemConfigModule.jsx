// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import { SectionCard } from '../../../shared/ui/primitives';
import {
  formatSystemConfigTestTime,
  SYSTEM_CONFIG_SECTION_LABELS,
} from './model';
import { useSystemConfigModule } from './useSystemConfigModule';
import './systemConfig.css';

export function SystemConfigModuleStatus({ controller }) {
  const { error, success, testStatus } = controller;
  if (!error && !success && !testStatus?.tested_at) {
    return null;
  }
  const tone = error ? 'error' : 'success';
  const message = error || success || testStatus?.message;
  return (
    <div
      className={`system-config-feedback ${tone}`}
      role={error ? 'alert' : 'status'}
      aria-live="polite"
    >
      <span>{message}</span>
      {testStatus?.tested_at && (
        <time dateTime={testStatus.tested_at}>
          测试时间：{formatSystemConfigTestTime(testStatus.tested_at)}
        </time>
      )}
    </div>
  );
}

export function SystemConfigReadOnlyNotice() {
  return (
    <div className="system-config-readonly" role="note">
      <span aria-hidden="true">🔒</span>
      当前为只读状态。仅平台管理员可以测试、导入或保存系统配置。
    </div>
  );
}

// eslint-disable-next-line complexity -- Preserve this established UI, hook, or event-contract boundary.
export default function SystemConfigModule({
  section,
  initialData = {},
  canEdit = false,
  title = SYSTEM_CONFIG_SECTION_LABELS[section] || section,
  description = '',
  children,
  request,
  onSaved,
  prepareData,
  showTest = true,
  showSave = true,
  className = '',
}) {
  const controller = useSystemConfigModule({
    section,
    initialData,
    readOnly: !canEdit,
    request,
    onSaved,
    prepareData,
  });
  const content =
    typeof children === 'function' ? children(controller) : children;
  const busy = Boolean(controller.busyAction);

  return (
    <SectionCard
      title={title}
      description={description}
      className={`system-config-module ${className}`.trim()}
    >
      {!canEdit && <SystemConfigReadOnlyNotice />}
      {content}
      <SystemConfigModuleStatus controller={controller} />
      <div className="system-config-module-actions">
        <span className="system-config-dirty" aria-live="polite">
          {controller.isDirty ? '有未保存修改' : '配置已同步'}
        </span>
        <div>
          {showTest && (
            <button
              type="button"
              className="btn"
              disabled={!canEdit || busy}
              onClick={() => controller.testConnection()}
            >
              {controller.busyAction === 'test' ? '测试中…' : '测试'}
            </button>
          )}
          <button
            type="button"
            className="btn"
            disabled={!canEdit || busy || !controller.isDirty}
            onClick={controller.resetDraft}
          >
            重置
          </button>
          {showSave && (
            <button
              type="button"
              className="btn btn-p"
              disabled={!canEdit || busy || !controller.isDirty}
              onClick={() => controller.save()}
            >
              {controller.busyAction === 'save' ? '保存中…' : '保存修改'}
            </button>
          )}
        </div>
      </div>
    </SectionCard>
  );
}
