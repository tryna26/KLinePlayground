"""Development launcher; configure storage before importing the application."""

import argparse
import os


def main():
    parser = argparse.ArgumentParser(description="KLinePlayground 开发启动入口")
    parser.add_argument("--home", help="运行根目录（默认 .runtime）")
    parser.add_argument("--data-dir", help="独立行情目录")
    parser.add_argument("--users-dir", help="独立用户目录")
    parser.add_argument("--port", type=int, default=5050, help="Web 端口，默认 5050")
    parser.add_argument("--desktop", action="store_true", help="打开 PyWebView 桌面窗口（自动选择端口）")
    parser.add_argument("--portfolio-demo", action="store_true", help="显式启用组合合成行情验收接口（非真实历史）")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1 至 65535 之间")
    if args.portfolio_demo:
        os.environ["KLINE_TRAINER_PORTFOLIO_DEMO"] = "1"
    for value, name in ((args.home, "KLINE_TRAINER_HOME"),
                        (args.data_dir, "KLINE_TRAINER_DATA_DIR"),
                        (args.users_dir, "KLINE_TRAINER_USERS_DIR")):
        if value is not None:
            os.environ[name] = value

    from backend.runtime_paths import get_runtime_paths
    paths = get_runtime_paths()
    print(f"行情目录: {paths.data}\n用户目录: {paths.users}", flush=True)
    if args.desktop:
        from webview_app.main_pywebview import main as desktop_main
        desktop_main([])
    else:
        from backend.app_enhanced import app
        app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
