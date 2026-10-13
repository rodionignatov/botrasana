import asyncio
import json
import logging
import os

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.errors import AuthKeyDuplicatedError
from telethon.errors.common import InvalidBufferError

load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Скрываем шум от telethon/asyncio при остановке
logging.getLogger('telethon').setLevel(logging.WARNING)
logging.getLogger('asyncio').setLevel(logging.WARNING)

BOT_TOKEN = os.getenv('BOT_TOKEN')
API_ID = int(os.getenv('API_ID', '0'))
API_HASH = os.getenv('API_HASH', '')

if not BOT_TOKEN or not API_ID or not API_HASH:
    logger.error('❌ Ошибка: проверьте .env файл!')
    raise SystemExit(1)

DATA_DIR = 'user_data'


# ════════════════════════════════════════
# 💾 ФАЙЛЫ
# ════════════════════════════════════════

def get_user_dir(user_id):
    path = os.path.join(DATA_DIR, str(user_id))
    os.makedirs(path, exist_ok=True)
    return path


def load_user_json(user_id, filename, default=None):
    path = os.path.join(get_user_dir(user_id), filename)
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return default or {}


def save_user_json(user_id, filename, data):
    path = os.path.join(get_user_dir(user_id), filename)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_user_session(user_id):
    path = os.path.join(get_user_dir(user_id), "session.txt")
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    return ""


def save_user_session(user_id, session):
    path = os.path.join(get_user_dir(user_id), "session.txt")
    with open(path, 'w', encoding='utf-8') as f:
        f.write(session)


# ════════════════════════════════════════
# 📝 СОСТОЯНИЯ
# ════════════════════════════════════════

login_clients = {}
login_states = {}
login_data = {}
login_qr_tasks = {}

user_broadcasts = {}
user_tasks = {}

bot_instance = None


# ════════════════════════════════════════
# 🚀 РАССЫЛКА
# ════════════════════════════════════════

