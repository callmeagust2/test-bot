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

# --- 🏛 سیستم مالی جدید: خزانه مرکزی، بانک آترامنتوم و وام پویا ---
# طبق درخواست، این دو متغیر به‌صورت جداگانه تعریف می‌شوند تا منطق مالی خزانه
# (که صرفاً یک حساب در جدول users است) از منطق دسترسی مدیریتی (سوپرادمین) تفکیک شود.
# از نظر عددی هر دو برابر با آیدی سوپرادمین اول هستند، اما در کد برای دو منظور متفاوت استفاده می‌شوند.
TREASURY_USER_ID = 8490505070
SUPER_ADMIN_ID = 8490505070

BANK_SAVINGS_CAP = 4000  # سقف سپرده‌گذاری در بانک آترامنتوم (آتر)
BANK_GRACE_PERIOD_HOURS = 24  # مهلت طلایی پرداخت اقساط قبل از جریمه دیرکرد
IRAN_TZ = timezone(timedelta(hours=3, minutes=30))  # UTC+3:30

# قفل هم‌روندی ناهمگام برای ایمن‌سازی تراکنش‌های مالی در برابر Race Condition
# همان قفل سراسری برای تراکنش‌های خزانه/بانک/وام هم استفاده می‌شود تا از قفل‌شدگی متقابل
# (Deadlock) بین چند قفل مجزا جلوگیری شود؛ تمام عملیات مالی این ماژول‌ها هم زیر همین قفل انجام می‌گیرند.
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


# --- FSM States جدید برای بخش فروشگاه و پستی ---
class AddProductForm(StatesGroup):
    waiting_for_photo = State()
    waiting_for_title = State()
    waiting_for_description = State()
    waiting_for_price = State()
    waiting_for_stock_type = State()
    waiting_for_stock = State()
    waiting_for_needs_courier = State()


class RequestShopForm(StatesGroup):
    waiting_for_channel = State()


# --- FSM States جدید برای بانک آترامنتوم و وام پویا ---
class BankForm(StatesGroup):
    waiting_for_deposit_amount = State()
    waiting_for_withdraw_amount = State()


class LoanForm(StatesGroup):
    waiting_for_amount = State()
    waiting_for_installments = State()
    waiting_for_method = State()
    waiting_for_guarantor = State()


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

        # --- جداول جدید ساختار فروشگاهی و پستی ---
        await db.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                val REAL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS shops (
                shop_id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER,
                channel_id TEXT,
                channel_title TEXT,
                status TEXT DEFAULT 'PENDING'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS products (
                product_id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id INTEGER,
                photo_id TEXT,
                title TEXT,
                description TEXT,
                price INTEGER,
                stock_type TEXT, -- 'SINGLE', 'LIMITED', 'UNLIMITED'
                stock_qty INTEGER DEFAULT 1,
                needs_courier BOOLEAN DEFAULT FALSE,
                channel_msg_id INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS couriers (
                user_id INTEGER PRIMARY KEY
            )
        """)

        # --- 🏦 مهاجرت (Migration) ستون‌های بانک آترامنتوم و وام پویا در جدول users ---
        # (بدون حذف هیچ‌کدام از داده‌ها یا ستون‌های قبلی)
        for col_name, col_type in [
            ("bank_savings", "INTEGER DEFAULT 0"),
            ("frozen_balance", "INTEGER DEFAULT 0"),
            ("last_bank_claim", "DATETIME DEFAULT NULL"),
            ("last_daily_profit", "INTEGER DEFAULT 0"),
        ]:
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
            except Exception:
                pass  # ستون از قبل وجود دارد
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id INTEGER PRIMARY KEY AUTOINCREMENT,
                code_10 TEXT UNIQUE,
                buyer_id INTEGER,
                shop_id INTEGER,
                product_id INTEGER,
                price INTEGER,
                courier_fee INTEGER,
                courier_id INTEGER DEFAULT 0,
                status TEXT DEFAULT 'PENDING', -- 'PENDING', 'DISPATCHED', 'DELIVERED'
                product_title TEXT,
                product_desc TEXT,
                product_photo_id TEXT
            )
        """)

        # --- 🏛 جداول جدید سیستم مالی: بانک آترامنتوم و وام پویا ---
        await db.execute("""
            CREATE TABLE IF NOT EXISTS loans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                guarantor_id INTEGER,
                total_amount INTEGER,
                interest_rate REAL,
                total_repayment INTEGER,
                installments_count INTEGER,
                status TEXT,          -- PENDING_GUARANTOR, PENDING_ADMIN, ACTIVE, REJECTED, PAID, FAILED
                loan_type TEXT,       -- COLLATERAL, GUARANTOR
                created_at TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS loan_installments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                loan_id INTEGER,
                installment_number INTEGER,
                amount INTEGER,
                due_date TIMESTAMP,
                status TEXT,          -- PENDING, PAID, DEFAULTED
                paid_at TIMESTAMP
            )
        """)
        # ستون‌های کمکی برای محاسبه صحیح جریمه دیرکرد بدون از دست دادن مبلغ پایه قسط
        for col_name, col_type in [
            ("base_amount", "INTEGER"),
            ("penalty_amount", "INTEGER DEFAULT 0"),
            ("last_reminder_stage", "TEXT DEFAULT ''"),
        ]:
            try:
                await db.execute(f"ALTER TABLE loan_installments ADD COLUMN {col_name} {col_type}")
            except Exception:
                pass  # ستون از قبل وجود دارد

        # --- مهاجرت (Migration) ستون‌های اسنپ‌شات محصول برای دیتابیس‌های قدیمی ---
        # این ستون‌ها باعث می‌شوند حذف فروشگاه/محصول، اطلاعات سفارش‌های قبلی خریداران را از بین نبرد
        for col_name, col_type in [
            ("product_title", "TEXT"),
            ("product_desc", "TEXT"),
            ("product_photo_id", "TEXT"),
        ]:
            try:
                await db.execute(f"ALTER TABLE orders ADD COLUMN {col_name} {col_type}")
            except Exception:
                pass  # ستون از قبل وجود دارد

        # درج تنظیمات پیش‌فرض نرخ مالیات و نرخ پست در صورت عدم وجود
        default_settings = [
            ("shop_seller_pct", 51.0),
            ("shop_bank_pct", 40.0),
            ("shop_burn_pct", 9.0),
            ("courier_pct", 61.0),
            ("courier_bank_pct", 30.0),
            ("courier_burn_pct", 9.0),
            ("tier1_pct", 8.0),   # تا 99 آتر
            ("tier2_pct", 10.0),  # 100 تا 999 آتر
            ("tier3_pct", 12.0),  # 1000 آتر به بالا

            # --- 🏦 تنظیمات پیش‌فرض بانک آترامنتوم ---
            ("bank_daily_rate", 1.23),   # درصد سود روزانه پیش‌فرض

            # --- 💳 تنظیمات پیش‌فرض وام پویا ---
            ("min_loan_amount", 2000),
            ("max_loan_amount", 25000),
            ("min_loan_interest", 2.0),
            ("max_loan_interest", 5.0),
            ("allowed_installments", "2,3"),
            ("collateral_rate", 0.17),
            ("required_balance_rate", 0.40),
            ("late_penalty_rate", 0.0085),
        ]
        for key, val in default_settings:
            await db.execute("INSERT OR IGNORE INTO system_settings (key, val) VALUES (?, ?)", (key, val))

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

        # --- 🏛 ثبت حساب خزانه مرکزی در جدول users (در صورت عدم وجود) ---
        await db.execute(
            """
            INSERT OR IGNORE INTO users (user_id, username, full_name, balance)
            VALUES (?, ?, ?, 0)
            """,
            (TREASURY_USER_ID, "Treasury", "🏛 خزانه مرکزی آترامنتوم"),
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
            "SELECT balance, is_admin, is_frozen, username, full_name, group_name, "
            "bank_savings, frozen_balance, last_bank_claim, last_daily_profit FROM"
            " users WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            return await cursor.fetchone()


async def get_setting(key: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT val FROM system_settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0.0


async def set_setting(key: str, value):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO system_settings (key, val) VALUES (?, ?)", (key, value)
        )
        await db.commit()


# --- 🏛 توابع کمکی خزانه مرکزی (Central Treasury) ---
# تمام توابع زیر باید درون یک کانکشن دیتابیس باز (db) و ترجیحاً زیر db_lock فراخوانی شوند
# تا از race condition و منفی شدن موجودی خزانه جلوگیری شود. هیچ‌کدام commit داخلی انجام نمی‌دهند
# مگر آن‌که صراحتاً commit شوند توسط فراخواننده، تا بخشی از یک تراکنش بزرگ‌تر باقی بمانند.

async def get_treasury_balance() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT balance FROM users WHERE user_id = ?", (TREASURY_USER_ID,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def treasury_credit(db, amount: int, reason: str, related_user: int = 0):
    """واریز درآمد به خزانه مرکزی (فروشگاه، پستچی و سایر ورودی‌های سیستم)."""
    if amount <= 0:
        return
    await db.execute(
        "UPDATE users SET balance = balance + ? WHERE user_id = ?",
        (amount, TREASURY_USER_ID),
    )
    tx_id = f"TRZ-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{random.randint(1000, 9999)}"
    await db.execute(
        "INSERT INTO audit_logs (tx_id, timestamp, from_user, to_user, amount, reason) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            tx_id,
            datetime.now(timezone.utc).isoformat(),
            related_user,
            TREASURY_USER_ID,
            amount,
            reason,
        ),
    )


async def treasury_debit(db, amount: int, reason: str, related_user: int = 0) -> bool:
    """
    کسر از خزانه مرکزی (سود بانکی، اصل وام و ...).
    قانون عدم خلق پول: در صورت ناکافی بودن موجودی خزانه، هیچ تغییری اعمال نشده و False برگردانده می‌شود.
    """
    if amount <= 0:
        return True
    async with db.execute(
        "SELECT balance FROM users WHERE user_id = ?", (TREASURY_USER_ID,)
    ) as cur:
        row = await cur.fetchone()
    treasury_balance = row[0] if row else 0
    if treasury_balance < amount:
        return False
    await db.execute(
        "UPDATE users SET balance = balance - ? WHERE user_id = ?",
        (amount, TREASURY_USER_ID),
    )
    tx_id = f"TRZ-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{random.randint(1000, 9999)}"
    await db.execute(
        "INSERT INTO audit_logs (tx_id, timestamp, from_user, to_user, amount, reason) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            tx_id,
            datetime.now(timezone.utc).isoformat(),
            TREASURY_USER_ID,
            related_user,
            amount,
            reason,
        ),
    )
    return True


user_router = Router()
admin_router = Router()
shop_router = Router()

user_router.message.middleware(AntiSpamMiddleware())
shop_router.message.middleware(AntiSpamMiddleware())


# --- دستور همگانی /cancel ---
# این هندلر عمداً بلافاصله بعد از تعریف روترها و قبل از هر هندلر دیگری ثبت می‌شود
# تا در هر مرحله از هر فرآیندی (انتقال آتر، ساخت فروشگاه، ثبت محصول، ریست و ...)
# با اولویت بالاتر از هندلرهای مخصوص هر state اجرا شده و آن عملیات را لغو کند.
@user_router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        return await message.reply("ℹ️ در حال حاضر هیچ عملیات درحال‌انجامی برای لغو کردن وجود ندارد.")
    await state.clear()
    await message.reply("✅ عملیات جاری با موفقیت لغو شد. می‌توانید از ابتدا شروع کنید.")


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


