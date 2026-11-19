from __future__ import annotations

import os
import sys
from pathlib import Path

# Load the MBBank SDK from flat .py files in the repository root.
# The import hook preserves the SDK's original package imports without folders.
import importlib.abc
import importlib.util

BASE_DIR = Path(__file__).resolve().parent

class _FlatSDKLoader(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    PREFIX = "mbbank"

    def _path_for(self, fullname):
        if fullname == self.PREFIX:
            return BASE_DIR / "mbbank__init__.py"
        if not fullname.startswith(self.PREFIX + "."):
            return None
        suffix = fullname.split(".")
        return BASE_DIR / ("__".join(suffix) + ".py")

    def find_spec(self, fullname, path=None, target=None):
        file_path = self._path_for(fullname)
        if file_path is None or not file_path.is_file():
            return None
        is_package = file_path.name.endswith("__init__.py")
        return importlib.util.spec_from_file_location(
            fullname, file_path,
            submodule_search_locations=[str(BASE_DIR)] if is_package else None,
        )

sys.meta_path.insert(0, _FlatSDKLoader())

from dotenv import load_dotenv
load_dotenv()

"""Small SQLite store for transaction de-duplication and bot preferences.

No bank username, password, session token, CAPTCHA, or OTP is persisted here.
"""

import os
import sqlite3
import threading
from pathlib import Path


class Store:
    def __init__(self, data_dir: str | None = None) -> None:
        root = Path(data_dir or os.getenv("DATA_DIR", "data")).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "mbbank_bot.sqlite3"
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS seen_transactions ("
            "account_no TEXT NOT NULL, ref_key TEXT NOT NULL, seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "PRIMARY KEY(account_no, ref_key))"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self._conn.commit()

    def has_any_seen(self) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM seen_transactions LIMIT 1"
            ).fetchone()
            return row is not None

    def has_seen(self, account_no: str, ref_key: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM seen_transactions WHERE account_no=? AND ref_key=?",
                (account_no, ref_key),
            ).fetchone()
            return row is not None

    def mark_seen(self, account_no: str, ref_key: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO seen_transactions(account_no, ref_key) VALUES(?, ?)",
                (account_no, ref_key),
            )
            self._conn.commit()

    def mark_many_seen(self, pairs: list[tuple[str, str]]) -> None:
        if not pairs:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO seen_transactions(account_no, ref_key) VALUES(?, ?)",
                pairs,
            )
            self._conn.commit()
            # Keep the local dedupe store bounded.
            self._conn.execute(
                "DELETE FROM seen_transactions WHERE rowid NOT IN "
                "(SELECT rowid FROM seen_transactions ORDER BY seen_at DESC, rowid DESC LIMIT 3000)"
            )
            self._conn.commit()

    def baseline_ready(self) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key='baseline_ready'"
            ).fetchone()
        return bool(row and row[0] == "1")

    def set_baseline_ready(self) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings(key, value) VALUES('baseline_ready', '1') "
                "ON CONFLICT(key) DO UPDATE SET value='1'"
            )
            self._conn.commit()

    def notifications_enabled(self) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key='notifications'"
            ).fetchone()
        return True if row is None else row[0] == "1"

    def set_notifications(self, enabled: bool) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings(key, value) VALUES('notifications', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("1" if enabled else "0",),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()



import asyncio
import html
import io
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from mbbank import CapchaProcessing, MBBank
from mbbank.errors import CapchaError, CryptoVerifyError, MBBankAPIError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


logger = logging.getLogger("mbbank_bot")
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

POLL_INTERVAL = max(30, int(os.getenv("POLL_INTERVAL_SECONDS", "30")))
CAPTCHA_TIMEOUT = max(60, int(os.getenv("CAPTCHA_TIMEOUT_SECONDS", "240")))
HISTORY_DAYS = max(1, min(7, int(os.getenv("HISTORY_DAYS", "3"))))


@dataclass
class CaptchaRequest:
    image: bytes
    event: threading.Event = field(default_factory=threading.Event)
    answer: str | None = None
    cancelled: bool = False
    error: str | None = None
    attempt: int = 1


class TelegramCaptchaProcessor(CapchaProcessing):
    """Bridge SDK's synchronous CAPTCHA callback to an async Telegram bot."""

    def __init__(self, application: Application, owner_id: int) -> None:
        super().__init__()
        self.application = application
        self.owner_id = owner_id
        self.loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.RLock()
        self._pending: CaptchaRequest | None = None
        self._attempt = 0
        self._cancel_requested = False

    @property
    def is_waiting(self) -> bool:
        with self._lock:
            return self._pending is not None

    async def _send_photo(self, request: CaptchaRequest) -> None:
        bio = io.BytesIO(request.image)
        bio.name = "mbbank-captcha.png"
        await self.application.bot.send_photo(
            chat_id=self.owner_id,
            photo=bio,
            caption=(
                "🛡️ <b>MB BANK · XÁC THỰC CAPTCHA</b>\n"
                f"Lượt thử: <b>{request.attempt}</b>\n\n"
                "Hãy gửi nội dung ký tự trong ảnh bằng một tin nhắn riêng.\n"
                "Dùng /cancel để hủy đăng nhập."
            ),
            parse_mode=ParseMode.HTML,
        )

    def process_image(self, img: bytes) -> str:
        if self.loop is None:
            raise RuntimeError("Telegram event loop chưa sẵn sàng để nhận CAPTCHA")
        with self._lock:
            if self._cancel_requested:
                raise RuntimeError("Người dùng đã hủy đăng nhập")
            if self._pending is not None:
                self._pending.cancelled = True
                self._pending.event.set()
            self._attempt += 1
            request = CaptchaRequest(image=img, attempt=self._attempt)
            self._pending = request

        try:
            send_future = asyncio.run_coroutine_threadsafe(
                self._send_photo(request), self.loop
            )
            send_future.result(timeout=30)
        except Exception as exc:
            with self._lock:
                if self._pending is request:
                    self._pending = None
            raise RuntimeError("Không gửi được ảnh CAPTCHA lên Telegram") from exc

        completed = request.event.wait(timeout=CAPTCHA_TIMEOUT)
        with self._lock:
            if self._pending is request:
                self._pending = None

        if not completed:
            raise TimeoutError("Hết thời gian chờ người dùng nhập CAPTCHA")
        if request.cancelled:
            raise RuntimeError("Người dùng đã hủy đăng nhập")
        if request.error:
            raise RuntimeError(request.error)
        answer = (request.answer or "").strip()
        if not answer:
            raise RuntimeError("CAPTCHA trống")
        return answer

    def submit(self, answer: str) -> bool:
        clean = answer.strip()
        if not clean or len(clean) > 32:
            return False
        with self._lock:
            request = self._pending
            if request is None or request.event.is_set():
                return False
            request.answer = clean
            request.event.set()
            return True

    def reset(self) -> None:
        with self._lock:
            if self._pending is not None:
                self._pending.cancelled = True
                self._pending.event.set()
            self._pending = None
            self._attempt = 0
            self._cancel_requested = False

    def cancel(self) -> bool:
        with self._lock:
            request = self._pending
            self._cancel_requested = True
            if request is None:
                return False
            request.cancelled = True
            request.event.set()
            return True


def to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}


