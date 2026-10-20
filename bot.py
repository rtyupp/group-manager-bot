"""بوت إدارة المجموعات — أوامر عربية وإنجليزية (ترحيب، حماية، إشراف).

الاستضافة: GitHub Actions مجاناً (نفس نمط بوت البيتكوين).
"""
import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    ChatMemberUpdated,
    ChatPermissions,
    Message,
)

# ── إعدادات ──
TOKEN = os.environ.get("BOT_TOKEN", "")
STATE_FILE = os.environ.get("STATE_FILE", "settings.json")
WARN_LIMIT = int(os.environ.get("WARN_LIMIT", "3"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("group-bot")

bot: Bot | None = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML)) if TOKEN else None
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ── الحالة المحفوظة (تُحفظ في settings.json ويحفظها Actions في المستودع) ──
state: dict = {}
# منع تكرار الترحيب (إضافة يدوية تولّد تحديثين معاً)
_recent_welcome: dict[tuple[int, int], float] = {}


def load_state() -> None:
    global state
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except Exception:  # noqa: BLE001
        state = {}


def save_state() -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
    except Exception:  # noqa: BLE001
        log.exception("فشل حفظ الحالة")


def cfg(chat_id: int) -> dict:
    return state.setdefault(str(chat_id), {"rules": "", "welcome": "", "antilink": False, "warns": {}})


# ── أدوات مساعدة ──
def open_permissions() -> ChatPermissions:
    return ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_invite_users=True,
    )


def closed_permissions() -> ChatPermissions:
    return ChatPermissions(can_send_messages=False)


def fmt_name(user) -> str:
    name = (user.first_name or "")[:30] if user else "عضو"
    return f'<a href="tg://user?id={user.id}">{name}</a>' if user else "عضو"


async def is_admin(chat_id: int, user_id: int) -> bool:
    if not bot:
        return False
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:  # noqa: BLE001
        return False


def parse_duration(text: str | None) -> timedelta:
    """المدة بصيغة: 30m / 2h / 1d — الافتراضي ساعة."""
    if text:
        m = re.fullmatch(r"(\d+)\s*([mhd]?)", text.strip().lower())
        if m:
            n = int(m.group(1))
            return timedelta(minutes=n * {"m": 1, "h": 60, "d": 1440}.get(m.group(2) or "m", 1))
    return timedelta(hours=1)


LINK_RE = re.compile(r"(?i)(https?://|t\.me/|telegram\.me/|telegram\.dog/|joinchat/)")


def reply_target(m: Message):
    """العضو المستهدف من الرد على رسالته."""
    if not m.reply_to_message or not m.reply_to_message.from_user:
        return None
    return m.reply_to_message.from_user


# ── الترحيب بالأعضاء الجدد ──
def welcome_text(cfg_chat: dict, name_html: str, chat_title: str) -> str:
    tpl = cfg_chat.get("welcome") or "أهلاً {name} 👋 نورت {chat} 🌟"
    try:
        return tpl.format(name=name_html, chat=chat_title)
    except Exception:  # noqa: BLE001
        return tpl


async def send_welcome(m_or_u, chat, user) -> None:
    if not bot or user.is_bot:
        return
    key = (chat.id, user.id)
    now = time.time()
    if now - _recent_welcome.get(key, 0) < 60:
        return  # رحّبنا له للتو (تحديث مزدوج)
    _recent_welcome[key] = now
    try:
        await bot.send_message(chat.id, welcome_text(cfg(chat.id), fmt_name(user), chat.title or "المجموعة"))
    except Exception:  # noqa: BLE001
        log.exception("فشل إرسال الترحيب")


@router.message(F.new_chat_members)
async def on_added(m: Message) -> None:
    # إذا أُضيف البوت نفسه → تعريف سريع
    if bot and any(u.id == bot.id for u in m.new_chat_members):
        await m.answer(
            "👋 أهلاً! أنا بوت إدارة المجموعة.\n"
            "خطوة واحدة باقية: اجعلني <b>مشرفاً</b> بصلاحيات (حذف الرسائل، حظر الأعضاء، دعوة الأعضاء، تثبيت الرسائل).\n\n"
            "اكتب /مساعدة لعرض الأوامر."
        )
        return
    for u in m.new_chat_members:
        await send_welcome(m, m.chat, u)


@router.chat_member()
async def on_member_updated(event: ChatMemberUpdated) -> None:
    # انضمام عبر رابط الدعوة (الإضافة اليدوية تأتي من new_chat_members)
    if event.new_chat_member.status in ("member", "restricted") and event.old_chat_member.status in (
        "left", "kicked",
    ):
        await send_welcome(event, event.chat, event.new_chat_member.user)


