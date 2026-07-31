import asyncio
from datetime import datetime, timezone, timedelta
import html
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
from aiohttp import web

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

SUPER_ADMIN_1 = 8490505070
SUPER_ADMIN_2 = 475473068  # ادمین دوم با دسترسی کامل

SUPER_ADMINS: set[int] = {SUPER_ADMIN_1, SUPER_ADMIN_2}

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "atr_bank.db"
BACKUP_DIR = "backups"
MAX_BALANCE_LIMIT = 1000000000  # سقف ۱ میلیارد آتر
USERS_PER_PAGE = 5  # تعداد کاربران در هر صفحه پنل مدیریت

BACKUP_CHANNEL_ID = -1003971216432

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


class AddProductForm(StatesGroup):
    waiting_for_shop_id = State()
    waiting_for_title = State()
    waiting_for_desc = State()
    waiting_for_price = State()
    waiting_for_needs_shipping = State()
    waiting_for_stock_type = State()
    waiting_for_stock_count = State()
    waiting_for_photo = State()


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
            INSERT OR IGNORE INTO users (user_id, username, full_name, balance)
            VALUES (0, 'central_treasury', 'خزانه بانک مرکزی', 0)
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

        await db.execute("""
            CREATE TABLE IF NOT EXISTS shop_settings (
                key TEXT PRIMARY KEY,
                value INTEGER
            )
        """)
        # درصد‌های پیش‌فرض سیستم شاپ
        await db.execute("INSERT OR IGNORE INTO shop_settings (key, value) VALUES ('seller_pct', 51)")
        await db.execute("INSERT OR IGNORE INTO shop_settings (key, value) VALUES ('treasury_pct', 40)")
        await db.execute("INSERT OR IGNORE INTO shop_settings (key, value) VALUES ('tax_pct', 9)")

        # درصد‌های پیش‌فرض سیستم پستچی
        await db.execute("INSERT OR IGNORE INTO shop_settings (key, value) VALUES ('courier_pct', 70)")
        await db.execute("INSERT OR IGNORE INTO shop_settings (key, value) VALUES ('courier_treasury_pct', 20)")
        await db.execute("INSERT OR IGNORE INTO shop_settings (key, value) VALUES ('courier_tax_pct', 10)")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS shops (
                shop_id TEXT PRIMARY KEY,
                owner_id INTEGER,
                shop_name TEXT,
                status TEXT DEFAULT 'PENDING'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS products (
                product_id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id TEXT,
                title TEXT,
                description TEXT,
                price INTEGER,
                stock_type TEXT DEFAULT 'UNLIMITED',
                stock_count INTEGER DEFAULT 0,
                photo_id TEXT,
                needs_shipping INTEGER DEFAULT 0,
                shipping_price INTEGER DEFAULT 0
            )
        """)

        try:
            await db.execute("ALTER TABLE products ADD COLUMN needs_shipping INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE products ADD COLUMN shipping_price INTEGER DEFAULT 0")
        except Exception:
            pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                shop_id TEXT,
                product_id INTEGER,
                amount INTEGER,
                status TEXT DEFAULT 'DELIVERED',
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS couriers (
                user_id INTEGER PRIMARY KEY
            )
        """)

        for g in ["Default"]:
            await db.execute(
                "INSERT OR IGNORE INTO groups (group_name) VALUES (?)", (g,)
            )

        for sa_id in [SUPER_ADMIN_1, SUPER_ADMIN_2]:
            await db.execute(
                "INSERT OR IGNORE INTO super_admins (user_id) VALUES (?)", (sa_id,)
            )

        await db.commit()
        await load_super_admins(db)


async def get_shop_rates():
    """دریافت درصد‌های تنظیم‌شده شاپ از دیتابیس"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT key, value FROM shop_settings") as cur:
            rows = await cur.fetchall()
            rates = {row["key"]: row["value"] for row in rows}
            return (
                rates.get("seller_pct", 51),
                rates.get("treasury_pct", 40),
                rates.get("tax_pct", 9)
            )


async def get_courier_rates():
    """دریافت درصد‌های تنظیم‌شده پستچی از دیتابیس"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT key, value FROM shop_settings") as cur:
            rows = await cur.fetchall()
            rates = {row["key"]: row["value"] for row in rows}
            return (
                rates.get("courier_pct", 70),
                rates.get("courier_treasury_pct", 20),
                rates.get("courier_tax_pct", 10)
            )


async def load_super_admins(db=None):
    global SUPER_ADMINS
    close_after = False
    if db is None:
        db = await aiosqlite.connect(DB_PATH)
        close_after = True
    try:
        async with db.execute("SELECT user_id FROM super_admins") as cur:
            rows = await cur.fetchall()
            SUPER_ADMINS = {row[0] for row in rows}
            SUPER_ADMINS.add(SUPER_ADMIN_1)
            SUPER_ADMINS.add(SUPER_ADMIN_2)
    finally:
        if close_after:
            await db.close()


