from . import facebook, instagram, threads, tiktok, youtube

UPLOADERS = {
    "youtube": youtube.upload,
    "tiktok": tiktok.upload,
    "facebook": facebook.upload,
    "instagram": instagram.upload,
    "threads": threads.upload,
}

# Gắn bình luận đầu tiên sau khi đăng (dùng cho affiliate)
COMMENTERS = {
    "facebook": facebook.comment,
    "threads": threads.comment,
}
