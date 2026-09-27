"""
Meme Court — a Telegram group bot that puts members on trial for bad takes.

Architecture
------------
- python-telegram-bot v20+ (async, ApplicationBuilder)
- In-memory state (see `active_trials` below) — swap for SQLite/Redis later,
  see the comment on that dict for exactly how.
- Pillow generates a "WANTED" mugshot PNG in memory (io.BytesIO), no disk writes.
- A 90-second asyncio background task tallies votes and delivers the verdict,
  optionally muting the defendant if the bot has admin rights in the chat.
"""

import io
import os
import random
import asyncio
import logging

from dotenv import load_dotenv  # local dev convenience: loads .env if present

load_dotenv()
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from PIL import Image, ImageDraw, ImageFont

from telegram import (
    Update,
    User,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut, NetworkError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("meme_court")

TRIAL_DURATION_SECONDS = 90
MUTE_DURATION_SECONDS = 180

# --------------------------------------------------------------------------- #
# STATE MANAGEMENT
#
# `active_trials` is a plain in-memory dict: chat_id -> Trial. It is lost on
# restart and does not work across multiple bot processes. That's fine for a
# single-instance deployment (e.g. one Render worker).
#
# To swap in SQLite:
#   - Create a `trials` table: chat_id INTEGER PRIMARY KEY, message_id, 
#     prosecutor_id, defendant_id, crime TEXT, votes TEXT (JSON blob), 
#     created_at TIMESTAMP.
#   - Replace `active_trials[chat_id] = trial` with an INSERT/UPDATE, and
#     `active_trials.pop(chat_id)` with a DELETE.
#   - Replace `active_trials.get(chat_id)` with a SELECT + json.loads(votes).
#
# To swap in Redis:
#   - Store each trial as a hash `trial:{chat_id}` (HSET) with an expiry
#     (EXPIRE) of TRIAL_DURATION_SECONDS, so crashed trials self-clean.
#   - Store votes as a Redis hash `trial:{chat_id}:votes` (user_id -> choice).
#   - This also lets you run the bot across multiple processes/webhooks.
# --------------------------------------------------------------------------- #


@dataclass
class Trial:
    chat_id: int
    message_id: int
    prosecutor_id: int
    prosecutor_name: str
    defendant_id: int
    defendant_name: str
    crime: str
    votes: Dict[int, str] = field(default_factory=dict)       # user_id -> "guilty" | "innocent"
    voter_names: Dict[int, str] = field(default_factory=dict)
    task: Optional[asyncio.Task] = None
    ended: bool = False


active_trials: Dict[int, Trial] = {}


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #

def escape_md(text: str) -> str:
    """Escape the handful of characters legacy Markdown parse mode cares about."""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


async def send_with_retry(func, *args, max_retries: int = 3, **kwargs):
    """Call a bot method, retrying once on RetryAfter (rate limit) or TimedOut."""
    last_exc = None
    for attempt in range(max_retries):
        try:
            return await func(*args, **kwargs)
        except RetryAfter as e:
            wait = e.retry_after + 1
            logger.warning("Rate limited, sleeping %.1fs (attempt %d)", wait, attempt + 1)
            await asyncio.sleep(wait)
            last_exc = e
        except TimedOut as e:
            logger.warning("Request timed out, retrying (attempt %d)", attempt + 1)
            await asyncio.sleep(2)
            last_exc = e
    # final attempt, let it raise if it fails
    return await func(*args, **kwargs) if last_exc else None


def build_keyboard(trial: Trial) -> InlineKeyboardMarkup:
    guilty_count = sum(1 for v in trial.votes.values() if v == "guilty")
    innocent_count = sum(1 for v in trial.votes.values() if v == "innocent")
    buttons = [
        [
            InlineKeyboardButton(
                f"🔨 GUILTY ({guilty_count})",
                callback_data=f"vote|guilty|{trial.chat_id}",
            ),
            InlineKeyboardButton(
                f"😇 INNOCENT ({innocent_count})",
                callback_data=f"vote|innocent|{trial.chat_id}",
            ),
        ]
    ]
    return InlineKeyboardMarkup(buttons)


# --------------------------------------------------------------------------- #
# Mugshot image generation (Pillow)
# --------------------------------------------------------------------------- #

def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """Try a few common DejaVu paths (present on most Linux hosts, incl. Render),
    falling back to Pillow's built-in bitmap font so this never crashes."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _wrap_text(text: str, font, max_width: int) -> list:
    words = text.split()
    lines, current = [], ""
    probe = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(probe)
    for word in words:
        trial_line = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), trial_line, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = trial_line
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _placeholder_avatar(size: int) -> Image.Image:
    img = Image.new("RGB", (size, size), (70, 70, 70))
    draw = ImageDraw.Draw(img)
    font = _load_font(size // 2)
    draw.text((size // 2, size // 2), "?", font=font, fill=(210, 210, 210), anchor="mm")
    return img


def generate_mugshot(defendant_name: str, crime: str, avatar_bytes: Optional[bytes]) -> io.BytesIO:
    """Build a dark/vintage 'court summons' PNG entirely in memory."""
    width, height = 800, 600
    bg = Image.new("RGB", (width, height), (24, 20, 16))
    draw = ImageDraw.Draw(bg)

    # cheap vintage vignette: subtle vertical shading, no external assets needed
    for x in range(0, width, 3):
        shade = random.randint(0, 12)
        draw.line([(x, 0), (x, height)], fill=(24 - shade, 20 - shade, 16 - shade))

    # header banner
    draw.rectangle([0, 0, width, 90], fill=(58, 14, 14))
    draw.text((width // 2, 45), "MEME COURT", font=_load_font(42),
               fill=(230, 210, 170), anchor="mm")

    # avatar (sepia-toned to sell the "mugshot" look)
    avatar_size = 260
    avatar_box = (width // 2 - avatar_size // 2, 115)
    if avatar_bytes:
        try:
            avatar_img = Image.open(io.BytesIO(avatar_bytes)).convert("RGB").resize(
                (avatar_size, avatar_size)
            )
        except Exception as e:
            logger.warning("Could not decode avatar image, using placeholder: %s", e)
            avatar_img = _placeholder_avatar(avatar_size)
    else:
        avatar_img = _placeholder_avatar(avatar_size)

    avatar_img = avatar_img.convert("L").convert("RGB")  # grayscale -> mugshot vibe
    bg.paste(avatar_img, avatar_box)
    draw.rectangle(
        [
            avatar_box[0] - 4, avatar_box[1] - 4,
            avatar_box[0] + avatar_size + 4, avatar_box[1] + avatar_size + 4,
        ],
        outline=(230, 210, 170), width=4,
    )

    draw.text(
        (width // 2, avatar_box[1] + avatar_size + 40),
        defendant_name.upper(),
        font=_load_font(30), fill=(255, 255, 255), anchor="mm",
    )

    crime_font = _load_font(22)
    y = avatar_box[1] + avatar_size + 85
    for line in _wrap_text(f"WANTED FOR: {crime.upper()}", crime_font, width - 80):
        draw.text((width // 2, y), line, font=crime_font, fill=(255, 205, 110), anchor="mm")
        y += 30

    buf = io.BytesIO()
    buf.name = "mugshot.png"
    bg.save(buf, format="PNG")
    buf.seek(0)
    return buf


async def fetch_avatar_bytes(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> Optional[bytes]:
    """Return the user's highest-res profile photo as raw bytes, or None if
    they have no photo / it can't be fetched (private settings, deleted, etc.)."""
    try:
        photos = await context.bot.get_user_profile_photos(user_id, limit=1)
    except (BadRequest, Forbidden) as e:
        logger.info("Could not read profile photos for %s: %s", user_id, e)
        return None

    if not photos.photos:
        return None

    try:
        file_id = photos.photos[0][-1].file_id
        tg_file = await context.bot.get_file(file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(out=buf)
        buf.seek(0)
        return buf.read()
    except Exception as e:
        logger.warning("Failed downloading avatar for %s: %s", user_id, e)
        return None


# --------------------------------------------------------------------------- #
# Command: /indict
# --------------------------------------------------------------------------- #

async def indict(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat_id = update.effective_chat.id

    if chat_id in active_trials:
        await message.reply_text(
            "⚖️ Order in the court! A trial is already underway here. Wait for the verdict."
        )
        return

    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply_text(
            "You must *reply* to the accused's message to file an indictment.\n"
            "Usage: reply to a message with `/indict being wrong about pineapple pizza`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    prosecutor: User = message.from_user
    defendant: User = message.reply_to_message.from_user

    if defendant.is_bot:
        await message.reply_text("You cannot put a bot on trial. We have rights too.")
        return

    if defendant.id == prosecutor.id:
        await message.reply_text(
            "Self-indictment denied. Nice try, but the court isn't your therapist."
        )
        return

    crime = " ".join(context.args).strip() if context.args else "Unspecified crimes against comedy"
    defendant_name = defendant.first_name or defendant.username or "Defendant"
    prosecutor_name = prosecutor.first_name or prosecutor.username or "Prosecutor"

    caption = (
        "🏛️ *MEME COURT IS NOW IN SESSION* 🏛️\n\n"
        f"*Prosecutor:* {escape_md(prosecutor_name)}\n"
        f"*Defendant:* {escape_md(defendant_name)}\n"
        f"*Charge:* {escape_md(crime)}\n\n"
        f"You have *{TRIAL_DURATION_SECONDS} seconds* to cast your verdict.\n"
        "_The defendant may not vote in their own trial._"
    )

    avatar_bytes = await fetch_avatar_bytes(context, defendant.id)

    try:
        mugshot = generate_mugshot(defendant_name, crime, avatar_bytes)
    except Exception as e:
        logger.error("Mugshot generation failed, falling back to text-only trial: %s", e)
        mugshot = None

    trial = Trial(
        chat_id=chat_id,
        message_id=0,
        prosecutor_id=prosecutor.id,
        prosecutor_name=prosecutor_name,
        defendant_id=defendant.id,
        defendant_name=defendant_name,
        crime=crime,
    )
    keyboard = build_keyboard(trial)

    try:
        if mugshot is not None:
            sent = await send_with_retry(
                message.reply_photo,
                photo=mugshot,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard,
            )
        else:
            sent = await send_with_retry(
                message.reply_text,
                text=caption,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard,
            )
    except (BadRequest, Forbidden, TimedOut, NetworkError) as e:
        logger.error("Failed to post trial message: %s", e)
        await message.reply_text(
            "The court reporter fainted. Could not start the trial (Telegram API error)."
        )
        return

    trial.message_id = sent.message_id
    active_trials[chat_id] = trial
    trial.task = asyncio.create_task(run_trial_timer(chat_id, context))


# --------------------------------------------------------------------------- #
# Voting
# --------------------------------------------------------------------------- #

async def handle_vote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    parts = (query.data or "").split("|")

    if len(parts) != 3:
        await query.answer("Malformed ballot.", show_alert=True)
        return

    _, choice, chat_id_str = parts
    try:
        chat_id = int(chat_id_str)
    except ValueError:
        await query.answer("Malformed ballot.", show_alert=True)
        return

    trial = active_trials.get(chat_id)
    if not trial or trial.ended or query.message.message_id != trial.message_id:
        await query.answer("This trial has already concluded.", show_alert=True)
        return

    if user.id == trial.defendant_id:
        await query.answer("The defendant cannot vote in their own trial!", show_alert=True)
        return

    if trial.votes.get(user.id) == choice:
        await query.answer("You already cast that vote.")
        return

    trial.votes[user.id] = choice
    trial.voter_names[user.id] = user.first_name or user.username or "Juror"

    try:
        await query.edit_message_reply_markup(reply_markup=build_keyboard(trial))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("Failed to refresh vote counts: %s", e)
    except (TimedOut, NetworkError) as e:
        logger.warning("Network hiccup updating vote counts: %s", e)

    await query.answer(f"Vote for {choice.upper()} recorded.")


# --------------------------------------------------------------------------- #
# Verdict & enforcement
# --------------------------------------------------------------------------- #

async def try_mute(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    """Attempt to mute the defendant. Returns True on success, False if the
    bot isn't an admin / lacks restrict rights (never raises)."""
    until = datetime.now(timezone.utc) + timedelta(seconds=MUTE_DURATION_SECONDS)
    permissions = ChatPermissions(can_send_messages=False)
    try:
        await context.bot.restrict_chat_member(
            chat_id=chat_id, user_id=user_id, permissions=permissions, until_date=until
        )
        return True
    except Forbidden:
        logger.info("Bot lacks admin/restrict rights in chat %s; falling back to public shaming.", chat_id)
        return False
    except BadRequest as e:
        logger.warning("Could not restrict user %s in chat %s: %s", user_id, chat_id, e)
        return False


async def run_trial_timer(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await asyncio.sleep(TRIAL_DURATION_SECONDS)
    except asyncio.CancelledError:
        return
    await conclude_trial(chat_id, context)


async def conclude_trial(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    trial = active_trials.pop(chat_id, None)
    if not trial or trial.ended:
        return
    trial.ended = True

    guilty_count = sum(1 for v in trial.votes.values() if v == "guilty")
    innocent_count = sum(1 for v in trial.votes.values() if v == "innocent")

    if guilty_count > innocent_count:
        verdict = "GUILTY"
    elif innocent_count > guilty_count:
        verdict = "INNOCENT"
    else:
        verdict = "HUNG JURY"

    result_text = (
        "⚖️ *THE COURT HAS REACHED A VERDICT* ⚖️\n\n"
        f"*Defendant:* {escape_md(trial.defendant_name)}\n"
        f"*Charge:* {escape_md(trial.crime)}\n"
        f"*Votes:* 🔨 {guilty_count}  vs  😇 {innocent_count}\n\n"
        f"*VERDICT: {verdict}*"
    )

    # Freeze the trial message so nobody can keep voting on a decided case.
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=trial.message_id, reply_markup=None
        )
    except BadRequest:
        pass
    except (TimedOut, NetworkError, Forbidden) as e:
        logger.warning("Could not clear keyboard for chat %s: %s", chat_id, e)

    enforcement_note = ""
    if verdict == "GUILTY":
        muted = await try_mute(context, chat_id, trial.defendant_id)
        if muted:
            enforcement_note = (
                f"\n\n🔇 The defendant has been silenced for "
                f"{MUTE_DURATION_SECONDS // 60} minute(s)."
            )
        else:
            enforcement_note = (
                "\n\n📢 *PUBLIC SHAMING SENTENCE*: the bot isn't an admin here, so it "
                "can't mute anyone — let it be known across the group that justice was "
                "served in spirit, if not in silence."
            )

    try:
        await send_with_retry(
            context.bot.send_message,
            chat_id=chat_id,
            text=result_text + enforcement_note,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error("Failed to deliver verdict for chat %s: %s", chat_id, e)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

async def post_init(application) -> None:
    logger.info("Meme Court is now in session.")


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN environment variable is not set. "
            "Export it locally or set it in Render's dashboard — never hardcode it."
        )

    app = ApplicationBuilder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("indict", indict))
    app.add_handler(CallbackQueryHandler(handle_vote, pattern=r"^vote\|"))

    logger.info("Starting Meme Court bot (long polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
