"""Приём оплаты через встроенные платежи Telegram (провайдер — ЮKassa).

Токен провайдера выдаёт @BotFather (Payments -> ЮKassa) и кладётся в
files/config.json как payment_provider_token либо в переменную окружения
AWG_PAYMENT_PROVIDER_TOKEN. Никакой публичный webhook при этом не нужен:
Telegram сам подтверждает оплату апдейтами pre_checkout_query и
successful_payment.
"""
import json
import logging

from aiogram.types import LabeledPrice

import config

logger = logging.getLogger(__name__)

PAYLOAD_PREFIX = 'sub'
NO_PROMOCODE = '-'


def to_minor_units(amount):
    """Переводит рубли в копейки — Telegram принимает только целые единицы."""
    return int(round(float(amount) * 100))


def apply_discount(price, discount):
    """Применяет скидку в процентах, не опускаясь ниже нуля."""
    discounted = float(price) * (1 - float(discount or 0) / 100)
    return max(discounted, 0.0)


def final_price(settings, period, discount=0.0):
    """Итоговая цена периода подписки с учётом скидки."""
    return apply_discount(settings.price(period), discount)


def build_payload(period, promocode=None):
    """Собирает payload счёта (Telegram ограничивает его 128 байтами)."""
    return f'{PAYLOAD_PREFIX}:{period}:{promocode or NO_PROMOCODE}'


def parse_payload(payload):
    """Разбирает payload счёта. Возвращает (период, промокод) или (None, None)."""
    parts = (payload or '').split(':')
    if len(parts) != 3 or parts[0] != PAYLOAD_PREFIX:
        return None, None
    period = parts[1] if parts[1] in config.PERIODS else None
    promocode = None if parts[2] == NO_PROMOCODE else parts[2]
    return period, promocode


def _receipt_provider_data(settings, price, description):
    """Чек для 54-ФЗ, если он включён в настройках."""
    receipt = getattr(settings, 'receipt', None)
    if not receipt or not receipt.get('enabled'):
        return None
    return {
        'receipt': {
            'items': [{
                'description': description[:128],
                'quantity': '1.00',
                'amount': {'value': f'{price:.2f}', 'currency': settings.currency},
                'vat_code': receipt.get('vat_code', 1),
            }],
            'tax_system_code': receipt.get('tax_system_code', 1),
        }
    }


def build_invoice(settings, period, discount=0.0, promocode=None):
    """Готовит именованные аргументы для bot.send_invoice."""
    price = final_price(settings, period, discount)
    label = config.period_label(period)
    title = f'VPN-ключ на {label}'
    description = f'Доступ к AmneziaVPN на {label}.'
    if discount:
        description += f' Промокод {promocode}: скидка {discount:g}%.'

    invoice = {
        'title': title,
        'description': description,
        'payload': build_payload(period, promocode),
        'provider_token': settings.provider_token,
        'currency': settings.currency,
        'prices': [LabeledPrice(label=label, amount=to_minor_units(price))],
    }

    provider_data = _receipt_provider_data(settings, price, title)
    if provider_data:
        invoice['provider_data'] = json.dumps(provider_data, ensure_ascii=False)
        invoice['need_email'] = True
        invoice['send_email_to_provider'] = True
    return invoice
