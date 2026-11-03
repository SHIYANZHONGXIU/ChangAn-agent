"""离线长会话记忆压力评测；只使用合成资料，不调用模型或读取私人聊天。"""

from __future__ import annotations

import argparse
import hashlib
import random
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from long_term_memory import MemoryStore
from persistence import write_json


def run_case(root: Path, seed: int, snapshots: int = 12) -> dict:
    """运行一个确定性长会话，返回可保存的指标与失败原因。"""
    if not 0 <= seed <= 2**32 - 1:
        raise ValueError("随机种子必须在0到4294967295之间")
    if not 1 <= snapshots <= 100:
        raise ValueError("快照数必须在1到100之间")
    path = Path(root) / f"memory-{seed}.sqlite3"
    if path.exists():
        raise FileExistsError("实验数据库已存在，请使用新的结果目录")
    store = MemoryStore(path)
    rng = random.Random(seed)
    checks = 0
    failures = []
    release_id = store.save(user_id="virtual", project_id="main", kind="fact", title="青禾发布日期",
                            content="青禾发布日期是2027年2月14日。", source_type="user",
                            source_chat="chat-main", source_message="m0")["id"]
    store.save(user_id="virtual", project_id="other", kind="fact", title="隔离项目口令", content="只属于另一个项目的内容。",
               source_type="user", source_chat="chat-other", source_message="m0")
    old_values = []

    def check(condition: bool, label: str):
        nonlocal checks
        checks += 1
        if not condition:
            failures.append(label)

    for snapshot in range(1, snapshots + 1):
        value = f"青禾发布日期改为2027年{((snapshot - 1) % 12) + 1}月{((snapshot * 3) % 27) + 1}日，版本{snapshot}。"
        old_values.append(value)
        store.save(user_id="virtual", project_id="main", kind="fact", title="青禾发布日期", content=value,
                   memory_id=release_id, source_type="user", source_chat="chat-main", source_message=f"m{snapshot}")
        for offset in range(5):
            number = rng.randrange(100000, 999999)
            store.save(user_id="virtual", project_id="main", kind="state", title=f"长会话事项{snapshot}-{offset}",
                       content=f"第{snapshot}轮的合成事项编号{number}。" + "长会话事项的虚构补充资料。" * 90, source_type="user",
                       source_chat="chat-main", source_message=f"m{snapshot}-{offset}")
        if snapshot % 3 == 0:
            store = MemoryStore(path)
        found = [row for row in store.search("virtual", "main", "青禾发布日期") if row["title"] == "青禾发布日期"]
        check(len(found) == 1, f"snapshot-{snapshot}:发布日期条目重复")
        check(found and found[0]["content"] == value, f"snapshot-{snapshot}:没有读取到最新发布日期")
        check(bool(found) and not any(old in found[0]["content"] for old in old_values[:-1]), f"snapshot-{snapshot}:旧值未被替换")
        context = store.context("virtual", "main", "青禾 长会话 事项")
        check(len(context) <= 6, f"snapshot-{snapshot}:上下文条数超限")
        check(sum(len(item["title"]) + len(item["content"]) for item in context) <= 5000,
              f"snapshot-{snapshot}:上下文字符数超限")
        other = store.search("virtual", "main", "隔离项目口令")
        check(not any("另一个项目" in item["content"] for item in other), f"snapshot-{snapshot}:项目隔离失败")

    forget_id = store.save(user_id="virtual", project_id="main", kind="fact", title="待彻底遗忘",
                           content="仅用于删除验收的合成内容。", source_type="user",
                           source_chat="forget-chat", source_message="forget-message")["id"]
    counts = store.forget_chat("virtual", "forget-chat")
    check(counts["memories"] == 1, "forget:没有删除来源记忆")
    check(not any(row["id"] == forget_id for row in store.list("virtual", "main")), "forget:删除后仍可检索")
    check(any(row["id"] == release_id for row in store.list("virtual", "main")), "forget:误删了其他聊天记忆")
    return {"seed": seed, "snapshots": snapshots, "checks": checks, "failures": failures,
            "passed": not failures, "database": str(path.name)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", default="7,23,41", help="逗号分隔的确定性种子")
    parser.add_argument("--snapshots", type=int, default=12)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        seeds = [int(item.strip()) for item in args.seeds.split(",") if item.strip()]
        if not seeds or len(set(seeds)) != len(seeds):
            raise ValueError("种子必须非空且不能重复")
        if any(not 0 <= seed <= 2**32 - 1 for seed in seeds) or not 1 <= args.snapshots <= 100:
            raise ValueError("种子需在0到4294967295之间，快照数需在1到100之间")
    except ValueError as exc:
        parser.error(str(exc))
    output = args.output or ROOT / "outputs" / "experiments" / (datetime.now().strftime("%Y-%m-%d_%H%M%S") + "-memory-stress")
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "protocol.json", {"version": 2, "authorization": "synthetic_only",
               "seeds": seeds, "snapshots": args.snapshots})
    source = Path(__file__).read_bytes()
    (output / "stress.py").write_bytes(source)
    memory_source = (ROOT / "long_term_memory.py").read_bytes()
    (output / "long_term_memory.py").write_bytes(memory_source)
    cases = [run_case(output, seed, args.snapshots) for seed in seeds]
    report = {"version": 2, "authorization": "synthetic_only", "seeds": seeds, "snapshots": args.snapshots,
              "code_sha256": hashlib.sha256(source).hexdigest(), "memory_code_sha256": hashlib.sha256(memory_source).hexdigest(),
              "cases": cases, "passed": all(case["passed"] for case in cases),
              "interpretation": "离线压力验收只验证记忆存储、更新、重启、边界和项目隔离，不代表模型回答质量。"}
    write_json(output / "report.json", report)
    print(output)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
