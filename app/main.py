import json
import os
import logging
import re
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import db, douyin, gemini, worker
from .browser import profile_key, remove_clones
from .config import PLATFORMS, PROFILES_DIR, SHOTS_DIR, VIDEO_EXTS, VIDEOS_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="Đăng video đa nền tảng")
STATIC = Path(__file__).parent / "static"


@app.on_event("startup")
def _startup():
    db.init()
    gemini.init()
    douyin.init()
    if os.environ.get("NO_WORKER") != "1":   # dùng khi chạy thử, không tự đăng video
        worker.start()
        gemini.start()


@app.on_event("shutdown")
def _shutdown():
    worker.stop()
    gemini.stop()


@app.get("/")
def index():
    # không cho trình duyệt giữ bản cũ -> sửa giao diện xong chỉ cần F5
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


# ---------------- Tài khoản ----------------
class AccountIn(BaseModel):
    platform: str
    name: str
    browser_group: str = ""
    target_name: str = ""


class AccountGroupIn(BaseModel):
    browser_group: str = ""


class AccountTargetIn(BaseModel):
    target_name: str = ""


@app.get("/api/version")
def version():
    """Tính năng máy chủ đang hỗ trợ (giao diện kiểm tra trước khi dùng)."""
    return {"features": ["per_video_time", "ocr", "prefs", "presets", "first_comment", "platform_schedule", "channel_stock", "douyin"]}


@app.get("/api/platforms")
def platforms():
    return {k: v["label"] for k, v in PLATFORMS.items()}


@app.get("/api/accounts")
def list_accounts():
    accs = db.rows(
        "SELECT a.*, "
        " (SELECT COUNT(*) FROM jobs j WHERE j.account_id=a.id AND j.status='success') AS success_count, "
        " (SELECT MAX(finished_at) FROM jobs j WHERE j.account_id=a.id AND j.status='success') AS last_success "
        "FROM accounts a ORDER BY COALESCE(NULLIF(browser_group,''), '~'), platform, name"
    )
    for a in accs:
        a["profile_key"] = profile_key(a)
    return accs


def _check_group(platform: str, group: str, exclude_id: int | None = None):
    """Một nhóm Chrome chỉ chứa tối đa 1 tài khoản cho mỗi nền tảng (vì dùng chung cookie)."""
    group = group.strip()
    if not group:
        return
    key = profile_key({"id": 0, "browser_group": group})
    for a in db.rows("SELECT * FROM accounts WHERE platform=?", (platform,)):
        if a["id"] != exclude_id and (a.get("browser_group") or "").strip() and profile_key(a) == key:
            raise HTTPException(
                409, f"Nhóm '{group}' đã có tài khoản {PLATFORMS[platform]['label']} ({a['name']}). "
                     "Hai tài khoản cùng nền tảng phải ở hai nhóm khác nhau."
            )


def _cleanup_profile(key: str):
    """Xoá thư mục profile nếu không còn tài khoản nào dùng."""
    if not any(profile_key(a) == key for a in db.rows("SELECT * FROM accounts")):
        shutil.rmtree(PROFILES_DIR / key, ignore_errors=True)
        remove_clones(key)


@app.post("/api/accounts")
def add_account(a: AccountIn):
    if a.platform not in PLATFORMS:
        raise HTTPException(400, "Nền tảng không hợp lệ")
    if not a.name.strip():
        raise HTTPException(400, "Thiếu tên tài khoản")
    _check_group(a.platform, a.browser_group)
    aid = db.execute(
        "INSERT INTO accounts(platform, name, browser_group, target_name, created_at) VALUES(?,?,?,?,?)",
        (a.platform, a.name.strip(), a.browser_group.strip(), a.target_name.strip(), db.now()),
    )
    return {"id": aid}


@app.put("/api/accounts/{aid}/group")
def set_account_group(aid: int, body: AccountGroupIn):
    acc = db.row("SELECT * FROM accounts WHERE id=?", (aid,))
    if not acc:
        raise HTTPException(404, "Không tìm thấy tài khoản")
    if db.row("SELECT 1 FROM jobs WHERE account_id=? AND status='running'", (aid,)):
        raise HTTPException(409, "Tài khoản đang đăng video, hãy chờ xong")
    _check_group(acc["platform"], body.browser_group, exclude_id=aid)
    old_key = profile_key(acc)
    db.execute("UPDATE accounts SET browser_group=? WHERE id=?", (body.browser_group.strip(), aid))
    if old_key != profile_key({**acc, "browser_group": body.browser_group}):
        _cleanup_profile(old_key)
    return {"ok": True}


@app.put("/api/accounts/{aid}/target")
def set_account_target(aid: int, body: AccountTargetIn):
    if not db.row("SELECT 1 FROM accounts WHERE id=?", (aid,)):
        raise HTTPException(404, "Không tìm thấy tài khoản")
    db.execute("UPDATE accounts SET target_name=? WHERE id=?", (body.target_name.strip(), aid))
    return {"ok": True}


@app.delete("/api/accounts/{aid}")
def delete_account(aid: int):
    acc = db.row("SELECT * FROM accounts WHERE id=?", (aid,))
    if not acc:
        return {"ok": True}
    if db.row("SELECT 1 FROM jobs WHERE account_id=? AND status='running'", (aid,)):
        raise HTTPException(409, "Tài khoản đang đăng video, hãy chờ xong")
    db.execute("DELETE FROM jobs WHERE account_id=?", (aid,))
    db.execute("DELETE FROM accounts WHERE id=?", (aid,))
    _cleanup_profile(profile_key(acc))
    return {"ok": True}


