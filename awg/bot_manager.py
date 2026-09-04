"""Telegram-бот управления AmneziaVPN (aiogram 3)."""
import asyncio
import html
import logging
import os
import sys
import zipfile
from datetime import datetime, timedelta

import pytz
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, FSInputFile, Message, PreCheckoutQuery,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import db
import keyboards as kb
import payments
import settings as settings_module
import vpn

logging.basicConfig(
    level=os.environ.get('AWG_LOG_LEVEL', 'INFO'),
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger(__name__)

config.ensure_dirs()

try:
    SETTINGS = settings_module.load()
except ValueError as e:
    logger.error(str(e))
    sys.exit(1)

bot = Bot(SETTINGS.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# payment_router подключается первым: апдейт successful_payment приходит без
# текста и иначе его перехватил бы обработчик, ждущим ввода в FSM.
payment_router = Router()
router = Router()
dp.include_router(payment_router)
dp.include_router(router)
scheduler = AsyncIOScheduler(timezone=pytz.utc)

ONLINE_THRESHOLD_SECONDS = 180


class AdminCommandCleanupMiddleware(BaseMiddleware):
    """Удаляет команды администраторов из чата, чтобы не засорять переписку."""

    async def __call__(self, handler, event: Message, data):
        user = event.from_user
        if user and event.text and event.text.startswith('/') and SETTINGS.is_admin(user.id):
            asyncio.create_task(delete_message_later(event.chat.id, event.message_id))
        return await handler(event, data)


async def delete_message_later(chat_id, message_id, delay=2):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


dp.message.middleware(AdminCommandCleanupMiddleware())


class Form(StatesGroup):
    """Состояния диалогов, заменяют самодельный словарь состояний."""
    user_name = State()
    admin_id = State()
    promocode = State()
    new_promocode = State()
    price = State()
    custom_date = State()


# --- Вспомогательные функции ------------------------------------------------

def esc(value):
    """Экранирует текст для HTML-разметки."""
    return html.escape(str(value))


def chat_id_of(callback):
    """Чат, в котором показан экран. Для недоступного сообщения — личный чат."""
    message = callback.message
    return message.chat.id if message is not None else callback.from_user.id


async def show(event, text, markup=None):
    """Показывает экран: редактирует текущее сообщение или отправляет новое."""
    if not isinstance(event, CallbackQuery):
        return await event.answer(text, reply_markup=markup)

    message = event.message
    if message is not None:
        try:
            return await message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as e:
            if 'message is not modified' in str(e):
                return message
            # Сообщение не редактируется (документ, слишком старое) — шлём новое.
        except AttributeError:
            # InaccessibleMessage: содержимого нет, редактировать нечего.
            pass
    return await bot.send_message(chat_id_of(event), text, reply_markup=markup)


async def show_main_menu(event, state=None, text='Выберите действие:'):
    if state is not None:
        await state.clear()
    user_id = event.from_user.id
    return await show(event, text, kb.main_menu(user_id, SETTINGS))


async def deny(callback, text='Нет прав.'):
    await callback.answer(text, show_alert=True)


async def send_client_config(chat_id, username, header=None):
    """Отправляет пользователю .conf и ссылку vpn:// для AmneziaVPN."""
    conf_path = vpn.client_conf_path(username)
    if not conf_path.exists():
        await bot.send_message(chat_id, f'Конфигурация для <b>{esc(username)}</b> не найдена.')
        return False

    caption = header or f'Конфигурация для <b>{esc(username)}</b>'
    document = await bot.send_document(
        chat_id, FSInputFile(conf_path, filename=f'{username}.conf'), caption=caption,
    )
    try:
        await bot.pin_chat_message(chat_id, document.message_id, disable_notification=True)
    except TelegramBadRequest:
        pass

    vpn_key = await vpn.generate_vpn_key(conf_path)
    if vpn_key:
        # Ключ уходит отдельным сообщением: он длинный и не всегда влезает в подпись.
        await bot.send_message(
            chat_id,
            'Ключ для приложения AmneziaVPN '
            '(<a href="https://play.google.com/store/apps/details?id=org.amnezia.vpn">Google Play</a>, '
            '<a href="https://github.com/amnezia-vpn/amnezia-client">GitHub</a>):\n'
            f'<pre>{esc(vpn_key)}</pre>',
            disable_web_page_preview=True,
        )
    else:
        await bot.send_message(
            chat_id,
            'Не удалось сформировать ссылку vpn:// — используйте приложенный файл .conf.',
        )
    return True


async def notify_admins(text):
    """Сообщает всем администраторам о ситуации, требующей вмешательства."""
    for admin_id in SETTINGS.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception as e:
            logger.warning(f"Не удалось уведомить админа {admin_id}: {e}")


def format_expiration(expiration):
    return expiration.strftime('%d.%m.%Y %H:%M UTC') if expiration else 'не установлен'


# --- Команды ----------------------------------------------------------------

@router.message(Command('start', 'help'))
async def start_command(message: Message, state: FSMContext):
    await state.clear()
    await message.answer('Выберите действие:', reply_markup=kb.main_menu(message.from_user.id, SETTINGS))


@router.message(Command('add_admin'))
async def add_admin_command(message: Message):
    if not SETTINGS.is_admin(message.from_user.id):
        await message.answer('У вас нет прав.')
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].lstrip('-').isdigit():
        await message.answer('Формат: /add_admin &lt;user_id&gt;')
        return
    new_admin_id = int(parts[1])
    if new_admin_id in SETTINGS.admin_ids:
        await message.answer(f'{new_admin_id} уже администратор.')
        return
    db.add_admin(new_admin_id)
    SETTINGS.admin_ids.add(new_admin_id)
    await message.answer(f'Админ {new_admin_id} добавлен.')
    try:
        await bot.send_message(new_admin_id, 'Вы назначены администратором!')
    except Exception as e:
        logger.warning(f"Не удалось уведомить нового админа {new_admin_id}: {e}")


# --- Навигация --------------------------------------------------------------

@router.callback_query(F.data == 'home')
async def return_home(callback: CallbackQuery, state: FSMContext):
    await show_main_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == 'settings')
