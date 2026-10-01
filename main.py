"""
Meme Court — a Telegram group bot that puts members on trial for bad takes.

Flow
----
1. /indict (reply to a message) opens a 90-second JURY LOBBY.
2. People tap "JOIN THE JURY". The defendant can't join.
3. After 90s: if fewer than MIN_JURORS joined, the lobby closes and the case
   is dismissed. Otherwise voting opens for another 90s (jurors only).
4. The defendant may shout OBJECTION! once (30% chance it's sustained and a
   guilty vote is struck).
5. Verdict is delivered, optionally muting the defendant (longer for landslides)
   if the bot is admin. Results are saved to an in-memory rap sheet.

Extras
------
- /rapsheet  -> shows a user's record in this chat (reply to someone, or self)
- Any unknown command gets a random judge quip.
- /start is deliberately ignored (no reply at all).

Architecture
------------
- python-telegram-bot v20+ (async, ApplicationBuilder)
- In-memory state (`active_trials`, `rap_sheets`) — see the comment below for
  how to swap in SQLite/Redis.
- Pillow generates a "WANTED" mugshot PNG in memory (io.BytesIO).
"""

import io
import os
import random
import asyncio
import logging

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
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
    MessageHandler,
    ContextTypes,
    filters,
)

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("meme_court")

LOBBY_DURATION_SECONDS = 90     # time to join before the lobby closes
TRIAL_DURATION_SECONDS = 90     # voting time once the trial starts
MIN_JURORS = 3                  # lobby closes if fewer people join
MUTE_BASE_SECONDS = 180         # mute for a 1-vote margin
MUTE_STEP_SECONDS = 90          # extra mute per additional vote of margin
MUTE_MAX_SECONDS = 600          # hard cap (10 minutes)
MAX_COMMAND_AGE_SECONDS = 60    # commands older than this are ignored as stale
OBJECTION_SUSTAIN_CHANCE = 0.30 # chance the judge sustains an objection
RANDOM_REPLY_CHANCE = 1.0       # 1.0 = always quip on unknown commands; 0.5 = half the time

# --------------------------------------------------------------------------- #
# Flavor text
# --------------------------------------------------------------------------- #

QUIPS = [
    "⚖️ The court has no idea what that command means, and neither do I.",
    "🔨 *BANG BANG.* Order! ...I forgot why I did that.",
    "🧑‍⚖️ Objection! ...to whatever you just typed.",
    "📜 The bailiff has read your command and chose to laugh.",
    "🏛️ This is a court, not a help desk. Try /indict.",
    "🥱 The judge is currently on a snack break. Please hold.",
    "👀 The court is watching. The court is always watching.",
    "🕵️ Noted. This has been added to your file. Don't ask what file.",
    "🍿 The jury is bored. Indict someone already.",
    "🐐 Sustained. Overruled. Whatever. Moving on.",
    "🤨 The court finds that command... suspicious.",
    "🧂 The judge has seen your type of command before. Salty.",
    "📞 Your lawyer is not picking up. Try again later.",
    "🪑 Please remain seated. Nobody asked you anything.",
    "🎭 Dramatic gavel noises. The command is denied.",
]

