import { useRef, useState } from 'react';
import { message } from 'antd';
import { api } from '../../lib/api.js';
import { MATERIAL_PURPOSES } from '../LeftPane.jsx';
import './modeling-home.css';

/* 全屏新建项目页 —— 结构与样式复刻 modeling workbench/pages/CreatePage.jsx。
   资料用途/精读模式随上传写入 resource/materials.json；无数据源选择（onto-web 不区分）。 */
export default function CreatePage({ onBack, onCreated }) {
  const [over, setOver] = useState(false);
  const [files, setFiles] = useState([]);
  const [name, setName] = useState('');
  const [goal, setGoal] = useState('');
  const [error, setError] = useState('');
  const [creating, setCreating] = useState(false);
  const [uploadNote, setUploadNote] = useState('');
  const input = useRef(null);

  const addFiles = (items) => {
    const incoming = [...(items || [])];
    setFiles((current) => {
      const names = new Set(current.map((item) => item.name));
      return [
        ...current,
        ...incoming
          .filter((file) => !names.has(file.name))
          .map((file) => ({
            file,
            name: file.name,
            meta: formatSize(file.size),
            purpose: 'auto',
            readingMode: 'on_demand',
          })),
      ];
    });
  };

  const updateFile = (name, patch) =>
    setFiles((current) => current.map((item) => (item.name === name ? { ...item, ...patch } : item)));

  const start = async (template = 'blank') => {
    if (!name.trim() || creating) return;
    setCreating(true);
    setError('');
    try {
      const created = await api.createProject(name.trim(), template, goal.trim());
      // 初始资料并行上传（带用途标签）；单个失败不阻断进入项目
      let done = 0;
      setUploadNote(files.length ? `0/${files.length}` : '');
      await Promise.all(
        files.map((item) =>
          api
            .uploadResource(
              created.id,
              autoSubdir(item.name),
              item.file,
              undefined,
              item.purpose,
              item.readingMode,
            )
            .then(() => {
              done += 1;
              setUploadNote(`${done}/${files.length}`);
            })
            .catch((cause) => message.warning(`${item.name} 上传失败：${cause.message}`)),
        ),
      );
      onCreated(created);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '项目创建失败');
      setCreating(false);
    }
  };

  return (
    <div className="wb mwr shell" style={{ background: 'var(--c-bg, #f2f4f8)' }}>
      <header className="wb-top">
        <button type="button" className="btn btn-ghost btn-sm" onClick={onBack} title="返回项目列表">
          ← 项目
        </button>
        <span className="goal">新建建模任务</span>
        <span className="sep">·</span>
        <span className="muted t-sm">第 1 步，共 1 步</span>
        <span className="spacer" />
        <span className="counts muted t-sm">已选 {files.length} 份资料</span>
      </header>

      <div className="wb-create">
        <div className="inner">
          <div className="wb-field">
            <label htmlFor="pn">任务名称</label>
            <input
              id="pn"
              className="input"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="例如：电商订单建模"
              autoFocus
            />
          </div>

          <div className="wb-field">
            <label htmlFor="pg">一句话说明这个知识包要解决什么问题（可以先粗略写，助手会带你确认）</label>
            <textarea
              id="pg"
              className="input"
              rows={2}
              value={goal}
              onChange={(event) => setGoal(event.target.value)}
              placeholder="先写一个粗略目标，助手会结合资料带你确认"
            />
          </div>

          <div
            className={`wb-drop${over ? ' over' : ''}`}
            onDragOver={(e) => {
              e.preventDefault();
              setOver(true);
            }}
            onDragLeave={() => setOver(false)}
            onDrop={(e) => {
              e.preventDefault();
              setOver(false);
              addFiles(e.dataTransfer.files);
            }}
            onClick={() => input.current?.click()}
            onKeyDown={(event) => {
              if (!['Enter', ' '].includes(event.key)) return;
              event.preventDefault();
              input.current?.click();
            }}
            role="button"
            tabIndex={0}
            aria-label="选择要上传的建模资料"
          >
            <div className="t1">把资料拖到这里</div>
            <div className="t2">md / txt / pdf / word / excel / drawio · 也可以点击选择 · 现在没有也可以直接开始</div>
          </div>
          <input
            ref={input}
            type="file"
            multiple
            hidden
            accept=".md,.txt,.pdf,.doc,.docx,.xls,.xlsx,.csv,.drawio"
            onChange={(event) => {
              addFiles(event.target.files);
              event.target.value = '';
            }}
          />

          <div style={{ marginTop: 12 }}>
            {files.map((f) => (
              <div className="wb-create-file" key={f.name}>
                <div className="wb-file">
                  <span className="dot dot-new" style={{ background: 'var(--c-accent, #2a78d6)' }} />
                  <span className="fn">{f.name}</span>
                  <span className="fs">{f.meta}</span>
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    onClick={() => setFiles((items) => items.filter((item) => item.name !== f.name))}
                  >
                    移除
                  </button>
                </div>
                <div className="wb-material-directives">
                  <label>
                    <span>用途</span>
                    <select
                      className="input"
                      value={f.purpose}
                      aria-label={`${f.name}的资料用途`}
                      onChange={(event) => updateFile(f.name, { purpose: event.target.value })}
                    >
                      {MATERIAL_PURPOSES.map((opt) => (
                        <option
                          key={opt.value}
                          value={opt.value}
                          disabled={opt.tableOnly && !/\.(xlsx?|csv)$/i.test(f.name)}
                        >
                          {opt.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="wb-material-reading">
                    <input
                      type="checkbox"
                      checked={f.readingMode === 'full'}
                      onChange={(event) =>
                        updateFile(f.name, { readingMode: event.target.checked ? 'full' : 'on_demand' })
                      }
                    />
                    <span>全文精读</span>
                  </label>
                </div>
              </div>
            ))}
          </div>

          {error && (
            <div className="card card-stale" style={{ marginTop: 12 }}>
              <div className="card-b">{error}</div>
            </div>
          )}

          <div className="btn-row" style={{ marginTop: 20 }}>
            <button
              type="button"
              className="btn btn-p"
              style={{ padding: '9px 22px' }}
              disabled={!name.trim() || creating}
              onClick={() => void start('blank')}
            >
              {creating ? (uploadNote ? `正在上传资料 ${uploadNote}…` : '正在创建…') : '开始建模'}
            </button>
            <button
              type="button"
              className="btn"
              disabled={creating}
              onClick={() => {
                if (!name.trim()) setName('外卖订单示例');
                void start('example');
              }}
            >
              注入外卖订单示例
            </button>
            <button type="button" className="btn btn-ghost" onClick={onBack} title="取消并返回项目列表">
              取消建模
            </button>
          </div>
          {!name.trim() && (
            <div className="wb-disabled-reason" role="status">
              填写任务名称后即可开始建模。
            </div>
          )}
          <div className="t-sm muted" style={{ marginTop: 8 }}>
            助手会先做资料分析与目标确认，然后才动知识包正文。
          </div>
        </div>
      </div>
    </div>
  );
}

function autoSubdir(filename) {
  const lower = filename.toLowerCase();
  if (lower.endsWith('.xlsx') || lower.endsWith('.xls')) return 'excel';
  if (lower.endsWith('.drawio')) return 'diagrams';
  return 'others';
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
