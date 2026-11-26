"""Chia video raw cho nhiều tài khoản Gemini (web) — mỗi tài khoản có sẵn 3 cuộc trò chuyện (3 tab).

- Mỗi tài khoản Gemini một profile Chrome riêng: data/profiles/gem_<id> (đăng nhập 1 lần).
- Tài khoản lưu danh sách link cuộc trò chuyện (tab). Mỗi lần bấm "Nạp video": MỖI TAB nhận 1 video
  (đính kèm + câu lệnh nếu có -> gửi). Không tải gì về — người dùng tự vào kiểm tra.
- Nhận diện video trùng theo nội dung file (dấu vân tay), không nạp 1 video vào 2 tab.
- Tài khoản hết lượt -> tạm nghỉ (tự chạy lại sau N giờ, hoặc bấm "Tiếp tục"); video còn lại có thể "Chia lại".
"""
import hashlib
import json
import logging
import re
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from . import db
from .browser import launch, profile_lock
from .config import SHOTS_DIR, VIDEO_EXTS
from .platforms.base import UploadError, try_click, type_text, wait_enabled

log = logging.getLogger("gemini")
_stop = threading.Event()
_login_threads: dict[int, threading.Thread] = {}
_watching: dict[int, dict] = {}   # tài khoản -> {tab: tên video} Gemini đang làm (chế độ tự nạp tiếp)
_pick_lock = threading.Lock()     # 2 tài khoản không lấy trùng 1 video
_fp_cache: dict = {}
_idle: set[int] = set()   # tài khoản đã nạp xong, Chrome đang để mở cho người dùng xem (không tính vào số chạy cùng lúc)
GEM_HOME = "https://gemini.google.com/app"

SCHEMA = """
CREATE TABLE IF NOT EXISTS gem_accounts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    tabs       TEXT NOT NULL DEFAULT '[]',   -- JSON: link các cuộc trò chuyện
    status     TEXT NOT NULL DEFAULT 'ok',   -- ok | limit | login | off
    note       TEXT,
    resume_at  TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gem_tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER REFERENCES gem_accounts(id) ON DELETE SET NULL,
    tab_idx     INTEGER,
    batch       TEXT NOT NULL DEFAULT '',
    video_path  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending | running | done | failed
    chat_url    TEXT,
    reply       TEXT,
    out_file    TEXT,
    error       TEXT,
    screenshot  TEXT,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_gem_tasks ON gem_tasks(status, account_id);
"""


def init():
    with db.conn() as c:
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(gem_tasks)")}
        for col, ddl in (("name", "TEXT NOT NULL DEFAULT ''"), ("size", "INTEGER NOT NULL DEFAULT 0"),
                         ("hidden", "INTEGER NOT NULL DEFAULT 0"), ("fhash", "TEXT NOT NULL DEFAULT ''")):   # hidden = đã ẩn khỏi danh sách nhưng vẫn nhớ là đã gửi
            if col not in cols:
                c.execute(f"ALTER TABLE gem_tasks ADD COLUMN {col} {ddl}")
        acols = {r["name"] for r in c.execute("PRAGMA table_info(gem_accounts)")}
        if "refresh" not in acols:   # 1 = người dùng bấm "Làm mới tab" -> mở phiên trò chuyện mới
            c.execute("ALTER TABLE gem_accounts ADD COLUMN refresh INTEGER NOT NULL DEFAULT 0")
        c.execute("UPDATE gem_tasks SET status='pending', started_at=NULL WHERE status='running'")


def pkey(aid) -> str:
    return f"gem_{aid}"


def setting(k, default=""):
    return db.get_setting("gem_" + k, default)


def tabs_of(acc) -> list[str]:
    try:
        return [u for u in json.loads(acc.get("tabs") or "[]") if u]
    except ValueError:
        return []


CHAT_RX = re.compile(r"^https://gemini\.google\.com/(u/\d+/)?(app|gem)/[\w-]+")


def clean_tab(u: str) -> str:
    u = u.strip()
    m = CHAT_RX.match(u)
    return m.group(0) if m else ""


# ---------------------------------------------------------------- Gemini web
class GemLimit(UploadError):
    pass


INPUT = 'rich-textarea div[contenteditable="true"], div.ql-editor[contenteditable="true"], div[contenteditable="true"][role="textbox"]'
SEND = 'button[aria-label*="Gửi" i], button[aria-label*="Send" i], button.send-button'
STOP = 'button[aria-label*="Dừng" i], button[aria-label*="Ngừng" i], button[aria-label*="Stop" i], button.stop'
HAS_VIDEO = ('video, video-player, generated-file, '
             'button[aria-label*="Download video" i], button[aria-label*="Tải video" i], '
             'a[download*=".mp4" i]')
RESP_BUSY = ('.thinking-header:not(.done), [data-test-id="status-footer"]:not(.status-done), '
             '[aria-busy="true"], mat-spinner, mat-progress-spinner')