# --- سیستم انتقال آتر (روش‌های درخواستی جدید) ---

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
    
    # حذف کلمات ابتدایی دستور
    if text.startswith("/transfer"):
        text = text[len("/transfer"):].strip()
    elif text.startswith("انتقال آتر"):
        text = text[len("انتقال آتر"):].strip()

    # راهنما در صورت عدم وارد کردن آرگومان
    if not text and not message.reply_to_message:
        return await message.reply(
            "📖 <b>روش‌های انتقال آتر:</b>\n\n"
            "1️⃣ <b>انتقال با آیدی عددی:</b>\n"
            "<code>انتقال آتر 123456789 500</code>\n\n"
            "2️⃣ <b>انتقال با نام کاربری:</b>\n"
            "<code>انتقال آتر @username 500</code>\n\n"
            "3️⃣ <b>انتقال با ریپلای روی پیام فرد:</b>\n"
            "ریپلای روی پیام شخص و نوشتن: <code>انتقال آتر 500</code>",
            parse_mode="HTML"
        )

    # حالت سوم: ریپلای روی پیام فرد
    if message.reply_to_message and message.reply_to_message.from_user:
        try:
            amount = int(text)
            to_user_id = message.reply_to_message.from_user.id
            return await process_transfer_request(message, state, to_user_id, amount)
        except ValueError:
            return await message.reply("❌ مبلغ وارد شده نامعتبر است. مبلغ باید یک عدد باشد.")

    # حالت اول و دوم: بر اساس آیدی عددی یا یوزرنیم
    parts = text.split()
    if len(parts) < 2:
        return await message.reply(
            "❌ فرمت دستور اشتباه است.\nمثال: <code>انتقال آتر @username 100</code> یا <code>انتقال آتر 123456 100</code>",
            parse_mode="HTML"
        )

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
            return await message.reply("❌ شماره حساب (آیدی عددی) یا نام کاربری وارد شده نامعتبر است.")

    return await process_transfer_request(message, state, to_user_id, amount)


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
    if target == TREASURY_USER_ID:
        return await message.reply(
            "🏛 حساب <code>8490505070</code> صرفاً نقش خزانه مرکزی سیستم را دارد و برای جلوگیری از "
            "دستکاری ناخواسته بودجه خزانه، از طریق دستورات مدیریتی شخصی قابل واریز نیست.",
            parse_mode="HTML",
        )

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
    if target == TREASURY_USER_ID:
        return await message.reply(
            "🏛 حساب <code>8490505070</code> صرفاً نقش خزانه مرکزی سیستم را دارد و برای جلوگیری از "
            "دستکاری ناخواسته بودجه خزانه، از طریق دستورات مدیریتی شخصی قابل کسر نیست.",
            parse_mode="HTML",
        )

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
        async with db.execute(
            "SELECT COALESCE(SUM(bank_savings), 0), COALESCE(SUM(frozen_balance), 0) FROM users"
        ) as cur2:
            bank_row = await cur2.fetchone()
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(total_amount), 0) FROM loans WHERE status = 'ACTIVE'"
        ) as cur3:
            loans_row = await cur3.fetchone()

    treasury_balance = await get_treasury_balance()
    await message.reply(
        f"👥 کل اعضا: <code>{row[0]}</code>\n"
        f"💰 حجم نقدینگی در گردش: <code>₳ {row[1] or 0}</code>\n"
        f"🏛 موجودی خزانه مرکزی: <code>₳ {treasury_balance}</code>\n"
        f"🏦 مجموع سپرده‌های بانکی: <code>₳ {bank_row[0]}</code>\n"
        f"🔒 مجموع وثیقه‌های قفل‌شده: <code>₳ {bank_row[1]}</code>\n"
        f"💳 وام‌های فعال: <code>{loans_row[0]}</code> عدد به مبلغ <code>₳ {loans_row[1]}</code>",
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
        transferable = max(0, u["balance"] - u["frozen_balance"])
        extra_treasury_line = ""
        if int(args[1]) == TREASURY_USER_ID:
            extra_treasury_line = "\n🏛 <b>این حساب، خزانه مرکزی سیستم است.</b>"
        await message.reply(
            f"🔎 <b>اطلاعات کامل کاربر <code>{args[1]}</code>:</b>\n\n"
            f"👤 نام کامل: {safe_full_name}\n"
            f"🏷 نام کاربری: @{safe_username}\n"
            f"💰 موجودی: <code>₳ {u['balance']}</code>\n"
            f"🔒 وثیقه قفل‌شده: <code>₳ {u['frozen_balance']}</code>\n"
            f"💳 موجودی قابل انتقال: <code>₳ {transferable}</code>\n"
            f"🏦 سپرده بانکی: <code>₳ {u['bank_savings']}</code>\n"
            f"👥 گروه: <b>{safe_group_name}</b>\n"
            f"⚡ وضعیت: {status}\n"
            f"🛡 دسترسی: {admin_st}"
            f"{extra_treasury_line}",
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


# --- 🛒 بخش جدید: سیستم فروشگاه، پست، و تراکنش‌های مالی جدید ---


# --- ۱. دستورات سوپرادمین برای تنظیم نرخ‌ها و مدیریت نقش‌ها ---

@admin_router.message(Command("list_shops"))
async def cmd_list_shops(message: Message):
    """دستور جدید 1: مشاهده لیست کامل فروشگاه‌ها"""
    if not is_private(message) or not await check_admin_filter(message):
        return

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT shop_id, owner_id, channel_id, channel_title, status FROM shops") as cur:
            shops = await cur.fetchall()

    if not shops:
        return await message.reply("ℹ️ هیچ فروشگاهی در دیتابیس ثبت نشده است.")

    txt = f"🏪 <b>لیست فروشگاه‌های ثبت‌شده</b> (<code>{len(shops)}</code> فروشگاه):\n\n"
    for idx, s in enumerate(shops, start=1):
        safe_title = html.escape(s['channel_title'] or 'بدون نام')
        safe_ch_id = html.escape(str(s['channel_id']))
        st_text = "✅ تایید شده" if s['status'] == "APPROVED" else "⏳ در انتظار تایید"
        
        txt += (
            f"<b>{idx}. {safe_title}</b>\n"
            f"🆔 شناسه فروشگاه: <code>{s['shop_id']}</code>\n"
            f"👤 آیدی صاحب شاپ: <code>{s['owner_id']}</code>\n"
            f"📢 کانال/گروه: <code>{safe_ch_id}</code>\n"
            f"⚡ وضعیت: {st_text}\n"
            f"------------------------------\n"
        )
        
    await message.reply(txt, parse_mode="HTML")


@admin_router.message(Command("list_couriers"))
async def cmd_list_couriers(message: Message):
    """دستور جدید 2: مشاهده لیست پستچی‌ها به همراه آیدی عددی آن‌ها"""
    if not is_private(message) or not await check_admin_filter(message):
        return

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT c.user_id, u.full_name, u.username FROM couriers c LEFT JOIN users u ON c.user_id = u.user_id"
        ) as cur:
            couriers = await cur.fetchall()

    if not couriers:
        return await message.reply("ℹ️ هیچ پستچی در سیستم ثبت نشده است.")

    txt = f"🚚 <b>لیست پستچی‌های فعال</b> (<code>{len(couriers)}</code> نفر):\n\n"
    for idx, c in enumerate(couriers, start=1):
        safe_name = html.escape(c['full_name'] or 'ناشناس')
        safe_uname = f"@{html.escape(c['username'])}" if c['username'] and c['username'] != "بدون آیدی" else "بدون یوزرنیم"
        
        txt += (
            f"<b>{idx}. {safe_name}</b> ({safe_uname})\n"
            f"🆔 آیدی عددی: <code>{c['user_id']}</code>\n"
            f"------------------------------\n"
        )

    await message.reply(txt, parse_mode="HTML")


@admin_router.message(Command("set_shop_rates"))
async def cmd_set_shop_rates(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 4:
        return await message.reply("راهنما: <code>/set_shop_rates [فروشنده] [بانک] [سوخت]</code>\nمثال: <code>/set_shop_rates 51 40 9</code>", parse_mode="HTML")
    try:
        s, b, f = float(args[1]), float(args[2]), float(args[3])
        if abs((s + b + f) - 100.0) > 0.01:
            return await message.reply("❌ مجموع درصدها باید برابر با ۱۰۰ باشد.")
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR REPLACE INTO system_settings (key, val) VALUES ('shop_seller_pct', ?)", (s,))
            await db.execute("INSERT OR REPLACE INTO system_settings (key, val) VALUES ('shop_bank_pct', ?)", (b,))
            await db.execute("INSERT OR REPLACE INTO system_settings (key, val) VALUES ('shop_burn_pct', ?)", (f,))
            await db.commit()
        await message.reply(f"✅ درصدهای فروشگاه با موفقیت تنظیم شد:\nفروشنده: {s}%\nبانک: {b}%\nسوخت: {f}%")
    except ValueError:
        await message.reply("❌ مقادیر وارد شده نامعتبر است.")


@admin_router.message(Command("set_courier_rates"))
async def cmd_set_courier_rates(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 7:
        return await message.reply(
            "راهنما:\n<code>/set_courier_rates [پستچی] [بانک] [سوخت] [بازه۱] [بازه۲] [بازه۳]</code>\n"
            "مثال:\n<code>/set_courier_rates 61 30 9 8 10 12</code>", parse_mode="HTML"
        )
    try:
        c, b, f = float(args[1]), float(args[2]), float(args[3])
        t1, t2, t3 = float(args[4]), float(args[5]), float(args[6])
        if abs((c + b + f) - 100.0) > 0.01:
            return await message.reply("❌ مجموع درصدهای تقسیم پستچی باید ۱۰۰ باشد.")
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR REPLACE INTO system_settings (key, val) VALUES ('courier_pct', ?)", (c,))
            await db.execute("INSERT OR REPLACE INTO system_settings (key, val) VALUES ('courier_bank_pct', ?)", (b,))
            await db.execute("INSERT OR REPLACE INTO system_settings (key, val) VALUES ('courier_burn_pct', ?)", (f,))
            await db.execute("INSERT OR REPLACE INTO system_settings (key, val) VALUES ('tier1_pct', ?)", (t1,))
            await db.execute("INSERT OR REPLACE INTO system_settings (key, val) VALUES ('tier2_pct', ?)", (t2,))
            await db.execute("INSERT OR REPLACE INTO system_settings (key, val) VALUES ('tier3_pct', ?)", (t3,))
            await db.commit()
        await message.reply(
            f"✅ درصدهای پست با موفقیت تنظیم شد:\n"
            f"تقسیم: پستچی {c}% | بانک {b}% | سوخت {f}%\n"
            f"بازه‌های هزینه: تا ۹۹ آتر ({t1}%) | ۱۰۰ تا ۹۹۹ ({t2}%) | ۱۰۰۰ به بالا ({t3}%)"
        )
    except ValueError:
        await message.reply("❌ مقادیر وارد شده نامعتبر است.")


# --- 🏦 دستورات سوپرادمین برای تنظیم بانک آترامنتوم و وام پویا ---

@admin_router.message(Command("set_bank_rate"))
async def cmd_set_bank_rate(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.reply(
            "راهنما: <code>/set_bank_rate [درصد]</code>\nمثال: <code>/set_bank_rate 1.23</code>",
            parse_mode="HTML"
        )
    try:
        rate = float(args[1])
        if rate < 0:
            raise ValueError
    except ValueError:
        return await message.reply("❌ مقدار نامعتبر است.")
    await set_setting("bank_daily_rate", rate)
    await message.reply(f"✅ نرخ سود روزانه بانک آترامنتوم به <b>{rate}٪</b> تغییر یافت.", parse_mode="HTML")


@admin_router.message(Command("set_min_loan"))
async def cmd_set_min_loan(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("راهنما: <code>/set_min_loan [مبلغ]</code>", parse_mode="HTML")
    try:
        amount = int(args[1])
        if amount <= 0:
            raise ValueError
    except ValueError:
        return await message.reply("❌ مقدار نامعتبر است.")
    await set_setting("min_loan_amount", amount)
    await message.reply(f"✅ حداقل مبلغ وام به <code>₳ {amount}</code> تغییر یافت.", parse_mode="HTML")


@admin_router.message(Command("set_max_loan"))
async def cmd_set_max_loan(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("راهنما: <code>/set_max_loan [مبلغ]</code>", parse_mode="HTML")
    try:
        amount = int(args[1])
        if amount <= 0:
            raise ValueError
    except ValueError:
        return await message.reply("❌ مقدار نامعتبر است.")
    await set_setting("max_loan_amount", amount)
    await message.reply(f"✅ حداکثر مبلغ وام به <code>₳ {amount}</code> تغییر یافت.", parse_mode="HTML")


@admin_router.message(Command("set_loan_interest"))
async def cmd_set_loan_interest(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 3:
        return await message.reply(
            "راهنما: <code>/set_loan_interest [حداقل] [حداکثر]</code>\nمثال: <code>/set_loan_interest 2 5</code>",
            parse_mode="HTML"
        )
    try:
        mn, mx = float(args[1]), float(args[2])
        if mn < 0 or mx < mn:
            raise ValueError
    except ValueError:
        return await message.reply("❌ مقادیر نامعتبر است.")
    await set_setting("min_loan_interest", mn)
    await set_setting("max_loan_interest", mx)
    await message.reply(f"✅ بازه سود وام به <b>{mn}٪ تا {mx}٪</b> تغییر یافت.", parse_mode="HTML")


@admin_router.message(Command("set_loan_installments"))
async def cmd_set_loan_installments(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply(
            "راهنما: <code>/set_loan_installments [لیست تعداد اقساط با کاما]</code>\n"
            "مثال: <code>/set_loan_installments 2,3,4</code>",
            parse_mode="HTML"
        )
    raw = args[1].strip().replace(" ", "")
    parts = raw.split(",")
    try:
        nums = [int(p) for p in parts if p]
        if not nums or any(n <= 0 for n in nums):
            raise ValueError
    except ValueError:
        return await message.reply("❌ فرمت نامعتبر است. مثال درست: <code>2,3,4</code>", parse_mode="HTML")
    await set_setting("allowed_installments", ",".join(str(n) for n in nums))
    await message.reply(f"✅ تعداد اقساط مجاز به <b>{raw}</b> تغییر یافت.", parse_mode="HTML")


@admin_router.message(Command("set_collateral_rate"))
async def cmd_set_collateral_rate(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.reply(
            "راهنما: <code>/set_collateral_rate [نسبت اعشاری]</code>\nمثال (۱۷٪): <code>/set_collateral_rate 0.17</code>",
            parse_mode="HTML"
        )
    try:
        rate = float(args[1])
        if not (0 < rate <= 1):
            raise ValueError
    except ValueError:
        return await message.reply("❌ مقدار باید بین 0 و 1 باشد (مثال: 0.17).")
    await set_setting("collateral_rate", rate)
    await message.reply(f"✅ نرخ وثیقه وام به <b>{rate * 100:.1f}٪</b> تغییر یافت.", parse_mode="HTML")


@admin_router.message(Command("set_req_balance_rate"))
async def cmd_set_req_balance_rate(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.reply(
            "راهنما: <code>/set_req_balance_rate [نسبت اعشاری]</code>\nمثال (۴۰٪): <code>/set_req_balance_rate 0.40</code>",
            parse_mode="HTML"
        )
    try:
        rate = float(args[1])
        if not (0 < rate <= 1):
            raise ValueError
    except ValueError:
        return await message.reply("❌ مقدار باید بین 0 و 1 باشد (مثال: 0.40).")
    await set_setting("required_balance_rate", rate)
    await message.reply(f"✅ نرخ موجودی اولیه لازم برای وام وثیقه‌ای به <b>{rate * 100:.1f}٪</b> تغییر یافت.", parse_mode="HTML")


@admin_router.message(Command("set_late_penalty_rate"))
async def cmd_set_late_penalty_rate(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.reply(
            "راهنما: <code>/set_late_penalty_rate [نسبت اعشاری روزانه]</code>\nمثال (۰.۸۵٪): <code>/set_late_penalty_rate 0.0085</code>",
            parse_mode="HTML"
        )
    try:
        rate = float(args[1])
        if rate < 0:
            raise ValueError
    except ValueError:
        return await message.reply("❌ مقدار نامعتبر است.")
    await set_setting("late_penalty_rate", rate)
    await message.reply(f"✅ نرخ جریمه دیرکرد روزانه به <b>{rate * 100:.2f}٪</b> تغییر یافت.", parse_mode="HTML")


@admin_router.message(Command("shop_requests"))
async def cmd_shop_requests(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM shops WHERE status = 'PENDING'") as cur:
            requests = await cur.fetchall()

    if not requests:
        return await message.reply("ℹ️ هیچ درخواست ثبت فروشگاهی وجود ندارد.")

    for req in requests:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ تایید فروشگاه", callback_data=f"approve_shop_{req['shop_id']}"),
            InlineKeyboardButton(text="❌ رد درخواست", callback_data=f"reject_shop_{req['shop_id']}")
        ]])
        safe_title = html.escape(req['channel_title'] or 'بدون نام')
        safe_ch = html.escape(str(req['channel_id']))
        await message.reply(
            f"🏪 <b>درخواست ساخت فروشگاه</b>\n"
            f"👤 مالکان: <code>{req['owner_id']}</code>\n"
            f"📢 کانال/گروه: <b>{safe_title}</b> (<code>{safe_ch}</code>)",
            reply_markup=kb,
            parse_mode="HTML"
        )


@admin_router.callback_query(F.data.startswith("approve_shop_"))
async def cb_approve_shop(callback: CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        return await callback.answer("عدم دسترسی", show_alert=True)
    shop_id = int(callback.data.split("_")[2])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE shops SET status = 'APPROVED' WHERE shop_id = ?", (shop_id,))
        async with db.execute("SELECT owner_id, channel_title FROM shops WHERE shop_id = ?", (shop_id,)) as cur:
            shop = await cur.fetchone()
        await db.commit()

    await callback.message.edit_text("✅ فروشگاه تایید شد.")
    if shop:
        try:
            safe_title = html.escape(shop[1] or '')
            await callback.bot.send_message(
                shop[0],
                f"🎉 درخواست ثبت فروشگاه شما برای کانال/گروه <b>{safe_title}</b> با موفقیت تایید شد!",
                parse_mode="HTML"
            )
        except Exception:
            pass


@admin_router.callback_query(F.data.startswith("reject_shop_"))
async def cb_reject_shop(callback: CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        return await callback.answer("عدم دسترسی", show_alert=True)
    shop_id = int(callback.data.split("_")[2])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT owner_id FROM shops WHERE shop_id = ?", (shop_id,)) as cur:
            shop = await cur.fetchone()
        await db.execute("DELETE FROM shops WHERE shop_id = ?", (shop_id,))
        await db.commit()

    await callback.message.edit_text("❌ درخواست فروشگاه رد شد.")
    if shop:
        try:
            await callback.bot.send_message(shop[0], "❌ متأسفانه درخواست ثبت فروشگاه شما رد شد.")
        except Exception:
            pass


@admin_router.message(Command("remove_shop"))
async def cmd_remove_shop(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("راهنما: <code>/remove_shop [شناسه_فروشگاه]</code>", parse_mode="HTML")
    shop_id = int(args[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM shops WHERE shop_id = ?", (shop_id,))
        await db.execute("DELETE FROM products WHERE shop_id = ?", (shop_id,))
        await db.commit()
    await message.reply(f"🗑 فروشگاه شماره {shop_id} و محصولات آن حذف شدند.")


@admin_router.message(Command("add_courier"))
async def cmd_add_courier(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("راهنما: <code>/add_courier [آیدی_عددی_کاربر]</code>", parse_mode="HTML")
    courier_id = int(args[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO couriers (user_id) VALUES (?)", (courier_id,))
        await db.commit()
    await message.reply(f"🚚 کاربر <code>{courier_id}</code> به لیست پستچی‌های مجاز اضافه شد.", parse_mode="HTML")


@admin_router.message(Command("remove_courier"))
async def cmd_remove_courier(message: Message):
    if not is_private(message) or not is_super_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("راهنما: <code>/remove_courier [آیدی_عددی_کاربر]</code>", parse_mode="HTML")
    courier_id = int(args[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM couriers WHERE user_id = ?", (courier_id,))
        await db.commit()
    await message.reply(f"🔥 کاربر <code>{courier_id}</code> از لیست پستچی‌ها حذف شد.", parse_mode="HTML")


# --- ۲. دستورات فروشندگان (Shop Owners) ---

@shop_router.message(Command("request_shop"))
async def cmd_request_shop(message: Message, state: FSMContext):
    if not is_private(message):
        return
    await message.reply("لطفاً آیدی عددی یا یوزرنیم کانال/گروه خود را ارسال کنید (مثال: @mychannel یا -100123456789):")
    await state.set_state(RequestShopForm.waiting_for_channel)


@shop_router.message(RequestShopForm.waiting_for_channel)
async def process_request_shop_channel(message: Message, state: FSMContext):
    channel_raw = message.text.strip()
    try:
        chat = await message.bot.get_chat(channel_raw)
        bot_member = await message.bot.get_chat_member(chat.id, message.bot.id)
        if bot_member.status not in ["administrator", "creator"]:
            return await message.reply("⚠️ ربات در این کانال/گروه ادمین نیست! ابتدا ربات را ادمین کنید و مجدداً تلاش کنید.")
    except Exception as e:
        return await message.reply(f"❌ یافتن کانال/گروه با خطا مواجه شد. از ادمین بودن ربات و صحت آیدی مطمئن شوید.\nخطا: {e}")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO shops (owner_id, channel_id, channel_title, status) VALUES (?, ?, ?, 'PENDING')",
            (message.from_user.id, str(chat.id), chat.title)
        )
        await db.commit()

    await state.clear()
    await message.reply("✅ درخواست ثبت فروشگاه ارسال شد و پس از بررسی توسط سوپرادمین تایید خواهد شد.")


@shop_router.message(Command("add_product"))
async def cmd_add_product(message: Message, state: FSMContext):
    if not is_private(message):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM shops WHERE owner_id = ? AND status = 'APPROVED'", (message.from_user.id,)) as cur:
            shops = await cur.fetchall()

    if not shops:
        return await message.reply("❌ شما هیچ فروشگاه تاییدشده‌ای ندارید.")

    await state.update_data(shop_id=shops[0]["shop_id"], channel_id=shops[0]["channel_id"])
    await message.reply("📸 لطفاً عکس محصول را ارسال کنید:")
    await state.set_state(AddProductForm.waiting_for_photo)


@shop_router.message(AddProductForm.waiting_for_photo, F.photo)
async def process_product_photo(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    await state.update_data(photo_id=photo_id)
    await message.reply("🏷 نام محصول را وارد کنید:")
    await state.set_state(AddProductForm.waiting_for_title)


@shop_router.message(AddProductForm.waiting_for_title)
async def process_product_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await message.reply("📝 توضیحات محصول را وارد کنید:")
    await state.set_state(AddProductForm.waiting_for_description)


@shop_router.message(AddProductForm.waiting_for_description)
async def process_product_desc(message: Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await message.reply("💰 قیمت محصول (به آتر) را وارد کنید:")
    await state.set_state(AddProductForm.waiting_for_price)


@shop_router.message(AddProductForm.waiting_for_price)
async def process_product_price(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        return await message.reply("❌ قیمت باید یک عدد مثبت باشد.")
    await state.update_data(price=int(message.text))

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="تکی (۱ عدد)", callback_data="st_SINGLE"),
        InlineKeyboardButton(text="محدود", callback_data="st_LIMITED"),
        InlineKeyboardButton(text="نامحدود", callback_data="st_UNLIMITED"),
    ]])
    await message.reply("📦 نوع موجودی محصول را انتخاب کنید:", reply_markup=kb)
    await state.set_state(AddProductForm.waiting_for_stock_type)


@shop_router.callback_query(AddProductForm.waiting_for_stock_type, F.data.startswith("st_"))
async def process_product_stock_type(callback: CallbackQuery, state: FSMContext):
    st_type = callback.data.split("_")[1]
    await state.update_data(stock_type=st_type)

    if st_type == "SINGLE":
        await state.update_data(stock_qty=1)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="بله (نیازمند پستچی)", callback_data="cour_YES"),
            InlineKeyboardButton(text="خیر (دیجیتالی/مستقیم)", callback_data="cour_NO"),
        ]])
        await callback.message.edit_text("🚚 آیا این محصول نیاز به پستچی دارد؟", reply_markup=kb)
        await state.set_state(AddProductForm.waiting_for_needs_courier)
    elif st_type == "UNLIMITED":
        await state.update_data(stock_qty=-1)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="بله (نیازمند پستچی)", callback_data="cour_YES"),
            InlineKeyboardButton(text="خیر (دیجیتالی/مستقیم)", callback_data="cour_NO"),
        ]])
        await callback.message.edit_text("🚚 آیا این محصول نیاز به پستچی دارد؟", reply_markup=kb)
        await state.set_state(AddProductForm.waiting_for_needs_courier)
    else:  # LIMITED
        await callback.message.edit_text("تعداد موجودی را به عدد وارد کنید:")
        await state.set_state(AddProductForm.waiting_for_stock)


@shop_router.message(AddProductForm.waiting_for_stock)
async def process_product_stock_qty(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        return await message.reply("❌ تعداد موجودی باید عدد مثبت باشد.")
    await state.update_data(stock_qty=int(message.text))

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="بله (نیازمند پستچی)", callback_data="cour_YES"),
        InlineKeyboardButton(text="خیر (دیجیتالی/مستقیم)", callback_data="cour_NO"),
    ]])
    await message.reply("🚚 آیا این محصول نیاز به پستچی دارد؟", reply_markup=kb)
    await state.set_state(AddProductForm.waiting_for_needs_courier)


@shop_router.callback_query(AddProductForm.waiting_for_needs_courier, F.data.startswith("cour_"))
async def process_product_final(callback: CallbackQuery, state: FSMContext):
    needs_courier = (callback.data.split("_")[1] == "YES")
    data = await state.get_data()
    await state.clear()

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO products (shop_id, photo_id, title, description, price, stock_type, stock_qty, needs_courier)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (data["shop_id"], data["photo_id"], data["title"], data["description"], data["price"], data["stock_type"], data["stock_qty"], needs_courier)
        )
        product_id = cursor.lastrowid
        await db.commit()

    # ارسال بنر محصول به کانال
    kb_buy = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛍 خرید این محصول", callback_data=f"buy_prod_{product_id}")
    ]])
    
    stock_str = "موجود" if data["stock_type"] == "UNLIMITED" else f"{data['stock_qty']} عدد"
    safe_title = html.escape(data['title'])
    safe_desc = html.escape(data['description'])
    caption = (
        f"🛍 <b>{safe_title}</b>\n\n"
        f"📝 {safe_desc}\n\n"
        f"💰 قیمت: <code>₳ {data['price']}</code>\n"
        f"📦 موجودی: <b>{stock_str}</b>"
    )

    try:
        sent_msg = await callback.bot.send_photo(
            chat_id=data["channel_id"],
            photo=data["photo_id"],
            caption=caption,
            reply_markup=kb_buy,
            parse_mode="HTML"
        )
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE products SET channel_msg_id = ? WHERE product_id = ?", (sent_msg.message_id, product_id))
            await db.commit()
    except Exception as e:
        await callback.message.edit_text(f"⚠️ محصول ثبت شد اما بنر در کانال ارسال نشد. مطمئن شوید ربات ادمین کانال است.\nخطا: {e}")
        return

    await callback.message.edit_text("🎉 محصول با موفقیت ثبت شد و بنر خرید در کانال قرار گرفت.")


@shop_router.message(Command("inventory"))
async def cmd_inventory(message: Message):
    if not is_private(message):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT p.* FROM products p JOIN shops s ON p.shop_id = s.shop_id WHERE s.owner_id = ?",
            (message.from_user.id,)
        ) as cur:
            products = await cur.fetchall()

    if not products:
        return await message.reply("📦 شما هیچ محصولی ثبت نکرده‌اید.")

    txt = "📦 <b>مدیریت انبار و موجودی:</b>\n\n"
    for p in products:
        st_str = "نامحدود" if p["stock_type"] == "UNLIMITED" else f"{p['stock_qty']} عدد"
        safe_title = html.escape(p['title'])
        txt += f"🔹 کد: <code>{p['product_id']}</code> | <b>{safe_title}</b> | قیمت: <code>₳ {p['price']}</code> | موجودی: <b>{st_str}</b>\n"
    await message.reply(txt, parse_mode="HTML")


@shop_router.message(Command("my_shop"))
async def cmd_my_shop(message: Message):
    if not is_private(message):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM shops WHERE owner_id = ?", (message.from_user.id,)) as cur:
            shops = await cur.fetchall()

        if not shops:
            return await message.reply("❌ شما هیچ فروشگاهی ندارید.")

        shop = shops[0]
        async with db.execute(
            "SELECT COUNT(*) as cnt, SUM(price) as total FROM orders WHERE shop_id = ?",
            (shop["shop_id"],)
        ) as cur_o:
            stats = await cur_o.fetchone()

    safe_title = html.escape(shop['channel_title'] or '')
    await message.reply(
        f"🏪 <b>گزارش فروشگاه {safe_title}</b>\n\n"
        f"📊 تعداد کل فروش: <code>{stats['cnt']}</code> عدد\n"
        f"💰 مجموع ارزش سفارشات: <code>₳ {stats['total'] or 0}</code>\n"
        f"⚡ وضعیت فروشگاه: <b>{shop['status']}</b>",
        parse_mode="HTML"
    )


# --- ۳. خرید محصول و محاسبات دقیق مالی آتر (با تاییدیه دو مرحله‌ای) ---

@shop_router.callback_query(F.data.startswith("buy_prod_"))
async def cb_initiate_buy(callback: CallbackQuery):
    """مرحله اول: بررسی اولیه و ارسال پیام تاییدیه به پیوی خریدار (بدون کسر هیچ مبلغی)."""
    buyer_id = callback.from_user.id
    product_id = int(callback.data.split("_")[2])

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM products WHERE product_id = ?", (product_id,)) as cur:
            prod = await cur.fetchone()

        if not prod:
            return await callback.answer("❌ محصول پیدا نشد.", show_alert=True)

        if prod["stock_type"] != "UNLIMITED" and prod["stock_qty"] <= 0:
            return await callback.answer("❌ موجودی این محصول به اتمام رسیده است.", show_alert=True)

        if prod["stock_type"] != "UNLIMITED":
            async with db.execute(
                "SELECT 1 FROM orders WHERE buyer_id = ? AND product_id = ? LIMIT 1",
                (buyer_id, product_id)
            ) as cur_chk:
                already_bought = await cur_chk.fetchone()
            if already_bought:
                return await callback.answer(
                    "❌ این محصول محدود است و شما قبلاً یک عدد از آن را خریداری کرده‌اید.",
                    show_alert=True
                )

        async with db.execute("SELECT balance, is_frozen FROM users WHERE user_id = ?", (buyer_id,)) as cur_u:
            buyer = await cur_u.fetchone()

    if not buyer or buyer["is_frozen"]:
        return await callback.answer("❌ حساب شما مسدود یا غیرفعال است.", show_alert=True)

    price = prod["price"]
    courier_fee = 0
    if prod["needs_courier"]:
        t1 = await get_setting("tier1_pct")
        t2 = await get_setting("tier2_pct")
        t3 = await get_setting("tier3_pct")
        if price <= 99:
            courier_fee = int(price * (t1 / 100.0))
        elif price <= 999:
            courier_fee = int(price * (t2 / 100.0))
        else:
            courier_fee = int(price * (t3 / 100.0))
    total_cost = price + courier_fee

    if buyer["balance"] < total_cost:
        return await callback.answer(f"❌ موجودی ناکافی! قیمت محصول: ₳ {price} + هزینه پست: ₳ {courier_fee} = مجموع: ₳ {total_cost}", show_alert=True)

    safe_title = html.escape(prod['title'])
    if prod["needs_courier"]:
        cost_line = f"💰 قیمت محصول: <code>₳ {price}</code>\n🚚 هزینه پست: <code>₳ {courier_fee}</code>\n💳 مجموع پرداختی: <code>₳ {total_cost}</code>\n"
    else:
        cost_line = f"💰 مبلغ پرداختی: <code>₳ {price}</code>\n"

    confirm_txt = (
        f"🛍 <b>تاییدیه خرید</b>\n\n"
        f"📦 محصول: <b>{safe_title}</b>\n"
        f"{cost_line}\n"
        f"❓ آیا از خرید این محصول مطمئن هستید؟ در صورت تایید، مبلغ فوراً از حساب شما کسر می‌شود."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ تایید و پرداخت", callback_data=f"confirm_buy_{product_id}"),
        InlineKeyboardButton(text="❌ انصراف", callback_data=f"cancel_buy_{product_id}"),
    ]])

    try:
        await callback.bot.send_message(buyer_id, confirm_txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        return await callback.answer(
            "⚠️ ابتدا ربات را در پیوی (چت خصوصی) استارت کنید، سپس دوباره روی خرید بزنید.",
            show_alert=True
        )

    await callback.answer("📩 جهت تایید نهایی خرید، به پیوی ربات مراجعه کنید.", show_alert=True)


@shop_router.callback_query(F.data.startswith("confirm_buy_"))
async def cb_confirm_buy(callback: CallbackQuery):
    """مرحله دوم: تایید نهایی خریدار در پیوی ربات و اجرای واقعی تراکنش خرید."""
    buyer_id = callback.from_user.id
    product_id = int(callback.data.split("_")[2])

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM products WHERE product_id = ?", (product_id,)) as cur:
                prod = await cur.fetchone()

            if not prod:
                await callback.answer("❌ محصول پیدا نشد یا حذف شده است.", show_alert=True)
                try:
                    await callback.message.edit_text("❌ این محصول دیگر در دسترس نیست.")
                except Exception:
                    pass
                return

            if prod["stock_type"] != "UNLIMITED" and prod["stock_qty"] <= 0:
                await callback.answer("❌ موجودی این محصول به اتمام رسیده است.", show_alert=True)
                try:
                    await callback.message.edit_text("❌ متاسفانه موجودی این محصول قبل از تایید شما به اتمام رسید.")
                except Exception:
                    pass
                return

            # جلوگیری از خرید بیش از یک عدد توسط یک کاربر، فقط برای محصولات محدود/تکی
            # (محصولات نامحدود هیچ محدودیتی در تعداد خرید ندارند)
            if prod["stock_type"] != "UNLIMITED":
                async with db.execute(
                    "SELECT 1 FROM orders WHERE buyer_id = ? AND product_id = ? LIMIT 1",
                    (buyer_id, product_id)
                ) as cur_chk:
                    already_bought = await cur_chk.fetchone()
                if already_bought:
                    await callback.answer(
                        "❌ این محصول محدود است و شما قبلاً یک عدد از آن را خریداری کرده‌اید.",
                        show_alert=True
                    )
                    try:
                        await callback.message.edit_text("❌ شما قبلاً این محصول محدود را خریداری کرده‌اید.")
                    except Exception:
                        pass
                    return

            async with db.execute("SELECT balance, is_frozen FROM users WHERE user_id = ?", (buyer_id,)) as cur_u:
                buyer = await cur_u.fetchone()

            if not buyer or buyer["is_frozen"]:
                return await callback.answer("❌ حساب شما مسدود یا غیرفعال است.", show_alert=True)

            price = prod["price"]

            # محاسبه هزینه پست در صورت نیاز به پستچی
            courier_fee = 0
            if prod["needs_courier"]:
                t1 = await get_setting("tier1_pct")
                t2 = await get_setting("tier2_pct")
                t3 = await get_setting("tier3_pct")
                if price <= 99:
                    courier_fee = int(price * (t1 / 100.0))
                elif price <= 999:
                    courier_fee = int(price * (t2 / 100.0))
                else:
                    courier_fee = int(price * (t3 / 100.0))

            total_cost = price + courier_fee

            if buyer["balance"] < total_cost:
                await callback.answer(f"❌ موجودی ناکافی! قیمت محصول: ₳ {price} + هزینه پست: ₳ {courier_fee} = مجموع: ₳ {total_cost}", show_alert=True)
                try:
                    await callback.message.edit_text("❌ موجودی حساب شما برای این خرید کافی نیست.")
                except Exception:
                    pass
                return

            # کسر مبلغ از خریدار
            await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (total_cost, buyer_id))

            # 🏛 هزینه پست دریافتی از خریدار به‌عنوان درآمد سیستم به خزانه مرکزی واریز می‌شود
            # (سهم پستچی بعداً هنگام تایید تحویل، از همین خزانه به او پرداخت خواهد شد)
            if courier_fee > 0:
                await treasury_credit(db, courier_fee, f"هزینه پست دریافتی سفارش محصول #{product_id}", related_user=buyer_id)

            # تقسیم کالا: ۵۱٪ فروشنده، ۴۰٪ بانک، ۹٪ سوخت
            async with db.execute("SELECT owner_id FROM shops WHERE shop_id = ?", (prod["shop_id"],)) as cur_s:
                shop_owner_id = (await cur_s.fetchone())["owner_id"]

            s_pct = await get_setting("shop_seller_pct")
            b_pct = await get_setting("shop_bank_pct")

            seller_share = int(price * (s_pct / 100.0))
            bank_share = int(price * (b_pct / 100.0))
            # باقی مانده درصد سوخت (امحا) می‌شود و به حسابی واریز نمی‌شود.

            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (seller_share, shop_owner_id))
            # 🏛 سهم بانک از فروش، به‌عنوان درآمد سیستم مستقیماً به خزانه مرکزی واریز می‌شود
            await treasury_credit(db, bank_share, f"سهم بانک از فروش محصول #{product_id}", related_user=shop_owner_id)

            # کسر از موجودی انبار
            new_qty = prod["stock_qty"]
            if prod["stock_type"] != "UNLIMITED":
                new_qty -= 1
                await db.execute("UPDATE products SET stock_qty = ? WHERE product_id = ?", (new_qty, product_id))

            # تولید کد ۱۰ رقمی امنیتی
            code_10 = "".join(random.choices(string.digits, k=10))

            # ثبت سفارش (به همراه اسنپ‌شات محصول تا در صورت حذف فروشگاه/محصول، اطلاعات خرید باقی بماند)
            await db.execute(
                """INSERT INTO orders
                   (code_10, buyer_id, shop_id, product_id, price, courier_fee, status, product_title, product_desc, product_photo_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (code_10, buyer_id, prod["shop_id"], product_id, price, courier_fee,
                 "DISPATCHED" if prod["needs_courier"] else "DELIVERED",
                 prod["title"], prod["description"], prod["photo_id"])
            )
            await db.commit()

            # به روزرسانی بنر در کانال
            try:
                async with db.execute("SELECT channel_id FROM shops WHERE shop_id = ?", (prod["shop_id"],)) as cur_ch:
                    channel_id = (await cur_ch.fetchone())["channel_id"]

                stock_str = "موجود" if prod["stock_type"] == "UNLIMITED" else f"{new_qty} عدد"
                safe_title = html.escape(prod['title'])
                safe_desc = html.escape(prod['description'])
                caption = (
                    f"🛍 <b>{safe_title}</b>\n\n"
                    f"📝 {safe_desc}\n\n"
                    f"💰 قیمت: <code>₳ {prod['price']}</code>\n"
                    f"📦 موجودی: <b>{stock_str}</b>"
                )
                kb_buy = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛍 خرید این محصول", callback_data=f"buy_prod_{product_id}")]])
                await callback.bot.edit_message_caption(chat_id=channel_id, message_id=prod["channel_msg_id"], caption=caption, reply_markup=kb_buy, parse_mode="HTML")
            except Exception:
                pass

    await callback.answer("🎉 خرید با موفقیت انجام شد!", show_alert=True)

    # ویرایش پیام تاییدیه در پیوی خریدار با جزئیات نهایی خرید
    if prod["needs_courier"]:
        cost_line = f"💰 قیمت محصول: <code>₳ {price}</code>\n🚚 هزینه پست: <code>₳ {courier_fee}</code>\n💳 مجموع پرداختی: <code>₳ {total_cost}</code>\n"
    else:
        cost_line = f"💰 مبلغ پرداختی: <code>₳ {price}</code>\n"
    msg_buyer = (
        f"🎉 خرید شما نهایی شد!\n"
        f"🛍 محصول: <b>{html.escape(prod['title'])}</b>\n"
        f"{cost_line}"
        f"🔐 کد امنیتی ۱۰ رقمی شما: <code>{code_10}</code>"
    )
    try:
        await callback.message.edit_text(msg_buyer, parse_mode="HTML")
    except Exception:
        try:
            await callback.bot.send_message(buyer_id, msg_buyer, parse_mode="HTML")
        except Exception:
            pass

    msg_seller = f"🛍 سفارش جدید ثبت شد!\nمحصول: <b>{html.escape(prod['title'])}</b>\n🔐 کد امنیتی ۱۰ رقمی: <code>{code_10}</code>"
    try:
        await callback.bot.send_message(shop_owner_id, msg_seller, parse_mode="HTML")
    except Exception:
        pass

    if prod["needs_courier"]:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT user_id FROM couriers") as cur_c:
                couriers = await cur_c.fetchall()

        msg_courier = (
            f"🚚 <b>سفارش جدید آماده ارسال!</b>\n"
            f"📦 محصول: <b>{html.escape(prod['title'])}</b>\n"
            f"💰 کرایه پست: <code>₳ {courier_fee}</code>\n"
            f"🔐 کد ۱۰ رقمی تحویل: <code>{code_10}</code>\n"
            f"جهت تایید تحویل، دستور زیر را بزنید:\n"
            f"<code>/confirm_dispatch {code_10}</code>"
        )
        for c in couriers:
            try:
                await callback.bot.send_message(c[0], msg_courier, parse_mode="HTML")
            except Exception:
                pass


@shop_router.callback_query(F.data.startswith("cancel_buy_"))
async def cb_cancel_buy(callback: CallbackQuery):
    """انصراف خریدار از خرید در مرحله تاییدیه؛ هیچ مبلغی کسر نشده بود."""
    await callback.answer("❌ خرید لغو شد.")
    try:
        await callback.message.edit_text("❌ خرید توسط شما لغو شد. هیچ مبلغی از حساب شما کسر نشد.")
    except Exception:
        pass


# --- ۴. دستورات پستچی‌ها (Couriers) ---

@shop_router.message(Command("courier_orders"))
async def cmd_courier_orders(message: Message):
    if not is_private(message):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM couriers WHERE user_id = ?", (message.from_user.id,)) as cur:
            if not await cur.fetchone() and not is_super_admin(message.from_user.id):
                return await message.reply("❌ شما دسترسی پستچی ندارید.")

        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM orders WHERE status = 'DISPATCHED'"
        ) as cur_o:
            orders = await cur_o.fetchall()

    if not orders:
        return await message.reply("📦 هیچ سفارشی منتظر ارسال نیست.")

    txt = "🚚 <b>سفارش‌های آماده ارسال:</b>\n\n"
    for o in orders:
        safe_title = html.escape(o['product_title'] or 'محصول حذف‌شده')
        txt += f"📦 سفارش: <b>{safe_title}</b> | هزینه پست: <code>₳ {o['courier_fee']}</code> | کد: <code>{o['code_10']}</code>\n"
    await message.reply(txt, parse_mode="HTML")


@shop_router.message(Command("confirm_dispatch"))
async def cmd_confirm_dispatch(message: Message):
    if not is_private(message):
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("راهنما: <code>/confirm_dispatch [کد_۱۰_رقمی]</code>", parse_mode="HTML")

    code_10 = args[1].strip()
    courier_id = message.from_user.id

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT user_id FROM couriers WHERE user_id = ?", (courier_id,)) as cur:
                if not await cur.fetchone() and not is_super_admin(courier_id):
                    return await message.reply("❌ شما در لیست پستچی‌های مجاز قرار ندارید.")

            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM orders WHERE code_10 = ?", (code_10,)) as cur_o:
                order = await cur_o.fetchone()

            if not order:
                return await message.reply("❌ کدهای وارد شده نامعتبر است.")

            if order["status"] == "DELIVERED":
                return await message.reply("⚠️ این سفارش قبلاً تحویل داده شده است.")

            c_pct = await get_setting("courier_pct")
            courier_share = int(order["courier_fee"] * (c_pct / 100.0))

            # 🏛 پرداخت سهم پستچی از خزانه مرکزی (طبق قانون عدم خلق پول)
            paid = await treasury_debit(
                db, courier_share, f"پرداخت سهم پستچی سفارش {order['code_10']}", related_user=courier_id
            )
            if not paid:
                await db.rollback()
                return await message.reply(
                    "❌ عدم امکان پرداخت به دلیل عدم کفایت موجودی خزانه. لطفاً بعداً دوباره تلاش کنید یا با سوپرادمین تماس بگیرید."
                )

            # واریز سهم خالص به پستچی و تغییر وضعیت سفارش
            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (courier_share, courier_id))
            await db.execute("UPDATE orders SET status = 'DELIVERED', courier_id = ? WHERE order_id = ?", (courier_id, order["order_id"]))
            await db.commit()

    await message.reply(f"✅ تحویل سفارش با موفقیت ثبت شد و مبلغ <code>₳ {courier_share}</code> به حساب شما واریز گردید.", parse_mode="HTML")


# --- ۵. خریداران و عمومی ---

@shop_router.message(Command("my_orders"))
async def cmd_my_orders(message: Message):
    if not is_private(message):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM orders WHERE buyer_id = ?",
            (message.from_user.id,)
        ) as cur:
            orders = await cur.fetchall()

    if not orders:
        return await message.reply("🛍 شما هیچ سفارشی ثبت نکرده‌اید.")

    txt = "🛍 <b>سفارش‌های من:</b>\n\n"
    for o in orders:
        st = "🟢 تحویل شده" if o["status"] == "DELIVERED" else "🚚 در حال ارسال"
        safe_title = html.escape(o['product_title'] or 'محصول حذف‌شده')
        txt += f"🔹 <b>{safe_title}</b> | کد: <code>{o['code_10']}</code> | وضعیت: {st}\n"
    await message.reply(txt, parse_mode="HTML")


@shop_router.message(Command("my_assets"))
async def cmd_my_assets(message: Message):
    if not is_private(message):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🖼 مشاهده محصولات خریداری‌شده", callback_data="show_my_assets")
    ]])
    await message.reply("📦 جهت مشاهده دارایی‌ها و محصولات خریداری‌شده خود کلیک کنید:", reply_markup=kb)


@shop_router.callback_query(F.data == "show_my_assets")
async def cb_show_my_assets(callback: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM orders WHERE buyer_id = ? ORDER BY order_id",
            (callback.from_user.id,)
        ) as cur:
            orders = await cur.fetchall()

    if not orders:
        return await callback.answer("❌ هیچ دارایی/محصولی یافت نشد.", show_alert=True)

    await callback.answer()

    # گروه‌بندی خریدها بر اساس محصول تا آیتم‌های تکراری (خریدهای متعدد از کالای نامحدود) استک شوند
    grouped = {}
    order_of_keys = []
    for o in orders:
        key = o["product_id"]
        if key not in grouped:
            grouped[key] = {
                "title": o["product_title"] or "محصول حذف‌شده",
                "desc": o["product_desc"] or "",
                "photo_id": o["product_photo_id"],
                "count": 0,
            }
            order_of_keys.append(key)
        grouped[key]["count"] += 1

    for key in order_of_keys:
        item = grouped[key]
        safe_title = html.escape(item["title"])
        safe_desc = html.escape(item["desc"])
        # استک کردن آیتم‌های تکراری به شکل «نام محصول × تعداد»
        title_line = f"{safe_title} × {item['count']}" if item["count"] > 1 else safe_title
        caption = f"🖼 <b>{title_line}</b>\n\n📝 {safe_desc}"
        try:
            if item["photo_id"]:
                await callback.bot.send_photo(chat_id=callback.from_user.id, photo=item["photo_id"], caption=caption, parse_mode="HTML")
            else:
                await callback.bot.send_message(chat_id=callback.from_user.id, text=caption, parse_mode="HTML")
        except Exception:
            pass


@shop_router.message(Command("track"))
async def cmd_track(message: Message):
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("راهنما: <code>/track [کد_۱۰_رقمی]</code>", parse_mode="HTML")

    code_10 = args[1].strip()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM orders WHERE code_10 = ?", (code_10,)) as cur:
            order = await cur.fetchone()

    if not order:
        return await message.reply("❌ سفارشی با این کد ۱۰ رقمی پیدا نشد.")

    st = "🟢 تحویل داده شده" if order["status"] == "DELIVERED" else "🚚 در حال ارسال توسط پستچی"
    safe_title = html.escape(order['product_title'] or 'محصول حذف‌شده')
    await message.reply(
        f"🔎 <b>پیگیری سفارش</b>\n\n"
        f"🛍 محصول: <b>{safe_title}</b>\n"
        f"🔐 کد پیگیری: <code>{order['code_10']}</code>\n"
        f"⚡ وضعیت: <b>{st}</b>",
        parse_mode="HTML"
    )


# =====================================================================================
# 🏦 بخش ششم: بانک آترامنتوم (Atramentum Bank)
# =====================================================================================

def _bank_panel_text(u) -> str:
    total_after_profit = u["bank_savings"] + u["last_daily_profit"]
    return (
        "🏦 <b>بانک آترامنتوم</b>\n\n"
        f"💰 مبلغ کل موجود در بانک: <code>₳ {u['bank_savings']}</code>\n"
        f"📈 آخرین سود روزانه دریافتی: <code>₳ {u['last_daily_profit']}</code>\n"
        f"🧮 موجودی کل بعد از دریافت سود: <code>₳ {total_after_profit}</code>"
    )


def _bank_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📥 واریز پول", callback_data="bank_deposit"),
        InlineKeyboardButton(text="📤 برداشت پول", callback_data="bank_withdraw"),
    ]])


@user_router.message(F.text == "بانک آترامنتوم")
async def cmd_bank_panel(message: Message):
    """این دستور متنی هم در گروه و هم در پیوی فعال است."""
    user_id = message.from_user.id
    await sync_user(user_id, message.from_user.username, message.from_user.full_name)
    u = await get_user_data(user_id)
    if not u:
        return await message.reply("❌ حساب شما یافت نشد.")
    await message.reply(_bank_panel_text(u), reply_markup=_bank_buttons(), parse_mode="HTML")


@user_router.message(Command("bank"))
@user_router.message(Command("atramentum_bank"))
@user_router.message(F.text == "بانک")
async def cmd_bank_full(message: Message):
    """این دستورات صرفاً در پیوی فعال هستند و اطلاعات کامل کیف پول را نمایش می‌دهند."""
    if not is_private(message):
        return
    user_id = message.from_user.id
    await sync_user(user_id, message.from_user.username, message.from_user.full_name)
    u = await get_user_data(user_id)
    if not u:
        return await message.reply("❌ حساب شما یافت نشد.")

    transferable = max(0, u["balance"] - u["frozen_balance"])
    text = (
        "🏦 <b>حساب بانکی آترامنتوم شما</b>\n\n"
        f"💰 موجودی کل کیف پول: <code>₳ {u['balance']}</code>\n"
        f"🔒 موجودی وثیقه قفل‌شده: <code>₳ {u['frozen_balance']}</code>\n"
        f"💳 موجودی قابل انتقال: <code>₳ {transferable}</code>\n\n"
        f"🏦 سپرده بانکی فعلی: <code>₳ {u['bank_savings']}</code>\n"
        f"📈 آخرین سود روزانه دریافتی: <code>₳ {u['last_daily_profit']}</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📥 واریز پول", callback_data="bank_deposit"),
            InlineKeyboardButton(text="📤 برداشت پول", callback_data="bank_withdraw"),
        ],
        [InlineKeyboardButton(text="💳 وام‌های آترامنتوم", callback_data="loan_menu")],
        [InlineKeyboardButton(text="⚙️ مدیریت حساب بانکی", callback_data="bank_manage")],
    ])
    await message.reply(text, reply_markup=kb, parse_mode="HTML")


@user_router.callback_query(F.data == "bank_manage")
async def cb_bank_manage(callback: CallbackQuery):
    await callback.answer(
        "⚙️ برای واریز/برداشت از دکمه‌های مربوطه و برای وام از بخش «وام‌های آترامنتوم» استفاده کنید.",
        show_alert=True,
    )


@user_router.callback_query(F.data == "bank_deposit")
async def cb_bank_deposit(callback: CallbackQuery, state: FSMContext):
    prompt = await callback.message.answer(
        "📥 <b>واریز به بانک آترامنتوم</b>\n\n"
        "لطفاً مبلغ مورد نظر برای واریز را با <b>ریپلای روی همین پیام</b> ارسال کنید.\n"
        "⚠️ فقط تا سقف «موجودی قابل انتقال» شما (موجودی منهای وثیقه قفل‌شده) قابل واریز است "
        f"و سقف کل سپرده بانکی <code>₳ {BANK_SAVINGS_CAP}</code> است.",
        parse_mode="HTML",
    )
    await state.update_data(bank_user=callback.from_user.id, bank_prompt_id=prompt.message_id)
    await state.set_state(BankForm.waiting_for_deposit_amount)
    await callback.answer()


@user_router.callback_query(F.data == "bank_withdraw")
async def cb_bank_withdraw(callback: CallbackQuery, state: FSMContext):
    prompt = await callback.message.answer(
        "📤 <b>برداشت از بانک آترامنتوم</b>\n\n"
        "لطفاً مبلغ مورد نظر برای برداشت را با <b>ریپلای روی همین پیام</b> ارسال کنید.",
        parse_mode="HTML",
    )
    await state.update_data(bank_user=callback.from_user.id, bank_prompt_id=prompt.message_id)
    await state.set_state(BankForm.waiting_for_withdraw_amount)
    await callback.answer()


@user_router.message(BankForm.waiting_for_deposit_amount)
async def process_bank_deposit(message: Message, state: FSMContext):
    data = await state.get_data()
    if message.from_user.id != data.get("bank_user"):
        return
    if not message.reply_to_message or message.reply_to_message.message_id != data.get("bank_prompt_id"):
        return await message.reply("⚠️ لطفاً روی پیام راهنمای بانک ریپلای کرده و مبلغ عددی را ارسال کنید.")
    try:
        amount = int(message.text.strip())
    except (ValueError, AttributeError):
        return await message.reply("❌ مبلغ وارد شده نامعتبر است. لطفاً یک عدد صحیح ارسال کنید.")
    if amount <= 0:
        return await message.reply("❌ مبلغ باید مثبت باشد.")

    await state.clear()
    user_id = message.from_user.id

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT balance, frozen_balance, bank_savings, is_frozen FROM users WHERE user_id = ?",
                (user_id,),
            ) as cur:
                u = await cur.fetchone()
            if not u or u["is_frozen"]:
                return await message.reply("❌ حساب شما مسدود (فریز) است.")

            transferable = max(0, u["balance"] - u["frozen_balance"])
            if amount > transferable:
                return await message.reply(
                    f"❌ حداکثر مبلغ قابل واریز شما (موجودی قابل انتقال): <code>₳ {transferable}</code>",
                    parse_mode="HTML",
                )
            remaining_cap = max(0, BANK_SAVINGS_CAP - u["bank_savings"])
            if amount > remaining_cap:
                return await message.reply(
                    f"❌ سقف سپرده‌گذاری بانک <code>₳ {BANK_SAVINGS_CAP}</code> است.\n"
                    f"سقف باقیمانده قابل واریز شما: <code>₳ {remaining_cap}</code>",
                    parse_mode="HTML",
                )

            await db.execute(
                "UPDATE users SET balance = balance - ?, bank_savings = bank_savings + ? WHERE user_id = ?",
                (amount, amount, user_id),
            )
            await db.commit()

    await message.reply(f"✅ مبلغ <code>₳ {amount}</code> با موفقیت به حساب بانکی شما واریز شد.", parse_mode="HTML")


@user_router.message(BankForm.waiting_for_withdraw_amount)
async def process_bank_withdraw(message: Message, state: FSMContext):
    data = await state.get_data()
    if message.from_user.id != data.get("bank_user"):
        return
    if not message.reply_to_message or message.reply_to_message.message_id != data.get("bank_prompt_id"):
        return await message.reply("⚠️ لطفاً روی پیام راهنمای بانک ریپلای کرده و مبلغ عددی را ارسال کنید.")
    try:
        amount = int(message.text.strip())
    except (ValueError, AttributeError):
        return await message.reply("❌ مبلغ وارد شده نامعتبر است. لطفاً یک عدد صحیح ارسال کنید.")
    if amount <= 0:
        return await message.reply("❌ مبلغ باید مثبت باشد.")

    await state.clear()
    user_id = message.from_user.id

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT bank_savings, is_frozen FROM users WHERE user_id = ?", (user_id,)
            ) as cur:
                u = await cur.fetchone()
            if not u or u["is_frozen"]:
                return await message.reply("❌ حساب شما مسدود (فریز) است.")
            if amount > u["bank_savings"]:
                return await message.reply(
                    f"❌ موجودی بانکی شما کافی نیست. سپرده فعلی: <code>₳ {u['bank_savings']}</code>",
                    parse_mode="HTML",
                )

            await db.execute(
                "UPDATE users SET balance = balance + ?, bank_savings = bank_savings - ? WHERE user_id = ?",
                (amount, amount, user_id),
            )
            await db.commit()

    await message.reply(f"✅ مبلغ <code>₳ {amount}</code> با موفقیت از بانک به کیف پول شما برداشت شد.", parse_mode="HTML")


# --- ⏰ پردازش خودکار شبانه سود بانک (ساعت ۰۰:۰۰ به وقت ایران) ---

async def run_nightly_bank_interest(bot: Bot):
    rate_raw = await get_setting("bank_daily_rate")
    try:
        rate = float(rate_raw) / 100.0
    except (TypeError, ValueError):
        rate = 0.0123
    if rate <= 0:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id, bank_savings FROM users WHERE bank_savings > 0"
        ) as cur:
            savers = await cur.fetchall()

    for saver in savers:
        user_id = saver["user_id"]
        savings = saver["bank_savings"]
        raw_profit = int(savings * rate)
        if raw_profit <= 0:
            continue

        # قانون توکن‌سوزی: فقط بخشی از سود که باعث عبور سپرده از سقف نمی‌شود پرداخت می‌شود
        allowed_profit = min(raw_profit, max(0, BANK_SAVINGS_CAP - savings))
        if allowed_profit <= 0:
            continue

        async with db_lock:
            async with aiosqlite.connect(DB_PATH) as db:
                paid = await treasury_debit(
                    db, allowed_profit, f"سود روزانه بانک برای کاربر {user_id}", related_user=user_id
                )
                if not paid:
                    logging.warning(
                        f"⚠️ عدم امکان پرداخت سود روزانه بانک به کاربر {user_id} به دلیل عدم کفایت موجودی خزانه."
                    )
                    await db.commit()
                    continue
                await db.execute(
                    "UPDATE users SET bank_savings = bank_savings + ?, last_daily_profit = ?, "
                    "last_bank_claim = ? WHERE user_id = ?",
                    (allowed_profit, allowed_profit, datetime.now(timezone.utc).isoformat(), user_id),
                )
                await db.commit()

        try:
            await bot.send_message(
                user_id,
                "🏦 <b>سود روزانه بانک آترامنتوم</b>\n\n"
                f"💰 سود امروز شما: <code>₳ {allowed_profit}</code>\n"
                f"🏦 موجودی جدید سپرده: <code>₳ {savings + allowed_profit}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass

        # فاصله کوتاه بین کاربران جهت جلوگیری از قفل شدن دیتابیس (db_lock)
        await asyncio.sleep(0.05)


async def bank_interest_loop(bot: Bot):
    while True:
        now_ir = datetime.now(IRAN_TZ)
        next_run = (now_ir + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        wait_seconds = max(60.0, (next_run - now_ir).total_seconds())
        await asyncio.sleep(wait_seconds)
        try:
            await run_nightly_bank_interest(bot)
        except Exception as e:
            logging.error(f"❌ خطا در پردازش سود شبانه بانک: {e}")


# =====================================================================================
# 💳 بخش هفتم: سیستم وام پویا و پیشرفته (Atramentum Loans)
# =====================================================================================

async def _get_loan_settings() -> dict:
    keys = [
        "min_loan_amount", "max_loan_amount", "min_loan_interest", "max_loan_interest",
        "allowed_installments", "collateral_rate", "required_balance_rate", "late_penalty_rate",
    ]
    result = {}
    for k in keys:
        result[k] = await get_setting(k)
    return result


def _compute_dynamic_interest(amount: int, settings: dict) -> float:
    """نرخ سود به‌صورت پویا و متناسب با مبلغ وام بین حداقل و حداکثر تعیین‌شده محاسبه می‌شود."""
    min_amt = float(settings.get("min_loan_amount") or 1)
    max_amt = float(settings.get("max_loan_amount") or (min_amt + 1))
    min_int = float(settings.get("min_loan_interest") or 0)
    max_int = float(settings.get("max_loan_interest") or min_int)
    if max_amt <= min_amt:
        return round(min_int, 2)
    ratio = max(0.0, min(1.0, (amount - min_amt) / (max_amt - min_amt)))
    interest = min_int + (max_int - min_int) * ratio
    return round(interest, 2)


async def _create_loan_installments(db, loan_id: int, total_repayment: int, count: int, created_at: datetime):
    base_each = total_repayment // count
    remainder = total_repayment - (base_each * count)
    for i in range(1, count + 1):
        amt = base_each + (remainder if i == count else 0)
        due_date = created_at + timedelta(days=10 * i)
        await db.execute(
            """
            INSERT INTO loan_installments
            (loan_id, installment_number, amount, due_date, status, base_amount, penalty_amount, last_reminder_stage)
            VALUES (?, ?, ?, ?, 'PENDING', ?, 0, '')
            """,
            (loan_id, i, amt, due_date.isoformat(), amt),
        )


def _loan_summary_text(target_data, amount: int, interest: float, installments: int, total_repayment: int, loan_type: str, collateral_amount: int = 0) -> str:
    safe_name = html.escape(target_data["full_name"] or "ناشناس")
    type_label = "🔒 وثیقه‌ای" if loan_type == "COLLATERAL" else "🤝 ضامنی"
    lines = [
        f"👤 متقاضی: <b>{safe_name}</b>",
        f"💰 موجودی فعلی: <code>₳ {target_data['balance']}</code>",
        f"💳 مبلغ وام: <code>₳ {amount}</code>",
        f"📈 نرخ سود: <b>{interest}٪</b>",
        f"🔢 تعداد اقساط: <b>{installments}</b>",
        f"🧮 مجموع بازپرداخت: <code>₳ {total_repayment}</code>",
        f"🏷 نوع وام: {type_label}",
    ]
    if loan_type == "COLLATERAL":
        lines.append(f"🔒 مبلغ وثیقه قفل‌شده: <code>₳ {collateral_amount}</code>")
    return "\n".join(lines)


@user_router.message(Command("loan"))
@user_router.message(F.text == "درخواست وام")
async def cmd_loan_start(message: Message, state: FSMContext):
    if not is_private(message):
        return
    user_id = message.from_user.id
    await sync_user(user_id, message.from_user.username, message.from_user.full_name)
    settings = await _get_loan_settings()
    await state.update_data(loan_settings=settings)
    await message.reply(
        "💳 <b>درخواست وام آترامنتوم</b>\n\n"
        f"💰 مبلغ وام باید بین <code>₳ {int(settings['min_loan_amount'])}</code> "
        f"تا <code>₳ {int(settings['max_loan_amount'])}</code> باشد.\n\n"
        "لطفاً مبلغ درخواستی خود را ارسال کنید:",
        parse_mode="HTML",
    )
    await state.set_state(LoanForm.waiting_for_amount)


@user_router.callback_query(F.data == "loan_menu")
async def cb_loan_menu(callback: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ درخواست وام جدید", callback_data="loan_new_request")],
        [InlineKeyboardButton(text="📋 وام‌های من", callback_data="loan_my_list")],
    ])
    try:
        await callback.message.edit_text("💳 <b>وام‌های آترامنتوم</b>\n\nیکی از گزینه‌های زیر را انتخاب کنید:", reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer("💳 <b>وام‌های آترامنتوم</b>\n\nیکی از گزینه‌های زیر را انتخاب کنید:", reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@user_router.callback_query(F.data == "loan_new_request")
async def cb_loan_new_request(callback: CallbackQuery, state: FSMContext):
    settings = await _get_loan_settings()
    await state.update_data(loan_settings=settings)
    await callback.message.answer(
        "💳 <b>درخواست وام آترامنتوم</b>\n\n"
        f"💰 مبلغ وام باید بین <code>₳ {int(settings['min_loan_amount'])}</code> "
        f"تا <code>₳ {int(settings['max_loan_amount'])}</code> باشد.\n\n"
        "لطفاً مبلغ درخواستی خود را ارسال کنید:",
        parse_mode="HTML",
    )
    await state.set_state(LoanForm.waiting_for_amount)
    await callback.answer()


@user_router.message(LoanForm.waiting_for_amount)
async def loan_process_amount(message: Message, state: FSMContext):
    data = await state.get_data()
    settings = data.get("loan_settings") or await _get_loan_settings()
    try:
        amount = int(message.text.strip())
    except (ValueError, AttributeError):
        return await message.reply("❌ لطفاً یک عدد صحیح ارسال کنید.")

    min_amt = int(settings["min_loan_amount"])
    max_amt = int(settings["max_loan_amount"])
    if amount < min_amt or amount > max_amt:
        return await message.reply(
            f"❌ مبلغ وام باید بین <code>₳ {min_amt}</code> تا <code>₳ {max_amt}</code> باشد.",
            parse_mode="HTML",
        )

    allowed_raw = str(settings.get("allowed_installments") or "2,3")
    allowed_list = [p.strip() for p in allowed_raw.split(",") if p.strip()]

    await state.update_data(loan_amount=amount)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"{n} قسط", callback_data=f"loan_inst_{n}") for n in allowed_list
    ]])
    await message.reply(
        f"🔢 تعداد اقساط مورد نظر خود را انتخاب کنید (مجاز: {allowed_raw}):",
        reply_markup=kb,
    )
    await state.set_state(LoanForm.waiting_for_installments)


@user_router.callback_query(LoanForm.waiting_for_installments, F.data.startswith("loan_inst_"))
async def loan_process_installments(callback: CallbackQuery, state: FSMContext):
    installments = int(callback.data.split("_")[2])
    await state.update_data(loan_installments=installments)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔒 وثیقه‌ای", callback_data="loan_method_collateral")],
        [InlineKeyboardButton(text="🤝 ضامنی", callback_data="loan_method_guarantor")],
    ])
    try:
        await callback.message.edit_text(
            "🏷 روش دریافت وام را انتخاب کنید:\n\n"
            "🔒 <b>وثیقه‌ای:</b> بخشی از موجودی شما به‌عنوان وثیقه قفل می‌شود.\n"
            "🤝 <b>ضامنی:</b> شخص دیگری به‌عنوان ضامن شما تایید می‌کند.",
            reply_markup=kb,
            parse_mode="HTML",
        )
    except Exception:
        pass
    await state.set_state(LoanForm.waiting_for_method)
    await callback.answer()


async def _send_loan_request_to_admins(bot: Bot, loan_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM loans WHERE id = ?", (loan_id,)) as cur:
            loan = await cur.fetchone()
        target_data = await get_user_data(loan["user_id"])

    treasury_balance = await get_treasury_balance()
    collateral_amount = 0
    if loan["loan_type"] == "COLLATERAL":
        settings = await _get_loan_settings()
        collateral_amount = int(loan["total_amount"] * float(settings["collateral_rate"]))

    summary = _loan_summary_text(
        target_data, loan["total_amount"], loan["interest_rate"], loan["installments_count"],
        loan["total_repayment"], loan["loan_type"], collateral_amount
    )
    text = (
        "💳 <b>درخواست وام جدید</b>\n\n"
        f"{summary}\n\n"
        f"🏛 موجودی فعلی خزانه: <code>₳ {treasury_balance}</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🟢 تأیید و واریز وام", callback_data=f"loan_admin_approve_{loan_id}"),
        InlineKeyboardButton(text="🔴 رد درخواست", callback_data=f"loan_admin_reject_{loan_id}"),
    ]])
    for sa_id in SUPER_ADMINS:
        try:
            await bot.send_message(sa_id, text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass


@user_router.callback_query(LoanForm.waiting_for_method, F.data == "loan_method_collateral")
async def loan_method_collateral(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    settings = data.get("loan_settings") or await _get_loan_settings()
    amount = data["loan_amount"]
    installments = data["loan_installments"]
    user_id = callback.from_user.id

    required_rate = float(settings["required_balance_rate"])
    collateral_rate = float(settings["collateral_rate"])
    required_balance = int(amount * required_rate)
    collateral_amount = int(amount * collateral_rate)

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT balance, frozen_balance, is_frozen FROM users WHERE user_id = ?", (user_id,)
            ) as cur:
                u = await cur.fetchone()

            if not u or u["is_frozen"]:
                await state.clear()
                return await callback.message.edit_text("❌ حساب شما مسدود (فریز) است.")

            transferable = max(0, u["balance"] - u["frozen_balance"])
            if transferable < required_balance:
                await state.clear()
                return await callback.message.edit_text(
                    f"❌ برای دریافت این وام باید حداقل <code>₳ {required_balance}</code> "
                    f"در موجودی آزاد خود داشته باشید.\nموجودی قابل انتقال فعلی شما: <code>₳ {transferable}</code>",
                    parse_mode="HTML",
                )
            if transferable < collateral_amount:
                await state.clear()
                return await callback.message.edit_text(
                    f"❌ موجودی آزاد شما برای قفل‌کردن وثیقه <code>₳ {collateral_amount}</code> کافی نیست.",
                    parse_mode="HTML",
                )

            interest = _compute_dynamic_interest(amount, settings)
            total_repayment = amount + int(amount * (interest / 100.0))

            await db.execute(
                "UPDATE users SET frozen_balance = frozen_balance + ? WHERE user_id = ?",
                (collateral_amount, user_id),
            )
            cur2 = await db.execute(
                """
                INSERT INTO loans
                (user_id, guarantor_id, total_amount, interest_rate, total_repayment,
                 installments_count, status, loan_type, created_at)
                VALUES (?, 0, ?, ?, ?, ?, 'PENDING_ADMIN', 'COLLATERAL', ?)
                """,
                (user_id, amount, interest, total_repayment, installments, datetime.now(timezone.utc).isoformat()),
            )
            loan_id = cur2.lastrowid
            await db.commit()

    await state.clear()
    try:
        await callback.message.edit_text(
            "✅ درخواست وام وثیقه‌ای شما ثبت شد و برای بررسی نهایی برای سوپرادمین ارسال گردید.\n"
            f"🔒 مبلغ <code>₳ {collateral_amount}</code> به‌عنوان وثیقه در حساب شما قفل شد.",
            parse_mode="HTML",
        )
    except Exception:
        pass

    await _send_loan_request_to_admins(callback.bot, loan_id)
    await callback.answer()


@user_router.callback_query(LoanForm.waiting_for_method, F.data == "loan_method_guarantor")
async def loan_method_guarantor(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.message.edit_text(
            "🤝 لطفاً آیدی عددی (شماره حساب) ضامن خود را ارسال کنید، یا روی پیام او در ربات ریپلای کرده و آیدی را بنویسید."
        )
    except Exception:
        pass
    await state.set_state(LoanForm.waiting_for_guarantor)
    await callback.answer()


@user_router.message(LoanForm.waiting_for_guarantor)
async def loan_process_guarantor(message: Message, state: FSMContext):
    data = await state.get_data()
    user_id = message.from_user.id

    guarantor_id = None
    if message.reply_to_message and message.reply_to_message.from_user:
        guarantor_id = message.reply_to_message.from_user.id
    else:
        try:
            guarantor_id = int(message.text.strip())
        except (ValueError, AttributeError):
            return await message.reply("❌ آیدی عددی نامعتبر است.")

    if guarantor_id == user_id:
        return await message.reply("❌ شما نمی‌توانید ضامن خودتان باشید.")

    guarantor_data = await get_user_data(guarantor_id)
    if not guarantor_data:
        return await message.reply("❌ کاربری با این آیدی در ربات یافت نشد.")
    if guarantor_data["is_frozen"]:
        return await message.reply("❌ حساب ضامن انتخابی مسدود (فریز) است.")

    settings = data.get("loan_settings") or await _get_loan_settings()
    amount = data["loan_amount"]
    installments = data["loan_installments"]
    interest = _compute_dynamic_interest(amount, settings)
    total_repayment = amount + int(amount * (interest / 100.0))

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                """
                INSERT INTO loans
                (user_id, guarantor_id, total_amount, interest_rate, total_repayment,
                 installments_count, status, loan_type, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'PENDING_GUARANTOR', 'GUARANTOR', ?)
                """,
                (user_id, guarantor_id, amount, interest, total_repayment, installments, datetime.now(timezone.utc).isoformat()),
            )
            loan_id = cur.lastrowid
            await db.commit()

    await state.clear()

    requester_data = await get_user_data(user_id)
    requester_name = html.escape(requester_data["full_name"] or str(user_id))

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ قبول می‌کنم", callback_data=f"guarantor_accept_{loan_id}"),
        InlineKeyboardButton(text="❌ قبول نمی‌کنم", callback_data=f"guarantor_reject_{loan_id}"),
    ]])
    try:
        await message.bot.send_message(
            guarantor_id,
            f"🤝 کاربر <b>{requester_name}</b> درخواست وام <code>₳ {amount}</code> آتر با سود "
            f"<b>{interest}٪</b> در <b>{installments}</b> قسط کرده است.\n"
            "آیا حاضر می‌شوید ضامن این شخص شوید؟\n\n"
            "⚠️ نکته: در صورت عدم پرداخت اقساط توسط متقاضی، مبالغ اقساط از موجودی شما کسر خواهد شد.",
            reply_markup=kb,
            parse_mode="HTML",
        )
        await message.reply("✅ درخواست تایید ضمانت برای ضامن انتخابی ارسال شد. پس از تایید ایشان، درخواست شما برای سوپرادمین ارسال خواهد شد.")
    except Exception:
        await message.reply("❌ امکان ارسال پیام به ضامن انتخابی وجود ندارد (احتمالاً ربات را استارت نکرده است).")


@user_router.callback_query(F.data.startswith("guarantor_accept_"))
async def cb_guarantor_accept(callback: CallbackQuery):
    loan_id = int(callback.data.split("_")[2])
    guarantor_id = callback.from_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM loans WHERE id = ?", (loan_id,)) as cur:
            loan = await cur.fetchone()

    if not loan or loan["guarantor_id"] != guarantor_id or loan["status"] != "PENDING_GUARANTOR":
        return await callback.answer("❌ این درخواست دیگر معتبر نیست.", show_alert=True)

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE loans SET status = 'PENDING_ADMIN' WHERE id = ?", (loan_id,))
            await db.commit()

    try:
        await callback.message.edit_text("✅ شما به‌عنوان ضامن این وام ثبت شدید. درخواست برای بررسی نهایی به سوپرادمین ارسال شد.")
    except Exception:
        pass
    await callback.answer()

    await _send_loan_request_to_admins(callback.bot, loan_id)

    try:
        await callback.bot.send_message(
            loan["user_id"],
            "🤝 ضامن شما درخواست ضمانت را پذیرفت. درخواست وام شما برای بررسی نهایی به سوپرادمین ارسال شد.",
        )
    except Exception:
        pass


@user_router.callback_query(F.data.startswith("guarantor_reject_"))
async def cb_guarantor_reject(callback: CallbackQuery):
    loan_id = int(callback.data.split("_")[2])
    guarantor_id = callback.from_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM loans WHERE id = ?", (loan_id,)) as cur:
            loan = await cur.fetchone()

    if not loan or loan["guarantor_id"] != guarantor_id or loan["status"] != "PENDING_GUARANTOR":
        return await callback.answer("❌ این درخواست دیگر معتبر نیست.", show_alert=True)

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE loans SET status = 'REJECTED' WHERE id = ?", (loan_id,))
            await db.commit()

    try:
        await callback.message.edit_text("❌ شما درخواست ضمانت را رد کردید.")
    except Exception:
        pass
    await callback.answer()

    try:
        await callback.bot.send_message(loan["user_id"], "❌ ضامن انتخابی شما درخواست ضمانت را رد کرد. درخواست وام شما لغو شد.")
    except Exception:
        pass


@admin_router.callback_query(F.data.startswith("loan_admin_approve_"))
async def cb_loan_admin_approve(callback: CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        return await callback.answer("❌ عدم دسترسی.", show_alert=True)

    loan_id = int(callback.data.split("_")[3])

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM loans WHERE id = ?", (loan_id,)) as cur:
                loan = await cur.fetchone()

            if not loan or loan["status"] != "PENDING_ADMIN":
                return await callback.answer("❌ این درخواست دیگر معتبر نیست یا قبلاً پردازش شده است.", show_alert=True)

            paid = await treasury_debit(
                db, loan["total_amount"], f"واریز اصل وام #{loan_id} به کاربر {loan['user_id']}",
                related_user=loan["user_id"],
            )
            if not paid:
                await db.commit()
                try:
                    await callback.message.edit_text(
                        f"{callback.message.html_text}\n\n"
                        "❌ <b>عدم امکان پرداخت به دلیل عدم کفایت موجودی خزانه.</b>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
                return await callback.answer("❌ موجودی خزانه کافی نیست.", show_alert=True)

            await db.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                (loan["total_amount"], loan["user_id"]),
            )
            await db.execute("UPDATE loans SET status = 'ACTIVE' WHERE id = ?", (loan_id,))
            await _create_loan_installments(
                db, loan_id, loan["total_repayment"], loan["installments_count"], datetime.now(timezone.utc)
            )
            await db.commit()

    try:
        await callback.message.edit_text(
            f"{callback.message.html_text}\n\n✅ <b>وام تأیید و واریز شد.</b>", parse_mode="HTML"
        )
    except Exception:
        pass
    await callback.answer("✅ وام با موفقیت واریز شد.")

    try:
        await callback.bot.send_message(
            loan["user_id"],
            f"🎉 وام شما به مبلغ <code>₳ {loan['total_amount']}</code> تأیید و به حساب شما واریز شد.\n"
            f"🧮 مجموع بازپرداخت: <code>₳ {loan['total_repayment']}</code> در <b>{loan['installments_count']}</b> قسط.",
            parse_mode="HTML",
        )
    except Exception:
        pass


@admin_router.callback_query(F.data.startswith("loan_admin_reject_"))
async def cb_loan_admin_reject(callback: CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        return await callback.answer("❌ عدم دسترسی.", show_alert=True)

    loan_id = int(callback.data.split("_")[3])

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM loans WHERE id = ?", (loan_id,)) as cur:
                loan = await cur.fetchone()

            if not loan or loan["status"] != "PENDING_ADMIN":
                return await callback.answer("❌ این درخواست دیگر معتبر نیست یا قبلاً پردازش شده است.", show_alert=True)

            if loan["loan_type"] == "COLLATERAL":
                settings = await _get_loan_settings()
                collateral_amount = int(loan["total_amount"] * float(settings["collateral_rate"]))
                await db.execute(
                    "UPDATE users SET frozen_balance = MAX(0, frozen_balance - ?) WHERE user_id = ?",
                    (collateral_amount, loan["user_id"]),
                )

            await db.execute("UPDATE loans SET status = 'REJECTED' WHERE id = ?", (loan_id,))
            await db.commit()

    try:
        await callback.message.edit_text(f"{callback.message.html_text}\n\n❌ <b>درخواست رد شد.</b>", parse_mode="HTML")
    except Exception:
        pass
    await callback.answer("❌ درخواست وام رد شد.")

    try:
        await callback.bot.send_message(loan["user_id"], "❌ متأسفانه درخواست وام شما توسط سوپرادمین رد شد.")
    except Exception:
        pass


async def _build_my_loans_view(user_id: int):
    """برمی‌گرداند: (متن، کیبورد یا None) برای نمایش وام‌های فعال/در انتظار کاربر."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM loans WHERE user_id = ? AND status IN ('ACTIVE', 'PENDING_ADMIN', 'PENDING_GUARANTOR') ORDER BY id DESC",
            (user_id,),
        ) as cur:
            loans = await cur.fetchall()

    if not loans:
        return "📋 شما در حال حاضر هیچ وام فعال یا در حال بررسی‌ای ندارید.", None

    status_labels = {
        "ACTIVE": "🟢 فعال", "PENDING_ADMIN": "⏳ در انتظار تایید سوپرادمین", "PENDING_GUARANTOR": "⏳ در انتظار تایید ضامن",
    }
    parts = ["📋 <b>وام‌های شما:</b>\n"]
    kb_rows = []
    for loan in loans:
        parts.append(
            f"\n🔹 وام #{loan['id']} | مبلغ: <code>₳ {loan['total_amount']}</code> | "
            f"وضعیت: {status_labels.get(loan['status'], loan['status'])}"
        )
        if loan["status"] == "ACTIVE":
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT * FROM loan_installments WHERE loan_id = ? AND status = 'PENDING' ORDER BY installment_number LIMIT 1",
                    (loan["id"],),
                ) as cur_i:
                    next_inst = await cur_i.fetchone()
            if next_inst:
                due = next_inst["due_date"][:10]
                parts.append(f"   💳 قسط بعدی: <code>₳ {next_inst['amount']}</code> | سررسید: {due}")
                kb_rows.append([InlineKeyboardButton(
                    text=f"💳 پرداخت قسط #{next_inst['installment_number']} وام #{loan['id']}",
                    callback_data=f"pay_inst_{next_inst['id']}",
                )])

    text = "".join(parts)
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None
    return text, kb


