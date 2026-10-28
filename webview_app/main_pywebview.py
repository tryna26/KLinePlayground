"""Windows desktop entry and localhost diagnostics for the same packaged app."""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
VERSION = "4.0.0-rc.1"


def arguments(argv=None):
    parser = argparse.ArgumentParser(description="KLinePlayground 组合训练桌面版")
    parser.add_argument("--home", help="独立运行数据根目录")
    parser.add_argument("--data-dir", help="独立行情目录")
    parser.add_argument("--users-dir", help="独立用户目录")
    parser.add_argument("--serve-only", action="store_true", help="诊断模式：只启动本机服务")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--ready-file", help="写入本机端口和运行目录，供验收使用")
    parser.add_argument("--portfolio", action="store_true", help="打开组合训练入口")
    parser.add_argument("--version", action="version", version=VERSION)
    return parser.parse_args(argv)


def main(argv=None):
    args = arguments(argv)
    if not 0 <= args.port <= 65535:
        raise ValueError("端口必须在 0 至 65535 之间")
    for value, name in ((args.home,"KLINE_TRAINER_HOME"),(args.data_dir,"KLINE_TRAINER_DATA_DIR"),(args.users_dir,"KLINE_TRAINER_USERS_DIR")):
        if value is not None:
            os.environ[name] = value
    from backend.runtime_paths import get_runtime_paths
    paths = get_runtime_paths()
    log_dir = paths.home / "logs"
    log_dir.mkdir(parents=True,exist_ok=True)
    handler = RotatingFileHandler(log_dir / "desktop.log",maxBytes=2_000_000,backupCount=2,encoding="utf-8")
    logging.basicConfig(level=logging.INFO,handlers=[handler],force=True)
    for name in ("stdout","stderr"):
        if getattr(sys,name) is None:
            setattr(sys,name,(log_dir / "desktop-console.log").open("a",encoding="utf-8",buffering=1))
    server = None
    try:
        from backend.app_enhanced import app, portfolio_repository
        from werkzeug.serving import make_server
        server = make_server("127.0.0.1",args.port,app,threaded=True)
        port = server.server_port
        ready = {"version":VERSION,"url":f"http://127.0.0.1:{port}","home":str(paths.home),
                 "data":str(paths.data),"users":str(paths.users),"pid":os.getpid(),"frozen":bool(getattr(sys,"frozen",False))}
        if args.ready_file:
            target = Path(args.ready_file)
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text(json.dumps(ready,ensure_ascii=False),encoding="utf-8")
        logging.info("Desktop %s ready on localhost port %s",VERSION,port)
        if args.serve_only:
            server.serve_forever()
        else:
            import webview
            thread = threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            webview.create_window(title=f"KLinePlayground {VERSION} · 多股票历史模拟",
                url=ready["url"] + ("/portfolio.html" if args.portfolio else "/"),
                width=1400,height=900,min_size=(800,600),text_select=True)
            webview.start(gui="edgechromium",debug=False,private_mode=False,storage_path=str(paths.home / "webview"))
            server.shutdown()
            thread.join(timeout=5)
        portfolio_repository.close()
    except Exception:
        logging.exception("Desktop startup failed")
        if not args.serve_only and sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,
                "启动失败。请确认已安装 Microsoft Edge WebView2 Runtime。\n"
                f"诊断日志：{log_dir / 'desktop.log'}\n"
                "官方安装地址：https://developer.microsoft.com/microsoft-edge/webview2/",
                "KLinePlayground 启动提示",0x10)
        raise
    finally:
        if server is not None:
            server.server_close()


if __name__ == "__main__":
    main()
