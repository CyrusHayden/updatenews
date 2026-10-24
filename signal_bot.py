"""
Bot đăng lại SIGNAL / NHẬN ĐỊNH.
Bạn forward (hoặc gửi) tin vào kênh/nhóm "Auto Post Assistant" -> bot nhờ Gemini đọc chữ + ảnh,
viết lại theo mẫu rồi:
  - Gemini chắc chắn  -> đăng thẳng lên channel chính (TARGET_CHAT)
  - Gemini chưa chắc  -> gửi bản nháp vào Auto Post Assistant kèm nút ✅ Y / ❌ N
Chạy 1 lượt rồi thoát (cron-job.org gọi GitHub Actions mỗi 1-2 phút). Không cần file state:
Telegram tự giữ tin chưa đọc, bản nháp nằm luôn trong Auto Post Assistant.
"""
import base64
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
TARGET_CHAT = os.environ.get("TARGET_CHAT", "").strip()                  # channel chính
ASSISTANT = os.environ.get("AUTO_POST_ASSISTANT_ID", "").strip()          # kênh/nhóm trung chuyển
INBOX_LINK = os.environ.get("INBOX_LINK", "").strip()                     # vd https://t.me/tenban
DRY_RUN = os.environ.get("DRY_RUN") == "1"

SECRETS = [s for s in (TARGET_CHAT, ASSISTANT, INBOX_LINK, BOT_TOKEN) if s]


def log(msg):
    for s in SECRETS:                       # repo public: không để lộ id/link trong log
        msg = msg.replace(s, "***")
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def fail(msg):
    for s in SECRETS:
        msg = msg.replace(s, "***")
    print(f"::error::{msg}", flush=True)
    sys.exit(1)


def load_config():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    sc = cfg.get("signal") or {}
    ai = cfg.get("ai") or {}
    sc.setdefault("models", [m for m in (ai.get("backup_model"), ai.get("primary_model")) if m])
    sc.setdefault("cta_entry", "🎁 Looking for better entries with clear TP & SL levels? DM {name}")
    sc.setdefault("cta_signal", "👑 DM {name} now to get more free VIP signals access!")
    sc["prompt"] = (ROOT / "signal_prompt.md").read_text(encoding="utf-8")
    return sc


# ---------------------------------------------------------------- Telegram
def tg(method, data=None, files=None):
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", data=data, files=files, timeout=60)
    js = r.json()
    if not js.get("ok"):
        raise RuntimeError(f"Telegram {method}: {js.get('description')}")
    return js["result"]


def get_updates():
    """Lấy tin mới rồi xác nhận NGAY (Telegram sẽ không gửi lại) -> không bao giờ đăng trùng.
    Tin nào xử lý lỗi, bot báo vào Auto Post Assistant để bạn forward lại."""
    ups = tg("getUpdates", {"timeout": 0, "limit": 100,
                            "allowed_updates": json.dumps(["message", "channel_post", "callback_query"])})
    if ups:
        tg("getUpdates", {"offset": ups[-1]["update_id"] + 1, "timeout": 0, "limit": 1})
    return ups


def download_file(file_id):
    info = tg("getFile", {"file_id": file_id})
    r = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{info['file_path']}", timeout=60)
    r.raise_for_status()
    return r.content


# ---------------------------------------------------------------- gom tin
OWN_MARKERS = ("Looking for better entries with clear TP & SL levels?", "now to get more free VIP signals access!",
               "Cần duyệt:", "Đã đăng lên channel chính", "Đã hủy, không đăng", "⚠️ Gemini lỗi", "⚠️ Lỗi khi xử lý",
               "⚠️ Không xử lý được")


def is_own(m, text):
    """Telegram thường không gửi lại tin của chính bot, đây chỉ là lớp an toàn thêm."""
    if (m.get("from") or {}).get("is_bot") and not m.get("forward_origin"):
        return True
    return not m.get("forward_origin") and any(k in text for k in OWN_MARKERS)


def group_posts(updates):
    """Gom các update thành từng bài (album nhiều ảnh = 1 bài). Chỉ nhận tin từ Auto Post Assistant."""
    posts, by_album = [], {}
    callbacks = []
    for u in updates:
        if "callback_query" in u:
            callbacks.append(u["callback_query"])
            continue
        m = u.get("message") or u.get("channel_post")
        if not m or str(m["chat"]["id"]) != ASSISTANT:
            continue
        text = m.get("text") or m.get("caption") or ""
        if is_own(m, text):
            continue                                    # tin do chính bot tạo ra (nháp, thông báo)
        photo = None
        if m.get("photo"):
            photo = m["photo"][-1]["file_id"]           # bản lớn nhất
        elif (m.get("document") or {}).get("mime_type", "").startswith("image/"):
            photo = m["document"]["file_id"]
        if not text.strip() and not photo:
            continue                                    # sticker, video không chữ...
        gid = m.get("media_group_id")
        if gid and gid in by_album:
            p = by_album[gid]
            if photo:
                p["photos"].append(photo)
            if text and not p["text"]:
                p["text"] = text
            continue
        p = {"msg_id": m["message_id"], "text": text, "photos": [photo] if photo else []}
        posts.append(p)
        if gid:
            by_album[gid] = p
    return posts, callbacks


