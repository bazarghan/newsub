#!/usr/bin/env python3
"""
SubLink Telegram Bot — CDN-based IP management
Uses python-telegram-bot v20+ with full inline keyboard UI.
Manages per-CDN JSON config files with IPs, SNI, and host settings.
"""

import re
import json
import base64
import logging
from pathlib import Path

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, filters,
)

from config import BOT_TOKEN, ADMIN_IDS, CDN_DIR

logging.basicConfig(level=logging.INFO, format="[Bot] %(message)s")
logger = logging.getLogger(__name__)

CDN_PATH = Path(__file__).parent / CDN_DIR

# Conversation states
ST_ADD_CDN_NAME = 10
ST_ADD_CDN_ABBR = 11
ST_ADD_IP = 20
ST_ADD_IP_CONFIRM = 21
ST_EDIT_IP_SELECT = 30
ST_EDIT_IP_NEW = 31
ST_EDIT_SNI = 40
ST_EDIT_HOST = 50
ST_DELETE_CDN_CONFIRM = 60


# ===== CDN File Helpers =====
def list_cdns() -> list[dict]:
    """Load all CDN configs from cdn/*.json, sorted by name."""
    if not CDN_PATH.exists():
        CDN_PATH.mkdir(parents=True, exist_ok=True)
        return []
    configs = []
    for f in sorted(CDN_PATH.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data["_filename"] = f.stem  # Track filename without extension
            configs.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return configs


def load_cdn(filename: str) -> dict | None:
    """Load a single CDN config by filename (without .json)."""
    fpath = CDN_PATH / f"{filename}.json"
    if not fpath.exists():
        return None
    try:
        data = json.loads(fpath.read_text(encoding="utf-8"))
        data["_filename"] = filename
        return data
    except (json.JSONDecodeError, OSError):
        return None


def save_cdn(filename: str, data: dict):
    """Save a CDN config to cdn/<filename>.json."""
    CDN_PATH.mkdir(parents=True, exist_ok=True)
    # Don't save internal fields
    save_data = {k: v for k, v in data.items() if not k.startswith("_")}
    fpath = CDN_PATH / f"{filename}.json"
    fpath.write_text(json.dumps(save_data, ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8")


def delete_cdn_file(filename: str) -> bool:
    """Delete a CDN config file. Returns True if deleted."""
    fpath = CDN_PATH / f"{filename}.json"
    if fpath.exists():
        fpath.unlink()
        return True
    return False


def cdn_filename_from_name(name: str) -> str:
    """Generate a safe filename from CDN name."""
    return re.sub(r'[^a-zA-Z0-9_-]', '', name.lower().replace(" ", "_"))


# ===== IP Helpers =====
def is_valid_ip(ip: str) -> bool:
    if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ip):
        return all(0 <= int(p) <= 255 for p in ip.split("."))
    if re.match(r'^[a-zA-Z0-9][a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', ip):
        return True
    if ":" in ip:
        return True
    return False


def extract_ips_from_text(text: str) -> list[str]:
    """Extract IPs from various formats:
    - Plain IPs (one per line)
    - Config links: vless://...@IP:PORT, trojan://...@IP:PORT, ss://...@IP:PORT
    - vmess://base64 → decode JSON → .add field
    - Bracketed: [IP:PORT] or [IP]
    Returns deduplicated list of IPs.
    """
    found = []

    # 1) Extract from protocol links
    for match in re.finditer(
        r'(?:vless|trojan|ss)://[^@\s]+@([^:?#\s]+)',
        text, re.IGNORECASE
    ):
        ip = match.group(1).strip()
        if ip and is_valid_ip(ip):
            found.append(ip)

    # 2) Extract from vmess:// (base64 JSON with "add" field)
    for match in re.finditer(r'vmess://([A-Za-z0-9+/=]+)', text):
        try:
            raw = base64.b64decode(match.group(1)).decode("utf-8")
            data = json.loads(raw)
            ip = str(data.get("add", "")).strip()
            if ip and is_valid_ip(ip):
                found.append(ip)
        except Exception:
            pass

    # 3) Extract from brackets: [IP:PORT] or [IP]
    for match in re.finditer(r'\[([^\]]+)\]', text):
        content = match.group(1).strip()
        ip = content.split(":")[0].strip()
        if ip and is_valid_ip(ip):
            found.append(ip)

    # 4) If nothing found, try plain IPs line by line
    if not found:
        for line in text.splitlines():
            ip = line.strip()
            if ip and is_valid_ip(ip):
                found.append(ip)

    # Deduplicate preserving order
    seen = set()
    unique = []
    for ip in found:
        if ip not in seen:
            seen.add(ip)
            unique.append(ip)
    return unique


# ===== Admin Check =====
def admin_only(func):
    async def wrapper(update: Update, context):
        user_id = update.effective_user.id
        if user_id not in ADMIN_IDS:
            if update.callback_query:
                await update.callback_query.answer("⛔ دسترسی ندارید", show_alert=True)
            else:
                await update.effective_message.reply_text(
                    "⛔ <b>دسترسی محدود</b>\n\nشما مجاز به استفاده از این ربات نیستید.",
                    parse_mode="HTML"
                )
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


# ===== Main Menu =====
MAIN_MENU_TEXT = (
    "🔗 <b>سابلینک — مدیریت CDN</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "🛠 از دکمه‌های زیر استفاده کنید:"
)


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 لیست CDN‌ها", callback_data="menu:cdn_list")],
        [
            InlineKeyboardButton("➕ افزودن CDN", callback_data="menu:cdn_add"),
            InlineKeyboardButton("🗑 حذف CDN", callback_data="menu:cdn_delete"),
        ],
        [InlineKeyboardButton("⚙️ مدیریت CDN", callback_data="menu:cdn_manage")],
        [InlineKeyboardButton("🔄 بارگذاری مجدد", callback_data="menu:reload")],
    ])


