from __future__ import annotations

"""Advanced eFootball account marketplace bot.

Requires aiogram 3.7+ and Python 3.10+.
Credentials are read only from environment variables; never commit .env or tokens.
"""

import asyncio
import html
import json
import logging
import os
import re
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))
load_dotenv()

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto,
    FSInputFile, KeyboardButton, Message, ReplyKeyboardMarkup,
)
from i18n import merge_translations
from storage import SQLiteFSMStorage

# ------------------------------- Configuration -------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "@VXStorePes").strip()
CHANNEL_URL = "https://t.me/VXStorePes"
ADMIN_CONTACT_USERNAME = os.getenv("ADMIN_CONTACT_USERNAME", "Imaaad_hk9").lstrip("@")
DB_PATH = Path(os.getenv("DATABASE_PATH", "marketplace.sqlite3"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Set it in your environment; do not hardcode it.")
if not ADMIN_ID_RAW.isdigit():
    raise RuntimeError("ADMIN_ID must be your numeric Telegram user ID in the environment.")
ADMIN_ID = int(ADMIN_ID_RAW)

DEFAULT_MARKUP_USD = 2.0
DEFAULT_MARKUP_MAD = 20.0
MAD_PER_USD = 10.0


def _rate(code: str, default: float) -> float:
    """Optional override from .env, e.g. RATE_GBP=0.08 (meaning 1 MAD = 0.08 GBP)."""
    try:
        return float(os.getenv(f"RATE_{code}", default))
    except ValueError:
        return default


# Base prices are calculated in MAD, then converted to the user's currency.
# "rate" = how much of that currency equals 1 MAD. CHANGE THESE to your real rates
# (or set RATE_GBP / RATE_EGP / RATE_DZD in your .env).
CURRENCIES: dict[str, dict[str, Any]] = {
    "GBP": {"flag": "🇬🇧", "symbol": "£", "rate": _rate("GBP", 0.08), "decimals": 2, "markup": 1.5},
    "MAD": {"flag": "🇲🇦", "symbol": "MAD", "rate": 1.0, "decimals": 2, "markup": DEFAULT_MARKUP_MAD},
    "EGP": {"flag": "🇪🇬", "symbol": "EGP", "rate": _rate("EGP", 5.0), "decimals": 0, "markup": 100.0},
    "DZD": {"flag": "🇩🇿", "symbol": "DZD", "rate": _rate("DZD", 13.0), "decimals": 0, "markup": 260.0},
    "USD": {"flag": "🇺🇸", "symbol": "$", "rate": 1 / MAD_PER_USD, "decimals": 2, "markup": DEFAULT_MARKUP_USD},
}
SELECTABLE_CURRENCIES = ("GBP", "USD", "MAD", "EGP", "DZD")


def currency_code(value: Any) -> str:
    code = str(value or "MAD").upper()
    return code if code in CURRENCIES else "MAD"


def markup_setting_key(code: str) -> str:
    return f"markup_{currency_code(code).lower()}"


def default_markup(code: str) -> float:
    return float(CURRENCIES[currency_code(code)]["markup"])


def money(amount: float, code: str) -> str:
    code = currency_code(code)
    symbol = CURRENCIES[code]["symbol"]
    return f"{symbol}{amount:g}" if symbol in ("£", "$") else f"{amount:g} {symbol}"


def convert_from_mad(amount_mad: float, code: str) -> float:
    info = CURRENCIES[currency_code(code)]
    return float(round(float(amount_mad) * info["rate"], info["decimals"]))


def currency_button_text(code: str) -> str:
    info = CURRENCIES[code]
    label = code if info["symbol"] == code else f"{info['symbol']} {code}"
    return f"{info['flag']} {label}"
NON_FREE_PRICE_TABLE = {0: 0, 1: 20, 2: 50, 3: 80, 4: 110, 5: 140}
NON_FREE_BASE = 20
NON_FREE_STEP = 30
SPAM_MAX_MESSAGES = 12
SPAM_WINDOW_SECONDS = 10
MAX_PHOTOS = 10
MAX_TEXT_LENGTH = 1000

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("efootball_marketplace")

bot = Bot(
    BOT_TOKEN,
    session=AiohttpSession(timeout=30),
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=SQLiteFSMStorage(DB_PATH))
router = Router()
dp.include_router(router)
USER_LANGUAGES: dict[int, str] = {}
USER_CURRENCIES: dict[int, str] = {}
BOT_USERNAME = ""

# --------------------------------- Language -----------------------------------
TEXT = {
    "welcome": {"en": "⚽ <b>Welcome to the eFootball Account Marketplace!</b>\nSell your account by submitting its details and screenshots. Our team will review it before publication.", "ar": "⚽ <b>مرحباً بك في سوق حسابات eFootball!</b>\nأرسل تفاصيل الحساب وصوره، وسيقوم فريقنا بمراجعته قبل النشر."},
    "menu_sell": {"en": "📤 Sell Account", "ar": "📤 بيع حساب"},
    "menu_market": {"en": "🛒 Marketplace", "ar": "🛒 السوق"},
    "menu_mine": {"en": "📦 My submissions", "ar": "📦 طلباتي"},
    "menu_help": {"en": "❓ Help", "ar": "❓ المساعدة"},
    "ask_photo": {"en": "📸 <b>Step 1/7:</b> Send the main team screenshot.", "ar": "📸 <b>الخطوة 1/7:</b> أرسل صورة الفريق الرئيسية."},
    "ask_extra": {"en": "Send extra screenshots (up to 10 total), then press Done.", "ar": "أرسل صوراً إضافية (10 صور كحد أقصى)، ثم اضغط تم."},
    "done": {"en": "✅ Done", "ar": "✅ انتهيت"},
    "ask_managers": {"en": "👔 <b>Step 3/7:</b> How many managers does the account have? Send a number.", "ar": "👔 <b>الخطوة 3/7:</b> كم عدد المدربين في الحساب؟ أرسل رقماً."},
    "ask_players": {"en": "⭐ <b>Step 4/7:</b> How many non-free / premium players? Send a number, or choose manual price.", "ar": "⭐ <b>الخطوة 4/7:</b> كم عدد اللاعبين غير المجانيين؟ أرسل رقماً أو اختر إدخال السعر يدوياً."},
    "manual": {"en": "✏️ Enter price manually", "ar": "✏️ أدخل السعر يدوياً"},
    "price_manual_prompt": {"en": "Send your requested price as a number.", "ar": "أرسل السعر الذي تريده كرقم."},
    "price_confirm": {"en": "Calculated price: <b>{price} MAD</b>. Continue?", "ar": "السعر المحسوب: <b>{price} MAD</b>. هل تريد المتابعة؟"},
    "continue": {"en": "✅ Continue", "ar": "✅ متابعة"},
    "edit": {"en": "✏️ Edit count", "ar": "✏️ تعديل العدد"},
    "currency": {"en": "Choose your currency:", "ar": "اختر العملة:"},
    "payment": {"en": "💳 <b>Step 5/7:</b> Choose your preferred payment method:", "ar": "💳 <b>الخطوة 5/7:</b> اختر طريقة الدفع المناسبة:"},
    "other_payment": {"en": "Type your payment method:", "ar": "اكتب طريقة الدفع:"},
    "platform": {"en": "🎮 <b>Step 6/7:</b> Choose the account platform:", "ar": "🎮 <b>الخطوة 6/7:</b> اختر منصة الحساب:"},
    "description": {"en": "📝 <b>Step 7/7:</b> Send an optional account description, or skip.", "ar": "📝 <b>الخطوة 7/7:</b> أرسل وصفاً اختيارياً للحساب أو تخطَّ."},
    "skip": {"en": "⏭ Skip", "ar": "⏭ تخطي"},
    "cancelled": {"en": "Your current submission was cancelled.", "ar": "تم إلغاء الطلب الحالي."},
    "banned": {"en": "⛔ Your access to this bot is restricted. Contact the administrator if you think this is a mistake.", "ar": "⛔ تم تقييد استخدامك للبوت. تواصل مع المشرف إذا كنت تعتقد أن هذا خطأ."},
    "rate": {"en": "⚠️ You're sending messages too quickly. Please wait a moment and try again.", "ar": "⚠️ ترسل الرسائل بسرعة كبيرة. انتظر قليلاً ثم حاول مجدداً."},
    "help": {"en": "Commands: /start, /sell, /marketplace, /find KEYWORD, /mysubmissions, /status, /cancel, /language en|ar.\nAdmin: /dashboard, /pending, /stats, /markup, /setmarkup MAD USD, /ban ID [reason], /unban ID, /bans, /broadcast TEXT.", "ar": "الأوامر: /start و/sell و/marketplace و/find كلمة و/mysubmissions و/status و/cancel و/language en|ar.\nللمشرف: /dashboard و/pending و/stats و/markup و/setmarkup MAD USD و/ban ID و/unban ID و/bans و/broadcast TEXT."},
}
TEXT = merge_translations(TEXT)
TEXT.update({
    "choose_currency": {
        "en": "💱 <b>Choose your currency</b>\nAll prices will be shown in it. You can change it later with /currency.",
        "ar": "💱 <b>اختر عملتك</b>\nستظهر جميع الأسعار بها. يمكنك تغييرها لاحقاً عبر /currency.",
    },
    "currency_saved": {"en": "✅ Currency set to {currency}", "ar": "✅ تم تعيين العملة: {currency}"},
    "price_confirm_cur": {
        "en": "Calculated price: <b>{price}</b>. Continue?",
        "ar": "السعر المحسوب: <b>{price}</b>. هل تريد المتابعة؟",
    },
    "price_manual_prompt_cur": {
        "en": "Send your requested price in {currency} as a number.",
        "ar": "أرسل السعر الذي تريده بعملة {currency} كرقم.",
    },
})


def tr(key: str, telegram_user_id: int | None = None, **kwargs: Any) -> str:
    language = get_language(telegram_user_id) if telegram_user_id else "ar"
    value = TEXT.get(key, {}).get(language, TEXT.get(key, {}).get("en", key))
    return value.format(**kwargs)

def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def now_text() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

# ---------------------------------- Database ----------------------------------

def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


async def db_call(function, *args, **kwargs):
    """Run legacy SQLite helpers off the event loop."""
    return await asyncio.to_thread(function, *args, **kwargs)


def backup_before_schema_upgrade() -> None:
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        return
    try:
        with sqlite3.connect(DB_PATH) as source:
            tables = {
                row[0]
                for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "submissions" not in tables:
                return
            required_tables = {
                "submission_status_history",
                "admin_activity",
                "channel_publications",
                "schema_migrations",
                "fsm_sessions",
            }
            columns = {
                row[1] for row in source.execute("PRAGMA table_info(submissions)")
            }
            required = {"submission_key", "channel_id", "channel_message_ids"}
            if required.issubset(columns) and required_tables.issubset(tables):
                return
            backup_dir = DB_PATH.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_path = backup_dir / f"{DB_PATH.stem}-pre-upgrade-{stamp}.sqlite3"
            with sqlite3.connect(backup_path) as backup:
                source.backup(backup)
            log.info("Created pre-upgrade database backup at %s", backup_path)
    except sqlite3.Error:
        log.exception("Could not create the pre-upgrade SQLite backup")
        raise


def create_database_backup() -> Path:
    if not DB_PATH.exists():
        raise FileNotFoundError("The SQLite database does not exist yet")
    backup_dir = DB_PATH.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"{DB_PATH.stem}-manual-{stamp}.sqlite3"
    with sqlite3.connect(DB_PATH) as source, sqlite3.connect(backup_path) as backup:
        source.backup(backup)
    return backup_path


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    backup_before_schema_upgrade()
    with closing(connect()) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
            language TEXT NOT NULL DEFAULT 'en', created_at TEXT NOT NULL, last_seen TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, client_id INTEGER NOT NULL,
            client_name TEXT, client_username TEXT, photo_file_ids TEXT NOT NULL,
            price REAL NOT NULL, currency TEXT NOT NULL, non_free_count INTEGER,
            managers_count INTEGER, payment_method TEXT, platform TEXT, platform_key TEXT,
            description TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
            platform_status TEXT, proposed_price REAL, final_price REAL,
            channel_message_id INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            submission_key TEXT, channel_id TEXT, channel_message_ids TEXT NOT NULL DEFAULT '[]'
        );
        CREATE INDEX IF NOT EXISTS idx_submissions_client ON submissions(client_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(status);
        CREATE TABLE IF NOT EXISTS bans (
            user_id INTEGER PRIMARY KEY, reason TEXT, banned_at TEXT, username TEXT, full_name TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            submission_id INTEGER, kind TEXT NOT NULL, message TEXT NOT NULL,
            sent_at TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS submission_status_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            submission_id INTEGER NOT NULL,
            old_status TEXT,
            new_status TEXT NOT NULL,
            actor_id INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_status_history_submission
            ON submission_status_history(submission_id, id DESC);
        CREATE TABLE IF NOT EXISTS admin_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            submission_id INTEGER,
            details TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS channel_publications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            submission_id INTEGER NOT NULL UNIQUE,
            channel_id TEXT NOT NULL,
            message_ids TEXT NOT NULL,
            published_at TEXT NOT NULL,
            removed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );
        """)
        existing_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(submissions)")
        }
        new_columns = {
            "submission_key": "TEXT",
            "channel_id": "TEXT",
            "channel_message_ids": "TEXT NOT NULL DEFAULT '[]'",
        }
        for column, definition in new_columns.items():
            if column not in existing_columns:
                db.execute(f"ALTER TABLE submissions ADD COLUMN {column} {definition}")
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_submissions_submission_key ON submissions(submission_key)"
        )
        db.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(1,?)",
            (now_text(),),
        )
        user_columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
        if "currency" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN currency TEXT")
        for code in CURRENCIES:
            db.execute(
                "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                (markup_setting_key(code), str(default_markup(code))),
            )
        db.commit()


def migrate_legacy_json() -> None:
    """One-time best-effort import of the original bot's JSON data."""
    with closing(connect()) as db:
        if Path("submissions.json").exists() and db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 0:
            try:
                raw = json.loads(Path("submissions.json").read_text(encoding="utf-8"))
                for old_id, s in raw.get("submissions", {}).items():
                    db.execute("""INSERT OR IGNORE INTO submissions
                    (id,client_id,client_name,client_username,photo_file_ids,price,currency,non_free_count,managers_count,payment_method,platform,platform_key,description,status,platform_status,proposed_price,final_price,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                        int(old_id), int(s.get("client_id") or 0), s.get("client_name"), s.get("client_username"),
                        json.dumps(s.get("photo_file_ids", [s.get("photo_file_id")])), float(s.get("price", 0)),
                        s.get("currency", "MAD"), s.get("non_free_count"), s.get("managers_count"),
                        s.get("payment_method"), s.get("platform"), s.get("platform_key"), s.get("description", ""),
                        s.get("status", "pending"), s.get("platform_status"), s.get("proposed_price"), s.get("final_price"),
                        s.get("created_at", now_text()), now_text()))
            except Exception as exc:
                log.exception("Legacy submissions import failed: %s", exc)
        if Path("bans.json").exists() and db.execute("SELECT COUNT(*) FROM bans").fetchone()[0] == 0:
            try:
                for uid, info in json.loads(Path("bans.json").read_text(encoding="utf-8")).items():
                    db.execute("INSERT OR IGNORE INTO bans VALUES(?,?,?,?,?)", (int(uid), info.get("reason", ""), info.get("banned_at", ""), info.get("username", ""), info.get("full_name", "")))
            except Exception as exc:
                log.exception("Legacy bans import failed: %s", exc)
        if Path("settings.json").exists():
            try:
                settings = json.loads(Path("settings.json").read_text(encoding="utf-8"))
                for k in ("markup_usd", "markup_mad"):
                    if k in settings:
                        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, str(float(settings[k]))))
            except Exception as exc:
                log.exception("Legacy settings import failed: %s", exc)
        db.commit()


def get_setting(key: str, default: float) -> float:
    with closing(connect()) as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        try:
            return float(row["value"]) if row else default
        except (TypeError, ValueError):
            return default


def set_setting(key: str, value: float) -> None:
    with closing(connect()) as db:
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))
        db.commit()


def get_language(user_id: int | None) -> str:
    if user_id is None:
        return "en"
    return USER_LANGUAGES.get(user_id, "en")


def touch_user(user) -> bool:
    if not user:
        return False
    with closing(connect()) as db:
        row = db.execute("SELECT language FROM users WHERE user_id=?", (user.id,)).fetchone()
        language = row["language"] if row else ("ar" if (user.language_code or "").lower().startswith("ar") else "en")
        db.execute("""INSERT INTO users(user_id,username,full_name,language,created_at,last_seen)
        VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,full_name=excluded.full_name,last_seen=excluded.last_seen""",
        (user.id, user.username or "", user.full_name or "", language, now_text(), now_text()))
        db.commit()
    USER_LANGUAGES[user.id] = language
    return row is None


def set_language(user_id: int, language: str) -> None:
    with closing(connect()) as db:
        db.execute("INSERT INTO users(user_id,language,created_at,last_seen) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET language=excluded.language,last_seen=excluded.last_seen", (user_id, language, now_text(), now_text()))
        db.commit()
    USER_LANGUAGES[user_id] = language


def load_user_languages() -> dict[int, str]:
    with closing(connect()) as db:
        rows = db.execute("SELECT user_id,language,currency FROM users").fetchall()
    loaded = {
        int(row["user_id"]): row["language"]
        for row in rows
        if row["language"] in ("en", "ar")
    }
    USER_LANGUAGES.update(loaded)
    for row in rows:
        if row["currency"] in SELECTABLE_CURRENCIES:
            USER_CURRENCIES[int(row["user_id"])] = row["currency"]
    return loaded


def get_currency(user_id: int | None) -> str | None:
    """The currency the user picked, or None if they have not chosen one yet."""
    if user_id is None:
        return None
    return USER_CURRENCIES.get(user_id)


def set_currency(user_id: int, currency: str) -> None:
    with closing(connect()) as db:
        db.execute(
            "INSERT INTO users(user_id,currency,created_at,last_seen) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET currency=excluded.currency,last_seen=excluded.last_seen",
            (user_id, currency, now_text(), now_text()),
        )
        db.commit()
    USER_CURRENCIES[user_id] = currency


async def markups_text(uid: int) -> str:
    lines = ["💰 <b>Markup per currency</b>"]
    for code in CURRENCIES:
        value = await db_call(get_setting, markup_setting_key(code), default_markup(code))
        lines.append(f"• {code}: {value:g}")
    lines.append("\nChange one: /setmarkup GBP 1.5\nLegacy form: /setmarkup MAD USD")
    return "\n".join(lines)


def get_submission(sid: int) -> dict | None:
    with closing(connect()) as db:
        row = db.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
    return row_to_submission(row) if row else None


def row_to_submission(row: sqlite3.Row) -> dict:
    data = dict(row)
    try:
        data["photo_file_ids"] = json.loads(data.get("photo_file_ids") or "[]")
    except json.JSONDecodeError:
        data["photo_file_ids"] = []
    try:
        data["channel_message_ids"] = json.loads(data.get("channel_message_ids") or "[]")
    except json.JSONDecodeError:
        data["channel_message_ids"] = []
    return data


def list_submissions(
    where: str = "1=1",
    params: tuple = (),
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    with closing(connect()) as db:
        rows = db.execute(
            f"SELECT * FROM submissions WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, max(0, offset)),
        ).fetchall()
    return [row_to_submission(r) for r in rows]


def create_submission(data: dict, user) -> int:
    submission_key = str(data.get("submission_key") or uuid4())
    with closing(connect()) as db:
        cur = db.execute("""INSERT OR IGNORE INTO submissions
        (client_id,client_name,client_username,photo_file_ids,price,currency,non_free_count,managers_count,payment_method,platform,platform_key,description,status,created_at,updated_at,submission_key)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            user.id, user.full_name, user.username or "", json.dumps(data["photo_file_ids"]), data["price"], data["currency"],
            data.get("non_free_count"), data.get("managers_count"), data.get("payment_method"), data.get("platform"),
            data.get("platform_key"), data.get("description", ""), "pending", now_text(), now_text(), submission_key))
        db.commit()
        if cur.rowcount:
            sid = int(cur.lastrowid)
            db.execute(
                "INSERT INTO submission_status_history(submission_id,old_status,new_status,actor_id,created_at) VALUES(?,?,?,?,?)",
                (sid, None, "pending", user.id, now_text()),
            )
            db.commit()
            return sid
        row = db.execute("SELECT id FROM submissions WHERE submission_key=?", (submission_key,)).fetchone()
        return int(row["id"])


def update_submission(sid: int, **fields: Any) -> None:
    allowed = {
        "status", "proposed_price", "final_price", "platform_status",
        "channel_message_id", "channel_id", "channel_message_ids",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    if isinstance(updates.get("channel_message_ids"), list):
        updates["channel_message_ids"] = json.dumps(updates["channel_message_ids"])
    updates["updated_at"] = now_text()
    sql = ",".join(f"{k}=?" for k in updates)
    with closing(connect()) as db:
        old = db.execute("SELECT status FROM submissions WHERE id=?", (sid,)).fetchone()
        db.execute(f"UPDATE submissions SET {sql} WHERE id=?", (*updates.values(), sid))
        if old and "status" in updates and old["status"] != updates["status"]:
            db.execute(
                "INSERT INTO submission_status_history(submission_id,old_status,new_status,actor_id,created_at) VALUES(?,?,?,?,?)",
                (sid, old["status"], updates["status"], None, now_text()),
            )
        db.commit()


def claim_publication(sid: int, final_price: float) -> dict[str, Any] | None:
    with closing(connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
        if not row or row["status"] not in (
            "pending",
            "awaiting_client_response",
            "publish_failed",
        ):
            db.rollback()
            return None
        old_status = row["status"]
        db.execute(
            "UPDATE submissions SET status='publishing',final_price=?,updated_at=? WHERE id=?",
            (final_price, now_text(), sid),
        )
        db.execute(
            "INSERT INTO submission_status_history(submission_id,old_status,new_status,actor_id,created_at) VALUES(?,?,?,?,?)",
            (sid, old_status, "publishing", ADMIN_ID, now_text()),
        )
        db.commit()
        updated = db.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
    return row_to_submission(updated) if updated else None


def save_publication(sid: int, final_price: float, channel_id: str, message_ids: list[int]) -> None:
    encoded_ids = json.dumps(message_ids)
    with closing(connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT status FROM submissions WHERE id=?", (sid,)).fetchone()
        if not row:
            db.rollback()
            raise ValueError(f"Submission {sid} disappeared during publication")
        db.execute(
            """UPDATE submissions SET status='accepted',final_price=?,channel_message_id=?,
               channel_id=?,channel_message_ids=?,updated_at=? WHERE id=?""",
            (
                final_price,
                message_ids[0] if message_ids else None,
                channel_id,
                encoded_ids,
                now_text(),
                sid,
            ),
        )
        if row["status"] != "accepted":
            db.execute(
                "INSERT INTO submission_status_history(submission_id,old_status,new_status,actor_id,created_at) VALUES(?,?,?,?,?)",
                (sid, row["status"], "accepted", ADMIN_ID, now_text()),
            )
        db.execute(
            """INSERT INTO channel_publications(submission_id,channel_id,message_ids,published_at,removed_at)
               VALUES(?,?,?,?,NULL)
               ON CONFLICT(submission_id) DO UPDATE SET channel_id=excluded.channel_id,
               message_ids=excluded.message_ids,published_at=excluded.published_at,removed_at=NULL""",
            (sid, channel_id, encoded_ids, now_text()),
        )
        db.commit()


def mark_submission_removed(sid: int, status: str) -> None:
    if status not in ("removed", "deleted"):
        raise ValueError("Invalid removed status")
    with closing(connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT status FROM submissions WHERE id=?", (sid,)).fetchone()
        if not row:
            db.rollback()
            raise ValueError(f"Submission {sid} does not exist")
        if row["status"] != status:
            db.execute(
                "UPDATE submissions SET status=?,updated_at=? WHERE id=?",
                (status, now_text(), sid),
            )
            db.execute(
                "INSERT INTO submission_status_history(submission_id,old_status,new_status,actor_id,created_at) VALUES(?,?,?,?,?)",
                (sid, row["status"], status, ADMIN_ID, now_text()),
            )
        db.execute(
            "UPDATE channel_publications SET removed_at=? WHERE submission_id=?",
            (now_text(), sid),
        )
        db.commit()


def delete_ban(user_id: int) -> bool:
    with closing(connect()) as db:
        cursor = db.execute("DELETE FROM bans WHERE user_id=?", (user_id,))
        db.commit()
        return cursor.rowcount > 0


def is_banned(user_id: int) -> bool:
    with closing(connect()) as db:
        return db.execute("SELECT 1 FROM bans WHERE user_id=?", (user_id,)).fetchone() is not None


def ban_user(user_id: int, reason: str, user=None) -> None:
    with closing(connect()) as db:
        db.execute("INSERT OR REPLACE INTO bans VALUES(?,?,?,?,?)", (user_id, reason, now_text(), getattr(user, "username", "") or "", getattr(user, "full_name", "") or ""))
        db.commit()


def record_notification(user_id: int, sid: int | None, kind: str, message: str, delivered: bool) -> None:
    with closing(connect()) as db:
        db.execute("INSERT INTO notifications(user_id,submission_id,kind,message,sent_at,delivered) VALUES(?,?,?,?,?,?)", (user_id, sid, kind, message[:4000], now_text(), int(delivered)))
        db.commit()

# --------------------------------- UI helpers ---------------------------------

def main_menu(uid: int) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text=tr("menu_market", uid)), KeyboardButton(text=tr("menu_sell", uid))],
        [KeyboardButton(text=tr("menu_mine", uid)), KeyboardButton(text=tr("menu_channel", uid))],
        [KeyboardButton(text=tr("menu_language", uid)), KeyboardButton(text=tr("menu_help", uid))],
        [KeyboardButton(text=tr("menu_support", uid)), KeyboardButton(text=tr("menu_cancel", uid))],
    ], resize_keyboard=True)


def language_kb(uid: int | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=tr("language_english", uid), callback_data="language:set:en"),
        InlineKeyboardButton(text=tr("language_arabic", uid), callback_data="language:set:ar"),
    ]])


