"""从已有运行记录和 JTL 手动重建离线 HTML 巡检报告。"""

from pathlib import Path
import sys

from jmeter_suite.manual_report import run_manual_report_application


PROJECT_ROOT = Path(__file__).resolve().parent


def main() -> int:
    if sys.argv[1:]:
        print("启动失败：该脚本不接受参数", file=sys.stderr)
        return 2
    return run_manual_report_application(PROJECT_ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
