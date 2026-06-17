#!/usr/bin/env node
/**
 * [worker-bridge] 服务端 Node 驱动 strategy_parameter_lab_worker.js
 *
 * 把浏览器 Web Worker 的全局环境 shim 到 Node vm 沙箱里，加载同一份 worker JS，
 * 按 start → batch → finish 协议驱动，收集 batch_done，把结果 JSON 写到 stdout。
 *
 * 协议（stdin 一行 JSON）：
 *   { "packet": {...v3 packet...}, "candidate_rows": [[...]], "include_series": bool }
 * 输出（stdout 一行 JSON）：
 *   { "success": true, "rows": [...] }   // rows 即 batch_done.rows，每个 row.observations[0] 含 metrics+trade_log
 *   { "success": false, "error": "...", "stack": "..." }
 *
 * worker 代码零改动；本文件只做环境适配。
 */
'use strict';

const vm = require('vm');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const WORKER_PATH = path.join(ROOT, 'web', 'static', 'strategy_parameter_lab_worker.js');
const STATIC_DIR = path.join(ROOT, 'web', 'static');

/** 把浏览器 /static/x.js?v=1 映射到仓库 web/static/x.js */
function mapStaticUrl(p) {
  const noQuery = String(p).split('?')[0];
  if (noQuery.startsWith('/static/')) return path.join(STATIC_DIR, noQuery.slice('/static/'.length));
  if (noQuery.startsWith('/')) return path.join(ROOT, noQuery.slice(1));
  return path.resolve(ROOT, noQuery);
}

// ── 消息收集 / 等待 ──────────────────────────────────────────────
const messages = [];
const waiters = [];
function postMessage(msg) {
  messages.push(msg);
  for (let i = waiters.length - 1; i >= 0; i--) {
    if (msg && msg.type === waiters[i].type) {
      const w = waiters.splice(i, 1)[0];
      w.resolve(msg);
    }
  }
}
function waitFor(type, timeoutMs) {
  const found = messages.find((m) => m && m.type === type);
  if (found) return Promise.resolve(found);
  return new Promise((resolve, reject) => {
    const w = { type, resolve, reject };
    waiters.push(w);
    setTimeout(() => {
      const idx = waiters.indexOf(w);
      if (idx >= 0) {
        waiters.splice(idx, 1);
        reject(new Error('timeout waiting for message type: ' + type));
      }
    }, timeoutMs);
  });
}

// ── 沙箱：shim 浏览器 Worker 全局 ────────────────────────────────
const timeOrigin = process.hrtime();
const sandbox = {};
// worker 的 diagnosticLog 用 console.info 输出 —— 全部重定向到 stderr，
// 保持 stdout 干净（只写最终 JSON 结果）。
const stderrConsole = {
  log: (...a) => process.stderr.write(a.map(fmtArg).join(' ') + '\n'),
  info: (...a) => process.stderr.write(a.map(fmtArg).join(' ') + '\n'),
  warn: (...a) => process.stderr.write(a.map(fmtArg).join(' ') + '\n'),
  error: (...a) => process.stderr.write(a.map(fmtArg).join(' ') + '\n'),
  debug: () => {},
};
function fmtArg(a) {
  if (a instanceof Error) return a.stack || a.message;
  if (typeof a === 'object' && a !== null) { try { return JSON.stringify(a); } catch (e) { return String(a); } }
  return String(a);
}
sandbox.console = stderrConsole;
sandbox.performance = { now: () => {
  const [s, n] = process.hrtime(timeOrigin);
  return s * 1000 + n / 1e6;
} };
sandbox.setTimeout = setTimeout;
sandbox.clearTimeout = clearTimeout;
sandbox.setInterval = setInterval;
sandbox.clearInterval = clearInterval;
sandbox.URL = URL;
sandbox.URLSearchParams = URLSearchParams;
sandbox.TextEncoder = TextEncoder;
sandbox.TextDecoder = TextDecoder;
sandbox.location = { href: 'file:///static/strategy_parameter_lab_worker.js?engine_v=1' };
sandbox.postMessage = postMessage;
sandbox.importScripts = function importScripts(p) {
  const fp = mapStaticUrl(p);
  const src = fs.readFileSync(fp, 'utf8');
  vm.runInContext(src, ctx, { filename: path.basename(fp) });
};
sandbox.self = sandbox; // worker 用 self.onmessage / self.location
sandbox.globalThis = sandbox;

