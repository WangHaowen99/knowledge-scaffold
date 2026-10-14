import { PlusOutlined } from '@ant-design/icons';
import { Alert, Button, Modal, Space, Table, Tag, Typography, message } from 'antd';
import { useEffect, useState } from 'react';
import { api } from '../lib/api.js';
import { removeEntry, rowActions, upsertEntry } from '../lib/contextPresets.js';
import ContextPresetForm from './ContextPresetForm.jsx';

const fmt = (n) => n.toLocaleString();
const fmtTime = (seconds) => new Date(seconds * 1000).toLocaleString();
const EMPTY = <Typography.Text type="secondary">—</Typography.Text>;

// 自定义条目与当前内置值不同时，小字标出内置值，升级后便于核对是否仍需覆盖
function TokenCell({ value, builtin }) {
  return (
    <>
      {fmt(value)}
      {builtin !== undefined && builtin !== value && (
        <div style={{ fontSize: 12 }}>
          <Typography.Text type="secondary">内置 {fmt(builtin)}</Typography.Text>
        </div>
      )}
    </>
  );
}

const ACTION_LABELS = { override: '覆盖', edit: '编辑', delete: '删除', restore: '恢复内置' };

const BASE_COLUMNS = [
  {
    title: '模型',
    dataIndex: 'id',
    render: (id, row) => (
      <>
        <Typography.Text code>{id}</Typography.Text>
        {row.origin === 'custom' && (
          <Tag color="blue" style={{ marginInlineStart: 4 }}>
            自定义
          </Tag>
        )}
        {row.aliases.length > 0 && (
          <div style={{ fontSize: 12 }}>
            <Typography.Text type="secondary">别名：{row.aliases.join('、')}</Typography.Text>
          </div>
        )}
      </>
    ),
  },
  { title: '厂商', dataIndex: 'vendor', width: 90, render: (vendor) => vendor || EMPTY },
  {
    title: '上下文窗口',
    dataIndex: 'max_context_tokens',
    width: 120,
    render: (value, row) => <TokenCell value={value} builtin={row.builtin_values?.max_context_tokens} />,
  },
  {
    title: '最大输出',
    dataIndex: 'max_output_tokens',
    width: 100,
    render: (value, row) => <TokenCell value={value} builtin={row.builtin_values?.max_output_tokens} />,
  },
  {
    title: '说明与来源',
    dataIndex: 'sources',
    render: (sources, row) =>
      row.note || sources.length > 0 ? (
        <div style={{ fontSize: 12 }}>
          {row.note && <div>{row.note}</div>}
          {sources.map((url) => (
            <div key={url}>
              <Typography.Link href={url} target="_blank" rel="noreferrer">
                {url.replace(/^https?:\/\//, '')}
              </Typography.Link>
            </div>
          ))}
        </div>
      ) : (
        EMPTY
      ),
  },
];

const fileName = (path) => path.split('/').pop();

const tokensText = (preset) =>
  `上下文窗口 ${fmt(preset.max_context_tokens)}、最大输出 ${fmt(preset.max_output_tokens)}`;

// 删除或恢复内置的确认文案
function removeConfirm(action, row) {
  const unaffected = '已保存的上下文参数不受影响。';
  if (action === 'restore') {
    return {
      title: `${row.id} 恢复为内置值？`,
      content: `从自定义表中删除这一条，推荐值恢复为内置的${tokensText(row.builtin_values || row)}。${unaffected}`,
      okText: '恢复内置',
    };
  }
  return {
    title: `删除 ${row.id}？`,
    content: `删除后配置页按模型名查表不再命中该模型。${unaffected}`,
    okText: '删除',
  };
}

// 停用确认：说清哪些推荐会消失、文件去哪了、已保存的配置不受影响
function DisableConfirmContent({ table }) {
  const { custom, presets, builtin_count: builtinCount } = table;
  const drifted = presets.filter((preset) => preset.builtin_values).length;
  const added = presets.length - builtinCount;
  const details = [
    drifted > 0 && `${drifted} 条改了内置值`,
    added > 0 && `${added} 条是内置表没有的模型`,
  ].filter(Boolean);
  return (
    <>
      <p>
        {custom.loaded_at
          ? `自定义表中的 ${custom.count} 条将不再推荐${details.length ? `（其中 ${details.join('，')}）` : ''}，`
          : '自定义表有误，当前未生效；停用后'}
        推荐表恢复为内置的 {builtinCount} 条。
      </p>
      <p>
        文件不会删除，改名为同目录下的 {fileName(custom.path)}.bak-日期-时间；把它改回 {fileName(custom.path)}{' '}
        即可重新启用，无需重启。
      </p>
      <p>已保存的模型配置与上下文参数不受影响：按自定义值保存过的上下文参数仍然生效，需要时请在配置页修改。</p>
    </>
  );
}

// 自定义表（数据目录下，页面或手改都即时生效）：有误时整份不生效，沿用上一次合法的结果，页面也不能编辑
function CustomTableStatus({ custom, presets, onDisable }) {
  if (!custom) return null;
  const disableButton = custom.exists ? (
    <Button size="small" danger onClick={onDisable}>
      停用自定义表
    </Button>
  ) : undefined;
  if (custom.error) {
    const fallback = custom.loaded_at
      ? `当前沿用 ${fmtTime(custom.loaded_at)} 加载的自定义表`
      : '当前仅使用内置表';
    return (
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 12 }}
        message={`自定义表有误，未生效：${custom.error}`}
        description={`${custom.path}；${fallback}。改正或停用后才能在页面编辑，改正后重新打开本窗口即可看到结果。`}
        action={disableButton}
      />
    );
  }
  if (custom.loaded_at) {
    const verified = custom.verified_at ? `，核对日期 ${custom.verified_at}` : '';
    const drifted = presets.filter((preset) => preset.builtin_values).length;
    return (
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message={`已加载自定义表 ${custom.path}（${custom.count} 条${verified}，${fmtTime(custom.loaded_at)} 加载），同 id 的条目以自定义表为准`}
        description={
          drifted > 0
            ? `${drifted} 条自定义值与当前内置值不同（见表中「内置」小字），升级后请核对是否仍需覆盖。`
            : undefined
        }
        action={disableButton}
      />
    );
  }
  return (
    <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
      {custom.backup_path && (
        <>
          当前使用内置推荐表。最近一次停用的自定义表备份在 {custom.backup_path}，改回{' '}
          {fileName(custom.path)} 即可重新启用，无需重启。
          <br />
        </>
      )}
      可在表中覆盖内置条目或新增模型，保存后写入 {custom.path}，即时生效，无需重启；运维也可参考{' '}
      {custom.example_path} 直接编辑该文件。
    </Typography.Paragraph>
  );
}

