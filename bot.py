"""
Bot tổng hợp tin Telegram -> channel của mình.
Đọc channel public qua trang web t.me/s/<channel> (không cần tài khoản),
lọc từ khóa, chống trùng, nhờ Gemini viết lại tiếng Anh, đăng bằng bot @BotFather.
Chạy 1 lượt rồi thoát (GitHub Actions gọi mỗi ~15 phút).
"""
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import base64

import requests
import yaml
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.enc"   # file trạng thái đã MÃ HÓA
UA = {"User-Agent": "Mozilla/5.0 (compatible; NewsDigestBot/1.0; +https://github.com)"}
WIKI_UA = {"User-Agent": "UpdateNewsBot/1.0 (https://github.com/CyrusHayden/updatenews) python-requests/2"}

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"   # =1: không đăng thật, chỉ in ra (dùng để thử)
STATE_KEY = os.environ.get("STATE_KEY", "")
SOURCE_CHANNELS = os.environ.get("SOURCE_CHANNELS", "")   # Secret: mỗi dòng 1 channel
TARGET_CHAT = os.environ.get("TARGET_CHAT", "")           # Secret: channel đích


def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def fail(msg):
    """Báo lỗi nổi bật trên trang Actions (dòng đỏ ở phần Annotations) rồi dừng."""
    print(f"::error::{msg}", flush=True)
    sys.exit(1)


# ---------------------------------------------------------------- config/state
def load_config():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg["prompt"] = (ROOT / "prompt.md").read_text(encoding="utf-8")
    if SOURCE_CHANNELS.strip():
        cfg["sources"] = [x for x in re.split(r"[\s,]+", SOURCE_CHANNELS) if x]
    if TARGET_CHAT.strip():
        cfg["target_chat"] = TARGET_CHAT.strip()
    cfg["sources"] = cfg.get("sources") or []
    return cfg


def _fernet():
    key = base64.urlsafe_b64encode(hashlib.sha256(STATE_KEY.encode()).digest())
    return Fernet(key)


def load_state():
    if STATE_FILE.exists() and STATE_FILE.stat().st_size > 0:
        try:
            raw = _fernet().decrypt(STATE_FILE.read_bytes())
        except Exception:
            print("::warning::STATE_KEY không khớp với file state.enc -> chạy lại từ đầu (đặt mốc mới).")
            return {"last_ids": {}, "recent": []}
        return json.loads(raw.decode("utf-8"))
    return {"last_ids": {}, "recent": []}


def save_state(state):
    STATE_FILE.write_bytes(_fernet().encrypt(json.dumps(state, ensure_ascii=False).encode("utf-8")))


def norm_hash(text):
    t = re.sub(r"https?://\S+|@\w+", "", text.lower())
    t = re.sub(r"[^\w]+", " ", t).strip()
    return hashlib.sha1(t[:400].encode()).hexdigest()[:16]


# ---------------------------------------------------------------- 1. đọc tin
def _bg_url(style):
    m = re.search(r"background-image:url\('([^']+)'\)", style or "")
    return m.group(1) if m else None


def _text_of(el):
    if el is None:
        return ""
    for br in el.find_all("br"):
        br.replace_with("\n")
    return el.get_text().strip()


def fetch_channel(channel, after_id):
    """Trả về list tin có id > after_id (cũ -> mới). Đọc tối đa 3 trang."""
    posts, before = {}, None
    for _ in range(3):
        url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        msgs = soup.select("div.tgme_widget_message[data-post]")
        if not msgs:
            break
        ids = []
        for m in msgs:
            pid = int(m["data-post"].split("/")[-1])
            ids.append(pid)
            if pid <= after_id:
                continue
            text = _text_of(m.select_one("div.tgme_widget_message_text"))
            images = [u for u in (_bg_url(a.get("style")) for a in m.select("a.tgme_widget_message_photo_wrap")) if u]
            if not images:  # video -> lấy ảnh bìa
                images = [u for u in (_bg_url(i.get("style")) for i in m.select("i.tgme_widget_message_video_thumb")) if u]
            t = m.select_one("time[datetime]")
            posts[pid] = {
                "channel": channel, "id": pid, "text": text, "images": images[:10],
                "time": t["datetime"] if t else "",
            }
        if min(ids) <= after_id or after_id == 0:
            break
        before = min(ids)
    return [posts[k] for k in sorted(posts)]


