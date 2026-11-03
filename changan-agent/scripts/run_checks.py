"""无真实 API 消费的本地回归检查，并输出可追溯报告。"""

import io
import json
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    for path in ROOT.glob("*.py"):
        compile(path.read_bytes(), str(path), "exec")
    stream = io.StringIO()
    started = time.monotonic()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    text = stream.getvalue()
    folder = ROOT / "outputs" / "verification" / datetime.now().strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "tests.txt").write_text(text, encoding="utf-8")
    report = {"time": datetime.now().isoformat(timespec="seconds"), "python": sys.version.split()[0], "tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped), "seconds": round(time.monotonic() - started, 2), "real_api_requests": 0, "passed": result.wasSuccessful()}
    (folder / "checks.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(text)
    print(f"验证报告：{folder}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
