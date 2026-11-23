// 零依赖开发服务器：静态托管 web/，并把 /api/* 原样代理到本地 FastAPI（默认 127.0.0.1:8000）。
// 代理是为了同源 SSE，避免依赖后端 CORS；不缓存、不改写响应体。
//
//   node web/serve.mjs                 # http://127.0.0.1:5173
//   PORT=8080 API_ORIGIN=http://127.0.0.1:9000 node web/serve.mjs

import http from 'node:http';
import https from 'node:https';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT || 5173);
const HOST = process.env.HOST || '127.0.0.1';
const API_ORIGIN = (process.env.API_ORIGIN || 'http://127.0.0.1:8000').replace(/\/+$/, '');

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
  '.txt': 'text/plain; charset=utf-8',
};

const CSP = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  "connect-src 'self' http://127.0.0.1:* http://localhost:*",
  "base-uri 'none'",
  "form-action 'self'",
  "object-src 'none'",
].join('; ');

const server = http.createServer((req, res) => {
  let url;
  try {
    url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  } catch {
    res.writeHead(400, { 'content-type': 'text/plain; charset=utf-8' });
    res.end('Bad request');
    return;
  }

  if (
    url.pathname === '/api' ||
    url.pathname.startsWith('/api/') ||
    url.pathname === '/openapi.json' ||
    url.pathname === '/healthz'
  ) {
    proxyToApi(req, res, url);
    return;
  }
  void serveStatic(req, res, url);
});

async function serveStatic(req, res, url) {
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    res.writeHead(405, { allow: 'GET, HEAD', 'content-type': 'text/plain; charset=utf-8' });
    res.end('Method not allowed');
    return;
  }

  let pathname;
  try {
    pathname = decodeURIComponent(url.pathname);
  } catch {
    res.writeHead(400, { 'content-type': 'text/plain; charset=utf-8' });
    res.end('Bad path');
    return;
  }

  const relative = pathname === '/' ? 'index.html' : pathname.replace(/^\/+/, '');
  const filePath = path.resolve(ROOT, relative);
  if (filePath !== ROOT && !filePath.startsWith(ROOT + path.sep)) {
    res.writeHead(403, { 'content-type': 'text/plain; charset=utf-8' });
    res.end('Forbidden');
    return;
  }

  let body;
  try {
    body = await readFile(filePath);
  } catch {
    res.writeHead(404, {
      'content-type': 'text/plain; charset=utf-8',
      'cache-control': 'no-store',
      'x-content-type-options': 'nosniff',
    });
    res.end('Not found');
    return;
  }

  const ext = path.extname(filePath).toLowerCase();
  const headers = {
    'content-type': MIME[ext] || 'application/octet-stream',
    'cache-control': 'no-store',
    'x-content-type-options': 'nosniff',
    'referrer-policy': 'no-referrer',
    'content-security-policy': CSP,
  };
  res.writeHead(200, headers);
  if (req.method === 'HEAD') res.end();
  else res.end(body);
}

function proxyToApi(req, res, url) {
  const target = new URL(API_ORIGIN);
  const client = target.protocol === 'https:' ? https : http;
  const upstream = client.request(
    {
      protocol: target.protocol,
      hostname: target.hostname,
      port: target.port || (target.protocol === 'https:' ? 443 : 80),
      method: req.method,
      path: `${url.pathname}${url.search}`,
      headers: { ...req.headers, host: target.host },
    },
    (upstreamRes) => {
      const headers = { ...upstreamRes.headers };
      const contentType = String(headers['content-type'] || '');
      if (contentType.includes('text/event-stream')) {
        headers['cache-control'] = 'no-cache, no-transform';
        headers['x-accel-buffering'] = 'no';
      }
      res.writeHead(upstreamRes.statusCode || 502, headers);
      if (typeof res.flushHeaders === 'function') res.flushHeaders();
      upstreamRes.pipe(res);
      upstreamRes.on('error', () => {
        if (!res.writableEnded) res.end();
      });
    },
  );

  upstream.on('error', (error) => {
    if (res.headersSent) {
      if (!res.writableEnded) res.end();
      return;
    }
    res.writeHead(502, {
      'content-type': 'application/problem+json; charset=utf-8',
      'cache-control': 'no-store',
    });
    res.end(
      JSON.stringify({
        type: 'about:blank',
        title: 'Upstream unavailable',
        status: 502,
        code: 'PROXY_ERROR',
        message: `无法连接本地 API（${API_ORIGIN}）。请确认 FastAPI 已启动，或设置 API_ORIGIN。`,
        retryable: true,
        details: { reason: error.code || error.message },
      }),
    );
  });

  req.pipe(upstream);
  res.on('close', () => {
    if (!res.writableEnded) upstream.destroy();
  });
}

server.listen(PORT, HOST, () => {
  console.log(`文献推荐工具 M0: http://${HOST}:${PORT}`);
  console.log(`API 代理: /api/* -> ${API_ORIGIN}`);
});

server.on('error', (error) => {
  console.error(`无法启动开发服务器：${error.message}`);
  process.exitCode = 1;
});
