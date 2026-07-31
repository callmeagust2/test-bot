import random
import string
import html
import aiosqlite
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command, CommandObject

shop_router = Router()
DB_PATH = "atr_bank.db"

# ==========================================
# ۱. توابع کمکی دیتابیس و راه‌اندازی ساختار
# ==========================================

async def init_shop_db():
    """ایجاد جدول‌های مورد نیاز سیستم فروشگاهی در صورت عدم وجود"""
    async with aiosqlite.connect(DB_PATH) as db:
        # جدول فروشگاه‌ها
        await db.execute("""
            CREATE TABLE IF NOT EXISTS shops (
                shop_id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                shop_name TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                status TEXT DEFAULT 'PENDING'
            )
        """)
        # جدول محصولات
        await db.execute("""
            CREATE TABLE IF NOT EXISTS products (
                product_id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                price REAL NOT NULL,
                stock_type TEXT NOT NULL, -- 'SINGLE', 'LIMITED', 'UNLIMITED'
                stock_count INTEGER DEFAULT 0,
                needs_courier INTEGER DEFAULT 0,
                photo_file_id TEXT,
                channel_msg_id INTEGER
            )
        """)
        # جدول سفارش‌ها
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_code TEXT PRIMARY KEY,
                buyer_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                shop_id INTEGER NOT NULL,
                courier_id INTEGER DEFAULT 0,
                product_price REAL NOT NULL,
                shipping_fee REAL DEFAULT 0,
                status TEXT DEFAULT 'PROCESSING', -- 'PROCESSING', 'DELIVERED'
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # اضافه کردن ستون‌های نقش و دارایی در صورت نیاز به جدول کاربران
        try:
            await db.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'USER'")
        except:
            pass # ستون قبلا وجود دارد
            
        await db.commit()

def generate_10_digit_code():
    """تولید کد امنیتی ۱۰ رقمی یکتا"""
    return ''.join(random.choices(string.digits, k=10))


# ==========================================
# ۲. دسترسی‌ها و مدیریت سوپر ادمین (Super Admin)
# ==========================================