# ---------------------------------------------------------------- Gemini
def call_gemini(model, prompt, images):
    parts = [{"text": prompt}]
    for b in images:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(b).decode()}})
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"}}
    r = requests.post(url, params={"key": GEMINI_KEY}, json=body, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"{model} HTTP {r.status_code}")
    txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip()).strip()
    out = json.loads(txt)
    if isinstance(out, list):
        out = out[0]
    return out


def analyze(post, images, cfg):
    prompt = cfg["prompt"] + "\n\n## POST TEXT\n" + (post["text"].strip()[:4000] or "(no text, chart only)")
    for model in cfg["models"]:
        for attempt in range(2):
            try:
                return call_gemini(model, prompt, images)
            except Exception as e:
                log(f"AI lỗi ({e}); thử lại...")
                time.sleep(5 * (attempt + 1))
    return None


# ---------------------------------------------------------------- dựng bài
URL_RE = re.compile(r"(https?://\S+|www\.\S+|t\.me/\S+|telegram\.me/\S+)", re.I)
MENTION_RE = re.compile(r"(?<![\w.])@[A-Za-z0-9_]{3,}")


def clean_body(text):
    t = URL_RE.sub("", text or "")
    t = MENTION_RE.sub("", t)
    t = re.sub(r"[ \t]+([.,!?;:])", r"\1", t)     # dấu câu bị dư sau khi xóa link
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def md_to_html(text):
    t = html.escape(text, quote=False)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t, flags=re.S)
    return t.replace("**", "")


def norm_ticker(t):
    t = re.sub(r"[^A-Za-z0-9.]", "", str(t)).upper().strip(".")
    t = re.sub(r"(USDT|USD|PERP)(\.P)?$", "", t) if len(t) > 5 and not t.endswith(".D") else t
    t = t.removesuffix(".P")
    return {"XAU": "GOLD", "XAG": "SILVER"}.get(t, t)


def inbox_name(link):
    m = re.search(r"(?:t\.me|telegram\.me)/([A-Za-z0-9_]+)", link)
    return m.group(1) if m else link.lstrip("@")


def build_post(res, cfg):
    tickers = []
    for t in res.get("tickers") or []:
        t = norm_ticker(t)
        if t and t not in tickers:
            tickers.append(t)
    title = ("[" + " ".join("$" + t for t in tickers[:3]) + "] UPDATE") if tickers else "MARKET UPDATE"
    name = f'<b><a href="{html.escape(INBOX_LINK)}">{html.escape(inbox_name(INBOX_LINK))}</a></b>'
    cta = cfg["cta_signal"] if res.get("is_signal") else cfg["cta_entry"]
    cta_html = html.escape(cta, quote=False).replace("{name}", name)
    body = md_to_html(clean_body(res.get("body") or ""))
    return f"<b>{html.escape(title)}</b>\n\n{body}\n\n<i>{cta_html}</i>"


def visible_len(h):
    return len(html.unescape(re.sub(r"<[^>]+>", "", h)))


