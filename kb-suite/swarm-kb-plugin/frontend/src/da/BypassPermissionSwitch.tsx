import { useState } from 'react';
import { Button, Modal, Switch } from '../ui';

export function BypassPermissionSwitch({
  enabled, pending, onChange,
}: {
  enabled: boolean;
  pending: boolean;
  onChange: (enabled: boolean) => void;
}) {
  const [confirm, setConfirm] = useState(false);
  const close = () => setConfirm(false);

  return (
    <div className="da-bypass-switch">
      <Switch
        label="Bypass permission"
        checked={enabled}
        disabled={pending}
        onChange={(next) => {
          if (next) setConfirm(true);
          else onChange(false);
        }}
      />
      {confirm ? (
        <Modal
          title="开启 Bypass permission？"
          onClose={close}
          footer={
            <>
              <Button autoFocus onClick={close}>取消</Button>
              <Button variant="primary" onClick={() => { close(); onChange(true); }}>确认开启</Button>
            </>
          }
        >
          <p>开启后，当前对话后续的工具审批将自动允许，可能涉及执行命令、写文件或推送产物。切到其他页面后，正在运行的分析也会按此设置继续。</p>
        </Modal>
      ) : null}
    </div>
  );
}
