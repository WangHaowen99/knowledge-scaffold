import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

// 统一的 Markdown 视图：知识包文档与对话气泡共用，样式见 redesign.css 的 .mdv。
export default function Markdown({ children }) {
  return (
    <div className="mdv">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          pre({ children: preChildren, ...props }) {
            const code = Array.isArray(preChildren) ? preChildren[0] : preChildren;
            const lang = /language-(\w+)/.exec(code?.props?.className || '')?.[1] || '';
            return <pre data-lang={lang} {...props}>{preChildren}</pre>;
          },
          a({ ...props }) {
            return <a target="_blank" rel="noreferrer" {...props} />;
          },
        }}
      >
        {typeof children === 'string' ? children : ''}
      </ReactMarkdown>
    </div>
  );
}