def amount_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float, Decimal)):
        try:
            return int(Decimal(str(value)))
        except (InvalidOperation, ValueError):
            return 0
    # MBBank response amounts are VND strings. Remove an optional .00 / ,00
    # decimal suffix first, then remove locale thousand separators.
    raw = re.sub(r"[.,]0+$", "", str(value).strip())
    clean = re.sub(r"[^0-9-]", "", raw)
    try:
        return int(clean or "0")
    except ValueError:
        return 0


def money(value: Any, currency: str = "VND") -> str:
    formatted = f"{amount_int(value):,}".replace(",", ".")
    code = str(currency or "VND").upper()
    return formatted + (" đ" if code == "VND" else f" {code}")


def masked_account(account_no: Any) -> str:
    value = str(account_no or "")
    if len(value) <= 4:
        return "••••"
    return "•••• " + value[-4:]


def transaction_key(tx: dict[str, Any]) -> str:
    ref = str(tx.get("refNo") or tx.get("transactionId") or "").strip()
    if ref:
        return ref
    # Stable fallback for responses without a bank reference field.
    fields = [
        tx.get("accountNo", ""), tx.get("transactionDate", ""),
        tx.get("creditAmount", ""), tx.get("debitAmount", ""),
        tx.get("description", ""),
    ]
    return "|".join(str(x) for x in fields)


def transaction_sort_key(tx: dict[str, Any]) -> datetime:
    for field_name in ("transactionDate", "postingDate"):
        raw = str(tx.get(field_name) or "").strip()
        for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
    return datetime.min


def collect_snapshot(client: MBBank) -> dict[str, Any]:
    """Fetch current account balances and recent transactions using SDK methods."""
    now = datetime.now(VN_TZ)
    balance_model = client.getBalance()
    balance_data = to_dict(balance_model)
    accounts = [to_dict(x) for x in (getattr(balance_model, "acct_list", None) or [])]

    transactions: list[dict[str, Any]] = []
    from_date = now - timedelta(days=HISTORY_DAYS)
    if accounts:
        for account in accounts:
            account_no = str(account.get("acctNo") or "").strip()
            if not account_no:
                continue
            try:
                history = client.getTransactionAccountHistory(
                    accountNo=account_no,
                    from_date=from_date,
                    to_date=now,
                )
                tx_list = getattr(history, "transactionHistoryList", None) or []
                for item in tx_list:
                    tx = to_dict(item)
                    tx.setdefault("accountNo", account_no)
                    transactions.append(tx)
            except Exception as exc:
                # A failure on one sub-account should not hide the live balance.
                logger.warning("Không lấy được lịch sử một tài khoản: %s", type(exc).__name__)
    else:
        try:
            history = client.getTransactionAccountHistory(
                from_date=from_date,
                to_date=now,
            )
            tx_list = getattr(history, "transactionHistoryList", None) or []
            transactions.extend(to_dict(item) for item in tx_list)
        except Exception as exc:
            logger.warning("Không lấy được lịch sử giao dịch: %s", type(exc).__name__)

    # Deduplicate repeated references returned by overlapping account histories.
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for tx in transactions:
        account_no = str(tx.get("accountNo") or "")
        key = transaction_key(tx)
        unique[(account_no, key)] = tx
    transactions = sorted(unique.values(), key=transaction_sort_key, reverse=True)

    computed_total = sum(amount_int(a.get("currentBalance")) for a in accounts)
    total = balance_data.get("totalBalanceEquivalent")
    if total in (None, ""):
        total = computed_total
    currency = str(balance_data.get("currencyEquivalent") or "VND")

    return {
        "accounts": accounts,
        "transactions": transactions[:100],
        "total_balance": amount_int(total),
        "currency": currency,
        "updated_at": now,
    }


