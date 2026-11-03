"""补测真实评测的失败请求，并做一次真实提取到模型切换的接续；保留原报告。"""

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app_runtime import redact
from long_term_memory import MemoryStore
from orchestrator import Provider, BudgetExceeded, parse_json_object
from persistence import read_json, write_json
from secret_store import unprotect
from experiments.memory.evaluate import messages_for, score


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--real", action="store_true")
    args = parser.parse_args()
    if not args.real:
        parser.error("真实补测需要 --real")
    original = read_json(args.report)
    if original.get("data") != "synthetic_only":
        parser.error("只允许本项目虚构评测报告")
    folder = args.report.parent / (datetime.now().strftime("recovery-%H%M%S"))
    folder.mkdir(exist_ok=False)
    protocol = read_json(args.report.with_name("protocol.json"))
    if hashlib.sha256(args.report.with_name("protocol.json").read_bytes()).hexdigest() != original["protocol_sha256"]:
        parser.error("原协议校验失败")
    config = read_json(ROOT / "data/config/agent_config.json")
    providers = {}
    previous_attempts = sum(v["attempts"] for v in original["usage"].values())
    report = {"started": datetime.now().isoformat(), "original_report": str(args.report), "data": "synthetic_only",
              "previous_attempts": previous_attempts, "max_total_attempts_including_original": 24,
              "additional_token_soft_limit": 20000, "runs": [], "end_to_end": {}}
    def guard(reserved_tokens=0):
        if previous_attempts + sum(p.usage["attempts"] for p in providers.values()) >= 24 or sum(p.usage["total_tokens"] for p in providers.values()) + reserved_tokens > 20000:
            raise BudgetExceeded("达到补测用量上限")
    def save():
        report["usage"] = {k: dict(v.usage) for k, v in providers.items()}
        write_json(folder / "report.json", report)
    try:
        for name in ("gpt", "deepseek"):
            provider = Provider(name, config[name + "_base_url"], unprotect(config.get(name + "_key_enc", "")) or config.get(name + "_key", ""),
                                config[name + "_model"], config.get(name + "_reasoning", ""), timeout=120, retries=1)
            if provider.model != original["models"][name]["requested_model"] or provider.base_url != original["models"][name]["endpoint"]:
                raise ValueError("模型或端点已改变，不能混入同一次评测")
            provider.before_request = guard
            provider.check_reservation = lambda cost, tokens: guard(tokens)
            provider.on_usage = save
            provider.configure_budget(config.get(name + "_input_cost_per_1k", 0), config.get(name + "_output_cost_per_1k", 0), currency=config.get("cost_currency", ""))
            providers[name] = provider
        for row in original["runs"]:
            if "error" not in row:
                continue
            retried = {"snapshot": row["snapshot"], "condition": row["condition"], "provider": row["provider"], "original_error": row["error"]}
            questions = next(s["questions"] for s in protocol["snapshots"] if s["id"] == row["snapshot"])
            begin = time.monotonic()
            try:
                answer = providers[row["provider"]].chat(row["input"], temperature=0, max_tokens=2048)
                retried["raw_answer"] = answer
                answers = parse_json_object(answer)["answers"]
                if set(answers) != {q["id"] for q in questions}:
                    raise ValueError("回答字段不完整")
                retried["scores"] = [{"id": q["id"], "passed": score(q, answers.get(q["id"]))} for q in questions]
            except Exception as exc:
                retried["error"] = redact(str(exc))[:1000]
            retried["seconds"] = round(time.monotonic() - begin, 3)
            report["runs"].append(retried)
            save()
            print(retried["snapshot"], retried["condition"], retried["provider"], retried.get("error", "完成"), flush=True)
        store = MemoryStore(folder / "synthetic-memory.sqlite3")
        source = "请记住，青禾的内部代号是 QH-731，发布日期是2027年2月14日。"
        report["end_to_end"]["source"] = source
        store.extract(providers["gpt"], user_id="virtual", project_id="p-a", source_chat="c-e2e", source_message="u-e2e", text=source)
        candidates = store.candidates("virtual", "p-a")
        report["end_to_end"]["candidates"] = candidates
        # 只确认已标注虚构资料中的严格子集；这不是对真实用户候选的自动确认策略。
        approved = []
        for c in candidates:
            if all(value in source for value in __import__("re").findall(r"[A-Za-z]+-\d+|\d+", c["content"])) and ("QH-731" in c["content"] or "2027年2月14日" in c["content"]):
                approved.append(store.confirm_candidate("virtual", "p-a", c["id"], kind=c["kind"], title=c["title"], content=c["content"]))
        store = MemoryStore(folder / "synthetic-memory.sqlite3")
        questions = protocol["snapshots"][0]["questions"][:2]
        context = store.context("virtual", "p-a", "青禾内部代号与发布日期")
        answer = providers["deepseek"].chat(messages_for(questions, memories=context), temperature=0, max_tokens=1024)
        answers = parse_json_object(answer)["answers"]
        report["end_to_end"].update(confirmation="synthetic_fixture_only", approved=approved, raw_answer=answer,
                                    scores=[{"id": q["id"], "passed": score(q, answers.get(q["id"]))} for q in questions])
        report["status"] = "completed"
    except Exception as exc:
        report["status"] = "interrupted"
        report["error"] = redact(str(exc))[:1000]
    finally:
        save()
    print(folder, flush=True)
    return int(report["status"] != "completed")


if __name__ == "__main__":
    raise SystemExit(main())
