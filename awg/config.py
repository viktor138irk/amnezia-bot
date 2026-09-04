"""Пути и настройки бота.

Все пути вычисляются относительно расположения этого файла, поэтому бот
не привязан к конкретному каталогу установки и не зависит от текущего cwd.
Каталог можно переопределить переменной окружения AWG_BOT_HOME.
"""
import os
import re
import sys
from pathlib import Path

BASE_DIR = Path(os.environ.get('AWG_BOT_HOME') or Path(__file__).resolve().parent)

FILES_DIR = BASE_DIR / 'files'
USERS_DIR = BASE_DIR / 'users'

CONFIG_FILE = FILES_DIR / 'config.json'
USER_EXPIRATION_FILE = FILES_DIR / 'user_expiration.json'
USER_TELEGRAM_FILE = FILES_DIR / 'user_telegram.json'
PROMOCODES_FILE = FILES_DIR / 'promocodes.json'
PAYMENTS_FILE = FILES_DIR / 'payments.json'
SERVER_CONF_FILE = FILES_DIR / 'server.conf'
CLIENTS_TABLE_FILE = FILES_DIR / 'clientsTable'

NEWCLIENT_SCRIPT = BASE_DIR / 'newclient.sh'
REMOVECLIENT_SCRIPT = BASE_DIR / 'removeclient.sh'

# Интерпретатор, которым запущен бот, — не захардкоженная версия Python.
PYTHON_EXECUTABLE = sys.executable or 'python3'

# Имя клиента должно быть безопасным для файловой системы и для newclient.sh.
USERNAME_RE = re.compile(r'^[a-zA-Z0-9_-]{1,32}$')

DEFAULT_PRICING = {
    '1_month': 1000.0,
    '3_months': 2500.0,
    '6_months': 4500.0,
    '12_months': 8000.0,
}

# Периоды подписки и их длительность в месяцах.
PERIODS = {
    '1_month': 1,
    '3_months': 3,
    '6_months': 6,
    '12_months': 12,
}

PERIOD_LABELS = {
    '1_month': '1 месяц',
    '3_months': '3 месяца',
    '6_months': '6 месяцев',
    '12_months': '12 месяцев',
}


def period_label(period):
    """Человекочитаемое название периода подписки."""
    return PERIOD_LABELS.get(period, str(period).replace('_', ' '))


def ensure_dirs():
    """Создаёт рабочие каталоги, если их ещё нет."""
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    USERS_DIR.mkdir(parents=True, exist_ok=True)
