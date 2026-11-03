"""从保留的原始回答复核评分；无API请求，无模型训练。"""

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from experiments.memory.evaluate import score
from experiments.lora.run import style_score
from orchestrator import parse_json_object
from persistence import read_json, write_json


def summarize_memory(folder, recovery_folder):
    original, recovery = read_json(folder / "report.json"), read_json(recovery_folder / "report.json")
    protocol = read_json(folder / "protocol.json")
    rows = []
    for row in original["runs"]:
        final = next((r for r in recovery["runs"] if all(r[k] == row[k] for k in ("snapshot", "condition", "provider")) and "error" not in r), row)
        questions = next(s["questions"] for s in protocol["snapshots"] if s["id"] == row["snapshot"])
        try:
            answers = parse_json_object(final.get("raw_answer", "{}" )).get("answers", {})
            if not isinstance(answers, dict):
                answers = {}
        except ValueError:
            answers = {}
        rows.append({"snapshot": row["snapshot"], "condition": row["condition"], "provider": row["provider"], "recovered": "error" in row and "error" not in final,
                     "scores": [{"id": q["id"], "passed": score(q, answers.get(q["id"]))} for q in questions]})
    aggregate = []
    for name in original["models"]:
        for condition in protocol["conditions"]:
            selected = [r for r in rows if r["provider"] == name and r["condition"] == condition]
            aggregate.append({"provider": name, "condition": condition, "passed": sum(q["passed"] for r in selected for q in r["scores"]),
                              "questions": sum(len(r["scores"]) for r in selected)})
    result = {"scoring_version": 2, "scorer_sha256": hashlib.sha256((ROOT / "experiments/memory/evaluate.py").read_bytes()).hexdigest(),
              "changes": ["允许明确标记已取消的旧日期，当前日期仍须正确", "三条编号必须恰好三条"],
              "source_report": "report.json", "recovery_report": str((recovery_folder / "report.json").relative_to(folder)),
              "original_errors_retained": sum("error" in r for r in original["runs"]),
              "requests_including_errors": sum(u["attempts"] for r in (original, recovery) for u in r["usage"].values()),
              "reported_tokens": sum(u["total_tokens"] for r in (original, recovery) for u in r["usage"].values()),
              "actual_cost": None, "price_note": "未配置价格，不能推断免费；失败请求的实际计费未知", "model_identifiers": original["models"],
              "end_to_end": recovery.get("end_to_end", {}).get("scores", []), "rows": rows, "aggregate": aggregate}
    write_json(folder / "results-v2.json", result)
    lines = ["# 真实模型接续：复核结果", "", "| 模型 | 条件 | 通过/题数 |", "|---|---|---:|"]
    for row in aggregate:
        lines.append(f"| {row['provider']} | {row['condition']} | {row['passed']}/{row['questions']} |")
    lines += ["", f"共{result['requests_including_errors']}次请求尝试，服务返回{result['reported_tokens']} tokens；未配置价格，实际费用未知。",
              "原始超时与用量限制均保留；另行补测，没有覆盖原报告。v2只修复评分器，不重写模型回答。",
              "两个时间快照、每条件10题、单次小样本。外部记忆未显示优于完整历史或摘要的质量优势，尚不能推广为正式准确率。",
              "自动提取的首条假设式虚构消息返回0条；明确说请记住的接续样例提取成功，确认只针对预标注虚构资料。真实用户候选始终等待人工确认。"]
    (folder / "报告-复核.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


def summarize_lora(folder):
    report = copy.deepcopy(read_json(folder / "report.json"))
    report["scoring_version"] = 2
    report["scorer_sha256"] = hashlib.sha256((ROOT / "experiments/lora/run.py").read_bytes()).hexdigest()
    report["scoring_correction"] = "恰好3条编号；原评分只检查包含1、2、3。保留原报告与回答。"
    for evaluation in report["evaluations"]:
        for row in evaluation["rows"]:
            if row["split"] == "test":
                row["passed"] = style_score(row["answer"])
        cases = [r for r in evaluation["rows"] if r["split"] == "test"]
        evaluation["style_pass_rate"] = sum(r["passed"] for r in cases) / len(cases)
    baseline, prompt, external, lora, hybrid = report["evaluations"]
    gate = report["protocol"]["screening_gate"]
    report["continue_lora"] = (max(lora["style_pass_rate"], hybrid["style_pass_rate"]) >= max(prompt["style_pass_rate"], external["style_pass_rate"]) + gate["style_over_best_non_lora"]
                               and min(lora["general_accuracy"], hybrid["general_accuracy"]) >= baseline["general_accuracy"] - gate["general_accuracy_drop_max"]
                               and report["reload_exact_match"] and report["rollback_exact_match"])
    write_json(folder / "results-v2.json", report)
    lines = ["# 固定模型LoRA：复核结果", "", "| 条件 | 格式偏好（6题） | 基础问答（4题） |", "|---|---:|---:|"]
    for row in report["evaluations"]:
        lines.append(f"| {row['condition']} | {round(row['style_pass_rate'] * 6)}/6 | {round(row['general_accuracy'] * 4)}/4 |")
    lines += ["", f"固定{report['protocol']['steps']}步，训练{report['training_seconds']}秒；峰值GPU张量分配{report['peak_gpu_bytes'] / 2**30:.2f} GiB，不是整机显存峰值。",
              f"保存重载输出一致：{report['reload_exact_match']}；停用回滚一致：{report['rollback_exact_match']}；是否通过继续投入门槛：{report['continue_lora']}。",
              "仅单种子小样本格式筛选，没有评估建议的事实质量；不代表真实长期记忆或跨模型参数迁移。没有接入日常聊天。"]
    (folder / "报告-复核.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory", type=Path)
    parser.add_argument("--recovery", type=Path)
    parser.add_argument("--lora", type=Path, nargs="*")
    args = parser.parse_args()
    if args.memory:
        if not args.recovery:
            parser.error("记忆复核需要 --recovery")
        summarize_memory(args.memory, args.recovery)
    for folder in args.lora or []:
        summarize_lora(folder)


if __name__ == "__main__":
    main()
