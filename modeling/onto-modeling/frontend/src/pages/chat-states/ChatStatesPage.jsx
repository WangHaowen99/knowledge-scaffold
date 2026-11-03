// 对话卡片与状态（设计稿 chat-states）：工作台对话里每种卡片和状态的说明页。
// 每个小样都用工作台同一套组件渲染：示例事件先过对话状态机，再交给时间线组件，按钮只做演示提示。
import { useMemo } from 'react';
import { useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { applyEvents, initialConversation } from '../../state/conversation.js';
import { PageHead, ShellP } from '../../shell/Shell.jsx';
import { Button, Chip, Field, Icon, Inp, Kbd, Ring, Seg, cx } from '../../ui/index.jsx';
import {
  ActionsCtx, ConfigPendingItem, ContextPanel, ErrorItem, PackRecommendationCard, ReconnectedItem, ResumeCard, RoundChangesDetail,
} from '../workbench/cards.jsx';
import { Tli, TimelineGroup, WbCtx } from '../workbench/parts.jsx';
import { BranchCard, ExpertCard, PermissionCard, WalkthroughCard } from '../workbench/questions.jsx';
import { AssistantItem } from '../workbench/Timeline.jsx';
import { ConversationMenu } from '../workbench/WorkbenchPage.jsx';
import * as S from './samples.js';

const demo = (t = '这里是说明页，操作只做演示') => () => toast(t, 'info');
const DEMO_ACTIONS = {
  answer: async () => demo('回答会发到对话，助手随即继续')(), merge: async () => demo()(), discard: async () => demo()(),
  permission: async () => demo()(), resume: async () => demo()(), abandon: async () => demo()(), retry: async () => demo()(),
  clearInput: demo(), compare: demo('在卡内展开这一条的对比'), fail: () => {}, registerRound: () => {},
};

function Head({ conv = '超量与审批提示', convOn, state, ctx = 42 }) {
  return (
    <div className="g2-rh">
      <div className={cx('wx-sess', convOn && 'g2-on')} style={{ marginLeft: -4, height: 30 }}>
        <Icon name="msg" size={14} /><span className="nm ell" style={{ fontSize: 13 }}>{conv}</span><Icon name={convOn ? 'chevu' : 'chevd'} size={12} />
      </div>
      <span className="sp" />
      {state}
      <span className="row g4"><Ring p={ctx} color={ctx >= 75 ? '#F59E0B' : '#2563EB'} /><span className="xs muted num">{ctx}%</span></span>
    </div>
  );
}

function Comp({ top, ph = '给助手补充说明，可拖入文件或粘贴截图…', val = '' }) {
  return (
    <div className="wb-comp" style={{ margin: '0 10px 10px' }}>
      {top}
      <textarea placeholder={ph} defaultValue={val} readOnly />
      <div className="row">
        <Button icon="clip" size="sm" kind="ghost" /><Button icon="cmd" size="sm" kind="ghost" /><Button icon="at" size="sm" kind="ghost" />
        <span className="sp" /><Button icon="send" size="sm" kind="primary" />
      </div>
    </div>
  );
}

function Item({ n, title, feat, children, head = true, headProps, comp, post }) {
  return (
    <div className="g2-item">
      <div className="g2-lab"><span className="g2-n">{n}</span><b>{title}</b><span className="faint xs" style={{ marginLeft: 'auto' }}>{feat}</span></div>
      <div className="g2-pane">
        {head ? <Head {...headProps} /> : null}
        <div className="g2-pb">{children}</div>
        {comp ? <Comp {...comp} /> : null}
        {post}
      </div>
    </div>
  );
}

function SugStrip({ items }) {
  return (
    <div className="wb-sug" style={{ padding: '6px 0 0' }}>
      {items.map(([t, i, p]) => <span key={t} className={cx('s', p && 'p')}><Icon name={i} size={12} />{t}</span>)}
      <span className="sp" />
      <span className="s" style={{ border: 0, padding: '0 4px' }}><Icon name="cmd" size={12} />全部命令<Icon name="chevd" size={12} /></span>
    </div>
  );
}

// 示例事件 → 状态机 → 第一条助手侧条目
function useItems(events) {
  return useMemo(() => {
    const s = applyEvents(initialConversation(), events);
    return s.order.map((k) => ({ key: k, ...s.items[k] }));
  }, [events]);
}

function Rendered({ events, running }) {
  const items = useItems(events);
  return items.map((it) => <AssistantItem key={it.key} it={it} ctx={{ running, onInterrupt: demo('打断会停在当前步骤，已保存的内容不会丢') }} />);
}

const PCS = [
  ['资料地图卡', 'map', 'blue', '双击打开资料地图弹窗：可以改资料用途、让助手补读漏掉的部分', '弹窗编辑', 'wb-materials'],
  ['建模蓝图卡', 'layers', 'blue', '双击打开蓝图弹窗：拖动排序、合并、拆分、改名分段，确认后开始建模', '弹窗编辑', 'wb-blueprint'],
  ['本步产出卡', 'cube', 'blue', '卡片只读；点条目名去 Studio 编辑，「查看改动」在卡内展开对比', '跳 Studio', 'wb-l2'],
  ['流程图卡', 'branch', 'indigo', '双击打开流程图编辑弹窗，和 Studio 里是同一份；在助手写入范围内时，这一轮结束前只读', '弹窗编辑', 'flowchart'],
  ['变更点清单卡', 'refresh', 'amber', '双击打开弹窗逐条改写、接受变更点，确认后才动手修改', '弹窗编辑', 'incremental'],
  ['运行报告卡', 'rocket', 'indigo', '自主构建结束时出现；只读汇总，按钮进入待处理逐条复核', '只读', 'l4-run'],
];
const TONE = { blue: { background: 'var(--b50)', color: 'var(--blue)' }, indigo: { background: 'var(--i50)', color: 'var(--indigo)' }, amber: { background: 'var(--a50)', color: 'var(--amber2)' } };

export default function ChatStatesPage() {
  const { pid } = useProject();
  const items = [
    <Item key={1} n={1} title="会话切换菜单" feat="新建 · 续聊 · 重命名 · 删除" headProps={{ convOn: true }}>
      <ConversationMenu items={S.CONVERSATIONS} cid="c1" running className="" initialRenaming="c3" demoTitle="资料分析答疑" initialDeleting={S.CONVERSATIONS[1]} showOpsFor="c2" ops={{ create: demo(), rename: demo(), remove: demo(), switch: demo() }} />
    </Item>,
    <Item key={2} n={2} title="全部命令" feat="按业务名分组" head={false} comp={{ val: '/', ph: '' }}>
      <div className="g2-cmd">
        {S.COMMANDS.map(([g, rows]) => (
          <div key={g}>
            <div className="gt">{g}</div>
            {rows.map(([c, d, on]) => <div key={c} className={cx('it', on && 'on')}><b className="ell">{c}</b><span>{d}</span></div>)}
          </div>
        ))}
        <div className="row xs muted" style={{ padding: '6px 6px 0', borderTop: '1px solid var(--line)', marginTop: 4 }}>共 22 条<span className="sp" /><Kbd>↑</Kbd><Kbd>↓</Kbd> 选择 <Kbd>↵</Kbd> 发起</div>
      </div>
    </Item>,
    <Item key={3} n={3} title="运行中：子代理进度与打断" feat="可随时打断" headProps={{ state: <span className="chip blue sm"><span className="spin" style={{ width: 9, height: 9 }} />2:14</span> }} comp={{ ph: '运行中也能补充说明，这一步结束后送达' }}>
      <Rendered events={S.RUNNING_EVENTS} running />
    </Item>,
    <Item key={4} n={4} title="打断之后" feat="继续 · 从打断处继续 · 换个方向">
      <div className="tl"><Tli k="ok" t="生成验收场景 2 / 4" m="门店更严、门店超量 · 已保存" /><Tli k="gray" t="你在生成第 3 个场景时打断了" m="14:06 · 已打断" /></div>
      <SugStrip items={[['继续刚才的任务', 'play', 1], ['从打断处继续', 'redo'], ['换个方向', 'refresh']]} />
      <div className="mt8" style={{ fontSize: 11.5, color: 'var(--ink3)', lineHeight: 1.7 }}>
        <b style={{ color: 'var(--ink2)' }}>继续刚才的任务</b>：重新发起「生成验收场景」<br /><b style={{ color: 'var(--ink2)' }}>从打断处继续</b>：从第 3 个场景接着做<br /><b style={{ color: 'var(--ink2)' }}>换个方向</b>：清空输入框，重新下达指令
      </div>
    </Item>,
    <Item key={5} n={5} title="中断续跑卡" feat="服务重启后也能接着做"><ResumeCard card={S.RESUME_CARD} /></Item>,
    <Item key={6} n={6} title="权限确认卡" feat="允许 / 拒绝 · 超时按拒绝">
      <PermissionCard item={S.permissionItem()} />
      <div className="xs faint mt8">只在系统设置为「逐操作确认」时出现</div>
    </Item>,
    <Item key={7} n={7} title="走查分支选择 · 请专家确认" head={false}>
      <BranchCard q={S.BRANCH_Q} initialPick="o1" />
      <ExpertCard q={S.EXPERT_Q} />
    </Item>,
    <Item key={8} n={8} title="本轮改了什么 · 展开明细" feat="只算助手这一轮" head={false}><RoundChangesDetail card={S.ROUND_DETAIL} onCollapse={demo()} /></Item>,
    <Item key={9} n={9} title="排队中" feat="第 2 位" headProps={{ state: <Chip tone="amber" sm dot>排队中</Chip> }}>
      <div className="g2-msg g2-me">把临期与损耗检查这一段做完</div>
      <div className="g2-q mt12">
        <b>2</b>
        <div className="grow">
          <div className="b sm">正在排队，前面还有 1 个</div>
          <div className="xs muted mt4">服务器同时最多运行 12 个助手。轮到你时自动开始，不用守着页面。</div>
          <div className="prog amber thin"><i style={{ width: '55%' }} /></div>
        </div>
      </div>
      <div className="row mt8"><span className="sp" /><Button size="sm" kind="ghost" onClick={demo()}>取消排队</Button></div>
    </Item>,
    <Item key={10} n={10} title="上下文占用与自动压缩" feat="含项目累计用量" headProps={{ ctx: 78 }}>
      <ContextPanel context={S.CONTEXT} usage={S.USAGE} />
      <Rendered events={S.COMPACT_EVENTS} />
    </Item>,
    <Item key={11} n={11} title="重启会话" feat="按最新配置重连">
      <div className="g2-dim">
        <div className="g2-mm">
          <h5><Icon name="refresh" size={14} />重启会话？</h5>
          <div className="sm muted mt8">对话记录保留，按最新配置重连模型；稳定引擎会清空模型上下文。要从空白上下文开始，请新建会话。</div>
          <div className="row mt12"><span className="sp" /><Button size="sm" onClick={demo()}>取消</Button><Button size="sm" kind="primary" onClick={demo()}>重启会话</Button></div>
        </div>
      </div>
      <div className="row mt8"><Chip tone="blue" icon="check">会话已重置，下次发消息生效</Chip></div>
    </Item>,
    <Item key={12} n={12} title="沉淀为业务知识" feat="消息旁的按钮" head={false}>
      <div className="g2-msg">明白了：门店申请严于标准时按门店执行；超出标准时必须走区域审批，审批通过前不能下单。</div>
      <div className="g2-acts"><span><Icon name="copy" size={12} />复制</span><span className="hi"><Icon name="book" size={12} />沉淀为业务知识</span></div>
      <div className="g2-mm mt8">
        <h5><Icon name="book" size={14} />沉淀为业务知识</h5>
        <div className="col mt12" style={{ gap: 10 }}>
          <Field label="条目名称"><Inp defaultValue="门店申请严于标准时按门店执行" /></Field>
          <Field label="类型"><Seg items={[['fix', '纠错'], ['conv', '惯例'], ['term', '术语'], ['rule', '领域规则']]} value="rule" /></Field>
          <Field label="作用范围"><div className="inp sel"><span className="grow ell">对象：超量清单、审批提示</span></div></Field>
          <Field label="内容"><div className="inp area" style={{ minHeight: 64 }}><span className="grow" style={{ whiteSpace: 'pre-wrap' }}>{'## 背景\n补货规则 §9.2、§11.4\n## 规则\n严于标准按门店执行；超出标准须区域审批'}</span></div></Field>
        </div>
        <div className="xs faint mt8">入库前先和已有的 12 条知识查重、查冲突</div>
        <div className="row mt8"><span className="sp" /><Button size="sm" onClick={demo()}>取消</Button><Button size="sm" kind="primary" onClick={demo()}>确认入库</Button></div>
      </div>
      <div className="row mt8"><Chip tone="teal" icon="check">已入库，后续建模会自动应用</Chip></div>
    </Item>,
    <Item key={13} n={13} title="复制" head={false}>
      <div className="g2-msg">已按方向 A 修改「超量与审批提示」，新增规则引用 R-09，补了 2 个验收场景，推演全部通过。</div>
      <div className="g2-acts"><span className="on"><Icon name="check" size={12} />已复制</span><span><Icon name="book" size={12} />沉淀为业务知识</span></div>
    </Item>,
    <Item key={14} n={14} title="能力包升级提示" feat="下一条消息生效">
      <ConfigPendingItem item={S.CONFIG_PENDING} />
      <ReconnectedItem item={S.RECONNECTED} />
    </Item>,
    <Item key={15} n={15} title="出错时" feat="友好提示 + 原始错误留档">
      <div className="tl"><Tli k="ok" t="读完 09 修订通知" m="6 秒" /></div>
      <ErrorItem item={S.ERROR_ITEM} />
    </Item>,
    <Item key={16} n={16} title="助手推荐插件" feat="插件安装入口之一" head={false}>
      <div className="g2-msg">资料区有 3 份表结构（06-商品品类表.xlsx 等）。启用「数据库证据源」后，可以直接从 erp 系统取真实表名、字段和外键，对象和数据表的对应会更准。</div>
      <PackRecommendationCard card={S.PACK_CARD} />
    </Item>,
    <Item
      key={17} n={17} title="拖入附件 · 粘贴截图" feat="作为资料附件发给助手"
      comp={{ val: '这是区域审批的流程截图', top: <div className="row wrap" style={{ gap: 6, marginBottom: 6 }}><span className="g2-att"><span className="g2-thumb" />截图 15.02.png<Button icon="x" size="xs" kind="ghost" /></span><span className="g2-att"><Icon name="filetext" size={14} />09-修订通知.docx<Button icon="x" size="xs" kind="ghost" /></span></div> }}
      post={<div className="g2-drop"><Icon name="upload" size={24} /><b>松开以添加附件</b><span className="xs muted">Markdown、Word、Excel、PDF、Drawio、TXT · 单个不超过 20 MB</span></div>}
    >
      <TimelineGroup group={S.ATTACH_GROUP} />
      <div style={{ height: 64 }} />
    </Item>,
    <Item key={18} n={18} title="下一步建议条 · 三种情况" feat="1 个主推 + 最多 2 个次选" head={false}>
      {S.SUG_STRIPS.map(([t, r, its]) => (
        <div key={t} className="g2-strip"><div className="row xs"><b>{t}</b><span className="faint">· {r}</span></div><SugStrip items={its} /></div>
      ))}
      <div className="xs faint mt8">和助手回复末尾的「下一步建议」同一个来源，不会两边说法不一</div>
    </Item>,
    <Item key={19} n={19} title="插件页发来的任务" feat="按钮只发命令，进度回到插件页" head={false}>
      <div className="row" style={{ justifyContent: 'flex-end' }}>
        <div className="g2-src" style={{ maxWidth: '88%', padding: '9px 12px', borderRadius: 12, background: '#EEF2F8', fontSize: 13 }}>
          <div className="xs muted row g4" style={{ marginBottom: 3 }}><Icon name="flask" size={12} />来自「场景走查」页</div>批量推演 23 个场景
        </div>
      </div>
      <div className="tl mt12">
        <Tli k="ok" t="读取 23 个场景和示例数据" m="22 秒" />
        <div className="tl-i"><span className="tl-dot wait"><span className="spin" style={{ width: 10, height: 10 }} /></span><div><div className="tl-t">推演到第 9 个 · 超量与审批提示</div><div className="tl-m">进行中 · 推演到的场景在这一轮结束前只读</div></div></div>
      </div>
      <div className="row xs muted g4 mt4"><Icon name="info" size={12} />页面上同时显示进度条和「去对话里看」；跑完结果回到场景走查页的列表</div>
    </Item>,
    <Item key={20} n={20} title="交互走查卡" feat="从场景走查页发起，判断点就是决策卡" head={false}><WalkthroughCard q={S.WALK_Q} /></Item>,
  ];
  const cols = [[], [], []];
  items.forEach((h, i) => cols[i % 3].push(h));
  return (
    <ShellP active="chat-states">
      <ActionsCtx.Provider value={DEMO_ACTIONS}>
        <WbCtx.Provider value={{ pid, entries: {} }}>
          <PageHead
            eyebrow="工作台 · 对话"
            title="对话卡片与状态"
            desc="工作台对话区里的卡片和状态。下面的小样按组件单独画，编号对应功能点清单。"
            actions={<Button icon="layout" onClick={() => navigate(pagePath('wb-l2', pid))}>回到工作台</Button>}
          />
          <div className="sec-t"><Icon name="maximize" size={14} />产物卡（双击打开）<span className="sp" /><span className="faint" style={{ fontWeight: 500 }}>卡片本身只读；能编辑的弹窗和 Studio 用同一个编辑器，保存后生成版本</span></div>
          <div className="g2-pc">
            {PCS.map(([t, i, c, d, k, g]) => (
              <div key={t} className="g2-pci" onClick={() => navigate(pagePath(g, pid))}>
                <span className="ik" style={TONE[c]}><Icon name={i} size={18} /></span>
                <div className="grow" style={{ minWidth: 0 }}>
                  <div className="row g8"><b>{t}</b><Chip tone={k === '弹窗编辑' ? 'blue' : k === '跳 Studio' ? 'teal' : 'gray'} sm>{k}</Chip><span className="sp" /><span className="faint"><Icon name="chev" size={14} /></span></div>
                  <div className="d">{d}</div>
                </div>
              </div>
            ))}
          </div>
          <div className="sec-t"><Icon name="msg" size={14} />状态与交互</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 18, alignItems: 'start' }}>
            {cols.map((c, i) => <div key={i}>{c}</div>)}
          </div>
        </WbCtx.Provider>
      </ActionsCtx.Provider>
    </ShellP>
  );
}
