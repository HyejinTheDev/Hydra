"""Tải hàng loạt video của một kênh Douyin.

- Lấy danh sách: mở Chrome (profile riêng data/profiles/douyin) vào trang kênh, "nghe" dữ liệu
  /aweme/v1/web/aweme/post/ mà chính trang Douyin tải về, cuộn xuống tới hết danh sách.
  Cửa sổ Chrome hiện lên -> nếu Douyin bắt xác minh (captcha) / đăng nhập thì người dùng tự làm trong cửa sổ đó.
- Tải: lấy link video chất lượng cao nhất (ưu tiên H.264, không logo) rồi tải thẳng về thư mục
  <thư mục gốc>/<tên kênh>/YYYY-MM-DD_<mô tả>_<id>.mp4 (tên bắt đầu bằng ngày -> sắp theo thứ tự đăng).
- Nhớ video đã tải (bảng dy_videos) -> lần sau quét lại kênh chỉ cần tải video mới.
"""
import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from . import db, dy_series
from .browser import launch, profile_lock

log = logging.getLogger("douyin")
PROFILE = "douyin"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")
POST_API = "/aweme/v1/web/aweme/post/"

SCHEMA = """
CREATE TABLE IF NOT EXISTS dy_videos (
    aweme_id    TEXT PRIMARY KEY,
    sec_uid     TEXT NOT NULL DEFAULT '',
    author      TEXT NOT NULL DEFAULT '',
    descr       TEXT NOT NULL DEFAULT '',
    create_time INTEGER NOT NULL DEFAULT 0,
    path        TEXT,
    size        INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT '',   -- done | failed
    error       TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_dy_videos_sec ON dy_videos(sec_uid, status);
CREATE TABLE IF NOT EXISTS dy_groups (
    sec_uid TEXT NOT NULL,
    gkey    TEXT NOT NULL,                 -- khoá nhóm phim bộ (mix:<id> hoặc t:<tên>)
    label   TEXT NOT NULL DEFAULT '',      -- tên tiếng Việt do người dùng đặt (dùng làm tên thư mục)
    PRIMARY KEY (sec_uid, gkey)
);
CREATE TABLE IF NOT EXISTS dy_member (
    aweme_id TEXT PRIMARY KEY,             -- video này thuộc phim bộ nào (giữ ổn định giữa các lần lấy)
    sec_uid  TEXT NOT NULL,
    gkey     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dy_channels (
    sec_uid    TEXT PRIMARY KEY,
    nickname   TEXT NOT NULL DEFAULT '',
    label      TEXT NOT NULL DEFAULT '',   -- tên gợi nhớ (tiếng Việt) do người dùng đặt
    avatar     TEXT NOT NULL DEFAULT '',
    total      INTEGER,                    -- tổng số video trên kênh (theo Douyin)
    listed     TEXT NOT NULL DEFAULT '[]', -- JSON [[aweme_id, create_time], ...] lần lấy danh sách gần nhất (video, không tính bài ảnh)
    folder     TEXT NOT NULL DEFAULT '',
    scanned_at TEXT,
    created_at TEXT NOT NULL
);
"""

_lock = threading.Lock()
_scan: dict = {"running": False}
_dl: dict = {"running": False}
_urls: dict[str, list[str]] = {}       # aweme_id -> các link tải (hết hạn sau vài giờ)
_meta: dict[str, dict] = {}            # aweme_id -> thông tin video lần quét gần nhất
_stop_scan = threading.Event()
_stop_dl = threading.Event()


def init():
    with db.conn() as c:
        c.executescript(SCHEMA)
        for tbl in ("dy_channels", "dy_groups"):
            cols = {r["name"] for r in c.execute(f"PRAGMA table_info({tbl})")}
            if "label_auto" not in cols:   # 1 = tên do tool tự dịch (người dùng sửa -> 0, không ghi đè nữa)
                c.execute(f"ALTER TABLE {tbl} ADD COLUMN label_auto INTEGER NOT NULL DEFAULT 0")
        gcols = {r["name"] for r in c.execute("PRAGMA table_info(dy_groups)")}
        if "folder" not in gcols:   # thư mục đã tải tập đầu tiên -> các tập sau luôn vào đây
            c.execute("ALTER TABLE dy_groups ADD COLUMN folder TEXT NOT NULL DEFAULT ''")
    threading.Thread(target=_autoname_all, name="douyin-autoname", daemon=True).start()


