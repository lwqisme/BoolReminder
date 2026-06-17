#!/usr/bin/env node
/**
 * [leaps-parity] 直接驱动 leaps_ga_engine.js 的纯函数，用于与 Python
 * drawdown/leaps_option_ga.py 做数值一致性校验。
 *
 * leaps_ga_engine.js 是无 DOM 依赖的纯模块（module.exports），无需 worker 沙箱。
 *
 * 协议（stdin 一行 JSON 请求）：
 *   { "fn": "detectLeapsEntries"|"proxyOptionRoi"|"computeSellLadder",
 *     "args": [...] }
 * 输出（stdout 一行 JSON）：
 *   { "success": true, "result": <fn 返回值> }
 *   { "success": false, "error": "..." }
 *
 * 注意：JS 端 prices 形状为 [ts, price, dateStr] 三元组，调用方负责转换。
 */
'use strict';

const path = require('path');
const engine = require(path.resolve(__dirname, '..', 'web', 'static', 'leaps_ga_engine.js'));

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (c) => { input += c; });
process.stdin.on('end', () => {
  let req;
  try {
    req = JSON.parse(input);
  } catch (e) {
    out({ success: false, error: 'invalid JSON: ' + e.message });
    return;
  }
  try {
    const fnName = req.fn;
    const fn = engine[fnName];
    if (typeof fn !== 'function') {
      out({ success: false, error: 'unknown function: ' + fnName });
      return;
    }
    const result = fn.apply(null, req.args || []);
    out({ success: true, result });
  } catch (e) {
    out({ success: false, error: e && e.message ? e.message : String(e), stack: e && e.stack ? String(e.stack) : '' });
  }
});

function out(obj) {
  process.stdout.write(JSON.stringify(obj));
}
