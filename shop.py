from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
import aiosqlite
import html

# ساخت روتور اختصاصی بخش فروشگاه
shop_router = Router()
DB_PATH = "atr_bank.db"

# لیست آیتم‌های نمونه فروشگاه (می‌توانید آیتم‌ها و قیمت‌های دلخواه بگذارید)
SHOP_ITEMS = {
    "item_1": {"title": "اشتراک ۱ ماهه", "price": 500},
    "item_2": {"title": "اکانت ویژه", "price": 1200},
}

@shop_router.message(Command("shop"))
@shop_router.message(F.text == "فروشگاه")
async def show_shop(message: Message):
    """نمایش منوی فروشگاه"""
    text = "🛒 <b>فروشگاه آتر بانک</b>\n\nلطفاً محصول مورد نظر خود را انتخاب کنید:\n\n"
    buttons = []
    
    for code, item in SHOP_ITEMS.items():
        text += f"🔹 <b>{item['title']}</b> — قیمت: <code>₳ {item['price']}</code>\n"
        buttons.append([InlineKeyboardButton(text=f"خرید {item['title']}", callback_data=f"buy_{code}")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.reply(text, reply_markup=kb, parse_mode="HTML")


@shop_router.callback_query(F.data.startswith("buy_"))
async def process_purchase(callback: CallbackQuery):
    """ثبت خرید، کسر موجودی از atr_bank.db و ذخیره لوگ"""
    item_code = callback.data.split("_", 1)[1]
    
    if item_code not in SHOP_ITEMS:
        return await callback.answer("❌ محصول پیدا نشد.", show_alert=True)
        
    item = SHOP_ITEMS[item_code]
    user_id = callback.from_user.id
    price = item["price"]

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        
        # ۱. بررسی موجودی و عدم فریز بودن
        async with db.execute("SELECT balance, is_frozen FROM users WHERE user_id = ?", (user_id,)) as cur:
            user = await cur.fetchone()
            
        if not user:
            return await callback.answer("❌ ابتدا در ربات /start را بزنید.", show_alert=True)
            
        if user["is_frozen"]:
            return await callback.answer("❄️ حساب شما فریز است و امکان خرید ندارید.", show_alert=True)
            
        if user["balance"] < price:
            return await callback.answer(f"❌ موجودی کافی نیست! (موجودی نیاز: ₳ {price})", show_alert=True)

        # ۲. کسر موجودی و ثبت تراکنش
        await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (price, user_id))
        await db.execute(
            """
            INSERT INTO audit_logs (tx_id, timestamp, from_user, to_user, amount, reason, status)
            VALUES (?, DATETIME('now'), ?, 0, ?, ?, 'SUCCESS')
            """,
            (f"SHOP-{user_id}-{price}", user_id, price, f"خرید {item['title']}")
        )
        await db.commit()

    await callback.answer("✅ خرید موفقیت‌آمیز بود!", show_alert=True)
    await callback.message.edit_text(
        f"🎉 <b>خرید با موفقیت انجام شد!</b>\n\n"
        f"📦 محصول: <b>{html.escape(item['title'])}</b>\n"
        f"💰 مبلغ کسر شده: <code>₳ {price}</code>",
        parse_mode="HTML"
    )