GUILTY_LINES = [
    "The court has seen enough. Take them away.",
    "The evidence was cringe, the verdict is clear.",
    "The jury has spoken, and it was not kind.",
    "Justice has been served, with a side of ratio.",
]
INNOCENT_LINES = [
    "The take was bad, but not criminal. Walk free.",
    "Acquitted. Reputation: damaged. Freedom: intact.",
    "The jury felt merciful. Do not push your luck.",
    "Case closed. The defendant lives to post again.",
]
HUNG_LINES = [
    "The jury is hopelessly split. Everybody go home.",
    "A perfect tie. The court is mildly annoyed.",
    "No verdict. The gavel is confused.",
]
DISMISS_LINES = [
    "Lucky them. 🍀",
    "Nobody cared enough to show up. Brutal. 💀",
    "The jury was out getting snacks. 🍕",
]
COMMUNITY_SERVICE = [
    "Must post a cat photo within the hour. 🐱",
    "Must admit 'I was wrong' in the next message they send. 🙇",
    "Must compliment the prosecutor. Sincerely. 🤝",
    "Must reveal their favorite guilty-pleasure song. 🎵",
    "Must use only emojis for the next 5 messages. 😶",
    "Must apologise to pineapple pizza. 🍍",
]
OBJECTION_SUSTAINED = [
    "🧑‍⚖️ *SUSTAINED!* The judge strikes one guilty vote from the record.",
    "🧑‍⚖️ *SUSTAINED!* That was a good point. One guilty vote vanishes.",
]
OBJECTION_OVERRULED = [
    "🧑‍⚖️ *OVERRULED!* Sit down.",
    "🧑‍⚖️ *OVERRULED!* Nice try, counselor.",
    "🧑‍⚖️ *OVERRULED!* The judge didn't even look up.",
]

# --------------------------------------------------------------------------- #
# STATE MANAGEMENT
#
# `active_trials` is a plain in-memory dict: chat_id -> Trial. It is lost on
# restart and does not work across multiple bot processes. That's fine for a
# single-instance deployment (e.g. one Render worker).
#
# `rap_sheets` is chat_id -> user_id -> {"name", "guilty", "innocent", "hung"}.
# Same caveat: lost on restart. Persist it the same way as trials.
#
# To swap in SQLite:
#   - Create a `trials` table: chat_id INTEGER PRIMARY KEY, message_id,
#     prosecutor_id, defendant_id, crime TEXT, phase TEXT,
#     jurors TEXT (JSON), votes TEXT (JSON), created_at TIMESTAMP.
#   - Create a `rap_sheets` table: chat_id, user_id, guilty, innocent, hung.
#   - Replace `active_trials[chat_id] = trial` with an INSERT/UPDATE, and
#     `active_trials.pop(chat_id)` with a DELETE.
#   - Replace `active_trials.get(chat_id)` with a SELECT + json.loads(...).
#
# To swap in Redis:
#   - Store each trial as a hash `trial:{chat_id}` (HSET) with an expiry
#     (EXPIRE) so crashed trials self-clean.
#   - Store jurors/votes as Redis hashes (user_id -> name / choice).
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
    jurors: Dict[int, str] = field(default_factory=dict)      # user_id -> name
    phase: str = "lobby"                                      # "lobby" | "voting"
    has_photo: bool = True
    objection_used: bool = False
    task: Optional[asyncio.Task] = None
    ended: bool = False


active_trials: Dict[int, Trial] = {}
rap_sheets: Dict[int, Dict[int, dict]] = {}


def record_result(chat_id: int, user_id: int, name: str, outcome: str) -> None:
    """outcome: 'guilty' | 'innocent' | 'hung'"""
    sheet = rap_sheets.setdefault(chat_id, {}).setdefault(
        user_id, {"name": name, "guilty": 0, "innocent": 0, "hung": 0}
    )
    sheet["name"] = name
    sheet[outcome] += 1


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #

def escape_md(text: str) -> str:
    """Escape the handful of characters legacy Markdown parse mode cares about."""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


async def send_with_retry(func, *args, max_retries: int = 3, **kwargs):
    """Call a bot method, retrying on RetryAfter (rate limit) or TimedOut."""
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


def build_lobby_keyboard(trial: Trial) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"🙋 JOIN THE JURY ({len(trial.jurors)}/{MIN_JURORS} needed)",
            callback_data=f"join|{trial.chat_id}",
        )
    ]])