async def sync_user(user_id: int, username: str, full_name: str = "Unknown"):
    async with aiosqlite.connect(DB_PATH) as db:
        username_clean = username if username else "بدون آیدی"
        full_name_clean = full_name if full_name else "ناشناس"
        await db.execute(
            """
            INSERT INTO users (user_id, username, full_name, balance) VALUES (?, ?, ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET username = ?, full_name = ?
        """,
            (user_id, username_clean, full_name_clean, username_clean, full_name_clean),
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
shop_router = Router()

user_router.message.middleware(AntiSpamMiddleware())
shop_router.message.middleware(AntiSpamMiddleware())


async def check_admin_filter(message: Message) -> bool:
    if is_super_admin(message.from_user.id):
        return True
    u = await get_user_data(message.from_user.id)
    return u and u["is_admin"]


def is_super_admin(user_id: int) -> bool:
    return user_id in SUPER_ADMINS


def is_private(message: Message) -> bool:
    return message.chat.type == "private"


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
        await asyncio.sleep(3600)
        if os.path.exists(DB_PATH):
            try:
                await bot.send_document(
                    chat_id=BACKUP_CHANNEL_ID,
                    document=FSInputFile(DB_PATH),
                    caption=f"<b>📦 بکاپ خودکار دیتابیس</b>\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    parse_mode="HTML"
                )
                logging.info("✅ بکاپ خودکار به کانال تلگرام ارسال شد.")
            except Exception as e:
                logging.error(f"❌ خطا در ارسال بکاپ خودکار به تلگرام: {e}")


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

    text = f"👥 <b>لیست کاربران (صفحه {page} از {total_pages})</b>\n"
    text += f"📊 کل کاربران: <code>{total_users}</code> نفر\n\n"

    for idx, u in enumerate(users, start=offset + 1):
        safe_full_name = html.escape(u['full_name'] or 'ناشناس')
        safe_group_name = html.escape(u['group_name'] or 'Default')
        text += (
            f"<b>{idx}. {safe_full_name}</b>\n"
            f"شماره حساب: <code>{u['user_id']}</code>\n"
            f"موجودی: <code>₳ {u['balance']}</code>\n"
            f"گروه: <b>{safe_group_name}</b>\n"
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
    if not is_private(message):
        return

    user_id = message.from_user.id
    username = message.from_user.username
    full_name = message.from_user.full_name

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            already_exists = await cur.fetchone() is not None

    await sync_user(user_id, username, full_name)

    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        payload = args[1].strip()

        if payload.upper().startswith("BUY_"):
            p_id_str = payload[4:].strip()
            if p_id_str.isdigit():
                product_id = int(p_id_str)
                async with aiosqlite.connect(DB_PATH) as db:
                    db.row_factory = aiosqlite.Row
                    async with db.execute("""
                        SELECT p.*, s.shop_name 
                        FROM products p 
                        JOIN shops s ON p.shop_id = s.shop_id 
                        WHERE p.product_id = ? AND s.status = 'APPROVED'
                    """, (product_id,)) as cur:
                        product = await cur.fetchone()

                if not product:
                    return await message.reply("❌ کالای موردنظر یافت نشد یا فروشگاه مربوطه فعال نیست.")

                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🛒 تایید و خرید محصول", callback_data=f"confirm_buy_{product_id}")
                ]])

                ship_text = "نیازمند پست 🚚" if product["needs_shipping"] else "بدون نیاز به پست"
                stock_text = "نامحدود" if product["stock_type"] == "UNLIMITED" else f"{product['stock_count']} عدد"

                caption = (
                    f"🛒 <b>{html.escape(product['title'])}</b>\n\n"
                    f"🏪 فروشگاه: <b>{html.escape(product['shop_name'])}</b>\n"
                    f"📝 توضیحات: {html.escape(product['description'] or 'ندارد')}\n"
                    f"💰 قیمت کالا: <code>₳ {product['price']:,}</code>\n"
                    f"📦 موجودی: <b>{stock_text}</b>\n"
                    f"🚚 وضعیت ارسال: {ship_text}\n"
                )

                if product["photo_id"]:
                    return await message.reply_photo(photo=product["photo_id"], caption=caption, reply_markup=kb, parse_mode="HTML")
                else:
                    return await message.reply(caption, reply_markup=kb, parse_mode="HTML")

        if payload.upper().startswith("G"):
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
                            safe_code = html.escape(code)
                            return await message.reply(
                                f"❌ لینک دعوت نامعتبر است.\n"
                                f"کد دریافتی: <code>{safe_code}</code>",
                                parse_mode="HTML",
                            )

                        if hasattr(link_data, "keys"):
                            group_name = link_data["group_name"]
                            expires_val = link_data["expires_at"] if "expires_at" in link_data.keys() else None
                        else:
                            group_name = link_data.get("group_name")
                            expires_val = link_data.get("expires_at")

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

                        safe_g_name = html.escape(group_name)
                        return await message.reply(
                            f"🎉 شما با موفقیت عضو گروه <b>{safe_g_name}</b> شدید.",
                            parse_mode="HTML",
                        )
            except Exception as e:
                logging.error(f"start group link error: {e}")
                safe_err_type = html.escape(type(e).__name__)
                safe_err = html.escape(str(e))
                safe_code = html.escape(code)
                return await message.reply(
                    f"❌ خطا در عضویت گروه:\n<code>{safe_err_type}: {safe_err}</code>\n"
                    f"کد: <code>{safe_code}</code>",
                    parse_mode="HTML",
                )

    if not already_exists:
        await message.reply(
            f"به بانک جادویی Atramentum خوش اومدید.\n"
            f"شماره حساب: <code>{user_id}</code>",
            parse_mode="HTML",
        )
    else:
        await message.reply(
            f"شماره حساب: <code>{user_id}</code>",
            parse_mode="HTML",
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
    safe_name = html.escape(u['full_name'] or "ناشناس")

    await message.reply(
        f"👤 نام: {safe_name}\n"
        f"🆔 شماره حساب: <code>{user_id}</code>\n"
        f"💰 موجودی: <code>₳ {u['balance']}</code>\n"
        f"⚡ وضعیت حساب: {status_text}",
        parse_mode="HTML",
    )


# --- سیستم انتقال آتر ---

async def process_transfer_request(message: Message, state: FSMContext, to_user_id: int, amount: int):
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
    safe_target_name = html.escape(target_name)

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
        f"دریافت‌کننده: {safe_target_name} (<code>{to_user_id}</code>)\n"
        f"مبلغ: <code>₳ {amount}</code>\n"
        f"آیا مطمئن هستید؟",
        reply_markup=kb,
        parse_mode="HTML",
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
    if text.startswith("/transfer"):
        text = text[len("/transfer"):].strip()
    elif text.startswith("انتقال آتر"):
        text = text[len("انتقال آتر"):].strip()

    if message.reply_to_message and message.reply_to_message.from_user:
        try:
            amount = int(text)
            to_user_id = message.reply_to_message.from_user.id
            return await process_transfer_request(message, state, to_user_id, amount)
        except ValueError:
            return await message.reply("❌ مبلغ باید عدد باشد.")

    parts = text.split()
    if len(parts) == 0:
        await message.reply("لطفاً شماره حساب (آیدی عددی) فرد مقصد را وارد کنید:")
        await state.set_state(TxForm.waiting_for_to_user)
        return

    if len(parts) >= 2:
        target_raw = parts[0]
        try:
            amount = int(parts[1])
        except ValueError:
            return await message.reply("❌ مبلغ باید عدد باشد.")

        to_user_id = None
        if target_raw.startswith("@"):
            username = target_raw[1:]
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT user_id FROM users WHERE username = ?",
                    (username,),
                ) as cur:
                    row = await cur.fetchone()
                    if row:
                        to_user_id = row["user_id"]
            if not to_user_id:
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
    if callback.from_user.id != from_user:
        return await callback.answer("❌ فقط انتقال‌دهنده می‌تواند تأیید کند.", show_alert=True)

    await state.clear()
    to_user_id = data["to_user_id"]
    amount = data["amount"]
    target_name = data.get("target_name", "کاربر مقصد")
    safe_target_name = html.escape(target_name)

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
        f"به نام: <b>{safe_target_name}</b>\n"
        f"شناسه: <code>{tx_id}</code>\n"
        f"مبلغ: <code>₳ {amount}</code>",
        parse_mode="HTML",
    )

    sender_data = await get_user_data(from_user)
    sender_name = sender_data["full_name"] if sender_data else str(from_user)
    safe_sender_name = html.escape(sender_name)

    try:
        await callback.bot.send_message(
            from_user,
            f"📤 <b>رسید انتقال</b>\n\n"
            f"شما <code>₳ {amount}</code> به <b>{safe_target_name}</b> (<code>{to_user_id}</code>) انتقال دادید.\n"
            f"🔖 شناسه تراکنش: <code>{tx_id}</code>",
            parse_mode="HTML",
        )
    except Exception:
        pass

    try:
        await callback.bot.send_message(
            to_user_id,
            f"📥 <b>رسید دریافت</b>\n\n"
            f"شما <code>₳ {amount}</code> از <b>{safe_sender_name}</b> (<code>{from_user}</code>) دریافت کردید.\n"
            f"🔖 شناسه تراکنش: <code>{tx_id}</code>",
            parse_mode="HTML",
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


# ==========================================
# 🛍️ بخش شاپ و خریدهای کاربران
# ==========================================

@shop_router.message(Command("my_orders"))
@shop_router.message(Command("my_purchases"))
@shop_router.message(F.text == "خریدهای من")
async def cmd_my_orders(message: Message):
    if not is_private(message):
        return
    user_id = message.from_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT o.order_id, o.amount, o.status, o.created_at, p.title, s.shop_name
            FROM orders o
            LEFT JOIN products p ON o.product_id = p.product_id
            LEFT JOIN shops s ON o.shop_id = s.shop_id
            WHERE o.user_id = ?
            ORDER BY o.order_id DESC
        """, (user_id,)) as cur:
            orders = await cur.fetchall()

    if not orders:
        return await message.reply("🛍️ شما هنوز هیچ خریدی انجام نداده‌اید.")

    txt = "📦 <b>لیست اجناس خریده‌شده و دست‌رسیده:</b>\n\n"
    for o in orders:
        product_title = html.escape(o["title"] or "محصول حذف‌شده")
        shop_name = html.escape(o["shop_name"] or "فروشگاه ناپیدا")
        status_text = "✅ به دستتان رسیده" if o["status"] in ["DELIVERED", "COMPLETED"] else "⏳ در حال ارسال"
        try:
            dt = datetime.fromisoformat(o["created_at"]).strftime("%Y-%m-%d %H:%M")
        except Exception:
            dt = o["created_at"]
        
        txt += (
            f"🔹 <b>{product_title}</b>\n"
            f"🏪 فروشگاه: {shop_name}\n"
            f"💰 قیمت خرید: <code>₳ {o['amount']:,}</code>\n"
            f"📅 تاریخ: <code>{dt}</code>\n"
            f"📌 وضعیت: {status_text}\n"
            f"------------------------------\n"
        )
    await message.reply(txt, parse_mode="HTML")


@shop_router.message(Command("register_shop"))
async def cmd_register_shop(message: Message):
    if not is_private(message):
        return

    text_params = message.text.strip()[len("/register_shop"):].strip()
    if not text_params:
        return await message.reply(
            "❌ <b>نحوه ثبت فروشگاه:</b>\n"
            "<code>/register_shop (نام فروشگاه) (شناسه)</code>\n\n"
            "مثال: <code>/register_shop فروشگاه دیجیتال tech_store</code>",
            parse_mode="HTML"
        )

    parts = text_params.rsplit(maxsplit=1)
    if len(parts) < 2:
        return await message.reply(
            "❌ لطفاً هم نام فروشگاه و هم شناسه را وارد کنید.\n"
            "مثال: <code>/register_shop فروشگاه دیجیتال tech_store</code>",
            parse_mode="HTML"
        )

    shop_name, shop_id = parts[0].strip(), parts[1].lower().strip()

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT 1 FROM shops WHERE shop_id = ?", (shop_id,)) as cur:
                if await cur.fetchone():
                    return await message.reply("❌ این شناسه شاپ قبلاً ثبت شده است.")

            await db.execute(
                "INSERT INTO shops (shop_id, owner_id, shop_name, status) VALUES (?, ?, ?, 'PENDING')",
                (shop_id, message.from_user.id, shop_name)
            )
            await db.commit()

    await message.reply(
        f"✅ درخواست ثبت فروشگاه «<b>{html.escape(shop_name)}</b>» ارسال شد و پس از تأیید ادمین فعال خواهد شد.",
        parse_mode="HTML"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ تأیید شاپ", callback_data=f"approve_shop:{shop_id}"),
        InlineKeyboardButton(text="❌ رد شاپ", callback_data=f"reject_shop:{shop_id}")
    ]])

    for sa_id in SUPER_ADMINS:
        try:
            await message.bot.send_message(
                sa_id,
                f"🏪 <b>درخواست ساخت فروشگاه جدید!</b>\n\n"
                f"👤 فروشنده: <code>{message.from_user.id}</code>\n"
                f"🆔 شناسه شاپ: <code>{shop_id}</code>\n"
                f"🏷 نام شاپ: <b>{html.escape(shop_name)}</b>",
                reply_markup=kb,
                parse_mode="HTML"
            )
        except Exception:
            pass


@shop_router.callback_query(F.data.startswith("approve_shop:"))
async def cb_approve_shop(callback: CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        return await callback.answer("❌ فقط سوپرادمین می‌تواند این عملیات را انجام دهد.", show_alert=True)

    shop_id = callback.data.split(":")[1]
    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT owner_id, shop_name FROM shops WHERE shop_id = ?", (shop_id,)) as cur:
                shop = await cur.fetchone()
            if not shop:
                return await callback.answer("❌ فروشگاه یافت نشد.", show_alert=True)

            await db.execute("UPDATE shops SET status = 'APPROVED' WHERE shop_id = ?", (shop_id,))
            await db.commit()

    await callback.message.edit_text(
        f"✅ فروشگاه <b>{html.escape(shop['shop_name'])}</b> (<code>{shop_id}</code>) با موفقیت تأیید شد.",
        parse_mode="HTML"
    )
    try:
        await callback.bot.send_message(
            shop["owner_id"],
            f"🎉 فروشگاه شما با نام «<b>{html.escape(shop['shop_name'])}</b>» تأیید و فعال شد!",
            parse_mode="HTML"
        )
    except Exception:
        pass


@shop_router.callback_query(F.data.startswith("reject_shop:"))
async def cb_reject_shop(callback: CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        return await callback.answer("❌ فقط سوپرادمین می‌تواند این عملیات را انجام دهد.", show_alert=True)

    shop_id = callback.data.split(":")[1]
    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT owner_id, shop_name FROM shops WHERE shop_id = ?", (shop_id,)) as cur:
                shop = await cur.fetchone()
            if not shop:
                return await callback.answer("❌ فروشگاه یافت نشد.", show_alert=True)

            await db.execute("UPDATE shops SET status = 'REJECTED' WHERE shop_id = ?", (shop_id,))
            await db.commit()

    await callback.message.edit_text(
        f"❌ درخواست فروشگاه <b>{html.escape(shop['shop_name'])}</b> (<code>{shop_id}</code>) رد شد.",
        parse_mode="HTML"
    )
    try:
        await callback.bot.send_message(
            shop["owner_id"],
            f"❌ درخواست ثبت فروشگاه «<b>{html.escape(shop['shop_name'])}</b>» توسط مدیریت رد شد.",
            parse_mode="HTML"
        )
    except Exception:
        pass


@shop_router.message(Command("add_product"))
async def cmd_add_product(message: Message, state: FSMContext):
    if not is_private(message):
        return
    user_id = message.from_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT shop_id, shop_name FROM shops WHERE owner_id = ? AND status = 'APPROVED'", (user_id,)) as cur:
            shops = await cur.fetchall()

    if not shops:
        return await message.reply("❌ شما هیچ فروشگاه تأییدشده‌ای ندارید.")

    if len(shops) == 1:
        await state.update_data(shop_id=shops[0]["shop_id"])
        await message.reply("📦 لطفاً **عنوان محصول** را وارد کنید:")
        await state.set_state(AddProductForm.waiting_for_title)
    else:
        txt = "لطفاً شناسه شاپ موردنظر را وارد کنید:\n"
        for s in shops:
            txt += f"• <code>{s['shop_id']}</code> - {html.escape(s['shop_name'])}\n"
        await message.reply(txt, parse_mode="HTML")
        await state.set_state(AddProductForm.waiting_for_shop_id)


@shop_router.message(AddProductForm.waiting_for_shop_id)
async def process_prod_shop_id(message: Message, state: FSMContext):
    shop_id = message.text.strip().lower()
    await state.update_data(shop_id=shop_id)
    await message.reply("📦 لطفاً **عنوان محصول** را وارد کنید:")
    await state.set_state(AddProductForm.waiting_for_title)


@shop_router.message(AddProductForm.waiting_for_title)
async def process_prod_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await message.reply("📝 **توضیحات محصول** را وارد کنید (یا بنویسید -):")
    await state.set_state(AddProductForm.waiting_for_desc)


@shop_router.message(AddProductForm.waiting_for_desc)
async def process_prod_desc(message: Message, state: FSMContext):
    desc = message.text.strip()
    await state.update_data(desc="" if desc == "-" else desc)
    await message.reply("💰 **قیمت محصول به آتر** را وارد کنید:")
    await state.set_state(AddProductForm.waiting_for_price)


@shop_router.message(AddProductForm.waiting_for_price)
async def process_prod_price(message: Message, state: FSMContext):
    try:
        price = int(message.text.strip())
        if price <= 0:
            raise ValueError
    except ValueError:
        return await message.reply("❌ قیمت باید یک عدد مثبت باشد.")

    await state.update_data(price=price)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📦 بله (نیازمند پست)", callback_data="ship_yes"),
        InlineKeyboardButton(text="⚡ خیر (بدون نیاز به پست)", callback_data="ship_no")
    ]])
    await message.reply("🚚 آیا این محصول نیاز به پست/ارسال دارد؟", reply_markup=kb)
    await state.set_state(AddProductForm.waiting_for_needs_shipping)


@shop_router.callback_query(AddProductForm.waiting_for_needs_shipping, F.data.startswith("ship_"))
async def process_prod_needs_shipping(callback: CallbackQuery, state: FSMContext):
    needs_ship = 1 if callback.data == "ship_yes" else 0
    await state.update_data(needs_shipping=needs_ship, shipping_price=0)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="نامحدود", callback_data="stock_UNLIMITED"),
        InlineKeyboardButton(text="تک عددی", callback_data="stock_SINGLE"),
        InlineKeyboardButton(text="محدود", callback_data="stock_LIMITED")
    ]])
    await callback.message.edit_text("📊 نوع موجودی محصول را انتخاب کنید:", reply_markup=kb)
    await state.set_state(AddProductForm.waiting_for_stock_type)


@shop_router.callback_query(AddProductForm.waiting_for_stock_type, F.data.startswith("stock_"))
async def process_prod_stock_type(callback: CallbackQuery, state: FSMContext):
    stock_choice = callback.data.split("_")[1]

    if stock_choice == "UNLIMITED":
        await state.update_data(stock_type="UNLIMITED", stock_count=0)
        await callback.message.edit_text("🖼 عکس محصول را ارسال کنید (یا کلمه `no` را بفرستید):")
        await state.set_state(AddProductForm.waiting_for_photo)
    elif stock_choice == "SINGLE":
        await state.update_data(stock_type="LIMITED", stock_count=1)
        await callback.message.edit_text("🖼 عکس محصول را ارسال کنید (یا کلمه `no` را بفرستید):")
        await state.set_state(AddProductForm.waiting_for_photo)
    elif stock_choice == "LIMITED":
        await state.update_data(stock_type="LIMITED")
        await callback.message.edit_text("🔢 تعداد موجودی کالا را وارد کنید:")
        await state.set_state(AddProductForm.waiting_for_stock_count)


@shop_router.message(AddProductForm.waiting_for_stock_count)
async def process_prod_stock_count(message: Message, state: FSMContext):
    try:
        count = int(message.text.strip())
        if count < 0:
            raise ValueError
    except ValueError:
        return await message.reply("❌ تعداد باید عدد صحیح باشد.")

    await state.update_data(stock_count=count)
    await message.reply("🖼 عکس محصول را ارسال کنید (یا کلمه `no` را بفرستید):")
    await state.set_state(AddProductForm.waiting_for_photo)


@shop_router.message(AddProductForm.waiting_for_photo)
async def process_prod_photo(message: Message, state: FSMContext):
    photo_id = None
    if message.photo:
        photo_id = message.photo[-1].file_id
    elif message.text and message.text.lower().strip() != "no":
        return await message.reply("❌ لطفاً یک عکس ارسال کنید یا کلمه `no` را بنویسید.")

    data = await state.get_data()
    await state.clear()

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                INSERT INTO products (shop_id, title, description, price, stock_type, stock_count, photo_id, needs_shipping, shipping_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data["shop_id"],
                data["title"],
                data["desc"],
                data["price"],
                data["stock_type"],
                data.get("stock_count", 0),
                photo_id,
                data.get("needs_shipping", 0),
                0
            ))
            product_id = cursor.lastrowid
            await db.commit()

    stock_info = "نامحدود" if data["stock_type"] == "UNLIMITED" else f"{data.get('stock_count', 1)} عدد"
    ship_info = "نیازمند پست 🚚" if data.get("needs_shipping") else "بدون نیاز به پست"

    await message.reply(
        f"✅ <b>محصول با موفقیت ثبت شد!</b>\n\n"
        f"📦 شناسه کالا: <code>{product_id}</code>\n"
        f"🏷 عنوان: <b>{html.escape(data['title'])}</b>\n"
        f"💰 قیمت کالا: <code>₳ {data['price']:,}</code>\n"
        f"📊 موجودی: <b>{stock_info}</b>\n"
        f"🚚 وضعیت ارسال: {ship_info}\n\n"
        f"💡 برای انتشار در کانال از دستور زیر استفاده کنید:\n"
        f"<code>/post_product {product_id}</code>",
        parse_mode="HTML"
    )


@shop_router.message(Command("post_product"))
async def cmd_post_product(message: Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.reply("❌ مثال: <code>/post_product 12</code>", parse_mode="HTML")

    product_id = int(args[1])
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT p.*, s.shop_name, s.owner_id 
            FROM products p 
            JOIN shops s ON p.shop_id = s.shop_id 
            WHERE p.product_id = ?
        """, (product_id,)) as cur:
            product = await cur.fetchone()

    if not product:
        return await message.reply("❌ کالایی یافت نشد.")

    if product["owner_id"] != message.from_user.id and not is_super_admin(message.from_user.id):
        return await message.reply("❌ شما مالک این شاپ نیستید.")

    bot_info = await message.bot.get_me()
    buy_link = f"https://t.me/{bot_info.username}?start=BUY_{product_id}"

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛒 خرید مستقیم", url=buy_link)
    ]])

    ship_text = "نیازمند پست 🚚" if product["needs_shipping"] else "بدون نیاز به پست"
    stock_text = "نامحدود" if product["stock_type"] == "UNLIMITED" else f"{product['stock_count']} عدد"

    caption = (
        f"🛍️ <b>{html.escape(product['title'])}</b>\n\n"
        f"🏪 فروشگاه: <b>{html.escape(product['shop_name'])}</b>\n"
        f"📝 {html.escape(product['description'] or 'بدون توضیحات')}\n\n"
        f"💰 قیمت کالا: <code>₳ {product['price']:,}</code>\n"
        f"📦 موجودی: <b>{stock_text}</b>\n"
        f"🚚 وضعیت ارسال: {ship_text}"
    )

    if product["photo_id"]:
        await message.reply_photo(photo=product["photo_id"], caption=caption, reply_markup=kb, parse_mode="HTML")
    else:
        await message.reply(caption, reply_markup=kb, parse_mode="HTML")