def latest_id(channel):
    r = requests.get(f"https://t.me/s/{channel}", headers=UA, timeout=30)
    r.raise_for_status()
    ids = [int(x.split("/")[-1]) for x in re.findall(r'data-post="([^"]+)"', r.text)]
    return max(ids) if ids else 0


# ---------------------------------------------------------------- 2. lọc
def keyword_ok(text, kw):
    t = text.lower()
    inc = [k.lower() for k in (kw.get("include") or [])]
    exc = [k.lower() for k in (kw.get("exclude") or [])]
    if any(k in t for k in exc):
        return False
    return not inc or any(k in t for k in inc)


# ---------------------------------------------------------------- 3. AI
def call_gemini(model, prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json"},
    }
    r = requests.post(url, params={"key": GEMINI_KEY}, json=body, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"{model} HTTP {r.status_code}")
    data = r.json()
    txt = data["candidates"][0]["content"]["parts"][0]["text"]
    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip()).strip()
    return json.loads(txt)


def ai_process(batch, recent, cfg):
    ai = cfg["ai"]
    summaries = "\n".join(f"- {r['summary']}" for r in recent) or "(none)"
    items = [{"id": f"{p['channel']}/{p['id']}", "text": p["text"][:4000],
              "has_image": bool(p["images"])} for p in batch]
    prompt = (cfg["prompt"] + "\n\n## RECENT_SUMMARIES (already published)\n" + summaries
              + "\n\n## NEW_POSTS\n" + json.dumps(items, ensure_ascii=False))
    for model in (ai["primary_model"], ai["backup_model"]):
        for attempt in range(2):
            try:
                out = call_gemini(model, prompt)
                return {o["id"]: o for o in out if isinstance(o, dict) and "id" in o}
            except Exception as e:
                log(f"AI lỗi ({e}); thử lại...")
                time.sleep(15 * (attempt + 1))
    return None


def chunks(posts, max_chars):
    cur, size = [], 0
    for p in posts:
        n = len(p["text"]) + 200
        if cur and size + n > max_chars:
            yield cur
            cur, size = [], 0
        cur.append(p)
        size += n
    if cur:
        yield cur


# ---------------------------------------------------------------- 4. ảnh
PERSON_RE = re.compile(
    r"\b(politician|businessman|businesswoman|business magnate|entrepreneur|billionaire|investor|philanthropist|"
    r"actor|actress|singer|rapper|footballer|journalist|economist|lawyer|president|prime minister|"
    r"executive|chief executive|founder|born \d{4})\b|\(born |\b\d{4}\s*[–-]\s*\d{4}\b", re.I)


def wiki_image(query, kind=""):
    """Ảnh đại diện của bài Wikipedia. Thử đúng tên bài trước, không có thì tìm kiếm (5 kết quả đầu).
    Bỏ qua trang phân nhánh; nếu kind = company/org/place/other thì bỏ qua trang NGƯỜI
    (vd 'Bloomberg' -> Michael Bloomberg). Không có ảnh chắc chắn đúng -> trả None (bỏ ảnh đó)."""
    kind = (kind or "").lower()
    base = {"action": "query", "format": "json", "prop": "pageimages|description",
            "piprop": "thumbnail", "pithumbsize": 1000, "redirects": 1}
    tries = [dict(base, titles=query),
             dict(base, generator="search", gsrsearch=query, gsrlimit=5)]
    for params in tries:
        try:
            r = requests.get("https://en.wikipedia.org/w/api.php", headers=WIKI_UA, timeout=20, params=params)
            pages = sorted(r.json().get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 0))
            for p in pages:
                desc = p.get("description") or ""
                if not p.get("thumbnail") or "disambiguation" in desc.lower() or "referred to by the same term" in desc.lower():
                    continue
                if kind in ("company", "org", "organization", "place", "country", "other") and PERSON_RE.search(desc):
                    continue
                return p["thumbnail"]["source"]
        except Exception:
            pass
    return None