@admin_only
async def cmd_start(update: Update, context):
    await update.message.reply_text(
        MAIN_MENU_TEXT,
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )


@admin_only
async def show_main_menu(update: Update, context):
    query = update.callback_query
    await query.answer()
    # Clear any CDN selection
    context.user_data.pop("selected_cdn", None)
    await query.edit_message_text(
        MAIN_MENU_TEXT,
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )


# ===== CDN List =====
@admin_only
async def menu_cdn_list(update: Update, context):
    query = update.callback_query
    await query.answer()

    cdns = list_cdns()
    if not cdns:
        await query.edit_message_text(
            "📋 <b>لیست CDN‌ها</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔸 هیچ CDN ثبت نشده\n\n"
            "💡 از دکمه «افزودن CDN» استفاده کنید",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ افزودن CDN", callback_data="menu:cdn_add")],
                [InlineKeyboardButton("🔙 بازگشت", callback_data="menu:home")],
            ])
        )
        return

    lines = []
    for cdn in cdns:
        abbr = cdn.get("abbreviation", "?")
        name = cdn.get("name", "?")
        ip_count = len(cdn.get("ips", []))
        sni = cdn.get("sni", "") or "—"
        host = cdn.get("host", "") or "—"
        lines.append(
            f"  <b>{abbr}</b> · {name}\n"
            f"     📡 IPs: <code>{ip_count}</code>  |  🔒 SNI: <code>{sni}</code>\n"
            f"     🌐 Host: <code>{host}</code>"
        )

    text = (
        f"📋 <b>لیست CDN‌ها</b>  ·  <code>{len(cdns)}</code> عدد\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        + "\n\n".join(lines)
    )

    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 بازگشت", callback_data="menu:home")],
        ])
    )


# ===== Add CDN =====
@admin_only
async def menu_cdn_add(update: Update, context):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "➕ <b>افزودن CDN جدید</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📝 نام CDN را وارد کنید:\n"
        "  مثال: <code>Cloudflare</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ انصراف", callback_data="menu:home")],
        ])
    )
    return ST_ADD_CDN_NAME


@admin_only
async def receive_cdn_name(update: Update, context):
    name = update.message.text.strip()
    if not name or len(name) > 50:
        await update.message.reply_text(
            "⚠️ نام نامعتبر. دوباره وارد کنید:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ انصراف", callback_data="menu:home")],
            ])
        )
        return ST_ADD_CDN_NAME

    # Check for duplicate
    filename = cdn_filename_from_name(name)
    if load_cdn(filename):
        await update.message.reply_text(
            f"⚠️ CDN با نام <b>{name}</b> قبلاً وجود دارد",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ انصراف", callback_data="menu:home")],
            ])
        )
        return ST_ADD_CDN_NAME

    context.user_data["new_cdn_name"] = name
    await update.message.reply_text(
        f"✅ نام: <b>{name}</b>\n\n"
        "📝 مخفف (abbreviation) را وارد کنید:\n"
        "  مثال: <code>CF</code> , <code>FSLY</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ انصراف", callback_data="menu:home")],
        ])
    )
    return ST_ADD_CDN_ABBR


@admin_only
async def receive_cdn_abbr(update: Update, context):
    abbr = update.message.text.strip().upper()
    if not abbr or len(abbr) > 10 or not re.match(r'^[A-Z0-9]+$', abbr):
        await update.message.reply_text(
            "⚠️ مخفف نامعتبر. فقط حروف و اعداد انگلیسی:\n"
            "  مثال: <code>CF</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ انصراف", callback_data="menu:home")],
            ])
        )
        return ST_ADD_CDN_ABBR

    name = context.user_data.get("new_cdn_name", "Unknown")
    filename = cdn_filename_from_name(name)

    cdn_data = {
        "name": name,
        "abbreviation": abbr,
        "sni": "",
        "host": "",
        "ips": [],
    }
    save_cdn(filename, cdn_data)

    await update.message.reply_text(
        "✅ <b>CDN اضافه شد</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"  📛 نام: <b>{name}</b>\n"
        f"  🏷 مخفف: <b>{abbr}</b>\n"
        f"  📄 فایل: <code>{filename}.json</code>\n\n"
        "💡 از «مدیریت CDN» برای افزودن آی‌پی و تنظیم SNI/Host استفاده کنید",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⚙️ مدیریت CDN", callback_data="menu:cdn_manage")],
            [InlineKeyboardButton("🔙 منوی اصلی", callback_data="menu:home")],
        ])
    )
    context.user_data.pop("new_cdn_name", None)
    return ConversationHandler.END