const ctx = vm.createContext(sandbox);

// 加载 worker 源码 —— 顶层会调 importScripts(leaps_ga_engine.js) 并注册 self.onmessage
const workerSrc = fs.readFileSync(WORKER_PATH, 'utf8');
vm.runInContext(workerSrc, ctx, { filename: 'strategy_parameter_lab_worker.js' });

if (typeof sandbox.self.onmessage !== 'function') {
  process.stderr.write('worker did not register self.onmessage\n');
  process.exit(2);
}

// ── 主流程：读 stdin 请求，驱动 worker ───────────────────────────
async function main() {
  let input = '';
  process.stdin.setEncoding('utf8');
  for await (const chunk of process.stdin) input += chunk;
  if (!input.trim()) throw new Error('empty stdin');

  let req;
  try {
    req = JSON.parse(input);
  } catch (e) {
    throw new Error('invalid JSON request: ' + e.message);
  }
  const packet = req.packet || {};
  const candidateRows = Array.isArray(req.candidate_rows) ? req.candidate_rows : [];
  const runId = String(packet.run_id || 'bridged');

  // 信号模式：单候选，真实交易回放 + 仅信号日引擎运行。
  if (req.mode === 'signal') {
    const signalDate = String(req.engine_signal_date || '');
    const overrides = req.trade_overrides || {};
    sandbox.self.onmessage({
      data: {
        type: 'signal_sim', run_id: runId, worker_index: 0,
        packet, candidate_rows: candidateRows.length ? candidateRows : [[0, 0, 0, 'c0']],
        trade_overrides: overrides, engine_signal_date: signalDate,
      },
    });
    const done = await waitFor('signal_done', 300000);
    return { success: true, signal_trades: done.signal_trades || [], final_state: done.final_state || {} };
  }

  // start
  sandbox.self.onmessage({ data: { type: 'start', run_id: runId, worker_index: 0, total_simulations: candidateRows.length, packet } });
  await waitFor('ready', 30000);

  // batch
  sandbox.self.onmessage({ data: { type: 'batch', run_id: runId, worker_index: 0, batch_id: 'b0', candidate_rows: candidateRows } });
  const done = await waitFor('batch_done', 300000);

  // finish（可选，不等待 done 也行）
  try {
    sandbox.self.onmessage({ data: { type: 'finish', run_id: runId, worker_index: 0 } });
  } catch (e) { /* ignore */ }

  return { success: true, rows: done.rows || [] };
}

main()
  .then((out) => {
    writeJsonAndExit(JSON.stringify(out), 0);
  })
  .catch((err) => {
    // worker 抛出的错误可能带 __parameter_lab_context
    const payload = {
      success: false,
      error: err && err.message ? err.message : String(err),
      stack: err && err.stack ? String(err.stack) : '',
    };
    if (err && err.__parameter_lab_context) payload.context = err.__parameter_lab_context;
    // 把已收集到的诊断消息一并回传，便于排查
    payload.recent_messages = messages.slice(-8).map((m) => (m && m.type) || 'unknown');
    writeJsonAndExit(JSON.stringify(payload), 1);
  });

// 大结果（含 trade_log/sold_lot_slices）可能超过 stdout 缓冲，必须等 drain 再退出，
// 否则 process.exit 会截断输出 → Python 侧 JSON 解析失败。
function writeJsonAndExit(text, code) {
  if (process.stdout.write(text)) {
    process.exit(code);
  } else {
    process.stdout.once('drain', () => process.exit(code));
  }
}