def support_url() -> str | None:
    username = ADMIN_CONTACT_USERNAME.strip().lstrip("@")
    if not username:
        return None
    return f"https://t.me/{username}"


def marketplace_keyboard(uid: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=tr("open_marketplace", uid),
        url=CHANNEL_URL,
    )]]
    contact_url = support_url()
    if contact_url:
        rows.append([InlineKeyboardButton(text=tr("support", uid), url=contact_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def channel_cta_keyboard(uid: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=tr("channel_marketplace_button", uid),
        url=CHANNEL_URL,
    )]]
    contact = support_url()
    if contact:
        rows.append([InlineKeyboardButton(
            text=tr("channel_support_button", uid),
            url=contact,
        )])
    if BOT_USERNAME:
        rows.append([InlineKeyboardButton(
            text=tr("channel_bot_button", uid),
            url=f"https://t.me/{BOT_USERNAME}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_dashboard_kb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=tr("dashboard_statistics", uid), callback_data="admin:stats"),
         InlineKeyboardButton(text=tr("dashboard_pending", uid), callback_data="admin:pending:0")],
        [InlineKeyboardButton(text=tr("dashboard_all", uid), callback_data="admin:all:0"),
         InlineKeyboardButton(text=tr("dashboard_published", uid), callback_data="admin:published:0")],
        [InlineKeyboardButton(text=tr("dashboard_users", uid), callback_data="admin:users:0"),
         InlineKeyboardButton(text=tr("dashboard_bans", uid), callback_data="admin:bans:0")],
        [InlineKeyboardButton(text=tr("dashboard_pricing", uid), callback_data="admin:pricing"),
         InlineKeyboardButton(text=tr("dashboard_channel", uid), callback_data="admin:channel")],
        [InlineKeyboardButton(text=tr("dashboard_broadcast", uid), callback_data="admin:broadcast"),
         InlineKeyboardButton(text=tr("dashboard_settings", uid), callback_data="admin:settings")],
        [InlineKeyboardButton(text=tr("dashboard_refresh", uid), callback_data="dashboard_refresh")],
    ])


def photo_controls(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=tr("replace_main_photo", uid), callback_data="photos_replace_main"),
         InlineKeyboardButton(text=tr("remove_last_photo", uid), callback_data="photos_remove_last")],
        [InlineKeyboardButton(text=tr("done", uid), callback_data="photos_done"),
         InlineKeyboardButton(text=tr("menu_cancel", uid), callback_data="submission_cancel")],
    ])


