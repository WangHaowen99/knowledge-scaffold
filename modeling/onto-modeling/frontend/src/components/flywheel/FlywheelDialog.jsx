import { Modal } from 'antd';
import { connection, version } from '../../lib/flywheel.js';
import { Banner, OptimizeCard, PublishCard } from './FlywheelCards.jsx';
import FlywheelRecords from './FlywheelRecords.jsx';
import Icon from './Icon.jsx';

// 弹框宽度；右侧抽屉按它算出与弹框右边界相接的宽度
export const MODAL_WIDTH = 'min(1168px, calc(100vw - 64px))';

export default function FlywheelDialog({ open, state, error, records, onClose, onAction }) {
  // 评测连接正常时不占位，只有异常才显示
  const link = state?.available ? connection(state) : null;
  let body;
  if (!state) {
    body = error
      ? <Banner color="red">数据飞轮状态读取失败：{error}</Banner>
      : <div className="fw-loading" role="status"><span className="fw-spinner" aria-hidden="true" />正在读取数据飞轮…</div>;
  } else if (!state.available) {
    body = <Banner color="neutral">{state.message}</Banner>;
  } else {
    body = (
      <>
        {state.message && <div className="fw-notice"><Banner color="neutral">{state.message}</Banner></div>}
        {error && <div className="fw-notice"><Banner color="amber">同步失败，显示的是最近一次状态：{error}</Banner></div>}
        {!state.can_write && <div className="fw-notice"><Banner color="neutral">只读：可以查看，不能修改开关或执行操作。</Banner></div>}
        <OptimizeCard state={state} onAction={onAction} />
        <PublishCard state={state} onAction={onAction} />
        <FlywheelRecords state={state} recordsRef={records.ref} tab={records.tab} filter={records.filter}
          onTab={records.setTab} onFilter={records.setFilter} onAction={onAction} />
      </>
    );
  }
  return (
    <Modal open={open} onCancel={onClose} footer={null} closable={false} centered width={MODAL_WIDTH}
      rootClassName="fw-scope fw-modal-root" destroyOnHidden>
      <section className="fw-modal">
        <header className="fw-header">
          <div className="fw-mark"><Icon name="flywheel" /></div>
          <h1 id="fw-title">知识包数据飞轮</h1>
          {state?.available && (
            <span className="fw-header-meta fw-mono">{state.project.name} · {version(state.package.version)}</span>
          )}
          <span className="fw-spacer" />
          {link && link.color !== 'green' && <span className="fw-header-status"><span className={`fw-dot ${link.color}`} />{link.text}</span>}
          {state?.available && (
            <button type="button" className="fw-icon-button" aria-label="设置" title="设置" onClick={() => onAction('settings')}>
              <Icon name="gear" />
            </button>
          )}
          <button type="button" className="fw-icon-button" aria-label="关闭知识包数据飞轮" title="关闭（Esc）" onClick={onClose}>
            <Icon name="close" />
          </button>
        </header>
        <div className="fw-body">{body}</div>
      </section>
    </Modal>
  );
}
