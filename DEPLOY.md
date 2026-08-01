- có 2 dự án: Chụp ảnh và crop/phát hiện mờ của ảnh

- Khi chụp ảnh sẽ gọi api của dự án crop ảnh (dự án hiện tại)
  => Vì lượng user/request sử dụng chụp ảnh lớn => cần xử lý performance cho dự án crop ảnh, khả năng chịu tải cao, nếu cần thiết có thể dùng queue để xử lý

## Đã xử lý (Phase 1)

Đã fix nguyên nhân chính khiến server chịu tải kém: route xử lý khai báo
`async def` nhưng gọi code OpenCV/PyMuPDF đồng bộ bên trong, chặn cả event
loop mỗi lần xử lý 1 file. Đã đổi sang `def` (FastAPI tự chạy trong
threadpool), bật multi-worker (`WEB_CONCURRENCY`), và thêm giới hạn tải
(`SCAN_MAX_CONCURRENT_JOBS`, `SCAN_MAX_UPLOAD_MB`) để từ chối có kiểm soát
(503) thay vì crash khi quá tải. Chi tiết + cách tune: xem mục
"Performance / khả năng chịu tải" trong [README.md](README.md).

Chưa làm queue (job_id + polling) vì app chụp ảnh hiện chỉ chấp nhận
request–response đồng bộ. Cân nhắc làm khi cần scale ngang nhiều
container/host, hoặc khi traffic vượt quá khả năng của multi-worker +
backpressure dù đã tune hợp lý.
