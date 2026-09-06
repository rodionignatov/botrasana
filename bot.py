import asyncio
import logging
import json
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters, \
    ConversationHandler
from telethon import TelegramClient
from telethon.sessions import StringSession
from dotenv import load_dotenv

# Загружаем переменные из .env
load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ════════════════════════════════════════
# 🔑 ДАННЫЕ ИЗ .ENV
# ════════════════════════════════════════

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")

if not BOT_TOKEN or not API_ID or not API_HASH:
    logger.error("❌ Ошибка: проверьте .env файл!")
    exit(1)

# ════════════════════════════════════════

DATA_DIR = "user_data"


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
# 📝 СОСТОЯНИЯ ДЛЯ ГЕНЕРАЦИИ
# ════════════════════════════════════════

PHONE, CODE, PASSWORD = range(3)

user_broadcasts = {}
user_tasks = {}


# ════════════════════════════════════════
# 🚀 ОСНОВНАЯ ЛОГИКА РАССЫЛКИ
# ════════════════════════════════════════

async def get_user_groups(user_id):
    session = get_user_session(user_id)
    if not session:
        return []
    client = TelegramClient(StringSession(session), API_ID, API_HASH)
    await client.start()
    groups = []
    async for dialog in client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            groups.append({'id': dialog.id, 'title': dialog.title or "Без названия"})
    await client.disconnect()
    return groups


async def send_user_to_all(user_id, text):
    session = get_user_session(user_id)
    if not session:
        return 0, 0

    groups = await get_user_groups(user_id)
    if not groups:
        return 0, 0

    client = TelegramClient(StringSession(session), API_ID, API_HASH)
    await client.start()
    sent, failed = 0, 0
    for group in groups:
        try:
            await client.send_message(group['id'], text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(3)
    await client.disconnect()
    return sent, failed


async def user_broadcast_loop(user_id):
    while user_broadcasts.get(user_id, False):
        try:
            messages = load_user_json(user_id, "messages.json", {})
            text = messages.get("message", "")

            if text:
                sent, failed = await send_user_to_all(user_id, text)
                logger.info(f"Пользователь {user_id}: отправлено {sent}, ошибок {failed}")
            else:
                logger.warning(f"Пользователь {user_id}: нет сообщения для рассылки")

            await asyncio.sleep(600)  # 10 минут

        except Exception as e:
            logger.error(f"Ошибка для пользователя {user_id}: {e}")
            await asyncio.sleep(60)


# ════════════════════════════════════════
# 🎯 ГЕНЕРАЦИЯ STRING SESSION
# ════════════════════════════════════════

async def generate_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📱 <b>Генерация String Session</b>\n\n"
        "Введите номер телефона в формате:\n"
        "<code>+79830698459</code>\n\n"
        "❗ Не забудьте + в начале",
        parse_mode='HTML'
    )
    return PHONE


async def generate_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    context.user_data['phone'] = phone

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()

    try:
        await client.send_code_request(phone)
        context.user_data['client'] = client

        await update.message.reply_text(
            "📨 <b>Код отправлен!</b>\n\n"
            "Введите код из Telegram:",
            parse_mode='HTML'
        )
        return CODE
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}\nПопробуйте /start")
        return ConversationHandler.END


async def generate_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    client = context.user_data['client']
    phone = context.user_data['phone']

    try:
        await client.sign_in(phone, code)

        session_string = client.session.save()

        await update.message.reply_text(
            f"✅ <b>String Session готов!</b>\n\n"
            f"<code>{session_string}</code>\n\n"
            "📌 Скопируйте эту строку.\n"
            "Чтобы использовать её, напишите:\n"
            "<code>/setsession СКОПИРОВАННАЯ_СТРОКА</code>",
            parse_mode='HTML'
        )

        await client.disconnect()
        return ConversationHandler.END

    except Exception as e:
        if "password" in str(e).lower():
            await update.message.reply_text(
                "🔐 <b>Введите пароль от аккаунта</b>\n\n"
                "У вас включена двухфакторная аутентификация:",
                parse_mode='HTML'
            )
            return PASSWORD
        else:
            await update.message.reply_text(f"❌ Ошибка: {e}\nПопробуйте /start")
            return ConversationHandler.END


async def generate_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    client = context.user_data['client']

    try:
        await client.sign_in(password=password)

        session_string = client.session.save()

        await update.message.reply_text(
            f"✅ <b>String Session готов!</b>\n\n"
            f"<code>{session_string}</code>\n\n"
            "📌 Скопируйте эту строку.\n"
            "Чтобы использовать её, напишите:\n"
            "<code>/setsession СКОПИРОВАННАЯ_СТРОКА</code>",
            parse_mode='HTML'
        )

        await client.disconnect()
        return ConversationHandler.END

    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}\nПопробуйте /start")
        return ConversationHandler.END


