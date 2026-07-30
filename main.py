import asyncio
from datetime import datetime, timezone, timedelta
import logging
import os
import random
import shutil
import string
import uuid
import zipfile

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
import aiosqlite
from aiohttp import web  # اضافه شده برای ایجاد Web Service در Render

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

SUPER_ADMIN_1 = 8490505070
SUPER_ADMIN_2 = 475473068  # ادمین دوم با دسترسی کامل

# لیست سوپرادمین‌ها (از دیتابیس بارگذاری و به‌روزرسانی می‌شود)
SUPER_ADMINS: set[int] = {SUPER_ADMIN_1, SUPER_ADMIN_2}

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "atr_bank.db"
BACKUP_DIR = "backups"
MAX_BALANCE_LIMIT = 1000000000  # سقف ۱ میلیارد آتر
USERS_PER_PAGE = 5  # تعداد کاربران در هر صفحه پنل مدیریت

# ⚙️ آیدی عددی کانال خصوصی بکاپ تلگرام شما
BACKUP_CHANNEL_ID = -1003971216432

# قفل هم‌روندی ناهمگام برای ایمن‌سازی تراکنش‌های مالی در برابر Race Condition
db_lock = asyncio.Lock()


# --- وب‌سرور سبک برای ساخت Web Service در Render ---
async def handle_health_check(request):
    return web.Response(text="Atr Bank Bot is Running Successfully!")


async def start_dummy_server():
    app = web.Application()
    app.router.add_get("/", handle_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"🌐 Dummy Web Server started on port {port}")


class TxForm(StatesGroup):
    waiting_for_to_user = State()
    waiting_for_amount = State()
    waiting_for_confirm = State()


class AdminConfirmForm(StatesGroup):
    waiting_for_confirm = State()


class AntiSpamMiddleware(BaseMiddleware):

    def __init__(self, limit=1.5):
        self.limit = limit
        self.users = {}
        super().__init__()

    async def __call__(self, handler, event, data):
        user_id = event.from_user.id
        now = datetime.now().timestamp()
        if user_id in self.users:
            if now - self.users[user_id] < self.limit:
                if isinstance(event, Message):
                    await event.reply(
                        "⚠️ لطفاً از اسپم خودداری کنید! کمی آرام‌تر."
                    )
                return
        self.users[user_id] = now
        return await handler(event, data)