@shop_router.callback_query(F.data.startswith("confirm_buy_"))
async def cb_confirm_buy(callback: CallbackQuery):
    product_id = int(callback.data.split("_")[2])
    buyer_id = callback.from_user.id

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")

            async with db.execute("""
                SELECT p.*, s.owner_id as seller_id, s.shop_name 
                FROM products p 
                JOIN shops s ON p.shop_id = s.shop_id 
                WHERE p.product_id = ? AND s.status = 'APPROVED'
            """, (product_id,)) as cur:
                p = await cur.fetchone()

            if not p:
                await db.execute("ROLLBACK")
                return await callback.answer("❌ محصول یافت نشد یا فروشگاه غیرفعال است.", show_alert=True)

            if p["stock_type"] == "LIMITED" and p["stock_count"] <= 0:
                await db.execute("ROLLBACK")
                return await callback.answer("❌ موجودی این کالا به پایان رسیده است.", show_alert=True)

            async with db.execute("SELECT balance, is_frozen FROM users WHERE user_id = ?", (buyer_id,)) as cur:
                buyer = await cur.fetchone()

            if not buyer or buyer["is_frozen"]:
                await db.execute("ROLLBACK")
                return await callback.answer("❌ حساب شما مسدود (فریز) است.", show_alert=True)

            product_price = p["price"]
            total_price = product_price

            if buyer["balance"] < total_price:
                await db.execute("ROLLBACK")
                return await callback.answer("❌ موجودی حساب شما برای خرید این کالا کافی نیست.", show_alert=True)

            seller_pct, treasury_pct, tax_pct = await get_shop_rates()

            seller_share = (product_price * seller_pct) // 100
            total_treasury_share = (product_price * treasury_pct) // 100

            seller_id = p["seller_id"]

            await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (total_price, buyer_id))
            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (seller_share, seller_id))
            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = 0", (total_treasury_share,))

            if p["stock_type"] == "LIMITED":
                await db.execute("UPDATE products SET stock_count = stock_count - 1 WHERE product_id = ?", (product_id,))

            now_iso = datetime.now(timezone.utc).isoformat()
            async with db.execute("""
                INSERT INTO orders (user_id, shop_id, product_id, amount, status, created_at)
                VALUES (?, ?, ?, ?, 'DELIVERED', ?)
            """, (buyer_id, p["shop_id"], product_id, total_price, now_iso)) as cur:
                order_id = cur.lastrowid

            tx_id = f"TX-SHOP-{uuid.uuid4().hex[:8]}"
            await db.execute("""
                INSERT INTO audit_logs (tx_id, timestamp, from_user, to_user, amount, reason, status)
                VALUES (?, ?, ?, 0, ?, ?, 'SUCCESS')
            """, (tx_id, now_iso, buyer_id, total_price, f"خرید از شاپ [{p['shop_id']}] - کالا {product_id}"))

            await db.commit()

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"🎉 <b>خرید با موفقیت انجام شد!</b>\n\n"
        f"📦 نام کالا: <b>{html.escape(p['title'])}</b>\n"
        f"🏪 فروشگاه: <b>{html.escape(p['shop_name'])}</b>\n"
        f"💰 مبلغ پرداختی کل: <code>₳ {total_price:,}</code>\n"
        f"🔖 شماره سفارش: <code>{order_id}</code>",
        parse_mode="HTML"
    )

    try:
        await callback.bot.send_message(
            seller_id,
            f"🛍️ <b>سفارش جدید دریافت شد!</b>\n\n"
            f"📦 کالا: <b>{html.escape(p['title'])}</b>\n"
            f"💵 قیمت محصول: <code>₳ {product_price:,}</code>\n"
            f"🚚 نیاز به ارسال پست: {'بله' if p['needs_shipping'] else 'خیر'}\n"
            f"📥 سهم واریزی شما ({seller_pct}٪): <code>₳ {seller_share:,}</code>\n"
            f"👤 خریدار: <code>{buyer_id}</code>",
            parse_mode="HTML"
        )
    except Exception:
        pass


