# py-project

Crop và làm rõ PDF / ảnh theo kiểu scanner. Có sẵn CLI, FastAPI và Docker Compose.

Nếu không tìm thấy đủ bốn góc tài liệu, chương trình giữ nguyên toàn trang/ảnh
và vẫn làm rõ nội dung. Các tham số xử lý: `mode` (`color`/`scan`/`bw`),
`rotate` (`0`/`90`/`180`/`270`), `sharpness`, `min_area_ratio`, `crop`
(bật/tắt tìm viền), và với PDF thêm `dpi`, `jpeg_quality`.

Ngoài ra, mỗi trang/ảnh còn được đánh giá chất lượng (không làm hỏng việc xử
lý, chỉ gắn cờ cảnh báo):

- **Ảnh bị mờ** — dựa trên độ nét (variance of Laplacian) của ảnh gốc, chỉnh
  bằng `blur_threshold` (mặc định `100.0`, điểm càng thấp càng dễ bị coi là mờ).
- **Tài liệu nghi bị nát/rách** — dựa trên độ "đặc" (solidity = diện tích viền
  giấy / diện tích convex hull) của contour giấy tìm được trước khi crop,
  chỉnh bằng `solidity_threshold` (mặc định `0.85`, càng thấp càng dễ bị coi
  là rách). Đây là các heuristic, có thể cần tinh chỉnh theo dữ liệu thực tế.

### Debug crop

Bật `debug` (CLI: `--debug`, API: `debug=true`) để xuất thêm một ảnh overlay
vẽ lên ảnh gốc (trước crop): contour vùng giấy tìm được (viền xanh dương), 4
góc đã chọn để crop nếu có (viền xanh lá + chấm đỏ ở góc), và các chỉ số
`blur_score`/`solidity` dạng chữ ở góc trên trái. Dùng để soi vì sao một
ảnh/trang không crop được hoặc bị gắn cờ mờ/nát.

## API (FastAPI)

Chạy server:

```bash
source .venv/bin/activate
uvicorn py_project.api:app --reload
# hoặc: scan-api
```

Xem tài liệu tương tác tại http://127.0.0.1:8000/docs

### Xử lý một file

```bash
curl -X POST http://127.0.0.1:8000/api/scan \
  -F "file=@input.pdf" \
  -F "mode=scan" \
  -F "rotate=0"
```

Trả về trạng thái xử lý kèm `download_url` nếu thành công. `status` là
`success` (không có vấn đề gì), `warning` (xử lý thành công nhưng phát hiện
mờ/nghi bị nát — vẫn có file kết quả để tải), hoặc `error` (xử lý thất bại,
không có file kết quả):

```json
{
  "filename": "input.pdf",
  "status": "warning",
  "warnings": ["Trang 1 bị mờ", "Trang 2 nghi bị nát/rách"],
  "total_pages": 2,
  "cropped_pages": 1,
  "blurry_pages": 1,
  "damaged_pages": 1,
  "blurry_page_numbers": [1],
  "damaged_page_numbers": [2],
  "download_url": "/api/download/<job_id>",
  "view_url": "/api/view/<job_id>",
  "saved_path": "/duong/dan/output_scans/<job_id>_input.pdf",
  "debug_url": null,
  "debug_page_count": null
}
```

`blurry_page_numbers`/`damaged_page_numbers` đánh số trang từ 1, cho biết
chính xác trang nào bị gắn cờ. Với ảnh lẻ, thay vì các trường theo trang, kết
quả có `cropped`, `is_blurry`, `is_damaged`.

