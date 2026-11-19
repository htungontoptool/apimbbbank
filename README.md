# MB Bank Telegram Bot — Render

Bot Telegram cá nhân để theo dõi số dư và biến động tài khoản MB Bank. Bot chỉ phản hồi Telegram ID được khai báo trong `OWNER_TELEGRAM_ID`.

## Cấu trúc GitHub

Toàn bộ file nằm ngay tại thư mục gốc repository. Không cần tạo thư mục con và không có ZIP SDK lồng bên trong. Các file `mbbank__*.py` là SDK MB Bank đã được tách phẳng; `main.py` nạp chúng bằng import hook để giữ nguyên các import nội bộ.

## 1. Tạo bot Telegram

1. Mở Telegram, tìm `@BotFather`.
2. Gửi `/newbot`, đặt tên và username cho bot.
3. Sao chép token BotFather cấp. Không gửi token cho người khác và không đưa token vào GitHub.
4. Lấy Telegram ID cá nhân bằng bot lấy ID đáng tin cậy hoặc API Telegram; nhập ID số vào Render.

## 2. Đưa mã nguồn lên GitHub

1. Đăng nhập GitHub và chọn **New repository**.
2. Đặt tên, ví dụ `mbbank-telegram-bot`; nên chọn **Private**.
3. Tạo repository trống.
4. Mở repository → **Add file** → **Upload files**.
5. Giải nén gói ZIP tải về trên máy/điện thoại.
6. Chọn tất cả file và file ẩn `.python-version` trong thư mục vừa giải nén, tải trực tiếp lên trang gốc repository. Không tải chính file ZIP lên GitHub.
7. Kéo xuống → **Commit changes**.
8. Sau khi tải xong, ở trang gốc phải thấy `main.py`, `requirements.txt`, `render.yaml`, `.python-version`, `README.md`, `MB_BANK_SDK_LICENSE` và nhiều file `mbbank__*.py`. Không có thư mục con, không có `mbbank_sdk.zip`.

**Lưu ý khi dùng iPhone:** giải nén ZIP trong ứng dụng Tệp (Files), mở thư mục vừa giải nén, chọn các file rồi chia sẻ/tải lên GitHub bằng trình duyệt. Nếu giao diện GitHub trên điện thoại không cho chọn nhiều file hoặc file ẩn, hãy bật yêu cầu trang web cho máy tính hoặc dùng máy tính. Đảm bảo `.python-version` được tải lên.

## 3. Tạo dịch vụ trên Render

1. Mở https://render.com và đăng nhập.
2. Chọn **New +** → **Web Service**.
3. Kết nối GitHub nếu Render hỏi quyền truy cập; chỉ cấp quyền repository cần triển khai.
4. Chọn repository `mbbank-telegram-bot` vừa tải lên.
5. Cấu hình:
   - **Name:** `mbbank-telegram-assistant`
   - **Runtime:** Python 3
   - **Branch:** `main` (hoặc nhánh bạn đang dùng)
   - **Root Directory:** để trống
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python main.py`
   - **Instance Type:** chọn gói phù hợp. Free có thể ngủ khi không có truy cập và không phù hợp để đảm bảo bot polling liên tục.
6. Trong **Environment** thêm:
   - `BOT_TOKEN` = token lấy từ BotFather
   - `OWNER_TELEGRAM_ID` = Telegram ID dạng số của bạn
   - `POLL_INTERVAL_SECONDS` = `30`
   - `HISTORY_DAYS` = `3`
   - `CAPTCHA_TIMEOUT_SECONDS` = `240`
   - `DATA_DIR` = `data`
7. Không thêm dấu ngoặc kép quanh giá trị. Không đưa mật khẩu MB Bank, OTP, CAPTCHA hay token bot vào file mã nguồn/GitHub.
8. Chọn **Create Web Service** và theo dõi tab **Logs**. Đợi build và deploy hoàn tất, kiểm tra log không có `ModuleNotFoundError` hoặc lỗi khởi động.
9. Mở URL Render được cấp, thêm `/health` ở cuối. Nếu thấy phản hồi trạng thái OK thì web service đã khởi động. Sau đó mở bot Telegram và gửi `/start`.

## 4. Cập nhật code

Mỗi lần sửa code: tải file mới lên GitHub và **Commit changes**. Nếu Auto-Deploy đang bật, Render tự deploy lại; nếu không, vào Render → **Manual Deploy** → **Deploy latest commit**.

## 5. Lưu ý quan trọng

- Không commit `.env`, token Telegram, mật khẩu MB Bank, dữ liệu phiên đăng nhập hoặc file SQLite.
- Render Free có thể spin down; không đảm bảo kiểm tra ngân hàng mỗi 30 giây 24/7. Muốn chạy liên tục cần dịch vụ không sleep/worker phù hợp.
- Hệ thống file của Render có thể không bền vững khi deploy/restart. Nếu cần giữ lịch sử/dữ liệu SQLite, cấu hình Persistent Disk và trỏ `DATA_DIR` vào đường dẫn mount theo cấu hình Render của bạn.
- CAPTCHA được xử lý theo khả năng SDK; không cam kết vượt được OTP/xác minh bổ sung. Nếu MB Bank yêu cầu xác minh ngoài luồng hỗ trợ, đăng nhập qua kênh chính thức.
- Bot chỉ dành cho chủ sở hữu đã cấu hình ID và không có chức năng chuyển tiền.
- Đây là mã nguồn tham khảo tích hợp SDK bên thứ ba. Kiểm tra điều khoản MB Bank và giấy phép SDK trước khi sử dụng.
