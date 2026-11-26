"""Mở Chrome với profile riêng (giữ phiên đăng nhập).

- Tài khoản không có nhóm: mỗi tài khoản một profile (thư mục data/profiles/<id>).
- Tài khoản cùng "nhóm Chrome": dùng chung một profile -> một cửa sổ Chrome, mỗi nền tảng một tab.
"""
import re
import shutil
import threading
import unicodedata
from collections import defaultdict

from .config import PROFILES_DIR

# Một profile Chrome chỉ mở được một lần tại một thời điểm.
_profile_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)


def slugify(text: str) -> str:
    text = text.replace("Đ", "D").replace("đ", "d")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "nhom"


def profile_key(account: dict) -> str:
    group = (account.get("browser_group") or "").strip()
    return f"g_{slugify(group)}" if group else str(account["id"])


def profile_lock(key: str) -> threading.Lock:
    return _profile_locks[key]


def launch(pw, key: str, headless: bool = False):
    profile = PROFILES_DIR / key
    profile.mkdir(parents=True, exist_ok=True)
    opts = dict(
        user_data_dir=str(profile),
        headless=headless,
        no_viewport=True,
        chromium_sandbox=True,  # bỏ cờ --no-sandbox (Chrome hiện cảnh báo vàng khi có cờ này)
        locale="vi-VN",  # giao diện trang web bằng tiếng Việt
        args=[
            "--start-maximized",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=Translate,TranslateUI",
            "--disable-translate",
            "--lang=vi",
        ],
        ignore_default_args=["--enable-automation"],
    )
    try:
        # Ưu tiên Chrome thật đã cài trên máy (ít bị chặn đăng nhập hơn)
        return pw.chromium.launch_persistent_context(channel="chrome", **opts)
    except Exception:
        # Dự phòng: Chromium đi kèm Playwright (cần chạy: python -m playwright install chromium)
        return pw.chromium.launch_persistent_context(**opts)


# ---------------------------------------------------------------------------
# Đăng song song trong cùng một nhóm: mỗi nền tảng (trừ YouTube) chạy trên một BẢN SAO
# của profile nhóm (giữ nguyên cookie đăng nhập) -> YouTube, TikTok, Facebook... của cùng
# một kênh không phải xếp hàng chờ nhau nữa. YouTube giữ profile gốc vì Google có thể
# đăng xuất khi phiên đăng nhập bị chép sang profile khác.
CLONE_SKIP = shutil.ignore_patterns(
    "Cache", "Code Cache", "GPUCache", "GPUPersistentCache", "ShaderCache", "GrShaderCache", "DawnCache",
    "DawnGraphiteCache", "DawnWebGPUCache", "Service Worker", "CacheStorage", "Crashpad", "CrashpadMetrics*",
    "BrowserMetrics*", "component_crx_cache", "extensions_crx_cache", "OptGuidePredictionModels",
    "optimization_guide_model_store", "Safe Browsing", "segmentation_platform", "History*", "Favicons*",
    "Top Sites*", "*.indexeddb.blob", "Sessions", "Sessions_Encrypted", "Singleton*", "lockfile", "LOCK", "LOG", "LOG.old",
)
STALE = ".stale"


def worker_key(account: dict, platform: str) -> str:
    """Profile dùng để ĐĂNG bài cho tài khoản này."""
    key = profile_key(account)
    if key.startswith("g_") and platform != "youtube":
        return f"{key}__{platform}"
    return key


def source_key(key: str) -> str:
    return key.split("__", 1)[0]


def prepare_profile(key: str) -> bool:
    """Tạo/làm mới bản sao profile từ profile gốc của nhóm. False = profile gốc đang bận, thử lại sau."""
    if "__" not in key:
        return True
    dst = PROFILES_DIR / key
    if dst.is_dir() and not (dst / STALE).exists():
        return True
    src = PROFILES_DIR / source_key(key)
    if not src.is_dir():
        return True   # chưa đăng nhập lần nào -> để Chrome tạo profile trống, sẽ báo chưa đăng nhập
    lock = profile_lock(source_key(key))
    if not lock.acquire(blocking=False):   # Chrome đang mở profile gốc -> không chép được cookie
        return False
    try:
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst, ignore=CLONE_SKIP)
    finally:
        lock.release()
    return True


def mark_clones_stale(key: str):
    """Sau khi người dùng đăng nhập lại ở profile gốc -> các bản sao phải chép lại."""
    for d in PROFILES_DIR.glob(f"{key}__*"):
        try:
            (d / STALE).touch()
        except OSError:
            pass


def remove_clones(key: str):
    for d in PROFILES_DIR.glob(f"{key}__*"):
        shutil.rmtree(d, ignore_errors=True)