@shop_router.message(Command("shop_requests"))
async def list_shop_requests(message: Message):
    """بررسی درخواست‌های ثبت فروشگاه جدید"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM shops WHERE status = 'PENDING'") as cur:
            requests = await cur.fetchall()

    if not requests:
        return await message.reply("📝 هیچ درخواستی برای ثبت فروشگاه وجود ندارد.")

    text = "📥 <b>درخواست‌های معوقه فروشگاه:</b>\n\n"
    buttons = []
    for req in requests:
        text += f"🏪 نام: <b>{html.escape(req['shop_name'])}</b> | کانال: {req['channel_id']} | متقاضی: <code>{req['owner_id']}</code>\n"
        buttons.append([
            InlineKeyboardButton(text=f"✅ تایید {req['shop_name']}", callback_data=f"approve_shop_{req['shop_id']}"),
            InlineKeyboardButton(text=f"❌ رد", callback_data=f"reject_shop_{req['shop_id']}")
        ])
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.reply(text, reply_markup=kb, parse_mode="HTML")

@shop_router.callback_query(F.data.startswith("approve_shop_"))
async def approve_shop(callback: CallbackQuery):
    shop_id = int(callback.data.split("_")[2])
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT owner_id FROM shops WHERE shop_id = ?", (shop_id,)) as cur:
            shop = await cur.fetchone()
        
        if shop:
            await db.execute("UPDATE shops SET status = 'APPROVED' WHERE shop_id = ?", (shop_id,))
            await db.execute("UPDATE users SET role = 'SELLER' WHERE user_id = ?", (shop['owner_id'],))
            await db.commit()
            await callback.bot.send_message(shop['owner_id'], "🎉 درخواست ثبت فروشگاه شما توسط سوپرادمین تایید شد!")
            await callback.answer("✅ فروشگاه تایید شد.")
            await callback.message.edit_text("✅ درخواست پردازش شد.")

@shop_router.message(Command("add_courier"))
async def add_courier(message: Message, command: CommandObject):
    """ارتقای کاربر به نقش پستچی"""
    if not command.args:
        return await message.reply("⚠️ لطفاً آیدی عددی کاربر را وارد کنید:\nمثال: `/add_courier 12345678`", parse_mode="Markdown")
    
    courier_id = int(command.args.strip())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET role = 'COURIER' WHERE user_id = ?", (courier_id,))
        await db.commit()
    await message.reply(f"🚚 کاربر <code>{courier_id}</code> با موفقیت به لیست پستچی‌ها اضافه شد.", parse_mode="HTML")


# ==========================================
# ۳. دسترسی‌ها و مدیریت فروشندگان (Shop Owners)
# ==========================================

@shop_router.message(Command("request_shop"))
async def request_shop(message: Message, command: CommandObject, bot: Bot):
    """ارسال درخواست ثبت فروشگاه: /request_shop نام_فروشگاه @آیدی_کانال"""
    if not command.args or len(command.args.split()) < 2:
        return await message.reply("⚠️ فرمت اشتباه است!\nمثال: `/request_shop فروشگاه_آتر @my_channel`", parse_mode="Markdown")
    
    args = command.args.split(maxsplit=1)
    shop_name = args[0]
    channel_id = args[1]

    # بررسی ادمین بودن ربات در کانال/گروه
    try:
        chat_member = await bot.get_chat_member(chat_id=channel_id, user_id=bot.id)
        if chat_member.status not in ["administrator", "creator"]:
            return await message.reply("❌ **خطا:** ربات هنوز در کانال/گروه مشخص شده ادمین نشده است. ابتدا ربات را ادمین کرده و سپس تلاش کنید.")
    except Exception as e:
        return await message.reply(f"❌ خطا در بررسی کانال: اطمینان حاصل کنید آیدی کانال درست است و ربات در آن ادمین شده است.\nجزئیات: {e}")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO shops (owner_id, shop_name, channel_id) VALUES (?, ?, ?)",
            (message.from_user.id, shop_name, channel_id)
        )
        await db.commit()

    await message.reply("✅ درخواست ثبت فروشگاه ارسال شد و پس از بررسی ادمین فعال می‌شود.")

@shop_router.message(Command("add_product"))
async def add_product(message: Message, command: CommandObject, bot: Bot):
    """
    ثبت محصول جدید و انتشار خودکار بنر
    فرمت: /add_product قیمت | تعداد(SINGLE/LIMITED/UNLIMITED) | نیاز_به_پست(1 یا 0) | نام | توضیحات
    (عکس محصول را همراه این کپشن بفرستید)
    """
    if not message.photo:
        return await message.reply("📷 لطفاً دستور را همراه با عکس محصول بفرستید.")
    
    if not command.args:
        return await message.reply(
            "⚠️ فرمت دستور:\n`/add_product 500 | LIMITED:10 | 1 | اکانت گیمینگ | توضیحات کالا`",
            parse_mode="Markdown"
        )
    
    try:
        parts = [p.strip() for p in command.args.split("|")]
        price = float(parts[0])
        stock_info = parts[1].split(":")
        stock_type = stock_info[0].upper()
        stock_count = int(stock_info[1]) if len(stock_info) > 1 else (1 if stock_type == "SINGLE" else 999999)
        needs_courier = int(parts[2])
        title = parts[3]
        description = parts[4] if len(parts) > 4 else ""
        photo_file_id = message.photo[-1].file_id

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM shops WHERE owner_id = ? AND status = 'APPROVED'", (message.from_user.id,)) as cur:
                shop = await cur.fetchone()
            
            if not shop:
                return await message.reply("❌ شما فروشگاه تایید شده‌ای ندارید.")

            # ۱. ذخیره در دیتابیس
            cursor = await db.execute(
                """INSERT INTO products 
                (shop_id, title, description, price, stock_type, stock_count, needs_courier, photo_file_id) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (shop['shop_id'], title, description, price, stock_type, stock_count, needs_courier, photo_file_id)
            )
            product_id = cursor.lastrowid

            # ۲. ساخت متن بنر و ارسال به کانال
            courier_str = "🚚 دارد" if needs_courier else "❌ ندارد"
            stock_str = "تکی" if stock_type == "SINGLE" else ("نامحدود" if stock_type == "UNLIMITED" else f"{stock_count} عدد")
            
            caption = (
                f"🛍 <b>فروشگاه {html.escape(shop['shop_name'])}</b>\n\n"
                f"🔹 <b>محصول:</b> {html.escape(title)}\n"
                f"📝 <b>توضیحات:</b> {html.escape(description)}\n"
                f"💰 <b>قیمت:</b> <code>₳ {price}</code>\n"
                f"📦 <b>موجودی:</b> {stock_str}\n"
                f"🚚 <b>نیاز به پستچی:</b> {courier_str}\n"
            )
            
            bot_info = await bot.get_me()
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🛍 خرید این محصول", url=f"https://t.me/{bot_info.username}?start=buy_{product_id}")
            ]])

            sent_msg = await bot.send_photo(chat_id=shop['channel_id'], photo=photo_file_id, caption=caption, reply_markup=kb, parse_mode="HTML")
            
            # ثبت آیدی پیام بنر برای بروزرسانی موجودی
            await db.execute("UPDATE products SET channel_msg_id = ? WHERE product_id = ?", (sent_msg.message_id, product_id))
            await db.commit()

        await message.reply("✅ محصول با موفقیت ثبت شد و بنر آن در کانال قرار گرفت!")

    except Exception as e:
        await message.reply(f"❌ خطا در ثبت محصول. لطفاً ورودی‌ها را بررسی کنید.\nجزئیات: {e}")


