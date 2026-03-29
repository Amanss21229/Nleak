import os
import asyncio
import logging
import asyncpg
from aiohttp import web
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

ADMIN_IDS = {8162524828}

MOBILE, ALT_MOBILE, GMAIL, ALT_GMAIL = range(4)


def is_admin(update: Update) -> bool:
    """Returns True if the sender is a designated admin or the message is from the admin group."""
    user = update.effective_user
    if user and user.id in ADMIN_IDS:
        return True
    if update.effective_chat and update.effective_chat.id == GROUP_CHAT_ID:
        return True
    return False

db_pool = None


async def get_db():
    global db_pool
    if db_pool is None:
        db_pool = await asyncpg.create_pool(DATABASE_URL)
    return db_pool


async def init_db():
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_users (
                user_id BIGINT PRIMARY KEY,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                last_name TEXT DEFAULT '',
                mobile TEXT DEFAULT '',
                alt_mobile TEXT DEFAULT '',
                gmail TEXT DEFAULT '',
                alt_gmail TEXT DEFAULT '',
                referred_by BIGINT,
                is_verified BOOLEAN DEFAULT FALSE,
                verified_at TIMESTAMPTZ,
                data_submitted BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id SERIAL PRIMARY KEY,
                referrer_id BIGINT NOT NULL REFERENCES bot_users(user_id),
                referred_id BIGINT NOT NULL REFERENCES bot_users(user_id),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (referrer_id, referred_id)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS message_map (
                group_msg_id BIGINT PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES bot_users(user_id),
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
    logger.info("Database tables ready.")


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


async def save_message_map(group_msg_id: int, user_id: int):
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO message_map (group_msg_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            group_msg_id, user_id
        )


async def get_user_id_for_group_msg(group_msg_id: int):
    pool = await get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id FROM message_map WHERE group_msg_id = $1", group_msg_id
        )
        return row["user_id"] if row else None


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
                    "Shukriya! Aapke *3 unique referrals* complete ho gaye hain.\n\n"
                    "✅ *Ab aap verified hain!* Hum aapko jald se jald "
                    "NEET 2026 Question Paper ki PDF bhejne ki koshish karenge.\n\n"
                    "📢 *Yaad rahe:* Jitna zyada aap refer karenge, utna *pehle* aapko paper send kiya jaayega!\n\n"
                    "Referral karte rahiye aur apna chance badhaiye! 🚀"
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Failed to send verification message to {referred_by}: {e}")


async def forward_user_message_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forward every user message to the group with sender info label, then map the forwarded msg."""
    user = update.effective_user
    message = update.effective_message

    if not message:
        return

    try:
        full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
        username_str = f"@{user.username}" if user.username else "(no username)"
        label_text = (
            f"📨 *Message from user:*\n"
            f"👤 *Name:* {full_name}\n"
            f"🔖 *Username:* {username_str}\n"
            f"🆔 *User ID:* `{user.id}`"
        )

        label_msg = await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=label_text,
            parse_mode="Markdown"
        )

        forwarded = await message.forward(chat_id=GROUP_CHAT_ID)

        await save_message_map(label_msg.message_id, user.id)
        await save_message_map(forwarded.message_id, user.id)

    except Exception as e:
        logger.error(f"Failed to forward user message to group: {e}")


async def handle_group_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """When admin replies in the group, forward that reply to the original user."""
    message = update.effective_message

    if not message or not message.reply_to_message:
        return

    if update.effective_chat.id != GROUP_CHAT_ID:
        return

    replied_to_msg_id = message.reply_to_message.message_id
    target_user_id = await get_user_id_for_group_msg(replied_to_msg_id)

    if not target_user_id:
        return

    try:
        if message.text:
            await context.bot.send_message(chat_id=target_user_id, text=message.text)
        elif message.photo:
            await context.bot.send_photo(
                chat_id=target_user_id,
                photo=message.photo[-1].file_id,
                caption=message.caption or ""
            )
        elif message.video:
            await context.bot.send_video(
                chat_id=target_user_id,
                video=message.video.file_id,
                caption=message.caption or ""
            )
        elif message.document:
            await context.bot.send_document(
                chat_id=target_user_id,
                document=message.document.file_id,
                caption=message.caption or ""
            )
        elif message.audio:
            await context.bot.send_audio(
                chat_id=target_user_id,
                audio=message.audio.file_id,
                caption=message.caption or ""
            )
        elif message.voice:
            await context.bot.send_voice(
                chat_id=target_user_id,
                voice=message.voice.file_id,
                caption=message.caption or ""
            )
        elif message.sticker:
            await context.bot.send_sticker(
                chat_id=target_user_id,
                sticker=message.sticker.file_id
            )
        elif message.animation:
            await context.bot.send_animation(
                chat_id=target_user_id,
                animation=message.animation.file_id,
                caption=message.caption or ""
            )
        elif message.video_note:
            await context.bot.send_video_note(
                chat_id=target_user_id,
                video_note=message.video_note.file_id
            )
        elif message.poll:
            poll = message.poll
            options = [opt.text for opt in poll.options]
            await context.bot.send_poll(
                chat_id=target_user_id,
                question=poll.question,
                options=options,
                is_anonymous=poll.is_anonymous,
                type=poll.type,
                allows_multiple_answers=poll.allows_multiple_answers
            )
        elif message.contact:
            await context.bot.send_contact(
                chat_id=target_user_id,
                phone_number=message.contact.phone_number,
                first_name=message.contact.first_name,
                last_name=message.contact.last_name or ""
            )
        elif message.location:
            await context.bot.send_location(
                chat_id=target_user_id,
                latitude=message.location.latitude,
                longitude=message.location.longitude
            )
        else:
            await message.forward(chat_id=target_user_id)

    except Exception as e:
        logger.error(f"Failed to send group reply to user {target_user_id}: {e}")


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
        "📅 *Mai ek Neet Aspirant hu, but jb maina dekha ki har saal paper leak ho jaata hai, to mujhe bura lga, isliyemaine ye bot banaya hai. so agr is saal v paper leak hua, to ham aapko e8ther mobile sms ya gmail pe send kar denge. Because Mobioe sms or gmail sabse jaada safe hai. agr mai aapko telegram pe, telegram group pe ya whatsapp pe paper send karunga, to ye mere liye risky ho sakta hai. so paper either sms or gmail se hi send hoga. agr aap interested ho to neeche ke furthur step padho*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ *PDF sirf VERIFIED users ko milegi!*\n\n"
        "✅ Verified hone ke liye *minimum 3 unique users* ko refer karna *compulsory* hai.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📩 *Abhi kya karein?*\n"
        "1️⃣ /fill pe click karke apni details bhejein\n"
        "2️⃣ Apna referral link share karein — *3 log join karein*\n"
        "3️⃣ Verified hon aur 2 May ko PDF paayen! 🎯\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔗 *Aapka Referral Link:*\n"
        f"`{referral_link}`\n\n"
        "👥 *Jitna zyada refer karoge, utna jaldi paper milega!* 🚀"
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
        "📝 *Details Form — Step 1/4*\n\n"
        "⚠️ *Yaad rahe:* PDF sirf *verified users* ko milegi.\n"
        "Verified hone ke liye *3 referrals compulsory* hain!\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
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

    await update.message.reply_text(
        "✅ Mobile number save ho gaya!\n\n"
        "📝 *Step 2/4*\n\n"
        "📱 *Alternative Mobile Number* (optional):\n"
        "Koi doosra number bhejein ya /skip karein:",
        parse_mode="Markdown"
    )
    return ALT_MOBILE


async def received_alt_mobile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

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

    await update.message.reply_text(
        "✅ Gmail save ho gaya!\n\n"
        "📝 *Step 4/4*\n\n"
        "📧 *Alternative Gmail* (optional):\n"
        "Koi doosra Gmail bhejein ya /skip karein:",
        parse_mode="Markdown"
    )
    return ALT_GMAIL


async def received_alt_gmail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()

    if "@" not in text or "." not in text:
        await update.message.reply_text(
            "❌ *Invalid email!* Kripya valid Gmail bhejein ya /skip karein.",
            parse_mode="Markdown"
        )
        return ALT_GMAIL
    context.user_data["alt_gmail"] = text

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

    username_display = f"@{user.username}" if user.username else "N/A"
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
        data_msg = await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=forward_text,
            parse_mode="Markdown"
        )
        await save_message_map(data_msg.message_id, user.id)
    except Exception as e:
        logger.error(f"Failed to forward data to group: {e}")

    ref_count = await get_referral_count(user.id)
    refs_needed = max(0, 3 - ref_count)

    thank_you_text = (
        "🎉 *Shukriya! Aapki details successfully save ho gayi!*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ *Important: PDF sirf VERIFIED users ko milegi!*\n\n"
        "✅ Verified hone ke liye *3 referrals compulsory* hain.\n"
        "Bina 3 referrals ke PDF nahi milegi!\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📅 Verified users ko *2 May 2026 raat 10 baje* NEET 2026 Question Paper ki PDF bhejne ki koshish ki jaayegi.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔗 *Abhi share karein apna Referral Link:*\n\n"
        f"`{referral_link}`\n\n"
        f"📊 *Aapki current referrals:* {ref_count}/3\n"
    )

    if refs_needed > 0:
        thank_you_text += (
            f"\n⚡ *{refs_needed} aur referral(s)* chahiye — abhi share karo!\n\n"
        )
    else:
        thank_you_text += "\n✅ *Aapke 3 referrals complete hain! Verification processing...*\n\n"

    thank_you_text += (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💡 *Jitna zyada refer karoge, utna pehle paper milega!* 🚀"
    )

    await update.message.reply_text(thank_you_text, parse_mode="Markdown")

    if ref_count >= 3:
        already_verified = await is_verified(user.id)
        if not already_verified:
            await check_and_notify_referrer(context, user.id)


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
    refs_needed = max(0, 3 - ref_count)

    status_text = (
        f"📊 *Aapka Referral Status*\n\n"
        f"👥 *Total Referrals:* {ref_count}/3\n"
        f"✅ *Verified:* {'Haan! ✅' if verified else f'Nahi ❌ ({refs_needed} aur chahiye)'}\n\n"
        f"⚠️ *Yaad rahe: PDF sirf verified users ko milegi!*\n"
        f"Minimum *3 referrals compulsory* hain.\n\n"
        f"🔗 *Aapka Referral Link:*\n`{referral_link}`\n\n"
        f"💡 Jitna zyada refer karoge, utna jaldi aur pehle paper milega! 🚀"
    )

    await update.message.reply_text(status_text, parse_mode="Markdown")


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /stats — complete bot statistics. Admin only."""
    if not is_admin(update):
        await update.message.reply_text("⛔ You are not authorized to use this command.")
        return

    pool = await get_db()
    async with pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM bot_users")
        verified_users = await conn.fetchval("SELECT COUNT(*) FROM bot_users WHERE is_verified = TRUE")
        submitted_users = await conn.fetchval("SELECT COUNT(*) FROM bot_users WHERE data_submitted = TRUE")
        total_referrals = await conn.fetchval("SELECT COUNT(*) FROM referrals")
        users_with_referrals = await conn.fetchval("SELECT COUNT(DISTINCT referrer_id) FROM referrals")
        new_today = await conn.fetchval(
            "SELECT COUNT(*) FROM bot_users WHERE created_at >= NOW() - INTERVAL '24 hours'"
        )
        new_this_week = await conn.fetchval(
            "SELECT COUNT(*) FROM bot_users WHERE created_at >= NOW() - INTERVAL '7 days'"
        )
        top_referrers = await conn.fetch(
            """
            SELECT u.first_name, u.username, u.user_id, COUNT(r.referred_id) AS ref_count
            FROM referrals r
            JOIN bot_users u ON u.user_id = r.referrer_id
            GROUP BY u.user_id, u.first_name, u.username
            ORDER BY ref_count DESC
            LIMIT 5
            """
        )

    unverified = total_users - verified_users
    not_submitted = total_users - submitted_users

    stats_text = (
        "📊 *BOT STATISTICS*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 *Total Users:* {total_users}\n"
        f"🆕 *New Today:* {new_today}\n"
        f"📅 *New This Week:* {new_this_week}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ *Verified Users:* {verified_users}\n"
        f"❌ *Unverified Users:* {unverified}\n"
        f"📝 *Form Submitted:* {submitted_users}\n"
        f"⏳ *Not Submitted:* {not_submitted}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 *Total Referrals Made:* {total_referrals}\n"
        f"👤 *Users Who Referred:* {users_with_referrals}\n\n"
    )

    if top_referrers:
        stats_text += "🏆 *Top 5 Referrers:*\n"
        for i, row in enumerate(top_referrers, 1):
            name = row["first_name"] or "Unknown"
            uname = f"@{row['username']}" if row["username"] else f"ID:{row['user_id']}"
            stats_text += f"{i}. {name} ({uname}) — {row['ref_count']} referrals\n"

    await update.message.reply_text(stats_text, parse_mode="Markdown")


async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /broadcast (reply to any message) — sends that message to all bot users."""
    if not is_admin(update):
        await update.message.reply_text("⛔ You are not authorized to use this command.")
        return

    message = update.effective_message

    if not message.reply_to_message:
        await message.reply_text(
            "⚠️ *Usage:* Kisi bhi message ko *reply* karke `/broadcast` likhein.\n"
            "Woh message sabhi users ko bheja jaayega.\n\n"
            "_Supports: text, photo, video, audio, document, sticker, GIF, voice, poll, contact, location, video note._",
            parse_mode="Markdown"
        )
        return

    broadcast_msg = message.reply_to_message

    pool = await get_db()
    async with pool.acquire() as conn:
        user_ids = await conn.fetch("SELECT user_id FROM bot_users")

    total = len(user_ids)
    success = 0
    failed = 0

    status_msg = await message.reply_text(
        f"📢 *Broadcasting to {total} users...*\n_Please wait..._",
        parse_mode="Markdown"
    )

    for row in user_ids:
        uid = row["user_id"]
        try:
            await broadcast_msg.copy(chat_id=uid)
            success += 1
        except Exception as e:
            logger.warning(f"Broadcast failed for user {uid}: {e}")
            failed += 1
        await asyncio.sleep(0.05)

    await status_msg.edit_text(
        f"✅ *Broadcast Complete!*\n\n"
        f"📤 *Sent successfully:* {success}/{total}\n"
        f"❌ *Failed:* {failed}",
        parse_mode="Markdown"
    )


async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all non-command messages from private chats — forward them to the group."""
    if update.effective_chat.id == GROUP_CHAT_ID:
        return

    user = update.effective_user
    if not user:
        return

    await ensure_user(
        user_id=user.id,
        username=user.username or "",
        first_name=user.first_name or "",
        last_name=user.last_name or ""
    )

    await forward_user_message_to_group(update, context)


async def health_handler(request):
    return web.Response(text="OK")


async def run_health_server():
    port = int(os.environ.get("BOT_HEALTH_PORT", 8082))
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health server running on port {port}")


async def run_bot():
    app = Application.builder().token(BOT_TOKEN).build()

    await init_db()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("fill", fill_start)],
        states={
            MOBILE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_mobile)],
            ALT_MOBILE: [
                CommandHandler("skip", handle_skip_alt_mobile),
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_alt_mobile),
            ],
            GMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_gmail)],
            ALT_GMAIL: [
                CommandHandler("skip", handle_skip_alt_gmail),
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_alt_gmail),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("status", referral_status))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CommandHandler("broadcast", admin_broadcast))

    group_filter = filters.Chat(chat_id=GROUP_CHAT_ID) & filters.REPLY
    app.add_handler(MessageHandler(group_filter, handle_group_reply))

    private_filter = filters.ChatType.PRIVATE & ~filters.COMMAND
    app.add_handler(MessageHandler(private_filter, handle_user_message))

    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
        logger.info("Bot started, polling for updates...")
        await asyncio.Event().wait()
        await app.updater.stop()
        await app.stop()


def main():
    async def run_all():
        await run_health_server()
        await run_bot()

    logger.info("Bot starting...")
    asyncio.run(run_all())


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