@app.post("/api/accounts/{aid}/login")
def login_account(aid: int):
    """Mở một cửa sổ Chrome chứa tab đăng nhập của mọi tài khoản cùng nhóm với tài khoản này."""
    acc = db.row("SELECT * FROM accounts WHERE id=?", (aid,))
    if not acc:
        raise HTTPException(404, "Không tìm thấy tài khoản")
    key = profile_key(acc)
    group = [a for a in db.rows("SELECT * FROM accounts ORDER BY id") if profile_key(a) == key]
    group.sort(key=lambda a: a["id"] != aid)  # tab của tài khoản được bấm đứng đầu
    res = worker.open_login(group)
    if res == "busy":
        raise HTTPException(409, "Chrome của nhóm này đang được dùng để đăng video, hãy thử lại sau")
    return {"status": res, "tabs": len(group)}


# ---------------- Video ----------------
@app.get("/api/videos/scan")
def scan_folder(folder: str):
    p = Path(folder.strip().strip('"'))
    if not p.is_dir():
        raise HTTPException(400, "Thư mục không tồn tại")
    files = sorted(f for f in p.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXTS)
    return [{"path": str(f), "name": f.name, "size": f.stat().st_size} for f in files]


@app.post("/api/videos/upload")
def upload_videos(files: list[UploadFile] = File(...)):
    out = []
    for f in files:
        name = Path(f.filename or "video.mp4").name
        if Path(name).suffix.lower() not in VIDEO_EXTS:
            raise HTTPException(400, f"Không phải file video: {name}")
        dest = VIDEOS_DIR / f"{uuid.uuid4().hex[:8]}_{name}"
        with dest.open("wb") as w:
            shutil.copyfileobj(f.file, w, length=1024 * 1024)
        out.append({"path": str(dest), "name": name, "size": dest.stat().st_size})
    return out


class OcrIn(BaseModel):
    path: str


@app.post("/api/videos/ocr")
def ocr_video(body: OcrIn):
    """Đọc chữ trên vài giây đầu video để gợi ý tiêu đề."""
    from . import ocr
    if not Path(body.path).is_file():
        raise HTTPException(400, "Không tìm thấy file video")
    try:
        return ocr.read_title(body.path)
    except Exception as e:
        logging.getLogger("ocr").exception("Lỗi đọc chữ")
        raise HTTPException(500, f"Không đọc được chữ: {e}")


@app.get("/api/thumbs/{name}")
def thumb(name: str):
    from .ocr import THUMBS_DIR
    p = THUMBS_DIR / Path(name).name
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(p)


# ---------------- Job ----------------
class VideoItem(BaseModel):
    path: str
    title: str = ""
    description: str | None = None
    scheduled_at: str | None = None   # giờ đăng riêng của video (chế độ rải nhiều ngày)
    first_comment: str | None = None  # bình luận đầu tiên riêng của video (trống = dùng bình luận chung)


class JobsIn(BaseModel):
    videos: list[VideoItem]
    account_ids: list[int]
    description: str = ""
    hashtags: str = ""
    yt_visibility: str = "PUBLIC"
    start_at: str | None = None  # 'YYYY-MM-DDTHH:MM' giờ máy; trống = đăng ngay
    interval_minutes: int = 0
    first_comment: str = ""   # bình luận đầu tiên (affiliate) — chỉ Facebook, Threads
    platform_schedule: bool = False   # tải lên trước, hẹn giờ trên chính nền tảng (tắt máy được)


def _parse_time(s: str | None) -> datetime:
    if not s:
        return datetime.now().replace(microsecond=0)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        raise HTTPException(400, "Thời gian không hợp lệ")


COMMENT_PLATFORMS = {"facebook", "threads"}
# Nền tảng cho hẹn giờ trên web + khoảng hẹn trước tối đa mà tool dùng (an toàn, None = không giới hạn)
SCHEDULE_WINDOW = {"youtube": None, "tiktok": timedelta(days=9),
                   "facebook": timedelta(days=28), "threads": timedelta(days=28)}
MIN_LEAD = timedelta(minutes=20)


def _upload_plan(platform: str, when: datetime, wanted: bool):
    """-> (publish_mode, upload_at). Hẹn trên nền tảng: tải lên ngay, hoặc lúc vừa lọt khoảng cho phép."""
    now = datetime.now().replace(microsecond=0)
    if not wanted or platform not in SCHEDULE_WINDOW or when - now < MIN_LEAD:
        return "", None
    win = SCHEDULE_WINDOW[platform]
    up = now if win is None or when - win <= now else when - win
    return "platform", up.isoformat()


