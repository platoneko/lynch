import 'katex/dist/katex.min.css';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { reportOnline } from './offline/connection';
import './styles.css';

// The offline copy (public/sw.js). Production only: under vite dev it would cache dev modules. Missing where the
// origin is not secure (a desktop browser over the plain-HTTP tailnet address): the app then works as before.
if (import.meta.env.PROD && 'serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
  navigator.serviceWorker.addEventListener('message', (e) => {
    if (e.data === 'lynch-online') reportOnline();
  });
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
