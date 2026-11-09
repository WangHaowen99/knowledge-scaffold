import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { syncThemeWithHost } from './theme';
import './styles.css';

syncThemeWithHost();

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
