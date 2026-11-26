"""Luồng nền: lấy các job đến giờ đăng và chạy lần lượt từng job."""
import logging
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

from . import db
from .browser import launch, mark_clones_stale, prepare_profile, profile_key, profile_lock, worker_key
from .config import PLATFORMS, SHOTS_DIR
from .platforms import COMMENTERS, UPLOADERS
from .platforms.base import ScheduleTooFar, UploadError

log = logging.getLogger("worker")
_stop = threading.Event()
_login_threads: dict[str, threading.Thread] = {}


def _due_jobs():
    return db.rows(
        "SELECT j.*, a.platform, a.name AS account_name, a.browser_group, a.target_name FROM jobs j JOIN accounts a ON a.id=j.account_id "
        "WHERE j.status='pending' AND COALESCE(j.upload_at, j.scheduled_at) <= ? "
        "ORDER BY COALESCE(j.upload_at, j.scheduled_at), j.id LIMIT 200",
        (db.now(),),
    )


MIN_LEAD = timedelta(minutes=20)   # hẹn trên nền tảng cần cách giờ đăng ít nhất chừng này


def _fmt(dt) -> str:
    return dt.strftime("%H:%M %d/%m")


def _due_comments():
    """Bài đã hẹn giờ trên nền tảng, đã tới giờ lên sóng, còn chờ gắn bình luận."""
    return db.rows(
        "SELECT j.*, a.platform, a.name AS account_name, a.browser_group, a.target_name FROM jobs j JOIN accounts a ON a.id=j.account_id "
        "WHERE j.status='success' AND j.note LIKE '⏳%' AND j.upload_at <= ? ORDER BY j.upload_at, j.id LIMIT 200",
        (db.now(),),
    )


def _key(job):
    return worker_key({"id": job["account_id"], "browser_group": job["browser_group"]}, job["platform"])


def run_comment(job):
    """Mở trình duyệt chỉ để gắn bình luận cho bài hẹn giờ đã lên sóng."""
    key = _key(job)
    lock = profile_lock(key)
    if not lock.acquire(blocking=False):
        return False
    try:
        if not prepare_profile(key):
            return False
        with sync_playwright() as pw:
            ctx = launch(pw, key, headless=db.get_setting("headless", "0") == "1")
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(60_000)
            try:
                if not _first_comment(page, job, None, final=job["cmt_tries"] >= 2):
                    nxt = datetime.now() + timedelta(minutes=30)
                    db.execute("UPDATE jobs SET cmt_tries=cmt_tries+1, upload_at=?, note=? WHERE id=?",
                               (nxt.isoformat(timespec="seconds"),
                                f"⏳ Chưa gắn được bình luận, sẽ thử lại lúc {_fmt(nxt)}", job["id"]))
            finally:
                ctx.close()
    except Exception as e:
        db.execute("UPDATE jobs SET note=? WHERE id=?", ("⚠ Chưa gắn được bình luận: " + str(e)[:500], job["id"]))
    finally:
        lock.release()
    return True


def _first_comment(page, job, url, final=True):
    """Bài đã đăng thành công -> gắn bình luận đầu tiên. Lỗi ở bước này KHÔNG làm job thất bại
    (tránh bấm 'Thử lại' rồi đăng trùng video), chỉ ghi chú lại."""
    text = (job.get("first_comment") or "").strip()
    fn = COMMENTERS.get(job["platform"])
    if not text or not fn:
        return True
    if not db.channel_comment_enabled(dict(job)):
        db.execute("UPDATE jobs SET note=? WHERE id=?", ("⏸ Bỏ qua bình luận: kênh đang tắt gắn bình luận", job["id"]))
        return True
    try:
        post_url = fn(page, job, url, text) or url
        db.execute("UPDATE jobs SET note=?, result_url=? WHERE id=?", ("💬 Đã gắn bình luận đầu tiên", post_url, job["id"]))
        log.info("Job #%s: đã gắn bình luận đầu tiên", job["id"])
        return True
    except Exception as e:
        msg = str(e) if isinstance(e, UploadError) else f"{type(e).__name__}: {e}"
        shot = f"job{job['id']}_cmt_{int(time.time())}.png"
        try:
            page.screenshot(path=str(SHOTS_DIR / shot), full_page=False)
            db.execute("UPDATE jobs SET screenshot=? WHERE id=?", (shot, job["id"]))
        except Exception:
            pass
        log.error("Job #%s: lỗi gắn bình luận: %s\n%s", job["id"], msg, traceback.format_exc())
        if not final:
            return False
        db.execute("UPDATE jobs SET note=? WHERE id=?", ("⚠ Chưa gắn được bình luận: " + msg[:500], job["id"]))
        return True


