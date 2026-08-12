"""使用固定项目配置向已绑定群发送钉钉连通性测试消息和文件。"""

from pathlib import Path
import sys

from jmeter_suite.dingtalk_diagnostic import run_dingtalk_diagnostic


CONFIG_PATH = Path(__file__).resolve().parent / "config" / "app.toml"


def main() -> int:
    if sys.argv[1:]:
        print(
            "启动失败：该脚本不接受参数，配置固定为 config/app.toml",
            file=sys.stderr,
        )
        return 2
    return run_dingtalk_diagnostic(CONFIG_PATH)


if __name__ == "__main__":
    raise SystemExit(main())
