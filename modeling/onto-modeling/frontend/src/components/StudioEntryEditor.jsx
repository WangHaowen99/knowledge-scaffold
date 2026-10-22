import { DeleteOutlined, PlusOutlined } from '@ant-design/icons';
import { Alert, Button, Input, Modal, Space, Spin, Tag, Typography, message } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../lib/api.js';

// ---- markdown 表格 ↔ 结构化行 双向转换 ----

export function parseTable(text) {
  const lines = text.split('\n').filter((l) => l.includes('|'));
  if (lines.length < 2) return null;
  const cells = (line) => line.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map((c) => c.trim());
  const header = cells(lines[0]);
  if (!/^[:\-\s|]+$/.test(lines[1].replace(/\|/g, ''))) return null;
  const rows = lines.slice(2).map(cells).map((row) => {
    const record = {};
    header.forEach((col, i) => {
      record[col] = row[i] ?? '';
    });
    return record;
  });
  return { header, rows };
}

export function serializeTable(header, rows) {
  const line = (cells) => `| ${cells.join(' | ')} |`;
  return [
    line(header),
    line(header.map(() => '---')),
    ...rows.map((row) => line(header.map((col) => (row[col] ?? '').replace(/\n/g, ' ')))),
  ].join('\n');
}

function TableEditor({ columns, value, onChange }) {
  const parsed = useMemo(() => parseTable(value), [value]);
  // 内容不是表格（如"无"）时退回文本编辑
  if (!parsed) {
    return (
      <Space direction="vertical" style={{ width: '100%' }} size={4}>
        <Input.TextArea autoSize={{ minRows: 1 }} value={value} onChange={(e) => onChange(e.target.value)} />
        <Button
          size="small"
          type="dashed"
          icon={<PlusOutlined />}
          onClick={() => onChange(serializeTable(columns, [Object.fromEntries(columns.map((c) => [c, '']))]))}
        >
          转为表格
        </Button>
      </Space>
    );
  }
  const header = parsed.header.length >= columns.length ? parsed.header : columns;
  const update = (rows) => onChange(serializeTable(header, rows));
  return (
    <div className="studio-table-editor">
      <table>
        <thead>
          <tr>
            {header.map((col) => (
              <th key={col}>{col}</th>
            ))}
            <th />
          </tr>
        </thead>
        <tbody>
          {parsed.rows.map((row, rowIndex) => (
            // eslint-disable-next-line react/no-array-index-key
            <tr key={rowIndex}>
              {header.map((col) => (
                <td key={col}>
                  <Input.TextArea
                    autoSize
                    variant="borderless"
                    value={row[col]}
                    onChange={(e) => {
                      const rows = parsed.rows.map((r, i) =>
                        i === rowIndex ? { ...r, [col]: e.target.value } : r,
                      );
                      update(rows);
                    }}
                  />
                </td>
              ))}
              <td className="studio-row-del">
                <Button
                  size="small"
                  type="text"
                  icon={<DeleteOutlined />}
                  onClick={() => update(parsed.rows.filter((_, i) => i !== rowIndex))}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <Button
        size="small"
        type="dashed"
        icon={<PlusOutlined />}
        onClick={() => update([...parsed.rows, Object.fromEntries(header.map((c) => [c, '']))])}
      >
        添加一行
      </Button>
    </div>
  );
}

/** Studio 条目结构化编辑：schema 表单 + 草稿区 + 租约 + 提交分流（方案 7.2/7.3）。 */
export default function StudioEntryEditor({ project, entry, onExit, onCommitted, onCommand }) {
  const schema = entry.schema || [];
  const [lease, setLease] = useState(null);
  const [readOnly, setReadOnly] = useState(false);
  const [sections, setSections] = useState([]);
  const [draftState, setDraftState] = useState('clean'); // clean | dirty | saving
  const [submitting, setSubmitting] = useState(false);
  const [rejection, setRejection] = useState(null);
  const draftTimer = useRef(0);
  const baseRevisionRef = useRef(entry.revision);

  // 初始化：schema 章节顺序为骨架，套上文档现有内容；文档多出的章节保留在末尾
  useEffect(() => {
    const byTitle = new Map(entry.sections.map((s) => [s.title, s]));
    const level = entry.sections[0]?.level || 1;
    const merged = schema.map((spec) => ({
      ...spec,
      level: byTitle.get(spec.title)?.level || level,
      content: byTitle.get(spec.title)?.content ?? '',
    }));
    for (const section of entry.sections) {
      if (!schema.some((spec) => spec.title === section.title)) {
        merged.push({ title: section.title, kind: 'text', level: section.level, content: section.content });
      }
    }
    // 有历史草稿时优先载入草稿内容
    api.studioGetDraft(project.id, entry.path).then(({ draft }) => {
      if (draft?.content) {
        const draftSections = new Map(
          (draft.content.match(/^#{1,2}\s+.+$/gm) || []).length
            ? parseDraftSections(draft.content)
            : [],
        );
        if (draftSections.size) {
          setSections(
            merged.map((s) => ({ ...s, content: draftSections.get(s.title) ?? s.content })),
          );
          baseRevisionRef.current = draft.base_revision || entry.revision;
          setDraftState('dirty');
          message.info('已载入上次未提交的草稿');
          return;
        }
      }
      setSections(merged);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entry.path]);

  // 租约：进入编辑即申请；他人持有 → 只读；退出释放
  useEffect(() => {
    let mounted = true;
    api
      .studioLease(project.id, entry.path)
      .then((data) => mounted && setLease(data))
      .catch((error) => {
        if (mounted) {
          setReadOnly(true);
          message.warning(error.message);
        }
      });
    return () => {
      mounted = false;
      api.studioReleaseLease(project.id, entry.path).catch(() => {});
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project.id, entry.path]);

  const serialize = (list) =>
    (entry.name_frontmatter || '') + list
      .map((s) => `${'#'.repeat(s.level || 1)} ${s.title}\n\n${(s.content || '').trim()}\n`)
      .join('\n');

  const scheduleDraftSave = (next) => {
    setSections(next);
    setDraftState('dirty');
    clearTimeout(draftTimer.current);
    draftTimer.current = setTimeout(async () => {
      setDraftState('saving');
      try {
        await api.studioSaveDraft(project.id, entry.path, serialize(next), baseRevisionRef.current);
        setDraftState('dirty'); // 草稿已存但尚未提交为版本
      } catch (error) {
        message.error(error.message);
      }
    }, 800);
  };

  const submit = async () => {
    setSubmitting(true);
    setRejection(null);
    try {
      clearTimeout(draftTimer.current);
      await api.studioSaveDraft(project.id, entry.path, serialize(sections), baseRevisionRef.current);
      const result = await api.studioSubmit(project.id, entry.path);
      if (result.result === 'committed') {
        message.success('检查通过，已直接保存为新版本');
        onCommitted?.();
        onExit?.();
      } else if (result.result === 'needs_review') {
        Modal.info({
          title: '已转交助手复核',
          content: (
            <div>
              <p>改动超出确定性检查范围，已连同疑点发给助手复核，结果会出现在右侧对话里：</p>
              <ul>
                {result.review.map((r) => (
                  <li key={r}>{r}</li>
                ))}
              </ul>
            </div>
          ),
        });
        onExit?.();
      } else {
        setRejection(result.errors);
      }
    } catch (error) {
      message.error(error.message);
    } finally {
      setSubmitting(false);
    }
  };

  const discard = () => {
    Modal.confirm({
      title: '放弃草稿？',
      content: '未提交的修改将被丢弃，文档保持当前版本。',
      okText: '放弃',
      okButtonProps: { danger: true },
      onOk: async () => {
        await api.studioDiscardDraft(project.id, entry.path).catch(() => {});
        onExit?.();
      },
    });
  };

  if (!sections.length) return <Spin style={{ marginTop: 32 }} />;

  return (
    <div className="studio-editor">
      <Space style={{ marginBottom: 10 }} align="center" wrap>
        <Typography.Title level={5} style={{ margin: 0 }}>
          编辑：{entry.label || entry.name}
        </Typography.Title>
        {readOnly ? (
          <Tag color="warning">他人编辑中 · 只读</Tag>
        ) : (
          lease && <Tag color="success">已锁定编辑权（30 分钟）</Tag>
        )}
        <Tag>
          {draftState === 'saving' ? '草稿保存中…' : draftState === 'dirty' ? '草稿已暂存' : '未修改'}
        </Tag>
        <Button type="primary" size="small" loading={submitting} disabled={readOnly} onClick={submit}>
          提交（检查并保存版本）
        </Button>
        <Button size="small" onClick={discard} disabled={readOnly}>
          放弃草稿
        </Button>
        <Button size="small" onClick={onExit}>
          退出
        </Button>
      </Space>
      {rejection && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 10 }}
          message="未通过确定性检查，请修正后再提交"
          description={
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {rejection.map((e) => (
                <li key={e}>{e}</li>
              ))}
            </ul>
          }
        />
      )}
      {sections.map((section, index) => (
        <div key={section.title} className="studio-edit-section">
          <div className="studio-section-title">
            {section.title}
            {section.required && <span className="studio-required">必填</span>}
          </div>
          {section.kind === 'table' ? (
            <TableEditor
              columns={section.columns || []}
              value={section.content}
              onChange={(content) =>
                scheduleDraftSave(sections.map((s, i) => (i === index ? { ...s, content } : s)))
              }
            />
          ) : (
            <Input.TextArea
              autoSize={{ minRows: 2 }}
              value={section.content}
              readOnly={readOnly}
              onChange={(e) =>
                scheduleDraftSave(
                  sections.map((s, i) => (i === index ? { ...s, content: e.target.value } : s)),
                )
              }
            />
          )}
        </div>
      ))}
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8 }}>
        提交时先跑结构检查：通过直接落版本；涉及新链接/新章节等超范围改动会转助手复核。
        {onCommand && (
          <>
            {' '}
            也可以
            <Typography.Link onClick={() => onCommand(`请检查我在 Studio 对 ${entry.path} 的草稿修改`)}>
              直接请助手参与
            </Typography.Link>
            。
          </>
        )}
      </Typography.Paragraph>
    </div>
  );
}

function parseDraftSections(text) {
  const out = [];
  const regex = /^(#{1,2})\s+(.+)$/gm;
  const matches = [...text.matchAll(regex)];
  matches.forEach((match, i) => {
    const start = match.index + match[0].length;
    const end = i + 1 < matches.length ? matches[i + 1].index : text.length;
    out.push([match[2].trim(), text.slice(start, end).trim()]);
  });
  return out;
}