# ===== Delete CDN =====
@admin_only
async def menu_cdn_delete(update: Update, context):
    query = update.callback_query
    await query.answer()

    cdns = list_cdns()
    if not cdns:
        await query.edit_message_text(
            "🗑 <b>حذف CDN</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔸 هیچ CDN ثبت نشده",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 بازگشت", callback_data="menu:home")],
            ])
        )
        return

    buttons = []
    for cdn in cdns:
        fn = cdn["_filename"]
        abbr = cdn.get("abbreviation", "?")
        name = cdn.get("name", fn)
        ip_count = len(cdn.get("ips", []))
        buttons.append([InlineKeyboardButton(
            f"🗑 {abbr} · {name} ({ip_count} IP)",
            callback_data=f"cdndel:{fn}"
        )])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="menu:home")])

    await query.edit_message_text(
        "🗑 <b>حذف CDN</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔸 CDN مورد نظر برای حذف را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@admin_only
async def handle_cdn_delete_select(update: Update, context):
    query = update.callback_query
    fn = query.data.split(":", 1)[1]
    cdn = load_cdn(fn)
    if not cdn:
        await query.answer("⚠️ CDN یافت نشد")
        return

    name = cdn.get("name", fn)
    abbr = cdn.get("abbreviation", "?")
    ip_count = len(cdn.get("ips", []))

    await query.answer()
    await query.edit_message_text(
        f"⚠️ <b>تأیید حذف CDN</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"  📛 {abbr} · {name}\n"
        f"  📡 تعداد آی‌پی: {ip_count}\n\n"
        "❓ آیا مطمئنید؟",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ بله، حذف کن", callback_data=f"cdndelok:{fn}"),
                InlineKeyboardButton("❌ لغو", callback_data="menu:home"),
            ],
        ])
    )


@admin_only
async def confirm_cdn_delete(update: Update, context):
    query = update.callback_query
    fn = query.data.split(":", 1)[1]

    if delete_cdn_file(fn):
        await query.answer(f"✅ CDN حذف شد")
        await query.edit_message_text(
            "✅ <b>CDN حذف شد</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 منوی اصلی", callback_data="menu:home")],
            ])
        )
    else:
        await query.answer("⚠️ خطا در حذف")


# ===== CDN Manage — Select CDN =====
@admin_only
async def menu_cdn_manage(update: Update, context):
    query = update.callback_query
    await query.answer()

    cdns = list_cdns()
    if not cdns:
        await query.edit_message_text(
            "⚙️ <b>مدیریت CDN</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔸 هیچ CDN ثبت نشده",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ افزودن CDN", callback_data="menu:cdn_add")],
                [InlineKeyboardButton("🔙 بازگشت", callback_data="menu:home")],
            ])
        )
        return

    buttons = []
    for cdn in cdns:
        fn = cdn["_filename"]
        abbr = cdn.get("abbreviation", "?")
        name = cdn.get("name", fn)
        ip_count = len(cdn.get("ips", []))
        buttons.append([InlineKeyboardButton(
            f"⚙️ {abbr} · {name} ({ip_count} IP)",
            callback_data=f"cdnsel:{fn}"
        )])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="menu:home")])

    await query.edit_message_text(
        "⚙️ <b>مدیریت CDN</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔸 CDN مورد نظر را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ===== CDN Submenu =====
def cdn_submenu_keyboard(fn: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 لیست آی‌پی‌ها", callback_data=f"csub:list:{fn}")],
        [
            InlineKeyboardButton("➕ افزودن آی‌پی", callback_data=f"csub:add:{fn}"),
            InlineKeyboardButton("✏️ ویرایش آی‌پی", callback_data=f"csub:edit:{fn}"),
        ],
        [
            InlineKeyboardButton("🗑 حذف آی‌پی", callback_data=f"csub:remove:{fn}"),
            InlineKeyboardButton("🧹 پاک کردن همه", callback_data=f"csub:clear:{fn}"),
        ],
        [
            InlineKeyboardButton("🔒 تغییر SNI", callback_data=f"csub:sni:{fn}"),
            InlineKeyboardButton("🌐 تغییر Host", callback_data=f"csub:host:{fn}"),
        ],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="menu:cdn_manage")],
    ])


def cdn_submenu_text(cdn: dict) -> str:
    abbr = cdn.get("abbreviation", "?")
    name = cdn.get("name", "?")
    ip_count = len(cdn.get("ips", []))
    sni = cdn.get("sni", "") or "—"
    host = cdn.get("host", "") or "—"
    return (
        f"⚙️ <b>{abbr} · {name}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"  📡 آی‌پی‌ها: <b>{ip_count}</b>\n"
        f"  🔒 SNI: <code>{sni}</code>\n"
        f"  🌐 Host: <code>{host}</code>\n\n"
        "🛠 از دکمه‌های زیر استفاده کنید:"
    )


@admin_only
async def handle_cdn_select(update: Update, context):
    query = update.callback_query
    fn = query.data.split(":", 1)[1]
    cdn = load_cdn(fn)
    if not cdn:
        await query.answer("⚠️ CDN یافت نشد")
        return

    context.user_data["selected_cdn"] = fn
    await query.answer()
    await query.edit_message_text(
        cdn_submenu_text(cdn),
        parse_mode="HTML",
        reply_markup=cdn_submenu_keyboard(fn),
    )