// 模型上下文推荐表：配置页按模型名命中时自动填入窗口与最大输出。
// 页面编辑提交整份自定义条目并带上读取时的版本号，文件已被改过时服务端拒绝（409），重新加载后再操作。
// 改过推荐表（含停用）后调用 onChanged，配置页据此重新查表
export default function ContextPresetsModal({ open, onClose, onChanged }) {
  const [table, setTable] = useState(null);
  const [loading, setLoading] = useState(false);
  const [modal, modalContext] = Modal.useModal();
  const [editing, setEditing] = useState(null);
  const [formOpen, setFormOpen] = useState(false);
  // 每次打开表单换一个 key 重新挂载，用全新的表单实例：沿用旧实例时，上次打开的值
  // （如覆盖过的内置条目）会盖过本次的初始值，新增时出现上一条的信息
  const [formKey, setFormKey] = useState(0);
  // 自定义表有误时不能编辑（须先改正或停用）；加载中也不能，避免带着旧版本号提交
  const editable = Boolean(table && !table.custom.error && !loading);

  // 每次打开都重新请求，自定义表改过后不用刷新页面
  useEffect(() => {
    if (!open) return undefined;
    let active = true;
    setLoading(true);
    api
      .getModelContextPresets()
      .then((data) => {
        if (active) setTable(data);
      })
      .catch((error) => {
        if (!active) return;
        message.error(error.message);
        onClose();
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [open, onClose]);

  const reload = () => {
    setLoading(true);
    api
      .getModelContextPresets()
      .then(setTable)
      .catch((error) => message.error(error.message))
      .finally(() => setLoading(false));
  };

  // 返回 true 表示表单可以关闭：已保存，或文件已被改过、需按最新内容重新操作
  const saveEntries = async (entries, verifiedAt = null) => {
    try {
      const data = await api.saveCustomContextPresets({
        version: table.custom.version,
        presets: entries,
        verified_at: verifiedAt,
      });
      setTable(data);
      onChanged?.();
      message.success(data.previous_path ? `已保存，上一版备份为 ${data.previous_path}` : '已保存');
      return true;
    } catch (error) {
      if (error.status !== 409) {
        message.error(error.message);
        return false;
      }
      message.warning(error.message);
      reload();
      return true;
    }
  };

  const close = () => {
    setFormOpen(false);
    onClose();
  };

  const openForm = (mode, entry = null) => {
    setEditing({ mode, entry });
    setFormKey((key) => key + 1);
    setFormOpen(true);
  };

  const saveForm = async (entry, verifiedAt) => {
    if (await saveEntries(upsertEntry(table.custom.entries, entry), verifiedAt)) setFormOpen(false);
  };

  const confirmRemove = (action, row) =>
    modal.confirm({
      ...removeConfirm(action, row),
      okButtonProps: { danger: action === 'delete' },
      cancelText: '取消',
      onOk: () => saveEntries(removeEntry(table.custom.entries, row.id)),
    });

  const runAction = (action, row) => {
    if (action === 'delete' || action === 'restore') confirmRemove(action, row);
    else openForm(action, row);
  };

  const columns = [
    ...BASE_COLUMNS,
    {
      title: '操作',
      key: 'actions',
      width: 110,
      render: (_, row) => (
        <Space size={8} wrap>
          {rowActions(row).map((action) => (
            <Typography.Link key={action} disabled={!editable} onClick={() => runAction(action, row)}>
              {ACTION_LABELS[action]}
            </Typography.Link>
          ))}
        </Space>
      ),
    },
  ];

  const confirmDisable = () =>
    modal.confirm({
      title: '停用自定义推荐表？',
      width: 520,
      content: <DisableConfirmContent table={table} />,
      okText: '停用',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          const data = await api.disableCustomContextPresets();
          setTable(data);
          onChanged?.();
          message.success(
            data.disabled_backup ? `已停用，备份为 ${data.disabled_backup}` : '自定义表已不存在，推荐表为内置表',
          );
        } catch (error) {
          message.error(error.message);
        }
      },
    });

  return (
    <Modal title="模型上下文推荐表" open={open} onCancel={close} footer={null} width={980}>
      {modalContext}
      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        内置条目取自各厂商官方文档（核对日期 {table?.verified_at || '—'}），最大输出取官方默认值，思考与非思考默认值不同时取思考模式的（稳定引擎默认开启思考）；窗口为输入 + 输出总量。
        模型名匹配忽略大小写、provider/ 前缀、「.」「_」写法和 6 或 8 位日期后缀。网关限额可能与官方不同，以实际报错为准。
      </Typography.Paragraph>
      <CustomTableStatus custom={table?.custom} presets={table?.presets || []} onDisable={confirmDisable} />
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
        <Typography.Text type="secondary" style={{ flex: 1, fontSize: 12 }}>
          改表只影响推荐与预填，已保存的上下文参数不会跟着变。
        </Typography.Text>
        <Button size="small" icon={<PlusOutlined />} disabled={!editable} onClick={() => openForm('add')}>
          新增模型
        </Button>
      </div>
      <Table
        size="small"
        rowKey="id"
        pagination={false}
        loading={loading}
        dataSource={table?.presets || []}
        columns={columns}
        scroll={{ y: 440 }}
      />
      <ContextPresetForm
        key={formKey}
        open={open && formOpen}
        editing={editing}
        presets={table?.presets || []}
        verifiedAt={table?.custom.verified_at}
        onSave={saveForm}
        onCancel={() => setFormOpen(false)}
      />
    </Modal>
  );
}
