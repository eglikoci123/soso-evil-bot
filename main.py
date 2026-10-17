"""
Silly Court — a Telegram group bot that puts members on trial for silly takes.

Flow
----
1. /indict (reply to a message) opens a 90-second JURY LOBBY with a mugshot.
2. People tap "JOIN THE JURY". The defendant can't join.
3. After 90s: if fewer than MIN_JURORS joined, the case is DISMISSED (stamped
   image). Otherwise the post swaps to an "IN SESSION" image and voting opens
   for another 90s (jurors only).
4. The defendant may shout OBJECTION! once (30% chance it's sustained and a
   guilty vote is struck). An "OBJECTION!" banner image is posted.
5. A verdict image (GUILTY / NOT GUILTY / HUNG JURY stamp) is posted, and the
   defendant is optionally muted if the bot is admin.
6. /rapsheet shows a rap-sheet card image (reply to someone, or yourself).

The bot only reacts to /indict and /rapsheet (in groups). Everything else —
including /start — is ignored on purpose.

Anti-flood system (see the FLOOD CONTROL section)
-------------------------------------------------
- Per-user command flood guard   -> too many commands = silent ignore penalty
- Per-chat command flood guard   -> protects the group from multi-user spam
- Per-user cooldowns             -> /indict and /rapsheet spacing
- Chat cooldown between trials   -> no back-to-back trial spam
- Defendant protection window    -> same person can't be dragged to court again
                                    right away
- Button-tap guard               -> stops vote/join button mashing
- Charge length cap, temp (self-deleting) warnings, one-warning-per-penalty,
  stale-command filter, and periodic memory cleanup.

Architecture
------------
- python-telegram-bot v20+ (async, ApplicationBuilder, concurrent updates)
- In-memory state (`active_trials`, `rap_sheets`) — lost on restart.
- Pillow generates every image in memory (run in a worker thread so the bot
  never blocks). Needs Pillow >= 10.1.
- A tiny HTTP server + self-ping loop keeps a Render Free Web Service awake.
"""

import io
import os
import time
import math
import random
import asyncio
import logging
import threading
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Deque, Dict, Optional

from dotenv import load_dotenv  # local dev convenience: loads .env if present
from PIL import Image, ImageDraw, ImageFont

from telegram import (
    Update,
    User,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut, NetworkError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

load_dotenv()

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("silly_court")

LOBBY_DURATION_SECONDS = 90      # time to join before the lobby closes
TRIAL_DURATION_SECONDS = 90      # voting time once the trial starts
MIN_JURORS = 3                   # lobby closes if fewer people join
MUTE_BASE_SECONDS = 180          # mute for a 1-vote margin
MUTE_STEP_SECONDS = 90           # extra mute per additional vote of margin
MUTE_MAX_SECONDS = 600           # hard cap (10 minutes)
MAX_COMMAND_AGE_SECONDS = 60     # commands older than this are ignored as stale
OBJECTION_SUSTAIN_CHANCE = 0.30  # chance the judge sustains an objection
KEEP_ALIVE_INTERVAL_SECONDS = 600
MAX_CRIME_LENGTH = 100           # charges longer than this are trimmed

# ---- Anti-flood tuning ----
CMD_FLOOD_MAX = 4                # a user may send this many commands...
CMD_FLOOD_WINDOW = 20            # ...within this many seconds
CMD_FLOOD_PENALTY = 120          # otherwise they're ignored for this long

CHAT_FLOOD_MAX = 10              # whole-chat command limit...
CHAT_FLOOD_WINDOW = 30           # ...per this many seconds
CHAT_FLOOD_PENALTY = 60          # chat-wide timeout

BUTTON_FLOOD_MAX = 6             # button taps allowed...
BUTTON_FLOOD_WINDOW = 5          # ...per this many seconds
BUTTON_FLOOD_PENALTY = 15        # then taps are ignored this long

INDICT_USER_COOLDOWN = 180       # one indictment per user per 3 minutes
RAPSHEET_USER_COOLDOWN = 20      # one rap sheet per user per 20 seconds
CHAT_TRIAL_COOLDOWN = 45         # pause between trials in the same chat
DEFENDANT_PROTECTION = 600       # same person can't be re-indicted for 10 min
TEMP_MESSAGE_SECONDS = 8         # warnings auto-delete after this long

# --------------------------------------------------------------------------- #
# Keep-alive web server (for Render Free Web Service)
# --------------------------------------------------------------------------- #

class HealthCheckHandler(BaseHTTPRequestHandler):
    """Answers GET and HEAD (UptimeRobot's free plan uses HEAD)."""

    def _ok(self, body: bool = True) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        if body:
            self.wfile.write(b"OK")

    def do_GET(self):
        self._ok()

    def do_HEAD(self):
        self._ok(body=False)

    def log_message(self, format, *args):
        pass  # keeps logs clean


def start_health_check_server() -> None:
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthCheckHandler).serve_forever()