# ===== CDN Submenu: List IPs =====
@admin_only
async def csub_list(update: Update, context):
    query = update.callback_query
    fn = query.data.split(":", 2)[2]
    cdn = load_cdn(fn)
    if not cdn:
        await query.answer("⚠️ CDN یافت نشد")
        return

    await query.answer()
    ips = cdn.get("ips", [])
    abbr = cdn.get("abbreviation", "?")
    name = cdn.get("name", "?")

    if not ips:
        await query.edit_message_text(
            f"📋 <b>{abbr} · آی‌پی‌ها</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔸 هیچ آی‌پی ثبت نشده",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ افزودن آی‌پی", callback_data=f"csub:add:{fn}")],
                [InlineKeyboardButton("🔙 بازگشت", callback_data=f"cdnsel:{fn}")],
            ])
        )
        return

    lines = [f"  {i}. <code>{ip}</code>" for i, ip in enumerate(ips, 1)]
    await query.edit_message_text(
        f"📋 <b>{abbr} · آی‌پی‌ها</b>  ·  <code>{len(ips)}</code> عدد\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        + "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 بازگشت", callback_data=f"cdnsel:{fn}")],
        ])
    )


# ===== CDN Submenu: Add IP =====
@admin_only
async def csub_add(update: Update, context):
    query = update.callback_query
    fn = query.data.split(":", 2)[2]
    cdn = load_cdn(fn)
    if not cdn:
        await query.answer("⚠️ CDN یافت نشد")
        return

    context.user_data["selected_cdn"] = fn
    abbr = cdn.get("abbreviation", "?")
    await query.answer()
    await query.edit_message_text(
        f"➕ <b>{abbr} · افزودن آی‌پی</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📝 آی‌پی‌ها را ارسال کنید\n\n"
        "💡 <b>فرمت‌های قابل قبول:</b>\n"
        "  🔹 آی‌پی ساده: <code>104.18.32.47</code>\n"
        "  🔹 کانفیگ: <code>vless://...</code> <code>trojan://...</code>\n"
        "  🔹 لیست با براکت: <code>[104.18.32.47:443]</code>\n\n"
        "🔸 آی‌پی‌ها خودکار استخراج می‌شوند",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ انصراف", callback_data=f"cdnsel:{fn}")],
        ])
    )
    return ST_ADD_IP


@admin_only
async def receive_add_ip(update: Update, context):
    fn = context.user_data.get("selected_cdn")
    if not fn:
        await update.message.reply_text("⚠️ خطا: CDN انتخاب نشده")
        return ConversationHandler.END

    cdn = load_cdn(fn)
    if not cdn:
        await update.message.reply_text("⚠️ خطا: CDN یافت نشد")
        return ConversationHandler.END

    text = update.message.text.strip()
    extracted = extract_ips_from_text(text)
    abbr = cdn.get("abbreviation", "?")

    if not extracted:
        await update.message.reply_text(
            "⚠️ <b>هیچ آی‌پی معتبری یافت نشد</b>\n\n"
            "📝 دوباره ارسال کنید یا فرمت را بررسی کنید",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ انصراف", callback_data=f"cdnsel:{fn}")],
            ])
        )
        return ST_ADD_IP

    current_ips = cdn.get("ips", [])
    new_ips = [ip for ip in extracted if ip not in current_ips]
    dup_ips = [ip for ip in extracted if ip in current_ips]

    context.user_data["pending_add_ips"] = new_ips

    text_parts = []
    if new_ips:
        lines = [f"  🟢 <code>{ip}</code>" for ip in new_ips]
        text_parts.append(f"<b>آی‌پی‌های جدید ({len(new_ips)}):</b>\n" + "\n".join(lines))
    if dup_ips:
        dup_lines = [f"  🔁 <code>{ip}</code>  <i>(تکراری)</i>" for ip in dup_ips]
        text_parts.append(f"<b>تکراری ({len(dup_ips)}):</b>\n" + "\n".join(dup_lines))

    if not new_ips:
        await update.message.reply_text(
            f"⚠️ <b>{abbr} · همه آی‌پی‌ها تکراری هستند</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            + "\n\n".join(text_parts),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ ارسال مجدد", callback_data=f"csub:add:{fn}")],
                [InlineKeyboardButton("🔙 بازگشت", callback_data=f"cdnsel:{fn}")],
            ])
        )
        return ConversationHandler.END

    await update.message.reply_text(
        f"🔍 <b>{abbr} · آی‌پی‌های استخراج شده</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        + "\n\n".join(text_parts) +
        "\n\n❓ <b>اضافه شوند؟</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ بله، اضافه کن", callback_data="ipadd:yes"),
                InlineKeyboardButton("❌ لغو", callback_data="ipadd:no"),
            ],
        ])
    )
    return ST_ADD_IP_CONFIRM


