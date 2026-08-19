"""共通ロガー設定。

CLAUDE.md / docs/OPS.md §5 ガイドライン準拠:
- JST タイムスタンプ (ISO 8601)
- 7世代ローテーション
- ファイル + 標準出力の二重出力
- スタックトレース完全出力 (logger.exception 利用時)
- 機密情報のログ出力は呼出元の責任で禁止
"""

from __future__ import annotations

import logging
import logging.handlers
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

DEFAULT_LOG_DIR = Path("logs")
DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s:%(filename)s:%(lineno)d - %(message)s"
DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10MB / file
DEFAULT_BACKUP_COUNT = 7


class JSTFormatter(logging.Formatter):
    """JST タイムスタンプを ISO 8601 形式で出力する Formatter。"""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        dt = datetime.fromtimestamp(record.created, tz=JST)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat(timespec="seconds")


def setup_logger(
    name: str,
    *,
    log_dir: Path | str = DEFAULT_LOG_DIR,
    level: int = logging.INFO,
    fmt: str = DEFAULT_FORMAT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    to_stdout: bool = True,
) -> logging.Logger:
    """name 単位でロガーを設定する（既設定の場合は再利用）。

    Args:
        name: ロガー名 (兼ファイル名のbase) 例: "api" → logs/api.log
        log_dir: ログ出力先ディレクトリ
        level: ログレベル
        fmt: フォーマット文字列
        max_bytes: 1ファイルの最大サイズ
        backup_count: 保持する世代数 (`{name}.log.1` ... `.7`)
        to_stdout: 標準出力にも出すか
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(level)
    logger.propagate = False

    formatter = JSTFormatter(fmt=fmt)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / f"{name}.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if to_stdout:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    return logger
