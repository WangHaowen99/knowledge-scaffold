// 流程图编辑弹窗（设计稿 flowchart）：图示包自带的编辑器（ADR-0055，随平台编译），编辑图示包登记的插件产物
// diagram.flowchart，经 AR-03 读、AR-05 存（带 base_revision）、AR-08 校验。
// artifact：{type, key, title?}；key 为空时打开 AR-02 的第一张。工作台的流程图卡和 Studio 表格的「打开编辑」打开的是同一个弹窗。
// 只读两态由写入范围锁（CS-06）决定：助手运行中 → 「回到对话」；合入冲突 → 去待处理；编辑区变灰、保存禁用。
import {
  useCallback, useContext, useEffect, useMemo, useReducer, useRef, useState,
} from 'react';
import { createPortal } from 'react-dom';
import { useApp, useProject } from '@onto/app/context.js';
import { navigate, pagePath } from '@onto/app/router.js';
import { artifactQuery } from '@onto/app/packUi.js';
import { toast } from '@onto/app/toast.jsx';
import { setWorkspaceDirty } from '@onto/lib/workspaceDirty.js';
import { describeConflict, errorText } from '@onto/api/errors.js';
import { DrawioEditorFrame, drawioStateText } from '@onto/components/DrawioView.jsx';
import DrawioRepairPanel from './DrawioRepairPanel.jsx';
import FlowchartEditor from './FlowchartEditor.jsx';
import { FlowchartSvg } from './FlowShapes.jsx';
import { modelToDrawio } from './drawioConvert.js';
import { drawioSourceCheck, inspectDrawioRepair, validateRepairedDrawio } from './drawioRepair.js';
import { createFlowchartEditorSession, reduceFlowchartEditorSession } from './flowchartEditorState.js';
import { checkSummary, summarize } from './flowchartModel.js';
import { isLocked, lockState, readonlyInfo } from '@onto/state/readonly.js';
import {
  Button, Chip, Empty, Icon, Modal, Note, OverlayHostCtx, St, cx,
} from '@onto/ui/index.jsx';
import { initialSelection, lockBarText } from './flowchartDialog.js';
import '@onto/styles/flowchart.css';

const TYPE = 'diagram.flowchart';
const PACK_LABEL = '图示';
const repairEdited = (c) => c?.kind === 'repair' && c.xml !== c.original;

function Overlay({ children }) {
  const host = useContext(OverlayHostCtx);
  return host ? createPortal(children, host) : children;
}