@app.post("/api/jobs")
def create_jobs(body: JobsIn):
    if not body.videos:
        raise HTTPException(400, "Chưa chọn video")
    if not body.account_ids:
        raise HTTPException(400, "Chưa chọn tài khoản")
    if body.yt_visibility not in ("PUBLIC", "UNLISTED", "PRIVATE"):
        raise HTTPException(400, "Chế độ hiển thị YouTube không hợp lệ")
    acc_rows = db.rows("SELECT id, platform, browser_group FROM accounts")
    platform_of = {a["id"]: a["platform"] for a in acc_rows}
    chan_cmt, chan_off = db.channel_comments(), set(db.channel_comment_off())
    chan_of = {a["id"]: chan_cmt.get(db.chan_key(a), "").strip() for a in acc_rows}
    no_cmt = {a["id"] for a in acc_rows if db.chan_key(a) in chan_off}   # kênh đã TẮT gắn bình luận
    accounts = set(platform_of)
    if not set(body.account_ids) <= accounts:
        raise HTTPException(400, "Có tài khoản không tồn tại")
    for v in body.videos:
        if not Path(v.path).is_file():
            raise HTTPException(400, f"Không tìm thấy file: {v.path}")

    start = _parse_time(body.start_at)
    created = 0
    for i, v in enumerate(body.videos):
        if v.scheduled_at:
            when_dt = _parse_time(v.scheduled_at)
        else:
            when_dt = start + timedelta(minutes=max(0, body.interval_minutes) * i)
        when = when_dt.isoformat()
        title = v.title.strip() or re.sub(r"^[0-9a-f]{8}_", "", Path(v.path).stem)
        desc = body.description if v.description is None else v.description
        own = (v.first_comment or "").strip()
        for aid in body.account_ids:
            # Ưu tiên: bình luận riêng của video > link riêng của kênh > bình luận chung
            cmt = "" if aid in no_cmt else (own or chan_of.get(aid) or body.first_comment.strip())
            mode, upload_at = _upload_plan(platform_of[aid], when_dt, body.platform_schedule)
            db.execute(
                "INSERT INTO jobs(account_id, video_path, title, description, hashtags, yt_visibility, "
                "scheduled_at, created_at, first_comment, publish_mode, upload_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (aid, v.path, title, desc, body.hashtags, body.yt_visibility, when, db.now(),
                 cmt if platform_of[aid] in COMMENT_PLATFORMS else "", mode, upload_at),
            )
            created += 1
    return {"created": created}


@app.get("/api/channel_comments")
def get_channel_comments():
    return db.channel_comments()


@app.get("/api/channel_tray")
def channel_tray():
    """Khay link affiliate: {texts: {kênh: nội dung}, off: [kênh đang tắt]}."""
    return {"texts": db.channel_comments(), "off": db.channel_comment_off()}


class ChannelCmtIn(BaseModel):
    key: str
    text: str | None = None
    enabled: bool | None = None


@app.put("/api/channel_tray")
def put_channel_tray(body: ChannelCmtIn):
    """Sửa MỘT kênh (gộp vào dữ liệu đang lưu -> không ghi đè link của kênh khác)."""
    key = body.key.strip()
    if not key:
        raise HTTPException(400, "Thiếu tên kênh")
    if body.text is not None:
        texts = db.channel_comments()
        if body.text.strip():
            texts[key] = body.text.strip()
        else:
            texts.pop(key, None)
        db.set_setting("channel_comments", json.dumps(texts, ensure_ascii=False))
    if body.enabled is not None:
        off = [k for k in db.channel_comment_off() if k != key]
        if not body.enabled:
            off.append(key)
        db.set_setting("channel_comment_off", json.dumps(off, ensure_ascii=False))
    return {"texts": db.channel_comments(), "off": db.channel_comment_off()}


@app.get("/api/comment_jobs")
def comment_jobs(limit: int = 300):
    """Bài đã đăng/đã hẹn thành công trên nền tảng có hỗ trợ bình luận (cho tab Gắn bình luận)."""
    ph = ",".join("?" * len(COMMENT_PLATFORMS))
    return db.rows(
        "SELECT j.id, j.title, j.video_path, j.scheduled_at, j.finished_at, j.first_comment, j.note, j.result_url, "
        "j.screenshot, j.cmt_tries, a.id AS account_id, a.platform, a.name AS account_name, a.browser_group "
        f"FROM jobs j JOIN accounts a ON a.id=j.account_id WHERE j.status='success' AND a.platform IN ({ph}) "
        "ORDER BY j.scheduled_at DESC, j.id DESC LIMIT ?", tuple(COMMENT_PLATFORMS) + (limit,))


class CommentIn(BaseModel):
    text: str


@app.post("/api/jobs/{jid}/comment")
def comment_job(jid: int, body: CommentIn):
    """Chỉ gắn bình luận (không đăng lại video). Bài chưa lên sóng thì chờ tới giờ + 3 phút."""
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Chưa nhập nội dung bình luận")
    job = db.row("SELECT j.status, j.scheduled_at, a.platform, a.id AS account_id, a.browser_group FROM jobs j "
                 "JOIN accounts a ON a.id=j.account_id WHERE j.id=?", (jid,))
    if not job:
        raise HTTPException(404, "Không tìm thấy bài")
    if not db.channel_comment_enabled(job):
        raise HTTPException(409, "Kênh này đang TẮT gắn bình luận — bật lại ở khay link phía trên")
    if job["status"] != "success" or job["platform"] not in COMMENT_PLATFORMS:
        raise HTTPException(409, "Chỉ gắn được cho bài Facebook/Threads đã đăng hoặc đã hẹn thành công")
    live = _parse_time(job["scheduled_at"]) + timedelta(minutes=3)
    at = max(live, datetime.now().replace(microsecond=0))
    note = "⏳ Chờ gắn bình luận" + (f" — bài lên sóng lúc {live - timedelta(minutes=3):%H:%M %d/%m}" if live > datetime.now() else "")
    db.execute("UPDATE jobs SET first_comment=?, note=?, upload_at=?, cmt_tries=0, screenshot=NULL WHERE id=?",
               (text, note, at.isoformat(timespec="seconds"), jid))
    return {"ok": True, "at": at.isoformat(timespec="seconds")}


