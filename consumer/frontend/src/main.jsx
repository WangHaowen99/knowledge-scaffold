import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import RedesignApp from './redesign/RedesignApp.jsx'

// RedesignApp 是当前唯一受支持的前端入口。./App.jsx 仅保留历史实现参考，
// 其中的数据连接/绑定页面不兼容当前 ontology data-source API，不能作为回退入口。
createRoot(document.getElementById('root')).render(
  <StrictMode>
    <RedesignApp />
  </StrictMode>,
)
