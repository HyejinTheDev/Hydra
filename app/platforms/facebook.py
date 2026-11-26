import re

from .base import (NotLoggedIn, UploadError, build_caption, caption_snippet, already_commented, find_new_post, page_has_text, parse_date_any,
                   parse_time_any, pick_meta_day, rx,
                   type_text, try_click, wait_enabled)

NEXT = rx("Next", "Tiếp", "Tiếp tục")
PUBLISH = rx("Publish", "Đăng", "Chia sẻ", "Post", "Schedule", "Lên lịch")


ACCOUNT_MENU = (
    'div[role="button"][aria-label*="trang cá nhân của bạn" i], div[role="button"][aria-label*="your profile" i], '
    'div[role="button"][aria-label^="Tài khoản"], div[role="button"][aria-label^="Account"]'
)


def _active_page_id(page) -> str | None:
    for c in page.context.cookies("https://www.facebook.com"):
        if c["name"] == "i_user":
            return c["value"]
    return None


def switch_to_page(page, page_name: str):
    """Chuyển Facebook sang dùng với tư cách Trang `page_name` (qua menu ảnh đại diện góc phải)."""
    page.goto("https://www.facebook.com/", wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    if "login" in page.url or page.locator('input[name="email"]').count() > 0:
        raise NotLoggedIn("Facebook")
    before = _active_page_id(page)

    page.locator(ACCOUNT_MENU).first.click(timeout=20_000)
    page.wait_for_timeout(1500)
    target = page.get_by_role("dialog").get_by_text(page_name, exact=True)
    if target.count() == 0:
        target = page.get_by_text(page_name, exact=True)
    if target.count() == 0:
        raise UploadError(f"Không thấy Trang '{page_name}' trong menu tài khoản Facebook")
    target.first.click()
    page.wait_for_timeout(6000)
    # Có thể hiện hộp thoại xác nhận chuyển trang cá nhân
    try_click(page.get_by_role("button", name=rx("Switch", "Chuyển", "Chuyển ngay")), timeout=2000)
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(3000)

    now = _active_page_id(page)
    if not now:
        raise UploadError(
            f"Chưa chuyển được sang Trang '{page_name}'. Hãy kiểm tra tên Trang trong tab Tài khoản "
            "(phải ghi đúng như hiển thị trên Facebook)."
        )
    return before, now


def upload(page, job) -> str:
    """Đăng Reels. Nếu tài khoản có 'Tên Trang Facebook' thì tự chuyển sang Trang đó trước khi đăng."""
    page_name = (job.get("target_name") or "").strip()
    if page_name and not _active_page_id(page):
        switch_to_page(page, page_name)

    page.goto("https://www.facebook.com/reels/create/", wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    if "login" in page.url or page.locator('input[name="email"]').count() > 0:
        raise NotLoggedIn("Facebook")

    file_input = page.locator('input[type="file"][accept*="video"], input[type="file"]').first
    file_input.wait_for(state="attached", timeout=60_000)
    file_input.set_input_files(job["video_path"])

    # Facebook hay đổi số bước (Tải lên -> Chỉnh sửa -> Mô tả / Cài đặt...):
    # bấm Tiếp cho tới khi thấy ô Mô tả, gõ chú thích, rồi bấm Tiếp tiếp cho tới bước có nút Đăng.
    for _ in range(4):
        if _any_visible(page.locator(CAPTION_BOX)):
            break
        btn = page.get_by_role("button", name=NEXT)
        wait_enabled(btn, 600, "nút Tiếp của Facebook").click()
        page.wait_for_timeout(2500)

    caption = build_caption(job["title"], job["description"], job["hashtags"])
    _fill_caption(page, caption)

    for _ in range(3):
        if _any_visible(page.get_by_role("button", name=PUBLISH)) or _any_visible(SCHEDULE_BTN(page)):
            break
        nxt = page.get_by_role("button", name=NEXT)
        if not _any_visible(nxt):
            break
        wait_enabled(nxt, 600, "nút Tiếp của Facebook").click()
        page.wait_for_timeout(2500)

    if job.get("schedule_at"):
        # Bài hẹn giờ: TUYỆT ĐỐI không bấm "Đăng" (sẽ đăng ngay). Chỉ dùng nút Lên lịch.
        _set_schedule(page, job["schedule_at"])
        if not _left_composer(page, 5_000):
            # Nút cuối ở chân trang: "Lên lịch đăng bài" (bên cạnh là '27 Tháng 11 lúc 16:00')
            _click(wait_enabled(page.get_by_role("button", name=FINAL_SCHEDULE), 120, "nút Lên lịch đăng bài của Facebook"))
        _skip_upsell(page)
    else:
        publish = page.get_by_role("button", name=PUBLISH)
        wait_enabled(publish, 60 * 60, "Facebook xử lý video").click()
        _skip_upsell(page)

    try:
        page.wait_for_url(lambda u: "/reels/create" not in u, timeout=300_000)
    except Exception:
        done = page.get_by_text(re.compile(r"published|đã đăng|đã chia sẻ|reel của bạn", re.I)).first
        try:
            done.wait_for(timeout=60_000)
        except Exception:
            raise UploadError("Không xác nhận được Facebook đã đăng video")
    try_click(page.get_by_role("button", name=rx("Close", "Đóng")), timeout=3000)
    return page.url


CAPTION_BOX = ('div[role="textbox"][contenteditable="true"][aria-label*="mô tả" i], '
               'div[role="textbox"][contenteditable="true"][aria-label*="describe" i], '
               'div[role="textbox"][contenteditable="true"][aria-label*="description" i]')


def SCHEDULE_BTN(page):
    """Nút mở hẹn giờ: giao diện cũ 'Tùy chọn lên lịch', giao diện mới là nút 'Lên lịch' cạnh nút 'Đăng'."""
    return page.get_by_role("button", name=re.compile(
        r"^(Scheduling options|Tùy chọn lên lịch|Tùy chọn lịch)|^\s*(Lên lịch|Schedule)\s*$", re.I))


UPSELL_SKIP = re.compile(r"^\s*(Đăng bài viết gốc|Đăng bài gốc|Post original|Publish original|Post without)\b.*$", re.I)


def _skip_upsell(page, seconds: int = 20):
    """Sau khi bấm Lên lịch/Đăng, Facebook đôi khi hiện bảng mời quảng cáo ('Bạn đang tổ chức sự kiện?'
    -> Tiếp tục / Đăng bài viết gốc). Luôn chọn 'Đăng bài viết gốc', KHÔNG bấm 'Tiếp tục' (sang quảng cáo)."""
    for _ in range(seconds):
        if "/reels/create" not in page.url:
            return
        for loc in (page.get_by_role("button", name=UPSELL_SKIP), page.get_by_text(UPSELL_SKIP)):
            try:
                for i in range(loc.count()):
                    if loc.nth(i).is_visible():
                        _click(loc.nth(i))
                        page.wait_for_timeout(2000)
                        return
            except Exception:
                pass
        page.wait_for_timeout(1000)


def _left_composer(page, timeout_ms) -> bool:
    """Đã rời trang tạo Reels (tức là đã đăng/hẹn xong) chưa."""
    try:
        page.wait_for_url(lambda u: "/reels/create" not in u, timeout=timeout_ms)
        return True
    except Exception:
        return False


def _any_visible(locator) -> bool:
    try:
        return any(locator.nth(i).is_visible() for i in range(locator.count()))
    except Exception:
        return False


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def _fill_caption(page, caption: str):
    """Gõ chú thích vào ô Mô tả của Reels rồi KIỂM TRA lại; không vào được thì báo lỗi thay vì đăng trống."""
    if not caption.strip():
        return
    box = page.locator(CAPTION_BOX)
    if box.count() == 0:   # giao diện khác nhãn -> lấy ô soạn thảo hiển thị cuối cùng (ô của hộp đăng bài)
        box = page.locator('div[role="textbox"][contenteditable="true"]')
    box = wait_enabled(box, 60, "ô Mô tả của Facebook")
    want = _norm(caption)[:20]
    for attempt in range(3):
        if attempt < 2:
            type_text(page, box, caption)
        else:   # cách cuối: gõ từng phím (chậm hơn nhưng trình soạn thảo của Facebook luôn nhận)
            box.click()
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
            for i, line in enumerate(caption.split("\n")):
                if i:
                    page.keyboard.press("Shift+Enter")
                page.keyboard.type(line, delay=15)
        page.wait_for_timeout(1500)
        if want in _norm(box.inner_text()):
            break
    else:
        raise UploadError("Không gõ được chú thích vào ô Mô tả của Facebook")
    # Đưa con trỏ về cuối để đóng gợi ý hashtag (không dùng Escape vì có thể đóng/huỷ hộp đăng bài)
    try:
        box.press("End")
    except Exception:
        pass


COMMENT_BOX = ('div[role="textbox"][contenteditable="true"][aria-label*="bình luận" i], '
               'div[role="textbox"][contenteditable="true"][aria-label*="comment" i]')


def comment(page, job, url: str, text: str) -> str:
    """Tìm Reel vừa đăng trên Trang rồi gắn bình luận đầu tiên. Trả về link Reel."""
    # Đoạn tiêu đề DÀI để không nhầm các bài có đầu tiêu đề giống nhau ("Đạo cụ hôm nay: Bùa Dê / Bùa Rồng")
    snippet = re.sub(r"\s+", " ", job["title"] or "").strip()[:45].strip()
    owner = _active_page_id(page) or next(
        (c["value"] for c in page.context.cookies("https://www.facebook.com") if c["name"] == "c_user"), None)
    if not owner:
        raise UploadError("Không xác định được Trang Facebook để tìm Reel vừa đăng")
    reel = find_new_post(page, f"https://www.facebook.com/profile.php?id={owner}&sk=reels_tab",
                         'a[href*="/reel/"]', snippet, minutes=15, check=12)

    box = page.locator(COMMENT_BOX)
    if box.count() == 0 or not box.first.is_visible():   # trình xem Reel: phải bấm nút Bình luận để mở khung
        try_click(page.locator('div[role="button"][aria-label="Bình luận"], div[role="button"][aria-label="Comment"]'), 5000)
        page.wait_for_timeout(2000)
    box = wait_enabled(page.locator(COMMENT_BOX), 60, "ô bình luận Facebook")
    if already_commented(page, text):   # đã có bình luận này rồi -> không gắn trùng
        return reel
    type_text(page, box, text)
    page.keyboard.press("Enter")
    page.wait_for_timeout(5000)
    if not page_has_text(page, caption_snippet(text)):
        raise UploadError("Đã gõ bình luận nhưng không thấy hiện lên trên Facebook")
    return reel



FIELD_LABEL = {"date": re.compile(r"^(Ngày|Date)$", re.I), "time": re.compile(r"^(Thời gian|Giờ|Time)$", re.I)}
FINAL_SCHEDULE = re.compile(r"^\s*(Lên lịch đăng bài|Lên lịch bài viết|Lên lịch thước phim|Schedule post|Schedule reel|Schedule)\s*$", re.I)


def _click(loc, timeout=5000):
    """Bấm; nếu bị lớp phủ của Facebook che (intercepts pointer events) thì bấm cưỡng bức."""
    try:
        loc.click(timeout=timeout)
    except Exception:
        loc.click(force=True, timeout=timeout)


def _field(page, which):
    """Ô Ngày / Thời gian của hộp 'Lựa chọn lịch đăng'. Ô này KHÔNG nằm trong phần tử role=dialog
    (Facebook vẽ nội dung hộp thoại ở chỗ khác) -> tìm theo nhãn trên toàn trang."""
    lab = page.locator("label").filter(has=page.get_by_text(FIELD_LABEL[which]))
    for i in range(lab.count() - 1, -1, -1):
        el = lab.nth(i)
        try:
            if el.is_visible() and el.locator("input").count():
                return el, el.locator("input").first
        except Exception:
            pass
    return None, None


def _set_schedule(page, when):
    """Mở 'Lên lịch' -> chọn 'Lên lịch đăng sau' -> chọn ngày, gõ giờ -> 'Xong' (giao diện mới)
    hoặc 'Lên lịch đăng sau' (giao diện cũ). Hàm KHÔNG bấm nút cuối 'Lên lịch đăng bài'."""
    wait_enabled(SCHEDULE_BTN(page), 60, "nút Lên lịch của Facebook").click()
    page.wait_for_timeout(2000)

    # Giao diện mới có 2 lựa chọn: Đăng ngay / Lên lịch đăng sau -> luôn chọn Lên lịch
    later = page.locator('input[type="radio"][value="SCHEDULE_FOR_LATER"]')
    if later.count() and not later.first.is_checked():
        _click(page.locator("label").filter(has=later).first)
        page.wait_for_timeout(1000)
    if page.locator('input[type="radio"][value="PUBLISH_NOW"]:checked').count():
        raise UploadError("Facebook vẫn đang chọn 'Đăng ngay' - dừng lại để không đăng sai giờ")

    date_lab, date_in = _field(page, "date")
    time_lab, time_in = _field(page, "time")
    if date_in is None or time_in is None:   # giao diện cũ: 2 ô text dưới tiêu đề "Chọn ngày và giờ"
        head = page.get_by_text(re.compile(r"Choose a date and time|Chọn ngày và giờ", re.I))
        if head.count():
            ins = head.first.locator("xpath=../..").locator('input[type="text"]')
            if ins.count() >= 2:
                date_lab, date_in, time_lab, time_in = ins.first, ins.first, ins.nth(1), ins.nth(1)
    if date_in is None or time_in is None:
        raise UploadError("Không thấy ô chọn ngày/giờ hẹn của Facebook")

    # Ngày: bấm vào nhãn (ô input bị lớp phủ che) -> lịch hiện ra -> chọn ô ngày
    _click(date_lab)
    page.wait_for_timeout(1200)
    pick_meta_day(page, when, re.compile(r"^(Next Month|Tháng sau)$", re.I), "Facebook")
    page.wait_for_timeout(800)
    got = parse_date_any(date_in.input_value())
    if got != (when.year, when.month, when.day):
        raise UploadError(f"Không đặt được ngày hẹn Facebook (đang là {date_in.input_value()}, cần {when:%d/%m/%Y})")

    # Giờ
    hhmm = f"{when.hour:02d}:{when.minute:02d}"
    _click(time_in)
    page.keyboard.press("Control+A")
    page.keyboard.type(hhmm)
    page.wait_for_timeout(800)
    page.keyboard.press("Tab")
    page.wait_for_timeout(800)
    if parse_time_any(time_in.input_value()) != (when.hour, when.minute):
        raise UploadError(f"Không đặt được giờ hẹn Facebook (đang là {time_in.input_value()}, cần {hhmm})")

    # Xác nhận trong hộp thoại: mới = "Xong", cũ = "Lên lịch đăng sau".
    # KHÔNG bấm nút "Lên lịch" trơn ở chân trang (bị hộp thoại che).
    btn = page.get_by_role("button", name=re.compile(
        r"^(Xong|Done|Schedule for later|Lên lịch (đăng )?(cho )?sau)$", re.I))
    _click(wait_enabled(btn, 30, "nút Xong của hộp hẹn giờ Facebook"))
    page.wait_for_timeout(2000)
