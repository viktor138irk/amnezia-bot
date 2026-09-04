"""Хранилище данных бота (JSON-файлы) и операции с клиентами WireGuard."""
import json
import logging
import re
import subprocess
from datetime import datetime

import pytz

import config

logger = logging.getLogger(__name__)


def load_json(file_path, default=None):
    """Загружает JSON-файл, возвращает default при ошибке или отсутствии файла."""
    try:
        if file_path.exists():
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Ошибка загрузки {file_path}: {e}")
    return default if default is not None else {}


def save_json(file_path, data):
    """Сохраняет данные в JSON-файл (атомарно, через временный файл)."""
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = file_path.with_suffix(file_path.suffix + '.tmp')
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False, default=str)
        tmp_path.replace(file_path)
        return True
    except Exception as e:
        logger.error(f"Ошибка сохранения {file_path}: {e}")
        return False


# --- Конфигурация -----------------------------------------------------------

def get_config():
    """Возвращает конфигурацию из config.json."""
    return load_json(config.CONFIG_FILE, {})


def save_config(cfg):
    """Сохраняет конфигурацию в config.json."""
    return save_json(config.CONFIG_FILE, cfg)


def add_admin(admin_id):
    """Добавляет ID администратора в конфигурацию."""
    cfg = get_config()
    admin_ids = [str(a) for a in cfg.get('admin_ids', [])]
    if str(admin_id) not in admin_ids:
        admin_ids.append(str(admin_id))
        cfg['admin_ids'] = admin_ids
        save_config(cfg)


def remove_admin(admin_id):
    """Удаляет ID администратора из конфигурации."""
    cfg = get_config()
    admin_ids = [str(a) for a in cfg.get('admin_ids', [])]
    if str(admin_id) in admin_ids:
        admin_ids.remove(str(admin_id))
        cfg['admin_ids'] = admin_ids
        save_config(cfg)


def set_pricing(period, price):
    """Устанавливает цену для указанного периода подписки."""
    cfg = get_config()
    pricing = cfg.get('pricing', dict(config.DEFAULT_PRICING))
    pricing[period] = price
    cfg['pricing'] = pricing
    save_config(cfg)


# --- Клиенты WireGuard ------------------------------------------------------

def _client_conf_path(name):
    return config.USERS_DIR / name / f'{name}.conf'