def default_root() -> str:
    return db.get_setting("dy_folder", "") or str(Path.home() / "Videos" / "douyin")


def safe_name(s: str, n: int = 50) -> str:
    s = re.sub(r"#\S+", "", s or "")                        # bỏ hashtag (thường rất dài)
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:n].strip(" .")


# ---------------- Đọc dữ liệu một video ----------------
def _video_urls(v: dict) -> list[str]:
    """Link tải không logo, tốt nhất trước: H.264 bitrate cao -> H.265 -> play_addr mặc định."""
    video = v.get("video") or {}
    rates = video.get("bit_rate") or []
    rates = sorted(rates, key=lambda r: (not r.get("is_bytevc1") and not r.get("is_h265"), r.get("bit_rate") or 0), reverse=True)
    out = []
    for r in rates:
        out += (r.get("play_addr") or {}).get("url_list") or []
    out += (video.get("play_addr") or {}).get("url_list") or []
    # link trực tiếp CDN trước, link qua www.douyin.com/aweme/v1/play (cần chuyển hướng) để cuối
    seen, direct, other = set(), [], []
    for u in out:
        if u and u not in seen:
            seen.add(u)
            (other if "/aweme/v1/play" in u else direct).append(u)
    return direct + other


def _item(v: dict) -> dict:
    video = v.get("video") or {}
    cover = ((video.get("cover") or video.get("origin_cover") or {}).get("url_list") or [""])[0]
    is_image = bool(v.get("images")) or v.get("aweme_type") in (2, 68, 150)
    return {
        "aweme_id": str(v.get("aweme_id")),
        "desc": v.get("desc") or "",
        "create_time": int(v.get("create_time") or 0),
        "duration": round((video.get("duration") or 0) / 1000),
        "cover": cover,
        "is_image": is_image,
        "is_top": bool(v.get("is_top")),
        "likes": (v.get("statistics") or {}).get("digg_count") or 0,
        "mix_id": str((v.get("mix_info") or {}).get("mix_id") or ""),
        "mix_name": (v.get("mix_info") or {}).get("mix_name") or "",
        "mix_ep": ((v.get("mix_info") or {}).get("statis") or {}).get("current_episode") or None,
    }


def _mark_downloaded(items: list[dict]):
    ids = [i["aweme_id"] for i in items]
    done = {}
    for k in range(0, len(ids), 500):
        part = ids[k:k + 500]
        for r in db.rows(f"SELECT aweme_id, path, status FROM dy_videos WHERE aweme_id IN ({','.join('?' * len(part))})", part):
            if r["status"] == "done":
                done[r["aweme_id"]] = r["path"] or ""
    for i in items:
        i["downloaded"] = done.get(i["aweme_id"])
        i["file_missing"] = bool(i["downloaded"]) and not Path(i["downloaded"]).is_file()


# ---------------- Lấy danh sách video của kênh ----------------
def scan_state() -> dict:
    with _lock:
        s = dict(_scan)
        s["items"] = list(_scan.get("items") or [])
    if not s["running"] and s["items"]:
        _mark_downloaded(s["items"])
        listed = _listed(db.row("SELECT listed FROM dy_channels WHERE sec_uid=?", (s.get("sec_uid") or "",)))
        for i in s["items"]:
            fs = listed.get(i["aweme_id"], (0, ""))[1]
            i["is_new"] = bool(fs) and not i["downloaded"]
            i["first_seen"] = fs
        labels = group_labels(s.get("sec_uid") or "")
        autos = {r["gkey"] for r in db.rows("SELECT gkey FROM dy_groups WHERE sec_uid=? AND label_auto=1", (s.get("sec_uid") or "",))}
        for i in s["items"]:
            i["grp_label"] = labels.get(i.get("grp") or "", "")
            i["grp_label_auto"] = (i.get("grp") or "") in autos
        folders = group_folders(s.get("sec_uid") or "")
        for i in s["items"]:
            i["grp_folder"] = folders.get(i.get("grp") or "", "")
    return s