@shop_router.message(Command("shops"))
async def cmd_shops(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT shop_id, shop_name FROM shops WHERE status = 'APPROVED'") as cur:
            shops = await cur.fetchall()

    if not shops:
        return await message.reply("🏪 هیچ فروشگاه فعالی وجود ندارد.")

    txt = "🏪 <b>لیست فروشگاه‌های فعال:</b>\n\n"
    for s in shops:
        txt += f"• <b>{html.escape(s['shop_name'])}</b> | شناسه: <code>{s['shop_id']}</code>\n"
    await message.reply(txt, parse_mode="HTML")


# --- دستورات مدیریتی شاپ، پستچی و خزانه مرکزی ---

@admin_router.message(Command("add_courier"))
async def cmd_add_courier(message: Message):
    if not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.reply("استفاده: <code>/add_courier [user_id]</code>", parse_mode="HTML")

    user_id = int(args[1])
    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR IGNORE INTO couriers (user_id) VALUES (?)", (user_id,))
            await db.commit()

    await message.reply(f"✅ کاربر <code>{user_id}</code> به عنوان پستچی اضافه شد.", parse_mode="HTML")


@admin_router.message(Command("remove_courier"))
async def cmd_remove_courier(message: Message):
    if not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.reply("استفاده: <code>/remove_courier [user_id]</code>", parse_mode="HTML")

    user_id = int(args[1])
    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM couriers WHERE user_id = ?", (user_id,))
            await db.commit()

    await message.reply(f"❌ کاربر <code>{user_id}</code> از لیست پستچی‌ها حذف شد.", parse_mode="HTML")


@admin_router.message(Command("delete_shop"))
async def cmd_delete_shop(message: Message):
    if not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("استفاده: <code>/delete_shop [shop_id]</code>", parse_mode="HTML")

    shop_id = args[1].lower().strip()
    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM products WHERE shop_id = ?", (shop_id,))
            await db.execute("DELETE FROM shops WHERE shop_id = ?", (shop_id,))
            await db.commit()

    await message.reply(f"🗑 فروشگاه <code>{shop_id}</code> و تمامی محصولات آن حذف شدند.", parse_mode="HTML")


@admin_router.message(Command("set_shop_rates"))
async def cmd_set_shop_rates(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return

    args = message.text.split()
    if len(args) < 4:
        s, t, x = await get_shop_rates()
        return await message.reply(
            f"⚙️ <b>تنظیمات فعلی تقسیم درصد فروشگاه:</b>\n"
            f"• سهم فروشنده: <code>%{s}</code>\n"
            f"• سهم خزانه بانک: <code>%{t}</code>\n"
            f"• مالیات (سوخت): <code>%{x}</code>\n\n"
            f"💡 <b>نحوه تغییر:</b>\n"
            f"<code>/set_shop_rates [فروشنده] [خزانه] [مالیات]</code>\n"
            f"<i>مثال: <code>/set_shop_rates 51 40 9</code></i>",
            parse_mode="HTML"
        )

    try:
        seller, treasury, tax = int(args[1]), int(args[2]), int(args[3])
    except ValueError:
        return await message.reply("❌ درصدها باید اعداد صحیح باشند.")

    if seller < 0 or treasury < 0 or tax < 0 or (seller + treasury + tax != 100):
        return await message.reply("❌ مجموع ۳ درصد باید دقیقاً برابر با **۱۰۰** باشد.")

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR REPLACE INTO shop_settings (key, value) VALUES ('seller_pct', ?)", (seller,))
            await db.execute("INSERT OR REPLACE INTO shop_settings (key, value) VALUES ('treasury_pct', ?)", (treasury,))
            await db.execute("INSERT OR REPLACE INTO shop_settings (key, value) VALUES ('tax_pct', ?)", (tax,))
            await db.commit()

    await message.reply(
        f"✅ <b>درصدهای شاپ با موفقیت به‌روزرسانی شد!</b>\n\n"
        f"🏪 سهم فروشنده: <b>%{seller}</b>\n"
        f"🏛 سهم خزانه: <b>%{treasury}</b>\n"
        f"🔥 مالیات (سوخت): <b>%{tax}</b>",
        parse_mode="HTML"
    )


@admin_router.message(Command("set_courier_rates"))
async def cmd_set_courier_rates(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return

    args = message.text.split()
    if len(args) < 4:
        c, t, x = await get_courier_rates()
        return await message.reply(
            f"⚙️ <b>تنظیمات فعلی درصد پُستچی:</b>\n"
            f"• سهم پستچی: <code>%{c}</code>\n"
            f"• سهم خزانه بانک: <code>%{t}</code>\n"
            f"• مالیات (سوخت): <code>%{x}</code>\n\n"
            f"💡 <b>نحوه تغییر:</b>\n"
            f"<code>/set_courier_rates (پستچی) (خزانه) (مالیات)</code>\n"
            f"<i>مثال: <code>/set_courier_rates 70 20 10</code></i>",
            parse_mode="HTML"
        )

    try:
        courier, treasury, tax = int(args[1]), int(args[2]), int(args[3])
    except ValueError:
        return await message.reply("❌ درصدها باید اعداد صحیح باشند.")

    if courier < 0 or treasury < 0 or tax < 0 or (courier + treasury + tax != 100):
        return await message.reply("❌ مجموع ۳ درصد باید دقیقاً برابر با **۱۰۰** باشد.")

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR REPLACE INTO shop_settings (key, value) VALUES ('courier_pct', ?)", (courier,))
            await db.execute("INSERT OR REPLACE INTO shop_settings (key, value) VALUES ('courier_treasury_pct', ?)", (treasury,))
            await db.execute("INSERT OR REPLACE INTO shop_settings (key, value) VALUES ('courier_tax_pct', ?)", (tax,))
            await db.commit()

    await message.reply(
        f"✅ <b>درصدهای پستچی با موفقیت به‌روزرسانی شد!</b>\n\n"
        f"🚚 سهم پستچی: <b>%{courier}</b>\n"
        f"🏛 سهم خزانه: <b>%{treasury}</b>\n"
        f"🔥 مالیات (سوخت): <b>%{tax}</b>",
        parse_mode="HTML"
    )


@admin_router.message(Command("treasury"))
async def cmd_treasury_status(message: Message):
    if not is_super_admin(message.from_user.id):
        return

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT balance FROM users WHERE user_id = 0") as cur:
            bank = await cur.fetchone()
            balance = bank["balance"] if bank else 0

    await message.reply(
        f"🏛️ <b>موجودی خزانه بانک مرکزی:</b>\n"
        f"<code>₳ {balance:,}</code>",
        parse_mode="HTML"
    )


@admin_router.message(Command("withdraw_treasury"))
async def cmd_withdraw_treasury(message: Message):
    user_id = message.from_user.id
    if not is_super_admin(user_id):
        return await message.reply("❌ فقط سوپرادمین به خزانه دسترسی دارد.")

    args = message.text.split()
    if len(args) < 2:
        return await message.reply(
            "❌ <b>نحوه استفاده از دستور:</b>\n\n"
            "• برداشت به حساب خودتان:\n<code>/withdraw_treasury 50000</code>\n\n"
            "• برداشت و واریز به حساب دیگری:\n<code>/withdraw_treasury 50000 12345678</code>",
            parse_mode="HTML"
        )

    try:
        amount = int(args[1])
        target_user_id = int(args[2]) if len(args) >= 3 else user_id
    except ValueError:
        return await message.reply("❌ مبلغ یا آیدی واردشده نامعتبر است.")

    if amount <= 0:
        return await message.reply("❌ مبلغ برداشت باید بزرگتر از صفر باشد.")

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")

            async with db.execute("SELECT balance FROM users WHERE user_id = 0") as cur:
                treasury = await cur.fetchone()
                treasury_balance = treasury["balance"] if treasury else 0

            if treasury_balance < amount:
                await db.execute("ROLLBACK")
                return await message.reply(f"❌ موجودی خزانه کافی نیست!\n💰 موجودی فعلی: <code>₳ {treasury_balance:,}</code>", parse_mode="HTML")

            await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = 0", (amount,))
            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, target_user_id))

            tx_id = f"TX-TR-WITHDRAW-{uuid.uuid4().hex[:6]}"
            now_iso = datetime.now(timezone.utc).isoformat()
            await db.execute("""
                INSERT INTO audit_logs (tx_id, timestamp, from_user, to_user, amount, reason, status)
                VALUES (?, ?, 0, ?, ?, 'برداشت مستقیم ادمین از خزانه', 'SUCCESS')
            """, (tx_id, now_iso, target_user_id, amount))

            await db.commit()

    await message.reply(
        f"✅ <b>برداشت از خزانه با موفقیت انجام شد!</b>\n\n"
        f"💸 مبلغ برداشت شده: <code>₳ {amount:,}</code>\n"
        f"📥 مقصد واریز: <code>{target_user_id}</code>\n"
        f"🔖 شناسه تراکنش: <code>{tx_id}</code>",
        parse_mode="HTML"
    )


# --- بخش مدیریت و ادمین عمومی ---

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

    header = f"👥 <b>لیست تمام کاربران</b> (<code>{len(users)}</code> نفر)\n\n"
    
    parts = []
    current = header
    for idx, u in enumerate(users, start=1):
        safe_full_name = html.escape(u['full_name'] or 'ناشناس')
        safe_group_name = html.escape(u['group_name'] or 'Default')
        chunk = (
            f"<b>{idx}. {safe_full_name}</b>\n"
            f"شماره حساب: <code>{u['user_id']}</code>\n"
            f"موجودی: <code>₳ {u['balance']}</code>\n"
            f"گروه: <b>{safe_group_name}</b>\n"
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
        await message.reply(part, parse_mode="HTML")


@admin_router.callback_query(F.data.startswith("users_page_"))
async def cb_users_page(callback: CallbackQuery):
    if not await check_admin_filter(callback.message):
        return await callback.answer("عدم دسترسی.", show_alert=True)

    page = int(callback.data.split("_")[2])
    text, kb = await get_users_page(page)
    
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass
    await callback.answer()


@admin_router.callback_query(F.data == "users_noop")
async def cb_users_noop(callback: CallbackQuery):
    await callback.answer()


@admin_router.message(Command("create_group"))
async def cmd_create_group(message: Message):
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

    safe_g_name = html.escape(g_name)

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM groups WHERE group_name = ?", (g_name,)
        )
        exists = await cursor.fetchone()
        if exists:
            return await message.reply(f"ℹ️ گروه <b>{safe_g_name}</b> از قبل وجود دارد.", parse_mode="HTML")

        await db.execute(
            "INSERT INTO groups (group_name) VALUES (?)", (g_name,)
        )
        await db.commit()

    await message.reply(
        f"✅ گروه <b>{safe_g_name}</b> با موفقیت به لیست گروه‌ها اضافه شد.\n"
        f"(هیچ لینکی ساخته نشد)",
        parse_mode="HTML",
    )


@admin_router.message(Command("add_group"))
async def cmd_add_group(message: Message):
    if not is_private(message) or not await check_admin_filter(message):
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply(
            "⚠️ راهنما:\n"
            "/add_group [نام_گروه_مجازی]\n\n"
            "این دستور یک <b>گروه مجازی</b> درون ربات ایجاد میکند (نه یک گروه تلگرامی).\n"
            "کاربران با استفاده از لینک تولیدشده میتوانند به این گروه در ربات بپیوندند.",
            parse_mode="HTML"
        )

    g_name = args[1].strip()
    if not g_name:
        return await message.reply("❌ نام گروه نمی‌تواند خالی باشد.")

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

    safe_g_name = html.escape(g_name)

    await message.reply(
        f"✅ <b>گروه مجازی «{safe_g_name}»</b> با موفقیت در سیستم ربات ایجاد شد.\n\n"
        f"🔗 <b>لینک عضویت اختصاصی:</b>\n{link}\n\n"
        f"📌 توجه: این گروه صرفاً یک برچسب درون ربات است و ارتباطی با گروه‌های تلگرام ندارد.\n"
        f"کاربران با کلیک روی لینک فوق، به این گروه در ربات ملحق میشوند.",
        parse_mode="HTML"
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
    safe_g_name = html.escape(g_name)

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT code FROM group_links WHERE group_name = ? ORDER BY rowid DESC LIMIT 1",
                (g_name,),
            )
            row = await cursor.fetchone()
            if not row:
                return await message.reply(f"❌ گروهی با نام {safe_g_name} یا لینکی برای آن پیدا نشد.", parse_mode="HTML")

            old_code = row[0]

            try:
                await db.execute("ALTER TABLE group_links ADD COLUMN expires_at TEXT")
            except Exception:
                pass

            new_expires = (datetime.now(timezone.utc) + timedelta(days=extra_days)).isoformat()
            await db.execute(
                "UPDATE group_links SET expires_at = ? WHERE code = ?",
                (new_expires, old_code),
            )
            await db.commit()

    bot_info = await message.bot.get_me()
    link = f"https://t.me/{bot_info.username}?start=G_{old_code}"

    await message.reply(
        f"✅ لینک گروه <b>{safe_g_name}</b> به مدت {extra_days} روز تمدید شد.\n\n"
        f"🔗 لینک:\n{link}",
        parse_mode="HTML",
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
    safe_g_name = html.escape(g_name)

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT 1 FROM groups WHERE group_name = ?", (g_name,)
            )
            if not await cursor.fetchone():
                return await message.reply(f"❌ گروهی با نام {safe_g_name} پیدا نشد.", parse_mode="HTML")

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
        f"✅ لینک جدید برای گروه <b>{safe_g_name}</b> ساخته شد.\n"
        f"مدت اعتبار: {days} روز\n\n"
        f"🔗 لینک جدید:\n{link}",
        parse_mode="HTML",
    )


