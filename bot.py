import asyncio
import html
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from datetime import time as dtime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.constants import ChatType
from telegram.error import (
    BadRequest, RetryAfter, TelegramError, Forbidden, ChatMigrated, TimedOut, NetworkError,
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ChatMemberHandler, ContextTypes, filters,
)

# ---------- 日志 ----------
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("broadcastbot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.INFO)

# ---------- 配置 ----------
try:
    TOKEN = os.environ["BOT_TOKEN"]  # 从 @BotFather 拿到的 token，放在环境变量里
except KeyError:
    logger.critical("❌ 未找到环境变量 BOT_TOKEN（从 @BotFather 获取）")
    raise

# 管理员：用户名（不带@）或用户ID。也可用环境变量 ADMIN_IDS=123,456 追加
ADMIN_USERNAMES = {"IgAccJohn", "IgCsLyn", "MrK6776", "", "Dragonball77"}
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()}

_data_dir = os.path.realpath(os.environ.get("BOT_DATA_DIR", "./data"))
os.makedirs(_data_dir, exist_ok=True)
DATA_FILE = os.path.join(_data_dir, "broadcast_data.json")

PAGE = 8  # 勾选群时每页显示数量

DEFAULT_DATA = {
    "tz_offset": 8,
    "next_id": 1,
    "chats": {},       # {"chat_id": "群名"}   Bot 所在的群
    "categories": {},  # {"c1": {"name": "..", "chats": [chat_id, ...]}}
    "scripts": {},     # {"s2": {"name": "..", "preview": "..", "chat_id": 私聊ID, "message_id": 消息ID}}
    "jobs": {},        # {"j3": {cid, sid, cat_name, script_name, run_at, by, status}}
    "report_target": None,  # 固定报告位置 {"chat_id", "thread_id", "title"}，None = 发在操作所在聊天
    "run_log": [],     # 定时任务运行记录（每日汇总用），只保留 3 天
}


# ---------- 存取 ----------
def load_data():
    d = {}
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    for k, v in DEFAULT_DATA.items():
        if k not in d:
            d[k] = json.loads(json.dumps(v))
    return d


def save_data(d):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


def new_id(d, prefix):
    n = d["next_id"]
    d["next_id"] = n + 1
    return f"{prefix}{n}"


# ---------- 工具 ----------
def is_admin(user) -> bool:
    if user is None:
        return False
    if user.id in ADMIN_IDS:
        return True
    return bool(user.username) and user.username.lower() in {u.lower() for u in ADMIN_USERNAMES}


def esc(s):
    return html.escape(str(s))


def short(s, n=24):
    s = str(s).replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def btn(text, data):
    return InlineKeyboardButton(text, callback_data=data)


def kb(rows):
    return InlineKeyboardMarkup(rows)


def get_tz(d):
    return timezone(timedelta(hours=d.get("tz_offset", 8)))


def fmt_dt(dt):
    return dt.strftime("%Y-%m-%d %H:%M")


def fmt_offset(d):
    return f"UTC{d.get('tz_offset', 8):+g}"


def parse_time(text, tz):
    """支持 HH:MM / MM-DD HH:MM / YYYY-MM-DD HH:MM，返回带时区的 datetime，格式不对返回 None。"""
    text = text.strip().replace("：", ":")
    now = datetime.now(tz)
    for fmt, kind in (("%Y-%m-%d %H:%M", "full"), ("%m-%d %H:%M", "md"), ("%H:%M", "hm")):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if kind == "full":
            return dt.replace(tzinfo=tz)
        if kind == "md":
            try:
                return dt.replace(year=now.year, tzinfo=tz)
            except ValueError:
                return None
        dt = now.replace(hour=dt.hour, minute=dt.minute, second=0, microsecond=0)
        if dt <= now:
            dt += timedelta(days=1)
        return dt
    return None


def parse_hhmm(text):
    """解析每日时间：20:30 / 2030 / 930 / 20，返回 'HH:MM'，格式不对返回 None。"""
    t = text.strip().replace("：", ":")
    try:
        if ":" in t:
            h, m = (int(x) for x in t.split(":", 1))
        elif t.isdigit() and len(t) in (3, 4):
            h, m = int(t[:-2]), int(t[-2:])
        elif t.isdigit() and len(t) <= 2:
            h, m = int(t), 0
        else:
            return None
    except ValueError:
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return f"{h:02d}:{m:02d}"
    return None