def account_label(account: dict[str, Any]) -> str:
    name = account.get("acctNm") or account.get("acctAlias") or "Tài khoản thanh toán"
    return str(name).strip() or "Tài khoản thanh toán"


def dashboard_text(snapshot: dict[str, Any] | None, logged_in: bool, last_error: str | None = None) -> str:
    if not logged_in:
        return (
            "🏦 <b>MB BANK · PERSONAL ASSISTANT</b>\n"
            "<i>Trợ lý theo dõi tài khoản riêng tư</i>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🔒 <b>Chưa kết nối ngân hàng</b>\n\n"
            "Bấm <b>Đăng nhập MB</b> để kết nối tài khoản.\n"
            "Bot chỉ phục vụ Telegram ID chủ sở hữu đã cấu hình."
        )
    if not snapshot:
        return "🏦 <b>MB BANK</b>\n⏳ Đang tải dữ liệu tài khoản..."

    accounts = snapshot.get("accounts", [])
    lines = [
        "🏦 <b>MB BANK · PERSONAL ASSISTANT</b>",
        "<i>Giám sát số dư & biến động tài khoản</i>",
        "━━━━━━━━━━━━━━━━━━",
        "💰 <b>TỔNG SỐ DƯ</b>",
        f"<b>{money(snapshot.get('total_balance', 0), snapshot.get('currency', 'VND'))}</b>",
        "━━━━━━━━━━━━━━━━━━",
    ]
    if accounts:
        lines.append(f"🏧 <b>TÀI KHOẢN ({len(accounts)})</b>")
        for account in accounts[:5]:
            name = html.escape(account_label(account))
            number = html.escape(masked_account(account.get("acctNo")))
            currency = html.escape(str(account.get("ccyCd") or "VND"))
            balance = money(account.get("currentBalance", 0), currency)
            lines.append(f"• {name} · <code>{number}</code>\n  {html.escape(balance)} ({currency})")
    else:
        lines.append("⚠️ Không tìm thấy tài khoản trong phản hồi số dư.")
    updated = snapshot.get("updated_at")
    if isinstance(updated, datetime):
        updated_text = updated.strftime("%H:%M:%S · %d/%m/%Y")
    else:
        updated_text = "Chưa rõ"
    lines += [
        "━━━━━━━━━━━━━━━━━━",
        f"🕒 Cập nhật: <code>{html.escape(updated_text)}</code>",
        f"🔁 Chu kỳ kiểm tra: <b>{POLL_INTERVAL}s</b>",
    ]
    if last_error:
        lines.append(f"⚠️ Lần kiểm tra gần nhất có lỗi: {html.escape(last_error[:120])}")
    lines.append("\n<i>Dữ liệu chỉ đọc từ MB Bank; bot không thực hiện chuyển tiền.</i>")
    return "\n".join(lines)


def main_keyboard(logged_in: bool, notifications: bool) -> InlineKeyboardMarkup:
    if not logged_in:
        rows = [
            [InlineKeyboardButton("🔐 ĐĂNG NHẬP MB BANK", callback_data="login")],
            [InlineKeyboardButton("ℹ️ HƯỚNG DẪN", callback_data="help")],
        ]
    else:
        rows = [
            [InlineKeyboardButton("💰 SỐ DƯ", callback_data="balance"), InlineKeyboardButton("🧾 LỊCH SỬ", callback_data="history")],
            [InlineKeyboardButton("🔄 LÀM MỚI", callback_data="refresh"), InlineKeyboardButton("🔔 " + ("TẮT THÔNG BÁO" if notifications else "BẬT THÔNG BÁO"), callback_data="notifications")],
            [InlineKeyboardButton("🚪 ĐĂNG XUẤT", callback_data="logout"), InlineKeyboardButton("🏠 MENU", callback_data="home")],
        ]
    return InlineKeyboardMarkup(rows)


