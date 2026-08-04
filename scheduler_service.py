"""启动前台 Cron 调度服务，按配置周期执行 JMeter 巡检。"""

from pathlib import Path
import sys

from jmeter_suite.application import run_scheduler_application


CONFIG_PATH = Path(__file__).resolve().parent / "config" / "app.toml"


def main() -> int:
    if sys.argv[1:]:
        print(
            "启动失败：该脚本不接受参数，配置固定为 config/app.toml",
            file=sys.stderr,
        )
        return 2
    return run_scheduler_application(CONFIG_PATH)


if __name__ == "__main__":
    raise SystemExit(main())
