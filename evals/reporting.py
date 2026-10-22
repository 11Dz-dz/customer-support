"""版本元数据、分母明确的指标、人工审查队列和可比性检查。"""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path

from evals.dataset import ROOT, digest


def percentile(values, p):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    position = (len(values) - 1) * p
    low, high = math.floor(position), math.ceil(position)
    return round(values[low] + (values[high] - values[low]) * (position - low), 2)


def manifest(cases, source):
    from embedding_client import EmbeddingSettings
    from postgres_config import PROJECT_DIR
    files = sorted(list(PROJECT_DIR.glob("*.py")) + list(ROOT.glob("*.py")))
    hashes = {str(p.relative_to(PROJECT_DIR)): digest(p) for p in files}
    code_hash = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    versions = {}
    for name in ("langgraph", "langchain-core", "langchain-deepseek", "langgraph-checkpoint-postgres", "onnxruntime"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return {"created_at": datetime.now(timezone.utc).isoformat(), "dataset_version": "1.0.0", "scorer_version": "1.0.4",
            "dataset_sha256": digest(ROOT / "cases.jsonl"), "gold_sha256": digest(ROOT / "retrieval_gold.json"),
            "selected_ids": [c["id"] for c in cases], "code_sha256": code_hash, "code_files": hashes,
            "source_fingerprint": source, "dependencies": versions,
            "model": {"name": os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
                      "router_temperature": 0.0, "specialist_temperature": 0.2},
            "embedding": EmbeddingSettings.from_env().public_configuration(),
            "measurement_notes": ["同一进程串行评测；首次检索可能包含模型冷启动。",
                                  "延迟包含路由和模型调用，不含临时数据准备；并行工具耗时不相加。",
                                  "模型调用数为框架回调次数，不代表底层HTTP重试次数。",
                                  "回答依据完整性与推荐适用性仍待独立人工审查。"]}


def summarize(results):
    dimensions = defaultdict(lambda: {"passed": 0, "total": 0})
    turns, errors, failures, retrieval = [], [], Counter(), []
    for result in results:
        if result.get("error_type"):
            errors.append(result["error_type"])
        for turn in result.get("turns", []):
            turns.append(turn)
            if not result.get("unavailable"):
                retrieval.extend(turn.get("score", {}).get("retrieval", []))
            for check in ([] if result.get("unavailable") else turn.get("score", {}).get("checks", [])):
                d = dimensions[check["dimension"]]
                d["total"] += 1
                d["passed"] += int(check["passed"])
                if not check["passed"]:
                    failures[check["dimension"] + ":" + check["name"]] += 1
    dimension_metrics = {name: {**counts, "rate": counts["passed"] / counts["total"]}
                         for name, counts in dimensions.items() if counts["total"]}
    models = [m for t in turns for m in t.get("models", [])]
    known_usage = [m for m in models if m.get("total_tokens") is not None]
    available = [r for r in results if not r.get("unavailable")]
    available_turns = [t for r in available for t in r.get("turns", [])]
    return {"case_count": len(results), "scored_case_count": len(available),
            "unavailable_cases": len(results) - len(available), "turn_count": len(turns),
            "auto_passed_cases": sum(r.get("auto_pass", False) for r in available),
            "auto_pass_rate": sum(r.get("auto_pass", False) for r in available) / len(available) if available else None,
            "critical_failed_cases": sum(r.get("critical_failure", False) for r in results),
            "runtime_error_cases": len(errors), "manual_review_pending_cases": len(available),
            "dimensions": dimension_metrics, "failures": dict(failures),
            "latency_ms": {"samples": len(available_turns), "excluded_unavailable_turns": len(turns) - len(available_turns),
                           "p50": percentile([t.get("elapsed_ms") for t in available_turns], .5),
                           "p95": percentile([t.get("elapsed_ms") for t in available_turns], .95),
                           "ttft_samples": sum(t.get("first_text_ms") is not None for t in available_turns),
                           "ttft_p50": percentile([t.get("first_text_ms") for t in available_turns], .5)},
            "tools": {"requests": sum(len(t.get("tool_requests", [])) for t in turns),
                      "executions_started": sum(len(t.get("tools", [])) for t in turns),
                      "cache_hits": sum(sum(e.get("event") == "tool_cache_hit" for e in t.get("events", [])) for t in turns)},
            "models": {"calls": len(models), "usage_reported_calls": len(known_usage),
                       "token_usage_coverage": len(known_usage) / len(models) if models else None,
                       "reported_input_tokens": sum(m.get("input_tokens") or 0 for m in known_usage) if known_usage else None,
                       "reported_output_tokens": sum(m.get("output_tokens") or 0 for m in known_usage) if known_usage else None,
                       "reported_total_tokens": sum(m["total_tokens"] for m in known_usage) if known_usage else None},
            "retrieval": {"label_status": "partial_reference_draft", **{
                channel: {"samples": len(group), **{
                    key: sum(r[key] for r in group) / len(group) if group else None
                    for key in ("hit_at_5", "reference_recall_at_5", "mrr_at_5")}}
                for channel in ("agent_candidates", "retriever_probe")
                for group in [[r for r in retrieval if r.get("channel") == channel]]}}}


def compare_reports(current, baseline):
    fields = ("dataset_sha256", "gold_sha256", "scorer_version", "source_fingerprint", "selected_ids")
    reasons = [key + " 不一致" for key in fields if current["manifest"].get(key) != baseline["manifest"].get(key)]
    if current.get("repeat") != baseline.get("repeat"):
        reasons.append("重复次数不一致")
    if any(r.get("unavailable") for r in current["results"] + baseline["results"]):
        reasons.append("存在模型服务不可用，不能据此比较Agent质量")
    if {(r["id"], r["repeat"]) for r in current["results"]} != {(r["id"], r["repeat"]) for r in baseline["results"]}:
        reasons.append("实际完成的场景不一致")
    if reasons:
        return {"comparable": False, "reasons": reasons}
    old = {(r["id"], r["repeat"]): r for r in baseline["results"]}
    regressions, improvements = [], []
    for result in current["results"]:
        before = old.get((result["id"], result["repeat"]))
        if before and before["auto_pass"] != result["auto_pass"]:
            (improvements if result["auto_pass"] else regressions).append(result["id"])
    return {"comparable": True, "regressions": regressions, "improvements": improvements,
            "auto_pass_rate_delta": current["summary"]["auto_pass_rate"] - baseline["summary"]["auto_pass_rate"],
            "model_changed": current["manifest"]["model"] != baseline["manifest"]["model"],
            "note": "这是固定样本的描述性对比，不代表统计显著性；模型/代码等变化需结合元数据解释。"}


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str, allow_nan=False), encoding="utf-8")
    Path(path).chmod(0o600)