def coingecko_image(coin):
    try:
        r = requests.get("https://api.coingecko.com/api/v3/search", params={"query": coin}, timeout=20)
        coins = r.json().get("coins", [])
        if coins:
            return coins[0].get("large")
    except Exception:
        pass
    return None


def find_images(res, cfg, want=2):
    """Tìm tối đa `want` ảnh KHÁC NHAU cho tin chỉ có chữ: logo coin (CoinGecko) + ảnh Wikipedia."""
    urls = []
    if res.get("coin"):
        u = coingecko_image(res["coin"])
        if u:
            urls.append(u)
    queries = res.get("image_queries") or ([res["image_query"]] if res.get("image_query") else [])
    kinds = res.get("image_kinds") or []
    for i, q in enumerate(queries):
        if len(urls) >= want:
            break
        u = wiki_image(str(q), str(kinds[i]) if i < len(kinds) else "")
        if u and u not in urls:
            urls.append(u)
    images = [b for b in (load_image_bytes(u) for u in urls) if b][:want]
    if not images:
        b = load_image_bytes(cfg.get("default_image") or "")
        images = [b] if b else []
    return images


def load_image_bytes(src):
    if not src:
        return None
    if not src.startswith("http"):
        p = ROOT / src
        return p.read_bytes() if p.exists() else None
    try:
        r = requests.get(src, headers=WIKI_UA, timeout=30)
        if r.status_code == 200 and len(r.content) > 1000:
            return r.content
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- 5. đăng
def to_html(text):
    t = html.escape(text, quote=False)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t, flags=re.S)


def build_caption(post, cfg):
    parts = [cfg.get("header") or "", post.strip(), cfg.get("footer") or ""]
    return "\n\n".join(p for p in parts if p)


def tg(method, data=None, files=None):
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", data=data, files=files, timeout=60)
    js = r.json()
    if not js.get("ok"):
        raise RuntimeError(f"Telegram {method}: {js.get('description')}")
    return js


def publish(caption, images, cfg):
    chat = cfg["target_chat"]
    cap = to_html(caption)
    if DRY_RUN:
        print("\n----- DRY RUN -----\n" + caption + f"\n[ảnh: {len(images)}]\n")
        return
    if len(cap) > 1000 and len(images) > 1:   # caption quá dài: gửi album ảnh trước, chữ sau
        tg("sendMediaGroup", {"chat_id": chat, "media": json.dumps(
            [{"type": "photo", "media": f"attach://p{i}"} for i in range(len(images))])},
           {f"p{i}": (f"p{i}.jpg", b) for i, b in enumerate(images)})
        tg("sendMessage", {"chat_id": chat, "text": cap, "parse_mode": "HTML",
                           "disable_web_page_preview": "true"})
        return
    if len(cap) > 1000 or not images:          # caption ảnh tối đa 1024 ký tự
        if images:
            tg("sendPhoto", {"chat_id": chat}, {"photo": ("img.jpg", images[0])})
        tg("sendMessage", {"chat_id": chat, "text": cap, "parse_mode": "HTML",
                           "disable_web_page_preview": "true"})
        return
    if len(images) == 1:
        tg("sendPhoto", {"chat_id": chat, "caption": cap, "parse_mode": "HTML"},
           {"photo": ("img.jpg", images[0])})
        return
    media, files = [], {}
    for i, b in enumerate(images):
        item = {"type": "photo", "media": f"attach://p{i}"}
        if i == 0:
            item.update(caption=cap, parse_mode="HTML")
        media.append(item)
        files[f"p{i}"] = (f"p{i}.jpg", b)
    tg("sendMediaGroup", {"chat_id": chat, "media": json.dumps(media)}, files)


