import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { syncThemeWithHost } from '../theme';
import '../styles.css';
import './da.css';
import DaApp from './DaApp';

syncThemeWithHost();

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <DaApp />
  </StrictMode>,
);
