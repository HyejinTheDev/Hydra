import re
import time
from urllib.parse import urljoin


class UploadError(Exception):
    pass


class NotLoggedIn(UploadError):
    def __init__(self, platform):
        super().__init__(f"Chưa đăng nhập {platform}. Vào tab Tài khoản và bấm 'Đăng nhập' cho tài khoản này.")


def rx(*words: str) -> re.Pattern:
    """Regex khớp đúng một trong các nhãn (hỗ trợ giao diện tiếng Anh và tiếng Việt)."""
    return re.compile(r"^\s*(" + "|".join(re.escape(w) for w in words) + r")\s*$", re.I)


def build_caption(title: str, description: str, hashtags: str) -> str:
    """Tiêu đề, xuống 1 dòng là mô tả; hashtag nối liền ngay sau chữ cuối (không có dòng trống)."""
    text = "\n".join(p.strip() for p in (title, description) if p and p.strip())
    tags = normalize_hashtags(hashtags)
    return f"{text} {tags}".strip() if tags else text


def normalize_hashtags(hashtags: str) -> str:
    words = [w.strip().lstrip("#") for w in re.split(r"[\s,]+", hashtags or "") if w.strip().lstrip("#")]
    return " ".join("#" + w for w in words)


def type_text(page, locator, text: str, clear: bool = True):
    """Gõ vào ô contenteditable/textarea, giữ xuống dòng."""
    locator.click()
    if clear:
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line:
            page.keyboard.insert_text(line)
        if i < len(lines) - 1:
            page.keyboard.press("Shift+Enter")
    time.sleep(0.5)


def try_click(locator, timeout=3000) -> bool:
    try:
        locator.first.click(timeout=timeout)
        return True
    except Exception:
        return False


def _ready(el) -> bool:
    try:
        return (el.is_visible() and el.is_enabled() and el.get_attribute("aria-disabled") != "true"
                and el.get_attribute("disabled") is None)
    except Exception:
        return False


