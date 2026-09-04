"""Загрузка и валидация настроек бота.

Значения берутся из files/config.json, любое из них можно переопределить
переменной окружения — так секреты (токен бота, токен платёжного провайдера)
не обязаны лежать в репозитории.
"""
import logging
from dataclasses import dataclass, field
import os

import config
import db

logger = logging.getLogger(__name__)

# Telegram не принимает счета меньше этой суммы в рублях.
MIN_INVOICE_RUB = 60.0


def _env_or(cfg, key, env_name):
    value = os.environ.get(env_name)
    if value:
        return value
    return cfg.get(key)


def _as_id_set(raw):
    result = set()
    for item in raw or []:
        try:
            result.add(int(item))
        except (TypeError, ValueError):
            logger.warning(f"Некорректный Telegram ID в конфигурации: {item!r} — пропущен.")
    return result


@dataclass
class Settings:
    bot_token: str
    wg_config_file: str
    docker_container: str
    endpoint: str
    admin_ids: set = field(default_factory=set)
    moderator_ids: set = field(default_factory=set)
    pricing: dict = field(default_factory=lambda: dict(config.DEFAULT_PRICING))
    provider_token: str = ''
    currency: str = 'RUB'
    receipt: dict = field(default_factory=dict)

    @property
    def payments_enabled(self):
        """Оплата доступна, только если задан токен платёжного провайдера."""
        return bool(self.provider_token)

    def price(self, period):
        """Цена периода подписки с запасным значением по умолчанию."""
        return float(self.pricing.get(period, config.DEFAULT_PRICING.get(period, 0.0)))

    def is_admin(self, user_id):
        return user_id in self.admin_ids

    def is_staff(self, user_id):
        return user_id in self.admin_ids or user_id in self.moderator_ids


def load():
    """Читает настройки, дополняет их окружением и проверяет обязательные поля."""
    cfg = db.get_config()

    settings = Settings(
        bot_token=_env_or(cfg, 'bot_token', 'AWG_BOT_TOKEN') or '',
        wg_config_file=_env_or(cfg, 'wg_config_file', 'AWG_WG_CONFIG_FILE') or '',
        docker_container=_env_or(cfg, 'docker_container', 'AWG_DOCKER_CONTAINER') or '',
        endpoint=_env_or(cfg, 'endpoint', 'AWG_ENDPOINT') or '',
        admin_ids=_as_id_set(cfg.get('admin_ids')),
        moderator_ids=_as_id_set(cfg.get('moderator_ids')),
        pricing={**config.DEFAULT_PRICING, **(cfg.get('pricing') or {})},
        provider_token=_env_or(cfg, 'payment_provider_token', 'AWG_PAYMENT_PROVIDER_TOKEN') or '',
        currency=_env_or(cfg, 'payment_currency', 'AWG_PAYMENT_CURRENCY') or 'RUB',
        receipt=cfg.get('payment_receipt') or {},
    )

    missing = [
        name for name, value in (
            ('bot_token', settings.bot_token),
            ('wg_config_file', settings.wg_config_file),
            ('docker_container', settings.docker_container),
            ('endpoint', settings.endpoint),
        ) if not value
    ]
    if missing:
        raise ValueError(
            f"В {config.CONFIG_FILE} не заданы обязательные настройки: {', '.join(missing)}."
        )
    if not settings.admin_ids:
        raise ValueError(f"В {config.CONFIG_FILE} не задан ни один admin_ids.")

    if not settings.payments_enabled:
        logger.warning(
            "payment_provider_token не задан — оплата отключена, ключи выдаются только "
            "по промокодам. Получите токен у @BotFather (Payments -> ЮKassa)."
        )
    return settings