@app.get("/api/jobs")
def list_jobs(status: str | None = None, limit: int = 300):
    sql = (
        "SELECT j.*, a.platform, a.name AS account_name FROM jobs j JOIN accounts a ON a.id=j.account_id "
    )
    params: tuple = ()
    if status == "upcoming":   # chờ đăng + đã hẹn trên nền tảng nhưng chưa tới giờ lên sóng
        sql += "WHERE j.status IN ('pending','running') OR (j.status='success' AND j.scheduled_at > ?) ORDER BY j.scheduled_at, j.id"
        params = (db.now(),)
    elif status == "queue":
        sql += "WHERE j.status IN ('pending','running') ORDER BY j.scheduled_at, j.id"
    elif status == "history":
        sql += "WHERE j.status IN ('success','failed','cancelled') ORDER BY COALESCE(j.finished_at, j.created_at) DESC"
    else:
        sql += "ORDER BY j.id DESC"
    sql += " LIMIT ?"
    return db.rows(sql, params + (limit,))


@app.get("/api/calendar")
def calendar(start: str, end: str):
    """Các lượt đăng có thời gian hẹn trong khoảng [start, end) — dùng cho giao diện lịch."""
    return db.rows(
        "SELECT j.*, a.platform, a.name AS account_name, a.browser_group FROM jobs j JOIN accounts a ON a.id=j.account_id "
        "WHERE j.scheduled_at >= ? AND j.scheduled_at < ? AND j.status != 'cancelled' ORDER BY j.scheduled_at, j.id",
        (start, end),
    )


@app.get("/api/channel_stock")
def channel_stock():
    """Tồn kho video chưa lên sóng của từng kênh -> biết ngay kênh nào hết / sắp hết video cần nạp thêm."""
    now_dt = datetime.now().replace(microsecond=0)
    now_s = now_dt.isoformat()
    today_s = now_dt.strftime("%Y-%m-%dT00:00:00")
    pf_order = list(PLATFORMS.keys())

    accs = db.rows("SELECT * FROM accounts ORDER BY id")
    chans: dict[str, dict] = {}
    acc_to_chan: dict[int, str] = {}
    for a in accs:
        key = db.chan_key(a)
        name = (a.get("browser_group") or "").strip() or a["name"]
        acc_to_chan[a["id"]] = key
        ch = chans.setdefault(key, {"key": key, "name": name, "accounts": []})
        ch["accounts"].append(a)

    jobs = db.rows("SELECT account_id, video_path, scheduled_at, status FROM jobs WHERE status != 'cancelled'")
    by_acc: dict[int, list] = {}
    for j in jobs:
        by_acc.setdefault(j["account_id"], []).append(j)

    out = []
    for key, ch in chans.items():
        ch["accounts"].sort(key=lambda a: pf_order.index(a["platform"]) if a["platform"] in pf_order else 99)
        up_slots, posted_slots = set(), set()
        next_at, last_at, last_posted_at = None, None, None
        platforms = []
        for a in ch["accounts"]:
            ajobs = by_acc.get(a["id"], [])
            up = [j for j in ajobs if j["scheduled_at"] > now_s and j["status"] in ("pending", "running", "success")]
            past_ok = [j for j in ajobs if j["scheduled_at"] <= now_s and j["status"] == "success"]
            failed = sum(1 for j in ajobs if j["status"] == "failed" and j["scheduled_at"] >= today_s)
            pf_last = max((j["scheduled_at"] for j in up), default=None)
            platforms.append({
                "id": a["id"],
                "platform": a["platform"],
                "name": a["name"],
                "upcoming": len(up),
                "failed": failed,
                "last_at": pf_last,
            })
            for j in up:
                up_slots.add((j["scheduled_at"][:16], j["video_path"]))
                if next_at is None or j["scheduled_at"] < next_at:
                    next_at = j["scheduled_at"]
                if last_at is None or j["scheduled_at"] > last_at:
                    last_at = j["scheduled_at"]
            for j in past_ok:
                posted_slots.add((j["scheduled_at"][:16], j["video_path"]))
                if last_posted_at is None or j["scheduled_at"] > last_posted_at:
                    last_posted_at = j["scheduled_at"]

        videos_left = len(up_slots)
        hours_left = 0.0
        days_left = 0
        if last_at:
            try:
                last_dt = datetime.fromisoformat(last_at)
                hours_left = round(max(0.0, (last_dt - now_dt).total_seconds() / 3600), 1)
                days_left = max(0, (last_dt.date() - now_dt.date()).days)
            except ValueError:
                pass
        if videos_left == 0:
            level = "empty"
        elif videos_left <= 3 or hours_left <= 48:
            level = "low"
        else:
            level = "ok"
        max_pf = max((p["upcoming"] for p in platforms), default=0)
        min_pf = min((p["upcoming"] for p in platforms), default=0)
        out.append({
            "key": key,
            "name": ch["name"],
            "account_ids": [a["id"] for a in ch["accounts"]],
            "videos_left": videos_left,
            "posted_videos": len(posted_slots),
            "next_at": next_at,
            "last_at": last_at,
            "last_posted_at": last_posted_at,
            "hours_left": hours_left,
            "days_left": days_left,
            "level": level,
            "uneven": len(platforms) > 1 and max_pf != min_pf,
            "failed_total": sum(p["failed"] for p in platforms),
            "platforms": platforms,
        })

    rank = {"empty": 0, "low": 1, "ok": 2}
    out.sort(key=lambda c: (rank[c["level"]], c["videos_left"], c["last_at"] or "", c["name"].lower()))
    return out