def run_job(job):
    key = _key(job)
    lock = profile_lock(key)
    if not lock.acquire(blocking=False):
        return False  # tài khoản đang mở để đăng nhập -> thử lại sau
    try:
        ready = prepare_profile(key)
    except Exception:
        lock.release()
        raise
    if not ready:   # chưa chép được profile (profile gốc đang mở) -> thử lại sau
        lock.release()
        return False
    try:
        db.execute("UPDATE jobs SET status='running', started_at=?, error=NULL, run_now=0 WHERE id=?", (db.now(), job["id"]))
        log.info("Job #%s: %s -> %s (%s)", job["id"], Path(job["video_path"]).name, job["platform"], job["account_name"])
        if not Path(job["video_path"]).is_file():
            raise UploadError(f"Không tìm thấy file video: {job['video_path']}")

        job = dict(job)
        job["schedule_at"] = None
        if job.get("publish_mode") == "platform":
            t = datetime.fromisoformat(job["scheduled_at"])
            if t - datetime.now() >= MIN_LEAD:   # còn đủ xa -> hẹn trên nền tảng; sát giờ rồi thì đăng luôn
                job["schedule_at"] = t

        headless = db.get_setting("headless", "0") == "1"
        with sync_playwright() as pw:
            ctx = launch(pw, key, headless=headless)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(60_000)
            try:
                url = UPLOADERS[job["platform"]](page, job)
                db.execute(
                    "UPDATE jobs SET status='success', result_url=?, finished_at=? WHERE id=?",
                    (url, db.now(), job["id"]),
                )
                log.info("Job #%s thành công: %s", job["id"], url)
                when = job["schedule_at"]
                if not when:
                    _first_comment(page, job, url)
                elif (job.get("first_comment") or "").strip() and job["platform"] in COMMENTERS:
                    db.execute("UPDATE jobs SET note=?, upload_at=?, cmt_tries=0 WHERE id=?",
                               (f"⏳ Đã hẹn trên nền tảng lúc {_fmt(when)} — sẽ gắn bình luận sau khi bài lên sóng",
                                (when + timedelta(minutes=3)).isoformat(timespec="seconds"), job["id"]))
                else:
                    db.execute("UPDATE jobs SET note=? WHERE id=?", (f"📅 Đã hẹn trên nền tảng lúc {_fmt(when)}", job["id"]))
            except ScheduleTooFar as e:
                # Nền tảng chưa cho hẹn xa như vậy -> để đến gần ngày đăng rồi tải lên lại
                t = job["schedule_at"]
                retry = max(datetime.now() + timedelta(hours=1), t - timedelta(days=3))
                if retry > t - MIN_LEAD:
                    retry = t - MIN_LEAD
                db.execute("UPDATE jobs SET status='pending', upload_at=?, note=?, started_at=NULL WHERE id=?",
                           (retry.isoformat(timespec="seconds"),
                            f"{e} — tool sẽ tải lên lúc {_fmt(retry)} (máy cần bật lúc đó)", job["id"]))
                log.info("Job #%s: %s -> tải lên lại lúc %s", job["id"], e, retry)
            except Exception as e:
                shot = f"job{job['id']}_{int(time.time())}.png"
                try:
                    page.screenshot(path=str(SHOTS_DIR / shot), full_page=False)
                except Exception:
                    shot = None
                msg = str(e) if isinstance(e, UploadError) else f"{type(e).__name__}: {e}"
                db.execute(
                    "UPDATE jobs SET status='failed', error=?, screenshot=?, finished_at=? WHERE id=?",
                    (msg[:2000], shot, db.now(), job["id"]),
                )
                log.error("Job #%s lỗi: %s\n%s", job["id"], msg, traceback.format_exc())
            finally:
                ctx.close()
    except Exception as e:
        db.execute(
            "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=?",
            (str(e)[:2000], db.now(), job["id"]),
        )
        log.error("Job #%s lỗi: %s", job["id"], e)
    finally:
        lock.release()
    return True


