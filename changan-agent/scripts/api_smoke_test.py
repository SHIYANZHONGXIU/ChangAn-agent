"""可选真实端点检查；只请求模型列表，不发送收费聊天请求。"""

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from persistence import read_json
from secret_store import unprotect
from app_runtime import redact
from orchestrator import _NoRedirect


def main():
    config = read_json(ROOT / "agent_config.json", {})
    failed = False
    for prefix, name in (("gpt", "GPT"), ("deepseek", "DeepSeek"), ("claude", "Claude")):
        try:
            key = unprotect(config.get(prefix + "_key_enc", "")) or config.get(prefix + "_key", "")
            if not key:
                print(f"{name}: 未填写密钥，跳过")
                continue
            base = config.get(prefix + "_base_url", "").rstrip("/")
            request = urllib.request.Request(base + "/models", headers={"Authorization": "Bearer " + key})
            with urllib.request.build_opener(_NoRedirect).open(request, timeout=15) as response:
                data = json.loads(response.read(4_000_000).decode("utf-8"))
                models = [item["id"] for item in data.get("data", []) if isinstance(item.get("id"), str) and prefix in item["id"].lower()]
                print(f"{name}: HTTP {response.status}，识别 {len(models)} 个对应模型（不代表聊天已可用）")
        except Exception as exc:
            failed = True
            print(f"{name}: {redact(str(exc))}")
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
