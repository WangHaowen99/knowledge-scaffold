import { Modal } from '../../../shared/ui/primitives';
import {
  IMPORT_ACTION,
  IMPORT_PHASE,
  IMPORT_SAFETY_COPY,
  IMPORT_STOP_WAITING_COPY,
} from './importFlow';
import './importFlow.css';

// eslint-disable-next-line complexity, max-lines-per-function -- Preserve established boundary.
export default function ImportFlowDialog({
  state,
  onAcknowledge,
  onConfirmSafety,
  onNewVersionChange,
  onResolveConflict,
  onRetry,
  onReopenConflict,
  onCancel,
}) {
  if (state?.phase === IMPORT_PHASE.CONFIRMING) {
    return (
      <Modal
        title={IMPORT_SAFETY_COPY.title}
        description={`文件：${state.file?.name || ''}`}
        onClose={onCancel}
        actions={
          <>
            <button className="btn" type="button" onClick={onCancel}>
              取消
            </button>
            <button
              className="btn btn-p"
              type="button"
              disabled={!state.acknowledged}
              onClick={onConfirmSafety}
            >
              确认并解析
            </button>
          </>
        }
      >
        <div className="kb-import-dialog">
          <p>{IMPORT_SAFETY_COPY.body}</p>
          {state.error && (
            <div className="kb-import-error" role="alert">
              {state.error}
            </div>
          )}
          <label className="kb-import-ack">
            <input
              type="checkbox"
              checked={state.acknowledged}
              onChange={(event) => onAcknowledge(event.target.checked)}
            />
            <span>{IMPORT_SAFETY_COPY.acknowledgement}</span>
          </label>
        </div>
      </Modal>
    );
  }

  if (state?.phase === IMPORT_PHASE.UPLOADING) {
    return (
      <Modal
        title="正在导入知识包"
        description={`文件：${state.file?.name || ''}`}
        onClose={onCancel}
        actions={
          <button className="btn btn-danger" type="button" onClick={onCancel}>
            {IMPORT_STOP_WAITING_COPY.action}
          </button>
        }
      >
        <div className="kb-import-dialog">
          <div className="kb-import-progress" role="status" aria-live="polite">
            <span className="spin" aria-hidden="true" />
            <div>
              <strong>正在上传并解析</strong>
              <p>{IMPORT_STOP_WAITING_COPY.body}</p>
            </div>
          </div>
        </div>
      </Modal>
    );
  }

  if (state?.phase === IMPORT_PHASE.CONFLICT) {
    const conflict = state.conflict || {};
    const matching =
      conflict.overwriteAction === IMPORT_ACTION.OVERWRITE_MATCHING;
    const overwriteLabel = matching ?
      `覆盖匹配版本${conflict.version ? ` ${conflict.version}` : ''}` :
      `覆盖激活版本${conflict.activeVersion ? ` ${conflict.activeVersion}` : ''}`;
    return (
      <Modal
        title="知识包版本冲突"
        description={
          conflict.message ||
          `知识包「${conflict.ontologyName || conflict.ontologyId}」已存在。`
        }
        onClose={onCancel}
        actions={
          <button className="btn" type="button" onClick={onCancel}>
            取消导入
          </button>
        }
      >
        <div className="kb-import-dialog">
          <div className="kb-import-choice">
            <div>
              <strong>{overwriteLabel}</strong>
              <span>
                {matching ?
                  '替换上传包声明的同版本内容，完成后该版本立即成为激活版本。' :
                  '上传包未声明版本，将替换当前激活版本内容并保持其激活状态。'}
              </span>
            </div>
            <button
              className="btn btn-danger"
              type="button"
              onClick={() => onResolveConflict(conflict.overwriteAction)}
            >
              {overwriteLabel}
            </button>
          </div>
          <div className="kb-import-choice">
            <div>
              <strong>创建新版本</strong>
              <span>
                保留现有版本，把上传内容写入新版本；完成后新版本立即成为激活版本。
              </span>
            </div>
            <label>
              <span className="sr-only">新增版本号</span>
              <input
                value={state.newVersion}
                onChange={(event) => onNewVersionChange(event.target.value)}
                placeholder="例如 1.0.1"
              />
            </label>
            <button
              className="btn btn-p"
              type="button"
              onClick={() => onResolveConflict(IMPORT_ACTION.CREATE_VERSION)}
            >
              创建版本
            </button>
          </div>
          {state.error && (
            <div className="kb-import-error" role="alert">
              {state.error}
            </div>
          )}
          {conflict.versions?.length > 0 && (
            <div className="kb-import-versions">
              现有版本：{conflict.versions.join('、')}
            </div>
          )}
        </div>
      </Modal>
    );
  }

  if (state?.phase === IMPORT_PHASE.ERROR) {
    const nameConflict = state.errorCode === 'ontology_name_conflict';
    return (
      <Modal
        title={nameConflict ? '知识包名称已存在' : '知识包导入失败'}
        description={state.error}
        onClose={onCancel}
        actions={
          <>
            <button className="btn" type="button" onClick={onCancel}>
              取消
            </button>
            {state.conflict && (
              <button className="btn" type="button" onClick={onReopenConflict}>
                返回冲突选项
              </button>
            )}
            {state.request && (
              <button className="btn btn-p" type="button" onClick={onRetry}>
                重试
              </button>
            )}
          </>
        }
      >
        <div className="kb-import-dialog">
          <div className="kb-import-error" role="alert">
            {nameConflict && '请修改知识包 manifest.yaml 中的 name 字段，重新打包后再导入。'}
            {!nameConflict && state.request &&
              '已保留文件与导入决策，可直接重试或重新选择处理方式。'}
            {!nameConflict && !state.request &&
              '请关闭提示后重新选择符合要求的 ZIP 文件。'}
          </div>
        </div>
      </Modal>
    );
  }

  return null;
}
