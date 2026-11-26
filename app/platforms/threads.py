import re

from .base import (already_commented, NotLoggedIn, UploadError, build_caption, caption_snippet, find_new_post, normalize_hashtags,
                   page_has_text, pick_meta_day, rx, try_click, type_text, wait_enabled)

POST = rx("Post", "Đăng", "Schedule", "Lên lịch")


def upload(page, job) -> str:
    """Đăng bài Threads kèm video (đăng nhập Threads bằng tài khoản Instagram)."""
    page.goto("https://www.threads.com/", wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    if "/login" in page.url:
        raise NotLoggedIn("Threads")

    # Ô "Có gì mới?" ở đầu bảng feed (giao diện mới) hoặc nút "Tạo" (giao diện cũ)
    create = page.locator(
        'div[role="button"][aria-label*="soạn bài viết mới"], div[role="button"][aria-label*="compose a new" i], '
        'svg[aria-label="Create"], svg[aria-label="Tạo"]'
    ).first
    try:
        create.wait_for(state="visible", timeout=20_000)
    except Exception:
        raise NotLoggedIn("Threads")
    create.click()

    dialog = page.get_by_role("dialog").last
    box = dialog.locator('div[contenteditable="true"]').first
    box.wait_for(state="visible", timeout=30_000)
    # Threads chỉ cho 1 thẻ chủ đề mỗi bài -> chỉ dùng hashtag đầu tiên, các hashtag sau sẽ chỉ là chữ thường
    first_tag = (normalize_hashtags(job["hashtags"]).split() or [""])[0]
    caption = build_caption(job["title"], job["description"], first_tag)
    if len(caption) > 500:  # giới hạn ký tự của Threads
        caption = caption[:497] + "..."
    type_text(page, box, caption)

    dialog.locator('input[type="file"]').first.set_input_files(job["video_path"])
    page.wait_for_timeout(3000)

    when = job.get("schedule_at")
    if when:
        _set_schedule(page, dialog, when)

    post = dialog.get_by_role("button", name=POST)
    wait_enabled(post, 60 * 60, "Threads tải video lên").click()
    if when:   # bài hẹn giờ: không có link "Xem" -> chờ hộp soạn bài đóng là xong
        try:
            dialog.wait_for(state="detached", timeout=10 * 60 * 1000)
        except Exception:
            raise UploadError("Không xác nhận được Threads đã lên lịch bài")
        return "https://www.threads.com/"

    # Sau khi đăng: hộp thoại đóng và hiện thông báo "Đã đăng" kèm liên kết "Xem"
    url = None
    try:
        view = page.get_by_role("link", name=rx("View", "Xem")).first
        view.wait_for(state="visible", timeout=10 * 60 * 1000)
        href = view.get_attribute("href") or ""
        url = href if href.startswith("http") else "https://www.threads.com" + href
    except Exception:
        try:
            page.get_by_text(re.compile(r"^(Posted|Đã đăng)$", re.I)).first.wait_for(timeout=30_000)
        except Exception:
            raise UploadError("Không xác nhận được Threads đã đăng video")
    try_click(page.get_by_role("button", name=rx("Close", "Đóng")), timeout=2000)
    return url or "https://www.threads.com/"


def comment(page, job, url: str, text: str) -> str:
    """Trả lời ngay dưới bài vừa đăng (bình luận đầu tiên). Trả về link bài."""
    snippet = caption_snippet(job["title"])
    if url and "/post/" in url:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        post_url = page.url
    else:   # không có link -> tìm bài mới nhất trên trang cá nhân
        page.goto("https://www.threads.com/", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        prof = page.locator('a[href^="/@"]:has(svg[aria-label="Trang cá nhân"]), a[href^="/@"]:has(svg[aria-label="Profile"])').first
        href = prof.get_attribute("href", timeout=20_000)
        post_url = find_new_post(page, "https://www.threads.com" + href, 'a[href*="/post/"]', snippet, minutes=5)

    # Giao diện mới: có sẵn ô "Trả lời <tên>..." ngay dưới bài -> gõ thẳng vào đó.
    # Giao diện cũ: bấm biểu tượng Trả lời để mở hộp thoại.
    inline = page.locator('div[contenteditable="true"]')
    if not any(inline.nth(i).is_visible() for i in range(inline.count())):
        reply = page.locator('svg[aria-label="Trả lời"], svg[aria-label="Reply"], '
                             'svg[aria-label="Bình luận"], svg[aria-label="Comment"]').first
        reply.click(timeout=30_000)
        page.wait_for_timeout(1500)
    dialogs = page.get_by_role("dialog")
    scope = dialogs.last if dialogs.count() and dialogs.last.locator('div[contenteditable="true"]').count() else page
    box = wait_enabled(scope.locator('div[contenteditable="true"]'), 30, "ô trả lời Threads")
    if already_commented(page, text):   # đã có bình luận này rồi -> không gắn trùng
        return post_url
    type_text(page, box, text[:500])
    page.wait_for_timeout(1500)
    wait_enabled(scope.get_by_role("button", name=POST), 60, "nút Đăng trả lời Threads").click()
    page.wait_for_timeout(5000)
    if scope is not page:
        try:
            scope.wait_for(state="detached", timeout=60_000)
        except Exception:
            raise UploadError("Không xác nhận được đã đăng bình luận trên Threads")
    elif not page_has_text(page, caption_snippet(text)):
        raise UploadError("Đã gõ bình luận nhưng không thấy hiện lên trên Threads")
    return post_url



def _set_schedule(page, dialog, when):
    """Nút '...' của hộp soạn bài -> 'Lên lịch...' -> chọn ngày, giờ, phút -> Xong."""
    dialog.locator('[aria-label="Xem thêm"], [aria-label="More"]').first.click()
    page.wait_for_timeout(1200)
    page.get_by_role("menuitem", name=re.compile(r"^(Lên lịch|Schedule)", re.I)).first.click()
    page.wait_for_timeout(1500)
    pick_meta_day(page, when, re.compile(r"^(Tháng sau|Next month)$", re.I), "Threads")
    page.wait_for_timeout(500)
    page.locator('input[placeholder="hh"]').fill(f"{when.hour:02d}")
    page.locator('input[placeholder="mm"]').fill(f"{when.minute:02d}")
    page.wait_for_timeout(500)
    page.get_by_role("button", name=re.compile(r"^(Xong|Done)$", re.I)).first.click()
    page.wait_for_timeout(1500)
    hhmm = f"{when.hour:02d}:{when.minute:02d}"
    if dialog.get_by_text(re.compile(rf"{hhmm}")).count() == 0:
        raise UploadError(f"Không đặt được giờ hẹn Threads ({hhmm})")