export default function FlowchartDialog({ artifact, onClose }) {
  const { api, config } = useApp();
  const {
    pid, scope, subscribe, reloadScope, hasArtifactType, extensions,
  } = useProject();
  const type = artifact?.type || TYPE;
  const [key, setKey] = useState(artifact?.key || null);
  const [art, setArt] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [empty, setEmpty] = useState(false);
  const [session, dispatch] = useReducer(reduceFlowchartEditorSession, null, () => createFlowchartEditorSession(null));
  const [fitKey, setFitKey] = useState('');
  const [mode, setMode] = useState('std');
  const [dio, setDio] = useState({ key: 0, xml: '', dirty: false, state: 'loading' });
  const [savedVersion, setSavedVersion] = useState(null);
  const [validating, setValidating] = useState(false);
  const [findings, setFindings] = useState(null);
  const [conflict, setConflict] = useState(null);
  const [askClose, setAskClose] = useState(false);
  // 文件原文的结构问题（drawioSourceCheck）：null 没有；repair 在标准编辑器的位置上逐项修；blocked 只能用 drawio 高级编辑。
  // 模型里看不出来：内核解析时同号的节点只认第一个，没连上节点的连线略过
  const [check, setCheck] = useState(null);
  const frame = useRef(null);
  const sessionRef = useRef(session);
  sessionRef.current = session;
  const modeRef = useRef(mode);
  modeRef.current = mode;
  const checkRef = useRef(check);
  checkRef.current = check;
  const dioAsk = useRef(0);
  const checkAsk = useRef(0);
  const checkSeq = useRef(0);
  const repairing = check?.kind === 'repair';
  const repairDirty = repairEdited(check);
  const repairLeft = useMemo(() => (repairing ? inspectDrawioRepair(check.xml).length : 0), [repairing, check?.xml]);
  const dirtyRef = useRef(false);
  dirtyRef.current = session.dirty || dio.dirty || repairDirty || session.saveInFlight;
  // 流程图有没保存的修改或正在保存时挡住版本回退（develop 同）
  useEffect(() => {
    setWorkspaceDirty('flowchart-editor', session.dirty || session.saveStatus === 'saving');
    return () => setWorkspaceDirty('flowchart-editor', false);
  }, [session.dirty, session.saveStatus]);
  useEffect(() => {
    setWorkspaceDirty('flowchart-repair', repairDirty);
    return () => setWorkspaceDirty('flowchart-repair', false);
  }, [repairDirty]);

  // 有未保存修改时关页面、刷新先问一句
  useEffect(() => {
    const guard = (e) => {
      if (!dirtyRef.current) return;
      e.preventDefault();
      e.returnValue = '';
    };
    window.addEventListener('beforeunload', guard);
    return () => window.removeEventListener('beforeunload', guard);
  }, []);

  const compressed = session.model.storage === 'drawio_compressed';
  const ref = { type, key: key || artifact?.key || '' };
  // 只读按写入范围：锁里有这张图，或它的文件落在锁的路径里（和 AR-05 回 423 同一口径，同上传包编辑器的 editorReadonly）
  const lock = scope && key && isLocked(scope, art?.path ? { ...ref, path: art.path } : ref) ? scope : null;
  const ro = !!lock;
  const dirty = session.dirty || dio.dirty || repairDirty;
  const title = session.model.process.name || art?.title || artifact?.title || '流程图';
  const pluginOff = !!extensions && !hasArtifactType(type);

  // drawio 高级编辑打开的 XML（AR-10）：文件原文；有未保存修改时服务端把模型合进原文，尺寸、样式、泳道、批注、其他页都在。
  // 看的是某一轮的版本、或者取不到时按模型生成（带内核属性，描述、条件等随保存一起交回）
  const dioXml = useCallback(async (model, k, title, dirty) => {
    if (model?.storage === 'drawio_compressed') return model.drawio_xml || '';
    if (k && !artifact?.changeset) {
      try {
        const r = await api.artifactDrawio(pid, type, k, dirty ? { content: model } : {});
        if (r?.xml) return r.xml;
      } catch { /* 按模型生成 */ }
    }
    return modelToDrawio(model, title);
  }, [api, pid, type, artifact?.changeset]);

  // 看的是某一轮的版本时没有原文可查，按模型打开
  const sourceCheck = useCallback(async (k) => {
    if (!k || artifact?.changeset) return null;
    const r = await api.artifactDrawio(pid, type, k, {});
    const found = drawioSourceCheck(r?.xml);
    return found ? { ...found, xml: r.xml, original: r.xml, revision: r.revision, seq: ++checkSeq.current } : null;
  }, [api, pid, type, artifact?.changeset]);

  const applyCheck = useCallback((found) => {
    setCheck(found);
    if (found?.kind !== 'blocked' || modeRef.current === 'dio') return;
    setDio((d) => ({ ...d, key: d.key + 1, xml: found.xml, dirty: false, state: 'loading' }));
    setMode('dio');
  }, []);

  const recheck = (k) => {
    const asked = ++checkAsk.current;
    sourceCheck(k).then((found) => {
      if (asked === checkAsk.current && !sessionRef.current.dirty && !repairEdited(checkRef.current)) applyCheck(found);
    }).catch(() => {});
  };

  // 载入的代次：先发的请求后回来（换了图、又点了载入最新版本）时丢掉旧结果
  const loadGen = useRef(0);
  const load = useCallback(async () => {
    const gen = ++loadGen.current;
    setLoadError(null);
    try {
      let k = artifact?.key || null;
      if (!k) {
        const r = await api.artifacts(pid, type, { limit: 1 });
        if (gen !== loadGen.current) return;
        k = r.items?.[0]?.key || null;
        if (!k) { setEmpty(true); return; }
      }
      const [a, found] = await Promise.all([
        api.artifact(pid, type, k, artifactQuery(artifact, type, k)),
        sourceCheck(k).catch(() => null), // 取不到原文时按模型打开
      ]);
      if (gen !== loadGen.current) return;
      checkAsk.current += 1;
      setKey(k);
      setArt(a);
      setCheck(found);
      dispatch({ type: 'replace-model', model: a.content, savedRevision: a.revision, force: true, selection: initialSelection(a.content) });
      setFitKey(`${k}:${a.revision}`);
      // 压缩存储、结构问题修不了的图只能用 drawio 高级编辑；已经在 drawio 里时（载入最新版本）编辑器也换成新内容
      const packed = a.content?.storage === 'drawio_compressed';
      if (packed || found?.kind === 'blocked' || modeRef.current === 'dio') {
        const xml = found ? found.xml : await dioXml(a.content, k, a.title, false);
        if (gen !== loadGen.current) return;
        setDio((d) => ({ ...d, key: d.key + 1, xml, dirty: false }));
        setMode('dio');
      }
    } catch (e) {
      if (gen === loadGen.current) setLoadError(e);
    }
  }, [api, pid, type, artifact?.key, artifact?.changeset]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { load(); }, [load]);

  // 助手或别处改了这张图：没有未保存修改时直接换成最新版本
  useEffect(() => subscribe?.((env) => {
    if (env.type !== 'workspace.changed' || !key) return;
    if (!(env.data?.artifacts || []).some((a) => a.type === type && a.key === key)) return;
    const s = sessionRef.current;
    if (s.dirty || s.saveInFlight || repairEdited(checkRef.current)) return;
    const asked = ++checkAsk.current;
    Promise.all([api.artifact(pid, type, key), sourceCheck(key).catch(() => undefined)]).then(([a, found]) => {
      if (sessionRef.current.dirty || sessionRef.current.saveInFlight || repairEdited(checkRef.current)) return;
      setArt(a);
      dispatch({ type: 'replace-model', model: a.content, savedRevision: a.revision });
      if (found !== undefined && asked === checkAsk.current) applyCheck(found);
    }).catch(() => {});
  }), [subscribe, api, pid, type, key, sourceCheck, applyCheck]);

  const persist = async (content, base, how = {}) => {
    dispatch({ type: 'mark-saving' });
    try {
      const r = await api.saveArtifact(pid, type, key, {
        base_revision: base ?? sessionRef.current.savedRevision, content, summary: how.summary || '在流程图弹窗里修改',
      });
      setArt(r.artifact);
      dispatch({ type: 'mark-saved', model: r.artifact?.content, savedRevision: r.artifact?.revision });
      setSavedVersion(r.version_label || null);
      // 存的是整份 XML：修好了回标准编辑器，drawio 里改出来的结构问题也在这时发现
      if (content.drawio_xml) {
        if (checkRef.current?.kind === 'repair') setCheck(null);
        recheck(key);
      }
      // 包声明的检查只提示（F3b）：照常保存，没过的说一句
      const warn = r.warnings?.[0]?.message;
      const saved = r.version_label ? `已保存为版本 ${r.version_label}` : '已保存';
      toast([how.done, saved, warn].filter(Boolean).join(' · '), warn ? 'alert' : 'check');
      return r;
    } catch (e) {
      dispatch({ type: 'mark-save-failed', message: errorText(e) });
      if (e.isConflict) setConflict({ details: e.details, content, how });
      else if (e.isLocked) { toast(errorText(e), 'lock'); reloadScope?.(); } else toast(errorText(e), 'alert');
      throw e;
    }
  };

  // 修复后的原文按修之前读到的修订存：期间文件被改过时走冲突提示，不会盖掉别人的修改
  const saveRepair = () => {
    try {
      validateRepairedDrawio(check.xml);
    } catch (e) {
      toast(e.message, 'alert');
      return;
    }
    persist({ ...session.model, drawio_xml: check.xml }, check.revision, { summary: '在流程图弹窗里修复结构', done: '结构已修复' }).catch(() => {});
  };

  const save = () => {
    if (ro || session.saveInFlight) return;
    if (mode === 'dio' && frame.current?.ready) { frame.current.save(); return; }
    if (repairDirty) { saveRepair(); return; }
    if (!session.dirty) return;
    persist({ ...session.model, drawio_xml: null }).catch(() => {});
  };

  // drawio 编辑器里保存（Ctrl+S 或上面的「保存」）：交 XML，服务端解析成模型
  const saveXml = async (xml) => {
    await persist({ ...sessionRef.current.model, drawio_xml: xml });
    setDio((d) => ({ ...d, dirty: false }));
  };

  const switchMode = (m) => {
    if (m === 'std') {
      if (compressed) { toast('压缩格式只能在 drawio 里编辑', 'info'); return; }
      if (dio.dirty) { toast('先保存（Ctrl+S）再切回', 'alert'); return; }
      const asked = ++dioAsk.current;
      if (check?.kind !== 'blocked') { setMode('std'); return; }
      // 打不开的图切回时重新读一遍文件：已经改好了就打开
      checkAsk.current += 1;
      sourceCheck(key).then((found) => {
        if (asked !== dioAsk.current) return;
        setCheck(found);
        if (found?.kind === 'blocked') toast(`标准编辑器无法打开：${found.message}`, 'alert');
        else setMode('std');
      }).catch((e) => toast(errorText(e), 'alert'));
      return;
    }
    const asked = ++dioAsk.current;
    // 修到一半切过去，带着已经修好的部分
    if (repairing) {
      setDio((d) => ({ key: d.key + 1, xml: check.xml, dirty: false, state: 'loading' }));
      setMode('dio');
      return;
    }
    dioXml(session.model, key, title, session.dirty).then((xml) => {
      if (asked !== dioAsk.current) return; // 等原文时又切走了
      setDio((d) => ({ key: d.key + 1, xml, dirty: false, state: 'loading' }));
      setMode('dio');
    });
  };

  const validate = async () => {
    if (!key) return;
    setValidating(true);
    try {
      const r = await api.validateArtifact(pid, type, key, mode === 'std' ? { content: session.model } : {});
      setFindings(r.errors?.length ? r : null);
      toast(checkSummary(r), r.errors?.length ? 'alert' : 'shieldok');
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setValidating(false);
    }
  };

  const requestClose = useCallback(() => {
    if (askClose || conflict) return;
    if (sessionRef.current.dirty || dio.dirty || repairDirty) setAskClose(true);
    else onClose?.();
  }, [askClose, conflict, dio.dirty, repairDirty, onClose]);

  useEffect(() => {
    const h = (e) => { if (e.key === 'Escape' && !e.defaultPrevented) requestClose(); };
    document.addEventListener('keydown', h);
    return () => document.removeEventListener('keydown', h);
  }, [requestClose]);

  const info = lock ? lockBarText(lock, readonlyInfo(lock)) : null;
  const sum = summarize(session.model);
  const op = session.model.operation;

  let status = null;
  if (ro) status = <Chip tone="gray" icon="lock">只读</Chip>;
  else if (session.saveStatus === 'saving') status = <Chip tone="gray"><span className="spin" />保存中…</Chip>;
  else if (session.saveStatus === 'error') status = <Chip tone="red" icon="alert">保存失败</Chip>;
  else if (dirty) status = <Chip tone="amber" dot>有未保存修改</Chip>;
  else if (savedVersion) status = <Chip tone="teal" icon="check">已保存 · 版本 {savedVersion}</Chip>;

  const goConfirm = () => { onClose?.(); navigate(pagePath('wb-l2', pid)); };
  const lockAction = {
    assistant_running: <Button size="xs" onClick={() => onClose?.()}>回到对话</Button>,
    pending_confirm: <Button size="xs" kind="primary" icon="merge" onClick={goConfirm}>去确认</Button>,
    autobuild_slice: <Button size="xs" onClick={() => { onClose?.(); navigate(pagePath('l4-run', pid)); }}>查看运行</Button>,
  }[lockState(lock)];

  const drawioPane = (
    <>
      <div className="dtb">
        <b>draw.io</b>
        <span className="muted">{drawioStateText(dio.state)}</span>
      </div>
      {compressed ? <div style={{ padding: '12px 16px' }}><Note tone="amber" icon="info">压缩格式，只能在 drawio 里编辑</Note></div> : null}
      {check?.kind === 'blocked' ? <div style={{ padding: '12px 16px' }}><Note tone="red">标准编辑器无法打开：{check.message}</Note></div> : null}
      <div className="fc-dcv">
        <DrawioEditorFrame
          key={dio.key} ref={frame} xml={dio.xml} title={title} available={config?.features?.drawio !== false}
          onStatus={(s) => setDio((d) => (d.state === s ? d : { ...d, state: s }))}
          onChange={() => setDio((d) => (d.dirty ? d : { ...d, dirty: true }))}
          onSave={saveXml}
          fallback={(st, retry) => (
            <div className="fc-dfb">
              <FlowchartSvg model={session.model} selection={session.selection} />
              {st !== 'unavailable' ? <span className="act"><Button size="xs" icon="refresh" onClick={retry}>重新加载编辑器</Button></span> : null}
            </div>
          )}
        />
      </div>
    </>
  );

  let body;
  if (pluginOff) {
    body = (
      <div className="fc-load">
        <Empty icon="puzzle" title="图示插件没有启用" actions={<Button size="sm" onClick={() => { onClose?.(); navigate(pagePath('plugin-detail'), { pack: 'diagram' }); }}>去启用</Button>} />
      </div>
    );
  } else if (empty) {
    body = <div className="fc-load"><Empty icon="branch" title="还没有流程图" /></div>;
  } else if (loadError) {
    body = (
      <div className="fc-load">
        <Note tone="red">{errorText(loadError)} <Button size="xs" kind="ghost" icon="refresh" onClick={load}>重试</Button></Note>
      </div>
    );
  } else if (!art) {
    body = <div className="fc-load" role="status"><span className="spin" />正在读取流程图…</div>;
  } else {
    body = (
      <FlowchartEditor
        session={session} dispatch={dispatch} mode={mode} onModeChange={switchMode} drawio={drawioPane}
        readOnly={ro} onValidate={validate} validating={validating} findings={findings}
        onDismissFindings={() => setFindings(null)} fitKey={fitKey}
        repair={repairing ? (
          <DrawioRepairPanel key={check.seq} xml={check.xml} readOnly={ro} onChange={(xml) => setCheck((c) => (c ? { ...c, xml } : c))} />
        ) : null}
      />
    );
  }

  return (
    <Overlay>
      <div className="overlay" onMouseDown={(e) => e.stopPropagation()}>
        <div className={cx('fcm', ro && 'ro')} role="dialog" aria-modal="true" aria-label={title}>
          <div className="fcm-h">
            <span className="ik"><Icon name="branch" size={18} /></span>
            <div className="grow" style={{ minWidth: 0 }}>
              <div className="row">
                <h3 className="ell">{title}</h3>
                {art?.status ? <St k={art.status.value} sm label={art.status.label} /> : null}
                <span
                  className="plg-tag" data-pop={`由「${PACK_LABEL}」插件提供`}
                  onClick={() => (dirty ? toast('先保存或放弃这张图的修改，再去看插件', 'info') : navigate(pagePath('plugin-detail'), { pack: 'diagram' }))}
                >
                  <Icon name="puzzle" size={12} />
                </span>
              </div>
              <div className="xs muted">{art ? `${op ? `关联操作「${op.name}」· ` : ''}${sum.text}` : ' '}</div>
            </div>
            {status}
            <Button
              size="sm" kind="primary" icon="check" onClick={save}
              disabled={ro || !art || session.saveInFlight || !dirty || (mode === 'std' && repairLeft > 0)}
            >
              保存
            </Button>
            <button type="button" className="icon-btn" title="关闭" aria-label="关闭" onClick={requestClose}><Icon name="x" /></button>
          </div>
          {info ? (
            <div className="fcm-lock">
              <Icon name="lock" size={14} />
              <b>{info.title}</b>
              <span className="sp" />
              {lockAction}
            </div>
          ) : null}
          {body}
        </div>
      </div>
      <Modal
        open={askClose} onClose={() => setAskClose(false)} icon="alert" title="这张图还有没保存的修改" w={440}
        foot={(
          <>
            <Button onClick={() => setAskClose(false)}>继续编辑</Button>
            <Button kind="danger" onClick={() => { setAskClose(false); onClose?.(); }}>放弃修改并关闭</Button>
          </>
        )}
      >
        <div className="sm muted">关闭后这些修改会丢掉。</div>
      </Modal>
      <Modal
        open={!!conflict} onClose={() => setConflict(null)} icon="alert" title="这张流程图已经被改过了" w={480}
        foot={(
          <>
            <Button onClick={() => setConflict(null)}>继续编辑</Button>
            <Button
              onClick={() => {
                const c = conflict;
                setConflict(null);
                persist(c.content, c.details?.actual, c.how).catch(() => {});
              }}
              disabled={!conflict?.details?.actual}
            >
              用我的版本覆盖
            </Button>
            <Button kind="primary" onClick={() => { setConflict(null); setDio((d) => ({ ...d, dirty: false })); load(); }}>载入最新版本</Button>
          </>
        )}
      >
        <div className="sm muted">
          {describeConflict(conflict?.details).replace(/^文件/, '这张图').replace('请刷新对比后再保存', '你的修改还在')}。
        </div>
      </Modal>
    </Overlay>
  );
}