@admin_router.message(Command("rename_group"))
async def cmd_rename_group(message: Message):
    if not is_private(message) or not await check_admin_filter(message):
        return
    args = message.text.split()
    if len(args) < 3:
        return await message.reply("استفاده: <code>/rename_group [قدیمی] [جدید]</code>", parse_mode="HTML")
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
    txt = "👥 <b>لیست گروه‌ها:</b>\n"
    for r in rows:
        safe_name = html.escape(r[0])
        txt += f"- <code>{safe_name}</code>\n"
    await message.reply(txt, parse_mode="HTML")


@admin_router.message(Command("group_users"))
async def cmd_group_users(message: Message):
    if not is_private(message) or not await check_admin_filter(message):
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("استفاده: <code>/group_users [نام_گروه]</code>", parse_mode="HTML")
    g_name = args[1]
    safe_g_name = html.escape(g_name)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id, full_name, balance, is_frozen FROM users WHERE group_name = ?",
            (g_name,),
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        return await message.reply("عضوی یافت نشد.")
    txt = f"👥 <b>اعضای گروه {safe_g_name}:</b>\n"
    for r in rows:
        status = "❄️ فریز" if r["is_frozen"] else "🟢 فعال"
        safe_full_name = html.escape(r['full_name'] or 'ناشناس')
        txt += (
            f"- <b>{safe_full_name}</b> | شماره حساب: <code>{r['user_id']}</code> | موجودی: <code>₳ {r['balance']}</code> | وضعیت: {status}\n"
        )
    await message.reply(txt, parse_mode="HTML")


