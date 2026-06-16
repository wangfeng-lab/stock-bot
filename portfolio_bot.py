"""
portfolio_bot.py — 主入口

本文件只负责：
  1. 组装后台线程
  2. 启动 bucket runner
  3. 阻塞主进程

业务逻辑已拆分到：
  shared_state.py
  workers.py
  bucket_runner.py
  entry_engine.py
  position_manager.py
  order_executor.py
"""

from __future__ import annotations

import time

from bucket_runner import run_bucket
from workers import build_runtime_threads, print_runtime_banner


def main():
    threads = build_runtime_threads(run_bucket)
    for thread in threads:
        thread.start()

    print_runtime_banner()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n所有策略已停止")


if __name__ == '__main__':
    main()