# ==========================================
# ۴. فرایند خرید و تقسیمات مالی (Buyers & Logic)
# ==========================================

@shop_router.message(Command("start"))
async def handle_start_buy(message: Message, command: CommandObject, bot: Bot):
    """مدیریت لینک‌های خرید اختصاصی که از دکمه شیشه‌ای کانال آمده‌اند"""
    if command.args and command.args.startswith("buy_"):
        product_id = int(command.args.split("_")[1])
        await initiate_purchase(message.from_user.id, product_id, bot, message)

async def initiate_purchase(buyer_id: int, product_id: int, bot: Bot, event_target):
    """منطق کامل تراکنش مالی، کسر درصدها، پستچی و ارسال کد ۱۰ رقمی"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        
        # اطلاعات محصول و فروشگاه
        async with db.execute(
            "SELECT p.*, s.owner_id as seller_id, s.channel_id FROM products p JOIN shops s ON p.shop_id = s.shop_id WHERE p.product_id = ?", 
            (product_id,)
        ) as cur:
            prod = await cur.fetchone()

        if not prod or prod['stock_count'] <= 0:
            msg_text = "❌ این محصول به پایان رسیده است."
            return await event_target.reply(msg_text) if isinstance(event_target, Message) else await event_target.answer(msg_text, show_alert=True)

        # اطلاعات خریدار
        async with db.execute("SELECT balance, is_frozen FROM users WHERE user_id = ?", (buyer_id,)) as cur:
            buyer = await cur.fetchone()

        if not buyer or buyer['is_frozen']:
            msg_text = "❌ حساب شما فریز است یا در ربات ثبت‌نام نکرده‌اید."
            return await event_target.reply(msg_text) if isinstance(event_target, Message) else await event_target.answer(msg_text, show_alert=True)

        price = prod['price']
        
        # محاسبات پست
        shipping_pct = 0
        if prod['needs_courier']:
            if price <= 99:
                shipping_pct = 8
            elif price <= 999:
                shipping_pct = 10
            else:
                shipping_pct = 12
        
        shipping_fee = (price * shipping_pct) / 100
        total_cost = price + shipping_fee

        if buyer['balance'] < total_cost:
            msg_text = f"❌ موجودی کافی نیست!\nقیمت کالا: ₳{price}\nهزینه پست ({shipping_pct}٪): ₳{shipping_fee}\nجمع کل: ₳{total_cost}"
            return await event_target.reply(msg_text) if isinstance(event_target, Message) else await event_target.answer(msg_text, show_alert=True)

        # ----------------------------------
        # تقسیمات مالی دقیق:
        # ----------------------------------
        # ۱. تقسیم پول کالا: ۵۱٪ فروشنده، ۴۰٪ بانک، ۹٪ سوخت
        seller_share = price * 0.51
        bank_share = price * 0.40
        burn_share = price * 0.09

        # ۲. کسر موجودی از خریدار
        await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (total_cost, buyer_id))
        
        # ۳. واریز سهم فروشنده
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (seller_share, prod['seller_id']))
        
        # ۴. واریز به بانک مرکزی (اکانت سیستم با آیدی 0)
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = 0", (bank_share,))

        # ۵. کاهش موجودی کالا
        new_stock = prod['stock_count'] - 1
        await db.execute("UPDATE products SET stock_count = ? WHERE product_id = ?", (new_stock, product_id))

        # ۶. ثبت کد ۱۰ رقمی امنیتی سفارش
        order_code = generate_10_digit_code()
        initial_status = 'PROCESSING' if prod['needs_courier'] else 'DELIVERED'
        
        await db.execute(
            """INSERT INTO orders (order_code, buyer_id, product_id, shop_id, product_price, shipping_fee, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (order_code, buyer_id, product_id, prod['shop_id'], price, shipping_fee, initial_status)
        )

        # ثبت لوگ تراکنش
        await db.execute(
            "INSERT INTO audit_logs (tx_id, timestamp, from_user, to_user, amount, reason, status) VALUES (?, DATETIME('now'), ?, ?, ?, ?, 'SUCCESS')",
            (f"ORD-{order_code}", buyer_id, prod['seller_id'], price, f"خرید {prod['title']}")
        )
        await db.commit()

        # ----------------------------------
        # بروزرسانی موجودی بنر در کانال
        # ----------------------------------
        try:
            stock_str = "تکی (تمام شد)" if prod['stock_type'] == "SINGLE" and new_stock == 0 else ("نامحدود" if prod['stock_type'] == "UNLIMITED" else f"{new_stock} عدد")
            courier_str = "🚚 دارد" if prod['needs_courier'] else "❌ ندارد"
            
            new_caption = (
                f"🛍 <b>فروشگاه</b>\n\n"
                f"🔹 <b>محصول:</b> {html.escape(prod['title'])}\n"
                f"📝 <b>توضیحات:</b> {html.escape(prod['description'])}\n"
                f"💰 <b>قیمت:</b> <code>₳ {price}</code>\n"
                f"📦 <b>موجودی جدید:</b> {stock_str}\n"
                f"🚚 <b>نیاز به پستچی:</b> {courier_str}\n"
            )
            bot_info = await bot.get_me()
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🛍 خرید این محصول", url=f"https://t.me/{bot_info.username}?start=buy_{product_id}")
            ]])
            await bot.edit_message_caption(chat_id=prod['channel_id'], message_id=prod['channel_msg_id'], caption=new_caption, reply_markup=kb, parse_mode="HTML")
        except:
            pass

        # ----------------------------------
        # اطلاع‌رسانی به خریدار، فروشنده و پستچی‌ها
        # ----------------------------------
        msg_buyer = f"🎉 <b>خرید با موفقیت انجام شد!</b>\n\n📦 محصول: <b>{prod['title']}</b>\n🔑 <b>کد امنیتی ۱۰ رقمی شما:</b> <code>{order_code}</code>\n\n"
        if prod['needs_courier']:
            msg_buyer += "🚚 کد بالا را هنگام تحویل کالا به پستچی ارائه دهید."
        
        if isinstance(event_target, Message):
            await event_target.reply(msg_buyer, parse_mode="HTML")
        else:
            await bot.send_message(buyer_id, msg_buyer, parse_mode="HTML")

        # اطلاع به فروشنده
        await bot.send_message(
            prod['seller_id'], 
            f"🛒 <b>سفارش جدید!</b>\nمحصول: {prod['title']}\nمبلغ کسر شده: ₳{price}\nسهم شما (۵۱٪): ₳{seller_share}\n🔑 کد سفارش: <code>{order_code}</code>", 
            parse_mode="HTML"
        )

        # ارسال پیام به لیست پستچی‌ها در صورت نیاز
        if prod['needs_courier']:
            async with db.execute("SELECT user_id FROM users WHERE role = 'COURIER'") as cur:
                couriers = await cur.fetchall()
                for c in couriers:
                    try:
                        await bot.send_message(
                            c['user_id'],
                            f"📦 <b>سفارش جدید آماده ارسال!</b>\nکد سفارش: <code>{order_code}</code>\nمبلغ خرید: ₳{price}\nهزینه کل پست: ₳{shipping_fee}",
                            parse_mode="HTML"
                        )
                    except:
                        pass