def write_report(directory, report):
    directory = Path(directory)
    report["summary"] = summarize(report["results"])
    s = report["summary"]
    s["planned_case_count"] = len(report["manifest"]["selected_ids"]) * report["repeat"]
    report["gate_pass"] = bool(s["case_count"] == s["planned_case_count"] and s["case_count"] and s["auto_passed_cases"] == s["case_count"]
                              and not s["critical_failed_cases"] and report.get("source_unchanged") is True)
    write_json(directory / "report.json", report)
    rate_text = f"{s['auto_pass_rate']:.1%}" if s['auto_pass_rate'] is not None else "未知（无可评分场景）"
    lines = ["# Agent 评测报告", "", "此报告的通过率只代表自动规则，不等同于完整回答质量或生产可靠性。", "",
             f"- 完成场景：{s['case_count']}/{s['planned_case_count']}；轮次：{s['turn_count']}；自动规则通过：{s['auto_passed_cases']}。",
             f"- 可评分场景：{s['scored_case_count']}；模型服务不可用：{s['unavailable_cases']}。",
             f"- 自动规则通过率（仅可评分场景）：{rate_text}；严重失败：{s['critical_failed_cases']}；运行错误：{s['runtime_error_cases']}。",
             f"- 原始业务表未变化：{report.get('source_unchanged')}；自动回归门禁通过：{report['gate_pass']}。",
             f"- 待人工审查场景：{s['manual_review_pending_cases']}。", "", "## 分项规则", "",
             "|维度|通过/总数|通过率|", "|---|---:|---:|"]
    for name, metric in s["dimensions"].items():
        lines.append(f"|{name}|{metric['passed']}/{metric['total']}|{metric['rate']:.1%}|")
    lines.extend(["", "## 耗时与消耗", "", f"- 延迟：{s['latency_ms']}。", f"- 工具：{s['tools']}。",
                  f"- 模型及服务端报告的 Token：{s['models']}。", f"- 检索参考指标：{s['retrieval']}。",
                  "", "首段正文可能是调用工具前的公开说明，不等同于最终答案就绪时间。Token 缺失不按0消耗处理；不估算货币费用。",
                  "检索标签是基于源数据的初稿正例，非完整相关性标注；指标不证明推荐适用性。", "", "## 失败案例", ""])
    for result in report["results"]:
        if not result["auto_pass"]:
            if result.get("unavailable"):
                lines.append(f"- {result['id']}：模型服务不可用，未纳入Agent质量分数。")
                continue
            reasons = [c["name"] for t in result["turns"] for c in t.get("score", {}).get("checks", []) if not c["passed"]]
            lines.append(f"- {result['id']}（{result['split']}，第{result['repeat']}次）：{', '.join(reasons) or result.get('error_type', 'fixture_side_effect')}。")
    if report.get("comparison"):
        lines.extend(["", "## 版本对比", "", "```json", json.dumps(report["comparison"], ensure_ascii=False, indent=2), "```"])
    path = directory / "summary.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    reviews = []
    for result in report["results"]:
        if result.get("unavailable"):
            continue
        for i, turn in enumerate(result["turns"], 1):
            reviews.append({"case_id": result["id"], "repeat": result["repeat"], "turn": i,
                            "input": turn["input"], "answer": turn.get("answer"), "verdict": "pending",
                            "rubric": ["答案是否完成用户请求", "每项商品/订单断言是否有工具证据", "有无捏造政策、库存、币种或交易状态", "推荐理由是否超出描述依据"],
                            "evidence": f"report.json/results/{result['id']}/turns/{i}", "notes": ""})
    review_path = directory / "review_queue.jsonl"
    review_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in reviews), encoding="utf-8")
    review_path.chmod(0o600)