async def settings_menu(callback: CallbackQuery, state: FSMContext):
    if not SETTINGS.is_admin(callback.from_user.id):
        await deny(callback)
        return
    await state.clear()
    await show(callback, 'Настройки:', kb.settings_menu())
    await callback.answer()


# --- Управление клиентами ---------------------------------------------------

@router.callback_query(F.data == 'add_user')
async def prompt_for_user_name(callback: CallbackQuery, state: FSMContext):
    if not SETTINGS.is_staff(callback.from_user.id):
        await deny(callback)
        return
    await state.set_state(Form.user_name)
    await show(
        callback,
        'Введите имя пользователя (латиница, цифры, дефис и подчёркивание):',
        kb.home_only(),
    )
    await callback.answer()


@router.message(Form.user_name)
async def create_user(message: Message, state: FSMContext):
    if not SETTINGS.is_staff(message.from_user.id):
        await state.clear()
        return
    username = (message.text or '').strip()
    if not config.USERNAME_RE.match(username):
        await message.reply('Имя может содержать только латинские буквы, цифры, - и _.')
        return
    if vpn.client_conf_path(username).exists():
        await message.reply('Клиент с таким именем уже существует.')
        return

    status = await message.answer('Создаю клиента, это займёт несколько секунд...')
    created = await vpn.create_client(username, SETTINGS)
    await status.delete()

    if created:
        db.set_user_telegram_id(username, message.from_user.id)
        await send_client_config(message.chat.id, username)
    else:
        await message.answer(
            'Не удалось создать клиента. Проверьте логи бота и доступность контейнера '
            f'<code>{esc(SETTINGS.docker_container)}</code>.'
        )
    await show_main_menu(message, state)


@router.callback_query(F.data == 'list_users')
async def list_users(callback: CallbackQuery, state: FSMContext):
    if not SETTINGS.is_staff(callback.from_user.id):
        await deny(callback)
        return
    await state.clear()
    clients = db.get_client_list()
    if not clients:
        await show(callback, 'Список клиентов пуст.', kb.home_only())
        await callback.answer()
        return

    active = await vpn.active_clients(SETTINGS)
    now = datetime.now(pytz.utc)
    rows = []
    for username, _ in clients:
        handshake = active.get(username)
        online = handshake and (now - handshake).total_seconds() <= ONLINE_THRESHOLD_SECONDS
        rows.append((username, '🟢' if online else '❌'))

    await show(callback, 'Выберите пользователя:', kb.client_list(rows, 'client'))
    await callback.answer()