def confirmation_kb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=tr("confirm_submit", uid), callback_data="submission_confirm")],
        [InlineKeyboardButton(text=tr("back", uid), callback_data="submission_back"),
         InlineKeyboardButton(text=tr("menu_cancel", uid), callback_data="submission_cancel")],
    ])


def cancel_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=tr("menu_cancel", uid), callback_data="submission_cancel")
    ]])


def inline_done(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=tr("done", uid), callback_data="photos_done")]])


def currency_kb(uid: int) -> InlineKeyboardMarkup:
    """Currency choice inside the sell flow (only shown if the user has no saved currency)."""
    buttons = [InlineKeyboardButton(text=currency_button_text(c), callback_data=f"currency_{c}") for c in SELECTABLE_CURRENCIES]
    return InlineKeyboardMarkup(inline_keyboard=[
        buttons[:2],
        buttons[2:4],
        buttons[4:],
        [InlineKeyboardButton(text=tr("menu_cancel", uid), callback_data="submission_cancel")],
    ])


def currency_picker_kb() -> InlineKeyboardMarkup:
    """Currency picker for first-time users and the /currency command."""
    buttons = [InlineKeyboardButton(text=currency_button_text(c), callback_data=f"setcur:{c}") for c in SELECTABLE_CURRENCIES]
    return InlineKeyboardMarkup(inline_keyboard=[buttons[:2], buttons[2:4], buttons[4:]])


def payment_kb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=tr("pay_paypal", uid), callback_data="pay_paypal"),
         InlineKeyboardButton(text=tr("pay_crypto", uid), callback_data="pay_crypto")],
        [InlineKeyboardButton(text=tr("pay_cih", uid), callback_data="pay_cih"),
         InlineKeyboardButton(text=tr("pay_orange", uid), callback_data="pay_orange")],
        [InlineKeyboardButton(text=tr("pay_other", uid), callback_data="pay_other")],
        [InlineKeyboardButton(text=tr("menu_cancel", uid), callback_data="submission_cancel")],
    ])


def platform_kb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=tr("platform_konami", uid), callback_data="platform_konami")],
        [InlineKeyboardButton(text=tr("platform_google", uid), callback_data="platform_google")],
        [InlineKeyboardButton(text=tr("menu_cancel", uid), callback_data="submission_cancel")],
    ])


def admin_actions(sid: int, amount: float, currency: str, uid: int, seller_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{tr('admin_approve', uid)} ({money(amount, currency)})", callback_data=f"admin_accept:{sid}")],
        [InlineKeyboardButton(text=tr("admin_custom_price", uid), callback_data=f"admin_custom:{sid}"),
         InlineKeyboardButton(text=tr("admin_reject", uid), callback_data=f"admin_reject:{sid}")],
        [InlineKeyboardButton(text=tr("admin_contact_seller", uid), url=f"tg://user?id={seller_id}")],
    ])


def platform_result_kb(sid: int, uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=tr("platform_konami", uid), callback_data=f"plat_konami:{sid}"),
        InlineKeyboardButton(text=tr("platform_google", uid), callback_data=f"plat_google:{sid}"),
    ]])


def price_from_non_free_count(count: int) -> float:
    if count in NON_FREE_PRICE_TABLE:
        return float(NON_FREE_PRICE_TABLE[count])
    return 0.0 if count <= 0 else float(NON_FREE_BASE + (count - 1) * NON_FREE_STEP)


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

# -------------------------------- Rate limiting -------------------------------
_message_times: dict[int, deque[float]] = defaultdict(deque)


def rate_limited(user_id: int) -> bool:
    now = time.monotonic()
    q = _message_times[user_id]
    while q and now - q[0] > SPAM_WINDOW_SECONDS:
        q.popleft()
    q.append(now)
    return len(q) > SPAM_MAX_MESSAGES


async def guard_message(message: Message, state: FSMContext | None = None) -> bool:
    uid = message.from_user.id
    await db_call(touch_user, message.from_user)
    if is_admin(uid):
        return False
    if await db_call(is_banned, uid):
        if state:
            await state.clear()
        await message.answer(tr("banned", uid))
        return True
    if rate_limited(uid):
        if state:
            await state.clear()
        await message.answer(tr("rate", uid))
        log.warning("Rate limit triggered for user_id=%s", uid)
        try:
            await bot.send_message(
                ADMIN_ID,
                tr(
                    "rate_limit_admin",
                    ADMIN_ID,
                    id=uid,
                    username=esc(message.from_user.username or "-"),
                ),
            )
        except Exception:
            pass
        return True
    return False


async def notify_user(user_id: int, sid: int | None, kind: str, text: str, reply_markup=None) -> bool:
    delivered = False
    try:
        await bot.send_message(user_id, text, reply_markup=reply_markup)
        delivered = True
    except Exception as exc:
        log.warning("Notification delivery failed user=%s kind=%s: %s", user_id, kind, exc)
    await db_call(record_notification, user_id, sid, kind, text, delivered)
    return delivered

# --------------------------------- FSM states ---------------------------------
class SubmitFlow(StatesGroup):
    waiting_platform = State()
    waiting_photo = State()
    waiting_photo_replace = State()
    waiting_extra_photos = State()
    waiting_managers_count = State()
    waiting_non_free_count = State()
    waiting_manual_price = State()
    waiting_currency = State()
    waiting_payment = State()
    waiting_other_payment = State()
    waiting_description = State()
    waiting_confirmation = State()


class AdminCustomPrice(StatesGroup):
    waiting_price = State()


class AdminBroadcast(StatesGroup):
    waiting_confirmation = State()


# --------------------------------- User flows ---------------------------------
async def begin_sell(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(submission_key=str(uuid4()))
    await state.set_state(SubmitFlow.waiting_platform)
    await message.answer(
        tr("platform", message.from_user.id),
        reply_markup=platform_kb(message.from_user.id),
    )


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    is_new_user = await asyncio.to_thread(touch_user, message.from_user)
    if await asyncio.to_thread(is_banned, message.from_user.id):
        await message.answer(tr("banned", message.from_user.id))
        return
    await state.clear()
    if is_new_user:
        await message.answer(
            tr("choose_language", message.from_user.id),
            reply_markup=language_kb(message.from_user.id),
        )
        return
    if not get_currency(message.from_user.id):
        await message.answer(
            tr("choose_currency", message.from_user.id),
            reply_markup=currency_picker_kb(),
        )
        return
    await message.answer(tr("welcome", message.from_user.id), reply_markup=main_menu(message.from_user.id))


@router.message(Command("sell"))
async def sell_command(message: Message, state: FSMContext):
    if not await guard_message(message, state):
        await begin_sell(message, state)


@router.message(Command("help"))
async def help_command(message: Message, state: FSMContext):
    if await guard_message(message, state):
        return
    await message.answer(tr("help", message.from_user.id), reply_markup=main_menu(message.from_user.id))


@router.message(Command("language"))
async def language_command(message: Message):
    await asyncio.to_thread(touch_user, message.from_user)
    await message.answer(
        tr("usage_language", message.from_user.id),
        reply_markup=language_kb(message.from_user.id),
    )


@router.callback_query(F.data.startswith("language:set:"))
async def language_selected(callback: CallbackQuery):
    language = callback.data.rsplit(":", 1)[-1]
    if language not in ("en", "ar"):
        await callback.answer(tr("invalid_page", callback.from_user.id), show_alert=True)
        return
    await asyncio.to_thread(set_language, callback.from_user.id, language)
    await callback.answer(tr("language_updated", callback.from_user.id))
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if not get_currency(callback.from_user.id):
        # First time here: language is chosen, now ask for the currency.
        await callback.message.answer(
            tr("choose_currency", callback.from_user.id),
            reply_markup=currency_picker_kb(),
        )
        return
    await callback.message.answer(
        tr("welcome", callback.from_user.id),
        reply_markup=main_menu(callback.from_user.id),
    )


@router.message(Command("currency"))
async def currency_command(message: Message):
    await asyncio.to_thread(touch_user, message.from_user)
    if await asyncio.to_thread(is_banned, message.from_user.id):
        await message.answer(tr("banned", message.from_user.id))
        return
    await message.answer(
        tr("choose_currency", message.from_user.id),
        reply_markup=currency_picker_kb(),
    )


@router.callback_query(F.data.startswith("setcur:"))
async def currency_selected(callback: CallbackQuery):
    uid = callback.from_user.id
    code = callback.data.split(":", 1)[1]
    if code not in SELECTABLE_CURRENCIES:
        await callback.answer(tr("invalid_currency", uid), show_alert=True)
        return
    await asyncio.to_thread(set_currency, uid, code)
    await callback.answer(tr("currency_saved", uid, currency=code))
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(tr("welcome", uid), reply_markup=main_menu(uid))


@router.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext):
    if await guard_message(message, state):
        return
    await state.clear()
    await message.answer(tr("cancelled", message.from_user.id), reply_markup=main_menu(message.from_user.id))


