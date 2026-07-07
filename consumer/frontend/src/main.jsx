import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import RedesignApp from './redesign/RedesignApp.jsx'

// 重设计版入口（feat/console-redesign 分支）。旧版入口保留在 ./App.jsx，
// 如需回退：把下方 RedesignApp 换回 App 即可。
createRoot(document.getElementById('root')).render(
  <StrictMode>
    <RedesignApp />
  </StrictMode>,
)