@admin_only
async def confirm_add_ips(update: Update, context):
    query = update.callback_query
    decision = query.data.split(":")[1]
    fn = context.user_data.get("selected_cdn")

    if decision != "yes" or not fn:
        await query.answer("❌ لغو شد")
        await query.edit_message_text(
            "❌ <b>عملیات لغو شد</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 بازگشت", callback_data=f"cdnsel:{fn}" if fn else "menu:home")],
            ])
        )
        return ConversationHandler.END

    cdn = load_cdn(fn)
    if not cdn:
        await query.answer("⚠️ CDN یافت نشد")
        return ConversationHandler.END

    new_ips = context.user_data.get("pending_add_ips", [])
    ips = cdn.get("ips", [])
    added = []
    for ip in new_ips:
        if ip not in ips:
            ips.append(ip)
            added.append(ip)
    cdn["ips"] = ips
    save_cdn(fn, cdn)

    abbr = cdn.get("abbreviation", "?")
    items = "\n".join(f"  ✅ <code>{ip}</code>" for ip in added)
    await query.answer(f"✅ {len(added)} آی‌پی اضافه شد")
    await query.edit_message_text(
        f"📝 <b>{abbr} · نتیجه افزودن</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{items}\n\n"
        f"📊 مجموع: <b>{len(ips)}</b> آی‌پی",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ افزودن بیشتر", callback_data=f"csub:add:{fn}")],
            [InlineKeyboardButton("📋 مشاهده لیست", callback_data=f"csub:list:{fn}")],
            [InlineKeyboardButton("🔙 بازگشت", callback_data=f"cdnsel:{fn}")],
        ])
    )
    context.user_data.pop("pending_add_ips", None)
    return ConversationHandler.END


# ===== CDN Submenu: Remove IP =====
@admin_only
async def csub_remove(update: Update, context):
    query = update.callback_query
    fn = query.data.split(":", 2)[2]
    cdn = load_cdn(fn)
    if not cdn:
        await query.answer("⚠️ CDN یافت نشد")
        return

    context.user_data["selected_cdn"] = fn
    ips = cdn.get("ips", [])
    abbr = cdn.get("abbreviation", "?")

    if not ips:
        await query.answer()
        await query.edit_message_text(
            f"🗑 <b>{abbr} · حذف آی‌پی</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔸 لیست خالی است",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 بازگشت", callback_data=f"cdnsel:{fn}")],
            ])
        )
        return

    buttons = []
    for ip in ips:
        buttons.append([InlineKeyboardButton(f"❌ {ip}", callback_data=f"ipdel:{fn}:{ip}")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"cdnsel:{fn}")])

    await query.answer()
    await query.edit_message_text(
        f"🗑 <b>{abbr} · حذف آی‌پی</b>  ·  <code>{len(ips)}</code> عدد\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔸 آی‌پی مورد نظر را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@admin_only
async def handle_ip_delete(update: Update, context):
    query = update.callback_query
    parts = query.data.split(":", 2)
    fn = parts[1]
    ip = parts[2]

    cdn = load_cdn(fn)
    if not cdn:
        await query.answer("⚠️ CDN یافت نشد")
        return

    ips = cdn.get("ips", [])
    abbr = cdn.get("abbreviation", "?")

    if ip in ips:
        ips.remove(ip)
        cdn["ips"] = ips
        save_cdn(fn, cdn)
        await query.answer(f"✅ {ip} حذف شد")
    else:
        await query.answer("⚠️ قبلاً حذف شده")

    if not ips:
        await query.edit_message_text(
            f"🗑 <b>{abbr} · حذف آی‌پی</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "✅ همه آی‌پی‌ها حذف شدند",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 بازگشت", callback_data=f"cdnsel:{fn}")],
            ])
        )
        return

    buttons = []
    for remaining_ip in ips:
        buttons.append([InlineKeyboardButton(
            f"❌ {remaining_ip}", callback_data=f"ipdel:{fn}:{remaining_ip}"
        )])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"cdnsel:{fn}")])

    await query.edit_message_text(
        f"🗑 <b>{abbr} · حذف آی‌پی</b>  ·  <code>{len(ips)}</code> عدد\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔸 آی‌پی مورد نظر را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ===== CDN Submenu: Edit IP =====
@admin_only
async def csub_edit(update: Update, context):
    query = update.callback_query
    fn = query.data.split(":", 2)[2]
    cdn = load_cdn(fn)
    if not cdn:
        await query.answer("⚠️ CDN یافت نشد")
        return

    context.user_data["selected_cdn"] = fn
    ips = cdn.get("ips", [])
    abbr = cdn.get("abbreviation", "?")

    if not ips:
        await query.answer()
        await query.edit_message_text(
            f"✏️ <b>{abbr} · ویرایش آی‌پی</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔸 لیست خالی است",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 بازگشت", callback_data=f"cdnsel:{fn}")],
            ])
        )
        return ConversationHandler.END

    buttons = []
    for ip in ips:
        buttons.append([InlineKeyboardButton(f"✏️ {ip}", callback_data=f"ipedit:{fn}:{ip}")])
    buttons.append([InlineKeyboardButton("❌ انصراف", callback_data=f"cdnsel:{fn}")])

    await query.answer()
    await query.edit_message_text(
        f"✏️ <b>{abbr} · ویرایش آی‌پی</b>  ·  <code>{len(ips)}</code> عدد\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔸 آی‌پی مورد نظر را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ST_EDIT_IP_SELECT


@admin_only
async def edit_ip_select(update: Update, context):
    query = update.callback_query

    if query.data.startswith("cdnsel:"):
        fn = query.data.split(":", 1)[1]
        cdn = load_cdn(fn)
        if cdn:
            await query.answer()
            await query.edit_message_text(
                cdn_submenu_text(cdn),
                parse_mode="HTML",
                reply_markup=cdn_submenu_keyboard(fn),
            )
        return ConversationHandler.END

    parts = query.data.split(":", 2)
    fn = parts[1]
    old_ip = parts[2]
    context.user_data["edit_old_ip"] = old_ip
    context.user_data["selected_cdn"] = fn

    cdn = load_cdn(fn)
    abbr = cdn.get("abbreviation", "?") if cdn else "?"

    await query.answer()
    await query.edit_message_text(
        f"✏️ <b>{abbr} · ویرایش آی‌پی</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔴 آی‌پی فعلی: <code>{old_ip}</code>\n\n"
        "📝 آی‌پی جدید را ارسال کنید:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ انصراف", callback_data=f"cdnsel:{fn}")],
        ])
    )
    return ST_EDIT_IP_NEW