@router.message(F.text.in_({"❌ Cancel", "❌ إلغاء"}))
async def cancel_menu_button(message: Message, state: FSMContext):
    if await guard_message(message, state):
        return
    await state.clear()
    await message.answer(
        tr("cancelled", message.from_user.id),
        reply_markup=main_menu(message.from_user.id),
    )


@router.message(StateFilter(None), F.text)
async def menu_router(message: Message, state: FSMContext):
    if await guard_message(message, state):
        return
    text = message.text.strip()
    uid = message.from_user.id
    if text in (tr("menu_sell", uid), "➕ Sell an account", "➕ بيع حساب"):
        await begin_sell(message, state)
    elif text in (tr("menu_market", uid), "🛍 Marketplace", "🛍 السوق"):
        await show_marketplace(message, query="")
    elif text in (tr("menu_mine", uid), "📦 My submissions", "📦 طلباتي"):
        await show_my_submissions(message)
    elif text in (tr("menu_help", uid), "ℹ️ Help", "❓ Help", "ℹ️ المساعدة", "❓ المساعدة"):
        await message.answer(tr("help", uid), reply_markup=main_menu(uid))
    elif text in (tr("menu_channel", uid), "📢 Our Channel", "📢 قناتنا"):
        await message.answer(
            tr("channel_info", uid),
            reply_markup=marketplace_keyboard(uid),
        )
    elif text in (tr("menu_language", uid), "🌐 Language", "🌐 اللغة"):
        await message.answer(
            tr("choose_language", uid),
            reply_markup=language_kb(uid),
        )
    elif text in (tr("menu_support", uid), "📞 Contact Support", "📞 تواصل مع الدعم"):
        contact_url = support_url()
        if contact_url:
            await message.answer(
                tr("support", uid),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text=tr("support", uid), url=contact_url)
                ]]),
            )
        else:
            await message.answer(tr("support_unavailable", uid))


@router.callback_query(F.data == "submission_cancel")
async def submission_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer(tr("confirm_cancelled", callback.from_user.id))
    await callback.message.answer(
        tr("cancelled", callback.from_user.id),
        reply_markup=main_menu(callback.from_user.id),
    )


@router.message(SubmitFlow.waiting_photo, F.photo)
async def got_main_photo(message: Message, state: FSMContext):
    if await guard_message(message, state): return
    await state.update_data(photo_file_ids=[message.photo[-1].file_id])
    await state.set_state(SubmitFlow.waiting_extra_photos)
    await message.answer(tr("ask_extra", message.from_user.id), reply_markup=photo_controls(message.from_user.id))


@router.message(SubmitFlow.waiting_photo_replace, F.photo)
async def replace_main_photo(message: Message, state: FSMContext):
    if await guard_message(message, state):
        return
    data = await state.get_data()
    photos = data.get("photo_file_ids", [])
    if photos:
        photos[0] = message.photo[-1].file_id
    else:
        photos = [message.photo[-1].file_id]
    await state.update_data(photo_file_ids=photos)
    await state.set_state(SubmitFlow.waiting_extra_photos)
    await message.answer(
        tr("photo_replaced", message.from_user.id),
        reply_markup=photo_controls(message.from_user.id),
    )


@router.message(SubmitFlow.waiting_photo)
async def need_photo(message: Message):
    await message.answer(
        tr("send_photo", message.from_user.id),
        reply_markup=cancel_keyboard(message.from_user.id),
    )


@router.message(SubmitFlow.waiting_photo_replace)
async def need_replacement_photo(message: Message):
    await message.answer(
        tr("send_photo", message.from_user.id),
        reply_markup=cancel_keyboard(message.from_user.id),
    )


@router.message(SubmitFlow.waiting_extra_photos, F.photo)
async def got_extra_photo(message: Message, state: FSMContext):
    if await guard_message(message, state): return
    data = await state.get_data()
    photos = data.get("photo_file_ids", [])
    if len(photos) >= MAX_PHOTOS:
        await message.answer(
            tr("max_photos", message.from_user.id, count=MAX_PHOTOS),
            reply_markup=photo_controls(message.from_user.id),
        )
        return
    photos.append(message.photo[-1].file_id)
    await state.update_data(photo_file_ids=photos)
    await message.answer(
        tr("photo_count", message.from_user.id, count=len(photos), maximum=MAX_PHOTOS),
        reply_markup=photo_controls(message.from_user.id),
    )


@router.message(SubmitFlow.waiting_extra_photos)
async def extra_not_photo(message: Message):
    await message.answer(
        tr("photos_or_done", message.from_user.id),
        reply_markup=photo_controls(message.from_user.id),
    )


@router.callback_query(SubmitFlow.waiting_extra_photos, F.data == "photos_replace_main")
async def request_main_photo_replacement(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SubmitFlow.waiting_photo_replace)
    await callback.answer()
    await callback.message.answer(
        tr("ask_photo", callback.from_user.id),
        reply_markup=cancel_keyboard(callback.from_user.id),
    )


@router.callback_query(SubmitFlow.waiting_extra_photos, F.data == "photos_remove_last")
async def remove_last_photo(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photo_file_ids", [])
    if len(photos) > 1:
        photos.pop()
        await state.update_data(photo_file_ids=photos)
    await callback.answer()
    await callback.message.answer(
        tr("photo_removed", callback.from_user.id, count=len(photos)),
        reply_markup=photo_controls(callback.from_user.id),
    )


@router.callback_query(SubmitFlow.waiting_extra_photos, F.data == "photos_done")
async def photos_done(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(SubmitFlow.waiting_managers_count)
    await callback.message.answer(
        tr("ask_managers", callback.from_user.id),
        reply_markup=cancel_keyboard(callback.from_user.id),
    )


@router.message(SubmitFlow.waiting_managers_count)
async def managers_count(message: Message, state: FSMContext):
    if await guard_message(message, state): return
    if not message.text or not message.text.isdigit() or int(message.text) > 100:
        await message.answer(
            tr("valid_count", message.from_user.id),
            reply_markup=cancel_keyboard(message.from_user.id),
        )
        return
    await state.update_data(managers_count=int(message.text))
    await state.set_state(SubmitFlow.waiting_non_free_count)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=tr("manual", message.from_user.id), callback_data="manual_price")],
        [InlineKeyboardButton(text=tr("menu_cancel", message.from_user.id), callback_data="submission_cancel")],
    ])
    await message.answer(tr("ask_players", message.from_user.id), reply_markup=kb)


@router.message(SubmitFlow.waiting_non_free_count)
async def non_free_count(message: Message, state: FSMContext):
    if await guard_message(message, state): return
    if not message.text or not message.text.isdigit() or int(message.text) > 100:
        await message.answer(
            tr("valid_count", message.from_user.id),
            reply_markup=cancel_keyboard(message.from_user.id),
        )
        return
    count = int(message.text)
    await state.update_data(non_free_count=count, price_mad=price_from_non_free_count(count))
    await state.set_state(SubmitFlow.waiting_currency)
    shown_currency = get_currency(message.from_user.id) or "MAD"
    shown_price = convert_from_mad(price_from_non_free_count(count), shown_currency)
    await message.answer(tr("price_confirm_cur", message.from_user.id, price=money(shown_price, shown_currency)), reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=tr("continue", message.from_user.id), callback_data="price_continue")],
        [InlineKeyboardButton(text=tr("edit", message.from_user.id), callback_data="price_edit")],
    ]))


@router.callback_query(SubmitFlow.waiting_non_free_count, F.data == "manual_price")
async def manual_price_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(non_free_count=None)
    await state.set_state(SubmitFlow.waiting_manual_price)
    saved_currency = get_currency(callback.from_user.id)
    await callback.message.answer(
        tr("price_manual_prompt_cur", callback.from_user.id, currency=saved_currency)
        if saved_currency else tr("price_manual_prompt", callback.from_user.id),
        reply_markup=cancel_keyboard(callback.from_user.id),
    )


@router.callback_query(SubmitFlow.waiting_currency, F.data == "price_edit")
async def price_edit(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(SubmitFlow.waiting_non_free_count)
    await callback.message.answer(
        tr("ask_players", callback.from_user.id),
        reply_markup=cancel_keyboard(callback.from_user.id),
    )


@router.callback_query(SubmitFlow.waiting_currency, F.data == "price_continue")
async def price_continue(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(price=data.get("price_mad", 0), currency="MAD", price_is_calculated=True)
    await state.set_state(SubmitFlow.waiting_currency)
    await callback.answer()
    saved_currency = get_currency(callback.from_user.id)
    if saved_currency:
        await apply_currency(callback.message, state, callback.from_user.id, saved_currency)
        return
    await callback.message.answer(
        tr("currency", callback.from_user.id),
        reply_markup=currency_kb(callback.from_user.id),
    )


async def apply_currency(message: Message, state: FSMContext, uid: int, currency: str) -> None:
    """Store the chosen currency (converting calculated MAD prices) and go to payment."""
    data = await state.get_data()
    price = float(data.get("price", data.get("price_mad", 0)))
    if data.get("price_is_calculated"):
        price = convert_from_mad(float(data.get("price_mad", price)), currency)
    await state.update_data(currency=currency, price=price)
    await state.set_state(SubmitFlow.waiting_payment)
    await message.answer(tr("payment", uid), reply_markup=payment_kb(uid))


@router.message(SubmitFlow.waiting_manual_price)
async def manual_price_received(message: Message, state: FSMContext):
    if await guard_message(message, state): return
    try:
        price = float(message.text.strip().replace(",", "."))
        if not (0 < price <= 1_000_000): raise ValueError
    except (ValueError, AttributeError):
        await message.answer(
            tr("valid_price", message.from_user.id),
            reply_markup=cancel_keyboard(message.from_user.id),
        )
        return
    await state.update_data(price=price, price_is_calculated=False)
    await state.set_state(SubmitFlow.waiting_currency)
    saved_currency = get_currency(message.from_user.id)
    if saved_currency:
        await apply_currency(message, state, message.from_user.id, saved_currency)
        return
    await message.answer(
        tr("currency", message.from_user.id),
        reply_markup=currency_kb(message.from_user.id),
    )


@router.callback_query(SubmitFlow.waiting_currency, F.data.startswith("currency_"))
async def currency_choice(callback: CallbackQuery, state: FSMContext):
    currency = callback.data.split("_", 1)[1]
    if currency not in SELECTABLE_CURRENCIES:
        await callback.answer(tr("invalid_currency", callback.from_user.id), show_alert=True); return
    await asyncio.to_thread(set_currency, callback.from_user.id, currency)
    await callback.answer()
    await apply_currency(callback.message, state, callback.from_user.id, currency)


PAYMENT_METHODS = {"paypal": "PayPal", "crypto": "Crypto", "cih": "CIH Bank", "orange": "Orange Money"}


@router.callback_query(SubmitFlow.waiting_payment, F.data.startswith("pay_"))
async def payment_choice(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split("_", 1)[1]
    await callback.answer()
    if key == "other":
        await state.set_state(SubmitFlow.waiting_other_payment)
        await callback.message.answer(
            tr("other_payment", callback.from_user.id),
            reply_markup=cancel_keyboard(callback.from_user.id),
        )
        return
    if key not in PAYMENT_METHODS:
        return
    await state.update_data(payment_method=PAYMENT_METHODS[key])
    await state.set_state(SubmitFlow.waiting_description)
    await callback.message.answer(
        tr("description", callback.from_user.id),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=tr("skip", callback.from_user.id), callback_data="skip_description")],
            [InlineKeyboardButton(text=tr("menu_cancel", callback.from_user.id), callback_data="submission_cancel")],
        ]),
    )


