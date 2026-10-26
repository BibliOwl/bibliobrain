const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const root = path.resolve(__dirname, '..');

function createPreviewServer() {
  return http.createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    if (url.pathname === '/') {
      let html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8')
        .replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '');
      const mode = url.searchParams.get('mode') === 'normal' ? 'normal' : 'light';
      html = html.replace('<style id="startup-boot-critical">', `<script>localStorage.setItem('iassistant_performance-mode', '${mode}');</script><script src="/static/js/boot.js"></script><style id="startup-boot-critical">`);
      html = html.replace('</body>', '<script src="/tests/performance-mode-fixture.js"></script></body>');
      res.setHeader('Content-Type', 'text/html; charset=utf-8');
      res.end(html);
      return;
    }
    if (url.pathname.startsWith('/api/')) {
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify({ ok: true, rows: [], paths: [], metadata: [], notebooks: [], searches: [] }));
      return;
    }
    let target;
    try { target = path.resolve(root, '.' + decodeURIComponent(url.pathname)); }
    catch { res.writeHead(400).end(); return; }
    const allowed = target.startsWith(path.join(root, 'static') + path.sep)
      || target === path.join(__dirname, 'performance-mode-fixture.js');
    if (!allowed || !fs.existsSync(target) || !fs.statSync(target).isFile()) {
      res.writeHead(404).end();
      return;
    }
    const types = { '.css': 'text/css', '.js': 'text/javascript', '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2', '.ico': 'image/x-icon' };
    res.setHeader('Content-Type', types[path.extname(target)] || 'application/octet-stream');
    res.setHeader('Cache-Control', 'no-store');
    res.end(fs.readFileSync(target));
  });
}

module.exports = { createPreviewServer };
if (require.main === module) {
  const port = Number(process.env.PORT || 8766);
  createPreviewServer().listen(port, '127.0.0.1', () => console.log(`Preview: http://127.0.0.1:${port}/`));
}