@admin_router.message(Command("move_group"))
async def cmd_move_group(message: Message):
    if not is_private(message) or not await check_admin_filter(message):
        return
    args = message.text.split()
    if len(args) < 3:
        return await message.reply("استفاده: <code>/move_group [آیدی] [گروه]</code>", parse_mode="HTML")
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
        return await message.reply("استفاده: <code>/remove_group [آیدی]</code>", parse_mode="HTML")
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
        return await message.reply("استفاده: <code>/delete_group [نام_گروه]</code>", parse_mode="HTML")
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
            "❌ ساختار: <code>/give [آیدی] [مقدار] [دلیل_اختیاری]</code>",
            parse_mode="HTML"
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

    safe_target_name = html.escape(target_data['full_name'] or 'ناشناس')
    safe_reason = html.escape(reason)

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
        f"⚠️ <b>تأیید واریز مدیریتی</b>\n\n"
        f"👤 گیرنده: <b>{safe_target_name}</b> (<code>{target}</code>)\n"
        f"💰 مبلغ: <code>₳ {amount}</code>\n"
        f"📝 دلیل: {safe_reason}\n\n"
        f"آیا مطمئن هستید؟",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await state.set_state(AdminConfirmForm.waiting_for_confirm)


@admin_router.message(Command("take"))
async def cmd_take(message: Message, state: FSMContext):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=3)
    if len(args) < 3:
        return await message.reply(
            "❌ ساختار: <code>/take [آیدی] [مقدار] [دلیل_اختیاری]</code>",
            parse_mode="HTML"
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

    safe_target_name = html.escape(target_data['full_name'] or 'ناشناس')
    safe_reason = html.escape(reason)

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
        f"⚠️ <b>تأیید کسر مدیریتی</b>\n\n"
        f"👤 از حساب: <b>{safe_target_name}</b> (<code>{target}</code>)\n"
        f"💰 مبلغ: <code>₳ {amount}</code>\n"
        f"📝 دلیل: {safe_reason}\n\n"
        f"آیا مطمئن هستید؟",
        reply_markup=kb,
        parse_mode="HTML",
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

    safe_target_name = html.escape(target_name)
    safe_reason = html.escape(reason)

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
                result_text = f"✅ واریز شد.\n👤 به: <b>{safe_target_name}</b> (<code>{target}</code>)\n💰 مبلغ: <code>₳ {amount}</code>\n🔖 شناسه: <code>{tx_id}</code>"
                notify_text = (
                    f"📢 <b>عملیات سوپرادمین</b>\n\n"
                    f"👑 ادمین: <code>{admin_id}</code>\n"
                    f"➕ واریز به: <b>{safe_target_name}</b> (<code>{target}</code>)\n"
                    f"💰 مبلغ: <code>₳ {amount}</code>\n"
                    f"📝 دلیل: {safe_reason}\n"
                    f"🔖 شناسه: <code>{tx_id}</code>"
                )
            else:
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
                result_text = f"🔥 کسر شد.\n👤 از: <b>{safe_target_name}</b> (<code>{target}</code>)\n💰 مبلغ: <code>₳ {amount}</code>\n🔖 شناسه: <code>{tx_id}</code>"
                notify_text = (
                    f"📢 <b>عملیات سوپرادمین</b>\n\n"
                    f"👑 ادمین: <code>{admin_id}</code>\n"
                    f"➖ کسر از: <b>{safe_target_name}</b> (<code>{target}</code>)\n"
                    f"💰 مبلغ: <code>₳ {amount}</code>\n"
                    f"📝 دلیل: {safe_reason}\n"
                    f"🔖 شناسه: <code>{tx_id}</code>"
                )

    await callback.message.edit_text(result_text, parse_mode="HTML")

    for sa_id in SUPER_ADMINS:
        if sa_id != admin_id:
            try:
                await callback.bot.send_message(sa_id, notify_text, parse_mode="HTML")
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
            "استفاده: <code>/rewardgroup [گروه] [مقدار] [دلیل]</code>",
            parse_mode="HTML"
        )

    g_name, amount = args[1], int(args[2])
    reason = args[3] if len(args) > 3 else "پاداش گروهی مدیریت"
    if amount <= 0:
        return await message.reply("❌ مقدار نامعتبر است.")

    safe_g_name = html.escape(g_name)

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
                        "UPDATE users SET balance = balance + ? WHERE user_id = ?",
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
        f"📊 <b>گزارش واریز گروهی ({safe_g_name}):</b>\n\n"
        f"✅ موفق: <code>{success_p}</code> کاربر\n"
        f"❄️ اسکیپ (فریز): <code>{skipped_p}</code> کاربر\n"
        f"❌ خطا: <code>{failed_p}</code> کاربر\n"
        f"💰 توزیع شده: <code>₳ {total_dist}</code>",
        parse_mode="HTML",
    )


