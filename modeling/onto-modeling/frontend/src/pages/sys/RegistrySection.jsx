// 系统设置 · 配置注册表（SY-14，只读）：每项的当前值、来源、校验、生效时机。
// B3b 第 3 条：每项带自己的 precedence（模型接入、权限模式、会话池是设置页优先）、description，来自插件的带 pack。
import { useState } from 'react';
import { useApp } from '../../app/context.js';
import { Chip, Inp, Seg, Table } from '../../ui/index.jsx';
import { LoadNote, Loading, SysHead, useLoad } from './parts.jsx';
import {
  EFFECT_LABEL, EFFECT_SEG, EFFECT_TONE, SOURCE_CLASS, SOURCE_LABEL, filterRegistry, itemPrecedenceNote, precedenceText, registryValue,
} from './sysModel.js';

const EDIT_PAGE = { sys: '系统设置', 'ops-ctl': '运维管控' };

export function RegistrySection() {
  const { api } = useApp();
  const { data, error, reload } = useLoad(() => api.configRegistry());
  const [q, setQ] = useState('');
  const [effect, setEffect] = useState('all');
  const top = data?.precedence || ['env', 'data_dir', 'pack', 'default'];
  const items = filterRegistry(data?.items, { effect, q });

  return (
    <>
      <SysHead title="配置注册表" />
      <div className="row mb12">
        <Inp icon="search" placeholder="搜索配置项" w="260px" value={q} onChange={(e) => setQ(e.target.value)} />
        <Seg items={EFFECT_SEG} value={effect} onChange={setEffect} />
        <span className="sp" />
        <span className="xs muted">优先级：{precedenceText(top)}</span>
      </div>
      {!data ? (error ? <LoadNote error={error} what="配置注册表" onRetry={reload} /> : <Loading />) : (
        <>
          <Table
            cols={['配置项', '当前值', '来源', '校验', '生效时机']}
            rows={items.map((it) => {
              const v = registryValue(it);
              const pn = itemPrecedenceNote(it, top);
              const title = [it.description, it.edit_page ? `在「${EDIT_PAGE[it.edit_page] || it.edit_page}」修改` : ''].filter(Boolean).join(' · ');
              return [
                <span className="mono" title={title || undefined}>{it.key}</span>,
                <span className={v.mono ? 'mono xs' : v.muted ? 'faint' : undefined}>{v.text}</span>,
                <>
                  <span className={`pl-src ${SOURCE_CLASS[it.source] || 'def'}`}>{SOURCE_LABEL[it.source] || it.source}</span>
                  {it.pack ? <> <span className="mono xs muted">{it.pack}</span></> : null}
                  {pn ? <> <span className="xs faint" title={pn.title}>{pn.text}</span></> : null}
                </>,
                it.validation || '—',
                <Chip tone={EFFECT_TONE[it.effect] || 'gray'} sm>{EFFECT_LABEL[it.effect] || it.effect}</Chip>,
              ];
            })}
          />
          {!items.length ? <div className="sm muted mt8">没有匹配的配置项。</div> : null}
        </>
      )}
    </>
  );
}