@user_router.message(Command("my_loans"))
async def cmd_my_loans(message: Message):
    if not is_private(message):
        return
    text, kb = await _build_my_loans_view(message.from_user.id)
    await message.reply(text, reply_markup=kb, parse_mode="HTML")


@user_router.callback_query(F.data == "loan_my_list")
async def cb_my_loans_list(callback: CallbackQuery):
    text, kb = await _build_my_loans_view(callback.from_user.id)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@user_router.callback_query(F.data.startswith("pay_inst_"))
async def cb_pay_installment(callback: CallbackQuery):
    installment_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id

    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT li.*, l.user_id AS loan_user_id, l.guarantor_id, l.installments_count "
                "FROM loan_installments li JOIN loans l ON li.loan_id = l.id WHERE li.id = ?",
                (installment_id,),
            ) as cur:
                inst = await cur.fetchone()

            if not inst or inst["loan_user_id"] != user_id:
                return await callback.answer("❌ این قسط متعلق به شما نیست یا یافت نشد.", show_alert=True)
            if inst["status"] != "PENDING":
                return await callback.answer("✅ این قسط قبلاً پرداخت شده است.", show_alert=True)

            async with db.execute("SELECT balance, is_frozen FROM users WHERE user_id = ?", (user_id,)) as cur_u:
                u = await cur_u.fetchone()
            if not u or u["is_frozen"]:
                return await callback.answer("❌ حساب شما مسدود است.", show_alert=True)
            if u["balance"] < inst["amount"]:
                return await callback.answer(
                    f"❌ موجودی شما کافی نیست. مبلغ قسط: ₳ {inst['amount']}", show_alert=True
                )

            await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (inst["amount"], user_id))
            await treasury_credit(db, inst["amount"], f"بازپرداخت قسط #{inst['installment_number']} وام #{inst['loan_id']}", related_user=user_id)
            await db.execute(
                "UPDATE loan_installments SET status = 'PAID', paid_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), installment_id),
            )

            # بررسی تسویه کامل وام
            async with db.execute(
                "SELECT COUNT(*) FROM loan_installments WHERE loan_id = ? AND status != 'PAID'",
                (inst["loan_id"],),
            ) as cur_c:
                remaining = (await cur_c.fetchone())[0]

            fully_paid = remaining == 0
            if fully_paid:
                if inst["guarantor_id"] == 0:
                    # آزادسازی کامل وثیقه در پایان وام وثیقه‌ای
                    async with db.execute("SELECT total_amount FROM loans WHERE id = ?", (inst["loan_id"],)) as cur_l:
                        loan_row = await cur_l.fetchone()
                    settings = await _get_loan_settings()
                    collateral_amount = int(loan_row[0] * float(settings["collateral_rate"]))
                    await db.execute(
                        "UPDATE users SET frozen_balance = MAX(0, frozen_balance - ?) WHERE user_id = ?",
                        (collateral_amount, user_id),
                    )
                await db.execute("UPDATE loans SET status = 'PAID' WHERE id = ?", (inst["loan_id"],))

            await db.commit()

    await callback.answer("✅ قسط با موفقیت پرداخت شد.", show_alert=True)
    try:
        await callback.message.edit_text("✅ این قسط با موفقیت پرداخت شد.")
    except Exception:
        pass
    if fully_paid:
        try:
            await callback.bot.send_message(user_id, f"🎉 وام #{inst['loan_id']} شما به‌طور کامل تسویه شد!")
        except Exception:
            pass