# --- ساخت دیتابیس و جدول‌ها ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                balance INTEGER DEFAULT 0, 
                is_admin BOOLEAN DEFAULT FALSE,
                is_frozen BOOLEAN DEFAULT FALSE,
                group_name TEXT DEFAULT 'Default'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                tx_id TEXT PRIMARY KEY,
                timestamp TEXT,
                from_user INTEGER,
                to_user INTEGER,
                amount INTEGER,
                reason TEXT,
                status TEXT DEFAULT 'SUCCESS'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                group_name TEXT PRIMARY KEY
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS group_links (
                code TEXT PRIMARY KEY,
                group_name TEXT,
                expires_at TEXT,
                created_at TEXT,
                FOREIGN KEY(group_name) REFERENCES groups(group_name) ON DELETE CASCADE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS super_admins (
                user_id INTEGER PRIMARY KEY
            )
        """)

        # فقط گروه پیش‌فرض Default ثبت می‌شود
        for g in ["Default"]:
            await db.execute(
                "INSERT OR IGNORE INTO groups (group_name) VALUES (?)", (g,)
            )

        # ثبت سوپرادمین‌های پایه
        for sa_id in [SUPER_ADMIN_1, SUPER_ADMIN_2]:
            await db.execute(
                "INSERT OR IGNORE INTO super_admins (user_id) VALUES (?)", (sa_id,)
            )

        await db.commit()

        # بارگذاری لیست سوپرادمین‌ها از دیتابیس
        await load_super_admins(db)


async def load_super_admins(db=None):
    """بارگذاری لیست سوپرادمین‌ها از دیتابیس به متغیر سراسری"""
    global SUPER_ADMINS
    close_after = False
    if db is None:
        db = await aiosqlite.connect(DB_PATH)
        close_after = True
    try:
        async with db.execute("SELECT user_id FROM super_admins") as cur:
            rows = await cur.fetchall()
            SUPER_ADMINS = {row[0] for row in rows}
            # همیشه دو آیدی پایه را نگه دار
            SUPER_ADMINS.add(SUPER_ADMIN_1)
            SUPER_ADMINS.add(SUPER_ADMIN_2)
    finally:
        if close_after:
            await db.close()


async def sync_user(user_id: int, username: str, full_name: str = "Unknown"):
    async with aiosqlite.connect(DB_PATH) as db:
        username_esc = (
            username.replace("_", "\\_").replace("*", "\\*")
            if username
            else "بدون آیدی"
        )
        full_name_esc = (
            full_name.replace("_", "\\_").replace("*", "\\*")
            if full_name
            else "ناشناس"
        )
        await db.execute(
            """
            INSERT INTO users (user_id, username, full_name, balance) VALUES (?, ?, ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET username = ?, full_name = ?
        """,
            (user_id, username_esc, full_name_esc, username_esc, full_name_esc),
        )
        await db.commit()


async def get_user_data(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT balance, is_admin, is_frozen, username, full_name, group_name FROM"
            " users WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            return await cursor.fetchone()


user_router = Router()
admin_router = Router()
user_router.message.middleware(AntiSpamMiddleware())


async def check_admin_filter(message: Message) -> bool:
    if is_super_admin(message.from_user.id):
        return True
    u = await get_user_data(message.from_user.id)
    return u and u["is_admin"]


def is_super_admin(user_id: int) -> bool:
    return user_id in SUPER_ADMINS


def is_private(message: Message) -> bool:
    return message.chat.type == "private"


# --- ایجاد پوشه بکاپ ---
os.makedirs(BACKUP_DIR, exist_ok=True)


def create_zip_backup(prefix="manual"):
    if not os.path.exists(DB_PATH):
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_name = f"{prefix}_backup_{timestamp}.zip"
    zip_path = os.path.join(BACKUP_DIR, zip_name)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        zipf.write(DB_PATH, arcname="atr_bank.db")
    return zip_path


# --- سیستم بکاپ‌گیری و بازیابی خودکار تلگرامی ---
async def restore_db_from_telegram(bot: Bot):
    if os.path.exists(DB_PATH) and os.path.getsize(DB_PATH) > 0:
        logging.info("✅ فایل دیتابیس موجود است.")
        return

    logging.info("⚠️ دیتابیس روی سرور یافت نشد! در حال دانلود آخرین بکاپ از کانال تلگرام...")
    try:
        async for message in bot.get_chat_history(BACKUP_CHANNEL_ID, limit=15):
            if message.document and message.document.file_name.endswith(".db"):
                file_info = await bot.get_file(message.document.file_id)
                await bot.download_file(file_info.file_path, DB_PATH)
                logging.info("🎉 دیتابیس با موفقیت از کانال تلگرام بازیابی شد!")
                return
        logging.info("ℹ️ هیچ فایل بکاپی در کانال یافت نشد. دیتابیس جدید ساخته خواهد شد.")
    except Exception as e:
        logging.error(f"❌ خطا در بازیابی دیتابیس از تلگرام: {e}")


async def auto_backup_loop(bot: Bot):
    while True:
        await asyncio.sleep(3600)  # ارسال بکاپ خودکار هر ۱ ساعت
        if os.path.exists(DB_PATH):
            try:
                await bot.send_document(
                    chat_id=BACKUP_CHANNEL_ID,
                    document=FSInputFile(DB_PATH),
                    caption=f"📦 **بکاپ خودکار دیتابیس**\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                logging.info("✅ بکاپ خودکار به کانال تلگرام ارسال شد.")
            except Exception as e:
                logging.error(f"❌ خطا در ارسال بکاپ خودکار به تلگرام: {e}")


# --- ساخت صفحه کاربران ---
async def get_users_page(page: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT COUNT(*) as total FROM users") as cur:
            total_users = (await cur.fetchone())["total"]

        total_pages = max(1, (total_users + USERS_PER_PAGE - 1) // USERS_PER_PAGE)
        page = max(1, min(page, total_pages))
        offset = (page - 1) * USERS_PER_PAGE

        async with db.execute(
            "SELECT user_id, username, full_name, balance, group_name, is_frozen, is_admin FROM users LIMIT ? OFFSET ?",
            (USERS_PER_PAGE, offset),
        ) as cur:
            users = await cur.fetchall()

    text = f"👥 **لیست کاربران (صفحه {page} از {total_pages})**\n"
    text += f"📊 کل کاربران: `{total_users}` نفر\n\n"

    for idx, u in enumerate(users, start=offset + 1):
        text += (
            f"**{idx}. {u['full_name']}**\n"
            f"شماره حساب: `{u['user_id']}`\n"
            f"موجودی: `₳ {u['balance']}`\n"
            f"گروه: **{u['group_name']}**\n"
            f"------------------------------\n"
        )

    buttons = []
    nav_row = []

    if page > 1:
        nav_row.append(InlineKeyboardButton(text="➡️ قبلی", callback_data=f"users_page_{page - 1}"))
    
    nav_row.append(InlineKeyboardButton(text=f"📄 {page}/{total_pages}", callback_data="users_noop"))

    if page < total_pages:
        nav_row.append(InlineKeyboardButton(text="بعدی ⬅️", callback_data=f"users_page_{page + 1}"))

    if nav_row:
        buttons.append(nav_row)

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    return text, kb


# --- دستورات کاربران ---


@user_router.message(Command("start"))
async def cmd_start(message: Message):
    # فقط در چت خصوصی کار می‌کند
    if not is_private(message):
        return

    user_id = message.from_user.id
    username = message.from_user.username
    full_name = message.from_user.full_name

    # چک کنیم آیا کاربر قبلاً در دیتابیس بوده یا نه (برای تشخیص اولین بار)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            already_exists = await cur.fetchone() is not None

    await sync_user(user_id, username, full_name)

    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        payload = args[1].strip()

        # اگر payload با G شروع شود، آن را به عنوان کد دعوت در نظر بگیر
        if payload.upper().startswith("G"):
            # پشتیبانی از هر دو فرمت: G_XXXX و GXXXX
            if payload.startswith("G_") or payload.startswith("g_"):
                code = payload[2:].strip()
            else:
                code = payload[1:].strip()

            if not code:
                return await message.reply("❌ کد دعوت نامعتبر است.")

            try:
                async with db_lock:
                    async with aiosqlite.connect(DB_PATH) as db:
                        db.row_factory = aiosqlite.Row

                        # خواندن لینک (با و بدون ستون expires_at)
                        link_data = None
                        try:
                            async with db.execute(
                                "SELECT group_name, expires_at FROM group_links WHERE code = ?",
                                (code,),
                            ) as cur:
                                link_data = await cur.fetchone()
                        except Exception:
                            async with db.execute(
                                "SELECT group_name FROM group_links WHERE code = ?",
                                (code,),
                            ) as cur:
                                row = await cur.fetchone()
                                if row:
                                    link_data = {
                                        "group_name": row[0] if not hasattr(row, "keys") else row["group_name"],
                                        "expires_at": None,
                                    }

                        if not link_data:
                            # برای دیباگ: کد استخراج‌شده را نشان بده
                            return await message.reply(
                                f"❌ لینک دعوت نامعتبر است.\n"
                                f"کد دریافتی: `{code}`",
                                parse_mode="Markdown",
                            )

                        # تبدیل به دیکشنری ساده برای دسترسی امن
                        if hasattr(link_data, "keys"):
                            group_name = link_data["group_name"]
                            expires_val = link_data["expires_at"] if "expires_at" in link_data.keys() else None
                        else:
                            group_name = link_data.get("group_name")
                            expires_val = link_data.get("expires_at")

                        # چک انقضا
                        if expires_val:
                            try:
                                expires = datetime.fromisoformat(str(expires_val))
                                if datetime.now(timezone.utc) > expires:
                                    return await message.reply("❌ لینک دعوت منقضی شده است.")
                            except Exception:
                                pass

                        u = await get_user_data(user_id)
                        if u and u["is_frozen"]:
                            return await message.reply("❌ حساب شما فریز است.")

                        await db.execute(
                            "UPDATE users SET group_name = ? WHERE user_id = ?",
                            (group_name, user_id),
                        )
                        await db.commit()

                        return await message.reply(
                            f"🎉 شما با موفقیت عضو گروه **{group_name}** شدید.",
                            parse_mode="Markdown",
                        )
            except Exception as e:
                logging.error(f"start group link error: {e}")
                return await message.reply(
                    f"❌ خطا در عضویت گروه:\n`{type(e).__name__}: {e}`\n"
                    f"کد: `{code}`",
                    parse_mode="Markdown",
                )

    # اولین بار: خوش‌آمد + شماره حساب | دفعات بعد: فقط شماره حساب
    if not already_exists:
        await message.reply(
            f"به بانک جادویی Atramentum خوش اومدید.\n"
            f"شماره حساب: `{user_id}`",
            parse_mode="Markdown",
        )
    else:
        await message.reply(
            f"شماره حساب: `{user_id}`",
            parse_mode="Markdown",
        )


@user_router.message(Command("profile"))
@user_router.message(Command("balance"))
@user_router.message(F.text == "پروفایل")
async def cmd_profile(message: Message):
    user_id = message.from_user.id
    await sync_user(user_id, message.from_user.username, message.from_user.full_name)
    u = await get_user_data(user_id)

    if not u:
        return await message.reply("❌ حساب شما یافت نشد.")

    status_text = "❄️ فریز شده" if u["is_frozen"] else "🟢 فعال"

    await message.reply(
        f"👤 نام: {u['full_name']}\n"
        f"🆔 شماره حساب: `{user_id}`\n"
        f"💰 موجودی: `₳ {u['balance']}`\n"
        f"⚡ وضعیت حساب: {status_text}",
        parse_mode="Markdown",
    )


# --- سیستم انتقال آتر (چند روشه) ---

async def process_transfer_request(message: Message, state: FSMContext, to_user_id: int, amount: int):
    """تابع کمکی برای شروع تأیید انتقال"""
    from_user = message.from_user.id
    u = await get_user_data(from_user)

    if not u or u["is_frozen"]:
        return await message.reply("❌ حساب شما مسدود (فریز) است.")
    if amount <= 0 or amount > MAX_BALANCE_LIMIT or to_user_id == from_user or u["balance"] < amount:
        return await message.reply("❌ پارامترهای تراکنش یا موجودی نامعتبر است.")

    target = await get_user_data(to_user_id)
    if not target:
        return await message.reply("❌ کاربر مقصد در ربات عضویت ندارد.")
    if target["balance"] + amount > MAX_BALANCE_LIMIT:
        return await message.reply("❌ خطا: سقف گنجایش مقصد.")

    target_name = target["full_name"] if target["full_name"] else str(to_user_id)

    await state.update_data(
        to_user_id=to_user_id, amount=amount, target_name=target_name, from_user=from_user
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ تایید", callback_data="tx_yes"),
            InlineKeyboardButton(text="❌ انصراف", callback_data="tx_no"),
        ]]
    )
    await message.reply(
        f"⚠️ تأییدیه انتقال آتر\n"
        f"دریافت‌کننده: {target_name} (`{to_user_id}`)\n"
        f"مبلغ: `₳ {amount}`\n"
        f"آیا مطمئن هستید؟",
        reply_markup=kb,
        parse_mode="Markdown",
    )
    await state.set_state(TxForm.waiting_for_confirm)


@user_router.message(Command("transfer"))
@user_router.message(F.text.startswith("انتقال آتر"))
async def cmd_transfer(message: Message, state: FSMContext):
    from_user = message.from_user.id
    await sync_user(from_user, message.from_user.username, message.from_user.full_name)
    u = await get_user_data(from_user)
    if u and u["is_frozen"]:
        return await message.reply("❌ حساب شما مسدود (فریز) است.")

    text = message.text.strip()
    # حذف پیشوند
    if text.startswith("/transfer"):
        text = text[len("/transfer"):].strip()
    elif text.startswith("انتقال آتر"):
        text = text[len("انتقال آتر"):].strip()

    # حالت ریپلای
    if message.reply_to_message and message.reply_to_message.from_user:
        try:
            amount = int(text)
            to_user_id = message.reply_to_message.from_user.id
            return await process_transfer_request(message, state, to_user_id, amount)
        except ValueError:
            return await message.reply("❌ مبلغ باید عدد باشد.")

    parts = text.split()
    if len(parts) == 0:
        # حالت تعاملی: اول شماره حساب بپرس
        await message.reply("لطفاً شماره حساب (آیدی عددی) فرد مقصد را وارد کنید:")
        await state.set_state(TxForm.waiting_for_to_user)
        return

    if len(parts) >= 2:
        # حالت مستقیم: آیدی یا @یوزرنیم + مبلغ
        target_raw = parts[0]
        try:
            amount = int(parts[1])
        except ValueError:
            return await message.reply("❌ مبلغ باید عدد باشد.")

        to_user_id = None
        if target_raw.startswith("@"):
            # جستجو با یوزرنیم
            username = target_raw[1:]
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT user_id FROM users WHERE username = ? OR username = ?",
                    (username, f"\\_{username}" if False else username),  # ساده
                ) as cur:
                    row = await cur.fetchone()
                    if row:
                        to_user_id = row["user_id"]
            if not to_user_id:
                # تلاش ساده بدون escape
                async with aiosqlite.connect(DB_PATH) as db:
                    db.row_factory = aiosqlite.Row
                    async with db.execute(
                        "SELECT user_id FROM users WHERE username LIKE ?",
                        (f"%{username}%",),
                    ) as cur:
                        row = await cur.fetchone()
                        if row:
                            to_user_id = row["user_id"]
            if not to_user_id:
                return await message.reply("❌ کاربری با این آیدی یافت نشد.")
        else:
            try:
                to_user_id = int(target_raw)
            except ValueError:
                return await message.reply("❌ شماره حساب باید عدد باشد.")

        return await process_transfer_request(message, state, to_user_id, amount)

    # اگر فقط یک قسمت بود، شاید آیدی باشد و بعد مبلغ بپرسد
    try:
        to_user_id = int(parts[0])
        await state.update_data(to_user_id=to_user_id)
        await message.reply("مبلغ را وارد کنید:")
        await state.set_state(TxForm.waiting_for_amount)
    except ValueError:
        await message.reply("لطفاً شماره حساب (آیدی عددی) فرد مقصد را وارد کنید:")
        await state.set_state(TxForm.waiting_for_to_user)


@user_router.message(TxForm.waiting_for_to_user)
async def process_to_user(message: Message, state: FSMContext):
    try:
        to_user_id = int(message.text.strip())
    except ValueError:
        return await message.reply("❌ شماره حساب باید عدد باشد. دوباره وارد کنید:")
    await state.update_data(to_user_id=to_user_id)
    await message.reply("مبلغ را وارد کنید:")
    await state.set_state(TxForm.waiting_for_amount)


@user_router.message(TxForm.waiting_for_amount)
async def process_amount(message: Message, state: FSMContext):
    try:
        amount = int(message.text.strip())
    except ValueError:
        return await message.reply("❌ مبلغ باید عدد باشد. دوباره وارد کنید:")
    data = await state.get_data()
    to_user_id = data.get("to_user_id")
    if not to_user_id:
        await state.clear()
        return await message.reply("❌ خطا. دوباره از اول شروع کنید.")
    await process_transfer_request(message, state, to_user_id, amount)


@user_router.callback_query(TxForm.waiting_for_confirm, F.data == "tx_yes")
async def confirm_transfer_cb(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    from_user = data.get("from_user") or callback.from_user.id
    # فقط خود فرستنده بتواند تأیید کند
    if callback.from_user.id != from_user:
        return await callback.answer("❌ فقط انتقال‌دهنده می‌تواند تأیید کند.", show_alert=True)

    await state.clear()
    to_user_id = data["to_user_id"]
    amount = data["amount"]
    target_name = data.get("target_name", "کاربر مقصد")

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT balance, is_frozen FROM users WHERE user_id = ?",
                (from_user,),
            ) as cur:
                s = await cur.fetchone()
            async with db.execute(
                "SELECT balance FROM users WHERE user_id = ?", (to_user_id,)
            ) as cur2:
                r = await cur2.fetchone()

            if (
                not s
                or s["is_frozen"]
                or s["balance"] < amount
                or not r
                or (r["balance"] + amount > MAX_BALANCE_LIMIT)
            ):
                return await callback.message.edit_text(
                    "❌ خطا در هم‌روندی یا وضعیت حساب؛ تراکنش لغو شد."
                )

            tx_id = (
                f"TX-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{random.randint(100000, 999999)}"
            )
            await db.execute(
                "UPDATE users SET balance = balance - ? WHERE user_id = ?",
                (amount, from_user),
            )
            await db.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                (amount, to_user_id),
            )
            await db.execute(
                """
                INSERT INTO audit_logs (tx_id, timestamp, from_user, to_user, amount, reason)
                VALUES (?, ?, ?, ?, ?, 'انتقال کاربر به کاربر')
            """,
                (
                    tx_id,
                    datetime.now(timezone.utc).isoformat(),
                    from_user,
                    to_user_id,
                    amount,
                ),
            )
            await db.commit()

    await callback.message.edit_text(
        f"✅ تراکنش با موفقیت انجام شد!\n"
        f"به نام: **{target_name}**\n"
        f"شناسه: `{tx_id}`\n"
        f"مبلغ: `₳ {amount}`",
        parse_mode="Markdown",
    )

    # ارسال رسید خصوصی به هر دو طرف
    sender_data = await get_user_data(from_user)
    sender_name = sender_data["full_name"] if sender_data else str(from_user)

    try:
        await callback.bot.send_message(
            from_user,
            f"📤 **رسید انتقال**\n\n"
            f"شما `₳ {amount}` به **{target_name}** (`{to_user_id}`) انتقال دادید.\n"
            f"🔖 شناسه تراکنش: `{tx_id}`",
            parse_mode="Markdown",
        )
    except Exception:
        pass

    try:
        await callback.bot.send_message(
            to_user_id,
            f"📥 **رسید دریافت**\n\n"
            f"شما `₳ {amount}` از **{sender_name}** (`{from_user}`) دریافت کردید.\n"
            f"🔖 شناسه تراکنش: `{tx_id}`",
            parse_mode="Markdown",
        )
    except Exception:
        pass


@user_router.callback_query(TxForm.waiting_for_confirm, F.data == "tx_no")
async def cancel_transfer_cb(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    from_user = data.get("from_user") or callback.from_user.id
    if callback.from_user.id != from_user:
        return await callback.answer("❌ فقط انتقال‌دهنده می‌تواند لغو کند.", show_alert=True)
    await state.clear()
    await callback.message.edit_text("❌ انتقال وجه لغو شد.")


# --- بخش مدیریت و ادمین ---


@admin_router.message(Command("users"))
async def cmd_users(message: Message):
    if not is_private(message) or not await check_admin_filter(message):
        return

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id, full_name, balance, group_name FROM users ORDER BY user_id"
        ) as cur:
            users = await cur.fetchall()

    if not users:
        return await message.reply("هیچ کاربری یافت نشد.")

    text = f"👥 **لیست تمام کاربران** (`{len(users)}` نفر)\n\n"
    for idx, u in enumerate(users, start=1):
        text += (
            f"**{idx}. {u['full_name']}**\n"
            f"شماره حساب: `{u['user_id']}`\n"
            f"موجودی: `₳ {u['balance']}`\n"
            f"گروه: **{u['group_name']}**\n"
            f"------------------------------\n"
        )

    # اگر متن خیلی طولانی شد، به چند پیام تقسیم کن
    if len(text) > 4000:
        parts = []
        current = f"👥 **لیست تمام کاربران** (`{len(users)}` نفر)\n\n"
        for idx, u in enumerate(users, start=1):
            chunk = (
                f"**{idx}. {u['full_name']}**\n"
                f"شماره حساب: `{u['user_id']}`\n"
                f"موجودی: `₳ {u['balance']}`\n"
                f"گروه: **{u['group_name']}**\n"
                f"------------------------------\n"
            )
            if len(current) + len(chunk) > 4000:
                parts.append(current)
                current = chunk
            else:
                current += chunk
        if current:
            parts.append(current)
        for part in parts:
            await message.reply(part, parse_mode="Markdown")
    else:
        await message.reply(text, parse_mode="Markdown")


@admin_router.callback_query(F.data.startswith("users_page_"))
async def cb_users_page(callback: CallbackQuery):
    if not await check_admin_filter(callback.message):
        return await callback.answer("عدم دسترسی.", show_alert=True)

    page = int(callback.data.split("_")[2])
    text, kb = await get_users_page(page)
    
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        pass
    await callback.answer()


@admin_router.callback_query(F.data == "users_noop")
async def cb_users_noop(callback: CallbackQuery):
    await callback.answer()


@admin_router.message(Command("create_group"))
async def cmd_create_group(message: Message):
    """فقط گروه را به لیست اضافه می‌کند (بدون ساخت لینک)"""
    if not is_private(message) or not await check_admin_filter(message):
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply(
            "⚠️ راهنما:\n"
            "/create_group [نام_گروه]\n\n"
            "مثال:\n"
            "/create_group ترم۱"
        )

    g_name = args[1].strip()
    if not g_name:
        return await message.reply("❌ نام گروه نمی‌تواند خالی باشد.")

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM groups WHERE group_name = ?", (g_name,)
        )
        exists = await cursor.fetchone()
        if exists:
            return await message.reply(f"ℹ️ گروه **{g_name}** از قبل وجود دارد.")

        await db.execute(
            "INSERT INTO groups (group_name) VALUES (?)", (g_name,)
        )
        await db.commit()

    await message.reply(
        f"✅ گروه **{g_name}** با موفقیت به لیست گروه‌ها اضافه شد.\n"
        f"(هیچ لینکی ساخته نشد)",
        parse_mode="Markdown",
    )


@admin_router.message(Command("add_group"))
async def cmd_add_group(message: Message):
    if not is_private(message) or not await check_admin_filter(message):
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply("راهنما: /add_group [نام_گروه]")

    g_name = args[1].strip()
    code = "".join(
        random.choices(string.ascii_uppercase + string.digits, k=10)
    )

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR IGNORE INTO groups (group_name) VALUES (?)", (g_name,)
            )
            await db.execute(
                "INSERT INTO group_links (code, group_name) VALUES (?, ?)",
                (code, g_name),
            )
            await db.commit()

    bot_info = await message.bot.get_me()
    link = f"https://t.me/{bot_info.username}?start=G_{code}"

    await message.reply(
        f"✅ گروه {g_name} با موفقیت ساخته شد.\n\n🔗 **لینک عضویت"
        f" اختصاصی:**\n{link}",
        parse_mode="Markdown",
    )


@admin_router.message(Command("extend_group"))
async def cmd_extend_group(message: Message):
    if not is_private(message) or not await check_admin_filter(message):
        return

    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        return await message.reply(
            "راهنما: /extend_group [نام_گروه] [تعداد_روز]\n"
            "مثال: /extend_group ترم۱ 2"
        )

    g_name = args[1].strip()
    days_str = args[2].strip()

    if not days_str.isdigit() or int(days_str) <= 0:
        return await message.reply("❌ تعداد روز باید عدد مثبت باشد.")

    extra_days = int(days_str)

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            # پیدا کردن آخرین لینک این گروه
            cursor = await db.execute(
                "SELECT code FROM group_links WHERE group_name = ? ORDER BY rowid DESC LIMIT 1",
                (g_name,),
            )
            row = await cursor.fetchone()
            if not row:
                return await message.reply(f"❌ گروهی با نام {g_name} یا لینکی برای آن پیدا نشد.")

            old_code = row[0]

            # اگر ستون expires_at وجود ندارد اضافه کن
            try:
                await db.execute("ALTER TABLE group_links ADD COLUMN expires_at TEXT")
            except Exception:
                pass

            # محاسبه تاریخ انقضای جدید (از الان + روزها)
            new_expires = (datetime.now(timezone.utc) + timedelta(days=extra_days)).isoformat()
            await db.execute(
                "UPDATE group_links SET expires_at = ? WHERE code = ?",
                (new_expires, old_code),
            )
            await db.commit()

    bot_info = await message.bot.get_me()
    link = f"https://t.me/{bot_info.username}?start=G_{old_code}"

    await message.reply(
        f"✅ لینک گروه {g_name} به مدت {extra_days} روز تمدید شد.\n\n"
        f"🔗 لینک:\n{link}",
        parse_mode="Markdown",
    )


@admin_router.message(Command("renew_group"))
async def cmd_renew_group(message: Message):
    if not is_private(message) or not await check_admin_filter(message):
        return

    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        return await message.reply(
            "راهنما: /renew_group [نام_گروه] [تعداد_روز]\n"
            "مثال: /renew_group ترم۱ 30"
        )

    g_name = args[1].strip()
    days_str = args[2].strip()

    if not days_str.isdigit() or int(days_str) <= 0:
        return await message.reply("❌ تعداد روز باید عدد مثبت باشد.")

    days = int(days_str)
    new_code = "".join(random.choices(string.ascii_uppercase + string.digits, k=10))
    expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT 1 FROM groups WHERE group_name = ?", (g_name,)
            )
            if not await cursor.fetchone():
                return await message.reply(f"❌ گروهی با نام {g_name} پیدا نشد.")

            try:
                await db.execute("ALTER TABLE group_links ADD COLUMN expires_at TEXT")
            except Exception:
                pass
            try:
                await db.execute("ALTER TABLE group_links ADD COLUMN created_at TEXT")
            except Exception:
                pass

            await db.execute(
                "INSERT INTO group_links (code, group_name, expires_at) VALUES (?, ?, ?)",
                (new_code, g_name, expires_at),
            )
            await db.commit()

    bot_info = await message.bot.get_me()
    link = f"https://t.me/{bot_info.username}?start=G_{new_code}"

    await message.reply(
        f"✅ لینک جدید برای گروه {g_name} ساخته شد.\n"
        f"مدت اعتبار: {days} روز\n\n"
        f"🔗 لینک جدید:\n{link}",
        parse_mode="Markdown",
    )


@admin_router.message(Command("rename_group"))
async def cmd_rename_group(message: Message):
    if not is_private(message) or not await check_admin_filter(message):
        return
    args = message.text.split()
    if len(args) < 3:
        return await message.reply("استفاده: `/rename_group [قدیمی] [جدید]`")
    old_n, new_n = args[1], args[2]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO groups (group_name) VALUES (?)", (new_n,)
        )
        await db.execute(
            "UPDATE users SET group_name = ? WHERE group_name = ?",
            (new_n, old_n),
        )
        await db.execute("DELETE FROM groups WHERE group_name = ?", (old_n,))
        await db.commit()
    await message.reply("🔄 تغییر نام با موفقیت اعمال شد.")


@admin_router.message(Command("groups"))
async def cmd_groups(message: Message):
    if not is_private(message) or not await check_admin_filter(message):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT group_name FROM groups") as cur:
            rows = await cur.fetchall()
    txt = "👥 **لیست گروه‌ها:**\n"
    for r in rows:
        txt += f"- `{r[0]}`\n"
    await message.reply(txt, parse_mode="Markdown")


@admin_router.message(Command("group_users"))
async def cmd_group_users(message: Message):
    if not is_private(message) or not await check_admin_filter(message):
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("استفاده: `/group_users [نام_گروه]`")
    g_name = args[1]
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id, full_name, balance, is_frozen FROM users WHERE group_name = ?",
            (g_name,),
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        return await message.reply("عضوی یافت نشد.")
    txt = f"👥 **اعضای گروه {g_name}:**\n"
    for r in rows:
        status = "❄️ فریز" if r["is_frozen"] else "🟢 فعال"
        txt += (
            f"- **{r['full_name']}** | شماره حساب: `{r['user_id']}` | موجودی: `₳ {r['balance']}` | وضعیت: {status}\n"
        )
    await message.reply(txt, parse_mode="Markdown")


@admin_router.message(Command("move_group"))
async def cmd_move_group(message: Message):
    if not is_private(message) or not await check_admin_filter(message):
        return
    args = message.text.split()
    if len(args) < 3:
        return await message.reply("استفاده: `/move_group [آیدی] [گروه]`")
    t_id, g_name = int(args[1]), args[2]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET group_name = ? WHERE user_id = ?", (g_name, t_id)
        )
        await db.commit()
    await message.reply("👑 کاربر به گروه جدید منتقل شد.")


@admin_router.message(Command("remove_group"))
async def cmd_remove_group(message: Message):
    if not is_private(message) or not await check_admin_filter(message):
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("استفاده: `/remove_group [آیدی]`")
    t_id = int(args[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET group_name = 'Default' WHERE user_id = ?",
            (t_id,),
        )
        await db.commit()
    await message.reply("✅ کاربر به گروه پیش‌فرض برگردانده شد.")


@admin_router.message(Command("delete_group"))
async def cmd_delete_group(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("استفاده: `/delete_group [نام_گروه]`")
    g_name = args[1]
    if g_name == "Default":
        return await message.reply("❌ گروه پیش‌فرض حذف‌شدنی نیست.")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET group_name = 'Default' WHERE group_name = ?",
            (g_name,),
        )
        await db.execute("DELETE FROM group_links WHERE group_name = ?", (g_name,))
        await db.execute("DELETE FROM groups WHERE group_name = ?", (g_name,))
        await db.commit()
    await message.reply("🗑 گروه حذف شد و اعضا به Default منتقل شدند.")


@admin_router.message(Command("give"))
async def cmd_give(message: Message, state: FSMContext):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=3)
    if len(args) < 3:
        return await message.reply(
            "❌ ساختار: `/give [آیدی] [مقدار] [دلیل_اختیاری]`"
        )
    try:
        target, amount = int(args[1]), int(args[2])
        reason = args[3] if len(args) > 3 else "واریز مدیریت"
    except ValueError:
        return await message.reply("❌ ورودی نامعتبر.")
    if amount <= 0:
        return await message.reply("❌ مقدار باید مثبت باشد.")

    target_data = await get_user_data(target)
    if not target_data:
        return await message.reply("❌ کاربر مقصد یافت نشد.")
    if target_data["balance"] + amount > MAX_BALANCE_LIMIT:
        return await message.reply("❌ خطا: سقف موجودی مقصد.")

    await state.update_data(
        action="give",
        target=target,
        amount=amount,
        reason=reason,
        target_name=target_data["full_name"],
        admin_id=message.from_user.id,
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ تایید", callback_data="admin_yes"),
            InlineKeyboardButton(text="❌ انصراف", callback_data="admin_no"),
        ]]
    )
    await message.reply(
        f"⚠️ **تأیید واریز مدیریتی**\n\n"
        f"👤 گیرنده: **{target_data['full_name']}** (`{target}`)\n"
        f"💰 مبلغ: `₳ {amount}`\n"
        f"📝 دلیل: {reason}\n\n"
        f"آیا مطمئن هستید؟",
        reply_markup=kb,
        parse_mode="Markdown",
    )
    await state.set_state(AdminConfirmForm.waiting_for_confirm)


@admin_router.message(Command("take"))
async def cmd_take(message: Message, state: FSMContext):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=3)
    if len(args) < 3:
        return await message.reply(
            "❌ ساختار: `/take [آیدی] [مقدار] [دلیل_اختیاری]`"
        )
    try:
        target, amount = int(args[1]), int(args[2])
        reason = args[3] if len(args) > 3 else "کسر مدیریت"
    except ValueError:
        return await message.reply("❌ ورودی نامعتبر.")
    if amount <= 0:
        return await message.reply("❌ مقدار باید مثبت باشد.")

    target_data = await get_user_data(target)
    if not target_data:
        return await message.reply("❌ کاربر مقصد یافت نشد.")
    if target_data["balance"] < amount:
        return await message.reply("❌ موجودی ناکافی.")

    await state.update_data(
        action="take",
        target=target,
        amount=amount,
        reason=reason,
        target_name=target_data["full_name"],
        admin_id=message.from_user.id,
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ تایید", callback_data="admin_yes"),
            InlineKeyboardButton(text="❌ انصراف", callback_data="admin_no"),
        ]]
    )
    await message.reply(
        f"⚠️ **تأیید کسر مدیریتی**\n\n"
        f"👤 از حساب: **{target_data['full_name']}** (`{target}`)\n"
        f"💰 مبلغ: `₳ {amount}`\n"
        f"📝 دلیل: {reason}\n\n"
        f"آیا مطمئن هستید؟",
        reply_markup=kb,
        parse_mode="Markdown",
    )
    await state.set_state(AdminConfirmForm.waiting_for_confirm)


@admin_router.callback_query(AdminConfirmForm.waiting_for_confirm, F.data == "admin_yes")
async def admin_confirm_yes(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    admin_id = data.get("admin_id")
    if callback.from_user.id != admin_id:
        return await callback.answer("❌ فقط خودتان می‌توانید تأیید کنید.", show_alert=True)

    await state.clear()
    action = data["action"]
    target = data["target"]
    amount = data["amount"]
    reason = data["reason"]
    target_name = data.get("target_name", str(target))

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT balance FROM users WHERE user_id = ?", (target,)
            ) as cur:
                u = await cur.fetchone()

            if not u:
                return await callback.message.edit_text("❌ کاربر یافت نشد.")

            if action == "give":
                if u["balance"] + amount > MAX_BALANCE_LIMIT:
                    return await callback.message.edit_text("❌ خطا: سقف موجودی.")
                tx_id = f"TX-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{random.randint(100000, 999999)}"
                await db.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                    (amount, target),
                )
                await db.execute(
                    "INSERT INTO audit_logs (tx_id, timestamp, from_user, to_user, amount, reason) VALUES (?, ?, 0, ?, ?, ?)",
                    (tx_id, datetime.now(timezone.utc).isoformat(), target, amount, f"واریز مدیریت: {reason}"),
                )
                await db.commit()
                result_text = f"✅ واریز شد.\n👤 به: **{target_name}** (`{target}`)\n💰 مبلغ: `₳ {amount}`\n🔖 شناسه: `{tx_id}`"
                notify_text = (
                    f"📢 **عملیات سوپرادمین**\n\n"
                    f"👑 ادمین: `{admin_id}`\n"
                    f"➕ واریز به: **{target_name}** (`{target}`)\n"
                    f"💰 مبلغ: `₳ {amount}`\n"
                    f"📝 دلیل: {reason}\n"
                    f"🔖 شناسه: `{tx_id}`"
                )
            else:  # take
                if u["balance"] < amount:
                    return await callback.message.edit_text("❌ موجودی ناکافی.")
                tx_id = f"TX-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{random.randint(100000, 999999)}"
                await db.execute(
                    "UPDATE users SET balance = balance - ? WHERE user_id = ?",
                    (amount, target),
                )
                await db.execute(
                    "INSERT INTO audit_logs (tx_id, timestamp, from_user, to_user, amount, reason) VALUES (?, ?, ?, 0, ?, ?)",
                    (tx_id, datetime.now(timezone.utc).isoformat(), target, amount, f"کسر مدیریت: {reason}"),
                )
                await db.commit()
                result_text = f"🔥 کسر شد.\n👤 از: **{target_name}** (`{target}`)\n💰 مبلغ: `₳ {amount}`\n🔖 شناسه: `{tx_id}`"
                notify_text = (
                    f"📢 **عملیات سوپرادمین**\n\n"
                    f"👑 ادمین: `{admin_id}`\n"
                    f"➖ کسر از: **{target_name}** (`{target}`)\n"
                    f"💰 مبلغ: `₳ {amount}`\n"
                    f"📝 دلیل: {reason}\n"
                    f"🔖 شناسه: `{tx_id}`"
                )

    await callback.message.edit_text(result_text, parse_mode="Markdown")

    # اطلاع به سایر سوپرادمین‌ها
    for sa_id in SUPER_ADMINS:
        if sa_id != admin_id:
            try:
                await callback.bot.send_message(sa_id, notify_text, parse_mode="Markdown")
            except Exception:
                pass


@admin_router.callback_query(AdminConfirmForm.waiting_for_confirm, F.data == "admin_no")
async def admin_confirm_no(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    admin_id = data.get("admin_id")
    if callback.from_user.id != admin_id:
        return await callback.answer("❌ فقط خودتان می‌توانید لغو کنید.", show_alert=True)
    await state.clear()
    await callback.message.edit_text("❌ عملیات لغو شد.")


@admin_router.message(Command("rewardgroup"))
async def cmd_reward_group(message: Message):
    if not is_super_admin(message.from_user.id):
        return await message.reply("❌ این دستور فقط مخصوص سوپرادمین است.")

    args = message.text.split(maxsplit=3)
    if len(args) < 3:
        return await message.reply(
            "استفاده: `/rewardgroup [گروه] [مقدار] [دلیل]`"
        )

    g_name, amount = args[1], int(args[2])
    reason = args[3] if len(args) > 3 else "پاداش گروهی مدیریت"
    if amount <= 0:
        return await message.reply("❌ مقدار نامعتبر است.")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id, is_frozen FROM users WHERE group_name = ?",
            (g_name,),
        ) as cur:
            users = await cur.fetchall()

    if not users:
        return await message.reply("❌ هیچ کاربری در این گروه یافت نشد.")

    success_p, skipped_p, failed_p, total_dist = 0, 0, 0, 0

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            for u in users:
                try:
                    if u["is_frozen"]:
                        skipped_p += 1
                        continue

                    await db.execute("BEGIN IMMEDIATE")
                    sub_tx_id = f"TX-G-{str(uuid.uuid4()).upper()[:12]}"
                    await db.execute(
                        "UPDATE users SET balance = balance + ? WHERE user_id"
                        " = ?",
                        (amount, u["user_id"]),
                    )
                    await db.execute(
                        """
                        INSERT INTO audit_logs (tx_id, timestamp, from_user, to_user, amount, reason, status)
                        VALUES (?, ?, 0, ?, ?, ?, 'SUCCESS')
                    """,
                        (
                            sub_tx_id,
                            datetime.now(timezone.utc).isoformat(),
                            u["user_id"],
                            amount,
                            f"پاداش گروه [{g_name}]: {reason}",
                        ),
                    )
                    await db.commit()
                    success_p += 1
                    total_dist += amount
                except Exception:
                    await db.execute("ROLLBACK")
                    failed_p += 1

    await message.reply(
        f"📊 **گزارش واریز گروهی ({g_name}):**\n\n"
        f"✅ موفق: `{success_p}` کاربر\n"
        f"❄️ اسکیپ (فریز): `{skipped_p}` کاربر\n"
        f"❌ خطا: `{failed_p}` کاربر\n"
        f"💰 توزیع شده: `₳ {total_dist}`",
        parse_mode="Markdown",
    )


@admin_router.message(Command("undo"))
async def cmd_undo(message: Message):
    if not is_super_admin(message.from_user.id):
        return await message.reply("❌ این دستور فقط مخصوص سوپرادمین است.")
    args = message.text.split(maxsplit=2)
    if len(args) < 2:
        return await message.reply(
            "استفاده: `/undo [شناسه_تراکنش] [دلیل_اختیاری]`"
        )

    tx_id = args[1]
    reason = args[2] if len(args) > 2 else "لغو تراکنش توسط مدیریت ارشد"

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")

            async with db.execute(
                "SELECT from_user, to_user, amount, status FROM audit_logs"
                " WHERE tx_id = ?",
                (tx_id,),
            ) as cur:
                tx = await cur.fetchone()

            if not tx:
                await db.execute("ROLLBACK")
                return await message.reply("❌ تراکنشی با این شناسه یافت نشد.")
            if tx["status"] == "REFUNDED":
                await db.execute("ROLLBACK")
                return await message.reply("❌ این تراکنش قبلاً باطل شده است.")

            f_user, t_user, amount = tx["from_user"], tx["to_user"], tx["amount"]

            if t_user != 0:
                async with db.execute(
                    "SELECT balance FROM users WHERE user_id = ?", (t_user,)
                ) as cur_t:
                    target = await cur_t.fetchone()
                if not target or target["balance"] < amount:
                    await db.execute("ROLLBACK")
                    return await message.reply(
                        "❌ خطا: موجودی گیرنده برای برگشت زدن کافی نیست."
                    )

                await db.execute(
                    "UPDATE users SET balance = balance - ? WHERE user_id = ?",
                    (amount, t_user),
                )

            if f_user != 0:
                await db.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                    (amount, f_user),
                )

            new_tx_id = f"TX-REV-{str(uuid.uuid4()).upper()[:10]}"
            await db.execute(
                "UPDATE audit_logs SET status = 'REFUNDED' WHERE tx_id = ?",
                (tx_id,),
            )
            await db.execute(
                """
                INSERT INTO audit_logs (tx_id, timestamp, from_user, to_user, amount, reason, status)
                VALUES (?, ?, ?, ?, ?, ?, 'SUCCESS')
            """,
                (
                    new_tx_id,
                    datetime.now(timezone.utc).isoformat(),
                    t_user,
                    f_user,
                    amount,
                    f"برگشت تراکنش {tx_id}: {reason}",
                ),
            )

            await db.commit()

    await message.reply(
        f"🔄 تراکنش با موفقیت معکوس شد.\n🔖 شناسه برگشتی: `{new_tx_id}`",
        parse_mode="Markdown",
    )


@admin_router.message(Command("economy"))
async def cmd_economy(message: Message):
    if not is_super_admin(message.from_user.id):
        return await message.reply("❌ این دستور فقط مخصوص سوپرادمین است.")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(user_id), SUM(balance) FROM users"
        ) as cur:
            row = await cur.fetchone()
    await message.reply(
        f"👥 کل اعضا: `{row[0]}` | 💰 حجم نقدینگی در گردش: `₳ {row[1] or 0}`"
    )


@admin_router.message(Command("promote"))
async def cmd_promote(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("استفاده: `/promote [آیدی]`")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_admin = 1 WHERE user_id = ?",
            (int(args[1]),),
        )
        await db.commit()
    await message.reply("👑 کاربر به سطح ادمین ارتقا یافت.")


@admin_router.message(Command("demote"))
async def cmd_demote(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("استفاده: `/demote [آیدی]`")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_admin = 0 WHERE user_id = ?",
            (int(args[1]),),
        )
        await db.commit()
    await message.reply("🔥 دسترسی ادمینی کاربر سلب شد.")


@admin_router.message(Command("list_admins"))
async def cmd_list_admins(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return

    # سوپرادمین‌ها
    txt = "👑 **لیست سوپرادمین‌ها:**\n"
    for sa_id in sorted(SUPER_ADMINS):
        u = await get_user_data(sa_id)
        name = u["full_name"] if u else "ناشناس"
        txt += f"- **{name}** | `{sa_id}`\n"

    # ادمین‌های معمولی
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id, full_name FROM users WHERE is_admin = 1"
        ) as cur:
            admins = await cur.fetchall()

    txt += "\n👥 **لیست ادمین‌های معمولی:**\n"
    if admins:
        for a in admins:
            if a["user_id"] not in SUPER_ADMINS:
                txt += f"- **{a['full_name']}** | `{a['user_id']}`\n"
    else:
        txt += "- هیچ ادمین معمولی وجود ندارد.\n"

    await message.reply(txt, parse_mode="Markdown")


@admin_router.message(Command("add_super"))
async def cmd_add_super(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("استفاده: `/add_super [آیدی]`")
    try:
        new_id = int(args[1])
    except ValueError:
        return await message.reply("❌ آیدی باید عدد باشد.")

    if new_id in SUPER_ADMINS:
        return await message.reply("ℹ️ این کاربر از قبل سوپرادمین است.")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO super_admins (user_id) VALUES (?)", (new_id,)
        )
        await db.commit()
        await load_super_admins(db)

    # همچنین ادمین معمولی هم بشود
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_admin = 1 WHERE user_id = ?", (new_id,)
        )
        await db.commit()

    await message.reply(f"✅ کاربر `{new_id}` به سوپرادمین‌ها اضافه شد.")


@admin_router.message(Command("remove_super"))
async def cmd_remove_super(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("استفاده: `/remove_super [آیدی]`")
    try:
        rem_id = int(args[1])
    except ValueError:
        return await message.reply("❌ آیدی باید عدد باشد.")

    if rem_id in (SUPER_ADMIN_1, SUPER_ADMIN_2):
        return await message.reply("❌ سوپرادمین‌های پایه قابل حذف نیستند.")

    if rem_id not in SUPER_ADMINS:
        return await message.reply("ℹ️ این کاربر سوپرادمین نیست.")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM super_admins WHERE user_id = ?", (rem_id,))
        await db.commit()
        await load_super_admins(db)

    await message.reply(f"✅ کاربر `{rem_id}` از سوپرادمین‌ها حذف شد.")


@admin_router.message(Command("freeze"))
async def cmd_freeze(message: Message):
    if not is_super_admin(message.from_user.id):
        return await message.reply("❌ این دستور فقط مخصوص سوپرادمین است.")
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("استفاده: `/freeze [آیدی]`")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_frozen = 1 WHERE user_id = ?", (int(args[1]),)
        )
        await db.commit()
    await message.reply("❄️ حساب کاربر فریز شد.")


@admin_router.message(Command("unfreeze"))
async def cmd_unfreeze(message: Message):
    if not is_super_admin(message.from_user.id):
        return await message.reply("❌ این دستور فقط مخصوص سوپرادمین است.")
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("استفاده: `/unfreeze [آیدی]`")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_frozen = 0 WHERE user_id = ?", (int(args[1]),)
        )
        await db.commit()
    await message.reply("🟢 حساب کاربر فعال شد.")


@admin_router.message(Command("check"))
async def cmd_check(message: Message):
    if not is_super_admin(message.from_user.id):
        return await message.reply("❌ این دستور فقط مخصوص سوپرادمین است.")
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("استفاده: `/check [آیدی]`")
    u = await get_user_data(int(args[1]))
    if u:
        status = "❄️ فریز شده" if u["is_frozen"] else "🟢 فعال"
        admin_st = "👑 ادمین" if u["is_admin"] else "👤 کاربر عادی"
        await message.reply(
            f"🔎 **اطلاعات کامل کاربر `{args[1]}`:**\n\n"
            f"👤 نام کامل: {u['full_name']}\n"
            f"🏷 نام کاربری: @{u['username']}\n"
            f"💰 موجودی: `₳ {u['balance']}`\n"
            f"👥 گروه: **{u['group_name']}**\n"
            f"⚡ وضعیت: {status}\n"
            f"🛡 دسترسی: {admin_st}",
            parse_mode="Markdown",
        )


# --- دستورات بکاپ‌گیری دستی و بازیابی ---


@admin_router.message(Command("backup_now"))
async def cmd_backup_now(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return

    zip_path = create_zip_backup("manual")
    if zip_path and os.path.exists(zip_path):
        await message.reply_document(
            FSInputFile(zip_path), caption="📦 **فایل بکاپ کامل دیتابیس (ZIP)**"
        )
    else:
        await message.reply("❌ خطا در ایجاد فایل بکاپ.")


@admin_router.message(Command("force_backup"))
async def cmd_force_backup(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return

    if os.path.exists(DB_PATH):
        try:
            await message.bot.send_document(
                chat_id=BACKUP_CHANNEL_ID,
                document=FSInputFile(DB_PATH),
                caption=f"📦 **بکاپ دستی دیتابیس (توسط سوپرادمین)**\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            await message.reply("✅ فایل دیتابیس با موفقیت به کانال تلگرام بکاپ ارسال شد.")
        except Exception as e:
            await message.reply(f"❌ خطا در ارسال بکاپ به کانال: {e}")
    else:
        await message.reply("❌ فایل دیتابیس یافت نشد.")


@admin_router.message(Command("restore"))
async def cmd_restore(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return

    if not message.reply_to_message or not message.reply_to_message.document:
        return await message.reply(
            "❌ لطفاً این دستور را در **ریپلای (Reply)** روی یک فایل بکاپ ZIP"
            " یا db ارسال کنید."
        )

    doc = message.reply_to_message.document
    file_info = await message.bot.get_file(doc.file_id)
    download_path = f"temp_restore_{doc.file_name}"

    await message.bot.download_file(file_info.file_path, download_path)

    try:
        if download_path.endswith(".zip"):
            with zipfile.ZipFile(download_path, "r") as zip_ref:
                zip_ref.extractall("temp_extract")
            extracted_db = os.path.join("temp_extract", "atr_bank.db")
            if os.path.exists(extracted_db):
                shutil.move(extracted_db, DB_PATH)
                shutil.rmtree("temp_extract")
            else:
                os.remove(download_path)
                return await message.reply("❌ فایل `atr_bank.db` در فایل زیپ یافت نشد.")
        else:
            shutil.move(download_path, DB_PATH)

        if os.path.exists(download_path):
            os.remove(download_path)

        await message.reply(
            "✅ **پایگاه‌داده با موفقیت بازیابی شد!** ربات آماده به کار است."
        )
    except Exception as e:
        await message.reply(f"❌ خطا در بازیابی دیتابیس: {e}")


# --- راهنمای دستورات ---


@user_router.message(Command("help"))
@user_router.message(F.text == "راهنمای جامع بانک")
async def cmd_help(message: Message):
    user_id = message.from_user.id
    is_sa = is_super_admin(user_id)
    u = await get_user_data(user_id)
    is_adm = u and u["is_admin"]

    txt = (
        "📱 **راهنمای دستورات کاربران:**\n"
        "🔹 `/start` - شروع و دریافت شماره حساب\n"
        "🔹 `/profile` یا «پروفایل» - مشاهده نام، شماره حساب، موجودی و وضعیت\n"
        "🔹 `/transfer` یا «انتقال آتر» - انتقال آتر (چند روش مختلف)\n\n"
    )

    if is_adm or is_sa:
        txt += (
            "👥 **دستورات ادمین (فقط پیوی):**\n"
            "🔹 `/users` - لیست کاربران\n"
            "🔹 `/groups` - لیست گروه‌ها\n"
            "🔹 `/group_users [نام]` - اعضای یک گروه\n"
            "🔹 `/create_group [نام]` - فقط اضافه کردن گروه (بدون لینک)\n"
            "🔹 `/add_group [نام]` - ساخت گروه + لینک دعوت یکتا\n"
            "🔹 `/extend_group [نام] [روز]` - تمدید لینک فعلی\n"
            "🔹 `/renew_group [نام] [روز]` - ساخت لینک جدید با مدت اعتبار\n"
            "🔹 `/rename_group [قدیمی] [جدید]` - تغییر نام گروه\n"
            "🔹 `/move_group [آیدی] [گروه]` - تغییر گروه کاربر\n"
            "🔹 `/remove_group [آیدی]` - برگرداندن به Default\n\n"
        )

    if is_sa:
        txt += (
            "👑 **دستورات سوپرادمین (فقط پیوی):**\n"
            "🔸 `/give [آیدی] [مقدار]` - واریز (با تأیید دو مرحله‌ای)\n"
            "🔸 `/take [آیدی] [مقدار]` - کسر (با تأیید دو مرحله‌ای)\n"
            "🔸 `/rewardgroup [گروه] [مقدار]` - پاداش گروهی\n"
            "🔸 `/undo [شناسه]` - برگشت تراکنش\n"
            "🔸 `/economy` - آمار اقتصاد\n"
            "🔸 `/check [آیدی]` - اطلاعات کامل کاربر\n"
            "🔸 `/promote [آیدی]` - ارتقا به ادمین\n"
            "🔸 `/demote [آیدی]` - عزل ادمین\n"
            "🔸 `/list_admins` - مشاهده لیست ادمین‌ها و سوپرادمین‌ها\n"
            "🔸 `/add_super [آیدی]` - اضافه کردن سوپرادمین جدید\n"
            "🔸 `/remove_super [آیدی]` - حذف سوپرادمین\n"
            "🔸 `/delete_group [نام]` - حذف کامل گروه\n"
            "🔸 `/freeze [آیدی]` - فریز حساب\n"
            "🔸 `/unfreeze [آیدی]` - رفع فریز\n"
            "🔸 `/backup_now` - بکاپ ZIP\n"
            "🔸 `/force_backup` - ارسال بکاپ به کانال\n"
            "🔸 `/restore` - بازیابی (ریپلای روی فایل)\n"
        )

    await message.reply(txt, parse_mode="Markdown")


async def main():
    bot = Bot(token=BOT_TOKEN)

    # ۱. روشن کردن وب‌سرور سبک اختصاصی برای پلن Web Service در Render
    await start_dummy_server()

    # ۲. ابتدا دانلود خودکار آخرین بکاپ دیتابیس از کانال تلگرام
    await restore_db_from_telegram(bot)

    # ۳. مقداردهی اولیه دیتابیس و جدول‌ها
    await init_db()

    # ۴. شروع پروسه بکاپ‌گیری خودکار ۱ ساعته در پس‌زمینه
    asyncio.create_task(auto_backup_loop(bot))

    dp = Dispatcher(storage=MemoryStorage())

    dp.include_router(admin_router)
    dp.include_router(user_router)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