# ── الحماية: ضد الروابط ──
@router.message(F.text.regexp(LINK_RE))
async def antilink(m: Message) -> None:
    if not bot or m.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if not cfg(m.chat.id).get("antilink"):
        return
    if await is_admin(m.chat.id, m.from_user.id):
        return
    try:
        await m.delete()
        await m.answer(f"🚫 ممنوع نشر الروابط هنا يا {fmt_name(m.from_user)}")
    except TelegramForbiddenError:
        log.warning("البوت طُرد من المجموعة %s", m.chat.id)
    except Exception:  # noqa: BLE001
        log.exception("فشل حذف رسالة رابط")


# ── الأوامر ──
async def cmd_start(m: Message, args: str) -> None:
    if m.chat.type == ChatType.PRIVATE:
        await m.answer(
            "👋 أهلاً! أنا بوت إدارة المجموعات.\n"
            "أضفني إلى مجموعتك ثم اجعلني مشرفاً، وسأتولى:\n"
            "• الترحيب بالأعضاء الجدد\n• الحماية من الروابط\n• الحظر والطرد والكتم والتحذيرات\n"
            "• القفل والفتح والتثبيت والمسح\n\nأوامري: /مساعدة"
        )
    else:
        await m.answer("أنا موجود وشغال ✅ اكتب /مساعدة للأوامر")


async def cmd_help(m: Message, args: str) -> None:
    await m.answer(
        "📖 <b>الأوامر</b> (تشتغل بالعربي والإنجليزي)\n\n"
        "🛡 <b>إدارة:</b>\n"
        "• /قفل /فتح — قفل وفتح المجموعة\n"
        "• /حظر — بالرد (حظر نهائي)\n• /الغاء_حظر — بالرد أو آيدي\n"
        "• /طرد — بالرد (حظر + إزالة)\n"
        "• /كتم 30m — بالرد (m دقيقة، h ساعة، d يوم)\n• /الغاء_كتم — بالرد\n"
        "• /تحذير — بالرد (٣ تحذيرات = طرد)\n• /تحذيرات — بالرد\n• /تصفير — بالرد\n\n"
        "📌 <b>رسائل:</b>\n"
        "• /تثبيت /مسح — بالرد\n\n"
        "⚙️ <b>إعدادات (للمشرف):</b>\n"
        "• /وضع_قواعد نص — وضع القواعد\n• /قواعد — عرضها\n"
        "• /وضع_ترحيب نص — (المتغيرات: {name} و {chat})\n"
        "• /ضد_روابط on/off — تشغيل/إيقاف الحماية\n\n"
        "ℹ️ /الادمنية — قائمة المشرفين\nℹ️ /ايدي — المعرفات\nℹ️ /بنق — فحص السرعة"
    )


async def cmd_rules(m: Message, args: str) -> None:
    text = cfg(m.chat.id).get("rules")
    await m.answer(f"📜 <b>قواعد المجموعة</b>\n\n{text}" if text else "ما فيه قواعد مسجّلة بعد. المشرف يقدر يضيفها بـ /وضع_قواعد")


async def cmd_setrules(m: Message, args: str) -> None:
    if not args:
        await m.answer("الاستخدام: /وضع_قواعد اكتب القواعد هنا (أو أرسلها بدون نص لمسحها)")
        cfg(m.chat.id)["rules"] = ""
    else:
        cfg(m.chat.id)["rules"] = args[:3500]
        await m.answer("✅ تم حفظ القواعد")
    save_state()


async def cmd_setwelcome(m: Message, args: str) -> None:
    if not args:
        await m.answer("الاستخدام: /وضع_ترحيب أهلاً {name} في {chat}")
        cfg(m.chat.id)["welcome"] = ""
    else:
        cfg(m.chat.id)["welcome"] = args[:1500]
        await m.answer("✅ تم حفظ رسالة الترحيب")
    save_state()


async def cmd_lock(m: Message, args: str) -> None:
    await bot.set_chat_permissions(m.chat.id, permissions=closed_permissions())
    await m.answer("🔒 تم قفل المجموعة")


async def cmd_unlock(m: Message, args: str) -> None:
    await bot.set_chat_permissions(m.chat.id, permissions=open_permissions())
    await m.answer("🔓 تم فتح المجموعة")


async def _guard_target(m: Message):
    t = reply_target(m)
    if not t:
        await m.answer("استخدم الأمر بالرد على رسالة العضو")
        return None
    if t.id == (bot.id if bot else 0):
        await m.answer("😅 ما أقدر أطبق الأمر عليّ")
        return None
    if await is_admin(m.chat.id, t.id):
        await m.answer("🚫 ما أقدر أطبق الأمر على مشرف")
        return None
    return t


async def cmd_ban(m: Message, args: str) -> None:
    t = await _guard_target(m)
    if t:
        await bot.ban_chat_member(m.chat.id, t.id)
        await m.answer(f"⛔ تم حظر {fmt_name(t)}")