def history_text(snapshot: dict[str, Any] | None) -> str:
    if not snapshot:
        return "🧾 <b>LỊCH SỬ GIAO DỊCH</b>\nChưa có dữ liệu. Hãy đăng nhập và làm mới trước."
    txs = snapshot.get("transactions", [])[:12]
    if not txs:
        return "🧾 <b>LỊCH SỬ GIAO DỊCH</b>\n━━━━━━━━━━━━━━━━━━\nChưa tìm thấy giao dịch trong khoảng thời gian tra cứu."
    lines = ["🧾 <b>LỊCH SỬ GIAO DỊCH GẦN ĐÂY</b>", "━━━━━━━━━━━━━━━━━━"]
    for tx in txs:
        credit = amount_int(tx.get("creditAmount"))
        debit = amount_int(tx.get("debitAmount"))
        net = credit - debit
        if net >= 0:
            sign = "🟢 +"
        else:
            sign = "🔴 −"
        date = html.escape(str(tx.get("transactionDate") or tx.get("postingDate") or "Không rõ thời gian"))
        raw_desc = str(tx.get("description") or tx.get("addDescription") or "Không có nội dung")
        desc = html.escape(raw_desc[:180] + ("…" if len(raw_desc) > 180 else ""))
        ref = html.escape(str(tx.get("refNo") or "—"))
        currency = str(tx.get("currency") or "VND")
        amount = money(abs(net), currency)
        lines.append(f"{sign}<b>{amount}</b>\n<code>{date}</code>\n{desc}\nMã GD: <code>{ref}</code>")
        lines.append("──────────────")
    lines.append(f"<i>Đang hiển thị tối đa 12 giao dịch gần nhất trong {HISTORY_DAYS} ngày tra cứu.</i>")
    return "\n".join(lines)


def notification_text(tx: dict[str, Any]) -> str:
    credit = amount_int(tx.get("creditAmount"))
    debit = amount_int(tx.get("debitAmount"))
    net = credit - debit
    currency = str(tx.get("currency") or "VND")
    if net >= 0:
        direction = "🟢 <b>TIỀN VÀO</b>"
        amount = "+" + money(net, currency)
    else:
        direction = "🔴 <b>TIỀN RA</b>"
        amount = "−" + money(abs(net), currency)
    date = html.escape(str(tx.get("transactionDate") or tx.get("postingDate") or "Không rõ"))
    raw_desc = str(tx.get("description") or tx.get("addDescription") or "Không có nội dung")
    desc = html.escape(raw_desc[:700] + ("…" if len(raw_desc) > 700 else ""))
    account = html.escape(masked_account(tx.get("accountNo")))
    available = tx.get("availableBalance")
    available_line = f"\n💳 Số dư sau GD: <b>{money(available, currency)}</b>" if available not in (None, "") else ""
    ref = html.escape(str(tx.get("refNo") or "—"))
    return (
        "🔔 <b>MB BANK · BIẾN ĐỘNG SỐ DƯ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{direction}\n<b>{html.escape(amount)}</b>\n\n"
        f"🕒 {date}\n"
        f"🏧 Tài khoản: <code>{account}</code>\n"
        f"📝 Nội dung: {desc}"
        f"{available_line}\n"
        f"🔎 Mã giao dịch: <code>{ref}</code>"
    )


def is_owner_private(update: Update, owner_id: int) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    return bool(
        user and chat and user.id == owner_id and chat.type == ChatType.PRIVATE
    )


async def safe_edit(query, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await query.edit_message_text(
            text=text, parse_mode=ParseMode.HTML, reply_markup=markup,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            raise


async def show_home(chat_id: int, context: ContextTypes.DEFAULT_TYPE, message_id: int | None = None) -> None:
    app = context.application
    logged = app.bot_data.get("mb_client") is not None
    snapshot = app.bot_data.get("snapshot")
    store: Store = app.bot_data["store"]
    text = dashboard_text(snapshot, logged, app.bot_data.get("last_error"))
    markup = main_keyboard(logged, store.notifications_enabled())
    if message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text,
                parse_mode=ParseMode.HTML, reply_markup=markup,
                disable_web_page_preview=True,
            )
            app.bot_data["dashboard_text"] = text
            app.bot_data["view"] = "home"
            return
        except BadRequest as exc:
            if "Message is not modified" in str(exc):
                app.bot_data["dashboard_text"] = text
                app.bot_data["view"] = "home"
                return
    message = await context.bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
        reply_markup=markup, disable_web_page_preview=True,
    )
    app.bot_data["dashboard_message_id"] = message.message_id
    app.bot_data["dashboard_text"] = text
    app.bot_data["view"] = "home"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id: int = context.application.bot_data["owner_id"]
    if not is_owner_private(update, owner_id):
        return
    await show_home(update.effective_chat.id, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id: int = context.application.bot_data["owner_id"]
    if not is_owner_private(update, owner_id):
        return
    text = (
        "ℹ️ <b>HƯỚNG DẪN SỬ DỤNG</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>1.</b> Bấm Đăng nhập MB Bank và nhập tên đăng nhập/số điện thoại.\n"
        "<b>2.</b> Nhập mật khẩu trong chat riêng. Bot sẽ cố gắng xóa tin nhắn mật khẩu ngay sau khi nhận.\n"
        "<b>3.</b> Khi MB hiện CAPTCHA, bot gửi ảnh để bạn nhập nội dung.\n"
        "<b>4.</b> Sau khi kết nối, bot tự kiểm tra số dư mỗi 30 giây và báo giao dịch mới.\n\n"
        "Lệnh: /start · /login · /balance · /history · /status · /logout · /cancel\n\n"
        "⚠️ SDK đi kèm hiện hỗ trợ luồng đăng nhập mật khẩu + CAPTCHA. Nó không cung cấp luồng OTP đăng nhập độc lập; nếu MB yêu cầu xác minh khác hoặc chặn thiết bị, bot sẽ báo lỗi thay vì tự vượt xác minh.\n"
        "🔒 Không gửi mật khẩu/OTP cho bất kỳ ai. Chỉ dùng bot trong chat riêng."
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def begin_login(chat_id: int, context: ContextTypes.DEFAULT_TYPE, message_id: int | None = None) -> None:
    if context.application.bot_data.get("login_active"):
        await context.bot.send_message(chat_id=chat_id, text="Đang có một phiên đăng nhập chạy. Chờ hoàn tất hoặc dùng /cancel.")
        return
    if context.application.bot_data.get("mb_client") is not None:
        msg = "Bạn đang kết nối MB Bank. Hãy đăng xuất trước khi đổi tài khoản."
        if message_id:
            await context.bot.send_message(chat_id=chat_id, text=msg)
        else:
            await context.bot.send_message(chat_id=chat_id, text=msg)
        return
    context.application.bot_data["captcha"].reset()
    context.user_data.clear()
    context.user_data["login_state"] = "username"
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🔐 <b>ĐĂNG NHẬP MB BANK</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Gửi <b>tên đăng nhập hoặc số điện thoại</b> Internet Banking của bạn.\n\n"
            "Thông tin chỉ được giữ trong bộ nhớ của tiến trình để tạo phiên kết nối; không ghi vào file cấu hình.\n"
            "Dùng /cancel để hủy."
        ),
        parse_mode=ParseMode.HTML,
    )