# ════════════════════════════════════════
# 🤖 КОМАНДЫ БОТА
# ════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    keyboard = [
        [InlineKeyboardButton("🔑 Получить String Session", callback_data="how_to_get_session")],
        [InlineKeyboardButton("▶️ Запустить рассылку", callback_data="start_broadcast")],
        [InlineKeyboardButton("⏹ Остановить рассылку", callback_data="stop_broadcast")],
        [InlineKeyboardButton("📝 Изменить сообщение", callback_data="edit_message")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("🔄 Сменить аккаунт", callback_data="change_account")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    status = "🟢 РАБОТАЕТ" if user_broadcasts.get(user_id, False) else "🔴 ОСТАНОВЛЕН"
    groups = await get_user_groups(user_id)
    messages = load_user_json(user_id, "messages.json", {})
    current_message = messages.get("message", "")

    await update.message.reply_text(
        f"🤖 <b>Бот для рассылок</b>\n\n"
        f"Статус: {status}\n"
        f"Групп: {len(groups)}\n\n"
        f"📨 <b>Текущее сообщение:</b>\n"
        f"{current_message[:200]}{'...' if len(current_message) > 200 else ''}\n\n"
        f"📌 Чтобы изменить сообщение, нажмите 'Изменить сообщение'",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )


async def set_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text(
            "❌ <b>Как использовать:</b>\n\n"
            "/setsession НОВЫЙ_STRING_SESSION\n\n"
            "Получить String Session можно по ссылке:\n"
            "https://gabrielmaialva33.github.io/ssg/\n\n"
            f"📌 <b>Данные для входа:</b>\n"
            f"API ID: <code>{API_ID}</code>\n"
            f"API HASH: <code>{API_HASH}</code>",
            parse_mode='HTML'
        )
        return

    new_session = " ".join(context.args)

    if len(new_session) < 20:
        await update.message.reply_text("❌ Строка слишком короткая!")
        return

    was_running = user_broadcasts.get(user_id, False)
    if was_running:
        user_broadcasts[user_id] = False
        if user_id in user_tasks:
            user_tasks[user_id].cancel()
        await asyncio.sleep(2)

    save_user_session(user_id, new_session)

    try:
        groups = await get_user_groups(user_id)
        await update.message.reply_text(
            f"✅ <b>Аккаунт обновлён!</b>\n\n"
            f"📊 Найдено групп: {len(groups)}",
            parse_mode='HTML'
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
        return

    if was_running:
        user_broadcasts[user_id] = True
        task = asyncio.create_task(user_broadcast_loop(user_id))
        user_tasks[user_id] = task
        await update.message.reply_text("✅ Рассылка перезапущена!")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    query = update.callback_query
    await query.answer()

    if query.data == "how_to_get_session":
        await query.edit_message_text(
            "🔑 <b>Как получить String Session</b>\n\n"
            "Перейдите по ссылке:\n"
            "https://gabrielmaialva33.github.io/ssg/\n\n"
            "Введите данные:\n"
            f"• API ID: <code>{API_ID}</code>\n"
            f"• API HASH: <code>{API_HASH}</code>\n"
            "• Номер телефона (с +)\n\n"
            "Получите код в Telegram и введите его\n\n"
            "Скопируйте полученный String Session\n\n"
            "Отправьте боту:\n"
            "<code>/setsession СКОПИРОВАННАЯ_СТРОКА</code>",
            parse_mode='HTML'
        )

    elif query.data == "change_account":
        await query.edit_message_text(
            "<b>Смена аккаунта</b>\n\n"
            "Получить String Session:\n"
            "https://gabrielmaialva33.github.io/ssg/\n\n"
            f"📌 Ваши данные для входа:\n"
            f"API ID: <code>{API_ID}</code>\n"
            f"API HASH: <code>{API_HASH}</code>\n\n"
            "Отправить боту:\n"
            "<code>/setsession НОВЫЙ_STRING_SESSION</code>",
            parse_mode='HTML'
        )

    elif query.data == "start_broadcast":
        if user_broadcasts.get(user_id, False):
            await query.edit_message_text("⚠️ Рассылка уже запущена!")
            return

        if not get_user_session(user_id):
            await query.edit_message_text(
                "❌ Сначала установите String Session!\n"
                "Нажмите '🔑 Получить String Session'"
            )
            return

        user_broadcasts[user_id] = True
        save_user_json(user_id, "settings.json", {**load_user_json(user_id, "settings.json", {}), "is_running": True})

        task = asyncio.create_task(user_broadcast_loop(user_id))
        user_tasks[user_id] = task

        await query.edit_message_text("✅ Рассылка ЗАПУЩЕНА!")

    elif query.data == "stop_broadcast":
        user_broadcasts[user_id] = False
        if user_id in user_tasks:
            user_tasks[user_id].cancel()
        save_user_json(user_id, "settings.json", {**load_user_json(user_id, "settings.json", {}), "is_running": False})
        await query.edit_message_text("⏹ Рассылка ОСТАНОВЛЕНА!")

    elif query.data == "stats":
        messages = load_user_json(user_id, "messages.json", {})
        groups = await get_user_groups(user_id)
        await query.edit_message_text(
            f"📊 <b>Статистика</b>\n\n"
            f"📁 Групп: {len(groups)}\n"
            f"📨 Сообщение: {len(messages.get('message', ''))} символов\n"
            f"🔄 Статус: {'🟢 Работает' if user_broadcasts.get(user_id, False) else '🔴 Остановлен'}",
            parse_mode='HTML'
        )

    elif query.data == "edit_message":
        context.user_data['editing'] = 'message'
        await query.edit_message_text(
            "📝 Напишите НОВОЕ сообщение для рассылки\n\n"
            "Оно будет отправляться во все ваши группы каждые 10 минут."
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if update.effective_chat.type != "private":
        return

    if 'editing' in context.user_data:
        key = context.user_data['editing']
        messages = load_user_json(user_id, "messages.json", {})
        messages[key] = update.message.text
        save_user_json(user_id, "messages.json", messages)
        await update.message.reply_text("✅ Сообщение сохранено! Оно будет рассылаться каждые 10 минут.")
        del context.user_data['editing']
        return


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("generate", generate_start)],
        states={
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, generate_phone)],
            CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, generate_code)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, generate_password)],
        },
        fallbacks=[CommandHandler("start", start)]
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setsession", set_session))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🚀 Бот запущен!")
    logger.info("📨 Только одно сообщение для рассылки")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()