RESP_DONE = 'thumb-up-button button, copy-button button'
UPLOAD_MENU = ('simplified-input-menu button, uploader button, '
               'button[aria-label*="tải tệp" i], button[aria-label*="tải lên" i], button[aria-label*="upload" i], '
               'button[aria-label*="thêm tệp" i], button[aria-label*="add file" i], '
               'button[aria-label*="Mở trình đơn" i], button[aria-label*="open upload" i]')
UPLOAD_ITEM_TXT = re.compile(r"(Tải tệp lên|Tải lên tệp|Upload files?)", re.I)
AGREE = re.compile(r"^\s*(Đồng ý|Tôi đồng ý|Agree|I agree|Đã hiểu|Got it)\s*$", re.I)
RESP = "model-response"
LIMIT_RX = re.compile(
    r"đạt (đến )?giới hạn|hết lượt|vượt quá giới hạn|giới hạn (sử dụng|tải lên)|reached (your|the)( daily)? limit|"
    r"usage limit|limit for|quota|thử lại (vào )?sau|try again later|quá nhiều (yêu cầu|tệp)", re.I)


def _agree(page):
    for _ in range(2):
        if not try_click(page.get_by_role("button", name=AGREE), 1200):
            break
        page.wait_for_timeout(800)


def _upload_button(page):
    """Nút ➕ đính kèm NẰM Ở Ô NHẬP TIN NHẮN. Bỏ qua các nút trong lịch sử trò chuyện
    (video đã gửi trước đó cũng có nhãn chứa chữ 'upload' -> bấm nhầm sẽ mở trình xem video)."""
    box = page.locator(INPUT).first.bounding_box()
    best, best_key = None, None
    for el in page.locator(UPLOAD_MENU).all():
        try:
            if not el.is_visible() or el.evaluate(
                    "e => !!e.closest('user-query, model-response, .conversation-container, message-content, mat-sidenav, conversations-list')"):
                continue
            in_input = el.evaluate(
                "e => !!e.closest('input-container, .text-input-field, simplified-input-menu, uploader, rich-textarea')")
            b = el.bounding_box()
        except Exception:
            continue
        if not b:
            continue
        d = abs((b["y"] + b["height"] / 2) - (box["y"] + box["height"] / 2)) if box else 0
        key = (not in_input, d)
        if best is None or key < best_key:
            best, best_key = el, key
    if best is None or (box and best_key[1] > 250):
        raise UploadError("Không thấy nút đính kèm (➕) ở ô nhập tin nhắn của Gemini")
    return best


def _close_overlays(page):
    """Đóng trình xem video / hộp thoại đang che (nếu lỡ mở)."""
    for _ in range(3):
        ov = page.locator('div.cdk-overlay-backdrop-showing, [role="dialog"]')
        try:
            if not any(o.is_visible() for o in ov.all()):
                break
        except Exception:
            break
        page.keyboard.press("Escape")
        page.wait_for_timeout(600)


def _has_attachment(page) -> bool:
    """Ô nhập tin nhắn đã có khung xem trước file đính kèm chưa."""
    try:
        return page.evaluate("""() => {
            const c = document.querySelector('input-container, .text-input-field, fieldset');
            if (!c) return false;
            const sel = 'uploader-file-preview, [class*="file-preview"], [class*="attachment"], [data-test-id*="file"], ' +
                '[data-test-id*="cancel"], [data-test-id*="remove"], button[aria-label*="Remove" i], button[aria-label*="Xoá" i], ' +
                'button[aria-label*="Xóa" i], button[aria-label*="Cancel" i], button[aria-label*="Huỷ" i], button[aria-label*="Hủy" i], ' +
                'mat-progress-spinner, mat-spinner, [role="progressbar"], video';
            for (const el of c.querySelectorAll(sel)) {
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) return true;
            }
            for (const img of c.querySelectorAll('img')) {
                const r = img.getBoundingClientRect();
                if (r.width >= 32 && r.height >= 32) return true;
            }
            return false;
        }""")
    except Exception:
        return False


def _is_uploading(page) -> bool:
    """File trong ô nhập còn đang xoay vòng tải lên máy chủ Gemini chưa xong."""
    try:
        return page.evaluate("""() => {
            const c = document.querySelector('input-container, .text-input-field, fieldset');
            if (!c) return false;
            const sel = 'mat-progress-spinner, mat-spinner, [role="progressbar"], [class*="uploading"], ' +
                'button[aria-label*="Cancel upload" i], button[aria-label*="Stop upload" i], ' +
                'button[aria-label*="Huỷ tải" i], button[aria-label*="Hủy tải" i]';
            for (const el of c.querySelectorAll(sel)) {
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) return true;
            }
            return false;
        }""")
    except Exception:
        return False


def _wait_attached(page, secs: float = 8.0) -> bool:
    end = time.time() + secs
    while time.time() < end:
        if _has_attachment(page):
            return True
        page.wait_for_timeout(500)
    return False


