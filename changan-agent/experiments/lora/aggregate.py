"""汇总多随机种子LoRA筛选；只读取已有report.json，不训练也不调用API。"""

from __future__ import annotations

import argparse
import copy
import json
import math
import re
import statistics
from datetime import datetime
from pathlib import Path


def metric_value(evaluation: dict, name: str):
    if name in evaluation:
        return evaluation[name]
    round_two_name = {"style_pass_rate": "style", "general_accuracy": "general"}.get(name)
    return evaluation.get("rates", {}).get(round_two_name) if round_two_name else None


def aggregate(folders: list[Path], *, style_gain: float = 0.05, max_general_drop: float = 0.1) -> dict:
    if not folders:
        raise ValueError("至少需要一个实验目录")
    if not 0 < style_gain <= 1 or not 0 <= max_general_drop <= 1:
        raise ValueError("评分门槛超出有效范围")
    reports = []
    signatures = set()
    seen_seeds = set()
    for folder in folders:
        report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
        if report.get("status") != "completed":
            raise ValueError(f"实验未完成：{folder}")
        required = {"baseline", "prompt", "external", "lora", "hybrid"}
        evaluations = {row["condition"]: row for row in report.get("evaluations", [])}
        if set(evaluations) != required or len(report.get("evaluations", [])) != len(required):
            raise ValueError(f"实验条件不完整：{folder}")
        seed = report.get("seed", report.get("protocol", {}).get("seed"))
        if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
            raise ValueError(f"实验缺少有效随机种子：{folder}")
        if report.get("protocol", {}).get("seed", seed) != seed:
            raise ValueError(f"随机种子与实际协议不一致：{folder}")
        for key in ("data_sha256", "code_sha256"):
            if not isinstance(report.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", report[key]):
                raise ValueError(f"实验缺少有效的{key}：{folder}")
        for evaluation in evaluations.values():
            for name in ("style_pass_rate", "general_accuracy"):
                value = metric_value(evaluation, name)
                if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                    raise ValueError(f"实验指标无效：{folder}，{name}")
        if seed in seen_seeds:
            raise ValueError(f"随机种子重复：{seed}")
        seen_seeds.add(seed)
        protocol = copy.deepcopy(report.get("protocol", {}))
        protocol.pop("seed", None)
        signatures.add((json.dumps(protocol, sort_keys=True), report.get("data_sha256"), report.get("code_sha256")))
        reports.append((folder, report, evaluations))
    if len(signatures) != 1:
        raise ValueError("多种子结果的训练协议、数据或代码不一致，不能汇总")

    metrics = {}
    for condition in ("baseline", "prompt", "external", "lora", "hybrid"):
        for name in ("style_pass_rate", "general_accuracy"):
            values = [float(metric_value(evaluations[condition], name)) for _, _, evaluations in reports]
            metrics[f"{condition}.{name}"] = {
                "values": values,
                "mean": statistics.mean(values),
                "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
                "minimum": min(values),
                "maximum": max(values),
            }
    best_non_lora = max(metrics["prompt.style_pass_rate"]["mean"], metrics["external.style_pass_rate"]["mean"])
    general_ok = all(
        min(metric_value(evaluations["lora"], "general_accuracy"), metric_value(evaluations["hybrid"], "general_accuracy"))
        >= metric_value(evaluations["baseline"], "general_accuracy") - max_general_drop
        for _, _, evaluations in reports
    )
    style_ok = max(metrics["lora.style_pass_rate"]["mean"], metrics["hybrid.style_pass_rate"]["mean"]) >= best_non_lora + style_gain
    reload_ok = all(report.get("reload_exact_match") is True for _, report, _ in reports)
    rollback_ok = all(report.get("rollback_exact_match") is True for _, report, _ in reports)
    sufficient_seeds = len(reports) >= 3
    shared_protocol = copy.deepcopy(reports[0][1].get("protocol", {}))
    shared_protocol.pop("seed", None)
    return {
        "version": 1,
        "runs": [str(folder) for folder, _, _ in reports],
        "seeds": [report.get("seed", report.get("protocol", {}).get("seed")) for _, report, _ in reports],
        "data_sha256": reports[0][1].get("data_sha256"),
        "code_sha256": reports[0][1].get("code_sha256"),
        "protocol": shared_protocol,
        "metrics": metrics,
        "gate": {"style_gain": style_gain, "max_general_drop": max_general_drop, "style_ok": style_ok,
                  "general_ok": general_ok, "reload_ok": reload_ok, "rollback_ok": rollback_ok,
                  "sufficient_seeds": sufficient_seeds,
                  "continue_lora": sufficient_seeds and style_ok and general_ok and reload_ok and rollback_ok},
        "interpretation": "多种子合成筛选只判断是否值得继续研究，不代表真实聊天质量或跨模型迁移。",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folders", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = aggregate(args.folders)
    output = args.output or args.folders[0].parent / (datetime.now().strftime("%Y-%m-%d_%H%M%S_%f") + "-lora-multiseed-summary.json")
    if output.exists() or output.with_suffix(".md").exists():
        parser.error("结果文件已存在，请选择新的输出路径")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    gate = result["gate"]
    metrics = result["metrics"]
    lines = ["# LoRA 多种子筛选汇总", "", "仅虚构数据；不是正式科研结论。", "", "| 条件 | 格式均值±标准差 | 基础能力均值±标准差 |", "|---|---:|---:|"]
    for condition in ("baseline", "prompt", "external", "lora", "hybrid"):
        style = metrics[f"{condition}.style_pass_rate"]
        general = metrics[f"{condition}.general_accuracy"]
        lines.append(f"| {condition} | {style['mean']:.3f} ± {style['stdev']:.3f} | {general['mean']:.3f} ± {general['stdev']:.3f} |")
    lines += ["", f"门槛：格式收益={'通过' if gate['style_ok'] else '未通过'}；基础能力保留={'通过' if gate['general_ok'] else '未通过'}；保存重载={'通过' if gate['reload_ok'] else '未通过'}；停用回滚={'通过' if gate['rollback_ok'] else '未通过'}。", f"结论：{'可以继续LoRA研究' if gate['continue_lora'] else '暂不继续，也不启用适配器'}。", "", "多种子只用于发现不稳定性；真实聊天、跨模型迁移和长期记忆收益仍未证明。"]
    output.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(output)
    return 0 if result["gate"]["continue_lora"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
