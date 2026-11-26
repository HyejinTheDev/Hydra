"""Phân loại video Douyin của một kênh: PHIM BỘ (theo phần/tập) và VIDEO LẺ.

Douyin ít khi gắn video vào 合集 (tuyển tập chính thức), nên ngoài mix_info còn đọc TIÊU ĐỀ:
  - "第15集…", "第三集", "…（2）", "…21", "Part 3", "上集/下集"  -> tập số mấy
  - "…1~5合集", "…11~15"                                       -> TUYỂN TẬP gộp tập 1-5 (xếp sau các tập nó gộp)
  - "…后续", "…续集"                                           -> phần tiếp theo
  - Tên phim viết lệch giữa các tập ("抛弃时" / "抛弃后") -> so khớp gần đúng để gộp chung.
  - Video không đánh số nhưng tiêu đề trùng tên phim (thường là tập đầu) -> gộp vào phim đó.
"""
import re
from difflib import SequenceMatcher

CN = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn2int(s: str):
    if s.isdigit():
        return int(s)
    if not s or any(ch not in CN and ch not in "十百" for ch in s):
        return None
    n, cur = 0, 0
    for ch in s:
        if ch == "百":
            n += (cur or 1) * 100; cur = 0
        elif ch == "十":
            n += (cur or 1) * 10; cur = 0
        else:
            cur = CN[ch]
    return n + cur


def clean_title(desc: str) -> str:
    t = (desc or "").split("\n")[0]
    t = re.sub(r"原创\s*@\S*", " ", t)
    t = re.sub(r"[#@]\S*", " ", t)
    return re.sub(r"\s+", " ", t).strip()


EDGE = " \t:：,，、.。!！?？~～-—–_|·…\"'“”‘’《》【】[]()（）"
NUM = r"[0-9一二三四五六七八九十百两]+"
R_RANGE = re.compile(r"(?:第\s*)?(\d{1,3})\s*[~～\-—–到至]\s*(\d{1,3})\s*(?:集|期|话|章)?\s*(?:合集|合辑|全集)?\s*$")
R_DI = re.compile(r"第\s*(" + NUM + r")\s*[集期话話回章篇季部]")
R_PART = re.compile(r"(?i)\b(?:part|ep|tập)\s*\.?\s*(\d{1,3})\b")
R_BRACKET = re.compile(r"[（(【\[]\s*(\d{1,3})\s*[)）】\]]\s*$")
R_TAIL = re.compile(r"(?<![0-9.])(\d{1,3})\s*$")
R_UPDOWN = re.compile(r"([上中下])\s*[集篇部]\s*$")
R_SEQ = re.compile(r"(后续|後續|续集|續集|续篇|下集)\s*$")
UPDOWN = {"上": 1, "中": 2, "下": 3}


def norm(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", s or "").lower()


def parse(title: str) -> dict:
    """-> {name, kind: ep|comp|seq|none, ep, a, b, strong}"""
    t = title.strip()
    m = R_RANGE.search(t)
    if m and int(m.group(1)) < int(m.group(2)):
        return dict(name=t[:m.start()].strip(EDGE), kind="comp", a=int(m.group(1)), b=int(m.group(2)), ep=None, strong=True)
    m = R_DI.search(t)
    if m and cn2int(m.group(1)) is not None:
        before, after = t[:m.start()].strip(EDGE), t[m.end():].strip(EDGE)
        return dict(name=before, kind="ep", ep=cn2int(m.group(1)), strong=True, subtitle=after)
    m = R_PART.search(t)
    if m:
        return dict(name=(t[:m.start()] + " " + t[m.end():]).strip(EDGE), kind="ep", ep=int(m.group(1)), strong=True)
    m = R_UPDOWN.search(t)
    if m:
        return dict(name=t[:m.start()].strip(EDGE), kind="ep", ep=UPDOWN[m.group(1)], strong=True)
    m = R_SEQ.search(t)
    if m:
        return dict(name=t[:m.start()].strip(EDGE), kind="seq", ep=None, strong=True)
    for rx in (R_BRACKET, R_TAIL):
        m = rx.search(t)
        if m and 0 < int(m.group(1)) < 300:
            name = t[:m.start()].strip(EDGE)
            if len(norm(name)) >= 4:
                return dict(name=name, kind="ep", ep=int(m.group(1)), strong=False)
    return dict(name=t.strip(EDGE), kind="none", ep=None, strong=False)


def similar(a: str, b: str) -> bool:
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) < 4:
        return False
    sm = SequenceMatcher(None, a, b)
    if sm.ratio() >= 0.8:
        return True
    # tên ngắn nằm gần trọn trong tên dài ("当我能看到见面次数时" ~ "当我能看到和别人剩下的见面次数时")
    matched = sum(bl.size for bl in SequenceMatcher(None, short, long_).get_matching_blocks())
    return len(short) >= 5 and matched / len(short) >= 0.9 and len(short) / len(long_) >= 0.5


