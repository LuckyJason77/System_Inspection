"""从固定项目配置启动一次完整巡检，并将应用退出码返回给操作系统。"""

from pathlib import Path
import sys

from jmeter_suite.application import run_once_application


CONFIG_PATH = Path(__file__).resolve().parent / "config" / "app.toml"


def main() -> int:
    if sys.argv[1:]:
        print(
            "启动失败：该脚本不接受参数，配置固定为 config/app.toml",
            file=sys.stderr,
        )
        return 2
    return run_once_application(CONFIG_PATH)


if __name__ == "__main__":
    raise SystemExit(main())
