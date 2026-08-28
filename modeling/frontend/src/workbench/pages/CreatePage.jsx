import { useRef, useState } from 'react';
import { createModelingProject, downloadTermTemplate, savePackageDataSource } from '../lib/api.js';
import { actions, store, materialPurposeLabel } from '../lib/appStore.js';
import Icon from '../ui/Icon.jsx';

export function initialUploadOptions(files) {
  return {
    followUp: true,
    directivesByName: Object.fromEntries(files.map((item) => [item.name, {
      purpose: item.purpose,
      readingMode: item.readingMode,
    }])),
  };
}

/* 唯一的全屏上传页。之后补资料一律在对话流里做。 */
export default function CreatePage() {
  const [over, setOver] = useState(false);
  const [files, setFiles] = useState([]);
  const [name, setName] = useState('');
  const [goal, setGoal] = useState('');
  const [error, setError] = useState('');
  const [creating, setCreating] = useState(false);
  const [packageDataSourceType, setPackageDataSourceType] = useState('neo4j');
  const input = useRef(null);

  const addFiles = (items) => {
    const incoming = [...(items || [])];
    setFiles((current) => {
      const names = new Set(current.map((item) => item.name));
      return [...current, ...incoming.filter((file) => !names.has(file.name)).map((file) => ({
        file, name: file.name, st: 'ready', meta: formatSize(file.size), purpose: 'auto', readingMode: 'on_demand',
      }))];
    });
  };

  const updateFile = (name, patch) => setFiles((current) => current.map((item) => item.name === name ? { ...item, ...patch } : item));

  const downloadTemplate = async () => {
    const response = await downloadTermTemplate();
    const blob = await response.blob();
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = 'term-table-template.xlsx';
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  };

  const start = async () => {
    if (!name.trim() || creating) return;
    setCreating(true); setError('');
    try {
      const result = await createModelingProject({ name: name.trim(), background: goal.trim(), domain: '' });
      const projectId = result.project.id;
      await savePackageDataSource(projectId, packageDataSourceType);
      store.set({
        demo: false,
        projectId,
        project: result.project,
        revision: null,
        workflow: null,
        files: [],
        materials: [],
        messages: [],
        uploads: [],
        userMessages: [],
        interaction: null,
        run: null,
        activity: null,
        statusText: '',
        error: '',
        selectedPath: '',
        selectedMaterial: '',
        focus: { kind: 'empty', target: '' },
        pendingFocus: null,
        follow: true,
        lastSeq: 0,
        replayUntilSeq: 0,
        autoStartSent: false,
      });
      const initialUpload = files.length
        ? actions.upload(files.map((item) => item.file), initialUploadOptions(files))
        : null;
      const url = new URL(window.location.href);
      url.searchParams.set('project', projectId);
      if (initialUpload) url.searchParams.delete('start');
      else url.searchParams.set('start', '1');
      window.history.replaceState(null, '', `${url.pathname}${url.search}#/workbench`);
      window.dispatchEvent(new HashChangeEvent('hashchange'));
      if (initialUpload) {
        void initialUpload.catch((cause) => {
          store.set({ error: cause instanceof Error ? cause.message : '初始资料上传失败' });
        });
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '项目创建失败');
      setCreating(false);
    }
  };

  return (
    <div className="wb-app">
      <header className="wb-top">
        <span className="goal">新建建模项目</span>
        <span className="sep">·</span>
        <span className="muted t-sm">第 1 步，共 1 步</span>
        <span className="spacer" />
        <span className="counts">已选 {files.length} 份资料</span>
      </header>

      <div className="wb-create">
        <div className="inner">
          <div className="wb-field">
            <label htmlFor="pn">项目名称</label>
            <input id="pn" className="input" value={name} onChange={(event) => setName(event.target.value)}
              placeholder="例如：备件库存知识包" autoFocus />
          </div>

          <div className="wb-field">
            <label htmlFor="pg">一句话说明这个知识包要解决什么问题（可以先粗略写，助手会带你确认）</label>
            <textarea id="pg" className="input" rows={2} value={goal} onChange={(event) => setGoal(event.target.value)}
              placeholder="先写一个粗略目标，助手会结合资料带你确认" />
          </div>

          <div className={`wb-drop${over ? ' over' : ''}`}
            onDragOver={(e) => { e.preventDefault(); setOver(true); }}
            onDragLeave={() => setOver(false)}
            onDrop={(e) => { e.preventDefault(); setOver(false); addFiles(e.dataTransfer.files); }}
            onClick={() => input.current?.click()} role="button" tabIndex={0}>
            <Icon name="upload" size={26} style={{ color: 'var(--c-faint)' }} />
            <div className="t1">把资料拖到这里</div>
            <div className="t2">md / txt / pdf / word / excel · 也可以点击选择 · 现在没有也可以直接开始</div>
          </div>
          <input ref={input} type="file" multiple hidden accept=".md,.txt,.pdf,.doc,.docx,.xls,.xlsx,.csv"
            onChange={(event) => { addFiles(event.target.files); event.target.value = ''; }} />

          <div style={{ marginTop: 'var(--s-5)' }}>
            {files.map((f) => {
              const table = /\.(?:xlsx?|csv)$/i.test(f.name);
              return <div className="wb-create-file" key={f.name}>
                <div className="wb-file">
                  <span className={`dot dot-${f.st === 'ok' ? 'ok' : 'new'}`} />
                  <span className="fn">{f.name}</span>
                  <span className="fs">{f.meta}</span>
                  <button className="btn btn-ghost btn-sm" onClick={() => setFiles((items) => items.filter((item) => item.name !== f.name))}>移除</button>
                </div>
                <div className="wb-material-directives">
                  <label><span>用途</span><select className="input" value={f.purpose} aria-label={`${f.name}的资料用途`}
                    onChange={(event) => updateFile(f.name, { purpose: event.target.value })}>
                    {['auto', 'goal', 'schema', 'term', 'evidence'].map((value) => <option key={value} value={value}
                      disabled={['schema', 'term'].includes(value) && !table}>{materialPurposeLabel(value)}</option>)}
                  </select></label>
                  {f.purpose === 'term' && <button className="btn btn-sm" type="button"
                    onClick={(event) => { event.stopPropagation(); void downloadTemplate(); }}>
                    <Icon name="download" size={12} />下载模板
                  </button>}
                  <label className="wb-material-reading"><input type="checkbox" checked={f.readingMode === 'full'}
                    onChange={(event) => updateFile(f.name, { readingMode: event.target.checked ? 'full' : 'on_demand' })} />
                    <span>全文精读</span></label>
                </div>
              </div>;
            })}
          </div>

          {error && <div className="card card-stale" style={{ marginTop: 'var(--s-5)' }}><div className="card-b">{error}</div></div>}

          <DataSourceSelector value={packageDataSourceType} disabled={creating} onChange={setPackageDataSourceType} />

          <div className="btn-row" style={{ marginTop: 'var(--s-7)' }}>
            <button className="btn btn-p" style={{ padding: '9px 22px' }} disabled={!name.trim() || creating}
              onClick={() => void start()}>{creating ? '正在创建…' : '开始建模'}</button>
          </div>
          <div className="t-sm muted" style={{ marginTop: 'var(--s-3)' }}>
            助手会按资料设置取证、和你确认建模目标，然后才动知识包正文。
          </div>
        </div>
      </div>
    </div>
  );
}

function DataSourceSelector({ value, disabled, onChange }) {
  const options = [
    {
      value: 'neo4j',
      title: 'Neo4j（默认）',
    },
    {
      value: 'nl2sql',
      title: 'NL2SQL',
    },
  ];
  return (
    <fieldset className="wb-data-source-choice" disabled={disabled}>
      <legend>
        <span>知识包数据源类型</span>
        <span className="wb-help" tabIndex={0} aria-label="知识包数据源类型说明">?
          <span className="wb-help-pop" role="tooltip">
            选择知识包后续要连接的数据源类型。Neo4j 适合导入图数据库后查询，适合关系较多的场景。NL2SQL 适合直接基于表格数据查询。后续切换类型需要重新生成知识包。
          </span>
        </span>
      </legend>
      <div className="wb-data-source-options">
        {options.map((option) => (
          <label key={option.value} className={`wb-radio-card ${value === option.value ? 'on' : ''}`}>
            <input type="radio" name="package-data-source" value={option.value}
              checked={value === option.value} onChange={() => onChange(option.value)} />
            <span>{option.title}</span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
