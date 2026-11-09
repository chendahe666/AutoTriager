import React from 'react';
import { createRoot } from 'react-dom/client';
import '@cloudscape-design/global-styles/index-base.css';
import './font-faces.css';
import App from './App';
import './styles.css';
import './opsTheme.css';

createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);