@router.callback_query(F.data.startswith('client:'))
async def client_selected(callback: CallbackQuery):
    if not SETTINGS.is_staff(callback.from_user.id):
        await deny(callback)
        return
    username = callback.data.split(':', 1)[1]
    if not vpn.client_conf_path(username).exists():
        await callback.answer('Пользователь не найден.', show_alert=True)
        return

    active = await vpn.active_clients(SETTINGS)
    handshake = active.get(username)
    now = datetime.now(pytz.utc)
    if handshake and (now - handshake).total_seconds() <= ONLINE_THRESHOLD_SECONDS:
        status = '🟢 Онлайн'
    elif handshake:
        status = f'🔴 Офлайн (последний handshake {handshake.strftime("%d.%m.%Y %H:%M UTC")})'
    else:
        status = '🔴 Не подключался'

    owner = db.get_user_telegram_id(username)
    text = (
        f'📧 <b>Имя:</b> {esc(username)}\n'
        f'🌐 <b>Статус:</b> {esc(status)}\n'
        f'⏰ <b>Срок действия:</b> {esc(format_expiration(db.get_user_expiration(username)))}\n'
        f'👤 <b>Владелец:</b> {esc(owner) if owner else "не привязан"}'
    )
    await show(callback, text, kb.client_actions(username))
    await callback.answer()


@router.callback_query(F.data.startswith('delete:'))
async def delete_client(callback: CallbackQuery):
    if not SETTINGS.is_staff(callback.from_user.id):
        await deny(callback)
        return
    username = callback.data.split(':', 1)[1]
    await callback.answer('Удаляю...')
    if await vpn.delete_client(username, SETTINGS):
        text = f'Пользователь <b>{esc(username)}</b> удалён.'
        logger.info(f"Пользователь {username} удалён.")
    else:
        text = f'Не удалось удалить <b>{esc(username)}</b>. Проверьте логи.'
    await show(callback, text, kb.main_menu(callback.from_user.id, SETTINGS))


@router.callback_query(F.data.startswith('sendconf:'))
async def send_config_to_staff(callback: CallbackQuery):
    if not SETTINGS.is_staff(callback.from_user.id):
        await deny(callback)
        return
    username = callback.data.split(':', 1)[1]
    await callback.answer()
    await send_client_config(chat_id_of(callback), username)


@router.callback_query(F.data == 'get_config')
async def list_users_for_config(callback: CallbackQuery, state: FSMContext):
    if not SETTINGS.is_staff(callback.from_user.id):
        await deny(callback)
        return
    await state.clear()
    clients = db.get_client_list()
    if not clients:
        await show(callback, 'Список клиентов пуст.', kb.home_only())
        await callback.answer()
        return
    rows = [(username, '') for username, _ in clients]
    await show(callback, 'Выберите пользователя:', kb.client_list(rows, 'sendconf'))
    await callback.answer()


# --- Продление подписки -----------------------------------------------------

@router.callback_query(F.data.startswith('renew:'))
async def renew_prompt(callback: CallbackQuery):
    if not SETTINGS.is_admin(callback.from_user.id):
        await deny(callback)
        return
    username = callback.data.split(':', 1)[1]
    await show(callback, 'Выберите период продления или укажите дату:', kb.renewal_periods(username))
    await callback.answer()


@router.callback_query(F.data.startswith('renewp:'))
async def renew_period(callback: CallbackQuery, state: FSMContext):
    if not SETTINGS.is_admin(callback.from_user.id):
        await deny(callback)
        return
    _, username, period = callback.data.split(':', 2)

    if period == 'custom':
        await state.set_state(Form.custom_date)
        await state.update_data(username=username)
        await show(
            callback,
            'Введите дату окончания подписки в формате ДД-ММ-ГГГГ (например, 31-12-2026):',
            kb.single('Отмена', f'client:{username}'),
        )
        await callback.answer()
        return

    if period not in config.PERIODS:
        await callback.answer('Неизвестный период.', show_alert=True)
        return

    expiration = vpn.extend_subscription(username, period)
    logger.info(f"Подписка {username} продлена на {period} до {expiration}.")
    await show(
        callback,
        f'Подписка для <b>{esc(username)}</b> продлена до {esc(format_expiration(expiration))}.',
        kb.main_menu(callback.from_user.id, SETTINGS),
    )
    await callback.answer()