async def cmd_unban(m: Message, args: str) -> None:
    t = reply_target(m)
    if not t:
        await m.answer("استخدم الأمر بالرد على رسالة العضو")
        return
    await bot.unban_chat_member(m.chat.id, t.id, only_if_banned=True)
    await m.answer(f"✅ تم رفع الحظر عن {fmt_name(t)}")


async def cmd_kick(m: Message, args: str) -> None:
    t = await _guard_target(m)
    if t:
        await bot.ban_chat_member(m.chat.id, t.id)
        await bot.unban_chat_member(m.chat.id, t.id, only_if_banned=True)
        await m.answer(f"👢 تم طرد {fmt_name(t)} (يقدر يرجع بالدعوة)")


async def cmd_mute(m: Message, args: str) -> None:
    t = await _guard_target(m)
    if t:
        until = datetime.now(timezone.utc) + parse_duration(args)
        await bot.restrict_chat_member(m.chat.id, t.id, permissions=closed_permissions(), until_date=until)
        await m.answer(f"🔇 تم كتم {fmt_name(t)} لمدة {args or '60m'}")


async def cmd_unmute(m: Message, args: str) -> None:
    t = reply_target(m)
    if not t:
        await m.answer("استخدم الأمر بالرد على رسالة العضو")
        return
    await bot.restrict_chat_member(m.chat.id, t.id, permissions=open_permissions())
    await m.answer(f"🔊 تم فك الكتم عن {fmt_name(t)}")


async def cmd_warn(m: Message, args: str) -> None:
    t = await _guard_target(m)
    if not t:
        return
    warns = cfg(m.chat.id)["warns"]
    count = warns.get(str(t.id), 0) + 1
    warns[str(t.id)] = count
    save_state()
    if count >= WARN_LIMIT:
        await bot.ban_chat_member(m.chat.id, t.id)
        warns.pop(str(t.id), None)
        save_state()
        await m.answer(f"⛔ {fmt_name(t)} وصل {WARN_LIMIT} تحذيرات → تم حظره")
    else:
        await m.answer(f"⚠️ تحذير {count}/{WARN_LIMIT} لـ {fmt_name(t)}")


async def cmd_warns(m: Message, args: str) -> None:
    t = reply_target(m) or m.from_user
    count = cfg(m.chat.id)["warns"].get(str(t.id), 0)
    await m.answer(f"⚠️ عدد تحذيرات {fmt_name(t)}: {count}/{WARN_LIMIT}")


async def cmd_resetwarns(m: Message, args: str) -> None:
    t = reply_target(m)
    if not t:
        await m.answer("استخدم الأمر بالرد على رسالة العضو")
        return
    cfg(m.chat.id)["warns"].pop(str(t.id), None)
    save_state()
    await m.answer(f"♻️ تم تصفير تحذيرات {fmt_name(t)}")


async def cmd_pin(m: Message, args: str) -> None:
    if not m.reply_to_message:
        await m.answer("استخدم الأمر بالرد على الرسالة المطلوب تثبيتها")
        return
    await m.reply_to_message.pin()
    await m.answer("📌 تم التثبيت")


async def cmd_del(m: Message, args: str) -> None:
    if not m.reply_to_message:
        await m.answer("استخدم الأمر بالرد على الرسالة المطلوب حذفها")
        return
    await m.reply_to_message.delete()
    await m.delete()


async def cmd_antilink(m: Message, args: str) -> None:
    val = args.strip().lower() in ("on", "تشغيل", "1", "yes")
    cfg(m.chat.id)["antilink"] = val
    save_state()
    await m.answer("🛡 الحماية من الروابط: مفعّلة" if val else "🛡 الحماية من الروابط: موقّفة")


async def cmd_adminlist(m: Message, args: str) -> None:
    admins = await bot.get_chat_administrators(m.chat.id)
    lines = [f"• {fmt_name(a.user)} — {'المالك 👑' if a.status == 'creator' else 'مشرف'}" for a in admins]
    await m.answer("👥 <b>المشرفون</b>\n" + "\n".join(lines))


async def cmd_id(m: Message, args: str) -> None:
    text = f"🆔 المجموعة: <code>{m.chat.id}</code>\n🆔 معرفك: <code>{m.from_user.id}</code>"
    if m.reply_to_message and m.reply_to_message.from_user:
        text += f"\n🆔 معرف المُردود عليه: <code>{m.reply_to_message.from_user.id}</code>"
    await m.answer(text)


async def cmd_ping(m: Message, args: str) -> None:
    await m.answer("pong ⚡")


