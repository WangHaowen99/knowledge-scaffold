// 输入框上方：运行状态行（在做哪一步 · 在等什么 · 跑了多久；有思考内容时点开看）和平台代存的下一条命令
import { useEffect, useState } from 'react';
import { Icon, cx } from '../../ui/index.jsx';
import { fmtElapsed } from './composerModel.js';
import { useStickBottom } from './parts.jsx';

export function useTick(on, ms = 1000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!on) return undefined;
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [on, ms]);
  return now;
}

export function RunLine({ info }) {
  const [open, setOpen] = useState(false);
  const now = useTick(!!info);
  const box = useStickBottom(info?.thinking || '');
  if (!info) return null;
  const since = info.since ? new Date(info.since).getTime() : NaN;
  const thinking = info.thinking;
  return (
    <div className="wb-live wb-run">
      <div className={cx('hd', thinking && 'tg')} onClick={thinking ? () => setOpen(!open) : undefined}>
        <span className="spin" />
        <span className="t ell">{[info.title, info.doing].filter(Boolean).join(' · ')}</span>
        {Number.isFinite(since) ? <span className="tm">{fmtElapsed(now - since)}</span> : null}
        {thinking ? <Icon name="chevd" size={12} className={cx('cv', open && 'on')} /> : null}
      </div>
      {open && thinking ? <div className="bd" ref={box.ref} onScroll={box.onScroll}>{thinking}</div> : null}
    </div>
  );
}

// 运行中发的命令由平台代存，这一轮结束后开始；打断后不自动开始，等你点「现在开始」
export function NextBar({ next, running, onStart, onCancel }) {
  if (!next) return null;
  const label = next.label || next.text || next.command;
  return (
    <div className="wb-next">
      <Icon name="clock" size={13} />
      <span className="ell">{running && !next.paused ? `这一轮结束后开始「${label}」` : `接下来「${label}」`}</span>
      <span className="sp" />
      {!running ? <span className="lk p" onClick={onStart}>现在开始</span> : null}
      <span className="lk" onClick={onCancel}>取消</span>
    </div>
  );
}
