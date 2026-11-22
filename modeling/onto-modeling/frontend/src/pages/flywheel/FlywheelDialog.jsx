import { Chip, Icon, Modal, Note } from '../../ui/index.jsx';
import { Loading, OptimizeCard, PublishCard } from './FlywheelCards.jsx';
import FlywheelRecords from './FlywheelRecords.jsx';
import { connection, version } from './flywheelModel.js';

const WIDTH = 'min(1080px, calc(100vw - 64px))';

export default function FlywheelDialog({ open, state, error, records, onClose, onAction }) {
  // 评测连接正常时不占位，只有异常才显示
  const link = state?.available ? connection(state) : null;
  let body;
  if (!state) {
    body = error ? <Note tone="red">数据飞轮状态读取失败：{error}</Note> : <Loading text="正在读取数据飞轮…" />;
  } else if (!state.available) {
    body = <Note tone="gray">{state.message}</Note>;
  } else {
    body = (
      <div className="fw-stack">
        {state.message ? <Note tone="gray">{state.message}</Note> : null}
        {error ? <Note tone="amber">同步失败，显示的是最近一次状态：{error}</Note> : null}
        {!state.can_write ? <Note tone="gray" icon="lock">只读：可以查看，不能修改开关或执行操作。</Note> : null}
        <OptimizeCard state={state} onAction={onAction} />
        <PublishCard state={state} onAction={onAction} />
        <FlywheelRecords state={state} recordsRef={records.ref} tab={records.tab} filter={records.filter}
          onTab={records.setTab} onFilter={records.setFilter} onAction={onAction} />
      </div>
    );
  }
  const title = (
    <span className="fw-title">
      知识包数据飞轮
      {link && link.color !== 'green' ? <Chip sm tone={link.color} dot>{link.text}</Chip> : null}
      <span className="sp" />
      {state?.available ? (
        <button type="button" className="icon-btn" aria-label="设置" title="设置" onClick={() => onAction('settings')}>
          <Icon name="gear" />
        </button>
      ) : null}
    </span>
  );
  return (
    <Modal open={open} onClose={onClose} w={WIDTH} icon="recycle" className="fw-dialog" title={title}
      sub={state?.available ? `${state.project.name} · ${version(state.package?.version)}` : undefined}>
      {body}
    </Modal>
  );
}