def login_error_message(exc: Exception) -> str:
    if isinstance(exc, CryptoVerifyError):
        return (
            "MB Bank yêu cầu bước xác minh bảo mật bổ sung (HTTP 428). "
            "SDK hiện tại không hỗ trợ hoàn tất bước này. Không nhập OTP vào bot; "
            "hãy kiểm tra ứng dụng/website MB chính thức rồi thử lại sau."
        )
    if isinstance(exc, CapchaError):
        return "CAPTCHA không được chấp nhận sau số lần thử của SDK. Hãy bấm Đăng nhập và thử lại."
    if isinstance(exc, TimeoutError):
        return "Hết thời gian chờ CAPTCHA. Hãy bấm Đăng nhập để thử lại."
    if isinstance(exc, MBBankAPIError):
        return f"MB Bank từ chối yêu cầu ({html.escape(str(getattr(exc, 'code', 'API_ERROR')))}). Kiểm tra thông tin đăng nhập hoặc thử lại sau."
    if "hủy đăng nhập" in str(exc).lower():
        return "Đã hủy quá trình đăng nhập."
    return f"Không thể đăng nhập ({type(exc).__name__}). Kiểm tra kết nối hoặc thử lại sau."


async def login_flow(
    context: ContextTypes.DEFAULT_TYPE, username: str, password: str
) -> None:
    app = context.application
    owner_id = app.bot_data["owner_id"]
    broker: TelegramCaptchaProcessor = app.bot_data["captcha"]
    await context.bot.send_message(
        chat_id=owner_id,
        text="⏳ Đang kết nối MB Bank. Nếu xuất hiện CAPTCHA, ảnh sẽ được gửi tại đây.",
    )
    try:
        def connect_and_fetch():
            client = MBBank(
                username=username,
                password=password,
                ocr_class=broker,
                retry_times=3,
                timeout=(12, 30),
            )
            snapshot = collect_snapshot(client)
            return client, snapshot

        client, snapshot = await asyncio.to_thread(connect_and_fetch)
        app.bot_data["mb_client"] = client
        app.bot_data["snapshot"] = snapshot
        app.bot_data["last_error"] = None
        store: Store = app.bot_data["store"]
        pairs = [
            (str(tx.get("accountNo") or ""), transaction_key(tx))
            for tx in snapshot.get("transactions", [])
        ]
        if not store.baseline_ready():
            # First successful read is a baseline: do not send old transactions as new alerts.
            store.mark_many_seen(pairs)
            store.set_baseline_ready()
        else:
            # If the bot was offline, notify only references not seen before.
            for tx in snapshot.get("transactions", []):
                account_no = str(tx.get("accountNo") or "")
                key = transaction_key(tx)
                if not store.has_seen(account_no, key):
                    store.mark_seen(account_no, key)
                    if store.notifications_enabled():
                        await context.bot.send_message(
                            chat_id=owner_id, text=notification_text(tx),
                            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                        )
        await context.bot.send_message(
            chat_id=owner_id,
            text="✅ <b>ĐĂNG NHẬP THÀNH CÔNG</b>\nĐã nhận dữ liệu tài khoản. Bot bắt đầu kiểm tra mỗi 30 giây.",
            parse_mode=ParseMode.HTML,
        )
        await show_home(owner_id, context)
    except Exception as exc:
        logger.warning("Đăng nhập MB thất bại: %s", type(exc).__name__)
        broker.cancel()
        await context.bot.send_message(
            chat_id=owner_id,
            text="❌ <b>ĐĂNG NHẬP CHƯA THÀNH CÔNG</b>\n" + login_error_message(exc),
            parse_mode=ParseMode.HTML,
        )
    finally:
        app.bot_data["login_active"] = False
        # Do not retain user credentials in Telegram's per-user conversation storage.
        context.user_data.pop("login_state", None)
        context.user_data.pop("mb_username", None)


