// 保存后的告警、保存被拒的错误清单（api/fileSaveFeedback.js 的 note），留到下一次保存
import { Note } from '../ui/index.jsx';

export default function SaveNote({ note, className }) {
  if (!note) return null;
  return (
    <Note tone={note.tone} className={className}>
      <b>{note.title}</b>
      {note.lines.map((line, i) => <div key={`${i}-${line}`}>{line}</div>)}
    </Note>
  );
}
