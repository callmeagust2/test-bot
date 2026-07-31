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


class ResetForm(StatesGroup):
    waiting_for_confirm = State()


# --- FSMهای جدید بخش فروشگاه ---
class AddProductForm(StatesGroup):
    waiting_for_photo = State()
    waiting_for_name = State()
    waiting_for_description = State()
    waiting_for_price = State()
    waiting_for_shipping = State()
    waiting_for_stock_type = State()
    waiting_for_stock_qty = State()
    waiting_for_confirm = State()


class CheckoutForm(StatesGroup):
    waiting_for_address = State()
    waiting_for_phone = State()
    waiting_for_confirm = State()


class ShopRequestForm(StatesGroup):
    waiting_for_shop_name = State()


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

        # --- جدول‌های جدید فروشگاه و پیک ---
        await db.execute("""
            CREATE TABLE IF NOT EXISTS system_rates (
                id INTEGER PRIMARY KEY,
                seller_rate REAL DEFAULT 80.0,
                seller_treasury REAL DEFAULT 15.0,
                seller_tax REAL DEFAULT 5.0,
                courier_rate REAL DEFAULT 80.0,
                courier_treasury REAL DEFAULT 15.0,
                courier_tax REAL DEFAULT 5.0
            )
        """)
        await db.execute(
            "INSERT OR IGNORE INTO system_rates (id, seller_rate, seller_treasury, seller_tax, courier_rate, courier_treasury, courier_tax) VALUES (1, 80.0, 15.0, 5.0, 80.0, 15.0, 5.0)"
        )

        await db.execute("""
            CREATE TABLE IF NOT EXISTS couriers (
                user_id INTEGER PRIMARY KEY,
                added_at TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS shop_requests (
                user_id INTEGER PRIMARY KEY,
                shop_name TEXT,
                status TEXT DEFAULT 'PENDING',
                requested_at TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS shops (
                seller_id INTEGER PRIMARY KEY,
                shop_name TEXT,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS products (
                product_id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER,
                photo_id TEXT,
                name TEXT,
                description TEXT,
                price INTEGER,
                needs_shipping BOOLEAN,
                stock_type TEXT,
                stock_qty INTEGER DEFAULT -1,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER,
                buyer_id INTEGER,
                seller_id INTEGER,
                price INTEGER,
                shipping_fee INTEGER,
                total_price INTEGER,
                delivery_code TEXT UNIQUE,
                needs_shipping BOOLEAN,
                shipping_address TEXT,
                phone_number TEXT,
                status TEXT DEFAULT 'PAID',
                created_at TEXT,
                delivered_at TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS purchases (
                purchase_id INTEGER PRIMARY KEY AUTOINCREMENT,
                buyer_id INTEGER,
                order_id INTEGER,
                product_id INTEGER,
                product_name TEXT,
                photo_id TEXT,
                price_paid INTEGER,
                code_or_license TEXT,
                created_at TEXT
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


# --- توابع کمکی فروشگاه و درصدها ---
async def get_system_rates():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM system_rates WHERE id = 1") as cur:
            row = await cur.fetchone()
            if row:
                return dict(row)
            return {
                "seller_rate": 80.0, "seller_treasury": 15.0, "seller_tax": 5.0,
                "courier_rate": 80.0, "courier_treasury": 15.0, "courier_tax": 5.0
            }


async def is_seller(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM shops WHERE seller_id = ? AND is_active = 1", (user_id,)) as cur:
            return await cur.fetchone() is not None


async def is_courier(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM couriers WHERE user_id = ?", (user_id,)) as cur:
            return await cur.fetchone() is not None


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
                    caption=f"<b>📦 بکاپ خودکار دیتابیس</b>\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    parse_mode="HTML"
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
async def cmd_start(message: Message, state: FSMContext):
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

        # لینک عضویت در گروه
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

        # 🛍️ خرید مستقیم محصول از کانال (Deep-Linking)
        elif payload.startswith("prod_") or payload.isdigit():
            prod_id_str = payload.replace("prod_", "")
            if not prod_id_str.isdigit():
                return await message.reply("❌ شناسه محصول نامعتبر است.")
            
            prod_id = int(prod_id_str)
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("SELECT * FROM products WHERE product_id = ? AND is_active = 1", (prod_id,)) as cur:
                    p = await cur.fetchone()

            if not p:
                return await message.reply("❌ کالا یافت نشد یا غیرفعال شده است.")

            if p["stock_type"] == "limited" and p["stock_qty"] <= 0:
                return await message.reply("❌ موجودی این کالا به اتمام رسیده است.")

            shipping_cost = int(p["price"] * 0.12) if p["needs_shipping"] else 0
            total_price = p["price"] + shipping_cost

            caption = (
                f"🛍️ <b>صدور فاکتور خرید</b>\n\n"
                f"📦 نام کالا: <b>{html.escape(p['name'])}</b>\n"
                f"📝 توضیحات: {html.escape(p['description'])}\n"
                f"💰 قیمت اصل کالا: <code>₳ {p['price']}</code>\n"
            )
            if p["needs_shipping"]:
                caption += f"🚚 هزینه پست (۱۲٪): <code>₳ {shipping_cost}</code>\n"
            
            caption += f"💳 <b>مبلغ کل فاکتور: <code>₳ {total_price}</code></b>"

            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🛒 تایید و پرداخت فاکتور", callback_data=f"buy_prod_{prod_id}"),
                InlineKeyboardButton(text="❌ انصراف", callback_data="buy_cancel")
            ]])

            if p["photo_id"]:
                return await message.reply_photo(photo=p["photo_id"], caption=caption, reply_markup=kb, parse_mode="HTML")
            else:
                return await message.reply(caption, reply_markup=kb, parse_mode="HTML")

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


# =====================================================================
# 🛒 بخش ۴: خریداران و سفارش‌های من
# =====================================================================

@shop_router.callback_query(F.data.startswith("buy_prod_"))
async def cb_buy_product(callback: CallbackQuery, state: FSMContext):
    prod_id = int(callback.data.split("_")[2])
    buyer_id = callback.from_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM products WHERE product_id = ? AND is_active = 1", (prod_id,)) as cur:
            p = await cur.fetchone()

    if not p:
        return await callback.answer("❌ این کالا وجود ندارد یا غیرفعال شده است.", show_alert=True)

    u = await get_user_data(buyer_id)
    if not u or u["is_frozen"]:
        return await callback.answer("❌ حساب شما فریز یا نامعتبر است.", show_alert=True)

    shipping_cost = int(p["price"] * 0.12) if p["needs_shipping"] else 0
    total_price = p["price"] + shipping_cost

    if u["balance"] < total_price:
        return await callback.answer(f"❌ موجودی شما ناکافی است. (موجودی: ₳ {u['balance']})", show_alert=True)

    if p["needs_shipping"]:
        await state.update_data(prod_id=prod_id, total_price=total_price, shipping_cost=shipping_cost)
        await callback.message.reply("🚚 این کالا نیازمند ارسال پستی است.\nلطفاً <b>آدرس دقیق پستی</b> خود را ارسال کنید:", parse_mode="HTML")
        await state.set_state(CheckoutForm.waiting_for_address)
        await callback.answer()
    else:
        # خرید مستقیم کالای دیجیتال / بدون ارسال
        await execute_checkout(callback.message, buyer_id, p, shipping_cost, total_price, "", "")
        await callback.answer()


@shop_router.message(CheckoutForm.waiting_for_address)
async def process_checkout_address(message: Message, state: FSMContext):
    address = message.text.strip()
    await state.update_data(address=address)
    await message.reply("📞 لطفاً <b>شماره تماس</b> خود را وارد کنید:", parse_mode="HTML")
    await state.set_state(CheckoutForm.waiting_for_phone)


@shop_router.message(CheckoutForm.waiting_for_phone)
async def process_checkout_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    data = await state.get_data()
    await state.clear()

    prod_id = data["prod_id"]
    total_price = data["total_price"]
    shipping_cost = data["shipping_cost"]
    address = data["address"]

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM products WHERE product_id = ?", (prod_id,)) as cur:
            p = await cur.fetchone()

    await execute_checkout(message, message.from_user.id, p, shipping_cost, total_price, address, phone)


async def execute_checkout(message_or_msg, buyer_id: int, product, shipping_cost: int, total_price: int, address: str, phone: str):
    delivery_code = str(random.randint(1000000000, 9999999999))
    created_at = datetime.now(timezone.utc).isoformat()

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT balance, is_frozen FROM users WHERE user_id = ?", (buyer_id,)) as cur:
                u = await cur.fetchone()

            if not u or u["is_frozen"] or u["balance"] < total_price:
                return await message_or_msg.reply("❌ خرید ناموفق بود. موجودی ناکافی یا حساب مسدود است.")

            # کسر از موجودی خریدار
            tx_id = f"TX-BUY-{random.randint(100000, 999999)}"
            await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (total_price, buyer_id))
            await db.execute(
                "INSERT INTO audit_logs (tx_id, timestamp, from_user, to_user, amount, reason) VALUES (?, ?, ?, 0, ?, ?)",
                (tx_id, created_at, buyer_id, total_price, f"خرید کالا {product['name']}")
            )

            # ثبت سفارش
            status = "PENDING_DELIVERY" if product["needs_shipping"] else "COMPLETED"
            cursor = await db.execute("""
                INSERT INTO orders (product_id, buyer_id, seller_id, price, shipping_fee, total_price, delivery_code, needs_shipping, shipping_address, phone_number, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (product["product_id"], buyer_id, product["seller_id"], product["price"], shipping_cost, total_price, delivery_code, product["needs_shipping"], address, phone, status, created_at))
            order_id = cursor.lastrowid

            # کسر موجودی انبار کالا
            if product["stock_type"] == "limited":
                await db.execute("UPDATE products SET stock_qty = stock_qty - 1 WHERE product_id = ?", (product["product_id"],))

            # ثبت در کتابخانه خریدهای من
            await db.execute("""
                INSERT INTO purchases (buyer_id, order_id, product_id, product_name, photo_id, price_paid, code_or_license, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (buyer_id, order_id, product["product_id"], product["name"], product["photo_id"], total_price, delivery_code, created_at))

            await db.commit()

            # تسویه حساب آنی در صورتی که نیاز به ارسال پستی نداشته باشد
            if not product["needs_shipping"]:
                rates = await get_system_rates()
                seller_share = int(product["price"] * (rates["seller_rate"] / 100))
                await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (seller_share, product["seller_id"]))
                await db.commit()

    # پیام تسویه به خریدار
    msg_txt = (
        f"🎉 <b>خرید شما با موفقیت انجام شد!</b>\n\n"
        f"📦 نام کالا: <b>{html.escape(product['name'])}</b>\n"
        f"💳 مبلغ پرداختی: <code>₳ {total_price}</code>\n"
        f"🔑 <b>کد تحویل اختصاصی (۱۰ رقمی):</b> <code>{delivery_code}</code>\n\n"
    )
    if product["needs_shipping"]:
        msg_txt += "🚚 سفارش شما در صف ارسال توسط پستچی قرار گرفت. کد ۱۰ رقمی بالا را هنگام تحویل کالا به پستچی ارائه دهید."
    else:
        msg_txt += "✅ این محصول به آرشیو خریدهای شما (`/my_purchases`) اضافه شد."

    await message_or_msg.reply(msg_txt, parse_mode="HTML")

    # اطلاع‌رسانی به فروشنده
    try:
        await message_or_msg.bot.send_message(
            product["seller_id"],
            f"🛒 <b>سفارش جدید برای فروشگاه شما!</b>\n\n"
            f"📦 کالا: <b>{html.escape(product['name'])}</b>\n"
            f"👤 خریدار: <code>{buyer_id}</code>\n"
            f"🔑 کد تحویل ۱۰ رقمی: <code>{delivery_code}</code>\n"
            f"📊 وضعیت: {status}",
            parse_mode="HTML"
        )
    except Exception:
        pass


@shop_router.message(Command("my_orders"))
async def cmd_my_orders(message: Message):
    if not is_private(message):
        return
    buyer_id = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT o.*, p.name as prod_name FROM orders o
            JOIN products p ON o.product_id = p.product_id
            WHERE o.buyer_id = ? ORDER BY o.order_id DESC LIMIT 10
        """, (buyer_id,)) as cur:
            orders = await cur.fetchall()

    if not orders:
        return await message.reply("ℹ️ شما هیچ سفارش جاری یا قبلی ندارید.")

    txt = "🛍️ <b>پیگیری سفارش‌های شما:</b>\n\n"
    for o in orders:
        st = "🟢 تحویل شده" if o["status"] == "COMPLETED" else "🚚 در حال پردازش / ارسال"
        txt += (
            f"📦 کالا: <b>{html.escape(o['prod_name'])}</b>\n"
            f"💳 مبلغ: <code>₳ {o['total_price']}</code>\n"
            f"🔑 کد امنیتی تحویل: <code>{o['delivery_code']}</code>\n"
            f"📌 وضعیت: {st}\n"
            f"------------------------------\n"
        )
    await message.reply(txt, parse_mode="HTML")


@shop_router.message(Command("my_purchases"))
@shop_router.message(Command("my_library"))
async def cmd_my_purchases(message: Message):
    if not is_private(message):
        return
    buyer_id = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM purchases WHERE buyer_id = ? ORDER BY purchase_id DESC", (buyer_id,)) as cur:
            items = await cur.fetchall()

    if not items:
        return await message.reply("📚 آرشیو خریدهای شما خالی است.")

    txt = "📚 <b>کتابخانه و آرشیو خریدهای من:</b>\n\n"
    for idx, item in enumerate(items, start=1):
        txt += (
            f"<b>{idx}. {html.escape(item['product_name'])}</b>\n"
            f"💰 قیمت خریداری شده: <code>₳ {item['price_paid']}</code>\n"
            f"🔑 کد/لایسنس تحویلی: <code>{item['code_or_license']}</code>\n"
            f"📅 تاریخ: {item['created_at'][:10]}\n"
            f"------------------------------\n"
        )
    await message.reply(txt, parse_mode="HTML")


@shop_router.callback_query(F.data == "buy_cancel")
async def cb_buy_cancel(callback: CallbackQuery):
    await callback.message.edit_text("❌ عملیات خرید لغو شد.")


# =====================================================================
# 🛠️ بخش ۱: دسترسی‌ها و مدیریت سوپر ادمین (Super Admin)
# =====================================================================

@admin_router.message(Command("set_shop_rates"))
async def cmd_set_shop_rates(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 4:
        return await message.reply("⚠️ ساختار: <code>/set_shop_rates [فروشنده] [خزانه] [مالیات]</code>\nمثال: <code>/set_shop_rates 80 15 5</code>", parse_mode="HTML")
    try:
        s_rate, tr_rate, tax_rate = float(args[1]), float(args[2]), float(args[3])
        if s_rate + tr_rate + tax_rate != 100.0:
            return await message.reply("❌ مجموع درصدها باید دقیقاً ۱۰۰ باشد.")
    except ValueError:
        return await message.reply("❌ مقادیر درصد باید عدد باشند.")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE system_rates SET seller_rate = ?, seller_treasury = ?, seller_tax = ? WHERE id = 1
        """, (s_rate, tr_rate, tax_rate))
        await db.commit()

    await message.reply(f"✅ درصدهای فروش کالا به‌روزرسانی شد:\n👑 فروشنده: {s_rate}%\n🏦 خزانه: {tr_rate}%\n🔥 مالیات/سوزاندن: {tax_rate}%")


@admin_router.message(Command("set_courier_rates"))
async def cmd_set_courier_rates(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 4:
        return await message.reply("⚠️ ساختار: <code>/set_courier_rates [پستچی] [خزانه] [مالیات]</code>\nمثال: <code>/set_courier_rates 80 15 5</code>", parse_mode="HTML")
    try:
        c_rate, tr_rate, tax_rate = float(args[1]), float(args[2]), float(args[3])
        if c_rate + tr_rate + tax_rate != 100.0:
            return await message.reply("❌ مجموع درصدها باید دقیقاً ۱۰۰ باشد.")
    except ValueError:
        return await message.reply("❌ مقادیر درصد باید عدد باشند.")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE system_rates SET courier_rate = ?, courier_treasury = ?, courier_tax = ? WHERE id = 1
        """, (c_rate, tr_rate, tax_rate))
        await db.commit()

    await message.reply(f"✅ درصدهای هزینه ارسال به‌روزرسانی شد:\n🚴 پستچی: {c_rate}%\n🏦 خزانه: {tr_rate}%\n🔥 مالیات/سوزاندن: {tax_rate}%")


@admin_router.message(Command("add_courier"))
async def cmd_add_courier(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.reply("استفاده: <code>/add_courier [آیدی_عددی_کاربر]</code>", parse_mode="HTML")
    target_id = int(args[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO couriers (user_id, added_at) VALUES (?, ?)", (target_id, datetime.now(timezone.utc).isoformat()))
        await db.commit()
    await message.reply(f"✅ دسترسی پستچی به کاربر <code>{target_id}</code> اعطا شد.", parse_mode="HTML")


@admin_router.message(Command("remove_courier"))
async def cmd_remove_courier(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.reply("استفاده: <code>/remove_courier [آیدی_عددی_کاربر]</code>", parse_mode="HTML")
    target_id = int(args[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM couriers WHERE user_id = ?", (target_id,))
        await db.commit()
    await message.reply(f"🔥 دسترسی پستچی از کاربر <code>{target_id}</code> سلب شد.", parse_mode="HTML")


@admin_router.message(Command("shop_requests"))
async def cmd_shop_requests(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM shop_requests WHERE status = 'PENDING'") as cur:
            reqs = await cur.fetchall()

    if not reqs:
        return await message.reply("ℹ️ هیچ درخواست در انتظاری برای ساخت فروشگاه وجود ندارد.")

    for r in reqs:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ تایید درخواست", callback_data=f"app_shop_{r['user_id']}"),
            InlineKeyboardButton(text="❌ رد درخواست", callback_data=f"rej_shop_{r['user_id']}")
        ]])
        await message.reply(
            f"🏪 <b>درخواست ساخت فروشگاه</b>\n"
            f"👤 کاربر: <code>{r['user_id']}</code>\n"
            f"🏢 نام فروشگاه: <b>{html.escape(r['shop_name'])}</b>\n"
            f"📅 تاریخ: {r['requested_at'][:10]}",
            reply_markup=kb, parse_mode="HTML"
        )


@admin_router.callback_query(F.data.startswith("app_shop_"))
async def cb_approve_shop(callback: CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        return await callback.answer("عدم دسترسی", show_alert=True)
    target_id = int(callback.data.split("_")[2])

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM shop_requests WHERE user_id = ?", (target_id,)) as cur:
            req = await cur.fetchone()
        if req:
            await db.execute("INSERT OR REPLACE INTO shops (seller_id, shop_name, is_active, created_at) VALUES (?, ?, 1, ?)",
                             (target_id, req["shop_name"], datetime.now(timezone.utc).isoformat()))
            await db.execute("UPDATE shop_requests SET status = 'APPROVED' WHERE user_id = ?", (target_id,))
            await db.commit()

    await callback.message.edit_text(f"✅ فروشگاه کاربر <code>{target_id}</code> با موفقیت تایید و فعال شد.", parse_mode="HTML")
    try:
        await callback.bot.send_message(target_id, "🎉 درخواست ساخت فروشگاه شما توسط سوپرادمین تایید شد! اکنون می‌توانید با دستور /add_product محصول اضافه کنید.")
    except Exception:
        pass


@admin_router.callback_query(F.data.startswith("rej_shop_"))
async def cb_reject_shop(callback: CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        return await callback.answer("عدم دسترسی", show_alert=True)
    target_id = int(callback.data.split("_")[2])

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE shop_requests SET status = 'REJECTED' WHERE user_id = ?", (target_id,))
        await db.commit()

    await callback.message.edit_text(f"❌ درخواست ساخت فروشگاه کاربر <code>{target_id}</code> رد شد.", parse_mode="HTML")


@admin_router.message(Command("remove_shop"))
async def cmd_remove_shop(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.reply("استفاده: <code>/remove_shop [آیدی_عددی_فروشنده]</code>", parse_mode="HTML")
    seller_id = int(args[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE shops SET is_active = 0 WHERE seller_id = ?", (seller_id,))
        await db.execute("UPDATE products SET is_active = 0 WHERE seller_id = ?", (seller_id,))
        await db.commit()
    await message.reply(f"🗑 فروشگاه و محصولات کاربر <code>{seller_id}</code> غیرفعال گردید (بدون حذف سوابق دیتابیس).", parse_mode="HTML")


# =====================================================================
# 🏪 بخش ۲: فروشندگان (Seller)
# =====================================================================

@shop_router.message(Command("request_shop"))
async def cmd_request_shop(message: Message, state: FSMContext):
    if not is_private(message):
        return
    user_id = message.from_user.id
    if await is_seller(user_id):
        return await message.reply("ℹ️ شما در حال حاضر صاحب یک فروشگاه فعال هستید.")

    await message.reply("🏪 لطفاً نام پیشنهادی برای فروشگاه خود را وارد کنید:")
    await state.set_state(ShopRequestForm.waiting_for_shop_name)


@shop_router.message(ShopRequestForm.waiting_for_shop_name)
async def process_shop_name(message: Message, state: FSMContext):
    shop_name = message.text.strip()
    user_id = message.from_user.id
    await state.clear()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO shop_requests (user_id, shop_name, status, requested_at) VALUES (?, ?, 'PENDING', ?)",
            (user_id, shop_name, datetime.now(timezone.utc).isoformat())
        )
        await db.commit()

    await message.reply("✅ درخواست شما برای ساخت فروشگاه ثبت گردید و جهت بررسی به سوپر ادین ارسال شد.")


@shop_router.message(Command("my_shop"))
async def cmd_my_shop(message: Message):
    if not is_private(message):
        return
    seller_id = message.from_user.id
    if not await is_seller(seller_id):
        return await message.reply("❌ شما فروشگاه فعال ندارید. با دستور /request_shop درخواست دهید.")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT shop_name FROM shops WHERE seller_id = ?", (seller_id,)) as cur:
            s = await cur.fetchone()
        async with db.execute("SELECT COUNT(*) as cnt FROM products WHERE seller_id = ? AND is_active = 1", (seller_id,)) as cur:
            p_cnt = (await cur.fetchone())["cnt"]
        async with db.execute("SELECT COUNT(*) as sales, SUM(price) as rev FROM orders WHERE seller_id = ? AND status = 'COMPLETED'", (seller_id,)) as cur:
            stats = await cur.fetchone()

    sales_cnt = stats["sales"] or 0
    revenue = stats["rev"] or 0

    await message.reply(
        f"🏢 <b>داشبورد فروشگاه «{html.escape(s['shop_name'])}»</b>\n\n"
        f"📦 تعداد محصولات فعال: <code>{p_cnt}</code>\n"
        f"🛒 تعداد فروش‌های موفق: <code>{sales_cnt}</code>\n"
        f"💰 کل درآمد از فروش: <code>₳ {revenue}</code>\n\n"
        f"🛠 ابزارها:\n"
        f"➕ ثبت محصول جدید: /add_product\n"
        f"📋 مدیریت انبار و کالاها: /inventory\n"
        f"📊 درصدهای فعال سیستم: /shop_rates",
        parse_mode="HTML"
    )


@shop_router.message(Command("shop_rates"))
async def cmd_shop_rates(message: Message):
    rates = await get_system_rates()
    await message.reply(
        f"📊 <b>شفافیت مالی و درصدهای فعال سیستم:</b>\n\n"
        f"🛍️ <b>سهم از فروش اصل کالا:</b>\n"
        f"🔹 سهم خالص فروشنده: <code>{rates['seller_rate']}%</code>\n"
        f"🏦 سهم خزانه بانک: <code>{rates['seller_treasury']}%</code>\n"
        f"🔥 مالیات / سوزاندن: <code>{rates['seller_tax']}%</code>\n\n"
        f"🚚 <b>سهم از هزینه پستی:</b>\n"
        f"🔹 سهم پستچی / پیک: <code>{rates['courier_rate']}%</code>\n"
        f"🏦 سهم خزانه بانک: <code>{rates['courier_treasury']}%</code>\n"
        f"🔥 مالیات / سوزاندن: <code>{rates['courier_tax']}%</code>",
        parse_mode="HTML"
    )


# --- فرآیند ۷ مرحله‌ای ثبت محصول جدید ---
@shop_router.message(Command("add_product"))
async def cmd_add_product(message: Message, state: FSMContext):
    if not is_private(message):
        return
    if not await is_seller(message.from_user.id):
        return await message.reply("❌ شما دسترسی فروشندگی ندارید.")

    await message.reply("📸 <b>مرحله ۱ از ۷:</b> لطفاً عکس تصویر محصول را ارسال کنید (یا کلمه `بدون عکس` را تایپ کنید):", parse_mode="HTML")
    await state.set_state(AddProductForm.waiting_for_photo)


@shop_router.message(AddProductForm.waiting_for_photo)
async def process_prod_photo(message: Message, state: FSMContext):
    if message.photo:
        photo_id = message.photo[-1].file_id
    elif message.text and message.text.strip() == "بدون عکس":
        photo_id = None
    else:
        return await message.reply("❌ لطفاً یک عکس ارسال کنید یا کلمه «بدون عکس» را بنویسید.")

    await state.update_data(photo_id=photo_id)
    await message.reply("📦 <b>مرحله ۲ از ۷:</b> نام کالا را وارد کنید:", parse_mode="HTML")
    await state.set_state(AddProductForm.waiting_for_name)


@shop_router.message(AddProductForm.waiting_for_name)
async def process_prod_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.reply("📝 <b>مرحله ۳ از ۷:</b> توضیحات کامل کالا را وارد کنید:", parse_mode="HTML")
    await state.set_state(AddProductForm.waiting_for_description)


@shop_router.message(AddProductForm.waiting_for_description)
async def process_prod_desc(message: Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await message.reply("💰 <b>مرحله ۴ از ۷:</b> قیمت کالا را به آتر (عدد) وارد کنید:", parse_mode="HTML")
    await state.set_state(AddProductForm.waiting_for_price)


@shop_router.message(AddProductForm.waiting_for_price)
async def process_prod_price(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        return await message.reply("❌ قیمت باید یک عدد مثبت باشد.")
    await state.update_data(price=int(message.text))

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚚 بله (نیازمند ارسال پستی)", callback_data="ship_yes"),
        InlineKeyboardButton(text="⚡ خیر (دیجیتالی / تحویل فوری)", callback_data="ship_no")
    ]])
    await message.reply("🚚 <b>مرحله ۵ از ۷:</b> آیا این کالا نیاز به ارسال پستی/فیزیکی دارد؟\n(در صورت تایید، ۱۲٪ هزینه پست خودکار روی فاکتور خریدار محاسبه می‌شود)", reply_markup=kb, parse_mode="HTML")
    await state.set_state(AddProductForm.waiting_for_shipping)


@shop_router.callback_query(AddProductForm.waiting_for_shipping)
async def process_prod_shipping(callback: CallbackQuery, state: FSMContext):
    needs_shipping = (callback.data == "ship_yes")
    await state.update_data(needs_shipping=needs_shipping)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="1️⃣ تکی (فقط ۱ عدد)", callback_data="stock_single"),
        InlineKeyboardButton(text="🔢 محدود (تعداد مشخص)", callback_data="stock_limited"),
        InlineKeyboardButton(text="♾ نامحدود", callback_data="stock_unlimited")
    ]])
    await callback.message.edit_text("📊 <b>مرحله ۶ از ۷:</b> نوع موجودی کالا را انتخاب کنید:", reply_markup=kb, parse_mode="HTML")
    await state.set_state(AddProductForm.waiting_for_stock_type)


@shop_router.callback_query(AddProductForm.waiting_for_stock_type)
async def process_prod_stock_type(callback: CallbackQuery, state: FSMContext):
    st_type = callback.data.replace("stock_", "")
    await state.update_data(stock_type=st_type)

    if st_type == "limited":
        await callback.message.edit_text("🔢 لطفاً تعداد دقیق موجودی انبار را وارد کنید:")
        await state.set_state(AddProductForm.waiting_for_stock_qty)
    else:
        qty = 1 if st_type == "single" else -1
        await state.update_data(stock_qty=qty)
        await show_product_preview(callback.message, state)


@shop_router.message(AddProductForm.waiting_for_stock_qty)
async def process_prod_stock_qty(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        return await message.reply("❌ تعداد موجودی باید عدد مثبت باشد.")
    await state.update_data(stock_qty=int(message.text))
    await show_product_preview(message, state)


async def show_product_preview(message: Message, state: FSMContext):
    data = await state.get_data()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚀 تایید و انتشار کالا", callback_data="pub_prod_yes"),
        InlineKeyboardButton(text="❌ لغو", callback_data="pub_prod_no")
    ]])

    sh_text = "دارد (۱۲٪ هزینه پست)" if data["needs_shipping"] else "ندارد"
    preview_txt = (
        f"🔍 <b>مرحله ۷ از ۷: پیش‌نمایش و تایید انتشار محصول</b>\n\n"
        f"📦 نام کالا: <b>{html.escape(data['name'])}</b>\n"
        f"📝 توضیحات: {html.escape(data['description'])}\n"
        f"💰 قیمت: <code>₳ {data['price']}</code>\n"
        f"🚚 ارسال پستی: {sh_text}\n"
        f"📊 موجودی: <code>{data['stock_qty'] if data['stock_type']=='limited' else data['stock_type']}</code>\n\n"
        f"آیا محصول فوق مورد تایید است؟"
    )

    if data.get("photo_id"):
        await message.reply_photo(photo=data["photo_id"], caption=preview_txt, reply_markup=kb, parse_mode="HTML")
    else:
        await message.reply(preview_txt, reply_markup=kb, parse_mode="HTML")

    await state.set_state(AddProductForm.waiting_for_confirm)


@shop_router.callback_query(AddProductForm.waiting_for_confirm, F.data == "pub_prod_yes")
async def cb_publish_product(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    seller_id = callback.from_user.id
    await state.clear()

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO products (seller_id, photo_id, name, description, price, needs_shipping, stock_type, stock_qty, is_active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        """, (seller_id, data.get("photo_id"), data["name"], data["description"], data["price"], data["needs_shipping"], data["stock_type"], data["stock_qty"], datetime.now(timezone.utc).isoformat()))
        product_id = cursor.lastrowid
        await db.commit()

    bot_info = await callback.bot.get_me()
    buy_link = f"https://t.me/{bot_info.username}?start=prod_{product_id}"

    await callback.message.edit_text(
        f"🎉 <b>محصول شما با موفقیت منتشر شد!</b>\n\n"
        f"🆔 شناسه کالا: <code>{product_id}</code>\n"
        f"🔗 <b>لینک خرید مستقیم از کانال:</b>\n{buy_link}",
        parse_mode="HTML"
    )


@shop_router.callback_query(AddProductForm.waiting_for_confirm, F.data == "pub_prod_no")
async def cb_cancel_product(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ ساخت محصول جدید لغو شد.")


@shop_router.message(Command("inventory"))
async def cmd_inventory(message: Message):
    if not is_private(message):
        return
    seller_id = message.from_user.id
    if not await is_seller(seller_id):
        return await message.reply("❌ شما دسترسی فروشندگی ندارید.")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM products WHERE seller_id = ? AND is_active = 1", (seller_id,)) as cur:
            prods = await cur.fetchall()

    if not prods:
        return await message.reply("📦 انبار شما خالی است.")

    txt = "📋 <b>لیست و مدیریت انبار محصولات شما:</b>\n\n"
    for p in prods:
        st_qty = "نامحدود" if p["stock_type"] == "unlimited" else p["stock_qty"]
        txt += (
            f"📦 <b>{html.escape(p['name'])}</b> (کد: <code>{p['product_id']}</code>)\n"
            f"💰 قیمت: <code>₳ {p['price']}</code> | موجودی: <code>{st_qty}</code>\n"
            f"------------------------------\n"
        )
    await message.reply(txt, parse_mode="HTML")


# =====================================================================
# 🚴 بخش ۳: پستچی / پیک (Courier)
# =====================================================================

@shop_router.message(Command("courier_orders"))
async def cmd_courier_orders(message: Message):
    if not is_private(message):
        return
    if not await is_courier(message.from_user.id) and not is_super_admin(message.from_user.id):
        return await message.reply("❌ شما دسترسی پستچی ندارید.")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT o.*, p.name as prod_name FROM orders o
            JOIN products p ON o.product_id = p.product_id
            WHERE o.needs_shipping = 1 AND o.status = 'PENDING_DELIVERY'
        """) as cur:
            orders = await cur.fetchall()

    if not orders:
        return await message.reply("📦 هیچ مرسوله‌ای در انتظار تحویل وجود ندارد.")

    txt = "🛵 <b>لیست مرسولات پستی در انتظار تحویل:</b>\n\n"
    for o in orders:
        txt += (
            f"📦 کالا: <b>{html.escape(o['prod_name'])}</b>\n"
            f"👤 خریدار: <code>{o['buyer_id']}</code> | 📞 تلفن: <code>{html.escape(o['phone_number'] or 'ثبت نشده')}</code>\n"
            f"📍 آدرس: {html.escape(o['shipping_address'] or 'ثبت نشده')}\n"
            f"🚚 حق‌الزحمه ارسال: <code>₳ {o['shipping_fee']}</code>\n"
            f"------------------------------\n"
        )
    txt += "✅ پس از تحویل مرسوله، دستور زیر را جهت تایید نهایی و دریافت سهم بزنید:\n<code>/confirm_delivery [کد_۱۰_رقمی_خریدار]</code>"
    await message.reply(txt, parse_mode="HTML")


@shop_router.message(Command("confirm_delivery"))
async def cmd_confirm_delivery(message: Message):
    if not is_private(message):
        return
    courier_id = message.from_user.id
    if not await is_courier(courier_id) and not is_super_admin(courier_id):
        return await message.reply("❌ شما دسترسی پستچی ندارید.")

    args = message.text.split()
    if len(args) < 2:
        return await message.reply("⚠️ راهنما: <code>/confirm_delivery [کد_۱۰_رقمی]</code>", parse_mode="HTML")

    code = args[1].strip()

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM orders WHERE delivery_code = ? AND status = 'PENDING_DELIVERY'", (code,)) as cur:
                order = await cur.fetchone()

            if not order:
                return await message.reply("❌ کد ۱۰ رقمی نامعتبر است یا این سفارش قبلاً تحویل شده است.")

            rates = await get_system_rates()

            # محاسبه سهم فروشنده از اصل کالا
            seller_share = int(order["price"] * (rates["seller_rate"] / 100))
            # محاسبه سهم پستچی از هزینه ارسال
            courier_share = int(order["shipping_fee"] * (rates["courier_rate"] / 100))

            # واریز سهم فروشنده و پستچی
            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (seller_share, order["seller_id"]))
            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (courier_share, courier_id))

            # به‌روزرسانی وضعیت سفارش
            now_iso = datetime.now(timezone.utc).isoformat()
            await db.execute("UPDATE orders SET status = 'COMPLETED', delivered_at = ? WHERE order_id = ?", (now_iso, order["order_id"]))
            await db.commit()

    await message.reply(f"✅ تحویل مرسوله با موفقیت ثبت شد!\n💰 سهم شما از ارسال (<code>₳ {courier_share}</code>) به حسابتان واریز گردید.", parse_mode="HTML")

    # اطلاع‌رسانی به خریدار و فروشنده
    try:
        await message.bot.send_message(order["buyer_id"], "🎉 مرسوله شما توسط پیک تحویل داده شد. با تشکر از خرید شما!")
        await message.bot.send_message(order["seller_id"], f"✅ مرسوله شما با کد تحویل <code>{code}</code> به خریدار تحویل شد و مبلغ <code>₳ {seller_share}</code> به حسابتان واریز گردید.", parse_mode="HTML")
    except Exception:
        pass


@shop_router.message(Command("delivery_rates"))
async def cmd_delivery_rates(message: Message):
    rates = await get_system_rates()
    await message.reply(f"🛵 <b>درصد سهم پستچی از هزینه ارسال:</b> <code>{rates['courier_rate']}%</code>", parse_mode="HTML")


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


# --- دستور صفر کردن دیتابیس و ری‌استارت ---


@admin_router.message(Command("reset_all"))
async def cmd_reset_all(message: Message, state: FSMContext):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="💣 بله، دیتابیس کاملاً پاک شود", callback_data="reset_yes"),
            InlineKeyboardButton(text="❌ انصراف", callback_data="reset_no"),
        ]]
    )
    await message.reply(
        "⚠️ <b>هشدار بسیار مهم!</b>\n\n"
        "آیا مطمئن هستید؟ این دستور تمام داده‌ها، کاربران، گروه‌ها و تراکنش‌ها را <b>حذف کاملاً دائم</b> می‌کند و دیتابیس صفر خواهد شد.\n\n"
        "آیا قصد ادامه دارید؟",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await state.set_state(ResetForm.waiting_for_confirm)


@admin_router.callback_query(ResetForm.waiting_for_confirm, F.data == "reset_yes")
async def cb_reset_yes(callback: CallbackQuery, state: FSMContext):
    if not is_super_admin(callback.from_user.id):
        return await callback.answer("❌ عدم دسترسی.", show_alert=True)

    await state.clear()
    await callback.message.edit_text("⏳ در حال حذف دیتابیس و ری‌ست کردن سیستم...")

    async with db_lock:
        if os.path.exists(DB_PATH):
            try:
                os.remove(DB_PATH)
            except Exception as e:
                return await callback.message.edit_text(f"❌ خطا در حذف فایل دیتابیس: {e}")

        await init_db()

    await callback.message.edit_text("💥 <b>دیتابیس با موفقیت صفر شد و ربات ری‌ست گردید!</b>", parse_mode="HTML")


@admin_router.callback_query(ResetForm.waiting_for_confirm, F.data == "reset_no")
async def cb_reset_no(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ عملیات صفر کردن دیتابیس لغو شد.")


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
            "<b>✅ پایگاه‌داده با موفقیت بازیابی شد!</b> ربات آماده به کار است.",
            parse_mode="HTML"
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
        "📱 <b>راهنمای دستورات کاربران و خریداران:</b>\n"
        "🔹 <code>/start</code> - شروع و دریافت شماره حساب\n"
        "🔹 <code>/profile</code> یا «پروفایل» - مشاهده مشخصات و موجودی\n"
        "🔹 <code>/transfer</code> یا «انتقال آتر» - انتقال آتر\n"
        "🔹 <code>/my_orders</code> - پیگیری سفارش‌های جاری و کد ۱۰ رقمی\n"
        "🔹 <code>/my_purchases</code> - کتابخانه و آرشیو کامل خریدهای من\n\n"
        "🏪 <b>دستورات فروشندگان:</b>\n"
        "🔸 <code>/request_shop</code> - ارسال درخواست افتتاح فروشگاه\n"
        "🔸 <code>/my_shop</code> - داشبورد و آمار فروشگاه\n"
        "🔸 <code>/add_product</code> - ثبت محصول جدید (۷ مرحله‌ای)\n"
        "🔸 <code>/inventory</code> - مدیریت انبار و موجودی کالاها\n"
        "🔸 <code>/shop_rates</code> - مشاهده درصدهای فعال سیستم\n\n"
        "🛵 <b>دستورات پستچی / پیک:</b>\n"
        "🔹 <code>/courier_orders</code> - مشاهده مرسولات در انتظار تحویل\n"
        "🔹 <code>/confirm_delivery [کد_۱۰_رقمی]</code> - تایید تحویل و دریافت سهم\n"
        "🔹 <code>/delivery_rates</code> - مشاهده درصدهای سهم پستچی\n\n"
    )

    if is_adm or is_sa:
        txt += (
            "👥 <b>دستورات ادمین (فقط پیوی):</b>\n"
            "🔹 <code>/users</code> - لیست کاربران\n"
            "🔹 <code>/groups</code> - لیست گروه‌ها\n"
            "🔹 <code>/group_users [نام]</code> - اعضای یک گروه\n"
            "🔹 <code>/create_group [نام]</code> - اضافه کردن گروه\n"
            "🔹 <code>/add_group [نام]</code> - ساخت گروه مجازی + لینک دعوت\n"
            "🔹 <code>/extend_group [نام] [روز]</code> - تمدید لینک گروه\n"
            "🔹 <code>/renew_group [نام] [روز]</code> - ساخت لینک جدید\n"
            "🔹 <code>/rename_group [قدیمی] [جدید]</code> - تغییر نام گروه\n"
            "🔹 <code>/move_group [آیدی] [گروه]</code> - تغییر گروه کاربر\n"
            "🔹 <code>/remove_group [آیدی]</code> - برگرداندن به Default\n\n"
        )

    if is_sa:
        txt += (
            "👑 <b>دستورات سوپرادمین (فقط پیوی):</b>\n"
            "🔸 <code>/set_shop_rates [فروشنده] [خزانه] [مالیات]</code> - تنظیم درصد کالا\n"
            "🔸 <code>/set_courier_rates [پستچی] [خزانه] [مالیات]</code> - تنظیم درصد پست\n"
            "🔸 <code>/add_courier [آیدی]</code> و <code>/remove_courier</code> - مدیریت پستچی‌ها\n"
            "🔸 <code>/shop_requests</code> - مدیریت درخواست‌های فروشگاه\n"
            "🔸 <code>/remove_shop [آیدی]</code> - غیرفعال‌سازی فروشگاه\n"
            "🔸 <code>/give [آیدی] [مقدار]</code> - واریز مدیریتی\n"
            "🔸 <code>/take [آیدی] [مقدار]</code> - کسر مدیریتی\n"
            "🔸 <code>/rewardgroup [گروه] [مقدار]</code> - پاداش گروهی\n"
            "🔸 <code>/undo [شناسه]</code> - برگشت تراکنش\n"
            "🔸 <code>/economy</code> - آمار اقتصاد\n"
            "🔸 <code>/check [آیدی]</code> - اطلاعات کامل کاربر\n"
            "🔸 <code>/promote</code> و <code>/demote</code> - ارتقا/عزل ادمین\n"
            "🔸 <code>/list_admins</code> - مشاهده لیست ادمین‌ها\n"
            "🔸 <code>/add_super</code> و <code>/remove_super</code> - مدیریت سوپرادمین‌ها\n"
            "🔸 <code>/freeze</code> و <code>/unfreeze</code> - مسدود/فعال‌سازی حساب\n"
            "🔸 <code>/backup_now</code> - بکاپ ZIP\n"
            "🔸 <code>/force_backup</code> - ارسال بکاپ به کانال\n"
            "🔸 <code>/restore</code> - بازیابی دیتابیس\n"
            "🔸 <code>/reset_all</code> - پاکسازی کامل دیتابیس\n"
        )

    await message.reply(txt, parse_mode="HTML")


async def main():
    bot = Bot(token=BOT_TOKEN)

    await start_dummy_server()
    await restore_db_from_telegram(bot)
    await init_db()

    asyncio.create_task(auto_backup_loop(bot))

    dp = Dispatcher(storage=MemoryStorage())

    dp.include_router(admin_router)
    dp.include_router(shop_router)
    dp.include_router(user_router)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
