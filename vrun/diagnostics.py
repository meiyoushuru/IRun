# SPDX-License-Identifier: MPL-2.0
"""Export bounded excerpts from IRun diagnostic logs."""
from datetime import datetime
import os
from pathlib import Path
import platform
import re

from . import __version__
from .drivers import apple_usb_device_status, apple_usb_status

MAX_LOG_BYTES = 256 * 1024
PRIVATE_PATTERNS = (
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S),
     "[已隐藏私钥]"),
    (re.compile(r"(?i)(\bBearer\s+)[^\s,;'\"]+"), r"\1[已隐藏]"),
    (re.compile(r"(?i)(\b(?:api[_-]?key|tianditu_key|access[_-]?token|token|password|tk)\b['\"]?\s*[:=]\s*['\"]?)[^\s&,'\";}]+"),
     r"\1[已隐藏]"),
    (re.compile(r"(?i)([A-Z]:[/\\]+Users[/\\]+)[^/\\\r\n'\"<>|]+(?=[/\\]|$)"), r"\1[已隐藏]"),
    (re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{8}-[0-9a-f]{16}|[0-9a-f]{40})(?![0-9a-f])"), "[已隐藏设备标识]"),
    (re.compile(r"(?i)(\b(?:lat(?:itude)?|lng|lon(?:gitude)?)\b['\"]?\s*[:=]\s*['\"]?)[+-]?\d+(?:\.\d+)?(?:e[+-]?\d+)?"), r"\1[已隐藏]"),
    (re.compile(r"(?<![\d.])[+-]?\d{1,3}\.\d{4,}\s*[,，]\s*[+-]?\d{1,3}\.\d{4,}(?![\d.])"), "[已隐藏坐标]"),
)


def redact(text, private_values=()):
    for value in sorted(set(filter(None, private_values)), key=len, reverse=True):
        text = re.sub(re.escape(value), "[已隐藏]", text, flags=re.I)
    for pattern, replacement in PRIVATE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


ERROR_CODES = re.compile(
    r"\bSavePairRecord\s+\d+\b|\bE\d{5,}\b|"
    r"(?:\b(?:WinError|Errno|HTTP|return\s*code|error\s*code|code|status(?:_code)?|Number)\b|错误码|报错码|退出码)"
    r"[\"']?\s*[:=]?\s*[\"']?(?:0x[\da-f]+|-?\d+)\b|\b0x[\da-f]{4,}\b",
    re.IGNORECASE,
)


def error_summary(message, detail="", *, private_values=()):
    message = " ".join(redact(str(message), private_values).split())
    detail = redact(str(detail), private_values)
    codes = list(dict.fromkeys(match[0] for match in ERROR_CODES.finditer(message + "\n" + detail)))
    summary = message if len(message) <= 190 else message[:187] + "…"
    missing = [code for code in codes if code not in summary]
    if missing:
        summary += "（" + "，".join(missing[:4]) + "）"
    first_detail = next((line.strip() for line in detail.splitlines() if line.strip()), "")
    if first_detail and first_detail.casefold() not in message.casefold() and len(summary) < 200:
        excerpt = first_detail if len(first_detail) <= 95 else first_detail[:92] + "…"
        summary += " · " + excerpt
    return summary


def _tail(path):
    with path.open("rb") as source:
        size = source.seek(0, os.SEEK_END)
        start = max(0, size - MAX_LOG_BYTES)
        source.seek(start)
        data = source.read(MAX_LOG_BYTES)
    if start and b"\n" in data:
        data = data.partition(b"\n")[2]
    text = data.decode("utf-8-sig", errors="replace").strip()
    if not text:
        return "暂无日志记录。"
    return ("[仅保留最近的日志]\n" if start else "") + text


def export_report(destination: Path, user_log_dir: Path, *, status="", device=None, last_error=""):
    private = []
    sections = [
        "IRun 诊断日志",
        f"导出时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"IRun 版本：{__version__}",
        f"电脑系统：{platform.system()} {platform.version()} ({platform.machine()})",
    ]
    if device is not None:
        sections.append(f"设备：{device.product_type or '型号未知'} · {device.system_name} {device.version}")
        private.append(device.udid)
        if device.name.casefold() not in {"iphone", "ipad"}:
            private.append(device.name)
    sections.extend(("", "【当前状态】", status or "无", "", "【最近一次报错】", last_error or "无",
                     "", "【Apple USB 自动检查】", apple_usb_status(), apple_usb_device_status()))
    sources = [
        ("应用日志", user_log_dir / "irun.log"),
        ("设备连接日志", user_log_dir / "ios17-helper.log"),
    ]
    if program_data := os.environ.get("PROGRAMDATA"):
        sources.append(("Apple 驱动安装日志", Path(program_data) / "IRun" / "driver-install.log"))
    for title, path in sources:
        try:
            content = _tail(path)
        except FileNotFoundError:
            content = "暂无日志文件。"
        except OSError:
            content = "无法读取此日志。"
        sections.extend(("", f"【{title}】", content))
    destination.write_text(redact("\n".join(sections) + "\n", private), encoding="utf-8-sig")