@router.message(SubmitFlow.waiting_other_payment)
async def other_payment(message: Message, state: FSMContext):
    if await guard_message(message, state): return
    method = (message.text or "").strip()[:80]
    if not method:
        await message.answer(
            tr("payment_required", message.from_user.id),
            reply_markup=cancel_keyboard(message.from_user.id),
        )
        return
    await state.update_data(payment_method=method)
    await state.set_state(SubmitFlow.waiting_description)
    await message.answer(
        tr("description", message.from_user.id),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=tr("skip", message.from_user.id), callback_data="skip_description")],
            [InlineKeyboardButton(text=tr("menu_cancel", message.from_user.id), callback_data="submission_cancel")],
        ]),
    )


@router.callback_query(SubmitFlow.waiting_platform, F.data.startswith("platform_"))
async def platform_choice(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split("_", 1)[1]
    if key not in ("konami", "google"):
        await callback.answer(tr("submission_unavailable", callback.from_user.id), show_alert=True); return
    label = tr("platform_konami" if key == "konami" else "platform_google", callback.from_user.id)
    await state.update_data(platform=label, platform_key=key)
    await state.set_state(SubmitFlow.waiting_photo)
    await callback.answer()
    await callback.message.answer(
        tr("ask_photo", callback.from_user.id),
        reply_markup=cancel_keyboard(callback.from_user.id),
    )


@router.message(SubmitFlow.waiting_description)
async def description_received(message: Message, state: FSMContext):
    if await guard_message(message, state): return
    description = (message.text or "").strip()
    if len(description) > MAX_TEXT_LENGTH:
        await message.answer(
            tr("description_too_long", message.from_user.id, maximum=MAX_TEXT_LENGTH),
            reply_markup=cancel_keyboard(message.from_user.id),
        )
        return
    await state.update_data(description=description)
    await show_submission_confirmation(message, state)


@router.callback_query(SubmitFlow.waiting_description, F.data == "skip_description")
async def skip_description(callback: CallbackQuery, state: FSMContext):
    await state.update_data(description="")
    await callback.answer()
    await show_submission_confirmation(callback.message, state, user=callback.from_user)


async def show_submission_confirmation(message: Message, state: FSMContext, user=None):
    data = await state.get_data()
    user = user or message.from_user
    currency = data.get("currency", "MAD")
    price = float(data.get("price", 0))
    photos = data.get("photo_file_ids", [])
    summary = tr(
        "confirm_summary",
        user.id,
        platform=esc(data.get("platform", "—")),
        photos=len(photos),
        managers=data.get("managers_count", "—"),
        players=data.get("non_free_count") if data.get("non_free_count") is not None else "—",
        price=f"{price:g}",
        currency=esc(currency),
        payment=esc(data.get("payment_method", "—")),
        description=esc(data.get("description") or "—"),
    )
    try:
        if photos:
            if len(photos) == 1:
                await bot.send_photo(
                    user.id,
                    photos[0],
                    caption=tr("confirm_title", user.id),
                )
            else:
                media = [InputMediaPhoto(media=file_id) for file_id in photos]
                media[0].caption = tr("confirm_title", user.id)
                await bot.send_media_group(user.id, media)
        await bot.send_message(
            user.id,
            summary,
            reply_markup=confirmation_kb(user.id),
        )
    except Exception as exc:
        log.exception("Could not show submission preview to user %s: %s", user.id, exc)
        await message.answer(
            summary,
            reply_markup=confirmation_kb(user.id),
        )
    await state.set_state(SubmitFlow.waiting_confirmation)


@router.callback_query(SubmitFlow.waiting_confirmation, F.data == "submission_back")
async def submission_back(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SubmitFlow.waiting_description)
    await callback.answer()
    await callback.message.answer(
        tr("description", callback.from_user.id),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=tr("skip", callback.from_user.id), callback_data="skip_description")],
            [InlineKeyboardButton(text=tr("menu_cancel", callback.from_user.id), callback_data="submission_cancel")],
        ]),
    )


@router.callback_query(SubmitFlow.waiting_confirmation, F.data == "submission_confirm")
async def submission_confirm(callback: CallbackQuery, state: FSMContext):
    await callback.answer(tr("notification_processing", callback.from_user.id))
    await finalize_submission(callback.message, state, user=callback.from_user)


async def finalize_submission(message: Message, state: FSMContext, user=None):
    data = await state.get_data()
    user = user or message.from_user
    if (
        not data.get("photo_file_ids")
        or not data.get("platform_key")
        or not data.get("currency")
        or not data.get("payment_method")
        or "price" not in data
    ):
        await state.clear()
        await message.answer(
            tr("submission_unavailable", user.id),
            reply_markup=main_menu(user.id),
        )
        return
    sid = await asyncio.to_thread(create_submission, data, user)
    await state.clear()
    currency = data["currency"]
    sym = currency_code(currency)
    confirmation = tr("submission_received", user.id, id=sid)
    await message.answer(confirmation, reply_markup=main_menu(user.id))
    markup = await asyncio.to_thread(
        get_setting,
        markup_setting_key(currency),
        default_markup(currency),
    )
    accept_price = data["price"] + markup
    admin_caption = tr(
        "admin_new_submission",
        telegram_user_id=ADMIN_ID,
        id=sid,
        seller=esc(user.full_name),
        username=esc(user.username or "-"),
        user_id=user.id,
        photos=len(data["photo_file_ids"]),
        managers=data.get("managers_count", "-"),
        players=data.get("non_free_count") if data.get("non_free_count") is not None else "manual",
        price=f"{data['price']:g}",
        currency=sym,
        payment=esc(data["payment_method"]),
        platform=esc(data["platform"]),
        description=esc(data.get("description") or "—"),
        publish_price=f"{accept_price:g}",
        markup=f"{markup:g}",
    )
    try:
        if len(data["photo_file_ids"]) == 1:
            await bot.send_photo(
                ADMIN_ID,
                data["photo_file_ids"][0],
                caption=admin_caption,
                reply_markup=admin_actions(sid, accept_price, currency, ADMIN_ID, user.id),
            )
        else:
            media = [InputMediaPhoto(media=fid) for fid in data["photo_file_ids"]]
            media[0].caption = tr("admin_reviewing", ADMIN_ID, id=sid)
            await bot.send_media_group(ADMIN_ID, media)
            await bot.send_message(
                ADMIN_ID,
                admin_caption,
                reply_markup=admin_actions(sid, accept_price, currency, ADMIN_ID, user.id),
            )
    except Exception as exc:
        log.exception("Could not notify admin about submission %s: %s", sid, exc)
        await notify_user(
            user.id,
            sid,
            "admin_alert_failed",
            tr("admin_notification_failed", user.id),
        )

# -------------------------------- Marketplace ---------------------------------
async def show_marketplace(message: Message, query: str = "", page: int = 0):
    # Listings and photos are intentionally available only in the official channel.
    await message.answer(
        tr("marketplace_info", message.from_user.id),
        reply_markup=marketplace_keyboard(message.from_user.id),
    )


@router.message(Command("marketplace"))
async def marketplace_command(message: Message, state: FSMContext):
    if await guard_message(message, state): return
    await show_marketplace(message)


@router.message(Command("find"))
async def find_command(message: Message, state: FSMContext):
    if await guard_message(message, state): return
    await show_marketplace(message)


@router.callback_query(F.data.startswith("market:"))
async def marketplace_page(callback: CallbackQuery):
    if await asyncio.to_thread(is_banned, callback.from_user.id):
        await callback.answer(tr("banned", callback.from_user.id), show_alert=True)
        return
    # Old pagination buttons from previous bot versions now open the official channel.
    await callback.answer(url=CHANNEL_URL)


async def show_my_submissions(message: Message):
    rows = await asyncio.to_thread(
        list_submissions,
        "client_id=?",
        (message.from_user.id,),
        20,
    )
    if not rows:
        await message.answer(tr("no_submissions", message.from_user.id))
        return
    labels = {
        "pending": tr("status_pending", message.from_user.id),
        "awaiting_client_response": tr("status_awaiting", message.from_user.id),
        "accepted": tr("status_published", message.from_user.id),
        "rejected": tr("status_rejected", message.from_user.id),
        "publish_failed": tr("status_failed", message.from_user.id),
        "publishing": tr("status_publishing", message.from_user.id),
        "removed": tr("status_removed", message.from_user.id),
        "deleted": tr("status_deleted", message.from_user.id),
    }
    lines = [tr("my_submissions", message.from_user.id)]
    for s in rows:
        amount = s.get('final_price') or s.get('proposed_price') or s['price']
        lines.append(f"#{s['id']} — {labels.get(s['status'], esc(s['status']))} — {money(amount, s['currency'])}")
    await message.answer("\n".join(lines))


@router.message(Command("mysubmissions"))
async def my_submissions_command(message: Message, state: FSMContext):
    if not await guard_message(message, state): await show_my_submissions(message)


@router.message(Command("status"))
async def status_command(message: Message, state: FSMContext):
    if await guard_message(message, state): return
    rows = await asyncio.to_thread(
        list_submissions,
        "client_id=?",
        (message.from_user.id,),
        1,
    )
    if not rows:
        await message.answer(tr("no_status", message.from_user.id))
        return
    s = rows[0]
    status_key = {
        "pending": "status_pending",
        "awaiting_client_response": "status_awaiting",
        "accepted": "status_published",
        "rejected": "status_rejected",
        "publish_failed": "status_failed",
        "publishing": "status_publishing",
        "removed": "status_removed",
        "deleted": "status_deleted",
    }.get(s["status"])
    status_text = tr(status_key, message.from_user.id) if status_key else esc(s["status"])
    text = tr(
        "status_line",
        message.from_user.id,
        status=status_text,
        price=f"{s.get('final_price') or s.get('proposed_price') or s['price']:g}",
        currency=esc(s["currency"]),
        platform=esc(s.get("platform")),
    )
    await message.answer(f"📋 <b>#{s['id']}</b>\n{text}")

# -------------------------------- Admin dashboard -----------------------------
def dashboard_metrics() -> dict[str, Any]:
    with closing(connect()) as db:
        total = db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
        pending = db.execute("SELECT COUNT(*) FROM submissions WHERE status='pending'").fetchone()[0]
        awaiting = db.execute("SELECT COUNT(*) FROM submissions WHERE status='awaiting_client_response'").fetchone()[0]
        accepted = db.execute("SELECT COUNT(*) FROM submissions WHERE status='accepted'").fetchone()[0]
        rejected = db.execute("SELECT COUNT(*) FROM submissions WHERE status='rejected'").fetchone()[0]
        users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        bans = db.execute("SELECT COUNT(*) FROM bans").fetchone()[0]
        usd = db.execute("SELECT COALESCE(SUM(final_price),0) FROM submissions WHERE status='accepted' AND currency='USD'").fetchone()[0]
        mad = db.execute("SELECT COALESCE(SUM(final_price),0) FROM submissions WHERE status='accepted' AND currency='MAD'").fetchone()[0]
    return {
        "total": total,
        "pending": pending,
        "awaiting": awaiting,
        "published": accepted,
        "rejected": rejected,
        "users": users,
        "bans": bans,
        "usd": usd,
        "mad": mad,
    }


async def dashboard_text(uid: int) -> str:
    metrics = await asyncio.to_thread(dashboard_metrics)
    return tr(
        "dashboard",
        uid,
        **metrics,
        updated=esc(now_text()),
    )


def count_submissions(where: str, params: tuple = ()) -> int:
    with closing(connect()) as db:
        row = db.execute(f"SELECT COUNT(*) FROM submissions WHERE {where}", params).fetchone()
    return int(row[0])


