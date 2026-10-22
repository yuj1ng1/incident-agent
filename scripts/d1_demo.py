"""Present the existing P6.4 offline investigation in a dedicated process.

The model responses and HTTP responses are fixtures. Agent, Worker, tools,
budgets, checkpoints and SQLite are the existing project implementations.
"""
from contextlib import redirect_stdout
from datetime import datetime, timezone
import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
from uuid import uuid4

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.p64_offline_investigation import run_case


def formatted(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def run_demo(output_dir, scenario="multi_round"):
    """Run once into a new directory; only read the resulting Runtime again."""
    root = Path(output_dir).resolve()
    captured_stdout = io.StringIO()
    with redirect_stdout(captured_stdout):
        report = run_case(root, scenario)

    from investigation.store import RuntimeStore

    runtime = json.loads((root / "runtime/report.json").read_text(encoding="utf-8"))
    store_path = root / "runtime/stores/runtime.sqlite3"
    store = RuntimeStore(store_path, read_only=True)
    saved = store.get(report["session"]["session_id"])
    checkpoint = store.load_checkpoint(saved)
    if (
        saved.model_dump(mode="json") != report["session"]
        or checkpoint != report["checkpoint"]
    ):
        raise ValueError("demo_sqlite_report_mismatch")
    with store.read() as db:
        counts = {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "investigation_sessions",
                "investigation_checkpoints",
                "investigation_events",
                "human_requests",
            )
        }
        integrity = db.execute("PRAGMA quick_check").fetchone()[0]
    if integrity != "ok":
        raise ValueError("demo_sqlite_integrity_failed")

    manifest = json.loads((root / "sha256.json").read_text(encoding="utf-8"))
    for relative_path, expected in manifest.items():
        actual = hashlib.sha256((root / relative_path).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError("demo_original_artifact_hash_mismatch")

    summary = {
        "mode": "offline_mock",
        "scenario": scenario,
        "output_dir": str(root),
        "incident_id": saved.incident_id,
        "session_id": saved.session_id,
        "session_status": saved.status.value,
        "mock_model_rounds": saved.total_model_requests,
        "tool_executions": saved.total_tool_executions,
        "sqlite_read_only_reopen_matches": True,
        "sqlite_quick_check": integrity,
        "sqlite_counts": counts,
        "original_artifact_hashes_match": True,
        "result_trust": report["result_trust"],
        "real_model_requests": report["real_model_requests"],
        "real_service_requests": report["real_service_requests"],
        "business_route_requests": report["business_route_requests"],
        "remediation_executions": report["remediation_executions"],
        "forbidden_io_attempts": report["forbidden_io_attempts"],
        "business_recovery_verified": report["business_recovery_verified"],
        "human_action_required": "人工核对证据和原始回答；本轮没有正式批准",
    }
    lines = [
        "Incident Agent D1 — 离线演示",
        "以下按刚完成的持久化记录展示流程；不是实时逐行跟踪。",
        "模型选择及回答=脚本化 Mock；HTTP=离线夹具；SQLite/Runtime=实际执行。",
        "夹具中的 0.27、日志、aaaaaaaa… 版本均为合成值，不是现场事实。",
        "\n[1] 输入合成告警（重复观察由现有 Monitor 去重）",
        formatted(runtime["incident"]["alerts"][-1]),
        "\n[2] 创建 Incident / Session 并由 Worker 完成调查",
        f"Incident: {saved.incident_id}",
        f"Session: {saved.session_id} / {saved.status.value}",
        f"Mock 模型轮数: {saved.total_model_requests}；"
        f"工具执行: {saved.total_tool_executions}",
        "\n[3] Mock SDK 给出工具选择，生产 Agent 解析，Worker 校验并执行",
        "Worker 先执行自动版本探针；该探针也计入工具预算。",
    ]
    model_call_ids = set()
    for index, request in enumerate(report["mock_model_requests"], 1):
        calls = [
            {key: item[key] for key in ("call_id", "name", "arguments")}
            for item in request["response"]["output"]
            if item["type"] == "function_call"
        ]
        model_call_ids.update(item["call_id"] for item in calls)
        selection = formatted(calls) if calls else "最终 Mock 回答"
        lines.append(f"第 {index} 轮: {selection}")
    lines.append("\n[4] 已提交的工具参数、结果与证据身份（全部来自离线夹具）")
    for record in checkpoint["records"]:
        origin = (
            "scripted_mock_model_selection"
            if record["call_id"] in model_call_ids else "runtime_automatic_probe"
        )
        lines.append(formatted({
            "selection_origin": origin,
            **{key: record[key] for key in (
                "name", "arguments", "call_id", "observation_id",
                "collected_at", "success", "result",
            )},
        }))
    lines.extend([
        "HTTP 夹具来源（URL 是匹配标签，没有发出网络请求）：",
        formatted([{
            key: row[key] for key in (
                "source", "url", "parameters", "evidence_origin",
                "actual_network_request",
            )
        } for row in runtime["http_fixtures"]]),
        "\n[5] 只读重新打开本次 SQLite，核对 Session / Checkpoint 和原件哈希",
        str(store_path),
        formatted({
            "quick_check": integrity,
            "counts": counts,
            "saved_records_match": True,
        }),
        "\n[6] 原始 Mock 调查结论（逐字保留）",
        checkpoint["result"]["answer"],
        "证据限制：告警没有 starts_at；日志查询的是最近窗口，不是已确认的告警窗口。",
        "夹具版本已知仅是合成值，未收集代码因果证据，不能确认真实代码根因。",
        "\n[7] 等待人工审阅；流程完成不等于结论可信或业务恢复",
        formatted(report["result_trust"]),
        "mechanical_scope=result_schema_only 只说明结果结构合法，不代表事实或引用验证通过。",
        "本连续流程没有 HumanRequest 或修复提案；人工审阅不会自动改变 ResultTrust。",
        "修复提案审批是独立模块，操作方式及隔离验证见 D1 教程；本入口不执行修复。",
        "\n运行摘要：",
        formatted(summary),
        f"完整证据: {root / 'report.json'}",
        f"可读输出: {root / 'demo-transcript.txt'}",
    ])
    (root / "runtime-stdout.txt").write_text(
        captured_stdout.getvalue(), encoding="utf-8",
    )
    (root / "demo-summary.json").write_text(
        formatted(summary) + "\n", encoding="utf-8",
    )
    (root / "demo-transcript.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    demo_manifest = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    }
    (root / "demo-sha256.json").write_text(
        formatted(demo_manifest) + "\n", encoding="utf-8",
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="必须是尚不存在的新目录")
    parser.add_argument(
        "--scenario", choices=("multi_round", "no_data"), default="multi_round",
    )
    arguments = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = arguments.output_dir or (
        Path(__file__).resolve().parents[1]
        / "data" / f"d1-demo-{stamp}-{uuid4().hex[:8]}"
    )
    try:
        summary = run_demo(target, arguments.scenario)
    except FileExistsError:
        print(
            "演示目录已存在，已拒绝覆盖；请省略 --output-dir 或选择新目录。",
            file=sys.stderr,
        )
        return 2
    transcript = Path(summary["output_dir"]) / "demo-transcript.txt"
    print(transcript.read_text(encoding="utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