def _classify(sec_uid: str, items: list[dict]):
    """Phân loại + giữ danh tính phim ổn định: nhóm nào có video đã từng thuộc phim X (lần lấy trước)
    thì vẫn mang khoá X -> giữ tên tiếng Việt + thư mục cũ, kể cả khi tên tiếng Trung lần này lệch đi."""
    vids = [i for i in items if not i["is_image"]]
    dy_series.classify(vids)
    if not sec_uid:
        return
    old = {}
    ids = [i["aweme_id"] for i in vids]
    for k in range(0, len(ids), 500):
        part = ids[k:k + 500]
        for r in db.rows(f"SELECT aweme_id, gkey FROM dy_member WHERE aweme_id IN ({','.join('?' * len(part))})", part):
            old[r["aweme_id"]] = r["gkey"]
    groups: dict[str, list] = {}
    for i in vids:
        if i.get("grp"):
            groups.setdefault(i["grp"], []).append(i)
    used = set()
    # nhóm đông nhất chọn trước, để 2 nhóm không tranh 1 khoá cũ
    for key, mem in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        prev = [old[i["aweme_id"]] for i in mem if old.get(i["aweme_id"])]
        new_key = key
        if prev:
            best = max(set(prev), key=prev.count)
            if best not in used:
                new_key = best
        if new_key in used:   # trùng khoá (hiếm) -> giữ khoá mới tính
            new_key = key
        used.add(new_key)
        for i in mem:
            i["grp"] = new_key
    with db.conn() as c:
        for i in vids:
            if i.get("grp"):
                c.execute("INSERT INTO dy_member(aweme_id, sec_uid, gkey) VALUES(?,?,?) "
                          "ON CONFLICT(aweme_id) DO UPDATE SET gkey=excluded.gkey", (i["aweme_id"], sec_uid, i["grp"]))
            else:   # lần này thành video lẻ -> bỏ gắn cũ
                c.execute("DELETE FROM dy_member WHERE aweme_id=?", (i["aweme_id"],))


def group_folders(sec_uid: str) -> dict:
    """Thư mục đã khoá của từng phim (còn tồn tại trên máy)."""
    return {r["gkey"]: r["folder"] for r in db.rows("SELECT gkey, folder FROM dy_groups WHERE sec_uid=? AND folder<>''", (sec_uid,))
            if Path(r["folder"]).is_dir()}


def _lock_folder(sec_uid: str, gkey: str, folder: str):
    db.execute("INSERT INTO dy_groups(sec_uid, gkey, label, folder) VALUES(?,?,'',?) "
               "ON CONFLICT(sec_uid, gkey) DO UPDATE SET folder=CASE WHEN dy_groups.folder='' THEN excluded.folder ELSE dy_groups.folder END",
               (sec_uid, gkey, folder))


def group_labels(sec_uid: str) -> dict:
    return {r["gkey"]: r["label"] for r in db.rows("SELECT gkey, label FROM dy_groups WHERE sec_uid=? AND label<>''", (sec_uid,))}


def set_group_label(sec_uid: str, gkey: str, label: str):
    db.execute("INSERT INTO dy_groups(sec_uid, gkey, label, label_auto) VALUES(?,?,?,0) "
               "ON CONFLICT(sec_uid, gkey) DO UPDATE SET label=excluded.label, label_auto=0",
               (sec_uid, gkey, label.strip()[:80]))


# ---------------- Tự đặt tên tiếng Việt (dịch máy) ----------------
_tr_cache: dict[str, str] = {}


def translate_vi(text: str) -> str:
    """Dịch tiếng Trung -> tiếng Việt (Google Dịch). Lỗi mạng -> ''."""
    text = (text or "").strip()
    if not text or not re.search(r"[一-鿿]", text):
        return ""
    if text in _tr_cache:
        return _tr_cache[text]
    try:
        url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=zh-CN&tl=vi&dt=t&q="
               + urllib.parse.quote(text))
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        out = "".join(x[0] for x in data[0] if x and x[0]).strip().rstrip("?？.。")
        out = out[:1].upper() + out[1:]
    except Exception as e:
        log.info("dịch tên lỗi: %s", e)
        return ""
    _tr_cache[text] = out
    return out[:80]