def get_user_page(limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
    with closing(connect()) as db:
        total = int(db.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        rows = db.execute(
            "SELECT user_id,username,full_name,language,created_at,last_seen FROM users ORDER BY last_seen DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [dict(row) for row in rows], total


def get_ban_page(limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
    with closing(connect()) as db:
        total = int(db.execute("SELECT COUNT(*) FROM bans").fetchone()[0])
        rows = db.execute(
            "SELECT user_id,username,full_name,reason,banned_at FROM bans ORDER BY banned_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [dict(row) for row in rows], total


def get_all_user_ids() -> list[int]:
    with closing(connect()) as db:
        rows = db.execute("SELECT user_id FROM users ORDER BY user_id").fetchall()
    return [int(row[0]) for row in rows]


def record_admin_activity(action: str, submission_id: int | None, details: str = "") -> None:
    with closing(connect()) as db:
        db.execute(
            "INSERT INTO admin_activity(admin_id,action,submission_id,details,created_at) VALUES(?,?,?,?,?)",
            (ADMIN_ID, action, submission_id, details[:1000], now_text()),
        )
        db.commit()


def admin_submission_keyboard(submission: dict[str, Any], uid: int) -> InlineKeyboardMarkup:
    sid = int(submission["id"])
    status = submission["status"]
    rows: list[list[InlineKeyboardButton]] = []
    if status == "pending":
        rows.append([InlineKeyboardButton(
            text=tr("admin_approve", uid),
            callback_data=f"admin_accept:{sid}",
        )])
        rows.append([
            InlineKeyboardButton(text=tr("admin_custom_price", uid), callback_data=f"admin_custom:{sid}"),
            InlineKeyboardButton(text=tr("admin_reject", uid), callback_data=f"admin_reject:{sid}"),
        ])
    elif status == "publish_failed":
        rows.append([InlineKeyboardButton(
            text=tr("admin_retry", uid),
            callback_data=f"admin_retry:{sid}",
        )])
    elif status == "accepted":
        rows.append([
            InlineKeyboardButton(text=tr("admin_edit_price", uid), callback_data=f"admin_custom:{sid}"),
            InlineKeyboardButton(text=tr("admin_remove", uid), callback_data=f"admin_remove:{sid}"),
        ])
    if status not in ("deleted", "removed"):
        rows.append([InlineKeyboardButton(
            text=tr("admin_delete", uid),
            callback_data=f"admin_delete:{sid}",
        )])
    if submission.get("client_id"):
        rows.append([InlineKeyboardButton(
            text=tr("admin_contact_seller", uid),
            url=f"tg://user?id={int(submission['client_id'])}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_section_keyboard(section: str, page: int, total: int, uid: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text=tr("page_previous", uid),
            callback_data=f"admin:{section}:{page - 1}",
        ))
    if (page + 1) * 5 < total:
        nav.append(InlineKeyboardButton(
            text=tr("page_next", uid),
            callback_data=f"admin:{section}:{page + 1}",
        ))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(
        text=tr("back", uid),
        callback_data="admin:home",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def render_admin_section(callback: CallbackQuery, section: str, page: int = 0) -> None:
    uid = callback.from_user.id
    page = max(0, page)
    page_size = 5
    offset = page * page_size

    if section == "stats":
        await callback.message.answer(
            await dashboard_text(uid),
            reply_markup=admin_dashboard_kb(uid),
        )
        return

    if section in ("pending", "all", "published"):
        where = {
            "pending": "status IN ('pending','awaiting_client_response','publish_failed','publishing')",
            "all": "status NOT IN ('deleted')",
            "published": "status='accepted'",
        }[section]
        rows = await asyncio.to_thread(list_submissions, where, (), page_size, offset)
        total = await asyncio.to_thread(count_submissions, where)
        empty_key = "none_published" if section == "published" else "none_pending"
        heading_key = {
            "pending": "dashboard_pending",
            "all": "dashboard_all",
            "published": "dashboard_published",
        }[section]
        await callback.message.answer(
            tr(heading_key, uid),
            reply_markup=admin_section_keyboard(section, page, total, uid),
        )
        if not rows:
            await callback.message.answer(tr(empty_key, uid))
            return
        for submission in rows:
            await send_admin_submission(callback.message.chat.id, submission, uid)
        return

    if section == "users":
        rows, total = await asyncio.to_thread(get_user_page, page_size, offset)
        heading = tr("dashboard_users", uid)
        await callback.message.answer(
            heading,
            reply_markup=admin_section_keyboard(section, page, total, uid),
        )
        if not rows:
            await callback.message.answer(tr("none_users", uid))
            return
        for row in rows:
            text = tr(
                "admin_user_detail",
                uid,
                id=row["user_id"],
                name=esc(row["full_name"] or "—"),
                username=esc(row["username"] or "—"),
                language=esc(row["language"]),
                last_seen=esc(row["last_seen"]),
            )
            await callback.message.answer(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text=tr("admin_ban_user", uid),
                        callback_data=f"admin_ban:{row['user_id']}",
                    )
                ]]),
            )
        return

    if section == "bans":
        rows, total = await asyncio.to_thread(get_ban_page, page_size, offset)
        await callback.message.answer(
            tr("dashboard_bans", uid),
            reply_markup=admin_section_keyboard(section, page, total, uid),
        )
        if not rows:
            await callback.message.answer(tr("none_bans", uid))
            return
        for row in rows:
            text = tr(
                "admin_ban_detail",
                uid,
                id=row["user_id"],
                username=esc(row["username"] or "—"),
                name=esc(row["full_name"] or "—"),
                reason=esc(row["reason"] or "—"),
                date=esc(row["banned_at"] or "—"),
            )
            await callback.message.answer(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text=tr("admin_unban_user", uid),
                        callback_data=f"admin_unban:{row['user_id']}",
                    )
                ]]),
            )
        return

    setting_keys = {
        "pricing": "pricing_settings",
        "channel": "channel_settings",
        "settings": "bot_settings",
        "broadcast": "broadcast_instructions",
    }
    key = setting_keys.get(section)
    if key == "pricing_settings":
        text = await markups_text(uid)
    elif key == "channel_settings":
        text = tr(key, uid, channel=esc(CHANNEL_ID), url=CHANNEL_URL)
    elif key == "broadcast_instructions":
        text = tr(key, uid)
    else:
        text = tr(key or "bot_settings", uid)
    if section == "channel":
        await callback.message.answer(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=tr("check_channel_permissions", uid),
                    callback_data="admin_channel_check",
                )],
                [InlineKeyboardButton(text=tr("back", uid), callback_data="admin:home")],
            ]),
        )
        return
    await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=tr("back", uid), callback_data="admin:home")
    ]]))


async def send_admin_submission(chat_id: int, submission: dict[str, Any], uid: int) -> None:
    status_text = tr({
        "pending": "status_pending",
        "awaiting_client_response": "status_awaiting",
        "accepted": "status_published",
        "rejected": "status_rejected",
        "publish_failed": "status_failed",
        "publishing": "status_publishing",
        "removed": "status_removed",
        "deleted": "status_deleted",
    }.get(submission["status"], "status_pending"), uid)
    detail = tr(
        "admin_submission_detail",
        uid,
        id=submission["id"],
        seller=esc(submission.get("client_name") or "—"),
        username=esc(submission.get("client_username") or "—"),
        user_id=submission["client_id"],
        platform=esc(submission.get("platform") or "—"),
        managers=submission.get("managers_count") if submission.get("managers_count") is not None else "—",
        players=submission.get("non_free_count") if submission.get("non_free_count") is not None else "—",
        description=esc(submission.get("description") or "—"),
        price=f"{submission.get('price') or 0:g}",
        currency=esc(submission.get("currency") or "—"),
        status=status_text,
        date=esc(submission.get("created_at") or "—"),
    )
    photo_ids = submission.get("photo_file_ids") or []
    if photo_ids:
        if len(photo_ids) == 1:
            try:
                await bot.send_photo(
                    chat_id,
                    photo_ids[0],
                    caption=tr("admin_reviewing", uid, id=submission["id"]),
                )
            except Exception as exc:
                log.warning("Could not display submission photo %s: %s", submission["id"], exc)
        else:
            media = [InputMediaPhoto(media=file_id) for file_id in photo_ids[:MAX_PHOTOS]]
            media[0].caption = tr("admin_reviewing", uid, id=submission["id"])
            try:
                await bot.send_media_group(chat_id, media)
            except Exception as exc:
                log.warning("Could not display submission album %s: %s", submission["id"], exc)
    await bot.send_message(
        chat_id,
        detail,
        reply_markup=admin_submission_keyboard(submission, uid),
    )