async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = context.application.bot_data["owner_id"]
    if not is_owner_private(update, owner_id):
        return
    await begin_login(update.effective_chat.id, context)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = context.application.bot_data["owner_id"]
    if not is_owner_private(update, owner_id) or not update.message:
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    state = context.user_data.get("login_state")

    if state == "username":
        if len(text) > 100:
            await update.message.reply_text("Tên đăng nhập không hợp lệ. Gửi lại hoặc /cancel.")
            return
        context.user_data["mb_username"] = text
        context.user_data["login_state"] = "password"
        await update.message.reply_text(
            "🔑 Bây giờ gửi <b>mật khẩu Internet Banking</b>.\n"
            "Bot sẽ xóa tin nhắn này ngay sau khi nhận (nếu Telegram cho phép).",
            parse_mode=ParseMode.HTML,
        )
        return

    if state == "password":
        username = context.user_data.get("mb_username", "")
        password = text
        try:
            await update.message.delete()
        except TelegramError:
            logger.info("Không xóa được tin nhắn mật khẩu; người dùng nên tự xóa thủ công.")
        context.user_data.pop("login_state", None)
        context.application.bot_data["login_active"] = True
        # Fire-and-forget so that this update handler can keep processing CAPTCHA replies.
        context.application.create_task(
            login_flow(context, username, password),
            update=update,
            name="mbbank-login",
        )
        return

    broker: TelegramCaptchaProcessor = context.application.bot_data["captcha"]
    if broker.submit(text):
        await update.message.reply_text("✅ Đã nhận mã CAPTCHA. Đang tiếp tục xác thực...")
        return

    await update.message.reply_text(
        "Dùng các nút menu hoặc /help. Để kết nối ngân hàng, gửi /login.",
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = context.application.bot_data["owner_id"]
    if not is_owner_private(update, owner_id):
        return
    context.user_data.clear()
    broker: TelegramCaptchaProcessor = context.application.bot_data["captcha"]
    cancelled = broker.cancel()
    was_login_active = bool(context.application.bot_data.get("login_active"))
    context.user_data.clear()
    await update.effective_message.reply_text(
        "Đã gửi yêu cầu hủy đăng nhập." if (cancelled or was_login_active) else "Đã hủy thao tác nhập liệu."
    )


async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE, query=None) -> None:
    app = context.application
    if query:
        await query.answer("Đã đăng xuất")
    old_client = app.bot_data.get("mb_client")
    if old_client is not None:
        # Best-effort in-memory credential/session cleanup on logout.
        try:
            old_client._password = ""
            old_client.sessionId = None
            old_client._userinfo = None
        except Exception:
            pass
    app.bot_data["mb_client"] = None
    app.bot_data["snapshot"] = None
    app.bot_data["last_error"] = None
    app.bot_data["fail_count"] = 0
    broker: TelegramCaptchaProcessor = app.bot_data["captcha"]
    broker.cancel()
    if query:
        await safe_edit(query, dashboard_text(None, False), main_keyboard(False, app.bot_data["store"].notifications_enabled()))
        app.bot_data["dashboard_message_id"] = query.message.message_id
        app.bot_data["view"] = "home"
    elif update.effective_message:
        await show_home(update.effective_chat.id, context)


async def fetch_now(app: Application) -> dict[str, Any]:
    client = app.bot_data.get("mb_client")
    if client is None:
        raise RuntimeError("Chưa đăng nhập MB Bank")
    lock: asyncio.Lock = app.bot_data["bank_lock"]
    async with lock:
        snapshot = await asyncio.to_thread(collect_snapshot, client)
    app.bot_data["snapshot"] = snapshot
    app.bot_data["last_error"] = None
    app.bot_data["fail_count"] = 0
    return snapshot


