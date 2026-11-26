import re

from .base import NotLoggedIn, UploadError, build_caption, rx, type_text, try_click, wait_enabled

NOT_NOW = rx("Not Now", "Not now", "Lúc khác", "Để sau")
NEXT = rx("Next", "Tiếp")
SHARE = rx("Share", "Chia sẻ")


def upload(page, job) -> str:
    page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    if "/accounts/login" in page.url or page.locator('input[name="username"]').count() > 0:
        raise NotLoggedIn("Instagram")

    # Bỏ qua popup "Bật thông báo" / "Lưu thông tin đăng nhập"
    for _ in range(2):
        try_click(page.get_by_role("button", name=NOT_NOW), timeout=2000)

    create = page.locator(
        'svg[aria-label="New post"], svg[aria-label="Bài viết mới"], '
        'svg[aria-label="Create"], svg[aria-label="Tạo"]'
    ).first
    create.click(timeout=30_000)
    page.wait_for_timeout(1500)
    # Menu con "Bài viết" (nếu có)
    try_click(page.get_by_role("link", name=rx("Post", "Bài viết")), timeout=2000) or \
        try_click(page.get_by_text(rx("Post", "Bài viết")), timeout=1000)

    file_input = page.locator('input[type="file"]').first
    file_input.wait_for(state="attached", timeout=30_000)
    file_input.set_input_files(job["video_path"])

    # "Bài viết video giờ sẽ được chia sẻ dưới dạng thước phim" -> OK
    try_click(page.get_by_role("button", name=rx("OK")), timeout=5000)

    dialog = page.get_by_role("dialog").last
    for _ in range(2):  # Cắt -> Chỉnh sửa -> Chú thích
        wait_enabled(dialog.get_by_role("button", name=NEXT), 300, "nút Tiếp của Instagram").click()
        page.wait_for_timeout(2500)

    caption = build_caption(job["title"], job["description"], job["hashtags"])
    box = page.locator('div[contenteditable="true"][role="textbox"], div[contenteditable="true"][aria-label]').first
    box.wait_for(state="visible", timeout=60_000)
    type_text(page, box, caption[:2200])

    wait_enabled(dialog.get_by_role("button", name=SHARE), 120, "nút Chia sẻ của Instagram").click()

    done = page.get_by_text(re.compile(r"has been shared|đã được chia sẻ|đã chia sẻ", re.I)).first
    try:
        done.wait_for(timeout=60 * 60 * 1000)
    except Exception:
        raise UploadError("Không xác nhận được Instagram đã đăng video")
    page.wait_for_timeout(2000)
    return "https://www.instagram.com/"
