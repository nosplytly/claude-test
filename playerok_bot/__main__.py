"""Запуск: python -m playerok_bot [-c config.toml] [--check]."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .bot import BotError, check, run
from .config import ConfigError, load_config
from .storage import StateError


def main() -> int:
    # Консоль Windows может не уметь в эмодзи — лучше «?», чем падение.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(
        prog="playerok_bot", description="Бот автоматизации продаж на Playerok"
    )
    parser.add_argument("-c", "--config", default="config.toml", help="путь к файлу настроек")
    parser.add_argument(
        "--check", action="store_true", help="проверить настройки, токен, склад и Telegram"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Ошибка настроек: {exc}", file=sys.stderr)
        return 1

    _setup_logging(config.log_file, verbose=args.verbose)
    try:
        if args.check:
            return 0 if asyncio.run(check(config)) else 1
        asyncio.run(run(config))
    except (BotError, StateError) as exc:
        logging.getLogger("playerok_bot").error("%s", exc)
        return 1
    except KeyboardInterrupt:
        logging.getLogger("playerok_bot").info("Остановлено")
    return 0


def _setup_logging(log_file: Path | None, *, verbose: bool) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        )
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )
    # Сетевые библиотеки на INFO пишут каждый запрос — это шум.
    for name in ("httpx", "httpcore", "websockets"):
        logging.getLogger(name).setLevel(logging.WARNING)


if __name__ == "__main__":
    sys.exit(main())