@admin_only
async def receive_edit_new_ip(update: Update, context):
    new_ip = update.message.text.strip()
    old_ip = context.user_data.get("edit_old_ip", "")
    fn = context.user_data.get("selected_cdn")

    if not fn:
        await update.message.reply_text("⚠️ خطا: CDN انتخاب نشده")
        return ConversationHandler.END

    if not is_valid_ip(new_ip):
        await update.message.reply_text(
            f"⛔ آی‌پی <code>{new_ip}</code> نامعتبر است\n\n"
            "📝 دوباره آی‌پی جدید را ارسال کنید:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ انصراف", callback_data=f"cdnsel:{fn}")],
            ])
        )
        return ST_EDIT_IP_NEW

    cdn = load_cdn(fn)
    if not cdn:
        await update.message.reply_text("⚠️ CDN یافت نشد")
        return ConversationHandler.END

    ips = cdn.get("ips", [])
    abbr = cdn.get("abbreviation", "?")

    if old_ip in ips:
        idx = ips.index(old_ip)
        ips[idx] = new_ip
        cdn["ips"] = ips
        save_cdn(fn, cdn)

        await update.message.reply_text(
            f"✏️ <b>{abbr} · ویرایش انجام شد</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"  🔴 قبلی: <code>{old_ip}</code>\n"
            f"  🟢 جدید: <code>{new_ip}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ ویرایش دیگر", callback_data=f"csub:edit:{fn}")],
                [InlineKeyboardButton("🔙 بازگشت", callback_data=f"cdnsel:{fn}")],
            ])
        )
    else:
        await update.message.reply_text(
            f"⚠️ آی‌پی <code>{old_ip}</code> دیگر وجود ندارد",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 بازگشت", callback_data=f"cdnsel:{fn}")],
            ])
        )
    return ConversationHandler.END


# ===== CDN Submenu: Clear All IPs =====
@admin_only
async def csub_clear(update: Update, context):
    query = update.callback_query
    fn = query.data.split(":", 2)[2]
    cdn = load_cdn(fn)
    if not cdn:
        await query.answer("⚠️ CDN یافت نشد")
        return

    ips = cdn.get("ips", [])
    abbr = cdn.get("abbreviation", "?")

    if not ips:
        await query.answer()
        await query.edit_message_text(
            f"🔸 {abbr} · لیست آی‌پی خالی است",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 بازگشت", callback_data=f"cdnsel:{fn}")],
            ])
        )
        return

    await query.answer()
    await query.edit_message_text(
        f"⚠️ <b>{abbr} · پاک کردن همه آی‌پی‌ها</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🗑 تعداد <b>{len(ips)}</b> آی‌پی حذف خواهند شد\n\n"
        "❓ آیا مطمئنید؟",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ بله، پاک کن", callback_data=f"ipclear:{fn}"),
                InlineKeyboardButton("❌ لغو", callback_data=f"cdnsel:{fn}"),
            ],
        ])
    )


@admin_only
async def confirm_clear_ips(update: Update, context):
    query = update.callback_query
    fn = query.data.split(":", 1)[1]

    cdn = load_cdn(fn)
    if not cdn:
        await query.answer("⚠️ CDN یافت نشد")
        return

    abbr = cdn.get("abbreviation", "?")
    cdn["ips"] = []
    save_cdn(fn, cdn)

    await query.answer("✅ همه آی‌پی‌ها پاک شد")
    await query.edit_message_text(
        f"🗑 <b>{abbr} · تمام آی‌پی‌ها پاک شدند</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📊 مجموع: <b>0</b> آی‌پی",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 بازگشت", callback_data=f"cdnsel:{fn}")],
        ])
    )