@admin_router.message(Command("undo"))
async def cmd_undo(message: Message):
    if not is_super_admin(message.from_user.id):
        return await message.reply("❌ این دستور فقط مخصوص سوپرادمین است.")
    args = message.text.split(maxsplit=2)
    if len(args) < 2:
        return await message.reply(
            "استفاده: <code>/undo [شناسه_تراکنش] [دلیل_اختیاری]</code>",
            parse_mode="HTML"
        )

    tx_id = args[1]
    reason = args[2] if len(args) > 2 else "لغو تراکنش توسط مدیریت ارشد"

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")

            async with db.execute(
                "SELECT from_user, to_user, amount, status FROM audit_logs WHERE tx_id = ?",
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
        f"🔄 تراکنش با موفقیت معکوس شد.\n🔖 شناسه برگشتی: <code>{new_tx_id}</code>",
        parse_mode="HTML",
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
        f"👥 کل اعضا: <code>{row[0]}</code> | 💰 حجم نقدینگی در گردش: <code>₳ {row[1] or 0}</code>",
        parse_mode="HTML"
    )


@admin_router.message(Command("promote"))
async def cmd_promote(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("استفاده: <code>/promote [آیدی]</code>", parse_mode="HTML")
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
        return await message.reply("استفاده: <code>/demote [آیدی]</code>", parse_mode="HTML")
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

    txt = "👑 <b>لیست سوپرادمین‌ها:</b>\n"
    for sa_id in sorted(SUPER_ADMINS):
        u = await get_user_data(sa_id)
        name = html.escape(u["full_name"]) if u and u["full_name"] else "ناشناس"
        txt += f"- <b>{name}</b> | <code>{sa_id}</code>\n"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id, full_name FROM users WHERE is_admin = 1"
        ) as cur:
            admins = await cur.fetchall()

    txt += "\n👥 <b>لیست ادمین‌های معمولی:</b>\n"
    if admins:
        for a in admins:
            if a["user_id"] not in SUPER_ADMINS:
                safe_name = html.escape(a["full_name"] or "ناشناس")
                txt += f"- <b>{safe_name}</b> | <code>{a['user_id']}</code>\n"
    else:
        txt += "- هیچ ادمین معمولی وجود ندارد.\n"

    await message.reply(txt, parse_mode="HTML")


