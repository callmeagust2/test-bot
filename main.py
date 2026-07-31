
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
            ("tier3_pct", 12.0)   # 1000 آتر به بالا
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
 
 
async def get_setting(key: str) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT val FROM system_settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0.0
 
 
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
 
 
# --- ۳. خرید محصول و محاسبات دقیق مالی آتر ---
 
@shop_router.callback_query(F.data.startswith("buy_prod_"))
async def cb_buy_product(callback: CallbackQuery):
    buyer_id = callback.from_user.id
    product_id = int(callback.data.split("_")[2])
 
    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM products WHERE product_id = ?", (product_id,)) as cur:
                prod = await cur.fetchone()
 
            if not prod:
                return await callback.answer("❌ محصول پیدا نشد.", show_alert=True)
 
            if prod["stock_type"] != "UNLIMITED" and prod["stock_qty"] <= 0:
                return await callback.answer("❌ موجودی این محصول به اتمام رسیده است.", show_alert=True)
 
            # جلوگیری از خرید بیش از یک عدد توسط یک کاربر، فقط برای محصولات محدود/تکی
            # (محصولات نامحدود هیچ محدودیتی در تعداد خرید ندارند)
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
                return await callback.answer(f"❌ موجودی ناکافی! قیمت محصول: ₳ {price} + هزینه پست: ₳ {courier_fee} = مجموع: ₳ {total_cost}", show_alert=True)
 
            # کسر مبلغ از خریدار
            await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (total_cost, buyer_id))
 
            # تقسیم کالا: ۵۱٪ فروشنده، ۴۰٪ بانک، ۹٪ سوخت
            async with db.execute("SELECT owner_id FROM shops WHERE shop_id = ?", (prod["shop_id"],)) as cur_s:
                shop_owner_id = (await cur_s.fetchone())["owner_id"]
 
            s_pct = await get_setting("shop_seller_pct")
            b_pct = await get_setting("shop_bank_pct")
 
            seller_share = int(price * (s_pct / 100.0))
            bank_share = int(price * (b_pct / 100.0))
            # باقی مانده درصد سوخت (امحا) می‌شود و به حسابی واریز نمی‌شود.
 
            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (seller_share, shop_owner_id))
 
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
 
    # ارسال کد ۱۰ رقمی هم‌زمان برای خریدار، فروشنده و پستچی‌ها
    msg_buyer = f"🎉 خرید شما نهایی شد!\n🛍 محصول: <b>{html.escape(prod['title'])}</b>\n🔐 کد امنیتی ۱۰ رقمی شما: <code>{code_10}</code>"
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
        "🔹 <code>/profile</code> یا «پروفایل» - مشاهده نام، شماره حساب، موجودی و وضعیت\n"
        "🔹 <code>/transfer</code> یا «انتقال آتر» - انتقال آتر (چند روش مختلف)\n"
        "🔹 <code>/my_orders</code> - مشاهده وضعیت سفارش‌های خریداری‌شده\n"
        "🔹 <code>/my_assets</code> - مشاهده دارایی‌ها و عکس محصولات خریداری‌شده\n"
        "🔹 <code>/track [کد]</code> - پیگیری لحظه‌ای وضعیت سفارش با کد ۱۰ رقمی\n\n"
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
            "🔸 <code>/economy</code> - آمار کل نقدینگی\n"
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
 
    logging.info("🚀 ربات بانک آتر با موفقیت روشن شد و آماده پردازش است.")
    await dp.start_polling(bot)
 
 
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("🛑 ربات خاموش شد.")
 
