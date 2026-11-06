"""Hàng chờ đăng (outbox): đăng các bài Claude đẩy vào thư mục outbox/ lên channel chính.

Mỗi bài = 1 file chữ <ten>.txt (+ tùy chọn ảnh cùng tên <ten>.png/.jpg).
- Có ảnh: gửi ảnh kèm chữ làm caption (chữ > 1024 ký tự thì gửi ảnh trước, chữ sau).
- Không ảnh: gửi chữ.
Đăng xong thì xóa file khỏi outbox/ (bước Save state commit việc xóa) để không đăng trùng.
Lỗi thì đổi tên file chữ thành <ten>.failed (không thử lại, tránh báo lỗi liên tục) và báo vào Auto Post Assistant.
Muốn đăng lại: đổi .failed về .txt.
"""
import os, sys, glob, requests

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TARGET = os.environ.get("TARGET_CHAT", "")
ASSISTANT = os.environ.get("AUTO_POST_ASSISTANT_ID", "")
OUTBOX = "outbox"
CAPTION_MAX = 1024


def tg(method, data=None, files=None):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", data=data, files=files, timeout=60)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"{method}: {j.get('description', r.status_code)}")
    return j["result"]


def safe(e):
    """Che token trong thông báo lỗi (repo public, log ai cũng xem được)."""
    msg = str(e)
    return msg.replace(TOKEN, "***") if TOKEN else msg


def notify(text):
    if ASSISTANT:
        try:
            tg("sendMessage", {"chat_id": ASSISTANT, "text": text, "disable_notification": True})
        except Exception as e:
            print("Không gửi được thông báo vào Auto Post Assistant:", safe(e))


def find_image(stem):
    for ext in (".png", ".jpg", ".jpeg"):
        if os.path.exists(stem + ext):
            return stem + ext
    return None


def post(txt_path):
    stem = txt_path[:-4]
    text = open(txt_path, encoding="utf-8").read().strip()
    img = find_image(stem)
    if img:
        with open(img, "rb") as f:
            if len(text) <= CAPTION_MAX:
                tg("sendPhoto", {"chat_id": TARGET, "caption": text}, {"photo": f})
            else:
                tg("sendPhoto", {"chat_id": TARGET}, {"photo": f})
                tg("sendMessage", {"chat_id": TARGET, "text": text})
    else:
        tg("sendMessage", {"chat_id": TARGET, "text": text})
    for p in (txt_path, img):
        if p:
            os.remove(p)


def main():
    items = sorted(glob.glob(os.path.join(OUTBOX, "*.txt")))
    if not items:
        print("Outbox trống.")
        return
    if not TOKEN or not TARGET:
        print("Thiếu Secret TELEGRAM_BOT_TOKEN / TARGET_CHAT")
        sys.exit(1)
    for p in items:
        name = os.path.basename(p)[:-4]
        try:
            post(p)
            print("Đã đăng outbox:", name)
            notify(f"✅ Posted from outbox: {name}")
        except Exception as e:
            err = safe(e)
            print("Lỗi đăng outbox", name, "-", err)
            os.rename(p, p[:-4] + ".failed")
            notify(f"⚠️ Outbox post failed: {name} ({err}). Not retried; ask Claude to check.")


if __name__ == "__main__":
    main()