@router.message(Form.custom_date)
async def set_custom_date(message: Message, state: FSMContext):
    if not SETTINGS.is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    username = data.get('username')
    try:
        expiration = datetime.strptime((message.text or '').strip(), '%d-%m-%Y').replace(tzinfo=pytz.utc)
    except ValueError:
        await message.reply('Введите дату в формате ДД-ММ-ГГГГ (например, 31-12-2026).')
        return
    if expiration < datetime.now(pytz.utc):
        await message.reply('Дата должна быть в будущем.')
        return
    db.set_user_expiration(username, expiration)
    await message.reply(
        f'Подписка для <b>{esc(username)}</b> продлена до {esc(format_expiration(expiration))}.'
    )
    await show_main_menu(message, state)


# --- Администраторы ---------------------------------------------------------

@router.callback_query(F.data == 'list_admins')
async def list_admins(callback: CallbackQuery):
    if not SETTINGS.is_admin(callback.from_user.id):
        await deny(callback)
        return
    text = 'Администраторы:\n' + '\n'.join(f'• <code>{admin_id}</code>' for admin_id in sorted(SETTINGS.admin_ids))
    await show(callback, text, kb.admin_list(SETTINGS.admin_ids))
    await callback.answer()


@router.callback_query(F.data.startswith('rmadmin:'))
async def remove_admin(callback: CallbackQuery):
    if not SETTINGS.is_admin(callback.from_user.id):
        await deny(callback)
        return
    admin_id = int(callback.data.split(':', 1)[1])
    if admin_id not in SETTINGS.admin_ids:
        await callback.answer('Администратор не найден.', show_alert=True)
        return
    if len(SETTINGS.admin_ids) <= 1:
        await callback.answer('Нельзя удалить последнего администратора.', show_alert=True)
        return
    db.remove_admin(admin_id)
    SETTINGS.admin_ids.discard(admin_id)
    try:
        await bot.send_message(admin_id, 'Вы удалены из администраторов.')
    except Exception as e:
        logger.warning(f"Не удалось уведомить снятого админа {admin_id}: {e}")
    await callback.answer('Администратор удалён.')
    await list_admins(callback)


@router.callback_query(F.data == 'add_admin')
async def prompt_for_admin_id(callback: CallbackQuery, state: FSMContext):
    if not SETTINGS.is_admin(callback.from_user.id):
        await deny(callback)
        return
    await state.set_state(Form.admin_id)
    await show(callback, 'Введите Telegram ID нового администратора:', kb.single('Отмена', 'settings'))
    await callback.answer()


@router.message(Form.admin_id)
async def add_admin_from_state(message: Message, state: FSMContext):
    if not SETTINGS.is_admin(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or '').strip()
    if not raw.lstrip('-').isdigit():
        await message.reply('Введите корректный числовой Telegram ID.')
        return
    new_admin_id = int(raw)
    if new_admin_id in SETTINGS.admin_ids:
        await message.reply('Этот пользователь уже администратор.')
    else:
        db.add_admin(new_admin_id)
        SETTINGS.admin_ids.add(new_admin_id)
        await message.reply(f'Админ {new_admin_id} добавлен.')
        try:
            await bot.send_message(new_admin_id, 'Вы назначены администратором!')
        except Exception as e:
            logger.warning(f"Не удалось уведомить нового админа {new_admin_id}: {e}")
    await show_main_menu(message, state)


# --- Цены -------------------------------------------------------------------

@router.callback_query(F.data == 'pricing_settings')
async def pricing_settings(callback: CallbackQuery, state: FSMContext):
    if not SETTINGS.is_admin(callback.from_user.id):
        await deny(callback)
        return
    await state.clear()
    await show(callback, 'Настройки цен — выберите период:', kb.pricing_settings_menu(SETTINGS))
    await callback.answer()


@router.callback_query(F.data.startswith('setprice:'))
async def set_price_prompt(callback: CallbackQuery, state: FSMContext):
    if not SETTINGS.is_admin(callback.from_user.id):
        await deny(callback)
        return
    period = callback.data.split(':', 1)[1]
    if period not in config.PERIODS:
        await callback.answer('Неизвестный период.', show_alert=True)
        return
    await state.set_state(Form.price)
    await state.update_data(period=period)
    await show(
        callback,
        f'Введите новую цену для периода «{esc(config.period_label(period))}» в рублях (например, 1000):',
        kb.single('⬅️ Назад', 'pricing_settings'),
    )
    await callback.answer()


