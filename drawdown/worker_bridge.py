"""服务端 JS Worker 桥接。

把模拟引擎统一到 ``web/static/strategy_parameter_lab_worker.js``（与参数实验室 / GA
单元格回放同一份 worker），在服务端用 Node 驱动，避免 Python ``simulate_portfolio``
与 JS worker 的双引擎/warmup 分歧。

调用链：Python 组装 v3 packet → subprocess(node, worker_runner.js) → worker 跑
start/batch/finish → 返回 ``batch_done.rows``，每个 ``row.observations[i]`` 含
``return_pct`` / ``max_drawdown_pct`` / ``trade_count`` / ``trade_log`` / ``series``。

worker 代码零改动；本模块只负责进程调度与 JSON 往返。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RUNNER_PATH = _REPO_ROOT / "scripts" / "worker_runner.js"

# 默认超时：单次预设回测足够（worker 含 3× verify 时略慢）。
_DEFAULT_TIMEOUT = 300


class WorkerBridgeError(RuntimeError):
    """worker 执行失败（node 缺失 / worker 抛错 / 超时）。"""


def _node_bin() -> str:
    configured = os.environ.get("WORKER_NODE_BIN")
    if configured:
        return configured
    found = shutil.which("node")
    if not found:
        raise WorkerBridgeError(
            "未找到 node 可执行文件——请确认容器已安装 nodejs "
            "（Dockerfile apt-get install nodejs），或设置 WORKER_NODE_BIN。"
        )
    return found


def run_simulations(
    packet: dict[str, Any],
    candidate_rows: list[list[Any]],
    *,
    timeout: int = _DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """驱动 worker 跑给定 packet + candidate_rows，返回 batch_done.rows。

    每个 row 形如 ``{candidate_id, candidate_key, observations: [...]}``；
    ``observations[i]`` 即 ``simulate()`` 的返回（含 metrics / trade_log / series）。
    """
    request = {"packet": packet, "candidate_rows": candidate_rows}
    payload = (json.dumps(request) + "\n").encode("utf-8")

    try:
        proc = subprocess.run(
            [_node_bin(), str(_RUNNER_PATH)],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkerBridgeError(f"worker 执行超时（{timeout}s）") from exc
    except FileNotFoundError as exc:
        raise WorkerBridgeError(f"无法启动 node：{exc}") from exc

    if proc.returncode != 0:
        stderr_tail = (proc.stderr or b"").decode("utf-8", errors="replace")[-800:]
        # runner 失败时仍会在 stdout 写一行错误 JSON，尽力解析
        err = _try_parse_error(proc.stdout) or stderr_tail
        raise WorkerBridgeError(f"worker 退出码 {proc.returncode}: {err}")

    try:
        resp = json.loads(proc.stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        stderr_tail = (proc.stderr or b"").decode("utf-8", errors="replace")[-800:]
        raise WorkerBridgeError(
            f"worker 返回非 JSON：{exc}；stdout 尾={proc.stdout[-400:]!r}；stderr 尾={stderr_tail}"
        )

    if not resp.get("success"):
        raise WorkerBridgeError(
            "worker 报错: "
            + str(resp.get("error"))
            + (" | recent=" + str(resp.get("recent_messages")) if resp.get("recent_messages") else "")
        )

    rows = resp.get("rows") or []
    if not rows:
        raise WorkerBridgeError("worker 未返回任何结果行（rows 为空）")
    return rows


def _try_parse_error(stdout: bytes) -> str:
    try:
        resp = json.loads(stdout.decode("utf-8"))
        if isinstance(resp, dict) and resp.get("error"):
            return str(resp.get("error"))
    except Exception:
        pass
    return ""