def _attach_once(page, path: str):
    _close_overlays(page)
    page.locator(INPUT).first.click()
    plus = _upload_button(page)
    try:   # có giao diện bấm nút là mở hộp chọn tệp luôn
        with page.expect_file_chooser(timeout=3500) as fc:
            plus.click()
        fc.value.set_files(path)
        return
    except PWTimeout:
        pass
    _agree(page)
    # mục "Upload files / Tải tệp lên" trong menu (tên có thể dính chữ của biểu tượng -> lọc theo chữ hiển thị)
    item = page.locator('[role="menuitem"], [role="menu"] button, mat-action-list button, button').filter(has_text=UPLOAD_ITEM_TXT)
    for attempt in range(2):
        try:
            with page.expect_file_chooser(timeout=10_000) as fc:
                item.first.click(timeout=8000)
            fc.value.set_files(path)
            return
        except PWTimeout:
            if attempt:
                raise UploadError("Không mở được hộp chọn tệp của Gemini")
            _agree(page)   # lần đầu dùng có thể hiện hộp "Đồng ý" rồi mới mở hộp chọn tệp


def _attach(page, path: str):
    for _ in range(2):
        _attach_once(page, path)
        if _wait_attached(page, 8.0):
            return
        _agree(page)
    raise UploadError("Đã chọn file nhưng video chưa vào được ô nhập tin nhắn của Gemini")


MODEL_BTN = 'button[data-test-id="bard-mode-menu-button"], bard-mode-switcher button'


def ensure_model(page, model: str):
    """Luôn dùng đúng model (vd Neon): đang khác thì mở menu chọn lại; không chọn được thì KHÔNG gửi."""
    model = (model or "").strip()
    if not model:
        return
    want = re.compile(rf"\b{re.escape(model)}\b", re.I)
    btn = page.locator(MODEL_BTN).first
    try:
        btn.wait_for(state="visible", timeout=15_000)
    except PWTimeout:
        raise UploadError(f"Không thấy nút chọn model Gemini (cần model {model})")
    cur = lambda: (btn.get_attribute("aria-label") or "") + " " + btn.inner_text()
    if want.search(cur()):
        return
    for _ in range(2):
        btn.click()
        opt = page.get_by_role("menuitemradio").filter(has_text=want)
        try:
            opt.first.click(timeout=8000)
        except PWTimeout:
            page.keyboard.press("Escape")
            raise UploadError(f"Không thấy model {model} trong menu chọn model của Gemini")
        page.wait_for_timeout(1500)
        if want.search(cur()):
            return
    raise UploadError(f"Không chuyển được sang model {model}")


def _last_reply(page) -> str:
    r = page.locator(RESP)
    n = r.count()
    if not n:
        return ""
    try:
        return re.sub(r"\n{3,}", "\n\n", r.nth(n - 1).inner_text(timeout=5000)).strip()
    except Exception:
        return ""


def submit(page, url: str, path: str, prompt: str) -> int:
    """Mở cuộc trò chuyện, đính kèm video, gửi."""
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    if "accounts.google.com" in page.url:
        raise UploadError("LOGIN")
    try:
        page.locator(INPUT).first.wait_for(state="visible", timeout=30_000)
    except PWTimeout:
        raise UploadError("LOGIN")
    page.wait_for_timeout(2000)   # chờ tải hết lịch sử trò chuyện
    _agree(page)
    deadline = time.time() + float(setting("max_wait", "40") or 40) * 60
    while _response_busy(page, page.locator(RESP).last if page.locator(RESP).count() else None):   # Gemini còn đang làm video trước
        if time.time() > deadline:
            raise UploadError("Gemini vẫn đang làm video trước quá lâu — chưa gửi video mới")
        page.wait_for_timeout(5000)
    ensure_model(page, setting("model", "Neon"))
    before_q = page.locator("user-query").count()
    before = page.locator(RESP).count()
    _attach(page, path)
    page.wait_for_timeout(1500)
    _agree(page)
    if prompt.strip():
        type_text(page, page.locator(INPUT).first, prompt.strip(), clear=False)

    def _sent() -> bool:
        if page.locator("user-query").count() > before_q or page.locator(RESP).count() > before:
            return True
        return not _has_attachment(page) and _response_busy(
            page, page.locator(RESP).last if page.locator(RESP).count() else None)

    send_deadline = time.time() + 900   # chờ tối đa 15 phút cho video lớn tải lên xong
    clicks = 0
    while time.time() < send_deadline:
        if _sent():
            page.wait_for_timeout(2000)
            return before
        _agree(page)
        if _is_uploading(page):
            page.wait_for_timeout(1500)
            continue
        if not _has_attachment(page):
            page.wait_for_timeout(1500)
            if _sent():
                page.wait_for_timeout(2000)
                return before
            raise UploadError("Video bị mất khỏi ô nhập trước khi gửi (có thể lỗi tải file lên Gemini)")
        try:
            wait_enabled(page.locator(SEND), 5, "nút gửi của Gemini").click()
            clicks += 1
        except UploadError:
            page.wait_for_timeout(1500)
            continue
        for _ in range(8):   # chờ tới 4 giây xem tin nhắn đã đi chưa; chưa đi (vì Gemini còn đang nhận file) thì lặp lại bấm tiếp
            page.wait_for_timeout(500)
            if _sent():
                page.wait_for_timeout(2000)
                return before
        if clicks >= 3 and not _is_uploading(page):
            try:
                page.locator(INPUT).first.click()
                page.keyboard.press("Enter")
                page.wait_for_timeout(2000)
                if _sent():
                    return before
            except Exception:
                pass
    raise UploadError("Đã bấm Gửi nhưng Gemini chưa nhận tin nhắn (video còn kẹt ở ô nhập)")