def autoname(sec_uid: str, items: list[dict] | None = None, force: bool = False):
    """Tự đặt tên tiếng Việt cho kênh + các phim bộ chưa có tên. Không đè tên người dùng tự đặt
    (force=True: dịch lại cả tên tự dịch cũ, vẫn giữ tên người dùng)."""
    c = db.row("SELECT * FROM dy_channels WHERE sec_uid=?", (sec_uid,))
    if c and (not c["label"] or (force and c["label_auto"])):
        vi = translate_vi(c["nickname"])
        if vi:
            db.execute("UPDATE dy_channels SET label=?, label_auto=1 WHERE sec_uid=?", (vi, sec_uid))
    if items is None:
        with _lock:
            items = list(_scan.get("items") or []) if _scan.get("sec_uid") == sec_uid else []
    have = {r["gkey"]: r for r in db.rows("SELECT * FROM dy_groups WHERE sec_uid=?", (sec_uid,))}
    names = {}
    for i in items:
        if i.get("grp") and i["grp"] != "t:__noname":
            names.setdefault(i["grp"], i.get("grp_name") or "")
    for gkey, name in names.items():
        g = have.get(gkey)
        if g and g["label"] and not (force and g["label_auto"]):
            continue
        vi = translate_vi(name)
        if vi:
            db.execute("INSERT INTO dy_groups(sec_uid, gkey, label, label_auto) VALUES(?,?,?,1) "
                       "ON CONFLICT(sec_uid, gkey) DO UPDATE SET label=excluded.label, label_auto=1", (sec_uid, gkey, vi))


def _autoname_all():
    """Lúc khởi động: kênh nào chưa có tên tiếng Việt thì tự đặt."""
    try:
        for c in db.rows("SELECT sec_uid FROM dy_channels WHERE label=''"):
            autoname(c["sec_uid"], [])
    except Exception:
        log.exception("autoname")


SINGLES_DIR = "Video lẻ"


def target_path(root: Path, it: dict, aid: str, split: bool, labels: dict, folders: dict | None = None) -> Path:
    """Phim bộ -> <root>/Phim bộ - <tên>/Tap005_<mô tả>_<id>.mp4 ; lẻ -> <root>/Video lẻ/<ngày>_<mô tả>_<id>.mp4"""
    day = datetime.fromtimestamp(it["create_time"]).strftime("%Y-%m-%d") if it.get("create_time") else "0000-00-00"
    desc = safe_name(it.get("desc") or "")
    tail = f"{desc + '_' if desc else ''}{aid}.mp4"
    if not split:
        return root / f"{day}_{tail}"
    if not it.get("grp"):
        return root / SINGLES_DIR / f"{day}_{tail}"
    name = safe_name(labels.get(it["grp"]) or it.get("grp_name") or "", 60) or "chua_ro_ten"
    locked = (folders or {}).get(it["grp"])
    sub = Path(locked) if locked else root / f"Phim bộ - {name}"
    order = it.get("order") or 0
    if it.get("kind") == "comp":
        pre = f"Tap{int(order):03d}z_TuyenTap"            # z -> nằm sau tập cuối mà nó gộp
    elif it.get("ep") is not None:
        pre = f"Tap{int(it['ep']):03d}"
    else:
        pre = f"Tap{int(order):03d}b"                     # phần tiếp không đánh số
    return sub / f"{pre}_{tail}"


def start_scan(url: str, limit: int = 0) -> bool:
    with _lock:
        if _scan.get("running"):
            return False
        _scan.clear()
        _scan.update(running=True, url=url, limit=limit, items=[], nickname="", sec_uid="", total=None, msg="Đang mở Chrome…",
                     error="", has_more=None, started_at=db.now())
    _stop_scan.clear()
    threading.Thread(target=_scan_run, args=(url, limit), name="douyin-scan", daemon=True).start()
    return True


def stop_scan():
    _stop_scan.set()


def _set(**kw):
    with _lock:
        _scan.update(kw)