def send_post(chat, caption, photos):
    """Đăng 1 bài, trả về list message_id đã tạo."""
    if DRY_RUN:
        print(f"\n----- DRY RUN -> {'MAIN' if chat == TARGET_CHAT else 'ASSISTANT'} -----\n{caption}\n[ảnh: {len(photos)}]\n")
        return [0]
    base = {"chat_id": chat}
    text_msg = {"chat_id": chat, "text": caption, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    long_cap = visible_len(caption) > 1024
    if not photos:
        return [tg("sendMessage", text_msg)["message_id"]]
    if len(photos) == 1:
        if long_cap:
            ids = [tg("sendPhoto", dict(base, photo=photos[0]))["message_id"]]
            return ids + [tg("sendMessage", text_msg)["message_id"]]
        return [tg("sendPhoto", dict(base, photo=photos[0], caption=caption, parse_mode="HTML"))["message_id"]]
    media = [{"type": "photo", "media": f} for f in photos[:10]]
    if not long_cap:
        media[0].update(caption=caption, parse_mode="HTML")
    ids = [m["message_id"] for m in tg("sendMediaGroup", dict(base, media=json.dumps(media)))]
    if long_cap:
        ids.append(tg("sendMessage", text_msg)["message_id"])
    return ids


def notify(text, reply_to=None, buttons=None):
    if DRY_RUN:
        print(f"[ASSISTANT] {text} {buttons or ''}")
        return
    data = {"chat_id": ASSISTANT, "text": text, "parse_mode": "HTML"}
    if reply_to:
        data["reply_parameters"] = json.dumps({"message_id": reply_to, "allow_sending_without_reply": True})
    if buttons:
        data["reply_markup"] = json.dumps({"inline_keyboard": [buttons]})
    tg("sendMessage", data)


# ---------------------------------------------------------------- xử lý
def handle_post(post, cfg):
    images = []
    for f in post["photos"][:4]:
        try:
            images.append(download_file(f))
        except Exception as e:
            log(f"Không tải được ảnh ({e})")
    res = analyze(post, images, cfg)
    if not res:
        notify("⚠️ Gemini lỗi, chưa xử lý được tin này. Bạn forward lại sau ít phút nhé.", post["msg_id"])
        return "ai_error"
    caption = build_post(res, cfg)
    if res.get("confident") is True:
        send_post(TARGET_CHAT, caption, post["photos"])
        return "posted"
    # chưa chắc -> nháp + nút Y/N
    ids = send_post(ASSISTANT, caption, post["photos"])
    first, n = ids[0], len(ids)
    doubt = html.escape(str(res.get("doubt") or "Gemini chưa chắc chắn"), quote=False)
    notify(f"🤔 <b>Cần duyệt:</b> {doubt}\nĐăng bài trên lên channel chính?", first,
           [{"text": "✅ Y", "callback_data": f"y:{first}:{n}"},
            {"text": "❌ N", "callback_data": f"n:{first}:{n}"}])
    return "draft"


def handle_callback(cb):
    msg = cb.get("message") or {}
    if str((msg.get("chat") or {}).get("id")) != ASSISTANT:
        return
    m = re.fullmatch(r"([yn]):(\d+):(\d+)", cb.get("data") or "")
    if not m:
        return
    action, first, n = m.group(1), int(m.group(2)), int(m.group(3))
    try:
        tg("answerCallbackQuery", {"callback_query_id": cb["id"]})
    except Exception:
        pass                                   # nút bấm quá lâu trước đó -> bỏ qua thông báo nhỏ
    if action == "y":
        tg("copyMessages", {"chat_id": TARGET_CHAT, "from_chat_id": ASSISTANT,
                            "message_ids": json.dumps(list(range(first, first + n)))})
        new_text = "✅ Đã đăng lên channel chính."
    else:
        new_text = "❌ Đã hủy, không đăng."
    tg("editMessageText", {"chat_id": ASSISTANT, "message_id": msg["message_id"], "text": new_text})
    log(f"Nút {action.upper()} đã xử lý")


def main():
    cfg = load_config()
    updates = get_updates()
    posts, callbacks = group_posts(updates)
    log(f"{len(updates)} update: {len(posts)} bài mới, {len(callbacks)} lần bấm nút")
    for cb in callbacks:
        try:
            handle_callback(cb)
        except Exception as e:
            log(f"Nút lỗi: {e}")
            try:
                notify(f"⚠️ Không xử lý được nút bấm: {html.escape(str(e)[:200])}")
            except Exception:
                pass
    stats = {}
    for p in posts:
        try:
            r = handle_post(p, cfg)
        except Exception as e:
            r = "error"
            log(f"Bài lỗi: {e}")
            try:
                notify(f"⚠️ Lỗi khi xử lý tin này: {html.escape(str(e)[:200])}\nBạn forward lại nhé.", p["msg_id"])
            except Exception:
                pass
        stats[r] = stats.get(r, 0) + 1
        time.sleep(2)
    log(f"Kết quả: {stats or 'không có bài'}")


if __name__ == "__main__":
    need = [("TELEGRAM_BOT_TOKEN", BOT_TOKEN), ("GEMINI_API_KEY", GEMINI_KEY), ("TARGET_CHAT", TARGET_CHAT),
            ("AUTO_POST_ASSISTANT_ID", ASSISTANT), ("INBOX_LINK", INBOX_LINK)]
    missing = [n for n, v in need if not v]
    if missing:
        fail("Thiếu Secret: " + ", ".join(missing) + " (Settings > Secrets and variables > Actions).")
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        msg = f"{type(e).__name__}: {str(e)[:300]}"
        if "webhook" in msg.lower():
            msg += " -> bot đang bật webhook, cần tắt (deleteWebhook) mới dùng được."
        elif "terminated by other getUpdates" in msg or "Conflict" in msg:
            msg += " -> có chương trình khác đang dùng CÙNG bot token để đọc tin. Dùng bot khác cho 1 trong 2."
        fail("Bot lỗi: " + msg)   # không in traceback: có thể lộ token trong log public