# --- ⏰ پردازش خودکار شبانه اقساط: یادآوری، جریمه دیرکرد و کسر خودکار بدهی معوقه ---

async def process_due_installments(bot: Bot):
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT li.*, l.user_id AS loan_user_id, l.guarantor_id "
            "FROM loan_installments li JOIN loans l ON li.loan_id = l.id "
            "WHERE li.status = 'PENDING' AND l.status = 'ACTIVE'"
        ) as cur:
            due_installments = await cur.fetchall()

    for inst in due_installments:
        try:
            due_date = datetime.fromisoformat(inst["due_date"])
            if due_date.tzinfo is None:
                due_date = due_date.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        hours_late = (now - due_date).total_seconds() / 3600.0
        borrower_id = inst["loan_user_id"]

        # یادآوری محرمانه سررسید (صرفاً در پیوی متقاضی)
        if 0 <= hours_late < 1 and inst["last_reminder_stage"] != "DUE":
            try:
                await bot.send_message(
                    borrower_id,
                    f"⏰ قسط #{inst['installment_number']} وام #{inst['loan_id']} به مبلغ "
                    f"<code>₳ {inst['amount']}</code> امروز سررسید شده است.\n"
                    f"💳 مهلت طلایی ۲۴ ساعته بدون جریمه دارید.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            async with db_lock:
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE loan_installments SET last_reminder_stage = 'DUE' WHERE id = ?", (inst["id"],)
                    )
                    await db.commit()
            continue

        if hours_late < BANK_GRACE_PERIOD_HOURS:
            continue  # هنوز در مهلت طلایی است

        days_late = int(hours_late // 24)

        # اعلام اتمام مهلت طلایی و شروع جریمه (فقط یک‌بار)
        if inst["last_reminder_stage"] != "GRACE_OVER" and days_late >= 1:
            late_rate = await get_setting("late_penalty_rate")
            try:
                late_rate = float(late_rate)
            except (TypeError, ValueError):
                late_rate = 0.0085
            penalty = int(inst["base_amount"] * late_rate * days_late)
            new_amount = inst["base_amount"] + penalty

            async with db_lock:
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE loan_installments SET amount = ?, penalty_amount = ?, last_reminder_stage = 'GRACE_OVER' WHERE id = ?",
                        (new_amount, penalty, inst["id"]),
                    )
                    await db.commit()
            try:
                await bot.send_message(
                    borrower_id,
                    f"⚠️ مهلت طلایی قسط #{inst['installment_number']} وام #{inst['loan_id']} به پایان رسید.\n"
                    f"💰 جریمه دیرکرد اعمال شد. مبلغ جدید قسط: <code>₳ {new_amount}</code>",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            if inst["guarantor_id"]:
                try:
                    await bot.send_message(
                        inst["guarantor_id"],
                        f"⚠️ متقاضی وام #{inst['loan_id']} که شما ضامن او هستید، قسط سررسیدشده را پرداخت نکرده است.",
                    )
                except Exception:
                    pass

        # اولویت کسر اتوماتیک بدهی معوقه، فقط پس از حداقل ۲ روز تاخیر (۱ روز فرصت اضافه پس از پایان مهلت طلایی)
        if days_late < 2:
            continue

        await _auto_collect_overdue_installment(bot, inst["id"])
        await asyncio.sleep(0.05)


async def _auto_collect_overdue_installment(bot: Bot, installment_id: int):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT li.*, l.user_id AS loan_user_id, l.guarantor_id "
                "FROM loan_installments li JOIN loans l ON li.loan_id = l.id WHERE li.id = ?",
                (installment_id,),
            ) as cur:
                inst = await cur.fetchone()

            if not inst or inst["status"] != "PENDING":
                return

            remaining_debt = inst["amount"]
            borrower_id = inst["loan_user_id"]
            guarantor_id = inst["guarantor_id"]

            async with db.execute(
                "SELECT balance, frozen_balance FROM users WHERE user_id = ?", (borrower_id,)
            ) as cur_b:
                borrower = await cur_b.fetchone()

            collected = 0

            # اولویت اول: کسر از وثیقه وام‌گیرنده
            if borrower and borrower["frozen_balance"] > 0 and remaining_debt > 0:
                take = min(borrower["frozen_balance"], remaining_debt)
                await db.execute(
                    "UPDATE users SET frozen_balance = frozen_balance - ? WHERE user_id = ?", (take, borrower_id)
                )
                collected += take
                remaining_debt -= take

            # اولویت دوم: کسر از موجودی آزاد وام‌گیرنده
            # (در این مرحله وثیقه قبلاً در اولویت اول به‌طور کامل مصرف شده، پس کل موجودی، موجودی آزاد است)
            if remaining_debt > 0 and borrower:
                async with db.execute("SELECT balance FROM users WHERE user_id = ?", (borrower_id,)) as cur_bb:
                    fresh_balance = (await cur_bb.fetchone())["balance"]
                take = min(fresh_balance, remaining_debt)
                if take > 0:
                    await db.execute(
                        "UPDATE users SET balance = balance - ? WHERE user_id = ?", (take, borrower_id)
                    )
                    collected += take
                    remaining_debt -= take

            # اولویت سوم: کسر از موجودی آزاد ضامن (در صورت وجود ضامن)
            if remaining_debt > 0 and guarantor_id:
                async with db.execute(
                    "SELECT balance, frozen_balance FROM users WHERE user_id = ?", (guarantor_id,)
                ) as cur_g:
                    guarantor = await cur_g.fetchone()
                if guarantor:
                    g_transferable = max(0, guarantor["balance"] - guarantor["frozen_balance"])
                    take = min(g_transferable, remaining_debt)
                    if take > 0:
                        await db.execute(
                            "UPDATE users SET balance = balance - ? WHERE user_id = ?", (take, guarantor_id)
                        )
                        collected += take
                        remaining_debt -= take

            if collected > 0:
                await treasury_credit(
                    db, collected, f"کسر خودکار بدهی معوقه قسط #{inst['installment_number']} وام #{inst['loan_id']}",
                    related_user=borrower_id,
                )

            if remaining_debt <= 0:
                await db.execute(
                    "UPDATE loan_installments SET status = 'PAID', paid_at = ? WHERE id = ?",
                    (datetime.now(timezone.utc).isoformat(), installment_id),
                )
                async with db.execute(
                    "SELECT COUNT(*) FROM loan_installments WHERE loan_id = ? AND status != 'PAID'",
                    (inst["loan_id"],),
                ) as cur_c:
                    remaining_count = (await cur_c.fetchone())[0]
                if remaining_count == 0:
                    if not guarantor_id:
                        async with db.execute("SELECT total_amount FROM loans WHERE id = ?", (inst["loan_id"],)) as cur_l:
                            loan_row = await cur_l.fetchone()
                        settings = await _get_loan_settings()
                        collateral_amount = int(loan_row[0] * float(settings["collateral_rate"]))
                        await db.execute(
                            "UPDATE users SET frozen_balance = MAX(0, frozen_balance - ?) WHERE user_id = ?",
                            (collateral_amount, borrower_id),
                        )
                    await db.execute("UPDATE loans SET status = 'PAID' WHERE id = ?", (inst["loan_id"],))
            else:
                # موجودی وثیقه، وام‌گیرنده و ضامن به‌طور کامل کافی نبود
                await db.execute(
                    "UPDATE loan_installments SET amount = ? WHERE id = ?", (remaining_debt, installment_id)
                )

            await db.commit()

    if collected > 0:
        try:
            await bot.send_message(
                borrower_id,
                f"🔻 مبلغ <code>₳ {collected}</code> بابت قسط معوق #{inst['installment_number']} وام #{inst['loan_id']} "
                f"به‌صورت خودکار از حساب/وثیقه شما کسر شد.",
                parse_mode="HTML",
            )
        except Exception:
            pass
    if remaining_debt > 0:
        for sa_id in SUPER_ADMINS:
            try:
                await bot.send_message(
                    sa_id,
                    f"🚨 <b>هشدار نکول وام</b>\n\n"
                    f"وثیقه، موجودی وام‌گیرنده و ضامن برای پرداخت قسط #{inst['installment_number']} "
                    f"وام #{inst['loan_id']} (کاربر <code>{borrower_id}</code>) کافی نبود.\n"
                    f"💰 باقیمانده بدهی وصول‌نشده: <code>₳ {remaining_debt}</code>",
                    parse_mode="HTML",
                )
            except Exception:
                pass


