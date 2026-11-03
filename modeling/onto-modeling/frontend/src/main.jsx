import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './app/App.jsx';
import './styles/base.css';
import './styles/shell.css';
import './styles/entry.css';
import './styles/workbench.css';
import './styles/chat-states.css';

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