def keep_alive_loop() -> None:
    """Ping our own public URL so Render sees inbound traffic.
    RENDER_EXTERNAL_URL is set automatically by Render."""
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        logger.info("RENDER_EXTERNAL_URL not set; self-ping disabled.")
        return
    while True:
        time.sleep(KEEP_ALIVE_INTERVAL_SECONDS)
        try:
            urllib.request.urlopen(url, timeout=15).read()
        except Exception as e:
            logger.warning("Keep-alive ping failed: %s", e)

# --------------------------------------------------------------------------- #
# FLOOD CONTROL
# --------------------------------------------------------------------------- #

class FloodGuard:
    """Sliding-window limiter with a penalty box.

    hit(key) returns:
      "ok"       -> allowed
      "tripped"  -> this hit exceeded the limit; penalty just started
                    (warn exactly once!)
      "blocked"  -> still in the penalty box; ignore silently
    """

    def __init__(self, max_events: int, window: float, penalty: float):
        self.max_events = max_events
        self.window = window
        self.penalty = penalty
        self._events: Dict[Any, Deque[float]] = {}
        self._blocked: Dict[Any, float] = {}

    def hit(self, key: Any) -> str:
        now = time.monotonic()
        until = self._blocked.get(key)
        if until is not None:
            if now < until:
                return "blocked"
            del self._blocked[key]
            self._events.pop(key, None)

        q = self._events.setdefault(key, deque())
        while q and now - q[0] > self.window:
            q.popleft()
        q.append(now)

        if len(q) > self.max_events:
            self._blocked[key] = now + self.penalty
            q.clear()
            return "tripped"
        return "ok"

    def cleanup(self) -> None:
        now = time.monotonic()
        for key in [k for k, t in self._blocked.items() if t <= now]:
            del self._blocked[key]
        for key in list(self._events):
            q = self._events[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if not q:
                del self._events[key]


class Cooldowns:
    """Simple keyed cooldown timers."""

    def __init__(self) -> None:
        self._until: Dict[Any, float] = {}

    def set(self, key: Any, seconds: float) -> None:
        self._until[key] = time.monotonic() + seconds

    def remaining(self, key: Any) -> float:
        left = self._until.get(key, 0) - time.monotonic()
        if left <= 0:
            self._until.pop(key, None)
            return 0.0
        return left

    def cleanup(self) -> None:
        now = time.monotonic()
        for key in [k for k, t in self._until.items() if t <= now]:
            del self._until[key]


user_cmd_guard = FloodGuard(CMD_FLOOD_MAX, CMD_FLOOD_WINDOW, CMD_FLOOD_PENALTY)
chat_cmd_guard = FloodGuard(CHAT_FLOOD_MAX, CHAT_FLOOD_WINDOW, CHAT_FLOOD_PENALTY)
button_guard = FloodGuard(BUTTON_FLOOD_MAX, BUTTON_FLOOD_WINDOW, BUTTON_FLOOD_PENALTY)
cooldowns = Cooldowns()

_bg_tasks: set = set()  # keeps references so background tasks aren't GC'd


def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _delete_later(message, seconds: float) -> None:
    await asyncio.sleep(seconds)
    try:
        await message.delete()
    except Exception:
        pass


async def reply_temp(message, text: str, seconds: float = TEMP_MESSAGE_SECONDS, **kwargs) -> None:
    """Reply with a warning that deletes itself, so warnings can't clutter chat."""
    try:
        sent = await message.reply_text(text, **kwargs)
    except Exception as e:
        logger.info("Could not send temp reply: %s", e)
        return
    spawn(_delete_later(sent, seconds))


def _fmt_wait(seconds: float) -> str:
    seconds = int(math.ceil(seconds))
    if seconds >= 60:
        m, s = divmod(seconds, 60)
        return f"{m}m {s}s" if s else f"{m}m"
    return f"{seconds}s"


async def passes_flood_checks(update: Update) -> bool:
    """Run the per-user and per-chat command guards. Returns False if the
    command must be ignored. Warns once when a penalty starts, then goes quiet."""
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return False

    result = user_cmd_guard.hit(user.id)
    if result == "blocked":
        return False
    if result == "tripped":
        name = user.first_name or user.username or "Someone"
        await reply_temp(
            message,
            f"🚨 {name}, the bailiff says SLOW DOWN. "
            f"Your commands are ignored for {_fmt_wait(CMD_FLOOD_PENALTY)}.",
        )
        return False

    result = chat_cmd_guard.hit(chat.id)
    if result == "blocked":
        return False
    if result == "tripped":
        await reply_temp(
            message,
            f"🧯 Too much noise in the court! Taking a {_fmt_wait(CHAT_FLOOD_PENALTY)} recess.",
        )
        return False

    return True


async def passes_button_guard(query) -> bool:
    """Stop button mashing. Always answers the query so the spinner clears."""
    result = button_guard.hit(query.from_user.id)
    if result == "ok":
        return True
    if result == "tripped":
        await query.answer("🐢 Too fast! Taps are paused for a few seconds.", show_alert=True)
    else:
        await query.answer("🐢 Slow down.")
    return False


async def housekeeping_loop() -> None:
    """Periodically drop expired limiter entries so memory never grows."""
    while True:
        await asyncio.sleep(300)
        try:
            user_cmd_guard.cleanup()
            chat_cmd_guard.cleanup()
            button_guard.cleanup()
            cooldowns.cleanup()
        except Exception as e:
            logger.warning("Housekeeping failed: %s", e)

# --------------------------------------------------------------------------- #
# Flavor text
# --------------------------------------------------------------------------- #

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
# `rap_sheets` is chat_id -> user_id -> {"name", "guilty", "innocent", "hung",
# "last_crime"}. Same caveat: lost on restart.
#
# To persist, swap these dicts for SQLite (a `trials` and `rap_sheets` table)
# or Redis (hash per trial with EXPIRE so crashed trials self-clean).
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
    case_no: int = 0
    avatar_bytes: Optional[bytes] = None
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


def record_result(chat_id: int, user_id: int, name: str, outcome: str, crime: str) -> None:
    """outcome: 'guilty' | 'innocent' | 'hung'"""
    sheet = rap_sheets.setdefault(chat_id, {}).setdefault(
        user_id, {"name": name, "guilty": 0, "innocent": 0, "hung": 0, "last_crime": ""}
    )
    sheet["name"] = name
    sheet["last_crime"] = crime
    sheet[outcome] += 1


def finish_trial_cooldowns(trial: Trial) -> None:
    """Start the post-trial pauses (anti-flood)."""
    cooldowns.set(("chat_trial", trial.chat_id), CHAT_TRIAL_COOLDOWN)
    cooldowns.set(("defendant", trial.chat_id, trial.defendant_id), DEFENDANT_PROTECTION)


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
        f"🏛️ *SILLY COURT IS OPENING* 🏛️  _Case #{trial.case_no:04d}_\n\n"
        f"*Prosecutor:* {escape_md(trial.prosecutor_name)}\n"
        f"*Defendant:* {escape_md(trial.defendant_name)}\n"
        f"*Charge:* {escape_md(trial.crime)}\n\n"
        f"Tap *JOIN THE JURY* within *{LOBBY_DURATION_SECONDS} seconds*.\n"
        f"At least *{MIN_JURORS} jurors* are needed or the lobby closes.\n"
        "_The defendant may not join the jury._"
    )