# ---------------------------------------------------------------- main
def main():
    cfg = load_config()
    if not cfg["sources"] or not cfg.get("target_chat"):
        fail("Thiếu Secret SOURCE_CHANNELS hoặc TARGET_CHAT (Settings > Secrets and variables > Actions).")
    state = load_state()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=cfg.get("dedupe_hours", 24))
    state["recent"] = [r for r in state["recent"] if datetime.fromisoformat(r["t"]) > cutoff]
    recent_hashes = {r["h"] for r in state["recent"]}

    # 1. đọc
    new_posts = []
    for n, ch in enumerate(cfg["sources"], 1):
        ch = str(ch).strip().replace("https://t.me/", "").replace("t.me/", "").lstrip("@")
        tag = f"channel #{n}"   # log KHÔNG ghi tên channel (repo public)
        last = state["last_ids"].get(ch, 0)
        try:
            if last == 0:   # lần đầu: chỉ lấy N tin mới nhất để đăng luôn, không đăng tràn tin cũ
                n_first = int(cfg.get("first_run_posts", 3))
                top = latest_id(ch)
                posts = fetch_channel(ch, max(top - 50, 1) - 1)[-n_first:] if n_first > 0 else []
                state["last_ids"][ch] = top
                log(f"{tag}: lần đầu, lấy {len(posts)} tin mới nhất")
                new_posts += posts
                continue
            posts = fetch_channel(ch, last)
            log(f"{tag}: {len(posts)} tin mới")
            new_posts += posts
        except Exception as e:
            print(f"::warning::{tag}: không đọc được ({type(e).__name__}) - kiểm tra tên channel / t.me/s có xem được không", flush=True)

    # 2. lọc từ khóa + trùng y hệt
    cand = []
    for p in new_posts:
        if not p["text"] or not keyword_ok(p["text"], cfg.get("keywords") or {}):
            continue
        h = norm_hash(p["text"])
        if h in recent_hashes:
            continue
        recent_hashes.add(h)
        p["hash"] = h
        cand.append(p)
    log(f"Qua bộ lọc từ khóa: {len(cand)}/{len(new_posts)}")

    # 3. AI
    results, failed = {}, []
    for i, batch in enumerate(chunks(cand, cfg["ai"]["max_chars_per_call"])):
        if i:
            time.sleep(cfg["ai"]["seconds_between_calls"])
        out = ai_process(batch, state["recent"], cfg)
        if out is None:
            failed += batch
            continue
        results.update(out)
    log(f"AI xử lý xong: {len(results)} tin, lỗi: {len(failed)}")

    # 4+5. ảnh + đăng (theo thứ tự thời gian)
    posted = 0
    for p in sorted(cand, key=lambda x: x["time"]):
        res = results.get(f"{p['channel']}/{p['id']}")
        if not res or res.get("skip") or res.get("duplicate") or not res.get("post"):
            continue
        if posted >= cfg.get("max_posts_per_run", 30):
            break
        imgs = [b for b in (load_image_bytes(u) for u in p["images"]) if b]
        if not imgs:
            imgs = find_images(res, cfg, int(cfg.get("auto_images", 2)))
        try:
            publish(build_caption(res["post"], cfg), imgs, cfg)
            posted += 1
            state["recent"].append({"t": now.isoformat(), "h": p["hash"],
                                    "summary": (res.get("summary") or "")[:200]})
            time.sleep(cfg.get("seconds_between_posts", 3))
        except Exception as e:
            log(f"Đăng lỗi: {str(e).replace(cfg['target_chat'], '***')}")

    # 6. cập nhật mốc: tin AI lỗi sẽ được thử lại lượt sau
    for ch in {p["channel"] for p in new_posts}:
        ch_failed = [p["id"] for p in failed if p["channel"] == ch]
        ch_all = [p["id"] for p in new_posts if p["channel"] == ch]
        state["last_ids"][ch] = (min(ch_failed) - 1) if ch_failed else max(ch_all + [state["last_ids"].get(ch, 0)])
    save_state(state)
    log(f"Đã đăng {posted} bài.")


if __name__ == "__main__":
    missing = [n for n, v in [("TELEGRAM_BOT_TOKEN", BOT_TOKEN), ("GEMINI_API_KEY", GEMINI_KEY),
                              ("STATE_KEY", STATE_KEY)] if not v]
    if DRY_RUN:
        missing = [m for m in missing if m != "TELEGRAM_BOT_TOKEN"]
    if missing:
        fail("Thiếu Secret: " + ", ".join(missing) + " (Settings > Secrets and variables > Actions).")
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        msg = f"{type(e).__name__}: {e}"
        if TARGET_CHAT:
            msg = msg.replace(TARGET_CHAT, "***")
        fail("Bot lỗi: " + msg[:300])
