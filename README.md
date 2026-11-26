# Đăng video đa nền tảng

Tool chạy trên máy, đăng video lên **YouTube, TikTok, Facebook (Reels), Instagram (Reels), Threads** bằng cách tự động hoá Chrome.

## Cách dùng
1. Nhấp đúp `run.bat` (lần đầu sẽ tự cài thư viện). Trình duyệt mở `http://127.0.0.1:8000`.
2. Tab **Tài khoản** → thêm tài khoản → bấm **Đăng nhập** → đăng nhập trong cửa sổ Chrome vừa mở → đóng cửa sổ.
3. Tab **Đăng video** → chọn video (kéo thả / chọn file / quét thư mục) → sửa tiêu đề → chọn tài khoản → Đăng ngay hoặc Hẹn giờ.
4. Tab **Tải Douyin**: dán link kênh → *Lấy danh sách* → chọn video → *Tải* (bấm *Đăng nhập Douyin* 1 lần để lấy được hết video của kênh). Tải xong có nút chuyển thư mục sang tab *Dịch video*.
5. Theo dõi ở tab **Hàng đợi** và **Lịch sử** (có link video, lỗi và ảnh chụp màn hình khi lỗi).

## Lưu ý
- Mỗi tài khoản có một profile Chrome riêng trong `data/profiles/<id>` → dùng được nhiều tài khoản cùng nền tảng.
- Video được đăng lần lượt. Không thao tác vào cửa sổ Chrome đang tự động đăng.
- Phải giữ cửa sổ `run.bat` mở thì video hẹn giờ mới được đăng.
- Tự động hoá trình duyệt phụ thuộc giao diện web của từng nền tảng; khi nền tảng đổi giao diện, cần cập nhật bộ chọn trong `app/platforms/*.py`.
- Đăng quá nhiều/quá dày có thể bị nền tảng hạn chế tài khoản — nên đặt khoảng cách giữa các video.
- Nếu không có Google Chrome, chạy: `.venv\Scripts\python -m playwright install chromium`.

## Cấu trúc
- `app/main.py` – API + phục vụ giao diện web
- `app/worker.py` – luồng nền chạy hàng đợi/hẹn giờ, mở cửa sổ đăng nhập
- `app/douyin.py` – lấy danh sách + tải video kênh Douyin
- `app/platforms/` – kịch bản đăng cho từng nền tảng
- `app/static/index.html` – giao diện
- `data/` – CSDL SQLite, profile Chrome, video, ảnh lỗi