@router.message(Form.price)
async def set_price(message: Message, state: FSMContext):
    if not SETTINGS.is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    period = data.get('period')
    try:
        price = float((message.text or '').strip().replace(',', '.'))
    except ValueError:
        await message.reply('Введите корректное число (например, 1000).')
        return
    if price <= 0:
        await message.reply('Цена должна быть больше нуля.')
        return
    if SETTINGS.currency == 'RUB' and price < settings_module.MIN_INVOICE_RUB:
        await message.reply(
            f'Telegram не принимает счета меньше {settings_module.MIN_INVOICE_RUB:g} ₽. '
            'Укажите большую сумму.'
        )
        return

    db.set_pricing(period, price)
    SETTINGS.pricing[period] = price
    await message.reply(f'Цена для «{esc(config.period_label(period))}» обновлена: {price:.2f} ₽')
    await state.clear()
    await message.answer('Настройки цен — выберите период:', reply_markup=kb.pricing_settings_menu(SETTINGS))


# --- Промокоды --------------------------------------------------------------

@router.callback_query(F.data == 'manage_promocodes')
async def manage_promocodes(callback: CallbackQuery, state: FSMContext):
    if not SETTINGS.is_admin(callback.from_user.id):
        await deny(callback)
        return
    await state.clear()
    promocodes = db.get_promocodes()
    if promocodes:
        lines = []
        for code, info in promocodes.items():
            expires = info['expires_at'].strftime('%d.%m.%Y') if info['expires_at'] else 'бессрочно'
            lines.append(
                f"<code>{esc(code)}</code> — скидка {info['discount']:g}%, "
                f"использован {info['uses']}/{info['max_uses'] or '∞'}, до {expires}, "
                f"период: {esc(config.period_label(info['subscription_period'])) if info['subscription_period'] else 'нет'}"
            )
        text = 'Промокоды:\n' + '\n'.join(lines)
    else:
        text = 'Промокоды отсутствуют.'
    await show(callback, text, kb.promocodes_menu())
    await callback.answer()


@router.callback_query(F.data == 'add_promocode')
async def add_promocode_prompt(callback: CallbackQuery, state: FSMContext):
    if not SETTINGS.is_admin(callback.from_user.id):
        await deny(callback)
        return
    await state.set_state(Form.new_promocode)
    await show(
        callback,
        'Введите промокод в формате:\n'
        '<code>&lt;код&gt; &lt;скидка%&gt; &lt;дней_действия&gt; &lt;макс_использований|none&gt; &lt;период|none&gt;</code>\n\n'
        'Период выдаёт ключ бесплатно, скидка — уменьшает цену при оплате.\n'
        'Примеры:\n'
        '<code>FREE1M 100 30 10 1_month</code> — бесплатный ключ на месяц, 10 активаций\n'
        '<code>SALE20 20 30 none none</code> — скидка 20% при оплате, без лимита',
        kb.single('⬅️ Назад', 'manage_promocodes'),
    )
    await callback.answer()


@router.message(Form.new_promocode)
async def add_promocode(message: Message, state: FSMContext):
    if not SETTINGS.is_admin(message.from_user.id):
        await state.clear()
        return
    hint = (
        'Формат: <code>&lt;код&gt; &lt;скидка%&gt; &lt;дней&gt; &lt;макс_использований|none&gt; &lt;период|none&gt;</code>\n'
        'Пример: <code>SALE20 20 30 none none</code>'
    )
    parts = (message.text or '').strip().split()
    if len(parts) != 5:
        await message.reply(hint)
        return
    code, raw_discount, raw_days, raw_max_uses, period = parts
    try:
        discount = float(raw_discount.replace(',', '.'))
        days_valid = int(raw_days)
    except ValueError:
        await message.reply(hint)
        return
    if not 0 <= discount <= 100:
        await message.reply('Скидка должна быть от 0 до 100.')
        return
    if raw_max_uses.lower() == 'none':
        max_uses = None
    elif raw_max_uses.isdigit() and int(raw_max_uses) > 0:
        max_uses = int(raw_max_uses)
    else:
        await message.reply('Максимум использований — положительное число или <code>none</code>.')
        return
    if period.lower() == 'none':
        period = None
    elif period not in config.PERIODS:
        await message.reply(f"Период должен быть одним из: {', '.join(config.PERIODS)} или <code>none</code>.")
        return
    if period is None and discount <= 0:
        await message.reply('Промокод без периода и без скидки бесполезен.')
        return

    expires_at = datetime.now(pytz.utc) + timedelta(days=days_valid) if days_valid > 0 else None

    if db.add_promocode(code, discount, expires_at, max_uses, period):
        await message.reply(
            f'Промокод <code>{esc(code)}</code> добавлен: скидка {discount:g}%, '
            f"действует {days_valid if days_valid > 0 else '∞'} дней, "
            f"использований: {max_uses or '∞'}, "
            f"период: {esc(config.period_label(period)) if period else 'нет'}."
        )
    else:
        await message.reply('Такой промокод уже существует.')
    await state.clear()
    await message.answer('Управление промокодами:', reply_markup=kb.promocodes_menu())