def _scan_run(url: str, limit: int):
    lock = profile_lock(PROFILE)
    if not lock.acquire(timeout=5):
        _set(running=False, error="Cửa sổ Chrome Douyin đang mở ở việc khác — đóng nó rồi thử lại")
        return
    found: dict[str, dict] = {}
    order: list[str] = []
    state = {"has_more": None, "last": time.time(), "resp": 0, "author": None, "total": None, "logged_in": False}

    def on_response(resp):
        if "/aweme/v1/web/user/profile/other" in resp.url:   # thông tin kênh: tên + tổng số video
            try:
                u = resp.json().get("user") or {}
                state["total"] = u.get("aweme_count")
                state["avatar"] = ((u.get("avatar_thumb") or u.get("avatar_168x168") or {}).get("url_list") or [""])[0]
                if not state["author"] and u.get("nickname"):
                    state["author"] = (u.get("nickname") or "", u.get("sec_uid") or "")
                _set(total=state["total"], nickname=(state["author"] or ("", ""))[0])
            except Exception:
                pass
            return
        if POST_API not in resp.url:
            return
        try:
            data = resp.json()
        except Exception:
            return
        state["resp"] += 1
        state["last"] = time.time()
        state["has_more"] = bool(data.get("has_more"))
        for v in data.get("aweme_list") or []:
            it = _item(v)
            aid = it["aweme_id"]
            if aid not in found:
                order.append(aid)
            found[aid] = it
            _urls[aid] = _video_urls(v)
            if not state["author"]:
                a = v.get("author") or {}
                state["author"] = (a.get("nickname") or "", a.get("sec_uid") or "")

    try:
        with sync_playwright() as pw:
            ctx = launch(pw, PROFILE)
            try:
                state["logged_in"] = _logged_in(ctx)
                _set(logged_in=state["logged_in"])
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.on("response", on_response)
                _set(msg="Đang mở trang kênh…")
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                m = re.search(r"/user/([\w-]+)", page.url)
                if not m:
                    page.wait_for_timeout(3000)
                    m = re.search(r"/user/([\w-]+)", page.url)
                if not m:
                    raise RuntimeError("Link không phải trang kênh Douyin (cần dạng douyin.com/user/... hoặc v.douyin.com/...)")
                _set(sec_uid=m.group(1))

                # Chờ đợt dữ liệu đầu tiên (có thể phải giải captcha/đăng nhập trong cửa sổ Chrome)
                t0 = time.time()
                while not state["resp"] and time.time() - t0 < 180 and not _stop_scan.is_set():
                    waited = int(time.time() - t0)
                    _set(msg="Đang chờ trang tải danh sách video…" + (
                        " Nếu Douyin hiện ô xác minh / đăng nhập, hãy làm trong cửa sổ Chrome." if waited > 10 else ""))
                    page.wait_for_timeout(1000)
                if not state["resp"]:
                    if _stop_scan.is_set():
                        raise RuntimeError("Đã dừng")
                    raise RuntimeError("Không nhận được danh sách video (kênh riêng tư, chưa xác minh, hoặc Douyin đổi giao diện)")

                # Cuộn tới hết danh sách
                stall = 0
                while not _stop_scan.is_set():
                    if limit and len(order) >= limit:
                        break
                    if state["has_more"] is False:
                        break
                    n0, r0 = len(order), state["resp"]
                    _set(msg=f"Đang cuộn lấy danh sách… đã thấy {len(order)} video",
                         nickname=(state["author"] or ("", ""))[0], has_more=state["has_more"],
                         items=[found[a] for a in order])
                    try:
                        page.evaluate("""() => {
                            const li = document.querySelectorAll('[data-e2e="user-post-list"] li, [data-e2e="scroll-list"] li');
                            if (li.length) li[li.length - 1].scrollIntoView({block: 'end'});
                            window.scrollBy(0, 2000);
                            document.querySelectorAll('*').forEach(el => {
                                if (el.scrollHeight > el.clientHeight + 200 && /(auto|scroll)/.test(getComputedStyle(el).overflowY)) el.scrollTop += 2000;
                            });
                        }""")
                        page.mouse.wheel(0, 3000)
                    except Exception:
                        pass
                    t1 = time.time()
                    while state["resp"] == r0 and time.time() - t1 < 6 and not _stop_scan.is_set():
                        page.wait_for_timeout(300)
                    if len(order) == n0 and state["resp"] == r0:
                        stall += 1
                        if stall >= 6:   # ~36 giây không có gì mới
                            break
                    else:
                        stall = 0
                    page.wait_for_timeout(600)
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
        items = [found[a] for a in order]
        if limit:
            items = items[:limit]
        items.sort(key=lambda i: i["create_time"], reverse=True)   # mới nhất trước (video ghim nằm đúng chỗ)
        _classify(_scan.get("sec_uid") or (state["author"] or ("", ""))[1], items)
        for i in items:
            _meta[i["aweme_id"]] = i
        nick, sec = state["author"] or ("", "")
        note = ""
        total = state.get("total") or 0
        if not limit and total and len(items) < total:
            note = (f" / kênh có {total} video. Douyin chỉ cho xem một phần khi CHƯA ĐĂNG NHẬP — bấm \"Đăng nhập Douyin\" rồi lấy lại."
                    if not state.get("logged_in") else f" / kênh có {total} video (phần còn thiếu có thể là bài riêng tư/đã ẩn — hoặc thử lấy lại).")
        elif not limit and state["has_more"] is not False:
            note = " (có thể chưa hết: Douyin ngừng trả thêm — thử lấy lại)"
        _save_channel(_scan.get("sec_uid") or sec, nick, state.get("avatar") or "", state.get("total"), items, full=not limit)
        threading.Thread(target=autoname, args=(_scan.get("sec_uid") or sec, items), daemon=True).start()
        _set(running=False, items=items, nickname=nick, has_more=state["has_more"],
             msg=("Đã dừng — " if _stop_scan.is_set() else "") + f"Lấy xong {len(items)} video" + note, finished_at=db.now())
    except Exception as e:
        log.exception("douyin scan")
        items = [found[a] for a in order]
        items.sort(key=lambda i: i["create_time"], reverse=True)
        _classify(_scan.get("sec_uid") or (state["author"] or ("", ""))[1], items)
        for i in items:
            _meta[i["aweme_id"]] = i
        if items:
            _save_channel(_scan.get("sec_uid") or (state["author"] or ("", ""))[1], (state["author"] or ("", ""))[0],
                          state.get("avatar") or "", state.get("total"), items, full=False)
        _set(running=False, items=items, error=str(e).splitlines()[0][:300],
             nickname=(state["author"] or ("", ""))[0], msg=f"Dừng ở {len(items)} video", finished_at=db.now())
    finally:
        lock.release()


