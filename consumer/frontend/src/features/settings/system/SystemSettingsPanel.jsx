import { useCallback, useEffect, useState } from 'react';

import { FieldRow, SectionCard } from '../../../shared/ui/primitives';
import { loadSystemConfig } from './api';
import {
  buildSystemConfigSectionPayload,
  nextSystemSettingsTab,
  SYSTEM_CONFIG_FIELDS,
  SYSTEM_SETTINGS_PANEL_SECTIONS,
  SYSTEM_SETTINGS_SECTION_META,
  systemSettingsModuleKey,
  systemSettingsPanelId,
  systemSettingsTabId,
} from './fields';
import SystemConfigModule from './SystemConfigModule';
import SystemConfigTransfer from './SystemConfigTransfer';
import './systemConfig.css';

function inputValue(field, draft) {
  if (field.secret) {
    return draft[field.name] || '';
  }
  return draft[field.name] ?? '';
}

function SystemConfigField({ field, controller }) {
  const { draft, patchDraft, readOnly } = controller;
  const hasSavedSecret = field.hasField ?
    Boolean(draft[field.hasField]) :
    false;
  const clearRequested = field.clearField ?
    draft[field.clearField] === true :
    false;
  let { hint } = field;
  if (field.secret) {
    hint = hasSavedSecret ?
      '已保存密钥；留空会保留现有值' :
      '尚未保存密钥';
  }

  const updateValue = (event) => {
    let { value } = event.target;
    if (field.type === 'checkbox') {
      value = event.target.checked;
    }
    if (field.type === 'number' && value !== '') {
      value = Number(value);
    }
    const patch = { [field.name]: value };
    if (field.secret && field.clearField && value) {
      patch[field.clearField] = false;
    }
    patchDraft(patch);
  };

  let control;
  if (field.type === 'checkbox') {
    control = (
      <label className="system-config-toggle">
        <input
          type="checkbox"
          aria-label={field.label}
          checked={Boolean(draft[field.name])}
          disabled={readOnly}
          onChange={updateValue}
        />
        <span>{draft[field.name] ? '已启用' : '已关闭'}</span>
      </label>
    );
  } else if (field.type === 'select') {
    control = (
      <select
        aria-label={field.label}
        value={inputValue(field, draft)}
        disabled={readOnly}
        onChange={updateValue}
      >
        {field.options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    );
  } else {
    control = (
      <div className="system-config-input-wrap">
        <input
          type={field.type}
          aria-label={field.label}
          value={inputValue(field, draft)}
          disabled={readOnly || clearRequested}
          min={field.min}
          max={field.max}
          step={field.step}
          placeholder={field.placeholder || ''}
          autoComplete={field.secret ? 'new-password' : undefined}
          onChange={updateValue}
        />
        {field.clearField && hasSavedSecret && (
          <label className="system-config-clear-secret">
            <input
              type="checkbox"
              aria-label={`清除${field.label}`}
              checked={clearRequested}
              disabled={readOnly}
              onChange={(event) =>
                patchDraft({
                  [field.clearField]: event.target.checked,
                  ...(event.target.checked ? { [field.name]: '' } : {}),
                })
              }
            />
            保存时清除现有密钥
          </label>
        )}
      </div>
    );
  }

  return (
    <FieldRow label={field.label} hint={hint}>
      {control}
    </FieldRow>
  );
}

function SystemConfigForm({ section, controller }) {
  return (
    <div className="system-config-fields">
      {SYSTEM_CONFIG_FIELDS[section].map((field) => (
        <SystemConfigField
          key={field.name}
          field={field}
          controller={controller}
        />
      ))}
    </div>
  );
}

// eslint-disable-next-line max-lines-per-function -- Preserve established boundary.
export default function SystemSettingsPanel({
  canEdit = false,
  initialSection = 'agent',
  request,
  onConfigChanged,
}) {
  const safeInitialSection = SYSTEM_SETTINGS_PANEL_SECTIONS.includes(
    initialSection,
  ) ?
    initialSection :
    'agent';
  const [activeSection, setActiveSection] = useState(safeInitialSection);
  const [config, setConfig] = useState(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [reloadRevision, setReloadRevision] = useState(0);
  const [importRevision, setImportRevision] = useState(0);

  useEffect(() => {
    let active = true;
    loadSystemConfig(request)
      .then((payload) => {
        if (!active) {
          return;
        }
        setConfig(payload);
      })
      .catch((error) => {
        if (!active) {
          return;
        }
        setLoadError(error?.message || '加载系统配置失败');
      })
      .finally(() => {
        if (active) {
          setLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, [reloadRevision, request]);

  const handleImported = useCallback(
    (nextConfig, payload) => {
      setConfig(nextConfig);
      setImportRevision((revision) => revision + 1);
      onConfigChanged?.(nextConfig, payload);
    },
    [onConfigChanged],
  );

  const handleTabKeyDown = (section, event) => {
    const nextSection = nextSystemSettingsTab(section, event.key);
    if (nextSection === section) {
      return;
    }
    event.preventDefault();
    setActiveSection(nextSection);
    requestAnimationFrame(() =>
      document.getElementById(systemSettingsTabId(nextSection))?.focus(),
    );
  };

  const handleSectionSaved = useCallback(
    (section, saved, payload) => {
      const next = { ...(config || {}), [section]: saved };
      setConfig(next);
      onConfigChanged?.(next, payload);
    },
    [config, onConfigChanged],
  );

  return (
    <div className="system-settings-panel">
      <SystemConfigTransfer
        canEdit={canEdit}
        request={request}
        onImported={handleImported}
      />

      <div
        className="system-settings-section-tabs"
        role="tablist"
        aria-label="系统配置模块"
      >
        {SYSTEM_SETTINGS_PANEL_SECTIONS.map((section) => (
          <button
            type="button"
            role="tab"
            key={section}
            id={systemSettingsTabId(section)}
            aria-controls={systemSettingsPanelId(section)}
            aria-selected={section === activeSection}
            tabIndex={section === activeSection ? 0 : -1}
            className={section === activeSection ? 'on' : ''}
            onClick={() => setActiveSection(section)}
            onKeyDown={(event) => handleTabKeyDown(section, event)}
          >
            {SYSTEM_SETTINGS_SECTION_META[section].title}
          </button>
        ))}
      </div>

      {loading && !config && (
        <SectionCard>
          <div className="empty" role="status">
            正在加载系统配置…
          </div>
        </SectionCard>
      )}
      {loadError && !config && (
        <SectionCard>
          <div className="system-config-load-error" role="alert">
            <span>{loadError}</span>
            <button
              type="button"
              className="btn"
              onClick={() => {
                setLoading(true);
                setLoadError('');
                setReloadRevision((value) => value + 1);
              }}
            >
              重试
            </button>
          </div>
        </SectionCard>
      )}
      {config &&
        SYSTEM_SETTINGS_PANEL_SECTIONS.map((section) => {
          const meta = SYSTEM_SETTINGS_SECTION_META[section];
          return (
            <div
              key={section}
              role="tabpanel"
              id={systemSettingsPanelId(section)}
              aria-labelledby={systemSettingsTabId(section)}
              hidden={section !== activeSection}
            >
              <SystemConfigModule
                key={systemSettingsModuleKey(section, importRevision)}
                section={section}
                title={meta.title}
                description={meta.description}
                initialData={config[section] || {}}
                canEdit={canEdit}
                request={request}
                prepareData={(draft) =>
                  buildSystemConfigSectionPayload(section, draft)
                }
                onSaved={(saved, payload) =>
                  handleSectionSaved(section, saved, payload)
                }
              >
                {(controller) => (
                  <SystemConfigForm section={section} controller={controller} />
                )}
              </SystemConfigModule>
            </div>
          );
        })}
    </div>
  );
}
