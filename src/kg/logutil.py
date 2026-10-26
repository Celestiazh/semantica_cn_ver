# -*- coding: utf-8 -*-
"""输出与日志：把本机几个已知的坑一次性挡在这里。

已知本机坑（都实测踩过）：
  - Windows 控制台是 GBK，print emoji / 生僻字会 UnicodeEncodeError 直接崩
  - 只放宽 errors、**不要改 encoding** —— 改成 utf-8 会让 PowerShell 抓到的日志整段乱码
  - PowerShell 工具不回显 stdout → 报告一律**自己写 UTF-8 文件**，别指望管道
"""
import logging
import sys
from pathlib import Path


def setup_console() -> None:
    """把 stdout / stderr 的编码错误降级为替换，避免 GBK 控制台崩在 print 上。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def get_logger(name: str = "kg") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


def write_text(path, text: str) -> Path:
    """写 UTF-8 文本（父目录自动创建）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_report(path, lines) -> Path:
    """写 UTF-8 报告文件（列表逐行）。"""
    return write_text(path, "\n".join(str(x) for x in lines) + "\n")