class RescheduleIn(BaseModel):
    scheduled_at: str


@app.post("/api/jobs/{jid}/reschedule")
def reschedule_job(jid: int, body: RescheduleIn):
    when_dt = _parse_time(body.scheduled_at)
    when = when_dt.isoformat()
    job = db.row("SELECT j.status, j.publish_mode, a.platform FROM jobs j JOIN accounts a ON a.id=j.account_id WHERE j.id=?", (jid,))
    if not job:
        raise HTTPException(404, "Không tìm thấy lượt đăng")
    if job["status"] not in ("pending", "cancelled", "failed"):
        raise HTTPException(409, "Chỉ đổi lịch được lượt đăng chưa tải lên, đã huỷ hoặc bị lỗi")
    mode, upload_at = _upload_plan(job["platform"], when_dt, job["publish_mode"] == "platform")
    db.execute(
        "UPDATE jobs SET scheduled_at=?, status='pending', error=NULL, screenshot=NULL, finished_at=NULL, "
        "note=NULL, publish_mode=?, upload_at=? WHERE id=?",
        (when, mode or job["publish_mode"], upload_at, jid),
    )
    return {"ok": True}


@app.get("/api/stats")
def stats():
    r = db.rows("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
    return {x["status"]: x["n"] for x in r}


@app.post("/api/jobs/{jid}/retry")
def retry_job(jid: int):
    """Thử lại GIỮ NGUYÊN giờ hẹn nếu giờ đó vẫn còn ở tương lai (trước đây luôn đổi thành 'ngay bây giờ'
    -> bài hẹn giờ bị đăng ngay). Chỉ bài đã quá giờ hẹn mới đăng ngay."""
    job = db.row("SELECT j.status, j.scheduled_at, j.publish_mode, a.platform FROM jobs j "
                 "JOIN accounts a ON a.id=j.account_id WHERE j.id=?", (jid,))
    if not job or job["status"] not in ("failed", "cancelled"):
        return {"ok": True}
    when_dt = _parse_time(job["scheduled_at"])
    if when_dt - datetime.now() >= MIN_LEAD:
        when = job["scheduled_at"]
        mode, upload_at = _upload_plan(job["platform"], when_dt, job["publish_mode"] == "platform")
        mode = mode or job["publish_mode"]
    else:
        when, mode, upload_at = db.now(), job["publish_mode"], None
    db.execute(
        "UPDATE jobs SET status='pending', error=NULL, screenshot=NULL, result_url=NULL, note=NULL, finished_at=NULL, "
        "started_at=NULL, cmt_tries=0, scheduled_at=?, publish_mode=?, upload_at=? WHERE id=?",
        (when, mode, upload_at, jid),
    )
    return {"ok": True}


@app.post("/api/jobs/{jid}/run")
def run_job_now(jid: int):
    """Chạy bài này ngay (kể cả khi đang Tạm dừng). Hẹn trên nền tảng thì tải lên ngay và vẫn hẹn đúng giờ đã đặt."""
    if not db.row("SELECT 1 FROM jobs WHERE id=? AND status='pending'", (jid,)):
        raise HTTPException(409, "Chỉ chạy được bài đang chờ đăng")
    db.execute("UPDATE jobs SET run_now=1, upload_at=? WHERE id=?", (db.now(), jid))
    return {"ok": True}


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: int):
    db.execute("UPDATE jobs SET status='cancelled', finished_at=? WHERE id=? AND status='pending'", (db.now(), jid))
    return {"ok": True}


@app.delete("/api/jobs/{jid}")
def delete_job(jid: int):
    db.execute("DELETE FROM jobs WHERE id=? AND status!='running'", (jid,))
    return {"ok": True}


@app.get("/api/screenshots/{name}")
def screenshot(name: str):
    p = SHOTS_DIR / Path(name).name
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(p)


# ---------------- Cài đặt ----------------
class SettingsIn(BaseModel):
    headless: bool


@app.get("/api/settings")
def get_settings():
    return {"headless": db.get_setting("headless", "0") == "1"}


@app.put("/api/settings")
def put_settings(s: SettingsIn):
    db.set_setting("headless", "1" if s.headless else "0")
    return {"ok": True}


# ---------------- Nội dung cố định (hashtag, chế độ YouTube) ----------------
PREF_KEYS = {"hashtags", "yt_visibility", "auto_ocr", "first_comment", "platform_schedule"}


@app.get("/api/pause")
def get_pause():
    return {"paused": db.get_setting("paused", "0") == "1"}


@app.put("/api/pause")
def put_pause(body: dict):
    db.set_setting("paused", "1" if body.get("paused") else "0")
    return get_pause()


@app.get("/api/prefs")
def get_prefs():
    try:
        return json.loads(db.get_setting("prefs", "{}"))
    except ValueError:
        return {}


@app.put("/api/prefs")
def put_prefs(prefs: dict):
    cur = get_prefs()
    cur.update({k: str(v)[:2000] for k, v in prefs.items() if k in PREF_KEYS})
    db.set_setting("prefs", json.dumps(cur, ensure_ascii=False))
    return cur


# ---------------- Nút giờ đăng cố định ----------------
class PresetIn(BaseModel):
    time: str            # "HH:MM"
    interval: int = 0    # phút giữa các video


@app.get("/api/presets")
def get_presets():
    try:
        return json.loads(db.get_setting("time_presets", "[]"))
    except ValueError:
        return []