MAX_PARALLEL = 5   # số cửa sổ Chrome đăng cùng lúc (mỗi nền tảng của một nhóm chạy trên bản sao profile riêng)


def _loop():
    """Mỗi profile Chrome (nhóm) chạy một luồng riêng -> các nhóm khác nhau đăng song song,
    còn trong cùng một nhóm vẫn đăng lần lượt (một profile chỉ mở được một cửa sổ)."""
    skipped: dict[int, float] = {}
    busy: dict[str, threading.Thread] = {}

    def _run(key, fn, item, skip_id):
        try:
            if not fn(item):
                skipped[skip_id] = time.time() + 30
                if fn is run_job:   # profile đang bận (cửa sổ đăng nhập) -> trả job về hàng chờ
                    db.execute("UPDATE jobs SET status='pending' WHERE id=? AND status='running'", (item["id"],))
        except Exception:
            log.exception("Lỗi chạy job")

    while not _stop.is_set():
        try:
            for k in [k for k, t in busy.items() if not t.is_alive()]:
                del busy[k]
            started = False
            try:
                limit = max(1, int(db.get_setting("max_parallel", str(MAX_PARALLEL))))
            except ValueError:
                limit = MAX_PARALLEL
            due, comments = _due_jobs(), _due_comments()
            if db.get_setting("paused", "0") == "1":   # Tạm dừng -> chỉ chạy bài người dùng bấm "Chạy"
                due, comments = [j for j in due if j.get("run_now")], []
            for fn, items, sign in ((run_job, due, 1), (run_comment, comments, -1)):
                for item in items:
                    if len(busy) >= limit:
                        break
                    k = _key(item)
                    sid = sign * item["id"]
                    if k in busy or skipped.get(sid, 0) >= time.time():
                        continue
                    if fn is run_job:
                        # đánh dấu đang chạy ngay để vòng sau không lấy trùng
                        db.execute("UPDATE jobs SET status='running' WHERE id=? AND status='pending'", (item["id"],))
                    t = threading.Thread(target=_run, args=(k, fn, item, sid), name=f"job-{k}", daemon=True)
                    busy[k] = t
                    t.start()
                    started = True
            if started:
                _stop.wait(1)
                continue
        except Exception:
            log.exception("Lỗi vòng lặp worker")
        _stop.wait(1 if busy else 5)


def start():
    threading.Thread(target=_loop, name="upload-worker", daemon=True).start()


def stop():
    _stop.set()


def open_login(accounts: list[dict]):
    """Mở MỘT cửa sổ Chrome cho profile của các tài khoản (cùng nhóm), mỗi nền tảng một tab.
    Người dùng tự đăng nhập rồi đóng cửa sổ là xong."""
    key = profile_key(accounts[0])
    t = _login_threads.get(key)
    if t and t.is_alive():
        return "already_open"
    lock = profile_lock(key)
    if not lock.acquire(blocking=False):
        return "busy"

    def _run():
        try:
            with sync_playwright() as pw:
                ctx = launch(pw, key, headless=False)
                for i, acc in enumerate(accounts):
                    page = ctx.pages[0] if i == 0 and ctx.pages else ctx.new_page()
                    try:
                        page.goto(PLATFORMS[acc["platform"]]["login_url"])
                    except Exception:
                        log.warning("Không mở được trang đăng nhập %s", acc["platform"])
                if ctx.pages:
                    ctx.pages[0].bring_to_front()
                # Chờ đến khi người dùng đóng cửa sổ Chrome (không giới hạn thời gian)
                ctx.wait_for_event("close", timeout=0)
        except Exception:
            log.exception("Lỗi mở trình duyệt đăng nhập")
        finally:
            mark_clones_stale(key)   # vừa đăng nhập lại -> bản sao dùng để đăng song song phải chép lại
            lock.release()

    t = threading.Thread(target=_run, name=f"login-{key}", daemon=True)
    _login_threads[key] = t
    t.start()
    return "opened"