# ===== CDN Submenu: Edit SNI =====
@admin_only
async def csub_sni(update: Update, context):
    query = update.callback_query
    fn = query.data.split(":", 2)[2]
    cdn = load_cdn(fn)
    if not cdn:
        await query.answer("⚠️ CDN یافت نشد")
        return

    context.user_data["selected_cdn"] = fn
    abbr = cdn.get("abbreviation", "?")
    current_sni = cdn.get("sni", "") or "—"

    await query.answer()
    await query.edit_message_text(
        f"🔒 <b>{abbr} · تغییر SNI</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔒 SNI فعلی: <code>{current_sni}</code>\n\n"
        "📝 SNI جدید را ارسال کنید:\n"
        "  مثال: <code>example.com</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ انصراف", callback_data=f"cdnsel:{fn}")],
        ])
    )
    return ST_EDIT_SNI


@admin_only
async def receive_sni(update: Update, context):
    fn = context.user_data.get("selected_cdn")
    if not fn:
        await update.message.reply_text("⚠️ خطا: CDN انتخاب نشده")
        return ConversationHandler.END

    cdn = load_cdn(fn)
    if not cdn:
        await update.message.reply_text("⚠️ CDN یافت نشد")
        return ConversationHandler.END

    new_sni = update.message.text.strip()
    old_sni = cdn.get("sni", "") or "—"
    abbr = cdn.get("abbreviation", "?")

    cdn["sni"] = new_sni
    save_cdn(fn, cdn)

    await update.message.reply_text(
        f"🔒 <b>{abbr} · SNI تغییر کرد</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"  🔴 قبلی: <code>{old_sni}</code>\n"
        f"  🟢 جدید: <code>{new_sni}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 بازگشت", callback_data=f"cdnsel:{fn}")],
        ])
    )
    return ConversationHandler.END


# ===== CDN Submenu: Edit Host =====
@admin_only
async def csub_host(update: Update, context):
    query = update.callback_query
    fn = query.data.split(":", 2)[2]
    cdn = load_cdn(fn)
    if not cdn:
        await query.answer("⚠️ CDN یافت نشد")
        return

    context.user_data["selected_cdn"] = fn
    abbr = cdn.get("abbreviation", "?")
    current_host = cdn.get("host", "") or "—"

    await query.answer()
    await query.edit_message_text(
        f"🌐 <b>{abbr} · تغییر Host</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🌐 Host فعلی: <code>{current_host}</code>\n\n"
        "📝 Host جدید را ارسال کنید:\n"
        "  مثال: <code>example.com</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ انصراف", callback_data=f"cdnsel:{fn}")],
        ])
    )
    return ST_EDIT_HOST


@admin_only
async def receive_host(update: Update, context):
    fn = context.user_data.get("selected_cdn")
    if not fn:
        await update.message.reply_text("⚠️ خطا: CDN انتخاب نشده")
        return ConversationHandler.END

    cdn = load_cdn(fn)
    if not cdn:
        await update.message.reply_text("⚠️ CDN یافت نشد")
        return ConversationHandler.END

    new_host = update.message.text.strip()
    old_host = cdn.get("host", "") or "—"
    abbr = cdn.get("abbreviation", "?")

    cdn["host"] = new_host
    save_cdn(fn, cdn)

    await update.message.reply_text(
        f"🌐 <b>{abbr} · Host تغییر کرد</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"  🔴 قبلی: <code>{old_host}</code>\n"
        f"  🟢 جدید: <code>{new_host}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 بازگشت", callback_data=f"cdnsel:{fn}")],
        ])
    )
    return ConversationHandler.END


# ===== Reload =====
@admin_only
async def menu_reload(update: Update, context):
    query = update.callback_query
    cdns = list_cdns()
    total_ips = sum(len(c.get("ips", [])) for c in cdns)
    await query.answer(f"🔄 بارگذاری شد · {len(cdns)} CDN · {total_ips} IP")
    await query.edit_message_text(
        "🔄 <b>بارگذاری مجدد</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 تعداد CDN: <b>{len(cdns)}</b>\n"
        f"📡 مجموع آی‌پی: <b>{total_ips}</b>\n\n"
        "✅ با موفقیت بارگذاری شد",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 لیست CDN‌ها", callback_data="menu:cdn_list")],
            [InlineKeyboardButton("🔙 منوی اصلی", callback_data="menu:home")],
        ])
    )


# ===== Cancel for conversations =====
async def cancel_conversation(update: Update, context):
    query = update.callback_query
    if query:
        data = query.data
        # If returning to CDN submenu
        if data.startswith("cdnsel:"):
            fn = data.split(":", 1)[1]
            cdn = load_cdn(fn)
            if cdn:
                await query.answer()
                await query.edit_message_text(
                    cdn_submenu_text(cdn),
                    parse_mode="HTML",
                    reply_markup=cdn_submenu_keyboard(fn),
                )
                return ConversationHandler.END

        # Otherwise go to main menu
        if data == "menu:home":
            await query.answer()
            context.user_data.pop("selected_cdn", None)
            await query.edit_message_text(
                MAIN_MENU_TEXT, parse_mode="HTML",
                reply_markup=main_menu_keyboard()
            )
    return ConversationHandler.END