# ==========================================
# ۵. دسترسی‌ها و مدیریت پستچی‌ها (Couriers)
# ==========================================

@shop_router.message(Command("courier_orders"))
async def list_courier_orders(message: Message):
    """مشاهده سفارش‌های آماده برای ارسال"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM orders WHERE status = 'PROCESSING'") as cur:
            orders = await cur.fetchall()

    if not orders:
        return await message.reply("📦 هیچ سفارشی در انتظار ارسال نیست.")

    text = "🚚 <b>لیست سفارش‌های در انتظار ارسال:</b>\n\n"
    for ord_item in orders:
        text += f"🔹 کد: <code>{ord_item['order_code']}</code> | سهم پست: <code>₳ {ord_item['shipping_fee']}</code>\n"
    
    await message.reply(text, parse_mode="HTML")

@shop_router.message(Command("confirm_delivery"))
async def confirm_delivery(message: Message, command: CommandObject, bot: Bot):
    """ثبت تحویل کالا با کد ۱۰ رقمی: /confirm_delivery 1234567890"""
    if not command.args:
        return await message.reply("⚠️ لطفاً کد ۱۰ رقمی تحویل را وارد کنید:\nمثال: `/confirm_delivery 1234567890`", parse_mode="Markdown")

    code = command.args.strip()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM orders WHERE order_code = ? AND status = 'PROCESSING'", (code,)) as cur:
            order = await cur.fetchone()

        if not order:
            return await message.reply("❌ کدی با این مشخصات یافت نشد یا قبلاً تحویل شده است.")

        shipping_fee = order['shipping_fee']
        
        # تقسیمات دریافتی پستچی: ۶۱٪ پستچی، ۳۰٪ بانک، ۹٪ سوخت
        courier_share = shipping_fee * 0.61
        bank_share = shipping_fee * 0.30

        courier_id = message.from_user.id

        # ۱. واریز به پستچی
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (courier_share, courier_id))
        # ۲. واریز سهم بانک
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = 0", (bank_share,))
        # ۳. تغییر وضعیت سفارش
        await db.execute("UPDATE orders SET status = 'DELIVERED', courier_id = ? WHERE order_code = ?", (courier_id, code))
        
        await db.commit()

        # خبر رسانی به خریدار
        await bot.send_message(order['buyer_id'], f"✅ سفارش شما با کد <code>{code}</code> تحویل داده شد و به دارایی‌های شما اضافه گردید.", parse_mode="HTML")

    await message.reply(f"✅ تحویل کالا با موفقیت تایید شد.\n💰 سهم شما (۶۱٪ از هزینه پست): ₳{courier_share} به حساب شما اضافه شد.")


# ==========================================
# ۶. دسترسی‌ها عمومی و پیگیری (Public / Buyers)
# ==========================================

@shop_router.message(Command("my_assets"))
@shop_router.message(F.text == "دارایی های من")
async def show_my_assets(message: Message):
    """مشاهده لیست محصولات خریداری شده به همراه عکس و اطلاعات"""
    user_id = message.from_user.id
    
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT p.title, p.description, p.photo_file_id, o.order_code, o.created_at 
               FROM orders o 
               JOIN products p ON o.product_id = p.product_id 
               WHERE o.buyer_id = ? AND o.status = 'DELIVERED'""", 
            (user_id,)
        ) as cur:
            assets = await cur.fetchall()

    if not assets:
        return await message.reply("🛍 شما هنوز هیچ محصول تحویل‌شده‌ای در دارایی‌های خود ندارید.")

    for item in assets:
        caption = (
            f"📦 <b>{html.escape(item['title'])}</b>\n\n"
            f"📝 {html.escape(item['description'])}\n"
            f"🔑 کد خرید: <code>{item['order_code']}</code>\n"
            f"📅 تاریخ: {item['created_at']}"
        )
        if item['photo_file_id']:
            await message.reply_photo(photo=item['photo_file_id'], caption=caption, parse_mode="HTML")
        else:
            await message.reply(caption, parse_mode="HTML")