@app.put("/api/presets")
def put_presets(presets: list[PresetIn]):
    clean = []
    for p in presets:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", p.time):
            raise HTTPException(400, f"Giờ không hợp lệ: {p.time}")
        item = {"time": p.time, "interval": max(0, p.interval)}
        if item not in clean:
            clean.append(item)
    clean.sort(key=lambda x: (x["time"], x["interval"]))
    db.set_setting("time_presets", json.dumps(clean))
    return clean


# ---------------- Gemini: chia video raw cho các tài khoản ----------------
class GemAccIn(BaseModel):
    name: str
    tabs: str = ""   # mỗi dòng một link cuộc trò chuyện


def _gem_tabs(text: str) -> list[str]:
    out = []
    for line in re.split(r"[\s,]+", text or ""):
        u = gemini.clean_tab(line)
        if u and u not in out:
            out.append(u)
    return out


@app.get("/api/gem/accounts")
def gem_accounts():
    accs = db.rows("SELECT * FROM gem_accounts ORDER BY id")
    cnt = {}
    for r in db.rows("SELECT account_id, status, COUNT(*) n FROM gem_tasks GROUP BY account_id, status"):
        cnt.setdefault(r["account_id"], {})[r["status"]] = r["n"]
    running = {}   # tab -> video đang nạp / chờ nạp / nạp gần nhất
    for r in db.rows("SELECT account_id, tab_idx, video_path, status, finished_at FROM gem_tasks "
                     "WHERE tab_idx IS NOT NULL ORDER BY id"):
        tabs = running.setdefault(r["account_id"], {})
        cur = tabs.get(r["tab_idx"])
        if not cur or cur["status"] in ("done", "failed") or r["status"] in ("pending", "running"):
            if not (cur and cur["status"] in ("pending", "running") and r["status"] in ("done", "failed")):
                tabs[r["tab_idx"]] = {"name": Path(r["video_path"]).name, "status": r["status"], "at": r["finished_at"]}
    for a in accs:
        a["tabs"] = gemini.tabs_of(a)
        a["counts"] = cnt.get(a["id"], {})
        a["running"] = running.get(a["id"], {})
        logins = {k for k, t in gemini._login_threads.items() if t.is_alive()}
        a["login_open"] = a["id"] in logins
        a["chrome_open"] = a["id"] in gemini._idle or a["login_open"]
        a["watching"] = gemini._watching.get(a["id"], {})
        a["queue"] = {r["tab_idx"]: r["n"] for r in db.rows(
            "SELECT tab_idx, COUNT(*) n FROM gem_tasks WHERE account_id=? AND status='pending' GROUP BY tab_idx", (a["id"],))}
    return accs


@app.post("/api/gem/accounts")
def gem_add_account(body: GemAccIn):
    if not body.name.strip():
        raise HTTPException(400, "Thiếu tên tài khoản")
    aid = db.execute("INSERT INTO gem_accounts(name, tabs, created_at) VALUES(?,?,?)",
                     (body.name.strip(), json.dumps(_gem_tabs(body.tabs)), db.now()))
    return {"id": aid}


@app.put("/api/gem/accounts/{aid}")
def gem_edit_account(aid: int, body: GemAccIn):
    db.execute("UPDATE gem_accounts SET name=?, tabs=? WHERE id=?", (body.name.strip(), json.dumps(_gem_tabs(body.tabs)), aid))
    return {"ok": True}


@app.delete("/api/gem/accounts/{aid}")
def gem_del_account(aid: int):
    if db.row("SELECT 1 FROM gem_tasks WHERE account_id=? AND status='running'", (aid,)):
        raise HTTPException(409, "Tài khoản đang chạy, hãy Tạm dừng trước")
    db.execute("UPDATE gem_tasks SET account_id=NULL WHERE account_id=? AND status='pending'", (aid,))
    db.execute("DELETE FROM gem_accounts WHERE id=?", (aid,))
    shutil.rmtree(PROFILES_DIR / gemini.pkey(aid), ignore_errors=True)
    return {"ok": True, "moved": gemini.rebalance()}


@app.post("/api/gem/accounts/{aid}/login")
def gem_login(aid: int):
    if not db.row("SELECT 1 FROM gem_accounts WHERE id=?", (aid,)):
        raise HTTPException(404, "Không tìm thấy tài khoản")
    res = gemini.open_login(aid)
    if res == "busy":
        raise HTTPException(409, "Chrome của tài khoản này đang mở sẵn rồi (đang nạp hoặc để mở xem kết quả) — xem trên thanh tác vụ")
    return {"status": res}


@app.post("/api/gem/accounts/{aid}/{action}")
def gem_acc_action(aid: int, action: str):
    if action == "resume":
        db.execute("UPDATE gem_accounts SET status='ok', resume_at=NULL, note=NULL WHERE id=?", (aid,))
    elif action == "off":
        db.execute("UPDATE gem_accounts SET status='off', note=NULL WHERE id=?", (aid,))
    elif action == "refresh":
        if not gemini.setting("setup_prompt").strip():
            raise HTTPException(400, "Chưa nhập câu lệnh thiết lập phiên mới (mục 🆕 Phiên mới)")
        if aid in {k for k, t in gemini._login_threads.items() if t.is_alive()}:
            raise HTTPException(409, "Cửa sổ đăng nhập của tài khoản này đang mở — đóng lại trước")
        db.execute("UPDATE gem_accounts SET refresh=1, note='⏳ Đang mở phiên mới…' WHERE id=?", (aid,))
    else:
        raise HTTPException(400, "Thao tác không hợp lệ")
    return {"ok": True}


