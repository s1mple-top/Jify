"""Jify Gateway - thin entry point. See gateway/ package."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gateway import app

if __name__ == "__main__":
    from cli.bootstrap import ensure_jify_home
    ensure_jify_home()

    import uvicorn, argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9090)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    args = parser.parse_args()
    print(f"\n  Jify Gateway 启动中...")
    print(f"  登录: http://localhost:{args.port}")
    print(f"  聊天: http://localhost:{args.port}/chat\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