def classify(items: list[dict]) -> list[dict]:
    """Gắn cho mỗi item: grp (khoá nhóm, '' = video lẻ), grp_name, kind, ep, ep_label, order.
    items cần: aweme_id, desc, create_time, (tuỳ chọn) mix_id, mix_name, mix_ep."""
    groups: list[dict] = []   # {key, name, names:[...], members:[item]}

    def find(name):
        for g in groups:
            if any(similar(name, n) for n in g["names"]):
                return g
        return None

    # 1) 合集 chính thức của Douyin
    for it in items:
        it["_p"] = parse(clean_title(it.get("desc")))
        if it.get("mix_id"):
            key = "mix:" + str(it["mix_id"])
            g = next((x for x in groups if x["key"] == key), None)
            if not g:
                mname = re.sub(r"(合集|合辑|全集)$", "", (it.get("mix_name") or "").strip()).strip(EDGE)
                g = dict(key=key, name=mname or it.get("mix_name") or "合集", names=[mname] if mname else [], members=[])
                groups.append(g)
            if it["_p"]["name"] and it["_p"]["name"] not in g["names"]:
                g["names"].append(it["_p"]["name"])
            g["members"].append(it)

    # 2) đánh số / tuyển tập / phần tiếp trong tiêu đề
    for it in items:
        p = it["_p"]
        if it.get("mix_id") or p["kind"] == "none":
            continue
        if not p["name"]:   # "第15集：…" không có tên phim -> nhóm chung "chưa rõ tên"
            g = next((x for x in groups if x["key"] == "t:__noname"), None)
            if not g:
                g = dict(key="t:__noname", name="Phim bộ chưa rõ tên", names=[], members=[], fixed=True)
                groups.append(g)
            g["members"].append(it)
            continue
        g = find(p["name"])
        if not g:
            g = dict(key="t:" + norm(p["name"])[:40], name=p["name"], names=[p["name"]], members=[])
            groups.append(g)
        elif p["name"] not in g["names"]:
            g["names"].append(p["name"])
        g["members"].append(it)

    # 3) video không đánh số nhưng tiêu đề trùng tên phim (thường là tập đầu)
    for it in items:
        p = it["_p"]
        if it.get("mix_id") or p["kind"] != "none" or len(norm(p["name"])) < 5:
            continue
        g = find(p["name"])
        if g and len(g["members"]) >= 1:
            g["members"].append(it)

    # 4) nhóm chỉ có 1 video và chỉ dựa vào số cuối tiêu đề (dễ nhầm) -> coi là video lẻ
    keep = []
    for g in groups:
        if len(g["members"]) == 1 and not g["key"].startswith("mix:") and not g["members"][0]["_p"]["strong"]:
            continue
        keep.append(g)

    for it in items:
        it.update(grp="", grp_name="", kind="single", ep=None, ep_label="Lẻ", order=-(it.get("create_time") or 0))
    for g in keep:
        _number(g)
    for it in items:
        it.pop("_p", None)
    return items


def _number(g: dict):
    mem = sorted(g["members"], key=lambda i: i.get("create_time") or 0)
    # tên hiển thị: tên xuất hiện nhiều nhất trong các tập
    if not g["key"].startswith("mix:") and not g.get("fixed"):
        names = [i["_p"]["name"] for i in mem if i["_p"]["name"]]
        if names:
            g["name"] = max(set(names), key=lambda n: (names.count(n), -len(n)))
    taken = {i.get("mix_ep") or i["_p"].get("ep") for i in mem} - {None}
    last = None
    for it in mem:
        p = it["_p"]
        ep = it.get("mix_ep") or p.get("ep")
        kind = "comp" if p["kind"] == "comp" else "ep"
        if kind == "comp":
            order, label = p["b"] + 0.5, f"Tuyển tập {p['a']}–{p['b']}"
            ep = None
        else:
            if ep is None:   # không đánh số: tập đầu (nếu là video sớm nhất) hoặc phần tiếp của tập trước
                cand = 1 if last is None else last + 1
                ep = cand if cand not in taken else None
                if ep is not None:
                    taken.add(ep)
            label = f"Tập {ep}" if ep is not None else "Phần tiếp"
            order = float(ep) if ep is not None else (last or 0) + 0.25
            if ep is not None:
                last = ep
        it.update(grp=g["key"], grp_name=g["name"], kind=kind, ep=ep, ep_label=label, order=order)