class GemBatchIn(BaseModel):
    paths: list[str]
    account_ids: list[int]


def _gem_files(paths: list[str]):
    """-> danh sách video kèm trạng thái: đã nạp (cho ai, tab nào) / trùng nội dung với video khác / chưa nạp."""
    idx = gemini.sent_index()
    first: dict[str, str] = {}
    out = []
    for p in paths:
        f = Path(p)
        it = {"path": str(f), "name": f.name, "size": f.stat().st_size, "fhash": gemini.fingerprint(f), "sent": None, "dup_of": None}
        r = gemini.lookup(idx, it["path"], it["fhash"])
        if gemini.is_translated(f.name):   # video ĐÃ DỊCH (tải từ Gemini về) để chung thư mục -> không nạp lại
            it["dup_of"] = "video đã dịch (_vietsub) — bỏ qua"
        elif r:
            it["sent"] = {k: r[k] for k in ("id", "status", "account_name", "tab_idx", "finished_at", "created_at", "chat_url")}
            if Path(r["video_path"]).name != f.name:
                it["dup_of"] = Path(r["video_path"]).name   # cùng nội dung, khác tên
        elif it["fhash"] in first:
            it["dup_of"] = first[it["fhash"]]               # trùng với video khác trong cùng thư mục
        first.setdefault(it["fhash"], f.name)
        out.append(it)
    return out


@app.post("/api/gem/batch")
def gem_batch(body: GemBatchIn):
    """Một lượt nạp: mỗi tab (của các tài khoản được chọn) nhận 1 video chưa nạp, theo thứ tự thư mục."""
    if not body.paths:
        raise HTTPException(400, "Chưa chọn video")
    for p in body.paths:
        if not Path(p).is_file():
            raise HTTPException(400, f"Không tìm thấy file: {p}")
    slots = gemini.free_slots(body.account_ids)
    if not slots:
        raise HTTPException(409, "Không có tab nào để nạp: tài khoản chưa có tab hoặc chưa sẵn sàng (hết lượt / chưa đăng nhập / đã tắt)")
    files = _gem_files(list(dict.fromkeys(body.paths)))
    fresh = [f for f in files if not f["sent"] and not f["dup_of"]]
    made = gemini.distribute(fresh, slots, datetime.now().strftime("%Y%m%d_%H%M"))
    db.set_setting("gem_auto_folder", str(Path(body.paths[0]).parent))   # tự nạp tiếp lấy video từ thư mục này
    names = {a["id"]: a["name"] for a in db.rows("SELECT id, name FROM gem_accounts")}
    for m in made:
        m["account"] = names.get(m["account_id"])
    return {"created": len(made), "slots": len(slots), "left": len(fresh) - len(made),
            "dups": [{"name": f["name"], "dup_of": f["dup_of"]} for f in files if f["dup_of"] and not f["sent"]],
            "items": made}


@app.get("/api/gem/scan")
def gem_scan(folder: str):
    """Video trong thư mục + đã nạp chưa (cho ai, tab nào, lúc nào) + video trùng + nạp tới đâu rồi."""
    files = _gem_files([f["path"] for f in scan_folder(folder)])
    last = max((i for i, f in enumerate(files) if f["sent"]), default=-1)
    for f in files:
        f.pop("fhash", None)
    return {"files": files, "sent": sum(1 for f in files if f["sent"]), "total": len(files), "last_index": last,
            "last_name": files[last]["name"] if last >= 0 else None,
            "dups": sum(1 for f in files if f["dup_of"] and not f["sent"])}


@app.get("/api/gem/tasks")
def gem_tasks(limit: int = 500):
    return db.rows("SELECT t.id, t.account_id, t.tab_idx, t.batch, t.video_path, t.status, t.chat_url, t.error, t.screenshot, "
                   "t.started_at, t.finished_at, substr(t.reply,1,300) AS reply, a.name AS account_name FROM gem_tasks t "
                   "LEFT JOIN gem_accounts a ON a.id=t.account_id WHERE t.hidden=0 ORDER BY t.id DESC LIMIT ?", (limit,))


@app.post("/api/gem/refresh_all")
def gem_refresh_all():
    if not gemini.setting("setup_prompt").strip():
        raise HTTPException(400, "Chưa nhập câu lệnh thiết lập phiên mới (mục 🆕 Phiên mới)")
    with db.conn() as c:
        n = c.execute("UPDATE gem_accounts SET refresh=1, note='⏳ Đang mở phiên mới…' WHERE status='ok'").rowcount
    return {"accounts": n}


@app.post("/api/gem/rebalance")
def gem_rebalance():
    return {"moved": gemini.rebalance()}


@app.post("/api/gem/retry_failed")
def gem_retry_failed():
    with db.conn() as c:
        n = c.execute("UPDATE gem_tasks SET status='pending', error=NULL, screenshot=NULL, finished_at=NULL WHERE status='failed'").rowcount
    return {"retried": n}


@app.post("/api/gem/tasks/{tid}/{action}")
def gem_task_action(tid: int, action: str):
    if action == "retry":
        db.execute("UPDATE gem_tasks SET status='pending', error=NULL, screenshot=NULL, finished_at=NULL WHERE id=? AND status IN ('failed','done')", (tid,))
    elif action == "delete":
        db.execute("DELETE FROM gem_tasks WHERE id=? AND status='pending'", (tid,))   # bỏ khỏi hàng chờ = coi như chưa gửi
    elif action == "hide":
        db.execute("UPDATE gem_tasks SET hidden=1 WHERE id=? AND status IN ('done','failed')", (tid,))
    else:
        raise HTTPException(400, "Thao tác không hợp lệ")
    return {"ok": True}