def wait_enabled(locator, timeout_s: float, what: str):
    """Chờ nút hiện ra và hết bị vô hiệu (thường là chờ video tải lên xong).
    Trang có thể có nhiều nút trùng tên (vd: Facebook có nút "Đăng" ẩn) -> xét TẤT CẢ,
    lấy nút cuối cùng đang hiển thị và bấm được (thường là nút của hộp đăng bài)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            n = locator.count()
            for i in range(n - 1, -1, -1):
                el = locator.nth(i)
                if _ready(el):
                    return el
        except Exception:
            pass
        time.sleep(2)
    raise UploadError(f"Hết thời gian chờ: {what}")


# ---------------- Bình luận đầu tiên (affiliate) ----------------
def caption_snippet(title: str) -> str:
    """Một đoạn ngắn đầu chú thích để nhận ra đúng bài vừa đăng."""
    t = re.sub(r"\s+", " ", title or "").strip()
    return t[:18].strip()


def page_has_text(page, snippet: str) -> bool:
    if not snippet:
        return True
    try:
        body = re.sub(r"\s+", " ", page.locator("body").inner_text(timeout=10_000))
    except Exception:
        return False
    return snippet.lower() in body.lower()


def find_new_post(page, list_url: str, link_sel: str, snippet: str, minutes: float = 10, check: int = 3) -> str:
    """Mở trang danh sách bài (trang cá nhân / tab Reels), thử vài bài mới nhất,
    trả về link bài có chú thích chứa `snippet`. Video còn đang xử lý thì chờ rồi thử lại."""
    deadline = time.time() + minutes * 60
    while True:
        page.goto(list_url, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        hrefs = []
        for _ in range(6):   # cuộn xuống để tải thêm bài cũ khi cần xét nhiều bài
            for a in page.locator(link_sel).all():
                h = a.get_attribute("href") or ""
                if h and h not in hrefs:
                    hrefs.append(h)
            if len(hrefs) >= check:
                break
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(2000)
        for h in hrefs[:check]:
            url = urljoin(page.url, h)
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            if page_has_text(page, snippet):
                return page.url
        if time.time() > deadline:
            raise UploadError("Không tìm thấy bài vừa đăng để bình luận (có thể video còn đang xử lý)")
        page.wait_for_timeout(45_000)


# ---------------- Hẹn giờ trên chính nền tảng ----------------
class ScheduleTooFar(UploadError):
    """Ngày hẹn xa hơn mức nền tảng cho phép -> worker sẽ lùi việc tải lên đến gần ngày đăng hơn."""


MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December"]


def day_cell_regex(dt) -> re.Pattern:
    """Khớp ô ngày trong lịch Meta (Facebook/Threads), cả tiếng Anh lẫn tiếng Việt:
    'Wednesday 25 November 2026' / 'Thứ Tư, 25 Tháng 11, 2026'."""
    month = f"({MONTHS_EN[dt.month - 1]}|tháng {dt.month})"
    return re.compile(rf"(^|\D){dt.day}\D.*\b{month}\b.*{dt.year}", re.I)


def pick_meta_day(page, dt, next_name, what: str, max_months: int = 14):
    """Chọn ngày trong lịch kiểu Meta: tìm ô ngày, chưa thấy thì bấm 'Tháng sau'."""
    pat = day_cell_regex(dt)
    for _ in range(max_months):
        cell = page.locator('[role="gridcell"]').filter(has_text=pat)
        if cell.count():
            if cell.first.get_attribute("aria-disabled") == "true":
                raise ScheduleTooFar(f"{what} chưa cho hẹn tới ngày {dt:%d/%m/%Y}")
            cell.first.click()
            return
        nxt = page.get_by_role("button", name=next_name)
        if nxt.count() == 0 or nxt.first.get_attribute("aria-disabled") == "true":
            break
        nxt.first.click()
        page.wait_for_timeout(500)
    raise ScheduleTooFar(f"{what} chưa cho hẹn tới ngày {dt:%d/%m/%Y}")


# ---------------- Đọc ngày/giờ hiển thị (tiếng Việt / tiếng Anh) ----------------
_MON3 = {m[:3].lower(): i + 1 for i, m in enumerate(MONTHS_EN)}


def parse_date_any(text: str):
    """'Ngày 24 tháng 11 năm 2026' / '24 Tháng 11, 2026' / '24 thg 11, 2026' / 'Nov 24, 2026' /
    '24 November 2026' / '24/11/2026' / '2026-11-24'  ->  (2026, 11, 24). Không đọc được -> None."""
    t = re.sub(r"\s+", " ", (text or "").replace("\u202f", " ").replace("\xa0", " ")).strip().lower()
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        return int(m[1]), int(m[2]), int(m[3])
    m = re.search(r"(\d{1,2})\s*(?:tháng|thg)\s*(\d{1,2})\D+?(\d{4})", t)
    if m:
        return int(m[3]), int(m[2]), int(m[1])
    m = re.search(r"([a-z]{3})[a-z]*\.? (\d{1,2}),? (\d{4})", t)
    if m and m[1] in _MON3:
        return int(m[3]), _MON3[m[1]], int(m[2])
    m = re.search(r"(\d{1,2}) ([a-z]{3})[a-z]*\.?,? (\d{4})", t)
    if m and m[2] in _MON3:
        return int(m[3]), _MON3[m[2]], int(m[1])
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", t)
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        return (y, a, b) if b > 12 >= a else (y, b, a)
    return None


def parse_time_any(text: str):
    """'09:00' / '9:00 AM' / '4:00 CH' / '16:00'  ->  (giờ 0-23, phút). Không đọc được -> None."""
    t = (text or "").replace("\u202f", " ").replace("\xa0", " ").strip().upper()
    m = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM|SA|CH)?", t)
    if not m:
        return None
    h, mi, ap = int(m[1]), int(m[2]), m[3]
    if ap in ("AM", "SA"):
        h = 0 if h == 12 else h
    elif ap in ("PM", "CH"):
        h = 12 if h == 12 else h + 12
    return h, mi


def comment_key(text: str) -> str:
    """Đoạn nhận diện bình luận affiliate trên trang: phần chữ trước link (vd 'Ốp điện thoại siêu cấp vô địch vũ trụ'),
    bình luận chỉ có link thì lấy phần link bỏ 'https://' (Facebook hay ẩn tiền tố này)."""
    t = re.sub(r"\s+", " ", text or "").strip()
    head = re.split(r"https?://", t, maxsplit=1)[0].strip(" :-–—👉")
    if len(head) >= 8:
        return head[:40]
    m = re.search(r"https?://(\S{8,})", t)
    return (m[1][:28] if m else t[:28]).strip()


def already_commented(page, text: str, wait_ms: int = 3000) -> bool:
    """Bài đã có sẵn bình luận này chưa (đã gắn ở lần chạy trước / người dùng tự gắn tay) -> tránh bình luận trùng."""
    page.wait_for_timeout(wait_ms)   # chờ khung bình luận tải xong
    return page_has_text(page, comment_key(text))
