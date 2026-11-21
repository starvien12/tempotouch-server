# TempoTouch server trên Render

Gói này chứa máy chủ TempoTouch, bộ xác minh key gắn một thiết bị và các tệp APK phát hành hiện có. Khóa riêng cấp key không nằm trong repo hoặc ZIP.

## Cấu hình Render

- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn --bind 0.0.0.0:$PORT --workers 2 --timeout 120 render_app:application`
- Persistent Disk: gắn tại `/var/data`
- Environment: `TEMPO_LIMIT_DB=/var/data/tempo.sqlite3`

Có thể thêm các biến sau trong Environment của Render.

- `OPENAI_API_KEY` và tùy chọn `OPENAI_MODEL` cho AI OpenAI
- `CODEX86_API_KEY` và tùy chọn `CODEX86_MODEL` cho Codex Gateway 86
- `AI33_TTS_API_KEY` và tùy chọn `AI33_TTS_VOICE_ID` cho TTS AI33
- `TEMPO_LICENSE_PUBLIC_KEY` nếu dùng tệp khóa công khai ở vị trí riêng

Không đưa API key, issuer.pem hoặc khóa riêng Android vào GitHub, ZIP hay APK.

## Cơ chế key

Ứng dụng gửi key do chủ ứng dụng cấp tới `POST /v1/activate`. Key TT1 được gắn ở lần kích hoạt đầu với khóa Android Keystore của thiết bị. Cùng thiết bị có thể kích hoạt lại khi mạng lỗi. Thiết bị khác nhận lỗi 409. Gỡ ứng dụng hoặc đặt lại thiết bị làm mất danh tính Keystore, nên cần key mới.

Máy chủ lưu sổ kích hoạt và hạn mức trong SQLite. Persistent Disk là bắt buộc để ràng buộc một thiết bị tiếp tục có hiệu lực sau khi Render khởi động lại hoặc triển khai lại.

## Các tuyến chính

- `/` kiểm tra máy chủ
- `/releases/latest.json` đọc thông tin bản cập nhật
- `/releases/TempoTouch-N.apk` tải APK phát hành
- `/v1/activate` kích hoạt key gắn thiết bị
- `/v1/chat`, `/v1/plan`, `/v1/action`, `/v1/speech` gọi các chức năng AI tương ứng

## Phát hành APK mới

APK phải được ký bằng đúng kho khóa Android của bản phát hành trước. Dùng công cụ phát hành trong thư mục `server` của dự án Android để cập nhật `releases/latest.json` và đưa APK vào thư mục `releases`. APK debug không được đưa vào feed cập nhật vì khác chữ ký với APK phát hành.

Bản APK hiện có trong gói là bản feed cũ. APK debug 2.17 được tạo riêng tại `app/build/outputs/apk/debug/app-debug.apk` của dự án Android và cần ký release trước khi xuất bản cho người dùng đang cài bản phát hành.