@app.post("/api/gem/clear_done")
def gem_clear_done():
    with db.conn() as c:
        n = c.execute("UPDATE gem_tasks SET hidden=1 WHERE status='done' AND hidden=0").rowcount   # vẫn nhớ là đã gửi
    return {"deleted": n}


GEM_SETTINGS = {"setup_prompt": "", "tab_count": "3", "download_dir": "", "model": "Neon", "keep_open": "1", "auto_next": "1", "max_wait": "40", "prompt": "", "parallel": "1", "gap": "5", "cooldown": "24", "paused": "0"}


@app.get("/api/gem/settings")
def gem_get_settings():
    out = {k: gemini.setting(k, v) for k, v in GEM_SETTINGS.items()}
    out["download_dir_effective"] = str(gemini.download_dir())
    return out


@app.post("/api/gem/open_downloads")
def gem_open_downloads():
    os.startfile(str(gemini.download_dir()))   # mở thư mục video đã tải trong Windows Explorer
    return {"ok": True}


@app.put("/api/gem/settings")
def gem_put_settings(body: dict):
    for k, v in body.items():
        if k in GEM_SETTINGS:
            db.set_setting("gem_" + k, str(v)[:50000])
    return gem_get_settings()


# ---------------- Tải video kênh Douyin ----------------
class DyScanIn(BaseModel):
    url: str
    limit: int = 0


class DyDownloadIn(BaseModel):
    ids: list[str]
    folder: str = ""
    split: bool = True   # tự chia thư mục: mỗi phim bộ 1 thư mục, video lẻ 1 thư mục


@app.post("/api/dy/scan")
def dy_scan(body: DyScanIn):
    m = re.search(r"https?://\S+", body.url.strip())
    if not m or "douyin.com" not in m.group(0):
        raise HTTPException(400, "Dán link kênh Douyin (douyin.com/user/... hoặc v.douyin.com/...)")
    if not douyin.start_scan(m.group(0), max(0, body.limit)):
        raise HTTPException(409, "Đang lấy danh sách một kênh khác")
    return {"ok": True}


@app.get("/api/dy/scan")
def dy_scan_state():
    s = douyin.scan_state()
    s["root"] = douyin.default_root()
    s["login"] = douyin.login_state()
    return s


@app.post("/api/dy/login")
def dy_login():
    r = douyin.open_login()
    if r == "busy":
        raise HTTPException(409, "Chrome Douyin đang bận (đang lấy danh sách) — chờ xong rồi thử lại")
    return {"status": r}


@app.post("/api/dy/scan/stop")
def dy_scan_stop():
    douyin.stop_scan()
    return {"ok": True}


@app.post("/api/dy/download")
def dy_download(body: DyDownloadIn):
    s = douyin.scan_state()
    folder = body.folder.strip().strip('"')
    if not folder:
        folder = str(Path(douyin.default_root()) / (douyin.safe_name(s.get("nickname") or "", 60) or s.get("sec_uid") or "kenh"))
    if not Path(folder).is_absolute():
        raise HTTPException(400, "Thư mục lưu phải là đường dẫn đầy đủ, VD: C:\\Users\\admin\\Videos\\douyin\\TenKenh")
    ok, err = douyin.start_download(body.ids, folder, s.get("nickname") or "", s.get("sec_uid") or "", body.split)
    if not ok:
        raise HTTPException(409, err)
    return {"ok": True, "folder": folder}


@app.get("/api/dy/download")
def dy_download_state():
    return douyin.dl_state()


@app.post("/api/dy/download/stop")
def dy_download_stop():
    douyin.stop_download()
    return {"ok": True}


class DyLabelIn(BaseModel):
    label: str = ""


@app.get("/api/dy/channels")
def dy_channels():
    return douyin.channels()


@app.put("/api/dy/channels/{sec_uid}")
def dy_channel_label(sec_uid: str, body: DyLabelIn):
    douyin.set_label(sec_uid, body.label)
    return {"ok": True}


@app.delete("/api/dy/channels/{sec_uid}")
def dy_channel_delete(sec_uid: str):
    douyin.delete_channel(sec_uid)
    return {"ok": True}


@app.post("/api/dy/open_folder")
def dy_open_folder(body: dict):
    """Mở thư mục video trong Windows Explorer."""
    f = Path(str(body.get("folder") or "").strip().strip('"'))
    if not f.is_dir():
        raise HTTPException(400, "Thư mục không tồn tại (chưa tải video nào?)")
    os.startfile(str(f))
    return {"ok": True}


class DyGroupLabelIn(BaseModel):
    sec_uid: str
    gkey: str
    label: str = ""


@app.put("/api/dy/groups")
def dy_group_label(body: DyGroupLabelIn):
    """Đặt tên tiếng Việt cho một phim bộ (dùng làm tên thư mục khi tải)."""
    douyin.set_group_label(body.sec_uid, body.gkey, body.label)
    return {"ok": True}


@app.post("/api/dy/channels/{sec_uid}/autoname")
def dy_channel_autoname(sec_uid: str):
    """Tự đặt (dịch lại) tên tiếng Việt cho kênh + các phim bộ. Tên người dùng tự đặt được giữ nguyên."""
    douyin.autoname(sec_uid, force=True)
    return {"ok": True}