def voting_caption(trial: Trial) -> str:
    return (
        f"🏛️ *SILLY COURT IS NOW IN SESSION* 🏛️  _Case #{trial.case_no:04d}_\n\n"
        f"*Prosecutor:* {escape_md(trial.prosecutor_name)}\n"
        f"*Defendant:* {escape_md(trial.defendant_name)}\n"
        f"*Charge:* {escape_md(trial.crime)}\n"
        f"*Jurors:* {len(trial.jurors)}\n\n"
        f"Jurors, you have *{TRIAL_DURATION_SECONDS} seconds* to cast your verdict.\n"
        "_The defendant has one OBJECTION. Use it wisely._"
    )


async def edit_trial_message(context, trial: Trial, text: str, markup,
                             image: Optional[bytes] = None) -> None:
    """Edit the trial post (photo caption or plain text). If `image` is given
    and the post is a photo, the picture itself is swapped too."""
    try:
        if trial.has_photo and image is not None:
            try:
                await context.bot.edit_message_media(
                    chat_id=trial.chat_id, message_id=trial.message_id,
                    media=InputMediaPhoto(media=image, caption=text,
                                          parse_mode=ParseMode.MARKDOWN),
                    reply_markup=markup,
                )
                return
            except (BadRequest, TimedOut, NetworkError) as e:
                logger.warning("Could not swap trial image, editing caption only: %s", e)

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