async def loan_management_loop(bot: Bot):
    while True:
        await asyncio.sleep(3600)  # بررسی ساعتی جهت رعایت دقیق مهلت طلایی و جلوگیری از اسپم یادآوری‌ها
        try:
            await process_due_installments(bot)
        except Exception as e:
            logging.error(f"❌ خطا در پردازش اقساط وام: {e}")


# --- راهنمای دستورات ---


@user_router.message(Command("help"))
@user_router.message(F.text == "راهنمای جامع بانک")
async def cmd_help(message: Message):
    user_id = message.from_user.id
    is_sa = is_super_admin(user_id)
    u = await get_user_data(user_id)
    is_adm = u and u["is_admin"]

    txt = (
        "📱 <b>راهنمای دستورات کاربران:</b>\n"
        "🔹 <code>/start</code> - شروع و دریافت شماره حساب\n"
        "🔹 <code>/cancel</code> - لغو هر عملیات درحال‌انجام (انتقال، ساخت فروشگاه، ثبت محصول و...)\n"
        "🔹 <code>/profile</code> یا «پروفایل» - مشاهده نام، شماره حساب، موجودی و وضعیت\n"
        "🔹 <code>/transfer</code> یا «انتقال آتر» - انتقال آتر (چند روش مختلف)\n"
        "🔹 <code>/my_orders</code> - مشاهده وضعیت سفارش‌های خریداری‌شده\n"
        "🔹 <code>/my_assets</code> - مشاهده دارایی‌ها و عکس محصولات خریداری‌شده\n"
        "🔹 <code>/track [کد]</code> - پیگیری لحظه‌ای وضعیت سفارش با کد ۱۰ رقمی\n\n"
        "🏦 <b>بانک آترامنتوم:</b>\n"
        "🔹 «بانک آترامنتوم» - پنل سریع بانک (در گروه و پیوی)\n"
        "🔹 <code>/bank</code> یا <code>/atramentum_bank</code> یا «بانک» - حساب بانکی کامل (فقط پیوی)\n\n"
        "💳 <b>وام آترامنتوم:</b>\n"
        "🔹 <code>/loan</code> یا «درخواست وام» - ثبت درخواست وام جدید (فقط پیوی)\n"
        "🔹 <code>/my_loans</code> - مشاهده وام‌های فعال و پرداخت اقساط\n\n"
        "🏪 <b>دستورات فروشندگان:</b>\n"
        "🔹 <code>/request_shop</code> - ارسال درخواست ثبت فروشگاه\n"
        "🔹 <code>/add_product</code> - ثبت محصول جديد با عکس و مشخصات\n"
        "🔹 <code>/inventory</code> - مدیریت موجودی انبار\n"
        "🔹 <code>/my_shop</code> - آمار کل و میزان درآمد فروشگاه\n\n"
        "🚚 <b>دستورات پستچی‌ها:</b>\n"
        "🔹 <code>/courier_orders</code> - مشاهده سفارش‌های آماده ارسال\n"
        "🔹 <code>/confirm_dispatch [کد]</code> - ثبت تحویل نهایی سفارش با کد ۱۰ رقمی\n\n"
    )

    if is_adm or is_sa:
        txt += (
            "👥 <b>دستورات ادمین (فقط پیوی):</b>\n"
            "🔹 <code>/users</code> - لیست کاربران\n"
            "🔹 <code>/groups</code> - لیست گروه‌ها\n"
            "🔹 <code>/group_users [نام]</code> - اعضای یک گروه\n"
            "🔹 <code>/create_group [نام]</code> - فقط اضافه کردن گروه (بدون لینک)\n"
            "🔹 <code>/add_group [نام]</code> - ساخت <b>گروه مجازی</b> + لینک دعوت یکتا (این گروه فقط درون ربات است)\n"
            "🔹 <code>/extend_group [نام] [روز]</code> - تمدید لینک فعلی\n"
            "🔹 <code>/renew_group [نام] [روز]</code> - ساخت لینک جدید با مدت اعتبار\n"
            "🔹 <code>/rename_group [قدیمی] [جدید]</code> - تغییر نام گروه\n"
            "🔹 <code>/move_group [آیدی] [گروه]</code> - تغییر گروه کاربر\n"
            "🔹 <code>/remove_group [آیدی]</code> - برگرداندن به Default\n"
            "🔹 <code>/list_shops</code> - مشاهده لیست کامل فروشگاه‌ها\n"
            "🔹 <code>/list_couriers</code> - مشاهده لیست تمام پستچی‌ها و آیدی عددی آن‌ها\n\n"
        )

    if is_sa:
        txt += (
            "👑 <b>دستورات سوپرادمین (فقط پیوی):</b>\n"
            "🔸 <code>/set_shop_rates</code> - تنظیم درصدهای مالیات، بانک و سوخت فروشگاه\n"
            "🔸 <code>/set_courier_rates</code> - تنظیم درصدهای پستی و بازه‌ها\n"
            "🔸 <code>/shop_requests</code> - بررسی درخواست‌های فروشگاه جدید\n"
            "🔸 <code>/remove_shop [آیدی]</code> - حذف یا لغو مجوز فروشگاه\n"
            "🔸 <code>/add_courier [آیدی]</code> - افزودن پستچی جدید\n"
            "🔸 <code>/remove_courier [آیدی]</code> - حذف پستچی\n"
            "🔸 <code>/give [آیدی] [مقدار]</code> - واریز آتر\n"
            "🔸 <code>/take [آیدی] [مقدار]</code> - کسر آتر\n"
            "🔸 <code>/rewardgroup [گروه] [مقدار]</code> - واریز همگانی به یک گروه\n"
            "🔸 <code>/undo [شناسه_تراکنش]</code> - لغو و برگشت تراکنش\n"
            "🔸 <code>/freeze [آیدی]</code> / <code>/unfreeze [آیدی]</code> - مسدود/فعال‌سازی\n"
            "🔸 <code>/promote [آیدی]</code> / <code>/demote [آیدی]</code> - ارتقا/سلب ادمین\n"
            "🔸 <code>/add_super [آیدی]</code> / <code>/remove_super [آیدی]</code> - مدیریت سوپرادمین‌ها\n"
            "🔸 <code>/list_admins</code> - لیست ادمین‌ها و سوپرادمین‌ها\n"
            "🔸 <code>/check [آیدی]</code> - مشاهده اطلاعات کامل حساب\n"
            "🔸 <code>/economy</code> - آمار کل نقدینگی و وضعیت خزانه مرکزی\n"
            "🔸 <code>/set_bank_rate [درصد]</code> - تنظیم نرخ سود روزانه بانک\n"
            "🔸 <code>/set_min_loan [مبلغ]</code> / <code>/set_max_loan [مبلغ]</code> - بازه مبلغ وام\n"
            "🔸 <code>/set_loan_interest [حداقل] [حداکثر]</code> - بازه سود وام\n"
            "🔸 <code>/set_loan_installments [لیست با کاما]</code> - تعداد اقساط مجاز\n"
            "🔸 <code>/set_collateral_rate [نسبت]</code> - نرخ وثیقه وام (مثال: 0.17)\n"
            "🔸 <code>/set_req_balance_rate [نسبت]</code> - نرخ موجودی اولیه لازم\n"
            "🔸 <code>/set_late_penalty_rate [نسبت]</code> - نرخ جریمه دیرکرد روزانه\n"
            "🔸 <code>/backup_now</code> - دانلود بکاپ Zip دیتابیس\n"
            "🔸 <code>/force_backup</code> - ارسال فایل دیتابیس به کانال تلگرام\n"
            "🔸 <code>/restore</code> - بازیابی دیتابیس (با ریپلای روی فایل)\n"
            "🔸 <code>/reset_all</code> - صفر کردن کاملاً دائم دیتابیس\n"
        )

    await message.reply(txt, parse_mode="HTML")