@router.callback_query(F.data == 'delete_promocode')
async def delete_promocode_menu(callback: CallbackQuery):
    if not SETTINGS.is_admin(callback.from_user.id):
        await deny(callback)
        return
    promocodes = db.get_promocodes()
    if not promocodes:
        await callback.answer('Промокоды отсутствуют.', show_alert=True)
        return
    await show(callback, 'Выберите промокод для удаления:', kb.promocode_list(promocodes))
    await callback.answer()


@router.callback_query(F.data.startswith('rmpromo:'))
async def remove_promocode(callback: CallbackQuery, state: FSMContext):
    if not SETTINGS.is_admin(callback.from_user.id):
        await deny(callback)
        return
    code = callback.data.split(':', 1)[1]
    if db.remove_promocode(code):
        await callback.answer(f'Промокод {code} удалён.')
    else:
        await callback.answer(f'Промокод {code} не найден.', show_alert=True)
    await manage_promocodes(callback, state)


@router.callback_query(F.data == 'use_promocode')
async def use_promocode_prompt(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Form.promocode)
    await show(callback, 'Введите промокод:', kb.home_only())
    await callback.answer()


@router.message(Form.promocode)
async def apply_promocode(message: Message, state: FSMContext):
    code = (message.text or '').strip()
    promo = db.validate_promocode(code)
    if not promo:
        await message.reply('Неверный или истёкший промокод.')
        await show_main_menu(message, state)
        return

    period = promo['subscription_period']
    if period:
        # Промокод сразу выдаёт ключ — оплата не нужна.
        status = await message.answer('Создаю ключ, это займёт несколько секунд...')
        username = await vpn.issue_subscription(message.from_user.id, period, SETTINGS)
        await status.delete()
        if username:
            db.consume_promocode(code)
            await send_client_config(
                message.chat.id, username,
                header=f'Ваш VPN-ключ на {esc(config.period_label(period))}',
            )
            await message.answer(
                f'Промокод активирован. Подписка действует до '
                f'{esc(format_expiration(db.get_user_expiration(username)))}.'
            )
        else:
            await message.answer('Не удалось выдать ключ. Обратитесь к администратору.')
            await notify_admins(
                f'❗ Не удалось выдать ключ по промокоду {code} пользователю {message.from_user.id}.'
            )
        await show_main_menu(message, state)
        return

    # Промокод только со скидкой — запоминаем и показываем цены со скидкой.
    if not SETTINGS.payments_enabled:
        await message.reply('Оплата сейчас недоступна, а этот промокод даёт только скидку.')
        await show_main_menu(message, state)
        return

    await state.clear()
    await state.update_data(promocode=code, discount=promo['discount'])
    await message.answer(
        f"Промокод <code>{esc(code)}</code> принят: скидка {promo['discount']:g}%.\n"
        'Выберите период подписки:',
        reply_markup=kb.buy_menu(SETTINGS, promo['discount']),
    )


# --- Оплата (встроенные платежи Telegram) -----------------------------------

@router.callback_query(F.data == 'buy_key')
async def buy_key_menu(callback: CallbackQuery, state: FSMContext):
    if not SETTINGS.payments_enabled:
        await callback.answer('Оплата не настроена. Используйте промокод.', show_alert=True)
        return
    data = await state.get_data()
    discount = data.get('discount', 0.0)
    text = 'Выберите период подписки:'
    if discount:
        text = f"Скидка по промокоду {esc(data.get('promocode'))}: {discount:g}%\n{text}"
    await show(callback, text, kb.buy_menu(SETTINGS, discount))
    await callback.answer()


