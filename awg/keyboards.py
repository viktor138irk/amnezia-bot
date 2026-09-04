"""Инлайн-клавиатуры бота.

В callback_data используется двоеточие как разделитель: имена клиентов
состоят из [a-zA-Z0-9_-], поэтому двоеточие гарантированно не встречается
внутри значения и разбор не ломается на именах с подчёркиваниями.
"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config

HOME = InlineKeyboardButton(text='🏠 Домой', callback_data='home')


def _finish(builder, *rows):
    for row in rows:
        builder.row(*row)
    return builder.as_markup()


def main_menu(user_id, settings):
    """Главное меню, состав зависит от роли пользователя."""
    builder = InlineKeyboardBuilder()
    if settings.is_admin(user_id):
        builder.row(
            InlineKeyboardButton(text='➕ Добавить пользователя', callback_data='add_user'),
            InlineKeyboardButton(text='📋 Список клиентов', callback_data='list_users'),
        )
        builder.row(
            InlineKeyboardButton(text='🔑 Получить конфиг', callback_data='get_config'),
            InlineKeyboardButton(text='🎟️ Управление промокодами', callback_data='manage_promocodes'),
        )
        builder.row(
            InlineKeyboardButton(text='⚙️ Настройки', callback_data='settings'),
            HOME,
        )
    elif settings.is_staff(user_id):
        builder.row(
            InlineKeyboardButton(text='➕ Добавить пользователя', callback_data='add_user'),
            InlineKeyboardButton(text='📋 Список клиентов', callback_data='list_users'),
        )
        builder.row(
            InlineKeyboardButton(text='🔑 Получить конфиг', callback_data='get_config'),
            HOME,
        )
    else:
        if settings.payments_enabled:
            builder.row(InlineKeyboardButton(text='💳 Купить ключ', callback_data='buy_key'))
        builder.row(InlineKeyboardButton(text='🎟️ Получить ключ по промокоду', callback_data='use_promocode'))
        builder.row(InlineKeyboardButton(text='🔑 Мои ключи', callback_data='my_keys'))
    return builder.as_markup()


def buy_menu(settings, discount=0.0):
    """Выбор периода подписки с ценами (при наличии — со скидкой)."""
    builder = InlineKeyboardBuilder()
    for period in config.PERIODS:
        price = settings.price(period) * (1 - discount / 100)
        builder.row(InlineKeyboardButton(
            text=f'{config.period_label(period)} — {price:,.0f} ₽'.replace(',', ' '),
            callback_data=f'buy:{period}',
        ))
    builder.row(InlineKeyboardButton(text='🎟️ Ввести промокод', callback_data='use_promocode'))
    builder.row(HOME)
    return builder.as_markup()


def settings_menu():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text='💾 Создать бэкап', callback_data='create_backup'),
        InlineKeyboardButton(text='👥 Список админов', callback_data='list_admins'),
    )
    builder.row(
        InlineKeyboardButton(text='👤 Добавить админа', callback_data='add_admin'),
        InlineKeyboardButton(text='💰 Настройки цен', callback_data='pricing_settings'),
    )
    builder.row(InlineKeyboardButton(text='⬅️ Назад', callback_data='home'))
    return builder.as_markup()


def pricing_settings_menu(settings):
    builder = InlineKeyboardBuilder()
    for period in config.PERIODS:
        builder.row(InlineKeyboardButton(
            text=f'{config.period_label(period)}: {settings.price(period):,.0f} ₽'.replace(',', ' '),
            callback_data=f'setprice:{period}',
        ))
    builder.row(InlineKeyboardButton(text='⬅️ Назад', callback_data='settings'))
    return builder.as_markup()


def renewal_periods(username):
    builder = InlineKeyboardBuilder()
    for period in config.PERIODS:
        builder.add(InlineKeyboardButton(
            text=config.period_label(period),
            callback_data=f'renewp:{username}:{period}',
        ))
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text='📅 Указать дату', callback_data=f'renewp:{username}:custom'))
    builder.row(
        InlineKeyboardButton(text='⬅️ Назад', callback_data=f'client:{username}'),
        HOME,
    )
    return builder.as_markup()


def client_actions(username):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text='🗑️ Удалить', callback_data=f'delete:{username}'),
        InlineKeyboardButton(text='🔄 Продлить', callback_data=f'renew:{username}'),
    )
    builder.row(
        InlineKeyboardButton(text='🔑 Конфиг', callback_data=f'sendconf:{username}'),
    )
    builder.row(
        InlineKeyboardButton(text='⬅️ Назад', callback_data='list_users'),
        HOME,
    )
    return builder.as_markup()


def client_list(clients, callback_prefix, back_callback='home'):
    """Список клиентов; clients — пары (имя, префикс статуса)."""
    builder = InlineKeyboardBuilder()
    for username, status in clients:
        builder.add(InlineKeyboardButton(
            text=f'{status} {username}'.strip(),
            callback_data=f'{callback_prefix}:{username}',
        ))
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text='🏠 Домой', callback_data=back_callback))
    return builder.as_markup()


def admin_list(admin_ids):
    builder = InlineKeyboardBuilder()
    for admin_id in sorted(admin_ids):
        builder.add(InlineKeyboardButton(
            text=f'🗑️ Удалить {admin_id}',
            callback_data=f'rmadmin:{admin_id}',
        ))
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text='⬅️ Назад', callback_data='settings'))
    return builder.as_markup()


def promocodes_menu():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text='➕ Добавить промокод', callback_data='add_promocode'),
        InlineKeyboardButton(text='🗑️ Удалить промокод', callback_data='delete_promocode'),
    )
    builder.row(HOME)
    return builder.as_markup()


def promocode_list(codes):
    builder = InlineKeyboardBuilder()
    for code in codes:
        builder.add(InlineKeyboardButton(text=f'🗑️ {code}', callback_data=f'rmpromo:{code}'))
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text='⬅️ Назад', callback_data='manage_promocodes'))
    return builder.as_markup()


def single(text, callback_data):
    """Клавиатура из одной кнопки."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, callback_data=callback_data),
    ]])


def home_only():
    return InlineKeyboardMarkup(inline_keyboard=[[HOME]])
