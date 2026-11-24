// Local stand-in for Vercel, for checking the site before deploying.
//   node serve_local.mjs 4173 > logs/local.log
// It runs the same middleware.js (so the same log lines appear on stdout), applies the
// headers from vercel.json, and serves public/ with clean addresses (/about -> about.html).
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import middleware from './middleware.js';

const root = path.dirname(fileURLToPath(import.meta.url));
const pub = path.join(root, 'public');
const vercel = JSON.parse(fs.readFileSync(path.join(root, 'vercel.json'), 'utf8'));
const extra = Object.fromEntries(vercel.headers[0].headers.map((h) => [h.key, h.value]));
const types = { '.html': 'text/html; charset=utf-8', '.txt': 'text/plain; charset=utf-8' };
const port = Number(process.argv[2] || 4173);

http.createServer((req, res) => {
  const url = new URL(req.url, `http://localhost:${port}`);
  middleware(new Request(url, { method: req.method, headers: req.headers }));
  let rel = decodeURIComponent(url.pathname);
  if (rel.endsWith('/')) rel += 'index';
  const base = path.normalize(path.join(pub, rel));
  const file = [base, base + '.html'].find(
    (f) => f.startsWith(pub) && fs.existsSync(f) && fs.statSync(f).isFile());
  if (!file) {
    res.writeHead(404, { ...extra, 'content-type': 'text/plain' });
    res.end('Not found\n');
    return;
  }
  res.writeHead(200, { ...extra, 'content-type': types[path.extname(file)] || 'application/octet-stream' });
  res.end(fs.readFileSync(file));
}).listen(port, () => console.error(`listing-test local server on http://localhost:${port}`));