@router.message(Command("dashboard"))
async def dashboard_command(message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer(tr("unauthorized", uid))
        return
    await asyncio.to_thread(touch_user, message.from_user)
    await message.answer(
        await dashboard_text(uid),
        reply_markup=admin_dashboard_kb(uid),
    )


@router.callback_query(F.data == "dashboard_refresh")
async def dashboard_refresh(callback: CallbackQuery):
    uid = callback.from_user.id
    if not is_admin(uid):
        await callback.answer(tr("unauthorized", uid), show_alert=True)
        return
    await callback.message.edit_text(
        await dashboard_text(uid),
        reply_markup=admin_dashboard_kb(uid),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:"))
async def admin_dashboard_callback(callback: CallbackQuery):
    uid = callback.from_user.id
    if not is_admin(uid):
        await callback.answer(tr("unauthorized", uid), show_alert=True)
        return
    parts = (callback.data or "").split(":")
    section = parts[1] if len(parts) > 1 else "home"
    page = 0
    if len(parts) > 2:
        try:
            page = int(parts[2])
        except ValueError:
            await callback.answer(tr("invalid_page", uid), show_alert=True)
            return
    await callback.answer()
    if section == "home":
        await callback.message.answer(
            await dashboard_text(uid),
            reply_markup=admin_dashboard_kb(uid),
        )
        return
    await render_admin_section(callback, section, page)


@router.message(Command("stats"))
async def stats_command(message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer(tr("unauthorized", uid))
        return
    await message.answer(await dashboard_text(uid), reply_markup=admin_dashboard_kb(uid))


@router.message(Command("pending"))
async def pending_command(message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer(tr("unauthorized", uid))
        return
    rows = await asyncio.to_thread(
        list_submissions,
        "status IN ('pending','awaiting_client_response','publish_failed','publishing')",
        (),
        5,
        0,
    )
    if not rows:
        await message.answer(tr("none_pending", uid))
        return
    await message.answer(tr("admin_pending", uid))
    for submission in rows:
        await send_admin_submission(message.chat.id, submission, uid)


@router.callback_query(F.data == "admin_pending")
async def admin_pending_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer(tr("unauthorized", callback.from_user.id), show_alert=True)
        return
    await callback.answer()
    await render_admin_section(callback, "pending", 0)


@router.callback_query(F.data.startswith("admin_ban:"))
async def admin_ban_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer(tr("unauthorized", callback.from_user.id), show_alert=True)
        return
    try:
        target_id = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer(tr("invalid_page", callback.from_user.id), show_alert=True)
        return
    if target_id == ADMIN_ID:
        await callback.answer(tr("cannot_ban_admin", callback.from_user.id), show_alert=True)
        return
    await asyncio.to_thread(ban_user, target_id, "Admin dashboard")
    await asyncio.to_thread(record_admin_activity, "ban", None, str(target_id))
    await callback.answer(tr("banned_user", callback.from_user.id, id=target_id), show_alert=True)
    await notify_user(target_id, None, "ban", tr("banned", target_id))


@router.callback_query(F.data.startswith("admin_unban:"))
async def admin_unban_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer(tr("unauthorized", callback.from_user.id), show_alert=True)
        return
    try:
        target_id = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer(tr("invalid_page", callback.from_user.id), show_alert=True)
        return
    removed = await asyncio.to_thread(delete_ban, target_id)
    if removed:
        await asyncio.to_thread(record_admin_activity, "unban", None, str(target_id))
        await notify_user(target_id, None, "unban", tr("unban_notice", target_id))
    await callback.answer(
        tr("unbanned" if removed else "not_banned", callback.from_user.id),
        show_alert=True,
    )


@router.callback_query(F.data == "admin_channel_check")
async def admin_channel_check_callback(callback: CallbackQuery):
    uid = callback.from_user.id
    if not is_admin(uid):
        await callback.answer(tr("unauthorized", uid), show_alert=True)
        return
    await callback.answer(tr("checking_channel", uid))
    allowed = await verify_channel_permissions()
    await callback.message.answer(
        tr("channel_permission_ok" if allowed else "channel_unreachable", uid)
    )


@router.message(Command("markup"))
async def markup_command(message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer(tr("unauthorized", uid))
        return
    await message.answer(await markups_text(uid))


@router.message(Command("setmarkup"))
async def setmarkup_command(message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer(tr("unauthorized", uid))
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer(tr("usage_setmarkup", uid) + "\n/setmarkup GBP|MAD|EGP|DZD|USD AMOUNT")
        return
    try:
        if parts[1].upper() in CURRENCIES:
            updates = {parts[1].upper(): float(parts[2])}
        else:  # legacy form: /setmarkup MAD USD
            updates = {"MAD": float(parts[1]), "USD": float(parts[2])}
        if any(v < 0 or v > 1_000_000 for v in updates.values()):
            raise ValueError
    except ValueError:
        await message.answer(tr("invalid_markup", uid))
        return
    await asyncio.gather(*(db_call(set_setting, markup_setting_key(c), v) for c, v in updates.items()))
    await db_call(record_admin_activity, "pricing_updated", None, "; ".join(f"{c}={v:g}" for c, v in updates.items()))
    await message.answer("✅ Markup updated.\n\n" + await markups_text(uid))


@router.message(Command("ban"))
async def ban_command(message: Message):
    admin_id = message.from_user.id
    if not is_admin(admin_id):
        await message.answer(tr("unauthorized", admin_id))
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer(tr("usage_ban", admin_id))
        return
    uid = int(parts[1]); reason = parts[2] if len(parts) > 2 else "Admin action"
    if uid == ADMIN_ID:
        await message.answer(tr("cannot_ban_admin", admin_id))
        return
    await db_call(ban_user, uid, reason)
    await db_call(record_admin_activity, "ban", None, f"{uid}: {reason}")
    await message.answer(tr("banned_user", admin_id, id=uid, reason=esc(reason)))
    await notify_user(uid, None, "ban", tr("banned", uid))


@router.message(Command("unban"))
async def unban_command(message: Message):
    admin_id = message.from_user.id
    if not is_admin(admin_id):
        await message.answer(tr("unauthorized", admin_id))
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer(tr("usage_unban", admin_id))
        return
    uid = int(parts[1])
    removed = await db_call(delete_ban, uid)
    await message.answer(tr("unbanned" if removed else "not_banned", admin_id))
    if removed:
        await db_call(record_admin_activity, "unban", None, str(uid))
        await notify_user(uid, None, "unban", tr("unban_notice", uid))


@router.message(Command("bans"))
async def bans_command(message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer(tr("unauthorized", uid))
        return
    rows, _ = await db_call(get_ban_page, 20, 0)
    if not rows:
        await message.answer(tr("none_bans", uid))
        return
    await message.answer(tr("banned_list", uid))
    for row in rows:
        await message.answer(
            tr(
                "admin_ban_detail",
                uid,
                id=row["user_id"],
                username=esc(row["username"] or "—"),
                name=esc(row["full_name"] or "—"),
                reason=esc(row["reason"] or "—"),
                date=esc(row["banned_at"] or "—"),
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text=tr("admin_unban_user", uid),
                    callback_data=f"admin_unban:{row['user_id']}",
                )
            ]]),
        )


@router.message(Command("backup"))
async def backup_command(message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer(tr("unauthorized", uid))
        return
    try:
        backup_path = await db_call(create_database_backup)
        await message.answer_document(
            FSInputFile(backup_path),
            caption=tr("backup_done", uid),
        )
    except Exception:
        log.exception("Manual database backup failed")
        await message.answer(tr("backup_failed", uid))


@router.message(Command("broadcast"))
async def broadcast_command(message: Message, state: FSMContext):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer(tr("unauthorized", uid))
        return
    text = (message.text or "").partition(" ")[2].strip()
    if not text:
        await message.answer(tr("usage_broadcast", uid))
        return
    if len(text) > 3500:
        await message.answer(tr("broadcast_too_long", uid))
        return
    await state.clear()
    await state.update_data(broadcast_text=text)
    await state.set_state(AdminBroadcast.waiting_confirmation)
    await message.answer(
        tr("broadcast_preview", uid, message=esc(text)),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=tr("broadcast_confirm", uid), callback_data="broadcast:confirm")],
            [InlineKeyboardButton(text=tr("broadcast_cancel", uid), callback_data="broadcast:cancel")],
        ]),
    )


@router.callback_query(AdminBroadcast.waiting_confirmation, F.data == "broadcast:cancel")
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await state.clear()
        await callback.answer(tr("unauthorized", callback.from_user.id), show_alert=True)
        return
    await state.clear()
    await callback.answer(tr("broadcast_cancelled", callback.from_user.id))


@router.callback_query(AdminBroadcast.waiting_confirmation, F.data == "broadcast:confirm")
async def broadcast_confirm(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await state.clear()
        await callback.answer(tr("unauthorized", callback.from_user.id), show_alert=True)
        return
    data = await state.get_data()
    text = str(data.get("broadcast_text") or "").strip()
    await state.clear()
    if not text:
        await callback.answer(tr("submission_unavailable", callback.from_user.id), show_alert=True)
        return
    ids = await db_call(get_all_user_ids)
    await callback.answer(tr("broadcast_started", callback.from_user.id))
    sent = failed = 0
    for uid in ids:
        try:
            await bot.send_message(uid, esc(text))
            sent += 1
        except Exception as exc:
            log.warning("Broadcast delivery failed user=%s: %s", uid, exc)
            failed += 1
        await asyncio.sleep(0.04)
    await callback.message.answer(
        tr("broadcast_finished", callback.from_user.id, sent=sent, failed=failed)
    )
    await db_call(record_admin_activity, "broadcast", None, f"sent={sent}; failed={failed}")

# ------------------------------ Admin callbacks -------------------------------
def parse_callback_sid(data: str) -> int | None:
    try:
        return int(data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        return None


@router.callback_query(F.data.startswith("admin_accept:"))
async def admin_accept(callback: CallbackQuery):
    uid = callback.from_user.id
    if not is_admin(uid):
        await callback.answer(tr("unauthorized", uid), show_alert=True)
        return
    sid = parse_callback_sid(callback.data)
    sub = await db_call(get_submission, sid) if sid else None
    if not sub or sub["status"] != "pending":
        await callback.answer(tr("submission_unavailable", uid), show_alert=True)
        return
    await notify_user(
        sub["client_id"],
        sid,
        "review_started",
        tr("seller_review_started", sub["client_id"], id=sid),
    )
    markup = await db_call(
        get_setting,
        markup_setting_key(sub["currency"]),
        default_markup(sub["currency"]),
    )
    await db_call(record_admin_activity, "approve", sid, "Approved for publication")
    await publish_submission(sid, float(sub["price"]) + markup, callback)


@router.callback_query(F.data.startswith("admin_retry:"))
async def admin_retry(callback: CallbackQuery):
    uid = callback.from_user.id
    if not is_admin(uid):
        await callback.answer(tr("unauthorized", uid), show_alert=True)
        return
    sid = parse_callback_sid(callback.data)
    sub = await db_call(get_submission, sid) if sid else None
    if not sub or sub["status"] != "publish_failed":
        await callback.answer(tr("no_failed_publication", uid), show_alert=True)
        return
    await callback.answer(tr("retrying", uid))
    await db_call(record_admin_activity, "retry_publication", sid)
    await publish_submission(sid, float(sub.get("final_price") or sub["price"]), callback)


@router.callback_query(F.data.startswith("admin_custom:"))
async def admin_custom(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if not is_admin(uid):
        await callback.answer(tr("unauthorized", uid), show_alert=True)
        return
    sid = parse_callback_sid(callback.data)
    sub = await db_call(get_submission, sid) if sid else None
    if not sub or sub["status"] not in ("pending", "accepted"):
        await callback.answer(tr("submission_unavailable", uid), show_alert=True)
        return
    await state.clear()
    await state.update_data(
        pending_sid=sid,
        price_mode="edit_published" if sub["status"] == "accepted" else "seller_offer",
    )
    await state.set_state(AdminCustomPrice.waiting_price)
    await callback.message.answer(
        tr("enter_final_price", uid, id=sid, currency=sub["currency"]),
        reply_markup=cancel_keyboard(uid),
    )
    await callback.answer()


@router.message(AdminCustomPrice.waiting_price)
async def custom_price_received(message: Message, state: FSMContext):
    uid = message.from_user.id
    if not is_admin(uid):
        await state.clear()
        return
    try:
        price = float((message.text or "").replace(",", "."))
        if not (0 < price <= 1_000_000): raise ValueError
    except ValueError:
        await message.answer(
            tr("invalid_positive_price", uid),
            reply_markup=cancel_keyboard(uid),
        )
        return
    data = await state.get_data()
    sid = int(data.get("pending_sid", 0))
    sub = await db_call(get_submission, sid)
    price_mode = data.get("price_mode", "seller_offer")
    if not sub:
        await state.clear()
        await message.answer(tr("submission_unavailable", uid))
        return
    if price_mode == "edit_published":
        if sub["status"] != "accepted" or not sub.get("channel_message_id"):
            await state.clear()
            await message.answer(tr("submission_unavailable", uid))
            return
        caption = build_channel_caption(sub, price)
        try:
            await bot.edit_message_caption(
                chat_id=sub.get("channel_id") or CHANNEL_ID,
                message_id=sub["channel_message_id"],
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            log.exception("Could not update published listing %s: %s", sid, exc)
            await state.clear()
            await message.answer(tr("channel_unreachable", uid))
            return
        await db_call(update_submission, sid, final_price=price)
        await db_call(record_admin_activity, "edit_published_price", sid, f"{price:g}")
        await state.clear()
        await message.answer(tr("published_price_updated", uid))
        return
    if sub["status"] != "pending":
        await state.clear()
        await message.answer(tr("submission_unavailable", uid))
        return
    await db_call(update_submission, sid, status="awaiting_client_response", proposed_price=price)
    await db_call(record_admin_activity, "custom_price_offer", sid, f"{price:g}")
    sym = currency_code(sub["currency"])
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=tr("admin_accept_price", sub["client_id"]), callback_data=f"client_accept:{sid}"),
        InlineKeyboardButton(text=tr("admin_decline_price", sub["client_id"]), callback_data=f"client_reject:{sid}"),
    ]])
    ok = await notify_user(
        sub["client_id"],
        sid,
        "custom_price",
        tr(
            "offer_notification",
            sub["client_id"],
            id=sid,
            price=f"{price:g}",
            currency=sym,
        ),
        kb,
    )
    await state.clear()
    await message.answer(tr("offer_sent" if ok else "offer_failed", uid))


@router.callback_query(F.data.startswith("admin_reject:"))
async def admin_reject(callback: CallbackQuery):
    uid = callback.from_user.id
    if not is_admin(uid):
        await callback.answer(tr("unauthorized", uid), show_alert=True)
        return
    sid = parse_callback_sid(callback.data)
    sub = await db_call(get_submission, sid) if sid else None
    if not sub or sub["status"] != "pending":
        await callback.answer(tr("submission_unavailable", uid), show_alert=True)
        return
    await db_call(update_submission, sid, status="rejected")
    await db_call(record_admin_activity, "reject", sid)
    await notify_user(
        sub["client_id"],
        sid,
        "rejected",
        tr("rejected", sub["client_id"], id=sid),
    )
    await callback.answer(tr("rejected", uid, id=sid))
    try: await callback.message.edit_reply_markup(reply_markup=None)
    except Exception: pass


@router.callback_query(F.data.startswith("client_accept:"))
async def client_accept(callback: CallbackQuery):
    uid = callback.from_user.id
    sid = parse_callback_sid(callback.data)
    sub = await db_call(get_submission, sid) if sid else None
    if not sub or sub["status"] != "awaiting_client_response":
        await callback.answer(tr("offer_unavailable", uid), show_alert=True)
        return
    if uid != sub["client_id"]:
        await callback.answer(tr("unauthorized", uid), show_alert=True)
        return
    await callback.answer(tr("offer_accepted", uid))
    try: await callback.message.edit_text(tr("offer_accepted", uid))
    except Exception: pass
    await publish_submission(sid, float(sub["proposed_price"]), callback)


@router.callback_query(F.data.startswith("client_reject:"))
async def client_reject(callback: CallbackQuery):
    uid = callback.from_user.id
    sid = parse_callback_sid(callback.data)
    sub = await db_call(get_submission, sid) if sid else None
    if not sub or sub["status"] != "awaiting_client_response":
        await callback.answer(tr("offer_unavailable", uid), show_alert=True)
        return
    if uid != sub["client_id"]:
        await callback.answer(tr("unauthorized", uid), show_alert=True)
        return
    await db_call(update_submission, sid, status="rejected")
    await callback.answer(tr("offer_declined", uid))
    try: await callback.message.edit_text(tr("offer_declined", uid))
    except Exception: pass
    await notify_user(
        ADMIN_ID,
        sid,
        "client_declined",
        tr("client_declined_admin", ADMIN_ID, id=sid),
    )

# ------------------------------ Publishing logic ------------------------------
def build_channel_caption(submission: dict[str, Any], final_price: float) -> str:
    currency = currency_code(submission["currency"])
    managers = submission.get("managers_count")
    players = submission.get("non_free_count")
    description = (submission.get("description") or "").strip()
    if len(description) > 260:
        description = description[:257].rstrip() + "…"
    escaped_description = esc(description) if description else "—"
    lines = [
        "🔥 <b>NEW eFOOTBALL ACCOUNT | حساب eFOOTBALL جديد</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"🆔 <b>Account ID / رقم الحساب:</b> #{submission['id']}",
        f"🎮 <b>Platform / المنصة:</b> {esc(submission.get('platform') or '—')}",
    ]
    if managers is not None:
        lines.append(f"👨‍💼 <b>Managers / المدربون:</b> {managers}")
    if players is not None:
        lines.append(f"⭐ <b>Premium Players / اللاعبون المميزون:</b> {players}")
    lines.extend([
        f"💰 <b>Price / السعر:</b> {money(final_price, currency)}",
        f"📝 <b>Description / الوصف:</b>\n{escaped_description}",
        "━━━━━━━━━━━━━━━━━━",
        f"📩 <b>Interested? / مهتم؟</b> @{esc(ADMIN_CONTACT_USERNAME)}",
    ])
    return "\n".join(lines)


async def publish_submission(sid: int, final_price: float, trigger=None):
    # The claim is an atomic status transition, so duplicate callbacks cannot publish twice.
    sub = await db_call(claim_publication, sid, final_price)
    if not sub:
        if isinstance(trigger, CallbackQuery):
            try:
                await trigger.answer(
                    tr("submission_unavailable", trigger.from_user.id),
                    show_alert=True,
                )
            except Exception:
                pass
        return

    currency = currency_code(sub["currency"])
    caption = build_channel_caption(sub, final_price)
    message_ids: list[int] = []
    try:
        if len(sub["photo_file_ids"]) == 1:
            sent = await bot.send_photo(
                CHANNEL_ID,
                sub["photo_file_ids"][0],
                caption=caption,
                reply_markup=channel_cta_keyboard(ADMIN_ID),
            )
            message_ids = [sent.message_id]
        else:
            media = [InputMediaPhoto(media=file_id) for file_id in sub["photo_file_ids"]]
            media[0].caption = caption
            media[0].parse_mode = ParseMode.HTML
            sent_group = await bot.send_media_group(CHANNEL_ID, media)
            message_ids = [item.message_id for item in sent_group]
            try:
                cta = await bot.send_message(
                    CHANNEL_ID,
                    tr("channel_cta", ADMIN_ID),
                    reply_markup=channel_cta_keyboard(ADMIN_ID),
                )
                message_ids.append(cta.message_id)
            except Exception as cta_exc:
                log.warning(
                    "Album posted for submission %s, but CTA message failed: %s",
                    sid,
                    cta_exc,
                )
    except Exception as exc:
        log.exception("Channel publication failed for submission %s: %s", sid, exc)
        await db_call(update_submission, sid, status="publish_failed", final_price=final_price)
        await notify_user(
            sub["client_id"],
            sid,
            "publish_failed",
            tr("publish_failed_seller", sub["client_id"], id=sid),
        )
        await notify_user(
            ADMIN_ID,
            sid,
            "publish_failed_admin",
            tr("publish_failed_admin", ADMIN_ID, id=sid),
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text=tr("admin_retry", ADMIN_ID),
                    callback_data=f"admin_retry:{sid}",
                )
            ]]),
        )
        if isinstance(trigger, CallbackQuery):
            try:
                await trigger.answer(
                    tr("channel_unreachable", trigger.from_user.id),
                    show_alert=True,
                )
            except Exception:
                pass
        return

    try:
        await db_call(save_publication, sid, final_price, CHANNEL_ID, message_ids)
    except Exception as exc:
        # Telegram has accepted the post; do not offer a blind retry that could duplicate it.
        log.exception("Telegram post saved but database finalization failed for %s: %s", sid, exc)
        await notify_user(
            ADMIN_ID,
            sid,
            "publication_uncertain",
            tr("publication_uncertain", ADMIN_ID),
        )
        return

    await db_call(record_admin_activity, "published", sid, f"{final_price:g} {currency}")
    await notify_user(
        sub["client_id"],
        sid,
        "published",
        tr("published", sub["client_id"], id=sid, price=f"{final_price:g}", currency=currency),
    )
    await notify_user(
        ADMIN_ID,
        sid,
        "published_admin",
        tr("publish_status_admin", ADMIN_ID, id=sid, price=f"{final_price:g}", currency=currency),
        platform_result_kb(sid, ADMIN_ID),
    )
    if isinstance(trigger, CallbackQuery):
        try:
            await trigger.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        try:
            await trigger.answer(
                tr("published", trigger.from_user.id, id=sid, price=f"{final_price:g}", currency=currency)
            )
        except Exception:
            pass
    elif isinstance(trigger, Message):
        await trigger.answer(
            tr("admin_published", ADMIN_ID, id=sid, price=f"{final_price:g}", currency=currency)
        )