async def refresh_and_process(app: Application, notify_new: bool = True) -> dict[str, Any]:
    snapshot = await fetch_now(app)
    store: Store = app.bot_data["store"]
    pairs = [
        (str(tx.get("accountNo") or ""), transaction_key(tx))
        for tx in snapshot.get("transactions", [])
    ]
    if not store.baseline_ready():
        # The first successful sync only establishes a baseline.
        store.mark_many_seen(pairs)
        store.set_baseline_ready()
        return snapshot

    notifications = store.notifications_enabled()
    for tx in snapshot.get("transactions", []):
        account_no = str(tx.get("accountNo") or "")
        key = transaction_key(tx)
        if not store.has_seen(account_no, key):
            store.mark_seen(account_no, key)
            if notify_new and notifications:
                await app.bot.send_message(
                    chat_id=app.bot_data["owner_id"],
                    text=notification_text(tx),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
    return snapshot


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = context.application.bot_data["owner_id"]
    if not is_owner_private(update, owner_id):
        return
    if context.application.bot_data.get("mb_client") is None:
        await update.effective_message.reply_text("Chưa đăng nhập. Gửi /login để kết nối MB Bank.")
        return
    await update.effective_message.reply_text("⏳ Đang cập nhật số dư...")
    try:
        await refresh_and_process(context.application, notify_new=True)
        await show_home(owner_id, context)
    except Exception as exc:
        logger.warning("Lấy số dư thất bại: %s", type(exc).__name__)
        await update.effective_message.reply_text("Không lấy được số dư ở lần này. Hãy thử /balance sau ít phút.")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = context.application.bot_data["owner_id"]
    if not is_owner_private(update, owner_id):
        return
    if context.application.bot_data.get("mb_client") is None:
        await update.effective_message.reply_text("Chưa đăng nhập. Gửi /login để kết nối MB Bank.")
        return
    try:
        await refresh_and_process(context.application, notify_new=True)
        text = history_text(context.application.bot_data.get("snapshot"))
        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 VỀ MENU", callback_data="home")]]),
        )
    except Exception as exc:
        logger.warning("Lấy lịch sử thất bại: %s", type(exc).__name__)
        await update.effective_message.reply_text("Không lấy được lịch sử giao dịch. Vui lòng thử lại sau.")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = context.application.bot_data["owner_id"]
    if not is_owner_private(update, owner_id):
        return
    connected = context.application.bot_data.get("mb_client") is not None
    status = "🟢 Đã kết nối" if connected else "⚪ Chưa đăng nhập"
    store: Store = context.application.bot_data["store"]
    text = (
        "⚙️ <b>TRẠNG THÁI BOT</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Kết nối MB: <b>{status}</b>\n"
        f"Chu kỳ kiểm tra: <b>{POLL_INTERVAL}s</b>\n"
        f"Thông báo giao dịch: <b>{'Bật' if store.notifications_enabled() else 'Tắt'}</b>\n"
        f"Dữ liệu: <code>{html.escape(str(store.path))}</code>\n"
        "Mật khẩu/session ngân hàng: <b>chỉ giữ trong RAM</b>"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = context.application.bot_data["owner_id"]
    if not is_owner_private(update, owner_id):
        return
    await logout(update, context)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    owner_id = context.application.bot_data["owner_id"]
    if not is_owner_private(update, owner_id):
        await query.answer("Không có quyền", show_alert=True)
        return
    action = query.data or "home"
    app = context.application
    store: Store = app.bot_data["store"]

    if action == "login":
        await query.answer()
        await begin_login(owner_id, context, query.message.message_id)
        return
    if action == "logout":
        await logout(update, context, query=query)
        return
    if action == "help":
        await query.answer()
        text = (
            "ℹ️ <b>HƯỚNG DẪN</b>\n"
            "Bấm Đăng nhập, gửi tên đăng nhập rồi mật khẩu. Khi có CAPTCHA, gửi nội dung ảnh vào chat.\n\n"
            "Bot chỉ đọc số dư/lịch sử, không có chức năng chuyển tiền. /cancel để hủy.\n"
            "SDK hiện tại không có luồng OTP đăng nhập độc lập."
        )
        await safe_edit(query, text, InlineKeyboardMarkup([[InlineKeyboardButton("🏠 MENU", callback_data="home")]]))
        app.bot_data["view"] = "help"
        return
    if action == "notifications":
        store.set_notifications(not store.notifications_enabled())
        await query.answer("Đã cập nhật thông báo")
        await safe_edit(
            query,
            dashboard_text(app.bot_data.get("snapshot"), app.bot_data.get("mb_client") is not None, app.bot_data.get("last_error")),
            main_keyboard(app.bot_data.get("mb_client") is not None, store.notifications_enabled()),
        )
        app.bot_data["dashboard_message_id"] = query.message.message_id
        app.bot_data["dashboard_text"] = dashboard_text(app.bot_data.get("snapshot"), app.bot_data.get("mb_client") is not None, app.bot_data.get("last_error"))
        app.bot_data["view"] = "home"
        return
    if action in ("balance", "refresh"):
        if app.bot_data.get("mb_client") is None:
            await query.answer("Bạn chưa đăng nhập MB Bank", show_alert=True)
            return
        await query.answer("Đang cập nhật số dư...")
        try:
            await refresh_and_process(app, notify_new=True)
            await show_home(owner_id, context, query.message.message_id)
        except Exception as exc:
            logger.warning("Làm mới dữ liệu thất bại: %s", type(exc).__name__)
            app.bot_data["last_error"] = "Không thể kết nối MB Bank ở thời điểm này"
            await show_home(owner_id, context, query.message.message_id)
        return
    if action == "history":
        if app.bot_data.get("mb_client") is None:
            await query.answer("Bạn chưa đăng nhập MB Bank", show_alert=True)
            return
        await query.answer("Đang tải lịch sử...")
        try:
            await refresh_and_process(app, notify_new=True)
        except Exception as exc:
            logger.warning("Tải lịch sử thất bại: %s", type(exc).__name__)
        await safe_edit(
            query,
            history_text(app.bot_data.get("snapshot")),
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 VỀ MENU", callback_data="home")]]),
        )
        app.bot_data["view"] = "history"
        return
    if action == "home":
        await query.answer()
        await show_home(owner_id, context, query.message.message_id)
        return
    await query.answer()


async def polling_loop(app: Application) -> None:
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        if app.bot_data.get("mb_client") is None:
            continue
        try:
            await refresh_and_process(app, notify_new=True)
            app.bot_data["fail_count"] = 0
            app.bot_data["last_error"] = None
            if app.bot_data.get("view", "home") == "home":
                message_id = app.bot_data.get("dashboard_message_id")
                new_text = dashboard_text(
                    app.bot_data.get("snapshot"), True, app.bot_data.get("last_error")
                )
                if message_id and new_text != app.bot_data.get("dashboard_text"):
                    try:
                        await app.bot.edit_message_text(
                            chat_id=app.bot_data["owner_id"],
                            message_id=message_id,
                            text=new_text,
                            parse_mode=ParseMode.HTML,
                            reply_markup=main_keyboard(True, app.bot_data["store"].notifications_enabled()),
                            disable_web_page_preview=True,
                        )
                        app.bot_data["dashboard_text"] = new_text
                    except BadRequest as exc:
                        if "Message is not modified" not in str(exc):
                            logger.info("Không cập nhật được thẻ dashboard: %s", type(exc).__name__)
        except Exception as exc:
            count = int(app.bot_data.get("fail_count", 0)) + 1
            app.bot_data["fail_count"] = count
            app.bot_data["last_error"] = "Kết nối MB Bank tạm thời gián đoạn"
            logger.warning("Chu kỳ kiểm tra thất bại (%s): %s", count, type(exc).__name__)
            if count in (3, 10):
                try:
                    await app.bot.send_message(
                        chat_id=app.bot_data["owner_id"],
                        text=(
                            "⚠️ <b>MB BANK TẠM THỜI KHÔNG PHẢN HỒI</b>\n"
                            "Bot vẫn đang chạy và sẽ thử lại ở chu kỳ tiếp theo. "
                            "Không cần gửi lại mật khẩu trừ khi phiên đăng nhập đã hết hạn."
                        ),
                        parse_mode=ParseMode.HTML,
                    )
                except TelegramError:
                    pass


async def post_init(app: Application) -> None:
    owner_id = app.bot_data["owner_id"]
    store = Store()
    app.bot_data["store"] = store
    app.bot_data["bank_lock"] = asyncio.Lock()
    app.bot_data["captcha"] = TelegramCaptchaProcessor(app, owner_id)
    app.bot_data["captcha"].loop = asyncio.get_running_loop()
    app.bot_data["mb_client"] = None
    app.bot_data["snapshot"] = None
    app.bot_data["login_active"] = False
    app.bot_data["view"] = "home"
    app.bot_data["fail_count"] = 0
    app.bot_data["last_error"] = None
    app.bot_data["dashboard_message_id"] = None
    app.bot_data["polling_task"] = asyncio.create_task(polling_loop(app), name="mbbank-poller")
    try:
        await app.bot.set_my_commands([
            ("start", "Mở bảng điều khiển"),
            ("login", "Đăng nhập MB Bank"),
            ("balance", "Xem số dư"),
            ("history", "Xem lịch sử giao dịch"),
            ("status", "Trạng thái bot"),
            ("logout", "Đăng xuất ngân hàng"),
            ("cancel", "Hủy thao tác đang nhập"),
            ("help", "Hướng dẫn"),
        ])
    except TelegramError as exc:
        logger.info("Không cập nhật được danh sách lệnh Telegram: %s", type(exc).__name__)
    logger.info("MB Bank Telegram bot started; poll interval=%ss", POLL_INTERVAL)


async def post_shutdown(app: Application) -> None:
    task = app.bot_data.get("polling_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    store: Store | None = app.bot_data.get("store")
    if store:
        store.close()


def run_bot() -> None:
    token = os.getenv("BOT_TOKEN", "").strip()
    owner_raw = os.getenv("OWNER_TELEGRAM_ID", "").strip()
    if not token:
        raise RuntimeError("Thiếu BOT_TOKEN trong Environment Variables")
    if not owner_raw.isdigit():
        raise RuntimeError("OWNER_TELEGRAM_ID phải là Telegram numeric user ID của bạn")
    owner_id = int(owner_raw)

    application = (
        Application.builder()
        .token(token)
        .concurrent_updates(8)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["owner_id"] = owner_id
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("login", login_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("logout", logout_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    application.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


# Minimal HTTP health endpoint required by Render Web Service.
import logging
import threading
from flask import Flask, jsonify

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
web = Flask(__name__)

@web.get("/")
def index():
    return "MB Bank Telegram Assistant is running. Use the private Telegram bot.", 200

@web.get("/health")
def health():
    return jsonify({"status": "ok", "service": "mbbank-telegram-assistant"}), 200

def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    threading.Thread(
        target=lambda: web.run(host="0.0.0.0", port=port, debug=False, use_reloader=False),
        name="health-web", daemon=True
    ).start()

if __name__ == "__main__":
    start_health_server()
    run_bot()