@router.callback_query(F.data.startswith('buy:'))
async def send_invoice(callback: CallbackQuery, state: FSMContext):
    if not SETTINGS.payments_enabled:
        await callback.answer('Оплата не настроена.', show_alert=True)
        return
    period = callback.data.split(':', 1)[1]
    if period not in config.PERIODS:
        await callback.answer('Неизвестный период.', show_alert=True)
        return

    data = await state.get_data()
    promocode = data.get('promocode')
    discount = data.get('discount', 0.0)
    # Промокод мог быть израсходован, пока пользователь выбирал период.
    if promocode and not db.validate_promocode(promocode):
        promocode, discount = None, 0.0
        await bot.send_message(chat_id_of(callback), 'Промокод больше не действует, счёт выставлен без скидки.')

    price = payments.final_price(SETTINGS, period, discount)
    if price <= 0:
        # Скидка 100% — счёт выставлять не нужно, выдаём ключ сразу.
        await callback.answer()
        await grant_paid_subscription(
            callback.from_user.id, chat_id_of(callback), period, promocode,
            amount=0, currency=SETTINGS.currency, charge_id=f'promo:{promocode}:{callback.from_user.id}',
        )
        await state.clear()
        return

    if SETTINGS.currency == 'RUB' and price < settings_module.MIN_INVOICE_RUB:
        await callback.answer(
            f'Итоговая сумма {price:.2f} ₽ меньше минимальной для Telegram '
            f'({settings_module.MIN_INVOICE_RUB:g} ₽).',
            show_alert=True,
        )
        return

    await callback.answer()
    try:
        await bot.send_invoice(
            chat_id_of(callback),
            **payments.build_invoice(SETTINGS, period, discount, promocode),
        )
    except TelegramBadRequest as e:
        logger.error(f"Не удалось выставить счёт: {e}")
        await bot.send_message(
            chat_id_of(callback),
            'Не удалось выставить счёт. Проверьте токен платёжного провайдера в настройках бота.',
        )


@payment_router.pre_checkout_query()
async def process_pre_checkout(query: PreCheckoutQuery):
    """Telegram ждёт ответ в течение 10 секунд, иначе оплата отменяется."""
    period, _ = payments.parse_payload(query.invoice_payload)
    if not SETTINGS.payments_enabled or not period:
        await query.answer(ok=False, error_message='Счёт устарел, оформите заказ заново.')
        return
    await query.answer(ok=True)


@payment_router.message(F.successful_payment)
async def process_successful_payment(message: Message, state: FSMContext):
    payment = message.successful_payment
    period, promocode = payments.parse_payload(payment.invoice_payload)
    charge_id = payment.provider_payment_charge_id or payment.telegram_payment_charge_id

    if not period:
        logger.error(f"Оплата {charge_id} с нераспознанным payload: {payment.invoice_payload!r}")
        await message.answer('Оплата получена, но заказ не распознан. Обратитесь к администратору.')
        await notify_admins(f'❗ Оплата {charge_id} с некорректным payload: {payment.invoice_payload!r}')
        return

    await state.clear()
    await grant_paid_subscription(
        message.from_user.id, message.chat.id, period, promocode,
        amount=payment.total_amount / 100, currency=payment.currency, charge_id=charge_id,
    )


async def grant_paid_subscription(user_id, chat_id, period, promocode, amount, currency, charge_id):
    """Выдаёт ключ после оплаты. Повторная доставка платежа ключ не дублирует."""
    if db.is_payment_recorded(charge_id):
        logger.info(f"Платёж {charge_id} уже обработан — повторная выдача пропущена.")
        return

    status = await bot.send_message(chat_id, 'Оплата получена. Создаю ключ...')
    username = await vpn.issue_subscription(user_id, period, SETTINGS)
    await status.delete()

    if not username:
        # Деньги списаны, а ключ не создан — это должен увидеть администратор.
        logger.error(f"Не удалось создать клиента после оплаты {charge_id} (пользователь {user_id}).")
        await bot.send_message(
            chat_id,
            'Оплата прошла, но выдать ключ автоматически не получилось. '
            'Администратор уже уведомлён и свяжется с вами.',
        )
        await notify_admins(
            f'❗ Оплата {charge_id} на {amount} {currency} от пользователя {user_id} '
            f'прошла, но клиент не создан. Требуется ручная выдача ключа.'
        )
        return

    db.record_payment(charge_id, user_id, username, period, amount, currency, promocode)
    if promocode:
        db.consume_promocode(promocode)

    await send_client_config(
        chat_id, username, header=f'Ваш VPN-ключ на {esc(config.period_label(period))}',
    )
    await bot.send_message(
        chat_id,
        f'Подписка активна до {esc(format_expiration(db.get_user_expiration(username)))}.',
        reply_markup=kb.main_menu(user_id, SETTINGS),
    )