@admin_router.message(Command("add_super"))
async def cmd_add_super(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("استفاده: <code>/add_super [آیدی]</code>", parse_mode="HTML")
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

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_admin = 1 WHERE user_id = ?", (new_id,)
        )
        await db.commit()

    await message.reply(f"✅ کاربر <code>{new_id}</code> به سوپرادمین‌ها اضافه شد.", parse_mode="HTML")


@admin_router.message(Command("remove_super"))
async def cmd_remove_super(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("استفاده: <code>/remove_super [آیدی]</code>", parse_mode="HTML")
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

    await message.reply(f"✅ کاربر <code>{rem_id}</code> از سوپرادمین‌ها حذف شد.", parse_mode="HTML")


@admin_router.message(Command("freeze"))
async def cmd_freeze(message: Message):
    if not is_super_admin(message.from_user.id):
        return await message.reply("❌ این دستور فقط مخصوص سوپرادمین است.")
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("استفاده: <code>/freeze [آیدی]</code>", parse_mode="HTML")
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
        return await message.reply("استفاده: <code>/unfreeze [آیدی]</code>", parse_mode="HTML")
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
        return await message.reply("استفاده: <code>/check [آیدی]</code>", parse_mode="HTML")
    u = await get_user_data(int(args[1]))
    if u:
        status = "❄️ فریز شده" if u["is_frozen"] else "🟢 فعال"
        admin_st = "👑 ادمین" if u["is_admin"] else "👤 کاربر عادی"
        safe_full_name = html.escape(u['full_name'] or 'ناشناس')
        safe_username = html.escape(u['username'] or 'بدون آیدی')
        safe_group_name = html.escape(u['group_name'] or 'Default')
        await message.reply(
            f"🔎 <b>اطلاعات کامل کاربر <code>{args[1]}</code>:</b>\n\n"
            f"👤 نام کامل: {safe_full_name}\n"
            f"🏷 نام کاربری: @{safe_username}\n"
            f"💰 موجودی: <code>₳ {u['balance']}</code>\n"
            f"👥 گروه: <b>{safe_group_name}</b>\n"
            f"⚡ وضعیت: {status}\n"
            f"🛡 دسترسی: {admin_st}",
            parse_mode="HTML",
        )


# --- دستورات بکاپ‌گیری دستی و بازیابی ---

@admin_router.message(Command("backup_now"))
async def cmd_backup_now(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return

    zip_path = create_zip_backup("manual")
    if zip_path and os.path.exists(zip_path):
        await message.reply_document(
            FSInputFile(zip_path), caption="<b>📦 فایل بکاپ کامل دیتابیس (ZIP)</b>", parse_mode="HTML"
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
                caption=f"<b>📦 بکاپ دستی دیتابیس (توسط سوپرادمین)</b>\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                parse_mode="HTML"
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