# ---------------------------------------------------------------- chạy theo tài khoản
# Mỗi lần bấm "Nạp video": mỗi tab nhận đúng 1 video (tab_idx gán sẵn lúc chia). Tool mở Chrome của tài khoản,
# vào từng tab, đính kèm video, bấm gửi -> xong là "Đã nạp" (không chờ Gemini làm xong, bạn tự vào kiểm tra).
def _fail(page, task, msg):
    shot = f"gem{task['id']}_{int(time.time())}.png"
    try:
        page.screenshot(path=str(SHOTS_DIR / shot))
    except Exception:
        shot = None
    db.execute("UPDATE gem_tasks SET status='failed', error=?, screenshot=?, finished_at=? WHERE id=?",
               (msg[:1500], shot, db.now(), task["id"]))
    log.error("Gemini #%s lỗi: %s", task["id"], msg)


def _set_acc(aid, status, note, resume_at=None):
    db.execute("UPDATE gem_accounts SET status=?, note=?, resume_at=? WHERE id=?", (status, note, resume_at, aid))


def _limit(aid, note):
    hrs = float(setting("cooldown", "24") or 24)
    _set_acc(aid, "limit", note, (datetime.now() + timedelta(hours=hrs)).isoformat(timespec="seconds"))


def _response_busy(page, last=None) -> bool:
    stop = page.locator(STOP)
    if stop.count() and stop.first.is_visible():
        return True
    if last is not None and last.locator(RESP_BUSY).count():
        return True
    return False


def gemini_finished(page, w) -> str | None:
    """None = Gemini còn đang làm. 'video' = đã xong và có video. 'text' = đã xong nhưng không thấy video."""
    r = page.locator(RESP)
    n = r.count()
    if n <= w["before"]:
        return None
    last = r.nth(n - 1)
    if _response_busy(page, last):
        w["still"] = 0
        return None
    has_done_bar = last.locator(RESP_DONE).count() > 0
    if has_done_bar and last.locator(HAS_VIDEO).count() > 0:
        return "video"
    txt = _last_reply(page)
    w["still"] = w.get("still", 0) + 1 if txt and txt == w.get("last") else 0
    w["last"] = txt
    # Chưa hiện thanh nút cuối câu trả lời (👍/Sao chép) -> Gemini có thể đang chuyển bước chạy code, chờ kỹ (~90s)
    if w["still"] < (3 if has_done_bar else 18):
        return None
    return "video" if last.locator(HAS_VIDEO).count() else "text"


def _fp(path):
    st = path.stat()
    k = (str(path), st.st_size, st.st_mtime)
    if k not in _fp_cache:
        _fp_cache[k] = fingerprint(path)
    return _fp_cache[k]


def is_translated(name: str) -> bool:
    return "vietsub" in name.lower()


def next_video(aid, tab):
    """Lấy video CHƯA NẠP tiếp theo trong thư mục của lượt nạp gần nhất, giao cho (tài khoản, tab)."""
    folder = Path(setting("auto_folder") or "")
    if not folder.is_dir():
        return None
    with _pick_lock:
        idx = sent_index()
        seen = set()
        for f in sorted(x for x in folder.iterdir() if x.is_file() and x.suffix.lower() in VIDEO_EXTS):
            if is_translated(f.name):
                continue
            fh = _fp(f)
            if fh in seen or lookup(idx, str(f), fh):
                seen.add(fh)
                continue
            seen.add(fh)
            tid = db.execute("INSERT INTO gem_tasks(account_id, tab_idx, batch, video_path, name, size, fhash, created_at) "
                             "VALUES(?,?,?,?,?,?,?,?)", (aid, tab, "tu_nap", str(f), f.name, f.stat().st_size, fh, db.now()))
            return tid
    return None


