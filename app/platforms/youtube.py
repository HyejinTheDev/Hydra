import re
import time

from .base import (NotLoggedIn, UploadError, normalize_hashtags, parse_date_any, parse_time_any, type_text, try_click,
                   wait_enabled)

LIMIT_RX = re.compile(r"giới hạn tải lên hằng ngày|giới hạn tải lên hàng ngày|daily upload limit", re.I)

UPLOAD_TIMEOUT_S = 60 * 60  # video lớn có thể tải lâu


def upload(page, job) -> str:
    page.goto("https://www.youtube.com/upload", wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    if "accounts.google.com" in page.url or "ServiceLogin" in page.url:
        raise NotLoggedIn("YouTube")

    # Một số tài khoản mới phải tạo kênh trước
    if "create_channel" in page.url:
        raise UploadError("Tài khoản Google này chưa có kênh YouTube. Hãy tạo kênh trước.")

    page.locator('input[type="file"]').first.set_input_files(job["video_path"])

    title_box = page.locator("#title-textarea #textbox")
    title_box.wait_for(state="visible", timeout=120_000)
    page.wait_for_timeout(3000)
    _check_limit(page)
    # Shorts chỉ hiện tiêu đề dưới video -> gắn thêm hashtag vào cuối tiêu đề (tối đa 100 ký tự)
    title = (job["title"] or "Video").strip()[:100]
    for tag in normalize_hashtags(job["hashtags"]).split():
        if len(title) + 1 + len(tag) <= 100:
            title += " " + tag
    type_text(page, title_box, title)

    desc = job["description"] or ""
    tags = normalize_hashtags(job["hashtags"])
    if tags:
        desc = (desc.strip() + " " + tags).strip()
    if desc:
        type_text(page, page.locator("#description-textarea #textbox"), desc[:5000])

    # "Không, nội dung này không dành cho trẻ em"
    page.locator('tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]').click()

    # Chi tiết -> Yếu tố video -> Kiểm tra -> Chế độ hiển thị
    for _ in range(3):
        wait_enabled(page.locator("#next-button"), 120, "nút Tiếp theo của YouTube").click()
        page.wait_for_timeout(1500)

    when = job.get("schedule_at")
    if when:
        _set_schedule(page, when)
    else:
        vis = job.get("yt_visibility") or "PUBLIC"
        page.locator(f'tp-yt-paper-radio-button[name="{vis}"]').click()
    page.wait_for_timeout(1000)

    url = None
    try:
        link = page.locator("span.video-url-fadeable a, a.ytcp-video-info").first
        link.wait_for(state="attached", timeout=60_000)
        url = link.get_attribute("href") or link.inner_text()
    except Exception:
        pass

    _wait_upload_finished(page)

    wait_enabled(page.locator("#done-button"), 300, "nút Xong/Xuất bản của YouTube").click()
    page.wait_for_timeout(3000)
    try_click(page.locator("ytcp-video-share-dialog #close-button, #close-button"), timeout=30_000)
    return (url or "").strip() or "https://studio.youtube.com/"


def _check_limit(page):
    """Kênh hết lượt tải lên trong ngày -> dừng ngay, báo rõ (không thử tiếp các bước sau)."""
    try:
        if page.get_by_text(LIMIT_RX).count():
            raise UploadError("YouTube: kênh đã đạt GIỚI HẠN TẢI LÊN HẰNG NGÀY. Chờ 24 giờ hoặc xác minh "
                              "số điện thoại kênh (studio.youtube.com -> Cài đặt -> Kênh -> Điều kiện sử dụng tính năng).")
    except UploadError:
        raise
    except Exception:
        pass


def _wait_upload_finished(page):
    """Chờ thanh tiến trình không còn hiển thị % (đã tải lên xong) trước khi đóng trình duyệt."""
    label = page.locator("ytcp-video-upload-progress .progress-label").first
    deadline = time.time() + UPLOAD_TIMEOUT_S
    stable = 0
    while time.time() < deadline:
        try:
            text = label.inner_text(timeout=5000)
        except Exception:
            text = ""
        if re.search(r"\d+\s*%", text):
            stable = 0
        else:
            stable += 1
            if stable >= 2:
                return
        time.sleep(3)
    raise UploadError("Hết thời gian chờ YouTube tải video lên")


MON_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _date_candidates(sample: str, when) -> list[str]:
    """Các cách viết ngày để gõ vào ô ngày của YouTube; kiểu giống ô đang hiển thị được thử trước."""
    d, m, y = when.day, when.month, when.year
    forms = {
        "vi_thg": f"{d} thg {m}, {y}",
        "en": f"{MON_EN[m - 1]} {d}, {y}",
        "en_dmy": f"{d} {MON_EN[m - 1]} {y}",
        "vi_long": f"Ngày {d} tháng {m} năm {y}",
        "vi_thang": f"{d} tháng {m}, {y}",
        "slash": f"{d:02d}/{m:02d}/{y}",
        "us_slash": f"{m:02d}/{d:02d}/{y}",
    }
    s = (sample or "").strip().lower()
    if "thg" in s:
        first = "vi_thg"
    elif s.startswith("ngày"):
        first = "vi_long"
    elif "tháng" in s:
        first = "vi_thang"
    elif re.match(r"^\d{1,2}\s+[a-z]{3}\b", s):
        first = "en_dmy"
    elif "/" in s:
        parts = [int(x) for x in re.findall(r"\d+", s)[:2]]
        first = "us_slash" if len(parts) == 2 and parts[1] > 12 >= parts[0] else "slash"
    else:
        first = "en"
    return [forms[first]] + [v for k, v in forms.items() if k != first]


def _picker_invalid(page) -> bool:
    return page.locator("ytcp-date-picker tp-yt-paper-input[invalid], ytcp-date-picker [invalid]").count() > 0


def _verified_date(page, trigger, want_d) -> tuple[bool, str]:
    """Kiểm tra ngày đã nhận trên YouTube; đọc lại trực tiếp từ input nếu nhãn ngoài bị Chrome Dịch giữ chữ cũ."""
    shown = trigger.inner_text().strip()
    if parse_date_any(shown) == want_d and trigger.locator("font").count() == 0:
        return True, shown
    di = page.locator("ytcp-date-picker tp-yt-paper-input input, ytcp-date-picker input").first
    if di.is_visible() and _picker_invalid(page):
        return False, shown
    opened = False
    if not di.is_visible():
        trigger.click()
        page.wait_for_timeout(800)
        opened = True
    val = di.input_value().strip() if not _picker_invalid(page) else ""
    if opened and di.is_visible():
        trigger.click()
        page.wait_for_timeout(500)
    if parse_date_any(val) == want_d:
        return True, val
    return False, val or shown


def _time_candidates(sample: str, when) -> list[str]:
    """YouTube có thể hiện giờ 24h ('16:00') hoặc 12h ('4:00 PM' / '4:00 CH')."""
    h, mi = when.hour, when.minute
    h12 = h % 12 or 12
    s = (sample or "").upper()
    if "SA" in s or "CH" in s:
        twelve = f"{h12}:{mi:02d} {'SA' if h < 12 else 'CH'}"
    else:
        twelve = f"{h12}:{mi:02d} {'AM' if h < 12 else 'PM'}"
    plain = f"{h:02d}:{mi:02d}"
    return [twelve, plain] if re.search(r"AM|PM|SA|CH", s) else [plain, twelve]


def _set_schedule(page, when):
    """Mục Chế độ hiển thị -> Lên lịch -> điền ngày giờ đăng (công khai đúng giờ).
    So sánh theo GIÁ TRỊ ngày/giờ (không so chuỗi) vì YouTube hiển thị khác kiểu với kiểu gõ vào."""
    page.locator("#second-container-expand-button").click()
    page.wait_for_timeout(1500)
    trigger = page.locator("#datepicker-trigger")
    want_d = (when.year, when.month, when.day)
    shown = ""
    ok = False
    tried = []
    sample = None
    cands = []
    for _ in range(8):
        di = page.locator("ytcp-date-picker tp-yt-paper-input input, ytcp-date-picker input").first
        if not di.is_visible():
            trigger.click()
            page.wait_for_timeout(1000)
        if sample is None:
            sample = di.input_value()
            cands = _date_candidates(sample, when)
        if not cands:
            break
        text = cands.pop(0)
        tried.append(text)
        di.fill(text)
        page.keyboard.press("Enter")
        page.wait_for_timeout(1200)
        ok, shown = _verified_date(page, trigger, want_d)
        if ok:
            break
    if not ok:
        raise UploadError(f"Không đặt được ngày hẹn YouTube (đang là {shown}, cần {when:%d/%m/%Y}; đã thử {tried})")
    if page.locator("ytcp-date-picker input").first.is_visible():
        trigger.click()   # đóng bảng chọn ngày (không dùng Escape: có thể đóng cả hộp tải lên)
        page.wait_for_timeout(500)

    ti = page.locator("#time-of-day-container input").first
    got = None
    for text in _time_candidates(ti.input_value(), when):
        ti.click()
        ti.fill(text)
        page.keyboard.press("Enter")
        page.wait_for_timeout(1200)
        got = ti.input_value()
        if parse_time_any(got) == (when.hour, when.minute):
            break
    else:
        raise UploadError(f"Không đặt được giờ hẹn YouTube (đang là {got}, cần {when:%H:%M})")
    # ngày có thể bị đổi khi gõ giờ -> kiểm tra lại lần cuối
    ok, shown = _verified_date(page, trigger, want_d)
    if not ok:
        raise UploadError(f"Ngày hẹn YouTube bị đổi thành {shown}")