# ===== Main =====
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Conversation: Add CDN
    add_cdn_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(menu_cdn_add, pattern=r"^menu:cdn_add$")],
        states={
            ST_ADD_CDN_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_cdn_name),
                CallbackQueryHandler(cancel_conversation, pattern=r"^menu:home$"),
            ],
            ST_ADD_CDN_ABBR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_cdn_abbr),
                CallbackQueryHandler(cancel_conversation, pattern=r"^menu:home$"),
            ],
        },
        fallbacks=[
            CommandHandler("start", cmd_start),
            CallbackQueryHandler(cancel_conversation, pattern=r"^menu:home$"),
        ],
        per_message=False,
    )

    # Conversation: Add IP to CDN
    add_ip_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(csub_add, pattern=r"^csub:add:")],
        states={
            ST_ADD_IP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_add_ip),
                CallbackQueryHandler(cancel_conversation, pattern=r"^(cdnsel:|menu:home)"),
            ],
            ST_ADD_IP_CONFIRM: [
                CallbackQueryHandler(confirm_add_ips, pattern=r"^ipadd:"),
                CallbackQueryHandler(cancel_conversation, pattern=r"^(cdnsel:|menu:home)"),
            ],
        },
        fallbacks=[
            CommandHandler("start", cmd_start),
            CallbackQueryHandler(cancel_conversation, pattern=r"^(cdnsel:|menu:home)"),
        ],
        per_message=False,
    )

    # Conversation: Edit IP
    edit_ip_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(csub_edit, pattern=r"^csub:edit:")],
        states={
            ST_EDIT_IP_SELECT: [
                CallbackQueryHandler(edit_ip_select, pattern=r"^(ipedit:|cdnsel:)"),
            ],
            ST_EDIT_IP_NEW: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_edit_new_ip),
                CallbackQueryHandler(cancel_conversation, pattern=r"^(cdnsel:|menu:home)"),
            ],
        },
        fallbacks=[
            CommandHandler("start", cmd_start),
            CallbackQueryHandler(cancel_conversation, pattern=r"^(cdnsel:|menu:home)"),
        ],
        per_message=False,
    )

    # Conversation: Edit SNI
    edit_sni_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(csub_sni, pattern=r"^csub:sni:")],
        states={
            ST_EDIT_SNI: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_sni),
                CallbackQueryHandler(cancel_conversation, pattern=r"^(cdnsel:|menu:home)"),
            ],
        },
        fallbacks=[
            CommandHandler("start", cmd_start),
            CallbackQueryHandler(cancel_conversation, pattern=r"^(cdnsel:|menu:home)"),
        ],
        per_message=False,
    )

    # Conversation: Edit Host
    edit_host_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(csub_host, pattern=r"^csub:host:")],
        states={
            ST_EDIT_HOST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_host),
                CallbackQueryHandler(cancel_conversation, pattern=r"^(cdnsel:|menu:home)"),
            ],
        },
        fallbacks=[
            CommandHandler("start", cmd_start),
            CallbackQueryHandler(cancel_conversation, pattern=r"^(cdnsel:|menu:home)"),
        ],
        per_message=False,
    )

    # Register handlers (order matters!)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(add_cdn_conv)
    app.add_handler(add_ip_conv)
    app.add_handler(edit_ip_conv)
    app.add_handler(edit_sni_conv)
    app.add_handler(edit_host_conv)

    # Menu callbacks
    app.add_handler(CallbackQueryHandler(show_main_menu, pattern=r"^menu:home$"))
    app.add_handler(CallbackQueryHandler(menu_cdn_list, pattern=r"^menu:cdn_list$"))
    app.add_handler(CallbackQueryHandler(menu_cdn_delete, pattern=r"^menu:cdn_delete$"))
    app.add_handler(CallbackQueryHandler(menu_cdn_manage, pattern=r"^menu:cdn_manage$"))
    app.add_handler(CallbackQueryHandler(menu_reload, pattern=r"^menu:reload$"))

    # CDN selection
    app.add_handler(CallbackQueryHandler(handle_cdn_select, pattern=r"^cdnsel:"))

    # CDN delete confirm
    app.add_handler(CallbackQueryHandler(handle_cdn_delete_select, pattern=r"^cdndel:"))
    app.add_handler(CallbackQueryHandler(confirm_cdn_delete, pattern=r"^cdndelok:"))

    # CDN submenu actions (non-conversation)
    app.add_handler(CallbackQueryHandler(csub_list, pattern=r"^csub:list:"))
    app.add_handler(CallbackQueryHandler(csub_remove, pattern=r"^csub:remove:"))
    app.add_handler(CallbackQueryHandler(csub_clear, pattern=r"^csub:clear:"))

    # IP delete/clear confirms
    app.add_handler(CallbackQueryHandler(handle_ip_delete, pattern=r"^ipdel:"))
    app.add_handler(CallbackQueryHandler(confirm_clear_ips, pattern=r"^ipclear:"))

    cdns = list_cdns()
    total_ips = sum(len(c.get("ips", [])) for c in cdns)

    print(f"""
╔══════════════════════════════════════════╗
║       🤖 SubLink Telegram Bot            ║
║       CDN-based IP management            ║
║                                          ║
║   CDNs: {len(cdns):<33}║
║   Total IPs: {total_ips:<28}║
║                                          ║
║   Waiting for messages...                ║
╚══════════════════════════════════════════╝
    """)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