# ---------------------------------------------------------------- tải video từ Gemini
# Chrome do Playwright điều khiển không tự lưu file tải về (chỉ giữ tạm với tên mã ngẫu nhiên, đóng Chrome là mất)
# -> bắt sự kiện tải về và lưu đúng tên (.mp4) vào thư mục tải về.
def download_dir() -> Path:
    d = Path(setting("download_dir") or (Path.home() / "Downloads" / "GeminiVideos"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def hook_downloads(ctx):
    pending = []

    def on_download(d):
        pending.append(d)

    for p in ctx.pages:
        p.on("download", on_download)
    ctx.on("page", lambda p: p.on("download", on_download))
    return pending


def save_downloads(pending):
    while pending:
        d = pending.pop(0)
        try:
            name = re.sub(r'[<>:"/\\|?*]+', "_", d.suggested_filename or "") or "gemini_video"
            if not Path(name).suffix:
                name += ".mp4"
            out = download_dir() / name
            n = 2
            while out.exists():
                out = out.with_name(f"{Path(name).stem} ({n}){Path(name).suffix}")
                n += 1
            d.save_as(str(out))
            log.info("Đã lưu video tải về: %s", out)
        except Exception:
            log.exception("Lỗi lưu file tải về")


def pump(ctx, pending, secs):
    """Chờ secs giây NHƯNG vẫn xử lý sự kiện của Chrome (tải về...) và lưu file vừa tải."""
    end = time.time() + secs
    while time.time() < end and not _stop.is_set():
        pages = [p for p in ctx.pages if not p.is_closed()]
        try:
            if pages:
                pages[0].wait_for_timeout(min(1000, max(50, (end - time.time()) * 1000)))
            else:
                time.sleep(0.5)
        except Exception:
            time.sleep(0.5)
        save_downloads(pending)
        if not pages:
            break


def run_account(aid):
    key = pkey(aid)
    lock = profile_lock(key)
    if not lock.acquire(blocking=False):
        return
    try:
        acc = db.row("SELECT * FROM gem_accounts WHERE id=?", (aid,))
        urls = tabs_of(acc) if acc else []
        if not acc or acc["status"] != "ok" or (not urls and not acc["refresh"]):
            return
        prompt, gap = setting("prompt"), float(setting("gap", "5") or 5)
        with sync_playwright() as pw:
            ctx = launch(pw, key, headless=False)
            dl = hook_downloads(ctx)
            try:
                pages: dict[int, object] = {}
                fails = 0

                def tab(i):   # mỗi cuộc trò chuyện một tab (giữ nguyên tab để bạn xem); tab bị đóng thì mở lại
                    if i not in pages or pages[i].is_closed():
                        free = [p for p in ctx.pages if p not in pages.values() and p.url in ("about:blank", "")]
                        pages[i] = free[0] if free else ctx.new_page()
                        pages[i].set_default_timeout(60_000)
                    return pages[i]

                watch: dict[int, dict] = {}   # tab -> Gemini đang làm video nào
                _watching[aid] = {}

                def check_watch():
                    for i, w in list(watch.items()):
                        page = pages.get(i)
                        if not page or page.is_closed():
                            watch.pop(i)
                            continue
                        try:
                            res = gemini_finished(page, w)
                        except Exception:
                            res = None
                        if res is None and time.time() - w["at"] > float(setting("max_wait", "40") or 40) * 60:
                            res = "timeout"
                        if res is None:
                            continue
                        watch.pop(i)
                        _watching[aid].pop(i, None)
                        if res == "video":
                            db.execute("UPDATE gem_tasks SET error=NULL WHERE id=?", (w["tid"],))
                            queued = db.row("SELECT 1 FROM gem_tasks WHERE account_id=? AND tab_idx=? AND status='pending'", (aid, i))
                            if not queued and setting("auto_next", "1") == "1" and setting("paused", "0") != "1":
                                if next_video(aid, i):
                                    log.info("Gemini tk %s tab %s: xong video, tự nạp video tiếp theo", aid, i + 1)
                        elif not w.get("setup"):
                            auto = setting("auto_next", "1") == "1"
                            tail = " — tab này dừng tự nạp, vào kiểm tra" if auto else " — vào kiểm tra"
                            db.execute("UPDATE gem_tasks SET error=? WHERE id=?", (
                                ("⚠ Gemini chưa làm xong sau thời gian chờ" if res == "timeout" else
                                 "⚠ Gemini trả lời nhưng chưa thấy video") + tail, w["tid"]))
                    for i, page in list(pages.items()):
                        if i in watch or not page or page.is_closed():
                            continue
                        warn = db.row("SELECT id FROM gem_tasks WHERE account_id=? AND tab_idx=? AND status='done' "
                                      "AND error LIKE '⚠%' ORDER BY id DESC LIMIT 1", (aid, i))
                        if warn:
                            try:
                                r = page.locator(RESP)
                                if r.count() and r.last.locator(HAS_VIDEO).count():
                                    db.execute("UPDATE gem_tasks SET error=NULL WHERE id=?", (warn["id"],))
                            except Exception:
                                pass

                def ready_task():
                    """Video chờ nạp tiếp theo của một tab đang rảnh (tab Gemini còn làm dở thì để sau)."""
                    for r in db.rows("SELECT * FROM gem_tasks WHERE account_id=? AND status='pending' ORDER BY id", (aid,)):
                        if (r["tab_idx"] if r["tab_idx"] is not None else 0) not in watch:
                            return r
                    return None

                def want_refresh():
                    return db.row("SELECT 1 FROM gem_accounts WHERE id=? AND refresh=1 AND status='ok'", (aid,))

                def do_refresh():
                    """Làm mới tab: mở N cuộc trò chuyện MỚI, dán câu lệnh thiết lập, gửi, lưu link làm tab của tài khoản."""
                    setup = setting("setup_prompt").strip()
                    n = max(1, int(setting("tab_count", "3") or 3))
                    if not setup:
                        db.execute("UPDATE gem_accounts SET refresh=0, note=? WHERE id=?",
                                   ("Chưa có câu lệnh thiết lập phiên mới — nhập ở mục 🆕 Phiên mới", aid))
                        return
                    new = []
                    try:
                        for i in range(n):
                            watch.pop(i, None)
                            _watching[aid].pop(i, None)
                            page = tab(i)
                            page.bring_to_front()
                            page.goto(GEM_HOME, wait_until="domcontentloaded")
                            page.wait_for_timeout(4000)
                            if "accounts.google.com" in page.url:
                                raise UploadError("LOGIN")
                            page.locator(INPUT).first.wait_for(state="visible", timeout=30_000)
                            _agree(page)
                            ensure_model(page, setting("model", "Neon"))
                            type_text(page, page.locator(INPUT).first, setup, clear=True)
                            wait_enabled(page.locator(SEND), 60, "nút gửi của Gemini").click()
                            page.wait_for_url(re.compile(r"/app/[0-9a-f]{6,}"), timeout=90_000)
                            u = clean_tab(page.url)
                            if not u:
                                raise UploadError("Gemini không tạo cuộc trò chuyện mới")
                            new.append(u)
                            # chờ Gemini trả lời xong câu lệnh thiết lập rồi mới nạp video vào tab này
                            watch[i] = {"tid": None, "before": 0, "at": time.time(), "setup": True}
                            _watching[aid][i] = "(đang thiết lập phiên mới)"
                            log.info("Gemini tk %s: mở phiên mới tab %s -> %s", aid, i + 1, u)
                            _stop.wait(gap)
                        urls[:] = new
                        db.execute("UPDATE gem_accounts SET tabs=?, refresh=0, note=? WHERE id=?",
                                   (json.dumps(new), f"🆕 Đã mở {len(new)} phiên mới lúc {datetime.now():%H:%M %d/%m}", aid))
                    except Exception as e:
                        msg = str(e) if isinstance(e, UploadError) else f"{type(e).__name__}: {e}"
                        if msg == "LOGIN":
                            _set_acc(aid, "login", "Chưa đăng nhập Google — bấm Mở Chrome để đăng nhập")
                            msg = "chưa đăng nhập"
                        log.error("Gemini tk %s: lỗi làm mới tab: %s\n%s", aid, msg, traceback.format_exc())
                        if new:   # mở được vài tab thì vẫn dùng: tab mới + tab cũ còn lại
                            urls[:] = new + urls[len(new):]
                            db.execute("UPDATE gem_accounts SET tabs=? WHERE id=?", (json.dumps(urls), aid))
                        db.execute("UPDATE gem_accounts SET refresh=0, note=? WHERE id=?",
                                   (f"⚠ Làm mới tab lỗi ở tab {len(new) + 1}: {msg[:200]}", aid))

                def want_more():
                    return want_refresh() or (setting("paused", "0") != "1" and db.row(
                        "SELECT 1 FROM gem_accounts WHERE id=? AND status='ok'", (aid,)) and ready_task())

                while not _stop.is_set():
                    check_watch()
                    cur = db.row("SELECT tabs FROM gem_accounts WHERE id=?", (aid,))
                    if cur and tabs_of(cur) and tabs_of(cur) != urls:   # người dùng vừa sửa link tab (nút ✎) -> dùng ngay
                        for i, u in enumerate(tabs_of(cur)):
                            if i >= len(urls) or urls[i] != u:
                                watch.pop(i, None)
                                _watching[aid].pop(i, None)
                                if i in pages and not pages[i].is_closed():
                                    try:
                                        pages[i].goto(u, wait_until="domcontentloaded")
                                    except Exception:
                                        pass
                        urls[:] = tabs_of(cur)
                    if want_refresh():
                        _idle.discard(aid)
                        do_refresh()
                        continue
                    if not want_more() and watch:
                        # còn tab Gemini đang làm (tự nạp tiếp) -> chờ, kiểm tra mỗi 5 giây
                        _idle.add(aid)
                        pump(ctx, dl, 5)
                        if not ctx.pages:
                            break
                        continue
                    _idle.discard(aid)
                    if not want_more():
                        if setting("keep_open", "1") != "1":
                            _stop.wait(float(setting("linger", "15") or 15))   # để Gemini nhận hết video trước khi đóng Chrome
                            break
                        # Giữ Chrome mở để xem kết quả: mở đủ các tab, chờ tới khi người dùng đóng cửa sổ.
                        # Trong lúc đó bấm "Nạp video" lần nữa thì nạp luôn vào cửa sổ này.
                        _idle.add(aid)
                        for i, u in enumerate(urls):
                            if i not in pages or pages[i].is_closed():
                                try:
                                    tab(i).goto(u, wait_until="domcontentloaded")
                                except Exception:
                                    pass
                        while not _stop.is_set() and ctx.pages and not want_more():
                            pump(ctx, dl, 3)
                            check_watch()
                        _idle.discard(aid)
                        if not ctx.pages:
                            break   # người dùng đã đóng cửa sổ
                        continue
                    t = ready_task()
                    if not t:
                        continue
                    i = t["tab_idx"] if t["tab_idx"] is not None and t["tab_idx"] < len(urls) else 0
                    db.execute("UPDATE gem_tasks SET status='running', tab_idx=?, started_at=?, error=NULL WHERE id=?", (i, db.now(), t["id"]))
                    page = tab(i)
                    page.bring_to_front()
                    try:
                        if not Path(t["video_path"]).is_file():
                            raise UploadError(f"Không tìm thấy file: {t['video_path']}")
                        before = submit(page, urls[i], t["video_path"], prompt)
                        body = page.locator("body").inner_text(timeout=10_000)
                        tail = body[-1500:]
                        if LIMIT_RX.search(tail):
                            db.execute("UPDATE gem_tasks SET status='failed', error=?, chat_url=?, finished_at=? WHERE id=?",
                                       ("Gemini báo hết lượt sau khi gửi — kiểm tra lại tab này", page.url, db.now(), t["id"]))
                            _limit(aid, "Gemini báo hết lượt")
                            break
                        db.execute("UPDATE gem_tasks SET status='done', chat_url=?, finished_at=? WHERE id=?", (page.url, db.now(), t["id"]))
                        db.execute("UPDATE gem_accounts SET note=NULL WHERE id=? AND note LIKE 'Dừng:%'", (aid,))
                        log.info("Gemini #%s: đã nạp %s vào tab %s (tk %s)", t["id"], Path(t["video_path"]).name, i + 1, aid)
                        fails = 0
                        watch[i] = {"tid": t["id"], "before": before, "at": time.time()}
                        _watching[aid][i] = Path(t["video_path"]).name
                    except Exception as e:
                        msg = str(e) if isinstance(e, UploadError) else f"{type(e).__name__}: {e}"
                        if msg == "LOGIN":
                            db.execute("UPDATE gem_tasks SET status='pending', started_at=NULL WHERE id=?", (t["id"],))
                            _set_acc(aid, "login", "Chưa đăng nhập Google — bấm Mở Chrome để đăng nhập")
                            break
                        try:
                            body = page.locator("body").inner_text(timeout=5000)
                        except Exception:
                            body = ""
                        if LIMIT_RX.search(body[-3000:]):
                            db.execute("UPDATE gem_tasks SET status='pending', started_at=NULL WHERE id=?", (t["id"],))
                            _limit(aid, "Gemini báo hết lượt")
                            break
                        log.error("Gemini #%s: %s\n%s", t["id"], msg, traceback.format_exc())
                        _fail(page, t, msg)
                        fails += 1
                        if fails >= 3:
                            db.execute("UPDATE gem_accounts SET note=? WHERE id=?", ("Dừng: 3 video lỗi liên tiếp — xem ảnh lỗi", aid))
                            break
                    _stop.wait(gap)
            finally:
                try:
                    save_downloads(dl)
                except Exception:
                    pass
                _idle.discard(aid)
                _watching.pop(aid, None)
                try:
                    ctx.close()
                except Exception:
                    pass
    except Exception:
        log.exception("Lỗi chạy tài khoản Gemini %s", aid)
    finally:
        db.execute("UPDATE gem_tasks SET status='pending', started_at=NULL WHERE account_id=? AND status='running'", (aid,))
        lock.release()


def _loop():
    busy: dict[int, threading.Thread] = {}
    while not _stop.is_set():
        try:
            for k in [k for k, t in busy.items() if not t.is_alive()]:
                del busy[k]
            db.execute("UPDATE gem_accounts SET status='ok', resume_at=NULL, note=NULL WHERE status='limit' AND resume_at<=?", (db.now(),))
            if setting("paused", "0") != "1":
                limit = max(1, int(setting("parallel", "1") or 1))
                ids = [r["account_id"] for r in db.rows(
                    "SELECT DISTINCT t.account_id FROM gem_tasks t JOIN gem_accounts a ON a.id=t.account_id "
                    "WHERE t.status='pending' AND a.status='ok' AND a.tabs!='[]' "
                    "UNION SELECT id FROM gem_accounts WHERE refresh=1 AND status='ok' ORDER BY 1")]
                for aid in ids:
                    if len([k for k in busy if k not in _idle]) >= limit:
                        break
                    if aid in busy or (aid in _login_threads and _login_threads[aid].is_alive()):
                        continue
                    th = threading.Thread(target=run_account, args=(aid,), name=f"gem-{aid}", daemon=True)
                    busy[aid] = th
                    th.start()
        except Exception:
            log.exception("Lỗi vòng lặp Gemini")
        _stop.wait(3)


def start():
    threading.Thread(target=_loop, name="gemini-worker", daemon=True).start()


def stop():
    _stop.set()


def open_login(aid: int):
    """Mở Chrome của tài khoản: đăng nhập Google + mở sẵn các tab trò chuyện đã lưu.
    Khi người dùng ĐÓNG cửa sổ, tool ghi lại các tab Gemini đang mở làm danh sách tab của tài khoản."""
    t = _login_threads.get(aid)
    if t and t.is_alive():
        return "already_open"
    lock = profile_lock(pkey(aid))
    if not lock.acquire(blocking=False):
        return "busy"
    acc = db.row("SELECT * FROM gem_accounts WHERE id=?", (aid,))
    urls = tabs_of(acc) or [GEM_HOME]

    def _run():
        seen: dict = {}
        try:
            with sync_playwright() as pw:
                ctx = launch(pw, pkey(aid), headless=False)
                dl = hook_downloads(ctx)
                for i, u in enumerate(urls):
                    page = ctx.pages[0] if i == 0 and ctx.pages else ctx.new_page()
                    try:
                        page.goto(u)
                    except Exception:
                        pass
                while ctx.pages:   # ghi nhận link từng tab cho tới khi đóng cửa sổ
                    seen = {id(p): p.url for p in ctx.pages}
                    pump(ctx, dl, 1.5)
                try:
                    ctx.close()
                except Exception:
                    pass
        except Exception:
            log.exception("Lỗi mở Chrome đăng nhập Gemini")
        finally:
            found = []
            for u in seen.values():
                c = clean_tab(u or "")
                if c and c not in found:
                    found.append(c)
            if found:
                db.execute("UPDATE gem_accounts SET tabs=? WHERE id=?", (json.dumps(found), aid))
            db.execute("UPDATE gem_accounts SET status='ok', note=NULL WHERE id=? AND status='login'", (aid,))
            lock.release()

    t = threading.Thread(target=_run, name=f"gem-login-{aid}", daemon=True)
    _login_threads[aid] = t
    t.start()
    return "opened"


# ---------------------------------------------------------------- nhận diện video trùng
def fingerprint(path) -> str:
    """Dấu vân tay nội dung: dung lượng + băm 2 MB đầu và 1 MB cuối (đổi tên file vẫn nhận ra)."""
    p = Path(path)
    size = p.stat().st_size
    h = hashlib.sha1(str(size).encode())
    with p.open("rb") as f:
        h.update(f.read(2 << 20))
        if size > 3 << 20:
            f.seek(-(1 << 20), 2)
            h.update(f.read())
    return h.hexdigest()


def sent_index() -> dict:
    """Mọi video đã từng nạp/chờ nạp (kể cả dòng đã ẩn), tra theo đường dẫn hoặc dấu vân tay."""
    idx = {}
    for r in db.rows("SELECT t.id, t.video_path, t.name, t.fhash, t.status, t.tab_idx, t.finished_at, t.created_at, t.chat_url, "
                     "a.name AS account_name FROM gem_tasks t LEFT JOIN gem_accounts a ON a.id=t.account_id ORDER BY t.id"):
        idx[r["video_path"]] = r
        if r["fhash"]:
            idx[r["fhash"]] = r
    return idx


def lookup(idx, path, fh):
    return idx.get(path) or idx.get(fh)


# ---------------------------------------------------------------- chia việc: mỗi tab 1 video / lượt
def free_slots(account_ids: list[int]) -> list[tuple[int, int]]:
    """Mọi (tài khoản, tab) của các tài khoản sẵn sàng — mỗi lần bấm Nạp, MỖI TAB nhận thêm 1 video.
    Tab đang bận (Gemini đang làm / còn video chờ) thì video mới xếp hàng, làm xong video trước mới gửi.
    Xếp xen kẽ tài khoản để chia đều."""
    accs = {a["id"]: a for a in db.rows("SELECT * FROM gem_accounts")}
    per = {a: [(a, i) for i in range(len(tabs_of(accs[a])))]
           for a in account_ids if a in accs and accs[a]["status"] == "ok"}
    out, k = [], 0
    while any(len(v) > k for v in per.values()):
        out += [v[k] for v in per.values() if len(v) > k]
        k += 1
    return out


def distribute(items: list[dict], slots: list[tuple[int, int]], batch: str) -> list[dict]:
    """items: [{path, name, size, fhash}] chưa nạp, theo thứ tự thư mục. Mỗi (tài khoản, tab) nhận 1 video."""
    out = []
    for it, (aid, tab) in zip(items, slots):
        tid = db.execute("INSERT INTO gem_tasks(account_id, tab_idx, batch, video_path, name, size, fhash, created_at) "
                         "VALUES(?,?,?,?,?,?,?,?)", (aid, tab, batch, it["path"], it["name"], it["size"], it["fhash"], db.now()))
        out.append({"id": tid, "account_id": aid, "tab": tab, "name": it["name"]})
    return out


def rebalance() -> int:
    """Video đang chờ của tài khoản không chạy được -> chuyển sang tab trống của tài khoản đang ổn."""
    stuck = db.rows("SELECT t.id FROM gem_tasks t LEFT JOIN gem_accounts a ON a.id=t.account_id "
                    "WHERE t.status='pending' AND (a.id IS NULL OR a.status!='ok' OR a.tabs='[]') ORDER BY t.id")
    ok = [r["id"] for r in db.rows("SELECT id FROM gem_accounts WHERE status='ok' AND tabs!='[]' ORDER BY id")]
    slots = free_slots(ok)
    for t, (aid, tab) in zip(stuck, slots):
        db.execute("UPDATE gem_tasks SET account_id=?, tab_idx=? WHERE id=?", (aid, tab, t["id"]))
    return min(len(stuck), len(slots))
