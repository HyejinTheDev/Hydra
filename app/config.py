from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PROFILES_DIR = DATA_DIR / "profiles"        # Chrome profile riêng cho từng tài khoản
VIDEOS_DIR = DATA_DIR / "videos"            # video tải lên qua giao diện web
SHOTS_DIR = DATA_DIR / "screenshots"        # ảnh chụp màn hình khi lỗi
DB_PATH = DATA_DIR / "app.db"

for d in (DATA_DIR, PROFILES_DIR, VIDEOS_DIR, SHOTS_DIR):
    d.mkdir(parents=True, exist_ok=True)

PLATFORMS = {
    "youtube": {"label": "YouTube", "login_url": "https://studio.youtube.com/"},
    "tiktok": {"label": "TikTok", "login_url": "https://www.tiktok.com/login"},
    "facebook": {"label": "Facebook", "login_url": "https://www.facebook.com/"},
    "instagram": {"label": "Instagram", "login_url": "https://www.instagram.com/accounts/login/"},
    "threads": {"label": "Threads", "login_url": "https://www.threads.com/login"},
}

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}