COMMANDS: dict[str, object] = {
    "start": cmd_start, "بدا": cmd_start,
    "help": cmd_help, "مساعدة": cmd_help, "الاوامر": cmd_help,
    "rules": cmd_rules, "قواعد": cmd_rules,
    "setrules": cmd_setrules, "وضع_قواعد": cmd_setrules,
    "setwelcome": cmd_setwelcome, "وضع_ترحيب": cmd_setwelcome,
    "lock": cmd_lock, "قفل": cmd_lock,
    "unlock": cmd_unlock, "فتح": cmd_unlock,
    "ban": cmd_ban, "حظر": cmd_ban,
    "unban": cmd_unban, "الغاء_حظر": cmd_unban,
    "kick": cmd_kick, "طرد": cmd_kick,
    "mute": cmd_mute, "كتم": cmd_mute,
    "unmute": cmd_unmute, "الغاء_كتم": cmd_unmute,
    "warn": cmd_warn, "تحذير": cmd_warn,
    "warns": cmd_warns, "تحذيرات": cmd_warns,
    "resetwarns": cmd_resetwarns, "تصفير": cmd_resetwarns,
    "pin": cmd_pin, "تثبيت": cmd_pin,
    "del": cmd_del, "مسح": cmd_del,
    "antilink": cmd_antilink, "ضد_روابط": cmd_antilink,
    "adminlist": cmd_adminlist, "الادمنية": cmd_adminlist,
    "id": cmd_id, "ايدي": cmd_id,
    "ping": cmd_ping, "بنق": cmd_ping,
}

ADMIN_CMDS = {
    "setrules", "وضع_قواعد", "setwelcome", "وضع_ترحيب", "lock", "قفل", "unlock", "فتح",
    "ban", "حظر", "unban", "الغاء_حظر", "kick", "طرد", "mute", "كتم", "unmute", "الغاء_كتم",
    "warn", "تحذير", "resetwarns", "تصفير", "pin", "تثبيت", "del", "مسح", "antilink", "ضد_روابط",
    "adminlist", "الادمنية",
}

# كشف عام للأوامر (يدعم العربية) — يُسجَّل أخيراً


@router.message(F.text)
async def dispatcher(m: Message) -> None:
    if not m.text or not m.text.startswith("/"):
        return
    token = m.text.split()[0].lstrip("/").split("@")[0].lower()
    handler = COMMANDS.get(token)
    if not handler:
        await m.answer("أمر غير معروف 🤔 جرّب /مساعدة")
        return
    if m.chat.type == ChatType.PRIVATE and token in ADMIN_CMDS:
        await m.answer("هذا الأمر يشتغل داخل المجموعة فقط — أضفني إلى مجموعتك 👋")
        return
    if token in ADMIN_CMDS and not await is_admin(m.chat.id, m.from_user.id):
        await m.answer("🚫 هذا الأمر للمشرفين فقط")
        return
    args = m.text.split(maxsplit=1)[1] if len(m.text.split(maxsplit=1)) > 1 else ""
    try:
        await handler(m, args)
    except TelegramBadRequest as e:
        await m.answer(f"⚠️ تيليجرام رفض الأمر: تأكد أن لي صلاحيات المشرف اللازمة ({e})")
    except TelegramForbiddenError:
        log.warning("البوت طُرد من %s", m.chat.id)
    except Exception:  # noqa: BLE001
        log.exception("خطأ في تنفيذ %s", token)
        await m.answer("⚠️ صار خطأ غير متوقع، جرّب مرة ثانية")


# ── التشغيل ──
async def on_startup() -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="تشغيل البوت"),
        BotCommand(command="help", description="قائمة الأوامر"),
    ], scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands([
        BotCommand(command="rules", description="قواعد المجموعة"),
        BotCommand(command="lock", description="قفل المجموعة"),
        BotCommand(command="unlock", description="فتح المجموعة"),
        BotCommand(command="ban", description="حظر عضو (بالرد)"),
        BotCommand(command="kick", description="طرد عضو (بالرد)"),
        BotCommand(command="mute", description="كتم عضو (بالرد)"),
        BotCommand(command="warn", description="تحذير عضو (بالرد)"),
        BotCommand(command="pin", description="تثبيت رسالة (بالرد)"),
        BotCommand(command="del", description="حذف رسالة (بالرد)"),
        BotCommand(command="antilink", description="الحماية من الروابط"),
        BotCommand(command="id", description="المعرّفات"),
    ], scope=BotCommandScopeAllGroupChats())
    log.info("البوت جاهز ✅")


async def main() -> int:
    if not TOKEN:
        print("⚠️ BOT_TOKEN غير موجود — أضفه في GitHub Secrets ثم شغّل سير العمل يدوياً (workflow_dispatch)")
        return 0
    load_state()
    await on_startup()
    try:
        me = await bot.get_me()
        log.info("مسجّل كـ @%s", me.username)
    except Exception:  # noqa: BLE001
        log.exception("فشل getMe — التوكن غير صالح؟")
        return 1
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