def register_daily(job_queue, jid, hhmm, offset):
    """注册每日任务。把本地时间换算成 UTC 钟点，避免时区对象兼容问题。"""
    h, m = (int(x) for x in hhmm.split(":"))
    total = (h * 60 + m - int(round(offset * 60))) % 1440
    job_queue.run_daily(scheduled_cb, time=dtime(total // 60, total % 60), name=jid, data=jid)


def describe_message(msg):
    if msg.text:
        return msg.text
    if msg.photo:
        label = "[图片]"
    elif msg.video:
        label = "[视频]"
    elif msg.animation:
        label = "[动图]"
    elif msg.document:
        label = "[文件]"
    elif msg.voice:
        label = "[语音]"
    elif msg.sticker:
        label = "[贴纸]"
    else:
        label = "[消息]"
    return f"{label} {msg.caption or ''}".strip()


def get_flow(context):
    flow = context.user_data.get("flow")
    if flow is None:
        flow = {"cid": None, "sid": None, "when": None}
        context.user_data["flow"] = flow
    return flow


def set_await(context, mode, chat_id):
    """记录「正在等这个管理员在这个聊天里输入什么」，防止群里正常聊天被误当成输入。"""
    context.user_data["await"] = mode
    context.user_data["await_chat"] = chat_id


async def show(update: Update, text, markup=None):
    q = update.callback_query
    if q:
        try:
            await q.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
    else:
        await update.effective_message.reply_text(text, reply_markup=markup, parse_mode="HTML")


async def notify(bot, chat_id, text):
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    except Exception:
        logger.exception("通知 %s 失败", chat_id)


def report_where(d) -> str:
    """报告发到哪里（HTML 用）：固定位置的群名 / 话题，未设置时是「操作所在聊天」。"""
    t = d.get("report_target")
    if not t:
        return "操作所在聊天"
    return esc(t["title"]) + (" / 话题" if t.get("thread_id") else "")


def report_line(d) -> str:
    """设置了固定报告位置时，在「开始发送」的提示里附一行说明。"""
    return f"\n📋 报告将发到：{report_where(d)}" if d.get("report_target") else ""


async def send_report(bot, fallback_chat, text):
    """群发 / 定时报告：有固定位置就发到那里，发不了就退回到操作所在的聊天。"""
    try:
        t = load_data().get("report_target")
        if not t:
            await notify(bot, fallback_chat, text)
            return
        try:
            await bot.send_message(
                chat_id=t["chat_id"], text=text,
                message_thread_id=t.get("thread_id"), parse_mode="HTML",
            )
        except (BadRequest, Forbidden):
            logger.exception("固定报告位置发送失败，改发到操作所在聊天")
            await notify(bot, fallback_chat, text + "\n\n⚠️ 固定话题发送失败，已改发到这里，请重新 /setreport")
    except Exception:
        logger.exception("发送报告失败")


# ---------- 群登记 ----------
_seen_chats = {}  # 内存缓存，避免群里每条消息都读写磁盘


def register_chat(chat) -> bool:
    title = chat.title or str(chat.id)
    if _seen_chats.get(chat.id) == title:
        return False
    d = load_data()
    key = str(chat.id)
    changed = d["chats"].get(key) != title
    if changed:
        d["chats"][key] = title
        save_data(d)
    _seen_chats[chat.id] = title
    return changed


def unregister_chat(chat_id):
    _seen_chats.pop(chat_id, None)
    d = load_data()
    d["chats"].pop(str(chat_id), None)
    for cat in d["categories"].values():
        if chat_id in cat["chats"]:
            cat["chats"].remove(chat_id)
    save_data(d)


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.my_chat_member
    chat = cm.chat
    if chat.type == ChatType.PRIVATE:
        return
    status = cm.new_chat_member.status
    if status in ("member", "administrator"):
        register_chat(chat)
        logger.info("已登记：%s (%s)", chat.title, chat.id)
    elif status in ("left", "kicked"):
        unregister_chat(chat.id)
        logger.info("已移除：%s (%s)", chat.title, chat.id)


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat:
        register_chat(chat)


async def register_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        return
    chat = update.effective_chat
    register_chat(chat)
    await update.message.reply_text(f"✅ 已登记本群：{chat.title}")


# ---------- 发送核心 ----------
def _secs(ra):
    return ra.total_seconds() if isinstance(ra, timedelta) else float(ra)


def explain_error(e) -> str:
    """把发送时的报错翻译成管理员看得懂的原因。"""
    s = str(e).lower()
    if isinstance(e, ChatMigrated):
        return "群已升级成超级群，请重新登记"
    if "topic_closed" in s or "topic closed" in s:
        return "该群话题已关闭"
    if isinstance(e, Forbidden):
        if "kicked" in s:
            return "Bot 已被踢出群"
        if any(k in s for k in ("not enough rights", "write_forbidden", "muted", "have no rights")):
            return "Bot 没有发言权限（仅管理员发言 / 被禁言）"
        return "Bot 已被踢出群或无权限"
    if isinstance(e, BadRequest):
        if "chat not found" in s:
            return "找不到这个群（已解散或 Bot 不在）"
        if "message to copy not found" in s or "message_id_invalid" in s:
            return "脚本原消息已被删除，请重新保存脚本"
        return str(e)[:80]
    if isinstance(e, RetryAfter):
        return "被 Telegram 限流"
    if isinstance(e, (TimedOut, NetworkError)):
        return "网络超时"
    return str(e)[:80]


async def broadcast_result(bot, cid, sid) -> dict:
    d = load_data()
    cat = d["categories"].get(cid)
    script = d["scripts"].get(sid)
    if not cat or not script:
        return {
            "ok": False, "error": "分类或脚本已被删除，未发送",
            "cat_name": cat["name"] if cat else "", "script_name": script["name"] if script else "",
            "total": 0, "ok_count": 0, "fails": [],
        }

    ok = 0
    fails = []
    for chat_id in list(cat["chats"]):
        name = d["chats"].get(str(chat_id), str(chat_id))
        last_err = None
        for _ in range(2):
            try:
                await bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=script["chat_id"],
                    message_id=script["message_id"],
                )
                last_err = None
                break
            except RetryAfter as e:
                last_err = e
                await asyncio.sleep(_secs(e.retry_after) + 1)
            except ChatMigrated as e:
                last_err = e
                break
            except TelegramError as e:
                last_err = e
                break
        if last_err is None:
            ok += 1
        else:
            fails.append({"chat_id": chat_id, "name": name, "reason": explain_error(last_err)})
        await asyncio.sleep(0.1)  # 避免触发限流

    return {
        "ok": True, "error": None,
        "cat_name": cat["name"], "script_name": script["name"],
        "total": len(cat["chats"]), "ok_count": ok,
        "fails": fails,
    }


def format_report(r) -> str:
    if not r["ok"]:
        return f"❌ {r['error']}"
    lines = [
        "📢 <b>群发完成</b>",
        f"分类：{esc(r['cat_name'])}",
        f"脚本：{esc(r['script_name'])}",
        f"✅ 成功：{r['ok_count']}",
        f"❌ 失败：{len(r['fails'])}",
    ]
    for f in r["fails"][:15]:
        lines.append(f"  • {esc(f['name'])}：{esc(f['reason'][:80])}")
    if len(r["fails"]) > 15:
        lines.append(f"  …还有 {len(r['fails']) - 15} 个失败")
    return "\n".join(lines)


async def do_broadcast(bot, cid, sid) -> str:
    return format_report(await broadcast_result(bot, cid, sid))


def drop_temp_script(sid):
    """即兴脚本发送完（或任务取消）后删除；普通脚本不动。"""
    d = load_data()
    s = d["scripts"].get(sid)
    if s and s.get("temp"):
        del d["scripts"][sid]
        save_data(d)


async def run_now_task(bot, cid, sid, report_chat):
    try:
        report = await do_broadcast(bot, cid, sid)
    except Exception:
        logger.exception("群发出错")
        report = "❌ 群发过程出错，请查看日志"
    drop_temp_script(sid)
    await send_report(bot, report_chat, report)


async def scheduled_cb(context: ContextTypes.DEFAULT_TYPE):
    jid = context.job.data
    d = load_data()
    job = d["jobs"].get(jid)
    if not job or job["status"] != "pending":
        return
    daily = job.get("repeat") == "daily"
    chat = job.get("report_chat", job["by"])
    job["status"] = "running"
    save_data(d)
    status = "pending" if daily else "done"  # 每日任务发完回到 pending，明天继续
    try:
        result = await broadcast_result(context.bot, job["cid"], job["sid"])
    except Exception:
        logger.exception("定时任务 %s 出错", jid)
        result = {"ok": False, "error": "群发过程出错，请查看日志", "total": 0,
                  "ok_count": 0, "fails": []}
        status = "pending" if daily else "failed"

    d = load_data()
    now = datetime.now(get_tz(d))
    if jid in d["jobs"] and d["jobs"][jid]["status"] == "running":
        d["jobs"][jid]["status"] = status
    # 运行记录：给每日汇总用，只保留 3 天
    d["run_log"].append({
        "date": now.strftime("%Y-%m-%d"),
        "jid": jid,
        "kind": "daily" if daily else "once",
        "cat_name": job["cat_name"],
        "script_name": job["script_name"],
        "time": job.get("time") or datetime.fromisoformat(job["run_at"]).strftime("%H:%M"),
        "total": result["total"],
        "ok": result["ok_count"],
        "fails": result["fails"],
        "error": result["error"],
        "summarized": False,
    })
    cutoff = (now - timedelta(days=3)).strftime("%Y-%m-%d")
    d["run_log"] = [e for e in d["run_log"] if e["date"] >= cutoff]
    save_data(d)
    if not daily:
        drop_temp_script(job["sid"])

    # 全部成功：不打扰，只记日志（当天汇总会统一发）；有失败：立即报警
    fails = result["fails"]
    if result["ok"] and not fails:
        logger.info("定时任务 %s 全部发送成功（%d 个群）", jid, result["ok_count"])
    else:
        where = f"每天 {job.get('time')}" if daily else "单次"
        head = f"⚠️ 任务 <code>{jid}</code>（{where}）{esc(job['cat_name'])} → {esc(job['script_name'])}"
        if not result["ok"]:
            lines = [f"{head}：{esc(result['error'])}"]
        else:
            lines = [f"{head}：{len(fails)} 个群失败"]
            for f in fails[:30]:
                lines.append(f"• {esc(f['name'])}：{esc(f['reason'])}")
            if len(fails) > 30:
                lines.append(f"…还有 {len(fails) - 30} 个")
        await send_report(context.bot, chat, "\n".join(lines))

    await maybe_send_daily_summary(context.bot, chat, jid)


async def maybe_send_daily_summary(bot, fallback_chat, jid=None):
    """今天的定时任务都跑完后，只发一条汇总；还有没跑完的，就等最后一个再发。"""
    d = load_data()
    tz = get_tz(d)
    now = datetime.now(tz)
    today = now.strftime("%Y-%m-%d")
    hm = now.strftime("%H:%M")
    for other, j in d["jobs"].items():
        if other == jid or j["status"] not in ("pending", "running"):
            continue
        if j["status"] == "running":
            return
        if j.get("repeat") == "daily":
            already_ran = any(e["jid"] == other and e["date"] == today for e in d["run_log"])
            if j["time"] > hm or (j["time"] == hm and not already_ran):
                return
        else:
            run_local = datetime.fromisoformat(j["run_at"]).astimezone(tz)
            if run_local > now and run_local.strftime("%Y-%m-%d") == today:
                return

    entries = [e for e in d["run_log"] if e["date"] == today and not e["summarized"]]
    if not entries:
        return
    fail_lines = []
    for e in entries:
        if e["error"]:
            fail_lines.append(f"• {esc(e['cat_name'])}（{esc(e['script_name'])}）：{esc(e['error'])}")
        for f in e["fails"]:
            fail_lines.append(f"• {esc(f['name'])}（{esc(e['cat_name'])}）：{esc(f['reason'])}")
    sum_total = sum(e["total"] for e in entries)
    sum_ok = sum(e["ok"] for e in entries)
    sum_fail = len(fail_lines)
    lines = [
        f"📊 <b>今日群发汇总</b>（{today}）",
        f"任务 {len(entries)} 个｜发送 {sum_total} 次｜成功 {sum_ok}｜失败 {sum_fail}",
    ]
    if sum_fail == 0:
        lines.append("🎉 今天全部成功")
    else:
        lines.append("❌ 失败的群：")
        lines += fail_lines[:30]
        if sum_fail > 30:
            lines.append(f"…还有 {sum_fail - 30} 个")
    await send_report(bot, fallback_chat, "\n".join(lines))

    d = load_data()
    for e in d["run_log"]:
        if e["date"] == today:
            e["summarized"] = True
    save_data(d)


# ---------- 界面：主菜单 ----------
async def screen_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("await", None)
    context.user_data.pop("flow", None)
    d = load_data()
    pending = sum(1 for j in d["jobs"].values() if j["status"] == "pending")
    text = (
        "📢 <b>群发广播</b>\n\n"
        f"已登记群：{len(d['chats'])} 个\n"
        f"分类：{len(d['categories'])} 个\n"
        f"脚本：{len(d['scripts'])} 个\n"
        f"待执行定时：{pending} 个\n"
        f"时区：{fmt_offset(d)}\n"
        f"📍 报告位置：{report_where(d)}"
    )
    rows = [
        [btn("📢 开始群发", "bc:new")],
        [btn("🗂 群组分类", "bc:cats"), btn("📝 脚本管理", "bc:scripts")],
        [btn("⏰ 定时任务 / 取消定时", "bc:jobs")],
    ]
    await show(update, text, kb(rows))


# ---------- 界面：群发流程 ----------
async def screen_pick_cat(update, context):
    context.user_data["flow"] = {"cid": None, "sid": None, "when": None}
    context.user_data.pop("await", None)
    d = load_data()
    rows = [
        [btn(f"🗂 {short(c['name'])}（{len(c['chats'])}群）", f"bc:fc:{cid}")]
        for cid, c in d["categories"].items()
    ]
    rows.append([btn("⚙️ 新建 / 管理分类", "bc:cats")])
    rows.append([btn("🏠 主菜单", "bc:menu")])
    text = "📢 <b>第 1/3 步：选择群组分类</b>"
    if not d["categories"]:
        text += "\n\n还没有分类，点「➕ 新建分类」"
    await show(update, text, kb(rows))


async def screen_pick_script(update, context):
    # 进入这一步就等着收文案：管理员直接发文字/图片/视频，就当作这次的即兴文案
    set_await(context, "adhoc", update.effective_chat.id)
    flow = get_flow(context)
    d = load_data()
    cat = d["categories"].get(flow["cid"])
    if not cat:
        await screen_pick_cat(update, context)
        return
    rows = [
        [btn(f"📝 {short(s['name'])}", f"bc:fs:{sid}")]
        for sid, s in d["scripts"].items() if not s.get("temp")
    ]
    rows.append([btn("➕ 新建脚本（存起来重复用）", "bc:snew")])
    rows.append([btn("⬅ 上一步", "bc:new"), btn("🏠 主菜单", "bc:menu")])
    text = (
        f"📢 <b>第 2/3 步：写文案 / 选脚本</b>\n\n分类：{esc(cat['name'])}（{len(cat['chats'])}群）\n\n"
        "✍️ <b>直接发送这次的文案</b>（文字 / 图片 / 视频都行），发完不会保存\n"
        "📝 或点下面选一个已存的脚本\n\n"
        "⚠️ 发送前请不要删除你发的那条文案"
    )
    await show(update, text, kb(rows))


async def screen_pick_confirm(update, context, sid):
    """点了已存脚本后，先看内容，再决定用不用。"""
    context.user_data.pop("await", None)
    d = load_data()
    s = d["scripts"].get(sid)
    if not s:
        await screen_pick_script(update, context)
        return
    text = f"📝 <b>{esc(s['name'])}</b>\n\n{esc(s['preview'])}\n\n———\n确定使用这个脚本吗？"
    rows = [
        [btn("✅ 使用这个脚本", f"bc:fu:{sid}")],
        [btn("👁 看原样（发一条到这里）", f"bc:spv:{sid}")],
        [btn("⬅ 换一个", "bc:fb")],
    ]
    await show(update, text, kb(rows))


async def screen_timing(update, context):
    context.user_data.pop("await", None)
    flow = get_flow(context)
    d = load_data()
    cat = d["categories"].get(flow["cid"])
    script = d["scripts"].get(flow["sid"])
    if not cat or not script:
        await screen_main(update, context)
        return
    text = (
        "📢 <b>第 3/3 步：选择发送时间</b>\n\n"
        f"分类：{esc(cat['name'])}（{len(cat['chats'])}群）\n"
        f"脚本：{esc(script['name'])}"
    )
    rows = [
        [btn("⚡ 马上发送", "bc:go:now")],
        [btn("🔁 每日定时", "bc:go:daily"), btn("⏰ 单次定时", "bc:go:later")],
        [btn("🛑 取消定时（查看全部）", "bc:jobs")],
        [btn("⬅ 上一步", f"bc:fc:{flow['cid']}"), btn("🏠 主菜单", "bc:menu")],
    ]
    await show(update, text, kb(rows))


async def screen_confirm(update, context):
    context.user_data.pop("await", None)
    flow = get_flow(context)
    d = load_data()
    cat = d["categories"].get(flow["cid"])
    script = d["scripts"].get(flow["sid"])
    if not cat or not script:
        await screen_main(update, context)
        return
    when = flow.get("when")
    if flow.get("daily"):
        when_txt = f"🔁 每天 {flow['daily']}（{fmt_offset(d)}），直到你取消"
        ok_txt = "✅ 确认每日定时"
    elif when:
        when_txt = f"⏰ {fmt_dt(datetime.fromisoformat(when))}（{fmt_offset(d)}）"
        ok_txt = "✅ 确认安排"
    else:
        when_txt = "⚡ 马上发送"
        ok_txt = "✅ 确认发送"
    text = (
        "📢 <b>请确认</b>\n\n"
        f"分类：{esc(cat['name'])}（{len(cat['chats'])}群）\n"
        f"脚本：{esc(script['name'])}\n"
        f"时间：{when_txt}"
    )
    rows = [[btn(ok_txt, "bc:ok")], [btn("❌ 取消", "bc:menu")]]
    await show(update, text, kb(rows))


# ---------- 界面：分类管理 ----------
async def screen_cats(update, context):
    context.user_data.pop("await", None)
    d = load_data()
    rows = [
        [btn(f"🗂 {short(c['name'])}（{len(c['chats'])}群）", f"bc:ce:{cid}:0")]
        for cid, c in d["categories"].items()
    ]
    rows.append([btn("➕ 新建分类", "bc:cnew")])
    back = "bc:new" if context.user_data.get("flow") is not None else "bc:menu"
    rows.append([btn("⬅ 返回", back)])
    text = "🗂 <b>群组分类</b>\n\n点分类可勾选里面的群"
    if not d["chats"]:
        text += "\n\n⚠️ 还没登记任何群：把 Bot 拉进群，或在群里发 /register"
    await show(update, text, kb(rows))


async def screen_cat_edit(update, context, cid, page=0, notice=""):
    context.user_data.pop("await", None)
    d = load_data()
    cat = d["categories"].get(cid)
    if not cat:
        await screen_cats(update, context)
        return
    chats = sorted(d["chats"].items(), key=lambda kv: kv[1].lower())
    pages = max(1, -(-len(chats) // PAGE))
    page = max(0, min(page, pages - 1))
    sel = set(cat["chats"])

    rows = []
    for key, title in chats[page * PAGE:(page + 1) * PAGE]:
        mark = "✅" if int(key) in sel else "⬜"
        rows.append([btn(f"{mark} {short(title, 30)}", f"bc:ct:{cid}:{key}:{page}")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(btn("◀", f"bc:ce:{cid}:{page - 1}"))
        nav.append(btn(f"{page + 1}/{pages}", "bc:noop"))
        if page < pages - 1:
            nav.append(btn("▶", f"bc:ce:{cid}:{page + 1}"))
        rows.append(nav)
    rows.append([btn("✏️ 改名", f"bc:cren:{cid}"), btn("🗑 删除", f"bc:cdel:{cid}")])
    if context.user_data.get("flow") is not None:
        rows.append([btn("✅ 完成，下一步", f"bc:fc:{cid}")])
    else:
        rows.append([btn("✅ 完成", "bc:cats")])

    text = f"🗂 <b>{esc(cat['name'])}</b>（已选 {len(sel)} 群）\n点下面的群来勾选/取消"
    if not chats:
        text += "\n\n⚠️ 还没登记任何群：把 Bot 拉进群，或在群里发 /register"
    if notice:
        text += f"\n\n{notice}"
    await show(update, text, kb(rows))


# ---------- 界面：脚本管理 ----------
async def screen_scripts(update, context):
    context.user_data.pop("await", None)
    d = load_data()
    rows = [
        [btn(f"📝 {short(s['name'])}", f"bc:se:{sid}")]
        for sid, s in d["scripts"].items() if not s.get("temp")
    ]
    rows.append([btn("➕ 新建脚本", "bc:snew")])
    rows.append([btn("🏠 主菜单", "bc:menu")])
    await show(update, "📝 <b>脚本管理</b>\n\n脚本 = 要群发的那条消息（文字/图片/视频都行）", kb(rows))


async def screen_script_view(update, context, sid):
    context.user_data.pop("await", None)
    d = load_data()
    s = d["scripts"].get(sid)
    if not s:
        await screen_scripts(update, context)
        return
    text = f"📝 <b>{esc(s['name'])}</b>\n\n{esc(s['preview'])}"
    rows = [
        [btn("👁 发一条给我预览", f"bc:spv:{sid}")],
        [btn("📄 改内容", f"bc:sedit:{sid}"), btn("✏️ 改名", f"bc:sren:{sid}")],
        [btn("🗑 删除", f"bc:sdel:{sid}"), btn("⬅ 返回", "bc:scripts")],
    ]
    await show(update, text, kb(rows))


# ---------- 界面：定时任务 ----------
async def screen_jobs(update, context):
    context.user_data.pop("await", None)
    d = load_data()
    pend = sorted(
        ((jid, j) for jid, j in d["jobs"].items() if j["status"] == "pending"),
        key=lambda x: (0, x[1].get("time", "")) if x[1].get("repeat") == "daily" else (1, x[1]["run_at"]),
    )[:20]
    if pend:
        lines = ["⏰ <b>定时任务</b>", ""]
        for jid, j in pend:
            if j.get("repeat") == "daily":
                when_txt = f"🔁 每天 {j['time']}"
            else:
                when_txt = fmt_dt(datetime.fromisoformat(j["run_at"]))
            lines.append(f"• <code>{jid}</code> {when_txt}｜{esc(j['cat_name'])} → {esc(j['script_name'])}")
        text = "\n".join(lines)
    else:
        text = "⏰ <b>定时任务</b>\n\n（没有定时任务）"
    rows = [[btn(f"🛑 取消定时 {jid}", f"bc:jc:{jid}")] for jid, _ in pend]
    rows.append([btn("🏠 主菜单", "bc:menu")])
    await show(update, text, kb(rows))


# ---------- 按钮回调 ----------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user):
        await q.answer("只有管理员能操作", show_alert=True)
        return
    await q.answer()
    p = q.data.split(":")
    act = p[1]

    if act == "noop":
        return
    if act == "menu":
        await screen_main(update, context)

    # ---- 群发流程 ----
    elif act == "new":
        await screen_pick_cat(update, context)
    elif act == "fc":
        cid = p[2]
        d = load_data()
        cat = d["categories"].get(cid)
        if not cat:
            await screen_pick_cat(update, context)
            return
        flow = get_flow(context)
        flow["cid"] = cid
        if not cat["chats"]:
            await screen_cat_edit(update, context, cid, 0, "⚠️ 这个分类还没有群，请先勾选")
            return
        await screen_pick_script(update, context)
    elif act == "fs":
        await screen_pick_confirm(update, context, p[2])
    elif act == "fb":
        await screen_pick_script(update, context)
    elif act == "fu":
        flow = get_flow(context)
        flow["sid"] = p[2]
        await screen_timing(update, context)
    elif act == "go":
        flow = get_flow(context)
        if p[2] == "now":
            # 马上发送：不再二次确认，点了就发
            d = load_data()
            if flow["cid"] not in d["categories"] or flow["sid"] not in d["scripts"]:
                await screen_main(update, context)
                return
            context.application.create_task(
                run_now_task(context.bot, flow["cid"], flow["sid"], q.message.chat_id)
            )
            context.user_data.pop("flow", None)
            await show(update, "⏳ 已开始发送，完成后会回报结果。" + report_line(d), kb([[btn("🏠 主菜单", "bc:menu")]]))
        elif p[2] == "daily":
            d = load_data()
            script = d["scripts"].get(flow["sid"])
            if script is None:
                await screen_main(update, context)
                return
            if script.get("temp"):
                await show(
                    update,
                    "⚠️ <b>即兴文案不能做每日定时</b>\n\n每天要重复发同一条内容，所以需要先把它存成脚本。\n"
                    "请回上一步，点「➕ 新建脚本」存起来，再选每日定时。",
                    kb([[btn("⬅ 回上一步", f"bc:fc:{flow['cid']}")]]),
                )
                return
            set_await(context, "time_daily", q.message.chat_id)
            await show(
                update,
                f"🔁 <b>每日定时</b>\n\n请输入每天发送的时间（时区 {fmt_offset(d)}）\n\n"
                "格式：\n<code>20:30</code>\n<code>2030</code>\n\n"
                "设好后每天这个时间自动群发，想停就在「⏰ 定时任务」里点「🛑 取消定时」。",
                kb([[btn("❌ 取消", "bc:menu")]]),
            )
        else:
            d = load_data()
            set_await(context, "time", q.message.chat_id)
            await show(
                update,
                f"⏰ 请输入发送时间（时区 {fmt_offset(d)}）\n\n"
                "格式：\n<code>20:30</code>\n<code>10-15 20:30</code>\n<code>2026-10-15 20:30</code>",
                kb([[btn("❌ 取消", "bc:menu")]]),
            )
    elif act == "ok":
        flow = context.user_data.get("flow")
        d = load_data()
        if (not flow or flow["cid"] not in d["categories"] or flow["sid"] not in d["scripts"]):
            await screen_main(update, context)
            return
        if flow.get("daily"):
            hhmm = flow["daily"]
            jid = new_id(d, "j")
            d["jobs"][jid] = {
                "cid": flow["cid"],
                "sid": flow["sid"],
                "cat_name": d["categories"][flow["cid"]]["name"],
                "script_name": d["scripts"][flow["sid"]]["name"],
                "repeat": "daily",
                "time": hhmm,
                "tz_offset": d.get("tz_offset", 8),
                "by": q.from_user.id,
                "report_chat": q.message.chat_id,
                "status": "pending",
            }
            save_data(d)
            register_daily(context.application.job_queue, jid, hhmm, d.get("tz_offset", 8))
            context.user_data.pop("flow", None)
            await show(
                update,
                f"✅ 已设置每日定时\n任务号：<code>{jid}</code>\n每天 {hhmm}（{fmt_offset(d)}）自动群发\n\n"
                "想停止：「⏰ 定时任务」→「🛑 取消定时」",
                kb([[btn("⏰ 查看定时任务", "bc:jobs")], [btn("🏠 主菜单", "bc:menu")]]),
            )
        elif flow.get("when") is None:
            context.application.create_task(
                run_now_task(context.bot, flow["cid"], flow["sid"], q.message.chat_id)
            )
            context.user_data.pop("flow", None)
            await show(update, "⏳ 已开始发送，完成后会回报结果。" + report_line(d), kb([[btn("🏠 主菜单", "bc:menu")]]))
        else:
            run_at = datetime.fromisoformat(flow["when"])
            if run_at <= datetime.now(get_tz(d)):
                await show(update, "⚠️ 这个时间已经过了，请重新选择。", kb([[btn("⬅ 重新选", "bc:fu:" + flow["sid"])]]))
                return
            jid = new_id(d, "j")
            d["jobs"][jid] = {
                "cid": flow["cid"],
                "sid": flow["sid"],
                "cat_name": d["categories"][flow["cid"]]["name"],
                "script_name": d["scripts"][flow["sid"]]["name"],
                "run_at": flow["when"],
                "by": q.from_user.id,
                "report_chat": q.message.chat_id,
                "status": "pending",
            }
            save_data(d)
            context.application.job_queue.run_once(scheduled_cb, when=run_at, name=jid, data=jid)
            context.user_data.pop("flow", None)
            await show(
                update,
                f"✅ 已安排定时群发\n任务号：<code>{jid}</code>\n时间：{fmt_dt(run_at)}（{fmt_offset(d)}）",
                kb([[btn("⏰ 查看定时任务", "bc:jobs")], [btn("🏠 主菜单", "bc:menu")]]),
            )

    # ---- 分类 ----
    elif act == "cats":
        await screen_cats(update, context)
    elif act == "cnew":
        set_await(context, "cat_new", q.message.chat_id)
        await show(update, "请输入新分类的名称：", kb([[btn("❌ 取消", "bc:cats")]]))
    elif act == "ce":
        await screen_cat_edit(update, context, p[2], int(p[3]))
    elif act == "ct":
        cid, chat_id, page = p[2], int(p[3]), int(p[4])
        d = load_data()
        cat = d["categories"].get(cid)
        if cat:
            if chat_id in cat["chats"]:
                cat["chats"].remove(chat_id)
            else:
                cat["chats"].append(chat_id)
            save_data(d)
        await screen_cat_edit(update, context, cid, page)
    elif act == "cren":
        set_await(context, f"cat_ren:{p[2]}", q.message.chat_id)
        await show(update, "请输入新的分类名称：", kb([[btn("❌ 取消", f"bc:ce:{p[2]}:0")]]))
    elif act == "cdel":
        d = load_data()
        cat = d["categories"].get(p[2])
        if cat:
            await show(
                update,
                f"确定删除分类「{esc(cat['name'])}」吗？",
                kb([[btn("✅ 确认删除", f"bc:cdelok:{p[2]}")], [btn("❌ 取消", f"bc:ce:{p[2]}:0")]]),
            )
    elif act == "cdelok":
        d = load_data()
        d["categories"].pop(p[2], None)
        save_data(d)
        await screen_cats(update, context)

    # ---- 脚本 ----
    elif act == "scripts":
        await screen_scripts(update, context)
    elif act == "fadhoc":
        flow = get_flow(context)
        set_await(context, "adhoc", q.message.chat_id)
        back = f"bc:fc:{flow['cid']}" if flow.get("cid") else "bc:new"
        await show(
            update,
            "✍️ <b>即兴输入</b>\n\n请直接把这次要群发的内容发给我（文字 / 图片 / 视频 / 文件都可以）。\n"
            "发完不会存进脚本列表，发送完成后自动清除。\n\n"
            "⚠️ 发送前请不要删除这条消息。",
            kb([[btn("❌ 取消", back)]]),
        )
    elif act == "snew":
        set_await(context, "script_name", q.message.chat_id)
        back = "bc:fc:" + (get_flow(context)["cid"] or "") if context.user_data.get("flow") else "bc:scripts"
        await show(
            update,
            "第 1 步：请先给这个脚本起个<b>代号</b>（例如：AU报价）：",
            kb([[btn("❌ 取消", back)]]),
        )
    elif act == "se":
        await screen_script_view(update, context, p[2])
    elif act == "spv":
        d = load_data()
        s = d["scripts"].get(p[2])
        if s:
            try:
                await context.bot.copy_message(
                    chat_id=q.message.chat_id, from_chat_id=s["chat_id"], message_id=s["message_id"]
                )
            except TelegramError as e:
                await context.bot.send_message(q.message.chat_id, f"❌ 预览失败：{e}")
    elif act == "sren":
        set_await(context, f"scr_ren:{p[2]}", q.message.chat_id)
        await show(update, "请输入新的脚本名称：", kb([[btn("❌ 取消", f"bc:se:{p[2]}")]]))
    elif act == "sedit":
        set_await(context, f"scr_edit:{p[2]}", q.message.chat_id)
        await show(
            update,
            "请把<b>新的脚本内容</b>发给我（文字 / 图片 / 视频 / 文件都可以）。\n\n"
            "会替换旧内容，脚本代号不变，已安排的定时任务也会用新内容。\n"
            "⚠️ 请不要删除这条新消息。",
            kb([[btn("❌ 取消", f"bc:se:{p[2]}")]]),
        )
    elif act == "sdel":
        d = load_data()
        s = d["scripts"].get(p[2])
        if s:
            await show(
                update,
                f"确定删除脚本「{esc(s['name'])}」吗？",
                kb([[btn("✅ 确认删除", f"bc:sdelok:{p[2]}")], [btn("❌ 取消", f"bc:se:{p[2]}")]]),
            )
    elif act == "sdelok":
        d = load_data()
        d["scripts"].pop(p[2], None)
        save_data(d)
        await screen_scripts(update, context)

    # ---- 定时任务 ----
    elif act == "jobs":
        await screen_jobs(update, context)
    elif act == "jc":
        jid = p[2]
        d = load_data()
        j = d["jobs"].get(jid)
        if j and j["status"] == "pending":
            j["status"] = "canceled"
            save_data(d)
            drop_temp_script(j["sid"])
        for job in context.application.job_queue.get_jobs_by_name(jid):
            job.schedule_removal()
        await screen_jobs(update, context)


# ---------- 文字输入（私聊 + 群） ----------
async def finish_new_script(update: Update, context: ContextTypes.DEFAULT_TYPE, msg):
    """代号已在上一步输入（pending_script_name），这里收到内容后入库。"""
    name = context.user_data.pop("pending_script_name", None)
    context.user_data.pop("await", None)
    preview = describe_message(msg)[:1000]
    d = load_data()
    sid = new_id(d, "s")
    final_name = (name or short(preview, 16) or "未命名")[:30]
    d["scripts"][sid] = {
        "name": final_name,
        "preview": preview,
        "chat_id": msg.chat_id,
        "message_id": msg.message_id,
    }
    save_data(d)
    await update.effective_message.reply_text(f"✅ 脚本「{esc(final_name)}」已保存", parse_mode="HTML")
    flow = context.user_data.get("flow")
    if flow is not None and flow.get("cid"):
        flow["sid"] = sid
        await screen_timing(update, context)
    else:
        await screen_script_view(update, context, sid)


async def on_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None:
        return
    in_group = update.effective_chat.type != ChatType.PRIVATE
    if not is_admin(update.effective_user):
        if not in_group:
            await msg.reply_text("⛔ 只有管理员能使用本 Bot")
        return  # 群里非管理员的消息一律静默忽略

    mode = context.user_data.get("await")
    text = (msg.text or "").strip()
    # 输入状态属于另一个聊天时，不接收这里的消息
    if mode and context.user_data.get("await_chat") not in (None, msg.chat_id):
        mode = None

    if not mode:
        if in_group:
            return
        if text == "群发广播":
            await screen_main(update, context)
        else:
            await msg.reply_text("发 /start 或「群发广播」打开菜单")
        return

    if mode == "adhoc":
        # 即兴内容：存成临时脚本（不出现在脚本列表），发送完自动删除
        d = load_data()
        in_use = {j["sid"] for j in d["jobs"].values() if j["status"] in ("pending", "running")}
        for k in [k for k, s in d["scripts"].items() if s.get("temp") and k not in in_use]:
            del d["scripts"][k]
        sid = new_id(d, "s")
        d["scripts"][sid] = {
            "name": f"即兴 {datetime.now(get_tz(d)).strftime('%H:%M')}",
            "preview": describe_message(msg)[:1000],
            "chat_id": msg.chat_id,
            "message_id": msg.message_id,
            "temp": True,
        }
        save_data(d)
        context.user_data.pop("await", None)
        get_flow(context)["sid"] = sid
        await msg.reply_text("✅ 已收到即兴内容")
        await screen_timing(update, context)
        return

    if mode == "script_new":
        # 第 2 步：收到内容，连同第 1 步的代号一起入库
        await finish_new_script(update, context, msg)
        return

    if mode.startswith("scr_edit:"):
        sid = mode.split(":", 1)[1]
        d = load_data()
        if sid in d["scripts"]:
            d["scripts"][sid].update({
                "preview": describe_message(msg)[:1000],
                "chat_id": msg.chat_id,
                "message_id": msg.message_id,
            })
            save_data(d)
        context.user_data.pop("await", None)
        await msg.reply_text("✅ 脚本内容已更新（代号不变）")
        await screen_script_view(update, context, sid)
        return

    if not text:
        await msg.reply_text("请发送文字")
        return

    if mode == "cat_new":
        d = load_data()
        cid = new_id(d, "c")
        d["categories"][cid] = {"name": text[:30], "chats": []}
        save_data(d)
        await screen_cat_edit(update, context, cid, 0)

    elif mode.startswith("cat_ren:"):
        cid = mode.split(":", 1)[1]
        d = load_data()
        if cid in d["categories"]:
            d["categories"][cid]["name"] = text[:30]
            save_data(d)
        await screen_cat_edit(update, context, cid, 0)

    elif mode == "script_name":
        # 第 1 步：先取代号，再要内容
        context.user_data["pending_script_name"] = text[:30]
        set_await(context, "script_new", msg.chat_id)
        flow = context.user_data.get("flow")
        back = f"bc:fc:{flow['cid']}" if flow and flow.get("cid") else "bc:scripts"
        await msg.reply_text(
            f"✅ 代号：<b>{esc(text[:30])}</b>\n\n"
            "第 2 步：请直接把要群发的内容发给我（文字 / 图片 / 视频 / 文件都可以）。\n\n"
            "⚠️ 请不要删除这条消息，否则群发时会失败。相册只会保存其中一条。",
            parse_mode="HTML",
            reply_markup=kb([[btn("❌ 取消", back)]]),
        )

    elif mode.startswith("scr_ren:"):
        sid = mode.split(":", 1)[1]
        d = load_data()
        if sid in d["scripts"]:
            d["scripts"][sid]["name"] = text[:30]
            save_data(d)
        await screen_script_view(update, context, sid)

    elif mode == "time":
        d = load_data()
        tz = get_tz(d)
        dt = parse_time(text, tz)
        if dt is None:
            await msg.reply_text("格式不对，请重新输入，例如：20:30 或 10-15 20:30")
            return
        if dt <= datetime.now(tz):
            await msg.reply_text("这个时间已经过了，请重新输入")
            return
        flow = get_flow(context)
        flow["when"] = dt.isoformat()
        flow["daily"] = None
        await screen_confirm(update, context)

    elif mode == "time_daily":
        hhmm = parse_hhmm(text)
        if hhmm is None:
            await msg.reply_text("格式不对，请重新输入，例如：20:30 或 2030")
            return
        flow = get_flow(context)
        flow["daily"] = hhmm
        flow["when"] = None
        await screen_confirm(update, context)


# ---------- 命令 ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        if update.effective_chat.type == ChatType.PRIVATE:
            await update.message.reply_text("⛔ 只有管理员能使用本 Bot")
        return  # 群里非管理员静默忽略，避免刷屏
    await screen_main(update, context)


async def tz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        return
    if not context.args:
        await update.message.reply_text("用法：/tz +8   （范围 -12 到 +14，支持 5.5）")
        return
    try:
        off = float(context.args[0])
    except ValueError:
        await update.message.reply_text("格式不对，例如 /tz +8")
        return
    if not (-12 <= off <= 14):
        await update.message.reply_text("时区范围是 -12 到 +14")
        return
    d = load_data()
    d["tz_offset"] = off
    save_data(d)
    await update.message.reply_text(f"✅ 时区已设为 UTC{off:+g}（只影响之后新建的定时任务）")


async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"你的用户ID：{update.effective_user.id}")


async def setreport_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        return
    msg = update.message
    chat = update.effective_chat
    thread_id = msg.message_thread_id if msg.is_topic_message else None
    title = chat.title or "私聊"
    d = load_data()
    d["report_target"] = {"chat_id": chat.id, "thread_id": thread_id, "title": title}
    save_data(d)
    where = title + (" / 话题" if thread_id else "")
    await msg.reply_text(f"✅ 以后群发/定时报告都会发到：{where}")


async def clearreport_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        return
    d = load_data()
    d["report_target"] = None
    save_data(d)
    await update.message.reply_text("✅ 已取消固定报告位置，恢复发在操作所在的聊天")


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("处理更新时出错", exc_info=context.error)


# ---------- 启动 ----------
async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "打开群发菜单"),
        BotCommand("broadcast", "打开群发菜单"),
        BotCommand("tz", "设置时区，如 /tz +8"),
        BotCommand("myid", "查看我的用户ID"),
        BotCommand("setreport", "把报告固定发到本群/话题（管理员）"),
        BotCommand("clearreport", "取消固定报告位置"),
    ])

    # 重启后恢复定时任务
    d = load_data()
    now = datetime.now(get_tz(d))
    changed = False
    for jid, job in d["jobs"].items():
        if job.get("repeat") == "daily" and job["status"] in ("pending", "running"):
            # 每日任务：重启后无条件重新注册，明天照常发
            if job["status"] == "running":
                job["status"] = "pending"
                changed = True
                await send_report(
                    application.bot, job.get("report_chat", job["by"]),
                    f"⚠️ 每日任务 <code>{jid}</code> 执行中 Bot 重启了，今天可能只发了一部分，请检查。明天起照常发送。",
                )
            register_daily(application.job_queue, jid, job["time"], job.get("tz_offset", 8))
            logger.info("已恢复每日任务 %s 每天 %s", jid, job["time"])
        elif job["status"] == "pending":
            run_at = datetime.fromisoformat(job["run_at"])
            if run_at > now:
                application.job_queue.run_once(scheduled_cb, when=run_at, name=jid, data=jid)
                logger.info("已恢复定时任务 %s @ %s", jid, job["run_at"])
            else:
                job["status"] = "missed"
                changed = True
                await send_report(
                    application.bot, job.get("report_chat", job["by"]),
                    f"⚠️ 定时任务 <code>{jid}</code>（{esc(job['cat_name'])} → {esc(job['script_name'])}）"
                    f"在 Bot 离线期间错过了发送时间，未发送。",
                )
        elif job["status"] == "running":
            job["status"] = "interrupted"
            changed = True
            await send_report(
                application.bot, job.get("report_chat", job["by"]),
                f"⚠️ 定时任务 <code>{jid}</code> 执行中 Bot 重启了，可能只发了一部分，请检查。",
            )
    if changed:
        save_data(d)


app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

if app.job_queue is None:
    logger.critical('❌ JobQueue 不可用，请安装：pip install "python-telegram-bot[job-queue]"')
    sys.exit(1)

PRIVATE = filters.ChatType.PRIVATE
GROUPS = filters.ChatType.GROUPS
app.add_handler(CommandHandler(["start", "broadcast"], start_cmd, filters=PRIVATE | GROUPS))
app.add_handler(CommandHandler("tz", tz_cmd, filters=PRIVATE))
app.add_handler(CommandHandler("myid", myid_cmd, filters=PRIVATE))
app.add_handler(CommandHandler("register", register_cmd, filters=filters.ChatType.GROUPS))
app.add_handler(CommandHandler("setreport", setreport_cmd, filters=PRIVATE | GROUPS))
app.add_handler(CommandHandler("clearreport", clearreport_cmd, filters=PRIVATE | GROUPS))
app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^bc:"))
app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
app.add_handler(MessageHandler((PRIVATE | GROUPS) & ~filters.COMMAND, on_admin_message))
app.add_handler(MessageHandler(filters.ChatType.GROUPS, on_group_message), group=1)
app.add_error_handler(on_error)

logger.info("群发 Bot 已启动，正在监听消息...")
app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)