Gọi với `-F "debug=true"` để có thêm `debug_url` (ảnh overlay contour + góc
crop, xem mục [Debug crop](#debug-crop) ở trên). Với PDF, `debug_page_count`
cho biết số trang có ảnh debug; lấy từng trang bằng `?page=N` (mặc định 1):

```bash
curl "http://127.0.0.1:8000/api/debug/<job_id>?page=2"
```

Tải file kết quả (buộc trình duyệt lưu về máy):

```bash
curl -OJ http://127.0.0.1:8000/api/download/<job_id>
```

Xem trực tiếp (hiển thị inline — dùng làm link ảnh để nhúng `<img src="...">`
hoặc mở nhanh trên trình duyệt):

```
http://127.0.0.1:8000/api/view/<job_id>
```

Ngoài ra, mỗi kết quả còn được lưu thêm một bản vào thư mục `output_scans/`
(ở gốc project) với tên `<job_id>_<tên_file_gốc>` để đối soát nhanh trực
tiếp trên ổ đĩa, không cần gọi API. `saved_path` trong response cho biết
chính xác file đó nằm ở đâu. Đổi thư mục này qua biến môi trường
`SCAN_OUTPUT_DIR`.

### Xử lý nhiều file

```bash
curl -X POST http://127.0.0.1:8000/api/scan/batch \
  -F "files=@a.pdf" -F "files=@b.jpg" \
  -F "mode=bw"
```

Trả về danh sách trạng thái, mỗi file có `status`, `message` (nếu lỗi) và
`download_url` (nếu thành công) riêng — một file lỗi không làm hỏng các file
còn lại trong lô.

### ⚠️ Không có xác thực (auth)

API hiện không yêu cầu API key hay đăng nhập — bất kỳ ai truy cập được vào
cổng (port) đang chạy đều có thể upload và xử lý file. Chỉ chạy trên máy/mạng
tin cậy. Mặc định trong `docker-compose.yml`, cổng chỉ được bind vào
`127.0.0.1` (localhost) để tránh lộ ra cả mạng LAN — **không** đổi thành
`0.0.0.0`/expose ra internet nếu chưa thêm xác thực và/hoặc reverse proxy.

## Docker

Chạy bằng Docker Compose (build image, expose cổng `8000` chỉ trên
localhost, mount thư mục `output_scans/` ra ngoài host để đối soát trực
tiếp):

```bash
docker compose up -d --build
```

Kiểm tra:

```bash
curl http://127.0.0.1:8000/health
curl -X POST http://127.0.0.1:8000/api/scan -F "file=@input_pdf/1.jpeg"
```

Xem log / dừng:

```bash
docker compose logs -f
docker compose down
```

Có thể đổi thư mục lưu kết quả trên host qua biến môi trường trong
`docker-compose.yml` (`SCAN_OUTPUT_DIR`, mặc định `/app/output_scans` bên
trong container, mount từ `./output_scans` trên host).

## Cấu hình qua biến môi trường

Toàn bộ biến môi trường (dọn dẹp tự động, giới hạn tải...) được liệt kê kèm
mô tả đầy đủ trong [`.env.example`](.env.example). Trước khi chạy local
hoặc qua Docker, copy file này thành `.env` rồi chỉnh nếu cần:

```bash
cp .env.example .env
```

`.env` được `docker compose` tự nạp vào container (`env_file` trong
`docker-compose.yml`), và app cũng tự đọc khi chạy trực tiếp qua
`uvicorn`/`scan-api` (nhờ `python-dotenv`) - không cần export tay. Biến đã
export sẵn trong shell hoặc đặt trong `docker-compose.yml` luôn được ưu
tiên hơn giá trị trong `.env`. File `.env` không chứa thông tin nhạy cảm
(API hiện không có auth) và đã được thêm vào `.gitignore`.

## Performance / khả năng chịu tải

API xử lý ảnh/PDF bằng OpenCV + PyMuPDF, tốn CPU đáng kể mỗi request. Ba
điều chỉnh sau giúp server chịu tải tốt hơn khi lượng request lớn (ví dụ từ
app chụp ảnh gọi vào):

1. **Route xử lý khai báo `def` (không phải `async def`)** — vì code xử lý
   ảnh hoàn toàn đồng bộ, không hỗ trợ `await`. Với `async def`, một request
   đang xử lý sẽ chiếm toàn bộ event loop, khiến mọi request khác (kể cả
   `/health`) phải chờ. Với `def`, FastAPI tự chạy trong threadpool riêng,
   không chặn các request khác.
2. **Nhiều uvicorn worker process** qua biến môi trường `WEB_CONCURRENCY`
   (mặc định `2`) — cho phép tận dụng nhiều CPU core thật sự (multi-process,
   không bị giới hạn bởi GIL như multi-thread). Nên đặt bằng số CPU core cấp
   cho container.
3. **Giới hạn tải (backpressure)** qua hai biến môi trường:
   - `SCAN_MAX_CONCURRENT_JOBS` (mặc định `4`): số job xử lý đồng thời tối
     đa **trên mỗi worker process**. Vượt quá, request mới bị từ chối ngay
     bằng `503` (kèm header `Retry-After`) thay vì xếp hàng vô hạn định và
     làm chậm dần mọi request. Tổng số job đồng thời thực tế trên toàn
     server ≈ `WEB_CONCURRENCY` × `SCAN_MAX_CONCURRENT_JOBS`.
   - `SCAN_MAX_UPLOAD_MB` (mặc định `50`): kích thước file upload tối đa
     mỗi file, chặn sớm trước khi tốn CPU xử lý file quá khổ.
4. **Dọn dẹp tự động** file tạm và bản lưu lâu dài, tránh phình đĩa vô hạn
   theo thời gian server chạy — `SCAN_STORAGE_TTL_HOURS` (mặc định `24`),
   `SCAN_OUTPUT_TTL_DAYS` (mặc định `30`), `SCAN_CLEANUP_INTERVAL_MINUTES`
   (mặc định `60`). Chi tiết xem [`.env.example`](.env.example).

### Giới hạn hiện tại / khi nào cần queue

Cách trên vẫn là kiến trúc **request–response đồng bộ, single-host**: client
gọi `/api/scan` và chờ kết quả trả về ngay trong 1 request; `STORAGE_DIR`/
`OUTPUT_DIR` dùng filesystem cục bộ của container. Việc này phù hợp khi app
gọi vào vẫn chấp nhận chờ đồng bộ.

Nếu sau này cần **scale ngang nhiều container/host**, hoặc traffic vượt quá
khả năng của multi-worker + backpressure (client bị timeout dù đã tăng
`WEB_CONCURRENCY`/`SCAN_MAX_CONCURRENT_JOBS` hợp lý), nên cân nhắc chuyển
sang mô hình hàng đợi (queue): API trả về `job_id` ngay (202), worker riêng
xử lý nền, client poll trạng thái — khi đó cần thêm hạ tầng (Redis/DB cho
job queue + trạng thái) và đổi cách gọi API sang polling, đồng thời chuyển
storage sang nơi dùng chung (S3/MinIO...) thay vì đĩa cục bộ.

## CLI

```bash
python batch_scan.py input.pdf output_scan.pdf --mode scan --dpi 250
python batch_scan.py photo.jpg scan_result.jpg --mode bw
python batch_scan.py ./mobile_photos ./scanned_photos --sharpness 1.2

# Xuất thêm ảnh debug (contour + góc crop) cạnh output
python batch_scan.py input.pdf output_scan.pdf --debug
# -> output_scan_debug/page_0001.jpg, page_0002.jpg, ...
python batch_scan.py photo.jpg scan_result.jpg --debug
# -> scan_result_debug.jpg
```

## Test

```bash
python -m pytest
```
# Crop_GCN
