import { useEffect, useState } from 'react';
import { ArrowLeft, Check, CircleOff, FlaskConical, LoaderCircle, Play, RefreshCw, TriangleAlert } from 'lucide-react';

import {
  armAcceptanceFault,
  closeAllAcceptanceFaults,
  closeAcceptanceFault,
  createAcceptanceScenario,
  getAcceptanceEnvironment,
} from './api.js';

const FAULT_STATUS = {
  armed: ['故障尚未消费', 'armed'],
  consumed: ['已消费', 'consumed'],
  closed: ['已关闭', 'closed'],
};

export default function AcceptancePage({ onOpenProject, onBack }) {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [created, setCreated] = useState(null);

  const load = async () => {
    try {
      setData(await getAcceptanceEnvironment());
      setError('');
    } catch (cause) {
      setError(cause.message || '验收环境读取失败');
    }
  };

  useEffect(() => { void load(); }, []);

  const create = async (scenarioId) => {
    setBusy(`scenario:${scenarioId}`); setError(''); setCreated(null);
    try {
      const result = await createAcceptanceScenario(scenarioId);
      setCreated(result);
    } catch (cause) {
      setError(cause.message || '合成场景创建失败');
    } finally {
      setBusy('');
    }
  };

  const changeFault = async (faultId, action) => {
    setBusy(`fault:${faultId}`); setError(''); setNotice('');
    try {
      if (action === 'arm') await armAcceptanceFault(faultId);
      else await closeAcceptanceFault(faultId);
      await load();
    } catch (cause) {
      setError(cause.message || '故障场景更新失败');
    } finally {
      setBusy('');
    }
  };

  const closeAllFaults = async () => {
    setBusy('fault:all'); setError(''); setNotice('');
    try {
      const result = await closeAllAcceptanceFaults();
      setData((current) => current ? { ...current, faults: result.faults || [] } : current);
      setNotice('全部故障已关闭');
    } catch (cause) {
      setError(cause.message || '关闭验收故障失败');
    } finally {
      setBusy('');
    }
  };

  return <main className="acceptance-page" aria-labelledby="acceptance-title">
    <header className="acceptance-head">
      <button type="button" className="wb-icon-btn" title="返回项目列表" aria-label="返回项目列表" onClick={onBack}>
        <ArrowLeft size={18} />
      </button>
      <div><h1 id="acceptance-title">浏览器验收场景</h1><p>全部内容为人工合成数据；故障仅对当前账号下一次对应操作生效。</p></div>
      <button type="button" className="btn btn-sm" disabled={Boolean(busy)} onClick={() => void load()}>
        <RefreshCw size={14} /> 刷新状态
      </button>
    </header>

    {error && <div className="acceptance-alert error" role="alert"><TriangleAlert size={17} />{error}</div>}
    {notice && <div className="acceptance-alert success" role="status"><Check size={17} />{notice}</div>}
    {created && <div className="acceptance-alert success" role="status">
      <Check size={17} /><span>已创建「{created.project.name}」</span>
      <button type="button" className="btn btn-p btn-sm" onClick={() => onOpenProject(created.project)}>
        <Play size={14} /> 进入工作台
      </button>
    </div>}

    {!data && !error && <div className="empty"><LoaderCircle className="wb-spin" size={18} /> 正在读取验收环境</div>}
    {data && <>
      <section className="acceptance-band" aria-labelledby="scenario-title">
        <div className="acceptance-band-title"><FlaskConical size={18} /><div><h2 id="scenario-title">合成基线</h2><p>点击后通过正式项目服务建立独立项目和知识包基线。</p></div></div>
        <div className="acceptance-scenarios">
          {data.scenarios.map((scenario) => <article key={scenario.id} className="acceptance-scenario-row">
            <code>{scenario.id}</code><div><b>{scenario.title}</b><span>{scenario.description}</span></div>
            <button type="button" className="btn btn-p btn-sm" data-testid={`create-${scenario.id}`} disabled={Boolean(busy)}
              onClick={() => void create(scenario.id)}>
              {busy === `scenario:${scenario.id}` ? <LoaderCircle className="wb-spin" size={14} /> : <Play size={14} />}
              创建项目
            </button>
          </article>)}
        </div>
      </section>

      <section className="acceptance-band" aria-labelledby="fault-title">
        <div className="acceptance-band-title"><TriangleAlert size={18} /><div><h2 id="fault-title">一次性故障</h2><p>只能选择固定行为，不能输入代码、地址或任意请求。</p></div>
          <button type="button" className="btn btn-sm" data-testid="close-all-faults" disabled={Boolean(busy)}
            onClick={() => void closeAllFaults()}>
            {busy === 'fault:all' ? <LoaderCircle className="wb-spin" size={14} /> : <CircleOff size={14} />}
            关闭全部故障
          </button>
        </div>
        <div className="acceptance-faults" role="table" aria-label="浏览器验收故障状态">
          <div className="acceptance-fault-head" role="row"><span>编号与行为</span><span>当前状态</span><span>消费次数</span><span>操作</span></div>
          {data.faults.map((fault) => {
            const [label, tone] = FAULT_STATUS[fault.status] || FAULT_STATUS.closed;
            return <div className="acceptance-fault-row" role="row" data-testid={`fault-${fault.id}`} key={fault.id}>
              <div><code>{fault.id}</code><span>{fault.description}</span></div>
              <strong className={`acceptance-state ${tone}`}>{label}</strong>
              <span className="acceptance-count">{fault.consumedCount} 次</span>
              <div className="acceptance-fault-actions">
                <button type="button" className="btn btn-sm" data-testid={`arm-${fault.id}`} disabled={Boolean(busy) || fault.status === 'armed'}
                  onClick={() => void changeFault(fault.id, 'arm')}>
                  {busy === `fault:${fault.id}` ? <LoaderCircle className="wb-spin" size={14} /> : <Play size={14} />}
                  仅下次生效
                </button>
                <button type="button" className="wb-icon-btn" data-testid={`close-${fault.id}`} title={`关闭 ${fault.id}`} aria-label={`关闭 ${fault.id}`}
                  disabled={Boolean(busy) || fault.status === 'closed'} onClick={() => void changeFault(fault.id, 'close')}>
                  <CircleOff size={16} />
                </button>
              </div>
            </div>;
          })}
        </div>
      </section>
    </>}
  </main>;
}
