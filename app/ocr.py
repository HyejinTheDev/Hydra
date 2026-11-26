"""Đọc chữ trên video (OCR tiếng Việt) để tự gợi ý tiêu đề.

Video meme thường có dòng chữ tiêu đề ở vài giây đầu -> lấy vài khung hình đầu,
đọc chữ, chọn khối chữ nằm trên cùng có độ tin cậy cao.
"""
import hashlib
import logging
import re
import threading
import warnings

from .config import DATA_DIR

log = logging.getLogger("ocr")
THUMBS_DIR = DATA_DIR / "thumbs"
THUMBS_DIR.mkdir(parents=True, exist_ok=True)

SAMPLE_SECONDS = (0.5, 1.5, 3.0)   # các thời điểm lấy khung hình
MIN_CONF = 0.4                      # bỏ chữ đọc không chắc chắn
MIN_LETTERS = 4                     # bỏ chữ quá ngắn (vd: "Tôi", "Bố" trên nhân vật)

_reader = None
_lock = threading.Lock()
_idle_timer = None
IDLE_UNLOAD_S = 600   # không dùng 10 phút thì giải phóng mô hình (~1GB RAM)


def _unload():
    global _reader
    with _lock:
        if _reader is not None:
            _reader = None
            import gc
            gc.collect()
            log.info("Đã giải phóng mô hình đọc chữ để tiết kiệm RAM")


def _schedule_unload():
    global _idle_timer
    if _idle_timer:
        _idle_timer.cancel()
    _idle_timer = threading.Timer(IDLE_UNLOAD_S, _unload)
    _idle_timer.daemon = True
    _idle_timer.start()


def _get_reader():
    global _reader
    if _reader is None:
        warnings.filterwarnings("ignore")
        import easyocr  # nạp chậm (~10 giây), chỉ khi cần
        log.info("Đang nạp mô hình đọc chữ tiếng Việt...")
        _reader = easyocr.Reader(["vi"], gpu=False, verbose=False)
    return _reader


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text.lstrip("*\"'“”-–• ").strip()


def _top_block(results):
    """Gom các dòng chữ đáng tin, lấy khối nằm trên cùng (các dòng sát nhau)."""
    lines = []
    for box, text, conf in results:
        t = _clean(text)
        if conf < MIN_CONF or sum(ch.isalpha() for ch in t) < MIN_LETTERS:
            continue
        ys = [p[1] for p in box]
        lines.append({"y0": min(ys), "y1": max(ys), "x": min(p[0] for p in box), "text": t, "conf": conf})
    if not lines:
        return None
    lines.sort(key=lambda l: (l["y0"], l["x"]))
    block = [lines[0]]
    for ln in lines[1:]:
        prev = block[-1]
        h = prev["y1"] - prev["y0"]
        if ln["y0"] - prev["y1"] <= max(h * 1.2, 12):   # dòng tiếp theo của cùng đoạn chữ
            block.append(ln)
        else:
            break
    text = " ".join(l["text"] for l in block)
    score = sum(l["conf"] * len(l["text"]) for l in block)
    return {"text": text, "conf": sum(l["conf"] for l in block) / len(block), "score": score}


def thumb_name(video_path: str) -> str:
    return hashlib.md5(video_path.encode("utf-8")).hexdigest()[:16] + ".jpg"


def read_title(video_path: str) -> dict:
    """Trả về {title, conf, thumb}. title rỗng nếu không đọc được chữ nào."""
    import cv2

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError("Không mở được video")
    frames = []
    try:
        for sec in SAMPLE_SECONDS:
            cap.set(cv2.CAP_PROP_POS_MSEC, sec * 1000)
            ok, fr = cap.read()
            if ok:
                h, w = fr.shape[:2]
                scale = 720 / max(w, 1)
                frames.append(cv2.resize(fr, (720, int(h * scale))) if scale < 1 else fr)
    finally:
        cap.release()
    if not frames:
        raise ValueError("Không đọc được khung hình nào")

    best, best_frame = None, frames[0]
    with _lock:   # mỗi lần chỉ đọc 1 video (đỡ tốn RAM/CPU)
        reader = _get_reader()
        for i, fr in enumerate(frames):
            blk = _top_block(reader.readtext(fr))
            if blk and (best is None or blk["score"] > best["score"]):
                best, best_frame = blk, fr
            # Chữ có thể hiện dần từng dòng -> luôn đọc ít nhất 2 khung hình; rõ ràng rồi thì bỏ khung cuối
            if i >= 1 and best and best["conf"] >= 0.85:
                break

    _schedule_unload()
    name = thumb_name(video_path)
    h, w = best_frame.shape[:2]
    cv2.imwrite(str(THUMBS_DIR / name), cv2.resize(best_frame, (180, int(h * 180 / w))),
                [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    return {"title": best["text"][:100] if best else "", "conf": round(float(best["conf"]), 2) if best else 0.0, "thumb": name}
