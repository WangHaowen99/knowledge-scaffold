// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './app/App.jsx';
import './shared/styles/base.css';

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
