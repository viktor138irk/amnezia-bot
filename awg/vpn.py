"""Операции с клиентами VPN: создание, удаление, выдача подписок.

Все блокирующие вызовы (docker, скрипты, DNS) выполняются в отдельном потоке,
чтобы не блокировать цикл событий бота.
"""
import asyncio
import logging
import shutil
import uuid
from datetime import datetime, timedelta

import pytz

import awg_decode
import config
import db

logger = logging.getLogger(__name__)


def client_conf_path(username):
    """Путь к файлу конфигурации клиента."""
    return config.USERS_DIR / username / f'{username}.conf'


def _encode_conf(conf_text):
    return awg_decode.encode(awg_decode.process_conf_data(conf_text))


async def generate_vpn_key(conf_path):
    """Преобразует конфиг клиента в ссылку vpn:// для приложения AmneziaVPN."""
    try:
        conf_text = conf_path.read_text(encoding='utf-8')
        # process_conf_data резолвит DNS, поэтому выносим в поток.
        return await asyncio.to_thread(_encode_conf, conf_text)
    except Exception as e:
        logger.error(f"Ошибка генерации vpn:// для {conf_path}: {e}")
        return ''


def make_username(telegram_id):
    """Генерирует уникальное имя клиента для пользователя Telegram."""
    return f'user_{telegram_id}_{uuid.uuid4().hex[:8]}'


async def create_client(username, settings):
    """Создаёт клиента WireGuard."""
    return await asyncio.to_thread(
        db.root_add, username, settings.endpoint,
        settings.wg_config_file, settings.docker_container,
    )


async def delete_client(username, settings):
    """Удаляет клиента WireGuard и все связанные с ним данные."""
    removed = await asyncio.to_thread(
        db.deactive_user_db, username,
        settings.wg_config_file, settings.docker_container,
    )
    if not removed:
        return False
    shutil.rmtree(config.USERS_DIR / username, ignore_errors=True)
    db.remove_user_expiration(username)
    db.remove_user_telegram_id(username)
    return True


async def active_clients(settings):
    """Возвращает {имя клиента: время последнего handshake}."""
    return await asyncio.to_thread(
        db.get_active_list, settings.wg_config_file, settings.docker_container,
    )


def expiration_for(period, since=None):
    """Вычисляет дату окончания подписки для периода."""
    months = config.PERIODS.get(period, 1)
    return (since or datetime.now(pytz.utc)) + timedelta(days=30 * months)


async def issue_subscription(telegram_id, period, settings):
    """Создаёт клиента и выдаёт ему подписку. Возвращает имя клиента или None."""
    username = make_username(telegram_id)
    if not await create_client(username, settings):
        return None
    db.set_user_expiration(username, expiration_for(period))
    db.set_user_telegram_id(username, telegram_id)
    return username


def extend_subscription(username, period):
    """Продлевает подписку клиента, отсчитывая от текущей даты окончания.

    Если подписка ещё активна, время не сгорает — новый период добавляется
    к остатку.
    """
    current = db.get_user_expiration(username)
    now = datetime.now(pytz.utc)
    since = current if current and current > now else now
    expiration = expiration_for(period, since=since)
    db.set_user_expiration(username, expiration)
    return expiration
