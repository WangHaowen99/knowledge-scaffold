// 模型单价（SY-18）：每个引擎、模型的输入 / 输出 / 缓存读取单价（每百万 tokens）与币种，用于计算费用。
// 设计稿没画入口，暂不挂载（见 gaps.md F4）。
// 挂载方法：在 ModelSection.jsx 末尾「默认引擎 / 个人 API Key」那个 grid2 之后加一行 <PricingCard className="mt16" />（契约 8.2「sys 页模型接入卡里加单价」）。
// 后端还没有这组接口时（404）只给说明：单价表落地前，费用是引擎报告的美元数（contract-changes B2 第 1 条）。
import { useEffect, useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import {
  Button, Card, Inp, Note, Seg, Select, Table,
} from '../../ui/index.jsx';
import { FieldErr, LoadNote, useLoad } from './parts.jsx';
import {
  PRICE_FIELDS, PRICING_FALLBACK, emptyPriceRow, moneySymbol, pricingBody, pricingForm, pricingProblems,
} from './sysModel.js';

const ENGINE_OPTIONS = [{ value: 'claude', label: '稳定引擎' }, { value: 'jiuwen', label: '内测引擎' }];

export function PricingCard({ className }) {
  const { api } = useApp();
  const { data, error, setData, reload } = useLoad(() => api.pricing());
  const [f, setF] = useState(null);
  const [tried, setTried] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (data) setF(pricingForm(data)); }, [data]);

  const problems = tried && f ? pricingProblems(f) : {};
  const dirty = !!(data && f && JSON.stringify(pricingForm(data)) !== JSON.stringify(f));
  const setRow = (i, k) => (v) => setF((x) => ({ ...x, items: x.items.map((it, j) => (j === i ? { ...it, [k]: v && v.target ? v.target.value : v } : it)) }));
  const removeRow = (i) => setF((x) => ({ ...x, items: x.items.filter((_, j) => j !== i) }));

  const save = async () => {
    setTried(true);
    if (Object.keys(pricingProblems(f)).length) return;
    setBusy(true);
    try {
      setData(await api.savePricing(pricingBody(f)));
      setTried(false);
      toast('单价已保存，此后的费用按新单价计算', 'check');
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };

  const sym = moneySymbol(f?.currency);
  const head = { title: '模型单价', sub: '每百万 tokens，用于计算费用', icon: 'tag', className };
  if (error && !data) {
    return (
      <Card {...head}>
        {error.status === 404 ? <Note tone="gray" icon="info">{PRICING_FALLBACK}</Note> : <LoadNote error={error} what="模型单价" code="SY-18" onRetry={reload} />}
      </Card>
    );
  }
  if (!f) return <Card {...head}><div className="sm muted">正在读取…</div></Card>;
  return (
    <Card {...head} extra={<Seg items={[['CNY', '人民币'], ['USD', '美元']]} value={f.currency} onChange={(v) => setF((x) => ({ ...x, currency: v }))} />}>
      {f.items.length ? (
        <Table
          compact
          wrapClassName="sy-price"
          cols={[{ t: '引擎', w: 140 }, '模型', ...PRICE_FIELDS.map(([, t]) => ({ t: `${t}（${sym}）`, w: 120 })), { t: '', w: 64 }]}
          rows={f.items.map((it, i) => {
            const p = problems.rows?.[i] || {};
            return [
              <Select value={it.engine} options={ENGINE_OPTIONS} onChange={setRow(i, 'engine')} w="120px" />,
              <div><Inp mono value={it.model} error={!!p.model} placeholder="model-…" onChange={setRow(i, 'model')} /><FieldErr>{p.model}</FieldErr></div>,
              ...PRICE_FIELDS.map(([k]) => (
                <div key={k}>
                  <Inp mono value={it[k]} error={!!p[k]} placeholder={k === 'cache_read_per_mtok' ? '可留空' : '0'} onChange={setRow(i, k)} />
                  <FieldErr>{p[k]}</FieldErr>
                </div>
              )),
              <Button size="xs" kind="ghost" onClick={() => removeRow(i)}>删除</Button>,
            ];
          })}
        />
      ) : <div className="sm muted">还没有单价：费用按引擎报告的金额显示。</div>}
      <div className="row mt12">
        <Button size="xs" icon="plus" onClick={() => setF((x) => ({ ...x, items: [...x.items, emptyPriceRow()] }))}>新增一行</Button>
        <span className="xs muted">没配单价的模型按引擎报告的费用计算；改币种不换算已有数字。</span>
        <span className="sp" />
        <Button size="sm" kind="primary" disabled={busy || !dirty} onClick={save}>保存</Button>
      </div>
    </Card>
  );
}