def _listed(c) -> dict:
    """{aweme_id: (create_time, first_seen)}. first_seen = '' -> có từ lần lấy đầu tiên (không tính là MỚI)."""
    out = {}
    for x in json.loads((c or {}).get("listed") or "[]"):
        out[x[0]] = (x[1], x[2] if len(x) > 2 else "")
    return out


def _save_channel(sec_uid, nick, avatar, total, items, full=True):
    """Ghi nhớ kênh. Luôn GỘP với danh sách cũ (lần lấy sau thiếu video cũng không làm mất video đã biết).
    Video xuất hiện lần đầu sau lần lấy đầu tiên -> ghi lúc nhìn thấy (first_seen) -> gắn tag MỚI."""
    if not sec_uid:
        return
    old = db.row("SELECT * FROM dy_channels WHERE sec_uid=?", (sec_uid,))
    known = _listed(old)
    now = db.now()
    for i in items:
        if i["is_image"]:
            continue
        aid = i["aweme_id"]
        fs = known[aid][1] if aid in known else (now if old else "")
        known[aid] = (i["create_time"], fs)
    vids = sorted(([a, t, fs] for a, (t, fs) in known.items()), key=lambda x: -x[1])
    db.execute(
        "INSERT INTO dy_channels(sec_uid, nickname, avatar, total, listed, scanned_at, created_at) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(sec_uid) DO UPDATE SET nickname=COALESCE(NULLIF(excluded.nickname,''), nickname), "
        "avatar=COALESCE(NULLIF(excluded.avatar,''), avatar), total=COALESCE(excluded.total, total), "
        "listed=excluded.listed, scanned_at=excluded.scanned_at",
        (sec_uid, nick or "", avatar or "", total, json.dumps(vids), now, now))


def _done_ids(sec_uid) -> dict:
    return {r["aweme_id"]: r["create_time"] for r in
            db.rows("SELECT aweme_id, create_time FROM dy_videos WHERE sec_uid=? AND status='done'", (sec_uid,))}