def build_keyboard(trial: Trial) -> InlineKeyboardMarkup:
    guilty_count = sum(1 for v in trial.votes.values() if v == "guilty")
    innocent_count = sum(1 for v in trial.votes.values() if v == "innocent")
    rows = [
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
    if not trial.objection_used:
        rows.append([
            InlineKeyboardButton(
                "🗣️ OBJECTION! (defendant only)",
                callback_data=f"obj|{trial.chat_id}",
            )
        ])
    return InlineKeyboardMarkup(rows)


def lobby_caption(trial: Trial) -> str:
    return (
        "🏛️ *MEME COURT IS OPENING* 🏛️\n\n"
        f"*Prosecutor:* {escape_md(trial.prosecutor_name)}\n"
        f"*Defendant:* {escape_md(trial.defendant_name)}\n"
        f"*Charge:* {escape_md(trial.crime)}\n\n"
        f"Tap *JOIN THE JURY* within *{LOBBY_DURATION_SECONDS} seconds*.\n"
        f"At least *{MIN_JURORS} jurors* are needed or the lobby closes.\n"
        "_The defendant may not join the jury._"
    )


def voting_caption(trial: Trial) -> str:
    return (
        "🏛️ *MEME COURT IS NOW IN SESSION* 🏛️\n\n"
        f"*Prosecutor:* {escape_md(trial.prosecutor_name)}\n"
        f"*Defendant:* {escape_md(trial.defendant_name)}\n"
        f"*Charge:* {escape_md(trial.crime)}\n"
        f"*Jurors:* {len(trial.jurors)}\n\n"
        f"Jurors, you have *{TRIAL_DURATION_SECONDS} seconds* to cast your verdict.\n"
        "_The defendant has one OBJECTION. Use it wisely._"
    )


async def edit_trial_message(context, trial: Trial, text: str, markup) -> None:
    """Edit the trial post whether it's a photo (caption) or plain text."""
    try:
        if trial.has_photo:
            await context.bot.edit_message_caption(
                chat_id=trial.chat_id, message_id=trial.message_id,
                caption=text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup,
            )
        else:
            await context.bot.edit_message_text(
                chat_id=trial.chat_id, message_id=trial.message_id,
                text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup,
            )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning("Failed to edit trial message in %s: %s", trial.chat_id, e)
    except (TimedOut, NetworkError, Forbidden) as e:
        logger.warning("Could not edit trial message in %s: %s", trial.chat_id, e)


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

    # avatar (grayscale to sell the "mugshot" look)
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

    avatar_img = avatar_img.convert("L").convert("RGB")
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

    # Ignore commands that were sent long before we handled them
    # (e.g. delivered late after a network outage or restart).
    age = (datetime.now(timezone.utc) - message.date).total_seconds()
    if age > MAX_COMMAND_AGE_SECONDS:
        logger.info("Ignoring stale /indict (%.0fs old) in chat %s", age, chat_id)
        return

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

    trial = Trial(
        chat_id=chat_id,
        message_id=0,
        prosecutor_id=prosecutor.id,
        prosecutor_name=prosecutor_name,
        defendant_id=defendant.id,
        defendant_name=defendant_name,
        crime=crime,
    )

    # Reserve the chat slot immediately so two /indict commands can't race
    # while the avatar is being fetched.
    active_trials[chat_id] = trial

    avatar_bytes = await fetch_avatar_bytes(context, defendant.id)
    try:
        mugshot = generate_mugshot(defendant_name, crime, avatar_bytes)
    except Exception as e:
        logger.error("Mugshot generation failed, falling back to text-only trial: %s", e)
        mugshot = None

    trial.has_photo = mugshot is not None
    caption = lobby_caption(trial)
    keyboard = build_lobby_keyboard(trial)

    try:
        if mugshot is not None:
            sent = await send_with_retry(
                message.reply_photo, photo=mugshot, caption=caption,
                parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard,
            )
        else:
            sent = await send_with_retry(
                message.reply_text, text=caption,
                parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard,
            )
    except (BadRequest, Forbidden, TimedOut, NetworkError) as e:
        logger.error("Failed to post trial message: %s", e)
        active_trials.pop(chat_id, None)
        await message.reply_text(
            "The court reporter fainted. Could not start the trial (Telegram API error)."
        )
        return

    trial.message_id = sent.message_id
    trial.task = asyncio.create_task(run_trial_timer(chat_id, context))


# --------------------------------------------------------------------------- #
# Command: /rapsheet
# --------------------------------------------------------------------------- #

async def rapsheet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat_id = update.effective_chat.id

    target: User = (
        message.reply_to_message.from_user
        if message.reply_to_message and message.reply_to_message.from_user
        else message.from_user
    )
    name = target.first_name or target.username or "Unknown"
    sheet = rap_sheets.get(chat_id, {}).get(target.id)

    if not sheet:
        await message.reply_text(
            f"📂 {name} has a spotless record in this court. Suspiciously spotless."
        )
        return

    total = sheet["guilty"] + sheet["innocent"] + sheet["hung"]
    await message.reply_text(
        f"📂 RAP SHEET: {name}\n\n"
        f"Trials: {total}\n"
        f"🔨 Guilty: {sheet['guilty']}\n"
        f"😇 Innocent: {sheet['innocent']}\n"
        f"🤷 Hung juries: {sheet['hung']}"
    )


# --------------------------------------------------------------------------- #
# Silence /start, and quip on every other unknown command
# --------------------------------------------------------------------------- #

async def ignore_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deliberately does nothing: the bot never answers /start."""
    return


async def random_command_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch-all for commands we don't handle. Only reached when no earlier
    CommandHandler matched, so /indict, /rapsheet and /start never land here."""
    message = update.effective_message
    if not message or not message.text:
        return

    # Skip commands explicitly addressed to some other bot (/cmd@otherbot).
    first_token = message.text.split()[0]
    if "@" in first_token:
        addressed_to = first_token.split("@", 1)[1].lower()
        if addressed_to != (context.bot.username or "").lower():
            return

    # Ignore stale commands (e.g. delivered late after a restart).
    age = (datetime.now(timezone.utc) - message.date).total_seconds()
    if age > MAX_COMMAND_AGE_SECONDS:
        return

    if random.random() > RANDOM_REPLY_CHANCE:
        return

    try:
        await send_with_retry(
            message.reply_text, random.choice(QUIPS), parse_mode=ParseMode.MARKDOWN
        )
    except (BadRequest, Forbidden, TimedOut, NetworkError) as e:
        logger.warning("Could not send quip: %s", e)


# --------------------------------------------------------------------------- #
# Joining the jury (lobby phase)
# --------------------------------------------------------------------------- #

async def handle_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    parts = (query.data or "").split("|")

    try:
        chat_id = int(parts[1])
    except (IndexError, ValueError):
        await query.answer("Malformed summons.", show_alert=True)
        return

    trial = active_trials.get(chat_id)
    if (not trial or trial.ended or trial.phase != "lobby"
            or query.message.message_id != trial.message_id):
        await query.answer("The jury lobby is closed.", show_alert=True)
        return

    if user.id == trial.defendant_id:
        await query.answer("The defendant cannot sit on their own jury!", show_alert=True)
        return

    if user.id in trial.jurors:
        await query.answer("You're already on the jury.")
        return

    trial.jurors[user.id] = user.first_name or user.username or "Juror"

    try:
        await query.edit_message_reply_markup(reply_markup=build_lobby_keyboard(trial))
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning("Failed to refresh lobby count: %s", e)
    except (TimedOut, NetworkError) as e:
        logger.warning("Network hiccup updating lobby count: %s", e)

    await query.answer("You've joined the jury! ⚖️")


# --------------------------------------------------------------------------- #
# Voting (voting phase)
# --------------------------------------------------------------------------- #

async def handle_vote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    parts = (query.data or "").split("|")

    if len(parts) != 3:
        await query.answer("Malformed ballot.", show_alert=True)
        return

    _, choice, chat_id_str = parts
    if choice not in ("guilty", "innocent"):
        await query.answer("Malformed ballot.", show_alert=True)
        return
    try:
        chat_id = int(chat_id_str)
    except ValueError:
        await query.answer("Malformed ballot.", show_alert=True)
        return

    trial = active_trials.get(chat_id)
    if (not trial or trial.ended or trial.phase != "voting"
            or query.message.message_id != trial.message_id):
        await query.answer("Voting isn't open for this trial.", show_alert=True)
        return

    if user.id == trial.defendant_id:
        await query.answer("The defendant cannot vote in their own trial!", show_alert=True)
        return

    if user.id not in trial.jurors:
        await query.answer("Only jurors who joined the lobby can vote.", show_alert=True)
        return

    if trial.votes.get(user.id) == choice:
        await query.answer("You already cast that vote.")
        return

    trial.votes[user.id] = choice
    trial.voter_names[user.id] = user.first_name or user.username or "Juror"

    try:
        await query.edit_message_reply_markup(reply_markup=build_keyboard(trial))
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning("Failed to refresh vote counts: %s", e)
    except (TimedOut, NetworkError) as e:
        logger.warning("Network hiccup updating vote counts: %s", e)

    await query.answer(f"Vote for {choice.upper()} recorded.")


# --------------------------------------------------------------------------- #
# Objection! (defendant's one-time button)
# --------------------------------------------------------------------------- #

async def handle_objection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    parts = (query.data or "").split("|")

    try:
        chat_id = int(parts[1])
    except (IndexError, ValueError):
        await query.answer("Malformed objection.", show_alert=True)
        return

    trial = active_trials.get(chat_id)
    if (not trial or trial.ended or trial.phase != "voting"
            or query.message.message_id != trial.message_id):
        await query.answer("Nothing to object to right now.", show_alert=True)
        return

    if user.id != trial.defendant_id:
        await query.answer("Only the defendant may object!", show_alert=True)
        return

    if trial.objection_used:
        await query.answer("You already used your objection.", show_alert=True)
        return

    trial.objection_used = True
    sustained = random.random() < OBJECTION_SUSTAIN_CHANCE

    if sustained:
        guilty_voters = [uid for uid, v in trial.votes.items() if v == "guilty"]
        if guilty_voters:
            struck = random.choice(guilty_voters)
            trial.votes.pop(struck, None)
            trial.voter_names.pop(struck, None)
            reply = random.choice(OBJECTION_SUSTAINED)
        else:
            reply = "🧑‍⚖️ *SUSTAINED!* ...but there were no guilty votes to strike. Awkward."
    else:
        reply = random.choice(OBJECTION_OVERRULED)

    try:
        await query.edit_message_reply_markup(reply_markup=build_keyboard(trial))
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning("Failed to refresh keyboard after objection: %s", e)
    except (TimedOut, NetworkError) as e:
        logger.warning("Network hiccup updating keyboard: %s", e)

    await query.answer("OBJECTION! Your plea has been heard.")

    try:
        await send_with_retry(
            context.bot.send_message,
            chat_id=chat_id,
            text=f"🗣️ *{escape_md(trial.defendant_name)}: OBJECTION!*\n\n{reply}",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.warning("Could not post objection result in %s: %s", chat_id, e)


# --------------------------------------------------------------------------- #
# Verdict & enforcement
# --------------------------------------------------------------------------- #

async def try_mute(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int,
                   duration_seconds: int) -> bool:
    """Attempt to mute the defendant. Returns True on success, False if the
    bot isn't an admin / lacks restrict rights (never raises)."""
    until = datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)
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


def sentence_seconds(margin: int) -> int:
    """Mute length grows with the guilty margin, capped."""
    return min(MUTE_BASE_SECONDS + max(margin - 1, 0) * MUTE_STEP_SECONDS, MUTE_MAX_SECONDS)


async def run_trial_timer(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ---- Phase 1: jury lobby ----
    try:
        await asyncio.sleep(LOBBY_DURATION_SECONDS)
    except asyncio.CancelledError:
        return

    trial = active_trials.get(chat_id)
    if not trial or trial.ended:
        return

    if len(trial.jurors) < MIN_JURORS:
        active_trials.pop(chat_id, None)
        trial.ended = True
        await edit_trial_message(
            context, trial,
            "🚪 *LOBBY CLOSED* 🚪\n\n"
            f"Only {len(trial.jurors)}/{MIN_JURORS} jurors showed up in "
            f"{LOBBY_DURATION_SECONDS} seconds.\n"
            f"The case against *{escape_md(trial.defendant_name)}* is dismissed. "
            f"{random.choice(DISMISS_LINES)}",
            None,
        )
        return

    # ---- Phase 2: voting ----
    trial.phase = "voting"
    await edit_trial_message(context, trial, voting_caption(trial), build_keyboard(trial))

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
        verdict, outcome, flavor = "GUILTY", "guilty", random.choice(GUILTY_LINES)
    elif innocent_count > guilty_count:
        verdict, outcome, flavor = "INNOCENT", "innocent", random.choice(INNOCENT_LINES)
    else:
        verdict, outcome, flavor = "HUNG JURY", "hung", random.choice(HUNG_LINES)

    record_result(chat_id, trial.defendant_id, trial.defendant_name, outcome)

    result_text = (
        "⚖️ *THE COURT HAS REACHED A VERDICT* ⚖️\n\n"
        f"*Defendant:* {escape_md(trial.defendant_name)}\n"
        f"*Charge:* {escape_md(trial.crime)}\n"
        f"*Jurors:* {len(trial.jurors)}\n"
        f"*Votes:* 🔨 {guilty_count}  vs  😇 {innocent_count}\n\n"
        f"*VERDICT: {verdict}*\n"
        f"_{flavor}_"
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
        margin = guilty_count - innocent_count
        duration = sentence_seconds(margin)
        muted = await try_mute(context, chat_id, trial.defendant_id, duration)
        service = random.choice(COMMUNITY_SERVICE)
        landslide = "\n🌋 *LANDSLIDE VERDICT.* The court shows no mercy." if margin >= 4 else ""
        if muted:
            enforcement_note = (
                f"\n\n🔇 The defendant has been silenced for "
                f"{duration // 60} minute(s)."
                f"{landslide}\n"
                f"🧹 *Community service:* {service}"
            )
        else:
            enforcement_note = (
                "\n\n📢 *PUBLIC SHAMING SENTENCE*: the bot isn't an admin here, so it "
                "can't mute anyone — let it be known across the group that justice was "
                f"served in spirit, if not in silence.{landslide}\n"
                f"🧹 *Community service:* {service}"
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

    # Order matters: handlers in the same group are checked top to bottom and
    # the first match wins. /start must come before the catch-all.
    app.add_handler(CommandHandler("start", ignore_start))
    app.add_handler(CommandHandler("indict", indict))
    app.add_handler(CommandHandler("rapsheet", rapsheet))
    app.add_handler(CallbackQueryHandler(handle_join, pattern=r"^join\|"))
    app.add_handler(CallbackQueryHandler(handle_vote, pattern=r"^vote\|"))
    app.add_handler(CallbackQueryHandler(handle_objection, pattern=r"^obj\|"))
    # Catch-all for every other command (must be registered LAST).
    app.add_handler(MessageHandler(filters.COMMAND, random_command_reply))

    logger.info("Starting Meme Court bot (long polling)...")
    # drop_pending_updates=True discards everything users sent while the bot was
    # offline/restarting (e.g. a Render redeploy), so old /indict commands are
    # never replayed.
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
