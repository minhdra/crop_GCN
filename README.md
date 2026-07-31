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
