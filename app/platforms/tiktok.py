import re

from .base import NotLoggedIn, ScheduleTooFar, UploadError, build_caption, rx, type_text, try_click, wait_enabled


def upload(page, job) -> str:
    page.goto("https://www.tiktok.com/tiktokstudio/upload?from=upload", wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    if "/login" in page.url:
        raise NotLoggedIn("TikTok")

    file_input = page.locator('input[type="file"][accept*="video"], input[type="file"]').first
    file_input.wait_for(state="attached", timeout=60_000)
    file_input.set_input_files(job["video_path"])

    editor = page.locator('div.public-DraftEditor-content[contenteditable="true"], div[contenteditable="true"]').first
    editor.wait_for(state="visible", timeout=180_000)
    page.wait_for_timeout(2000)
    _close_tours(page)
    caption = build_caption(job["title"], job["description"], job["hashtags"])
    for attempt in range(3):
        try:
            type_text(page, editor, caption[:2200])
            break
        except Exception:
            if attempt == 2:
                raise
            # thường do một bảng hỏi bật lên che ô mô tả -> tắt rồi thử lại
            page.wait_for_timeout(2000)
            _close_tours(page)
    page.keyboard.press("Escape")  # đóng gợi ý hashtag/mention nếu có

    when = job.get("schedule_at")
    if when:
        _close_tours(page)
        _set_schedule(page, when)

    post = page.locator('button[data-e2e="post_video_button"]')
    if post.count() == 0:
        post = page.get_by_role("button", name=rx("Post", "Đăng"))
    wait_enabled(post, 60 * 60, "TikTok tải video lên")
    _close_tours(page)
    post.first.click()

    # TikTok có thể hỏi "Tiếp tục đăng?" khi còn đang kiểm tra nội dung -> bấm nút xác nhận
    # (nút luôn ghi "Đăng ngay"; ở chế độ hẹn giờ nó vẫn giữ giờ hẹn đã chọn)
    page.wait_for_timeout(3000)
    try_click(page.get_by_role("button", name=rx("Post now", "Đăng ngay")), timeout=5000)

    try:
        page.wait_for_url(re.compile(r"tiktokstudio/content|/manage"), timeout=180_000)
    except Exception:
        ok = page.get_by_text(re.compile(r"uploaded|scheduled|đã được tải lên|đã đăng|đã lên lịch", re.I)).first
        try:
            ok.wait_for(timeout=30_000)
        except Exception:
            raise UploadError("Không xác nhận được TikTok đã đăng video")
    return page.url


def _set_schedule(page, when):
    """Chọn 'Lên lịch' ở mục Thời điểm đăng rồi đặt giờ (bước 5 phút) và ngày."""
    box = page.locator('[data-e2e="schedule_container"]')
    box.scroll_into_view_if_needed()
    box.locator('label:has(input[value="schedule"])').click(force=True)   # có lớp hướng dẫn che -> force
    page.wait_for_timeout(1500)
    # Lần đầu có thể hỏi cho phép lên lịch
    try_click(page.get_by_role("button", name=rx("Allow", "Cho phép", "Tiếp tục", "Continue")), timeout=1500)
    hh, mm = f"{when.hour:02d}", f"{when.minute - when.minute % 5:02d}"
    new_ui = page.locator('[role="combobox"][aria-label="Select date"]')
    try:
        new_ui.first.wait_for(state="visible", timeout=8000)
    except Exception:
        pass
    if new_ui.count():
        return _set_schedule_v2(page, when, hh, mm)
    inputs = box.locator('input[type="text"]')
    inputs.first.wait_for(state="visible", timeout=15_000)

    # Ngày trước (đổi ngày có thể làm giờ bị đặt lại)
    inputs.nth(1).click(force=True)
    page.wait_for_timeout(800)
    cal = page.locator(".calendar-wrapper").first
    cur = _shown_month(page)
    target = (when.year, when.month)
    for _ in range(14):
        if cur == target:
            break
        fwd = cur < target
        cal.locator(".month-header-wrapper .arrow").nth(1 if fwd else 0).click()
        page.wait_for_timeout(500)
        y, m = cur
        m += 1 if fwd else -1
        cur = (y + (m - 1) // 12, (m - 1) % 12 + 1)
    days = cal.locator("span.day", has_text=re.compile(rf"^{when.day}$"))
    n = days.count()
    if n == 0:
        raise ScheduleTooFar(f"TikTok chưa cho hẹn tới ngày {when:%d/%m/%Y}")
    day = days.first if when.day < 15 or n == 1 else days.last   # tránh trùng số ngày của tháng kế bên
    if "valid" not in (day.get_attribute("class") or ""):
        raise ScheduleTooFar(f"TikTok chưa cho hẹn tới ngày {when:%d/%m/%Y}")
    day.click()
    page.wait_for_timeout(800)

    # Giờ
    inputs.nth(0).click(force=True)
    page.wait_for_timeout(800)
    picker = page.locator(".tiktok-timepicker-time-picker-container").first
    for side, val in (("left", hh), ("right", mm)):
        opt = picker.locator(f".tiktok-timepicker-option-text.tiktok-timepicker-{side}", has_text=re.compile(rf"^{val}$")).first
        opt.scroll_into_view_if_needed()
        opt.click()
        page.wait_for_timeout(400)
    page.keyboard.press("Escape")
    try_click(box.locator(".scheduled-title-container"), timeout=2000)   # bấm ra ngoài để đóng bộ chọn giờ
    page.wait_for_timeout(800)

    got_t, got_d = inputs.nth(0).input_value(), inputs.nth(1).input_value()
    if got_t != f"{hh}:{mm}" or got_d != when.strftime("%Y-%m-%d"):
        raise UploadError(f"Không đặt được giờ hẹn TikTok (đang là {got_d} {got_t}, cần {when:%Y-%m-%d} {hh}:{mm})")


def _shown_month(page):
    """Tháng đang hiện trên lịch TikTok = tháng của ngày đang chọn trong ô ngày."""
    v = page.locator('[data-e2e="schedule_container"] input[type="text"]').nth(1).input_value()  # YYYY-MM-DD
    y, m = int(v[:4]), int(v[5:7])
    return (y, m)


CONTENT_CHECK_RX = re.compile(r"kiểm tra nội dung tự động|automatic content check|turn on automatic", re.I)


def _close_popups(page):
    """Bảng 'Bật kiểm tra nội dung tự động?' (Kiểm tra bản quyền nhạc / nội dung nhanh) hiện ra sau khi
    tải video và che ô mô tả -> bấm 'Bật' (giống mặc định của TikTok, lần sau không hỏi lại)."""
    try:
        if not any(t.is_visible() for t in page.get_by_text(CONTENT_CHECK_RX).all()):
            return
        btn = page.get_by_role("button", name=rx("Bật", "Turn on"))
        for i in range(btn.count() - 1, -1, -1):
            if btn.nth(i).is_visible():
                btn.nth(i).click(timeout=5000)
                page.wait_for_timeout(1000)
                return
    except Exception:
        pass


def _close_tours(page):
    """Tắt các bảng hướng dẫn ('Đã hiểu', ...) và bảng hỏi của TikTok - chúng che mất nút bấm."""
    _close_popups(page)
    for _ in range(4):
        btn = page.locator("#react-joyride-portal button, .react-joyride__tooltip button").filter(
            has_text=rx("Đã hiểu", "Got it", "OK", "Bỏ qua", "Skip", "Xong", "Done"))
        if btn.count() == 0:
            btn = page.locator('#react-joyride-portal button[aria-label="Close"], .react-joyride__tooltip button[title="Close"]')
        if btn.count() == 0:
            return
        try:
            btn.first.click(force=True, timeout=3000)
        except Exception:
            return
        page.wait_for_timeout(700)


MON_EN = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]


def _set_schedule_v2(page, when, hh, mm):
    """Giao diện mới: ô ngày ('22 tháng 11, 2026'), ô giờ, ô múi giờ - đều là combobox."""
    d, m, y = when.day, when.month, when.year
    day_rx = re.compile(rf"(ngày {d} tháng {m} năm {y})|({MON_EN[m - 1]} {d},? {y})|({d} {MON_EN[m - 1]},? {y})", re.I)
    date_tr = page.locator('[role="combobox"][aria-label="Select date"]').first
    time_tr = page.locator('[role="combobox"][aria-label="Select time"]').first

    # Ngày
    date_tr.click(force=True)
    panel = page.locator(".DatePicker__panel").first
    panel.wait_for(state="visible", timeout=10_000)
    for _ in range(14):
        day = [b for b in panel.locator("button.DatePicker__day").all() if day_rx.search(b.get_attribute("aria-label") or "")]
        if day:
            break
        nxt = panel.locator('button[aria-label="Next month"], button[aria-label="Tháng sau"]')
        if nxt.count() == 0 or nxt.first.is_disabled():
            raise ScheduleTooFar(f"TikTok chưa cho hẹn tới ngày {when:%d/%m/%Y}")
        nxt.first.click()
        page.wait_for_timeout(500)
    else:
        raise ScheduleTooFar(f"TikTok chưa cho hẹn tới ngày {when:%d/%m/%Y}")
    b = day[0]
    if b.get_attribute("data-disabled") == "true" or b.is_disabled():
        raise ScheduleTooFar(f"TikTok chưa cho hẹn tới ngày {when:%d/%m/%Y}")
    b.click()
    page.wait_for_timeout(800)
    if panel.is_visible():
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)

    # Giờ
    time_tr.click(force=True)
    tp = page.locator(".TimePicker__panel").first
    tp.wait_for(state="visible", timeout=10_000)
    for label in (f"Hours {hh}", f"Minutes {mm}"):
        opt = tp.locator(f'[role="option"][aria-label="{label}"]').first
        opt.scroll_into_view_if_needed()
        opt.click()
        page.wait_for_timeout(400)
    page.keyboard.press("Escape")
    page.wait_for_timeout(600)
    if tp.is_visible():
        time_tr.click(force=True)
        page.wait_for_timeout(400)

    got_d = date_tr.locator(".Picker__value").inner_text().strip()
    got_t = time_tr.locator(".Picker__value").inner_text().strip()
    ok_d = re.search(rf"(^|\D){d}\D", got_d) and str(y) in got_d and (f"tháng {m}," in got_d or MON_EN[m - 1][:3].lower() in got_d.lower())
    if got_t != f"{hh}:{mm}" or not ok_d:
        raise UploadError(f"Không đặt được giờ hẹn TikTok (đang là {got_d} {got_t}, cần {when:%d/%m/%Y} {hh}:{mm})")