def get_client_public_key(name, docker_container):
    """Возвращает публичный ключ клиента.

    Ключ выводится из приватного ключа в конфиге клиента; если конфига нет,
    используется clientsTable, который ведёт newclient.sh.
    """
    conf_path = _client_conf_path(name)
    if conf_path.exists():
        try:
            conf = conf_path.read_text(encoding='utf-8')
            match = re.search(r'^PrivateKey\s*=\s*(\S+)', conf, re.MULTILINE)
            if match:
                result = subprocess.run(
                    ['docker', 'exec', '-i', docker_container, 'wg', 'pubkey'],
                    input=match.group(1), capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
                logger.error(f"wg pubkey для {name} завершился с ошибкой: {result.stderr.strip()}")
        except Exception as e:
            logger.error(f"Не удалось вычислить публичный ключ для {name}: {e}")

    for entry in load_json(config.CLIENTS_TABLE_FILE, []):
        if entry.get('userData', {}).get('clientName') == name:
            return entry.get('clientId')
    return None


def root_add(name, endpoint, wg_config_file, docker_container):
    """Создаёт нового клиента через newclient.sh."""
    try:
        process = subprocess.run(
            [str(config.NEWCLIENT_SCRIPT), name, endpoint, wg_config_file, docker_container],
            capture_output=True, text=True, cwd=str(config.BASE_DIR), timeout=120,
        )
        if process.returncode == 0:
            return True
        logger.error(f"Ошибка добавления клиента {name}: {process.stderr.strip() or process.stdout.strip()}")
        return False
    except Exception as e:
        logger.error(f"Исключение при добавлении клиента {name}: {e}")
        return False


def deactive_user_db(name, wg_config_file, docker_container):
    """Удаляет клиента через removeclient.sh."""
    public_key = get_client_public_key(name, docker_container)
    if not public_key:
        logger.error(f"Не найден публичный ключ клиента {name} — удаление невозможно.")
        return False
    try:
        process = subprocess.run(
            [str(config.REMOVECLIENT_SCRIPT), name, public_key, wg_config_file, docker_container],
            capture_output=True, text=True, cwd=str(config.BASE_DIR), timeout=120,
        )
        if process.returncode == 0:
            return True
        logger.error(f"Ошибка удаления клиента {name}: {process.stderr.strip() or process.stdout.strip()}")
        return False
    except Exception as e:
        logger.error(f"Исключение при удалении клиента {name}: {e}")
        return False


def get_client_list():
    """Возвращает список клиентов в виде пар (имя, конфигурация)."""
    clients = []
    if not config.USERS_DIR.exists():
        return clients
    for user_dir in sorted(config.USERS_DIR.iterdir()):
        if not user_dir.is_dir():
            continue
        conf_file = user_dir / f'{user_dir.name}.conf'
        if conf_file.exists():
            clients.append((user_dir.name, conf_file.read_text(encoding='utf-8')))
    return clients


def get_active_list(wg_config_file, docker_container):
    """Возвращает {имя клиента: время последнего handshake (UTC) или None}.

    Данные берутся из `wg show latest-handshakes` внутри контейнера Amnezia.
    """
    interface = wg_config_file.rsplit('/', 1)[-1].rsplit('.', 1)[0]
    handshakes = {}
    try:
        result = subprocess.run(
            ['docker', 'exec', '-i', docker_container, 'wg', 'show', interface, 'latest-handshakes'],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            logger.error(f"wg show завершился с ошибкой: {result.stderr.strip()}")
            return {}
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            timestamp = int(parts[1])
            handshakes[parts[0]] = (
                datetime.fromtimestamp(timestamp, pytz.utc) if timestamp > 0 else None
            )
    except Exception as e:
        logger.error(f"Не удалось получить статусы клиентов: {e}")
        return {}

    active = {}
    for entry in load_json(config.CLIENTS_TABLE_FILE, []):
        name = entry.get('userData', {}).get('clientName')
        if name and entry.get('clientId') in handshakes:
            active[name] = handshakes[entry['clientId']]
    return active


# --- Подписки ---------------------------------------------------------------

def set_user_expiration(username, expiration, transfer_limit='Неограниченно'):
    """Устанавливает срок действия подписки пользователя."""
    data = load_json(config.USER_EXPIRATION_FILE, {})
    data[username] = {
        'expiration': expiration.isoformat() if expiration else None,
        'transfer_limit': transfer_limit,
    }
    save_json(config.USER_EXPIRATION_FILE, data)


def get_user_expiration(username):
    """Возвращает срок действия подписки пользователя (aware datetime в UTC)."""
    data = load_json(config.USER_EXPIRATION_FILE, {})
    expiration = data.get(username, {}).get('expiration')
    if not expiration:
        return None
    try:
        parsed = datetime.fromisoformat(expiration)
    except ValueError:
        logger.error(f"Некорректная дата окончания подписки у {username}: {expiration}")
        return None
    return parsed if parsed.tzinfo else pytz.utc.localize(parsed)


def get_all_expirations():
    """Возвращает {имя клиента: дата окончания подписки} для всех клиентов."""
    data = load_json(config.USER_EXPIRATION_FILE, {})
    return {username: get_user_expiration(username) for username in data}


def remove_user_expiration(username):
    """Удаляет информацию о сроке действия подписки пользователя."""
    data = load_json(config.USER_EXPIRATION_FILE, {})
    if username in data:
        del data[username]
        save_json(config.USER_EXPIRATION_FILE, data)


def set_user_telegram_id(username, telegram_id):
    """Связывает имя клиента с Telegram ID."""
    data = load_json(config.USER_TELEGRAM_FILE, {})
    data[username] = telegram_id
    save_json(config.USER_TELEGRAM_FILE, data)


def get_user_telegram_id(username):
    """Возвращает Telegram ID владельца клиента."""
    return load_json(config.USER_TELEGRAM_FILE, {}).get(username)


def remove_user_telegram_id(username):
    """Удаляет связь клиента с Telegram ID."""
    data = load_json(config.USER_TELEGRAM_FILE, {})
    if username in data:
        del data[username]
        save_json(config.USER_TELEGRAM_FILE, data)


def get_usernames_by_telegram_id(telegram_id):
    """Возвращает имена клиентов, принадлежащих пользователю Telegram."""
    data = load_json(config.USER_TELEGRAM_FILE, {})
    return [name for name, owner in data.items() if owner == telegram_id]


# --- Промокоды --------------------------------------------------------------

def add_promocode(code, discount, expires_at, max_uses, subscription_period):
    """Добавляет новый промокод."""
    promocodes = load_json(config.PROMOCODES_FILE, {})
    if code in promocodes:
        return False
    promocodes[code] = {
        'discount': discount,
        'expires_at': expires_at.isoformat() if expires_at else None,
        'max_uses': max_uses,
        'uses': 0,
        'subscription_period': subscription_period,
    }
    save_json(config.PROMOCODES_FILE, promocodes)
    return True


def validate_promocode(code):
    """Проверяет промокод, НЕ расходуя использование.

    Возвращает {'discount': ..., 'subscription_period': ...} или None.
    Счётчик использований увеличивает только consume_promocode().
    """
    promo = load_json(config.PROMOCODES_FILE, {}).get(code)
    if not promo:
        return None
    if promo['expires_at'] and datetime.fromisoformat(promo['expires_at']) < datetime.now(pytz.utc):
        return None
    if promo['max_uses'] is not None and promo['uses'] >= promo['max_uses']:
        return None
    return {
        'discount': promo['discount'],
        'subscription_period': promo['subscription_period'],
    }


def consume_promocode(code):
    """Расходует одно использование промокода после успешной выдачи ключа."""
    promocodes = load_json(config.PROMOCODES_FILE, {})
    promo = promocodes.get(code)
    if not promo:
        return False
    promo['uses'] += 1
    save_json(config.PROMOCODES_FILE, promocodes)
    return True


def get_promocodes():
    """Возвращает все промокоды с разобранными датами."""
    result = {}
    for code, info in load_json(config.PROMOCODES_FILE, {}).items():
        result[code] = {
            'discount': info['discount'],
            'expires_at': datetime.fromisoformat(info['expires_at']) if info['expires_at'] else None,
            'max_uses': info['max_uses'],
            'uses': info['uses'],
            'subscription_period': info['subscription_period'],
        }
    return result


def remove_promocode(code):
    """Удаляет промокод."""
    promocodes = load_json(config.PROMOCODES_FILE, {})
    if code in promocodes:
        del promocodes[code]
        save_json(config.PROMOCODES_FILE, promocodes)
        return True
    return False


# --- Платежи ----------------------------------------------------------------

def record_payment(charge_id, telegram_id, username, period, amount, currency, promocode=None):
    """Сохраняет успешный платёж. Возвращает False, если платёж уже записан.

    Telegram может доставить successful_payment повторно, поэтому запись
    идемпотентна по идентификатору платежа провайдера.
    """
    payments = load_json(config.PAYMENTS_FILE, {})
    if charge_id in payments:
        return False
    payments[charge_id] = {
        'telegram_id': telegram_id,
        'username': username,
        'period': period,
        'amount': amount,
        'currency': currency,
        'promocode': promocode,
        'paid_at': datetime.now(pytz.utc).isoformat(),
    }
    save_json(config.PAYMENTS_FILE, payments)
    return True


def is_payment_recorded(charge_id):
    """Проверяет, обработан ли уже платёж с таким идентификатором."""
    return charge_id in load_json(config.PAYMENTS_FILE, {})


def get_payments():
    """Возвращает все записанные платежи."""
    return load_json(config.PAYMENTS_FILE, {})