async def get_user_groups(user_id):
    session = get_user_session(user_id)
    if not session:
        return []

    client = TelegramClient(StringSession(session), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return []

        groups = []
        async for dialog in client.iter_dialogs():
            if dialog.is_group or dialog.is_channel:
                groups.append({
                    'id': dialog.id,
                    'title': dialog.title or 'Без названия'
                })
        return groups
    except AuthKeyDuplicatedError:
        logger.error(f'🔒 Сессия {user_id} скомпрометирована')
        return []
    except Exception as e:
        logger.error(f'Ошибка получения групп для {user_id}: {e}')
        return []
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def send_user_to_all(user_id, text):
    session = get_user_session(user_id)
    if not session:
        return 0, 0

    groups = await get_user_groups(user_id)
    if not groups:
        return 0, 0

    client = TelegramClient(StringSession(session), API_ID, API_HASH)
    sent, failed = 0, 0

    try:
        await client.connect()
        if not await client.is_user_authorized():
            return 0, len(groups)

        for group in groups:
            try:
                await client.send_message(group['id'], text)
                sent += 1
            except Exception as e:
                failed += 1
                logger.warning(f"Пользователь {user_id}: ошибка отправки в {group['title']}: {e}")
            await asyncio.sleep(5)

    except AuthKeyDuplicatedError:
        logger.error(f'🔒 Сессия {user_id} скомпрометирована')
        raise
    except InvalidBufferError:
        logger.warning(f'⏳ Telegram ограничил запросы для {user_id}')
        raise
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    return sent, failed


async def user_broadcast_loop(user_id):
    while user_broadcasts.get(user_id, False):
        try:
            messages = load_user_json(user_id, 'messages.json', {})
            text = messages.get('message', '')

            if text:
                sent, failed = await send_user_to_all(user_id, text)
                logger.info(f'Пользователь {user_id}: отправлено {sent}, ошибок {failed}')
            else:
                logger.warning(f'Пользователь {user_id}: нет сообщения для рассылки')

            await asyncio.sleep(900)

        except asyncio.CancelledError:
            break

        except AuthKeyDuplicatedError:
            logger.error(f'🔒 Сессия {user_id} скомпрометирована. Останавливаю.')
            user_broadcasts[user_id] = False
            save_user_json(user_id, 'settings.json', {'is_running': False})

            try:
                await bot_instance.send_message(
                    chat_id=user_id,
                    text=(
                        '🔒 <b>Ваша сессия скомпрометирована!</b>\n\n'
                        'Кто-то использует её с другого IP.\n'
                        'Подключите аккаунт заново.',
                    ),
                    parse_mode='HTML'
                )
            except Exception:
                pass
            break

        except InvalidBufferError:
            logger.warning('⏳ Telegram ограничил запросы (429). Жду 5 минут...')
            await asyncio.sleep(300)

        except Exception as e:
            logger.error(f'Ошибка для пользователя {user_id}: {e}')
            await asyncio.sleep(60)


# ════════════════════════════════════════
# 🔑 АВТОРИЗАЦИЯ: ОБЩИЕ ФУНКЦИИ
# ════════════════════════════════════════

async def close_login(user_id):
    task = login_qr_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()

    client = login_clients.pop(user_id, None)
    login_states.pop(user_id, None)
    login_data.pop(user_id, None)
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass


async def finish_login(user_id, bot, me=None):
    client = login_clients.get(user_id)
    if not client:
        return

    save_user_session(user_id, client.session.save())

    if me is None:
        try:
            me = await client.get_me()
        except Exception:
            me = None

    name = me.first_name if me else "аккаунт"

    await bot.send_message(
        chat_id=user_id,
        text=(
            f'✅ <b>Telegram подключён!</b>\n\n'
            f'👤 Аккаунт: <b>{name}</b>\n'
            f'Сессия сохранена на сервере.\n\n'
            f'Теперь можно запускать рассылку.'
        ),
        parse_mode='HTML'
    )
    await close_login(user_id)


# ════════════════════════════════════════
# 🔑 СПОСОБ 1: QR-КОД
# ════════════════════════════════════════

async def qr_login_task(user_id, bot):
    client = login_clients.get(user_id)
    if not client:
        return

    try:
        qr = await client.qr_login()

        text = (
            '📱 <b>Шаг 1: отсканируйте QR-код</b>\n\n'
            '1. Откройте Telegram на телефоне\n'
            '2. <b>Настройки → Устройства</b>\n'
            '3. Нажмите <b>«Подключить устройство»</b>\n'
            '4. Наведите камеру на QR-код ниже\n\n'
            '⌛ QR-код действителен 2 минуты.'
        )

        try:
            await bot.send_photo(
                chat_id=user_id,
                photo=f'https://api.qrserver.com/v1/create-qr-code/?size=400x400&data={qr.url}',
                caption=text,
                parse_mode='HTML'
            )
        except Exception:
            await bot.send_message(
                chat_id=user_id,
                text=text + f'\n\n🔗 Ссылка: <code>{qr.url}</code>',
                parse_mode='HTML'
            )

        # Ждём сканирования
        while True:
            try:
                await qr.wait(timeout=120)
                break
            except asyncio.TimeoutError:
                try:
                    await qr.recreate()
                    await bot.send_message(
                        chat_id=user_id,
                        text=(
                            '🔄 <b>QR-код обновлён.</b> Отсканируйте новый:\n\n'
                            f'<code>{qr.url}</code>'
                        ),
                        parse_mode='HTML'
                    )
                except Exception as e:
                    logger.error(f'Не удалось обновить QR: {e}')
                    await close_login(user_id)
                    return

        if await client.is_user_authorized():
            await finish_login(user_id, bot)
        else:
            login_states[user_id] = 'password_2fa'
            await bot.send_message(
                chat_id=user_id,
                text=(
                    '🔐 <b>Шаг 2: пароль 2FA</b>\n\n'
                    'У вас включена двухэтапная аутентификация.\n'
                    'Отправьте пароль от аккаунта.'
                ),
                parse_mode='HTML'
            )

    except errors.SessionPasswordNeededError:
        login_states[user_id] = 'password_2fa'
        await bot.send_message(
            chat_id=user_id,
            text=(
                '🔐 <b>Шаг 2: пароль 2FA</b>\n\n'
                'У вас включена двухэтапная аутентификация.\n'
                'Отправьте пароль от аккаунта.'
            ),
            parse_mode='HTML'
        )

    except asyncio.CancelledError:
        pass

    except Exception as e:
        logger.exception(f'Ошибка QR-входа для {user_id}')
        await bot.send_message(
            chat_id=user_id,
            text=f'❌ Ошибка входа: {e}\n\nПопробуйте ещё раз.'
        )
        await close_login(user_id)


async def start_qr_login(user_id, bot):
    await close_login(user_id)

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    login_clients[user_id] = client

    try:
        await client.connect()
    except Exception as e:
        logger.exception(f'Ошибка подключения клиента для {user_id}')
        await close_login(user_id)
        await bot.send_message(chat_id=user_id, text=f'❌ Не удалось подключиться: {e}')
        return

    task = asyncio.create_task(qr_login_task(user_id, bot))
    login_qr_tasks[user_id] = task


# ════════════════════════════════════════
# 🔑 СПОСОБ 2: STRING SESSION
# ════════════════════════════════════════

async def handle_string_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принимает String Session от пользователя и проверяет его"""
    user_id = update.effective_user.id

    if login_states.get(user_id) != 'string_session':
        return False

    text = update.message.text.strip()
    # Убираем кавычки и пробелы
    text = text.replace('"', '').replace("'", "").replace('\n', '').replace(' ', '')

    if len(text) < 50:
        await update.message.reply_text(
            '❌ <b>Строка слишком короткая.</b>\n\n'
            'Проверьте, что вы скопировали её полностью (обычно 300+ символов).',
            parse_mode='HTML'
        )
        return True

    client = TelegramClient(StringSession(text), API_ID, API_HASH)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await update.message.reply_text(
                '❌ <b>Сессия не работает.</b>\n\n'
                'Проверьте, что строка не повреждена и не устарела.',
                parse_mode='HTML'
            )
            await client.disconnect()
            login_states.pop(user_id, None)
            return True

        me = await client.get_me()
        save_user_session(user_id, text)

        await update.message.reply_text(
            f'✅ <b>Аккаунт подключён!</b>\n\n'
            f'👤 Имя: <b>{me.first_name}</b>\n'
            f'📱 Телефон: {me.phone or "скрыт"}\n\n'
            f'Теперь можно запускать рассылку.',
            parse_mode='HTML'
        )

        await client.disconnect()
        login_states.pop(user_id, None)
        return True

    except Exception as e:
        logger.exception(f'Ошибка проверки String Session для {user_id}')
        await update.message.reply_text(
            f'❌ <b>Ошибка:</b> {e}\n\n'
            'Попробуйте ещё раз или используйте QR-код.',
            parse_mode='HTML'
        )
        try:
            await client.disconnect()
        except Exception:
            pass
        login_states.pop(user_id, None)
        return True


# ════════════════════════════════════════
# 🔑 2FA ПОСЛЕ QR
# ════════════════════════════════════════

async def handle_2fa_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = login_states.get(user_id)

    if state != 'password_2fa':
        return False

    client = login_clients.get(user_id)
    if not client:
        await update.message.reply_text('❌ Сессия авторизации потеряна. Начните заново.')
        await close_login(user_id)
        return True

    password = update.message.text.strip()

    try:
        await client.sign_in(password=password)
    except errors.PasswordHashInvalidError:
        await update.message.reply_text('❌ Неверный пароль 2FA. Попробуйте ещё раз.')
        return True
    except Exception as e:
        logger.exception(f'Ошибка 2FA для {user_id}')
        await update.message.reply_text(f'❌ Ошибка: {e}\nНачните заново.')
        await close_login(user_id)
        return True

    await finish_login(user_id, context.bot)
    return True


# ════════════════════════════════════════
# 🤖 КОМАНДЫ
# ════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    keyboard = [
        [InlineKeyboardButton('🔑 Подключить через QR', callback_data='qr_login')],
        [InlineKeyboardButton('📋 Подключить через String Session', callback_data='string_login')],
        [InlineKeyboardButton('▶️ Запустить рассылку', callback_data='start_broadcast')],
        [InlineKeyboardButton('⏹ Остановить рассылку', callback_data='stop_broadcast')],
        [InlineKeyboardButton('📝 Изменить сообщение', callback_data='edit_message')],
        [InlineKeyboardButton('📊 Статистика', callback_data='stats')],
    ]

    status = '🟢 РАБОТАЕТ' if user_broadcasts.get(user_id, False) else '🔴 ОСТАНОВЛЕН'
    groups = await get_user_groups(user_id)
    messages = load_user_json(user_id, 'messages.json', {})
    current_message = messages.get('message', '')

    await update.message.reply_text(
        '🤖 <b>Бот для рассылок</b>\n\n'
        f'Статус: {status}\n'
        f'Групп: {len(groups)}\n\n'
        '📨 <b>Текущее сообщение:</b>\n'
        f'{current_message[:200]}{"..." if len(current_message) > 200 else ""}\n\n'
        '📌 <b>Как подключить аккаунт:</b>\n'
        '• <b>QR-код</b> — быстро, но нужен уже открытый Telegram\n'
        '• <b>String Session</b> — если у вас уже есть готовая строка',
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )


async def set_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text(
            '❌ <b>Как использовать:</b>\n\n'
            '/setsession НОВЫЙ_STRING_SESSION\n\n'
            'Или нажмите «📋 Подключить через String Session» в меню.',
            parse_mode='HTML'
        )
        return

    new_session = ' '.join(context.args).strip()
    if len(new_session) < 50:
        await update.message.reply_text('❌ Строка слишком короткая!')
        return

    was_running = user_broadcasts.get(user_id, False)
    if was_running:
        user_broadcasts[user_id] = False
        task = user_tasks.pop(user_id, None)
        if task:
            task.cancel()

    save_user_session(user_id, new_session)

    try:
        groups = await get_user_groups(user_id)
        await update.message.reply_text(
            f'✅ <b>Аккаунт обновлён!</b>\n\n📊 Найдено групп: {len(groups)}',
            parse_mode='HTML'
        )
    except Exception as e:
        await update.message.reply_text(f'❌ Ошибка: {e}')
        return

    if was_running:
        user_broadcasts[user_id] = True
        user_tasks[user_id] = asyncio.create_task(user_broadcast_loop(user_id))
        await update.message.reply_text('✅ Рассылка перезапущена!')


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    query = update.callback_query
    await query.answer()

    if query.data == 'qr_login':
        await query.edit_message_text(
            '📱 <b>Подключение через QR-код</b>\n\n'
            '<b>Инструкция:</b>\n'
            '1. Откройте Telegram на телефоне\n'
            '2. <b>Настройки → Устройства</b>\n'
            '3. Нажмите <b>«Подключить устройство»</b>\n'
            '4. Наведите камеру на QR-код\n\n'
            '⏳ Сейчас пришлю QR-код.',
            parse_mode='HTML'
        )
        await start_qr_login(user_id, context.bot)
        return

    if query.data == 'string_login':
        login_states[user_id] = 'string_session'
        await query.edit_message_text(
            '📋 <b>Подключение через String Session</b>\n\n'
            '<b>Что это:</b> длинная строка (300+ символов), которая даёт доступ к вашему Telegram.\n\n'
            '<b>Что делать:</b>\n'
            '• Если у вас уже есть готовая строка — просто отправьте её сюда одним сообщением\n'
            '• Если нет — используйте QR-код\n\n'
            '⚠️ <b>Важно:</b> никому не передавайте String Session.\n\n'
            '📩 <b>Отправьте строку одним сообщением:</b>',
            parse_mode='HTML'
        )
        return

    if query.data == 'start_broadcast':
        if user_broadcasts.get(user_id, False):
            await query.edit_message_text('⚠️ Рассылка уже запущена!')
            return

        if not get_user_session(user_id):
            await query.edit_message_text(
                '❌ Сначала подключите Telegram!\n\n'
                'Выберите способ подключения:',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton('🔑 Подключить через QR', callback_data='qr_login')],
                    [InlineKeyboardButton('📋 Подключить через String Session', callback_data='string_login')],
                ])
            )
            return

        user_broadcasts[user_id] = True
        settings = load_user_json(user_id, 'settings.json', {})
        settings['is_running'] = True
        save_user_json(user_id, 'settings.json', settings)
        user_tasks[user_id] = asyncio.create_task(user_broadcast_loop(user_id))
        await query.edit_message_text('✅ Рассылка ЗАПУЩЕНА!')
        return

    if query.data == 'stop_broadcast':
        user_broadcasts[user_id] = False
        task = user_tasks.pop(user_id, None)
        if task:
            task.cancel()

        settings = load_user_json(user_id, 'settings.json', {})
        settings['is_running'] = False
        save_user_json(user_id, 'settings.json', settings)
        await query.edit_message_text('⏹ Рассылка ОСТАНОВЛЕНА!')
        return

    if query.data == 'stats':
        messages = load_user_json(user_id, 'messages.json', {})
        groups = await get_user_groups(user_id)
        await query.edit_message_text(
            f'📊 <b>Статистика</b>\n\n'
            f'📁 Групп: {len(groups)}\n'
            f'📨 Сообщение: {len(messages.get("message", ""))} символов\n'
            f'🔄 Статус: {"🟢 Работает" if user_broadcasts.get(user_id, False) else "🔴 Остановлен"}',
            parse_mode='HTML'
        )
        return

    if query.data == 'edit_message':
        context.user_data['editing'] = 'message'
        await query.edit_message_text(
            '📝 Напишите НОВОЕ сообщение для рассылки.\n\n'
            'Оно будет отправляться во все ваши группы каждые 15 минут.'
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_chat.type != 'private':
        return

    user_id = update.effective_user.id

    # 2FA после QR
    if login_states.get(user_id) == 'password_2fa':
        if await handle_2fa_message(update, context):
            return

    # String Session
    if login_states.get(user_id) == 'string_session':
        if await handle_string_session(update, context):
            return

    # Редактирование сообщения
    if 'editing' in context.user_data:
        key = context.user_data['editing']
        messages = load_user_json(user_id, 'messages.json', {})
        messages[key] = update.message.text
        save_user_json(user_id, 'messages.json', messages)
        await update.message.reply_text('✅ Сообщение сохранено! Оно будет рассылаться каждые 15 минут.')
        del context.user_data['editing']


# ════════════════════════════════════════
# 🚀 ЗАПУСК
# ════════════════════════════════════════

def main():
    global bot_instance

    app = Application.builder().token(BOT_TOKEN).build()
    bot_instance = app.bot

    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('setsession', set_session))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info('🚀 Бот запущен!')
    logger.info('📱 Подключение: QR-код или String Session')
    logger.info('📨 Одно сообщение для рассылки, интервал 15 минут')

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()