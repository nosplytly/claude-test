"""Запуск: python -m supplierhub [--workdir PATH] [--browser] [--no-open] [--port N] [--debug]."""

import sys

# Абсолютный импорт: этот файл же служит точкой входа для сборки в exe,
# где он запускается как скрипт, а не как часть пакета.
from supplierhub.app import main

if __name__ == "__main__":
    sys.exit(main())