def channels() -> list[dict]:
    """Các kênh đã lấy + đã tải tới đâu."""
    out = []
    for c in db.rows("SELECT * FROM dy_channels ORDER BY COALESCE(scanned_at, created_at) DESC"):
        listed = _listed(c)
        c.pop("listed")
        done = _done_ids(c["sec_uid"])
        times = sorted(done.values())
        new = [a for a, (t, fs) in listed.items() if fs and a not in done]
        c.update(
            url=f"https://www.douyin.com/user/{c['sec_uid']}",
            listed=len(listed),
            downloaded=len(done),
            not_downloaded=sum(1 for a in listed if a not in done),
            new_count=len(new),                        # video MỚI (xuất hiện sau lần lấy đầu) chưa tải
            new_since=min((listed[a][1] for a in new), default=None),
            first_dl=times[0] if times else None,      # video cũ nhất đã tải (ngày đăng)
            last_dl=times[-1] if times else None,      # video mới nhất đã tải (ngày đăng)
            newest=max((t for t, _ in listed.values()), default=None),
            last_download_at=(db.row("SELECT MAX(finished_at) m FROM dy_videos WHERE sec_uid=? AND status='done'",
                                     (c["sec_uid"],)) or {}).get("m"),
        )
        out.append(c)
    return out


def set_label(sec_uid: str, label: str):
    db.execute("UPDATE dy_channels SET label=?, label_auto=0 WHERE sec_uid=?", (label.strip()[:80], sec_uid))


def delete_channel(sec_uid: str):
    db.execute("DELETE FROM dy_channels WHERE sec_uid=?", (sec_uid,))


def _logged_in(ctx) -> bool:
    try:
        ok = any(c["name"] in ("sessionid", "sessionid_ss") and c["value"] for c in ctx.cookies("https://www.douyin.com"))
    except Exception:
        return db.get_setting("dy_logged_in", "0") == "1"
    db.set_setting("dy_logged_in", "1" if ok else "0")
    return ok


_login_thread: threading.Thread | None = None


def open_login() -> str:
    """Mở Chrome profile Douyin để người dùng đăng nhập (quét QR). Đóng cửa sổ là xong."""
    global _login_thread
    if _login_thread and _login_thread.is_alive():
        return "already_open"
    lock = profile_lock(PROFILE)
    if not lock.acquire(blocking=False):
        return "busy"

    def _run():
        try:
            with sync_playwright() as pw:
                ctx = launch(pw, PROFILE)
                try:
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    try:
                        page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)
                    except Exception:
                        pass
                    while ctx.pages:
                        _logged_in(ctx)
                        try:
                            ctx.pages[0].wait_for_timeout(1500)
                        except Exception:
                            break
                    _logged_in(ctx)
                finally:
                    try:
                        ctx.close()
                    except Exception:
                        pass
        except Exception:
            log.exception("Lỗi mở Chrome đăng nhập Douyin")
        finally:
            lock.release()

    _login_thread = threading.Thread(target=_run, name="douyin-login", daemon=True)
    _login_thread.start()
    return "opened"


def login_state() -> dict:
    return {"logged_in": db.get_setting("dy_logged_in", "0") == "1",
            "window_open": bool(_login_thread and _login_thread.is_alive())}


# ---------------- Tải video ----------------
def dl_state() -> dict:
    with _lock:
        s = dict(_dl)
        s["items"] = dict(_dl.get("items") or {})
    return s


def start_download(ids: list[str], folder: str, author: str = "", sec_uid: str = "", split: bool = True) -> tuple[bool, str]:
    ids = [i for i in dict.fromkeys(ids) if i in _urls]
    if not ids:
        return False, "Không có video nào để tải (hãy lấy lại danh sách kênh)"
    with _lock:
        if _dl.get("running"):
            return False, "Đang tải đợt khác, chờ xong hoặc bấm Dừng"
        _dl.clear()
        _dl.update(running=True, folder=folder, total=len(ids), done=0, failed=0, skipped=0, current="", folders={}, split=split,
                   items={i: {"st": "wait"} for i in ids}, started_at=db.now())
    _stop_dl.clear()
    if sec_uid:
        db.execute("UPDATE dy_channels SET folder=? WHERE sec_uid=?", (folder, sec_uid))
    threading.Thread(target=_dl_run, args=(ids, Path(folder), author, sec_uid, split), name="douyin-dl", daemon=True).start()
    return True, ""


def stop_download():
    _stop_dl.set()


def _upd(aid, **kw):
    with _lock:
        _dl["items"][aid] = {**_dl["items"].get(aid, {}), **kw}