async def post_image_or_text(context, chat_id: int, image: Optional[bytes], text: str) -> None:
    """Send a photo with caption; fall back to plain text if the image is
    missing or Telegram rejects it."""
    if image is not None:
        try:
            await send_with_retry(
                context.bot.send_photo, chat_id=chat_id, photo=image,
                caption=text, parse_mode=ParseMode.MARKDOWN,
            )
            return
        except Exception as e:
            logger.warning("Photo post failed in %s, falling back to text: %s", chat_id, e)
    try:
        await send_with_retry(
            context.bot.send_message, chat_id=chat_id, text=text,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error("Failed to post message in chat %s: %s", chat_id, e)


# --------------------------------------------------------------------------- #
# Image generation (Pillow) — every function returns PNG bytes
# --------------------------------------------------------------------------- #

GOLD = (230, 210, 170)
DARK_BG = (24, 20, 16)


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """Try a few common DejaVu paths (present on most Linux hosts, incl. Render),
    falling back to Pillow's built-in font so this never crashes."""
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
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _fit_font(text: str, max_width: int, start: int, minimum: int = 20):
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    size = start
    while size > minimum:
        font = _load_font(size)
        box = probe.textbbox((0, 0), text, font=font)
        if box[2] - box[0] <= max_width:
            return font
        size -= 4
    return _load_font(minimum)


def _wrap_text(text: str, font, max_width: int) -> list:
    words = text.split()
    lines, current = [], ""
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    for word in words:
        candidate = f"{current} {word}".strip()
        box = probe.textbbox((0, 0), candidate, font=font)
        if box[2] - box[0] <= max_width:
            current = candidate
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
    draw.text((size // 2, size // 2), "?", font=_load_font(size // 2),
              fill=(210, 210, 210), anchor="mm")
    return img


def _avatar_image(avatar_bytes: Optional[bytes], size: int, gray: bool) -> Image.Image:
    img = None
    if avatar_bytes:
        try:
            img = Image.open(io.BytesIO(avatar_bytes)).convert("RGB").resize((size, size))
        except Exception as e:
            logger.warning("Could not decode avatar image, using placeholder: %s", e)
    if img is None:
        img = _placeholder_avatar(size)
    if gray:
        img = img.convert("L").convert("RGB")
    return img


def _dark_background(width: int, height: int, header_color, title: str) -> Image.Image:
    bg = Image.new("RGB", (width, height), DARK_BG)
    draw = ImageDraw.Draw(bg)
    # cheap vintage texture: subtle vertical shading, no external assets needed
    for x in range(0, width, 3):
        shade = random.randint(0, 12)
        draw.line([(x, 0), (x, height)], fill=(24 - shade, 20 - shade, 16 - shade))
    draw.rectangle([0, 0, width, 90], fill=header_color)
    draw.text((width // 2, 45), title, font=_load_font(42), fill=GOLD, anchor="mm")
    return bg


def _frame(bg: Image.Image, box, size: int) -> None:
    ImageDraw.Draw(bg).rectangle(
        [box[0] - 4, box[1] - 4, box[0] + size + 4, box[1] + size + 4],
        outline=GOLD, width=4,
    )


def _stamp(base: Image.Image, text: str, color, center_y: int, angle: float = -16) -> None:
    """Paste a rotated, slightly translucent rubber stamp onto `base`."""
    font = _fit_font(text, 520, 96, 32)
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    box = probe.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    pad = 22
    stamp = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    sd = ImageDraw.Draw(stamp)
    sd.rounded_rectangle([0, 0, stamp.width - 1, stamp.height - 1], radius=14,
                         outline=color + (255,), width=8)
    sd.text((pad - box[0], pad - box[1]), text, font=font, fill=color + (255,))
    stamp = stamp.rotate(angle, expand=True, resample=Image.BICUBIC)
    alpha = stamp.split()[3].point(lambda a: int(a * 0.9))
    stamp.putalpha(alpha)
    pos = ((base.width - stamp.width) // 2, center_y - stamp.height // 2)
    base.paste(stamp, pos, stamp)


def _to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def generate_mugshot(defendant_name: str, crime: str, avatar_bytes: Optional[bytes],
                     case_no: int = 0) -> bytes:
    """'WANTED' mugshot with a height chart and a case-number plate."""
    width, height = 800, 600
    bg = _dark_background(width, height, (58, 14, 14), "SILLY COURT")
    draw = ImageDraw.Draw(bg)

    # height chart on the wall (left side)
    for i, y in enumerate(range(130, 520, 24)):
        length = 44 if i % 5 == 0 else 24
        draw.line([(40, y), (40 + length, y)], fill=(120, 110, 95), width=3)
    draw.line([(40, 130), (40, 506)], fill=(120, 110, 95), width=3)

    avatar_size = 260
    avatar_box = (width // 2 - avatar_size // 2, 115)
    bg.paste(_avatar_image(avatar_bytes, avatar_size, gray=True), avatar_box)
    _frame(bg, avatar_box, avatar_size)

    # case plate (right side)
    draw.rectangle([590, 250, 770, 350], fill=(232, 232, 226), outline=(20, 20, 20), width=4)
    draw.text((680, 278), "CASE No.", font=_load_font(20), fill=(30, 30, 30), anchor="mm")
    draw.text((680, 318), f"#{case_no:04d}", font=_load_font(36), fill=(150, 20, 20), anchor="mm")

    draw.text((width // 2, avatar_box[1] + avatar_size + 40), defendant_name.upper()[:28],
              font=_fit_font(defendant_name.upper()[:28], width - 80, 30, 18),
              fill=(255, 255, 255), anchor="mm")

    crime_font = _load_font(22)
    y = avatar_box[1] + avatar_size + 85
    for line in _wrap_text(f"WANTED FOR: {crime.upper()}", crime_font, width - 80)[:3]:
        draw.text((width // 2, y), line, font=crime_font, fill=(255, 205, 110), anchor="mm")
        y += 30
    return _to_png(bg)


# verdict key -> (stamp text, stamp color, header color, jail bars?, grayscale?)
VERDICT_STYLES = {
    "GUILTY":    ("GUILTY",     (205, 35, 35),  (90, 14, 14),  True,  True),
    "INNOCENT":  ("NOT GUILTY", (45, 175, 85),  (14, 70, 36),  False, False),
    "HUNG":      ("HUNG JURY",  (160, 160, 175), (50, 50, 62), False, True),
    "DISMISSED": ("DISMISSED",  (125, 125, 140), (45, 45, 55), False, True),
    "SESSION":   ("IN SESSION", (225, 175, 45), (28, 44, 90),  False, False),
}


def generate_verdict_image(defendant_name: str, avatar_bytes: Optional[bytes], verdict_key: str,
                           guilty: int = 0, innocent: int = 0, jurors: int = 0,
                           case_no: int = 0) -> bytes:
    """Stamped picture for each stage: IN SESSION, GUILTY, NOT GUILTY, HUNG, DISMISSED."""
    text, color, header, bars, gray = VERDICT_STYLES[verdict_key]
    width, height = 800, 600
    bg = _dark_background(width, height, header, f"SILLY COURT  -  CASE #{case_no:04d}")
    draw = ImageDraw.Draw(bg)

    avatar_size = 260
    avatar_box = (width // 2 - avatar_size // 2, 115)
    bg.paste(_avatar_image(avatar_bytes, avatar_size, gray=gray), avatar_box)

    if bars:  # jail bars over the avatar
        for x in range(avatar_box[0] + 20, avatar_box[0] + avatar_size, 44):
            draw.rectangle([x, avatar_box[1], x + 10, avatar_box[1] + avatar_size],
                           fill=(40, 40, 46))
            draw.rectangle([x + 2, avatar_box[1], x + 4, avatar_box[1] + avatar_size],
                           fill=(95, 95, 105))
    _frame(bg, avatar_box, avatar_size)

    draw.text((width // 2, avatar_box[1] + avatar_size + 40), defendant_name.upper()[:28],
              font=_fit_font(defendant_name.upper()[:28], width - 80, 30, 18),
              fill=(255, 255, 255), anchor="mm")

    if verdict_key == "SESSION":
        footer = f"{jurors} JURORS SWORN IN  -  CAST YOUR VOTE"
    elif verdict_key == "DISMISSED":
        footer = "NOT ENOUGH JURORS SHOWED UP"
    else:
        footer = f"{jurors} JURORS   |   GUILTY {guilty}  -  INNOCENT {innocent}"
    draw.text((width // 2, avatar_box[1] + avatar_size + 90), footer,
              font=_fit_font(footer, width - 80, 24, 16), fill=(255, 205, 110), anchor="mm")

    _stamp(bg, text, color, center_y=avatar_box[1] + avatar_size // 2 + 10)
    return _to_png(bg)


def generate_banner(headline: str, sub: str, base_color) -> bytes:
    """Comic-style burst banner (used for OBJECTION! results)."""
    width, height = 800, 320
    img = Image.new("RGB", (width, height), base_color)
    draw = ImageDraw.Draw(img)
    cx, cy = width // 2, height // 2
    light = tuple(min(255, c + 45) for c in base_color)
    for deg in range(0, 360, 24):
        a1, a2 = math.radians(deg), math.radians(deg + 12)
        draw.polygon(
            [(cx, cy),
             (cx + 900 * math.cos(a1), cy + 900 * math.sin(a1)),
             (cx + 900 * math.cos(a2), cy + 900 * math.sin(a2))],
            fill=light,
        )
    draw.text((cx, cy - 35), headline, font=_fit_font(headline, width - 80, 110, 40),
              fill=(255, 255, 255), anchor="mm", stroke_width=8, stroke_fill=(15, 15, 15))
    draw.text((cx, cy + 75), sub, font=_fit_font(sub, width - 100, 52, 24),
              fill=(255, 235, 120), anchor="mm", stroke_width=5, stroke_fill=(15, 15, 15))
    return _to_png(img)


def _rank_for(sheet: Optional[dict]) -> str:
    if not sheet:
        return "SPOTLESS (SUSPICIOUSLY)"
    g = sheet["guilty"]
    if g >= 10:
        return "PUBLIC ENEMY #1"
    if g >= 5:
        return "REPEAT OFFENDER"
    if g >= 2:
        return "PERSON OF INTEREST"
    if g == 1:
        return "FIRST-TIME OFFENDER"
    return "ACQUITTED CITIZEN"


def generate_rapsheet_card(name: str, sheet: Optional[dict], avatar_bytes: Optional[bytes]) -> bytes:
    """A rap-sheet card with avatar, stats and a verdict-ratio bar."""
    width, height = 800, 520
    bg = _dark_background(width, height, (40, 40, 52), "SILLY COURT  -  RAP SHEET")
    draw = ImageDraw.Draw(bg)

    guilty = sheet["guilty"] if sheet else 0
    innocent = sheet["innocent"] if sheet else 0
    hung = sheet["hung"] if sheet else 0
    total = guilty + innocent + hung

    size = 230
    box = (40, 125)
    bg.paste(_avatar_image(avatar_bytes, size, gray=True), box)
    _frame(bg, box, size)

    x = 320
    draw.text((x, 135), name.upper()[:24], font=_fit_font(name.upper()[:24], 440, 34, 18),
              fill=(255, 255, 255), anchor="lm")
    draw.text((x, 178), _rank_for(sheet), font=_fit_font(_rank_for(sheet), 440, 24, 14),
              fill=(255, 205, 110), anchor="lm")

    rows = [("TRIALS", total, GOLD), ("GUILTY", guilty, (225, 80, 80)),
            ("INNOCENT", innocent, (90, 200, 120)), ("HUNG", hung, (170, 170, 185))]
    y = 225
    for label, value, color in rows:
        draw.text((x, y), label, font=_load_font(26), fill=(190, 180, 160), anchor="lm")
        draw.text((x + 440, y), str(value), font=_load_font(30), fill=color, anchor="rm")
        draw.line([(x, y + 22), (x + 440, y + 22)], fill=(70, 62, 52), width=2)
        y += 48

    # ratio bar
    bar_x, bar_y, bar_w, bar_h = 40, 420, 720, 26
    draw.rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h], fill=(55, 50, 44))
    if total:
        cur = bar_x
        for value, color in ((guilty, (205, 55, 55)), (innocent, (60, 175, 100)),
                             (hung, (150, 150, 165))):
            seg = int(bar_w * value / total)
            if seg:
                draw.rectangle([cur, bar_y, cur + seg, bar_y + bar_h], fill=color)
                cur += seg
    draw.rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h], outline=GOLD, width=2)

    last = (sheet or {}).get("last_crime") or "nothing. yet."
    draw.text((40, 482), f"LAST CHARGE: {last.upper()}"[:70],
              font=_fit_font(f"LAST CHARGE: {last.upper()}"[:70], 720, 20, 12),
              fill=(255, 205, 110), anchor="lm")
    return _to_png(bg)


async def fetch_avatar_bytes(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> Optional[bytes]:
    """Return the user's highest-res profile photo as raw bytes, or None if
    they have no photo / it can't be fetched (private settings, deleted, etc.)."""
    try:
        photos = await context.bot.get_user_profile_photos(user_id, limit=1)
    except (BadRequest, Forbidden) as e:
        logger.info("Could not read profile photos for %s: %s", user_id, e)
        return None
    except (TimedOut, NetworkError) as e:
        logger.info("Network issue reading profile photos for %s: %s", user_id, e)
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


async def make_image(builder, *args, **kwargs) -> Optional[bytes]:
    """Run a Pillow builder in a worker thread; never raises."""
    try:
        return await asyncio.to_thread(builder, *args, **kwargs)
    except Exception as e:
        logger.error("Image generation failed (%s): %s", getattr(builder, "__name__", "?"), e)
        return None


# --------------------------------------------------------------------------- #
# Command: /indict
# --------------------------------------------------------------------------- #

async def indict(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat_id = update.effective_chat.id
    prosecutor: Optional[User] = message.from_user if message else None
    if message is None or prosecutor is None:
        return

    # Ignore commands that were sent long before we handled them
    # (e.g. delivered late after a network outage or restart).
    age = (datetime.now(timezone.utc) - message.date).total_seconds()
    if age > MAX_COMMAND_AGE_SECONDS:
        logger.info("Ignoring stale /indict (%.0fs old) in chat %s", age, chat_id)
        return

    # Anti-flood: per-user + per-chat command guards.
    if not await passes_flood_checks(update):
        return

    if chat_id in active_trials:
        await reply_temp(
            message, "⚖️ Order in the court! A trial is already underway here. Wait for the verdict."
        )
        return

    left = cooldowns.remaining(("chat_trial", chat_id))
    if left > 0:
        await reply_temp(message, f"🧹 The court is being swept. Next trial in {_fmt_wait(left)}.")
        return

    left = cooldowns.remaining(("indict_user", chat_id, prosecutor.id))
    if left > 0:
        await reply_temp(message, f"⏳ Your gavel is still warm. Try again in {_fmt_wait(left)}.")
        return

    if not message.reply_to_message or not message.reply_to_message.from_user:
        await reply_temp(
            message,
            "You must *reply* to the accused's message to file an indictment.\n"
            "Usage: reply to a message with `/indict being wrong about pineapple pizza`",
            seconds=15, parse_mode=ParseMode.MARKDOWN,
        )
        return

    defendant: User = message.reply_to_message.from_user

    if defendant.is_bot:
        await reply_temp(message, "You cannot put a bot on trial. We have rights too.")
        return

    if defendant.id == prosecutor.id:
        await reply_temp(
            message, "Self-indictment denied. Nice try, but the court isn't your therapist."
        )
        return

    left = cooldowns.remaining(("defendant", chat_id, defendant.id))
    if left > 0:
        await reply_temp(
            message,
            f"🛡️ {defendant.first_name or 'They'} just went through a trial. "
            f"Double jeopardy protection for {_fmt_wait(left)}.",
        )
        return

    crime = " ".join(context.args).strip() if context.args else "Unspecified crimes against comedy"
    if len(crime) > MAX_CRIME_LENGTH:
        crime = crime[: MAX_CRIME_LENGTH - 1].rstrip() + "…"
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
        case_no=random.randint(1, 9999),
    )

    # Reserve the chat slot immediately (no await between the checks above and
    # this line) so two /indict commands can't race while the avatar loads.
    active_trials[chat_id] = trial

    trial.avatar_bytes = await fetch_avatar_bytes(context, defendant.id)
    mugshot = await make_image(generate_mugshot, defendant_name, crime,
                               trial.avatar_bytes, trial.case_no)

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
        await reply_temp(
            message, "The court reporter fainted. Could not start the trial (Telegram API error)."
        )
        return

    cooldowns.set(("indict_user", chat_id, prosecutor.id), INDICT_USER_COOLDOWN)
    trial.message_id = sent.message_id
    trial.task = asyncio.create_task(run_trial_timer(chat_id, context))


# --------------------------------------------------------------------------- #
# Command: /rapsheet
# --------------------------------------------------------------------------- #

async def rapsheet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat_id = update.effective_chat.id
    if message is None or message.from_user is None:
        return

    age = (datetime.now(timezone.utc) - message.date).total_seconds()
    if age > MAX_COMMAND_AGE_SECONDS:
        return

    if not await passes_flood_checks(update):
        return

    left = cooldowns.remaining(("rap", chat_id, message.from_user.id))
    if left > 0:
        await reply_temp(message, f"📂 The clerk is busy. Ask again in {_fmt_wait(left)}.")
        return
    cooldowns.set(("rap", chat_id, message.from_user.id), RAPSHEET_USER_COOLDOWN)

    target: User = (
        message.reply_to_message.from_user
        if message.reply_to_message and message.reply_to_message.from_user
        else message.from_user
    )
    name = target.first_name or target.username or "Unknown"
    sheet = rap_sheets.get(chat_id, {}).get(target.id)

    avatar = await fetch_avatar_bytes(context, target.id)
    card = await make_image(generate_rapsheet_card, name, sheet, avatar)

    if sheet:
        total = sheet["guilty"] + sheet["innocent"] + sheet["hung"]
        caption = (
            f"📂 *RAP SHEET: {escape_md(name)}*\n"
            f"🏷️ {_rank_for(sheet)}\n\n"
            f"Trials: {total}\n"
            f"🔨 Guilty: {sheet['guilty']}\n"
            f"😇 Innocent: {sheet['innocent']}\n"
            f"🤷 Hung juries: {sheet['hung']}"
        )
    else:
        caption = f"📂 {escape_md(name)} has a spotless record in this court. Suspiciously spotless."

    try:
        if card is not None:
            await send_with_retry(message.reply_photo, photo=card, caption=caption,
                                  parse_mode=ParseMode.MARKDOWN)
        else:
            await send_with_retry(message.reply_text, text=caption,
                                  parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.warning("Could not send rap sheet in %s: %s", chat_id, e)


# --------------------------------------------------------------------------- #
# Joining the jury (lobby phase)
# --------------------------------------------------------------------------- #

async def handle_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    if not await passes_button_guard(query):
        return
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
    if not await passes_button_guard(query):
        return
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
    if not await passes_button_guard(query):
        return
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

    banner = await make_image(
        generate_banner,
        "OBJECTION!",
        "SUSTAINED!" if sustained else "OVERRULED!",
        (35, 85, 175) if sustained else (160, 40, 40),
    )
    await post_image_or_text(
        context, chat_id, banner,
        f"🗣️ *{escape_md(trial.defendant_name)}: OBJECTION!*\n\n{reply}",
    )


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
        finish_trial_cooldowns(trial)
        image = await make_image(generate_verdict_image, trial.defendant_name,
                                 trial.avatar_bytes, "DISMISSED",
                                 0, 0, len(trial.jurors), trial.case_no)
        await edit_trial_message(
            context, trial,
            "🚪 *LOBBY CLOSED* 🚪\n\n"
            f"Only {len(trial.jurors)}/{MIN_JURORS} jurors showed up in "
            f"{LOBBY_DURATION_SECONDS} seconds.\n"
            f"The case against *{escape_md(trial.defendant_name)}* is dismissed. "
            f"{random.choice(DISMISS_LINES)}",
            None, image=image,
        )
        return

    # ---- Phase 2: voting ----
    trial.phase = "voting"
    session_img = await make_image(generate_verdict_image, trial.defendant_name,
                                   trial.avatar_bytes, "SESSION",
                                   0, 0, len(trial.jurors), trial.case_no)
    await edit_trial_message(context, trial, voting_caption(trial),
                             build_keyboard(trial), image=session_img)

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
    finish_trial_cooldowns(trial)

    guilty_count = sum(1 for v in trial.votes.values() if v == "guilty")
    innocent_count = sum(1 for v in trial.votes.values() if v == "innocent")

    if guilty_count > innocent_count:
        verdict, outcome, key, flavor = "GUILTY", "guilty", "GUILTY", random.choice(GUILTY_LINES)
    elif innocent_count > guilty_count:
        verdict, outcome, key, flavor = "INNOCENT", "innocent", "INNOCENT", random.choice(INNOCENT_LINES)
    else:
        verdict, outcome, key, flavor = "HUNG JURY", "hung", "HUNG", random.choice(HUNG_LINES)

    record_result(chat_id, trial.defendant_id, trial.defendant_name, outcome, trial.crime)

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

    image = await make_image(generate_verdict_image, trial.defendant_name,
                             trial.avatar_bytes, key, guilty_count, innocent_count,
                             len(trial.jurors), trial.case_no)
    await post_image_or_text(context, chat_id, image, result_text + enforcement_note)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

async def post_init(application) -> None:
    application.bot_data["housekeeping"] = asyncio.create_task(housekeeping_loop())
    logger.info("Silly Court is now in session.")


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN environment variable is not set. "
            "Export it locally or set it in Render's dashboard — never hardcode it."
        )

    # Start the web server FIRST so Render's port check passes quickly,
    # then the self-ping loop that keeps the free service from sleeping.
    threading.Thread(target=start_health_check_server, daemon=True).start()
    threading.Thread(target=keep_alive_loop, daemon=True).start()

    app = (
        ApplicationBuilder()
        .token(token)
        .concurrent_updates(True)  # one slow avatar download never blocks the chat
        .post_init(post_init)
        .build()
    )

    # Only /indict and /rapsheet, only in groups. No /start handler on purpose:
    # the bot stays silent for everything else.
    group_only = filters.ChatType.GROUPS
    app.add_handler(CommandHandler("indict", indict, filters=group_only))
    app.add_handler(CommandHandler("rapsheet", rapsheet, filters=group_only))
    app.add_handler(CallbackQueryHandler(handle_join, pattern=r"^join\|"))
    app.add_handler(CallbackQueryHandler(handle_vote, pattern=r"^vote\|"))
    app.add_handler(CallbackQueryHandler(handle_objection, pattern=r"^obj\|"))

    logger.info("Starting Silly Court bot (long polling)...")
    # drop_pending_updates=True discards everything users sent while the bot was
    # offline/restarting (e.g. a Render redeploy), so old /indict commands are
    # never replayed.
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()