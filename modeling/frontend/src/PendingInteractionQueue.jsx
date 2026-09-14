import React, { useState } from 'react';
import { ChevronDown, ChevronRight, ListChecks, LockKeyhole } from 'lucide-react';

const PURPOSE_LABEL = {
  controlled_repair: '确认知识包修复方案',
  controlled_repair_expert: '回答业务口径问题',
  check_finding_ignore: '填写暂缓理由',
  process_review: '确认业务流程',
  modeling_brief: '确认建模目标',
};

function queueTitle(interaction) {
  return String(
    interaction?.payload?.title
      ?? interaction?.title
      ?? PURPOSE_LABEL[interaction?.payload?.purpose]
      ?? '待处理事项',
  ).trim();
}

function independent(interaction) {
  return interaction?.payload?.queue?.independent === true;
}

function dependencies(interaction) {
  return Array.isArray(interaction?.payload?.queue?.dependsOn)
    ? interaction.payload.queue.dependsOn.map(String).filter(Boolean)
    : [];
}

export default function PendingInteractionQueue({
  interactions = [],
  activeInteractionId = '',
  onSelect,
}) {
  const [open, setOpen] = useState(true);
  if (!interactions.length) return null;
  const activeIds = new Set(interactions.map((interaction) => interaction.id));

  return (
    <section className="pchat-pending-queue" aria-label={`待处理事项，共 ${interactions.length} 项`}>
      <button type="button" className="pchat-pending-queue__toggle"
        aria-expanded={open} onClick={() => setOpen((current) => !current)}>
        <ListChecks size={15} aria-hidden="true" />
        <strong>待处理事项</strong>
        <span>{interactions.length}</span>
        <ChevronDown size={14} aria-hidden="true" className={open ? 'is-open' : ''} />
      </button>
      {open && (
        <ol className="pchat-pending-queue__list">
          {interactions.map((interaction, index) => {
            const isCurrent = index === 0;
            const dependencyPending = dependencies(interaction).some((id) => activeIds.has(id));
            const canOpen = isCurrent || independent(interaction) || !dependencyPending;
            const active = interaction.id === activeInteractionId;
            return (
              <li key={interaction.id}>
                <button type="button" className={active ? 'is-active' : ''}
                  disabled={!canOpen}
                  title={canOpen ? queueTitle(interaction) : '请先完成前面的待办'}
                  onClick={() => onSelect?.(interaction)}>
                  <span className={`pchat-pending-queue__state ${isCurrent ? 'is-current' : ''}`}>
                    {(() => { if (isCurrent) return '当前'; return independent(interaction) ? '可并行查看' : '排队'; })()}
                  </span>
                  <span className="pchat-pending-queue__title">{queueTitle(interaction)}</span>
                  {canOpen
                    ? <ChevronRight size={14} aria-hidden="true" />
                    : <LockKeyhole size={13} aria-hidden="true" />}
                </button>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