def _fetch(url: str, dest: Path, aid: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.douyin.com/", "Accept": "*/*"})
    tmp = dest.with_suffix(".part")
    with urllib.request.urlopen(req, timeout=60) as r:
        ctype = r.headers.get("Content-Type", "")
        if "text/html" in ctype or "json" in ctype:
            raise RuntimeError(f"máy chủ trả về {ctype}, không phải video")
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        with tmp.open("wb") as w:
            while True:
                if _stop_dl.is_set():
                    raise InterruptedError("Đã dừng")
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                w.write(chunk)
                got += len(chunk)
                if total:
                    _upd(aid, pct=round(got * 100 / total))
    if got < 50 * 1024:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"file quá nhỏ ({got} byte)")
    if total and got < total:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("tải chưa đủ (mất kết nối)")
    tmp.replace(dest)
    return got


def _save_video(aid, sec_uid, author, it, path, size, status, err):
    db.execute(
        "INSERT INTO dy_videos(aweme_id, sec_uid, author, descr, create_time, path, size, status, error, finished_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(aweme_id) DO UPDATE SET path=COALESCE(excluded.path, dy_videos.path), "
        "size=CASE WHEN excluded.size>0 THEN excluded.size ELSE dy_videos.size END, "
        "status=CASE WHEN dy_videos.status='done' AND excluded.status<>'done' THEN 'done' ELSE excluded.status END, "
        "error=excluded.error, finished_at=excluded.finished_at, author=excluded.author",
        (aid, sec_uid, author, it.get("desc") or "", it.get("create_time") or 0, path, size, status, err, db.now()))


def _lock_after(sec_uid, it, path: Path, split: bool, folders: dict):
    g = it.get("grp")
    if split and g and sec_uid and g not in folders:
        folders[g] = str(path.parent)
        _lock_folder(sec_uid, g, str(path.parent))


def _count_folder(dest: Path):
    with _lock:
        f = _dl.setdefault("folders", {})
        f[str(dest.parent)] = f.get(str(dest.parent), 0) + 1


def _dl_run(ids: list[str], folder: Path, author: str, sec_uid: str, split: bool = True):
    try:
        folder.mkdir(parents=True, exist_ok=True)
        labels = group_labels(sec_uid)
        folders = group_folders(sec_uid)
        for aid in ids:
            if _stop_dl.is_set():
                break
            it = _meta.get(aid, {"desc": "", "create_time": 0})
            dest = target_path(folder, it, aid, split, labels, folders)
            dest.parent.mkdir(parents=True, exist_ok=True)
            old = [f for f in folder.rglob(f"*_{aid}.mp4") if f.stat().st_size > 50 * 1024]
            with _lock:
                _dl["current"] = dest.name
            if old:
                _upd(aid, st="skip", path=str(old[0]), err="đã có trong thư mục")
                with _lock:
                    _dl["skipped"] += 1
                _save_video(aid, sec_uid, author, it, str(old[0]), old[0].stat().st_size, "done", None)
                _count_folder(old[0])
                _lock_after(sec_uid, it, old[0], split, folders)
                continue
            _upd(aid, st="run", pct=0)
            err, size = None, 0
            for u in _urls.get(aid, []):
                try:
                    size = _fetch(u, dest, aid)
                    err = None
                    break
                except InterruptedError:
                    err = "Đã dừng"
                    break
                except Exception as e:
                    err = str(e)[:200]
                    log.info("douyin %s: link lỗi (%s), thử link khác", aid, err)
            if err is None and size:
                _upd(aid, st="done", pct=100, path=str(dest))
                _count_folder(dest)
                _lock_after(sec_uid, it, dest, split, folders)
                with _lock:
                    _dl["done"] += 1
                status = "done"
            else:
                err = err or "không có link tải"
                if "403" in err or "410" in err:
                    err += " — link có thể đã hết hạn, hãy lấy lại danh sách kênh"
                _upd(aid, st="fail", err=err)
                with _lock:
                    _dl["failed"] += 1
                status = "failed"
            _save_video(aid, sec_uid, author, it, str(dest) if status == "done" else None,
                        size, status, None if status == "done" else err)
            time.sleep(0.5)   # đừng tải dồn dập
    except Exception as e:
        log.exception("douyin download")
        with _lock:
            _dl["error"] = str(e)[:300]
    finally:
        with _lock:
            _dl["running"] = False
            _dl["current"] = ""
            _dl["stopped"] = _stop_dl.is_set()
            _dl["finished_at"] = db.now()