@shop_router.message(Command("track"))
async def track_order(message: Message, command: CommandObject):
    """پیگیری وضعیت سفارش: /track 1234567890"""
    if not command.args:
        return await message.reply("⚠️ لطفاً کد ۱۰ رقمی سفارش را وارد کنید:\nمثال: `/track 1234567890`", parse_mode="Markdown")

    code = command.args.strip()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT o.*, p.title 
               FROM orders o 
               JOIN products p ON o.product_id = p.product_id 
               WHERE o.order_code = ?""", 
            (code,)
        ) as cur:
            order = await cur.fetchone()

    if not order:
        return await message.reply("❌ سفارشی با این کد یافت نشد.")

    status_str = "⏳ در حال پردازش / ارسال" if order['status'] == 'PROCESSING' else "✅ تحویل داده شده"
    
    text = (
        f"🔍 <b>اطلاعات سفارش:</b>\n\n"
        f"🔹 <b>محصول:</b> {html.escape(order['title'])}\n"
        f"🔑 <b>کد ۱۰ رقمی:</b> <code>{order['order_code']}</code>\n"
        f"📌 <b>وضعیت:</b> {status_str}\n"
        f"💰 <b>مبلغ پرداختی:</b> ₳ {order['product_price']}\n"
        f"🚚 <b>هزینه پست:</b> ₳ {order['shipping_fee']}"
    )
    await message.reply(text, parse_mode="HTML")
