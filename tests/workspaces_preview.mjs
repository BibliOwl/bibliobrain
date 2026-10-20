// Isolated UI regression fixture: node tests/workspaces_preview.mjs
// All API requests are simulated by workspaces_browser.js; no user draft is read or saved.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
const root = path.resolve(import.meta.dirname, '..');
const port = Number(process.argv[2]) || 7861;
http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/') {
    let html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
    html = html.replace('<head>', '<head><script src="/tests/workspaces_browser.js"></script>');
    html = html.replace(/<script src="\/static\/js\/app\/startup\.js[^>]*><\/script>/, '');
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    res.end(html);
    return;
  }
  const file = path.resolve(root, '.' + decodeURIComponent(url.pathname));
  if (!file.startsWith(root + path.sep) || !/^\/(static\/|tests\/workspaces_browser\.js$)/.test(url.pathname) || !fs.existsSync(file)) {
    res.writeHead(404); res.end(); return;
  }
  res.setHeader('Content-Type', ({'.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml','.woff2':'font/woff2'}[path.extname(file)] || 'application/octet-stream'));
  res.setHeader('Cache-Control', 'no-store');
  res.end(fs.readFileSync(file));
}).listen(port, '127.0.0.1', () => console.log(`Isolated workspace fixture: http://127.0.0.1:${port}`));