@router.callback_query(F.data.startswith("plat_konami:"))
async def platform_konami(callback: CallbackQuery):
    uid = callback.from_user.id
    if not is_admin(uid):
        await callback.answer(tr("unauthorized", uid), show_alert=True)
        return
    sid = parse_callback_sid(callback.data)
    sub = await db_call(get_submission, sid) if sid else None
    if not sub or sub["status"] != "accepted":
        await callback.answer(tr("submission_unavailable", uid), show_alert=True)
        return
    await db_call(update_submission, sid, platform_status="konami_accepted")
    await db_call(record_admin_activity, "platform_konami", sid)
    await notify_user(
        sub["client_id"],
        sid,
        "platform_update",
        tr("platform_update_konami", sub["client_id"], id=sid),
    )
    await callback.answer(tr("recorded", uid))


@router.callback_query(F.data.startswith("plat_google:"))
async def platform_google(callback: CallbackQuery):
    uid = callback.from_user.id
    if not is_admin(uid):
        await callback.answer(tr("unauthorized", uid), show_alert=True)
        return
    sid = parse_callback_sid(callback.data)
    sub = await db_call(get_submission, sid) if sid else None
    if not sub or sub["status"] != "accepted":
        await callback.answer(tr("submission_unavailable", uid), show_alert=True)
        return
    await db_call(update_submission, sid, platform_status="google_rejected")
    await db_call(record_admin_activity, "platform_google", sid)
    await notify_user(
        sub["client_id"],
        sid,
        "platform_update",
        tr("platform_update_google", sub["client_id"], id=sid),
    )
    await callback.answer(tr("recorded", uid))


async def remove_listing_from_channel(sid: int, final_status: str) -> dict[str, Any]:
    submission = await db_call(get_submission, sid)
    if not submission:
        raise ValueError("Submission not found")
    message_ids = submission.get("channel_message_ids") or []
    if not message_ids and submission.get("channel_message_id"):
        message_ids = [submission["channel_message_id"]]
    chat_id = submission.get("channel_id") or CHANNEL_ID
    for message_id in message_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=int(message_id))
        except Exception as exc:
            # Missing messages are already removed; other failures should leave DB status unchanged.
            if "message to delete not found" in str(exc).lower() or "message can't be deleted" in str(exc).lower():
                log.info("Channel message %s for submission %s was already absent", message_id, sid)
                continue
            raise
    await db_call(mark_submission_removed, sid, final_status)
    await db_call(record_admin_activity, final_status, sid)
    return submission


async def request_admin_removal(
    callback: CallbackQuery,
    sid: int | None,
    final_status: str,
) -> None:
    uid = callback.from_user.id
    if not is_admin(uid):
        await callback.answer(tr("unauthorized", uid), show_alert=True)
        return
    submission = await db_call(get_submission, sid) if sid else None
    if not submission:
        await callback.answer(tr("submission_unavailable", uid), show_alert=True)
        return
    if final_status == "removed" and submission["status"] != "accepted":
        await callback.answer(tr("submission_unavailable", uid), show_alert=True)
        return
    confirm_text = tr(
        "delete_confirm" if final_status == "deleted" else "remove_confirm",
        uid,
        id=sid,
    )
    button_text = tr(
        "admin_delete_confirm" if final_status == "deleted" else "admin_remove_confirm",
        uid,
    )
    await callback.message.answer(
        confirm_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=button_text,
                callback_data=f"admin_{final_status}_confirm:{sid}",
            ),
            InlineKeyboardButton(
                text=tr("broadcast_cancel", uid),
                callback_data="admin_action_cancel",
            ),
        ]]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_delete:"))
async def admin_delete_request(callback: CallbackQuery):
    await request_admin_removal(callback, parse_callback_sid(callback.data), "deleted")


@router.callback_query(F.data.startswith("admin_remove:"))
async def admin_remove_request(callback: CallbackQuery):
    await request_admin_removal(callback, parse_callback_sid(callback.data), "removed")


@router.callback_query(F.data == "admin_action_cancel")
async def admin_action_cancel(callback: CallbackQuery):
    await callback.answer(tr("confirm_cancelled", callback.from_user.id))
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data.startswith("admin_deleted_confirm:"))
async def admin_delete_confirm(callback: CallbackQuery):
    uid = callback.from_user.id
    if not is_admin(uid):
        await callback.answer(tr("unauthorized", uid), show_alert=True)
        return
    sid = parse_callback_sid(callback.data)
    try:
        submission = await remove_listing_from_channel(sid, "deleted") if sid else None
    except Exception as exc:
        log.exception("Failed to remove submission %s from Telegram", sid)
        await callback.answer(tr("channel_unreachable", uid), show_alert=True)
        return
    if not submission:
        await callback.answer(tr("submission_unavailable", uid), show_alert=True)
        return
    await notify_user(
        submission["client_id"],
        sid,
        "deleted",
        tr("submission_deleted", submission["client_id"], id=sid),
    )
    await callback.answer(tr("delete_complete", uid, id=sid), show_alert=True)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data.startswith("admin_removed_confirm:"))
async def admin_remove_confirm(callback: CallbackQuery):
    uid = callback.from_user.id
    if not is_admin(uid):
        await callback.answer(tr("unauthorized", uid), show_alert=True)
        return
    sid = parse_callback_sid(callback.data)
    try:
        submission = await remove_listing_from_channel(sid, "removed") if sid else None
    except Exception:
        log.exception("Failed to remove listing %s from Telegram", sid)
        await callback.answer(tr("channel_unreachable", uid), show_alert=True)
        return
    if not submission:
        await callback.answer(tr("submission_unavailable", uid), show_alert=True)
        return
    await notify_user(
        submission["client_id"],
        sid,
        "removed",
        tr("seller_removed", submission["client_id"], id=sid),
    )
    await callback.answer(tr("remove_complete", uid, id=sid), show_alert=True)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# ------------------------------- Error handling -------------------------------
@router.error()
async def global_error_handler(event):
    log.exception("Unhandled update error: %s", event.exception)
    return True

# ----------------------------------- Startup ----------------------------------
async def verify_channel_permissions() -> bool:
    try:
        chat = await bot.get_chat(CHANNEL_ID)
        me = await bot.get_me()
        membership = await bot.get_chat_member(chat.id, me.id)
        allowed = membership.status == "creator" or (
            membership.status == "administrator"
            and bool(getattr(membership, "can_post_messages", False))
        )
        if not allowed:
            raise PermissionError("Bot is not a channel administrator with post permission")
        log.info("Verified bot can publish to channel %s", CHANNEL_ID)
        return True
    except Exception as exc:
        log.error("Channel permission check failed for %s: %s", CHANNEL_ID, exc)
        try:
            await bot.send_message(
                ADMIN_ID,
                tr(
                    "channel_permission_error",
                    ADMIN_ID,
                    channel=esc(CHANNEL_ID),
                    details=esc(str(exc)),
                ),
            )
        except Exception:
            log.exception("Could not send the channel setup error to admin")
        return False


async def main():
    global BOT_USERNAME
    await db_call(init_db)
    await db_call(migrate_legacy_json)
    await dp.storage.setup()
    await db_call(load_user_languages)
    me = await bot.get_me()
    BOT_USERNAME = me.username or ""
    await verify_channel_permissions()
    log.info("Advanced eFootball marketplace bot starting; database=%s", DB_PATH.resolve())
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await dp.storage.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())