# --- نقطه شروع ربات ---


async def main():
    if not BOT_TOKEN:
        logging.error("❌ BOT_TOKEN یافت نشد! لطفا آن را در فایل .env یا Environment Variables تنظیم کنید.")
        return

    # اجرای وب سرور جهت ساخت Web Service در حالت Dummy
    await start_dummy_server()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # بازیابی دیتابیس از تلگرام در صورت نیازمندی سرور (مثل Render بعد از ری‌استارت)
    await restore_db_from_telegram(bot)

    # ایجاد جداول دیتابیس
    await init_db()

    dp.include_router(user_router)
    dp.include_router(admin_router)
    dp.include_router(shop_router)

    # شروع حلقه بکاپ‌گیری خودکار تلگرامی
    asyncio.create_task(auto_backup_loop(bot))

    # 🏦 شروع حلقه پردازش سود شبانه بانک آترامنتوم (ساعت ۰۰:۰۰ به وقت ایران)
    asyncio.create_task(bank_interest_loop(bot))

    # 💳 شروع حلقه مدیریت اقساط وام (یادآوری، جریمه دیرکرد و کسر خودکار بدهی معوقه)
    asyncio.create_task(loan_management_loop(bot))

    logging.info("🚀 ربات بانک آتر با موفقیت روشن شد و آماده پردازش است.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("🛑 ربات خاموش شد.")