# --- Ключи пользователя -----------------------------------------------------

@router.callback_query(F.data == 'my_keys')
async def my_keys(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    usernames = db.get_usernames_by_telegram_id(callback.from_user.id)
    usernames = [name for name in usernames if vpn.client_conf_path(name).exists()]
    if not usernames:
        await show(callback, 'У вас пока нет ключей.', kb.main_menu(callback.from_user.id, SETTINGS))
        await callback.answer()
        return

    lines = []
    for name in usernames:
        lines.append(f'• <code>{esc(name)}</code> — до {esc(format_expiration(db.get_user_expiration(name)))}')
    await show(callback, 'Ваши ключи:\n' + '\n'.join(lines), kb.main_menu(callback.from_user.id, SETTINGS))
    await callback.answer()
    for name in usernames:
        await send_client_config(chat_id_of(callback), name)


# --- Резервное копирование --------------------------------------------------

@router.callback_query(F.data == 'create_backup')
async def create_backup(callback: CallbackQuery):
    if not SETTINGS.is_admin(callback.from_user.id):
        await deny(callback)
        return
    await callback.answer('Готовлю бэкап...')

    backup_path = config.BASE_DIR / f"backup_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.zip"
    try:
        await asyncio.to_thread(_write_backup, backup_path)
        await bot.send_document(
            chat_id_of(callback),
            FSInputFile(backup_path, filename=backup_path.name),
            caption='Резервная копия конфигураций и данных бота.',
        )
    except Exception as e:
        logger.error(f"Ошибка создания бэкапа: {e}")
        await bot.send_message(chat_id_of(callback), f'Не удалось создать бэкап: {esc(e)}')
    finally:
        backup_path.unlink(missing_ok=True)


def _write_backup(backup_path):
    """Складывает в архив скрипты, files/ и users/."""
    with zipfile.ZipFile(backup_path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in ('awg_decode.py', 'newclient.sh', 'removeclient.sh'):
            script = config.BASE_DIR / name
            if script.exists():
                archive.write(script, name)
        for directory in (config.FILES_DIR, config.USERS_DIR):
            if not directory.exists():
                continue
            for path in directory.rglob('*'):
                if path.is_file():
                    archive.write(path, path.relative_to(config.BASE_DIR))


# --- Истечение подписок -----------------------------------------------------

async def enforce_expirations():
    """Удаляет клиентов с истёкшей подпиской и сообщает об этом владельцам."""
    now = datetime.now(pytz.utc)
    for username, expiration in db.get_all_expirations().items():
        if not expiration or expiration > now:
            continue
        if not vpn.client_conf_path(username).exists():
            db.remove_user_expiration(username)
            continue

        owner = db.get_user_telegram_id(username)
        logger.info(f"Подписка {username} истекла {expiration} — удаляю клиента.")
        if not await vpn.delete_client(username, SETTINGS):
            logger.error(f"Не удалось удалить клиента {username} с истёкшей подпиской.")
            continue
        if owner:
            try:
                await bot.send_message(
                    owner,
                    f'Срок действия ключа <code>{esc(username)}</code> истёк, доступ отключён.',
                    reply_markup=kb.main_menu(owner, SETTINGS),
                )
            except Exception as e:
                logger.warning(f"Не удалось уведомить {owner} об истечении подписки: {e}")


# --- Фолбэк для сообщений вне диалогов --------------------------------------

@router.message(F.text)
async def fallback(message: Message, state: FSMContext):
    """Любое сообщение вне диалога возвращает пользователя в главное меню."""
    if await state.get_state() is None:
        await show_main_menu(message, state)


# --- Запуск -----------------------------------------------------------------

async def main():
    scheduler.add_job(enforce_expirations, 'interval', hours=1, next_run_time=datetime.now(pytz.utc))
    scheduler.start()
    logger.info(
        f"Бот запущен. Каталог: {config.BASE_DIR}. "
        f"Оплата: {'включена' if SETTINGS.payments_enabled else 'отключена'}."
    )
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info('Бот остановлен.')
