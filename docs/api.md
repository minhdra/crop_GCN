# Tài liệu API — Document Scanner

API xử lý crop và làm rõ PDF / ảnh theo kiểu scanner, xây bằng FastAPI.

- Base URL mặc định: `http://127.0.0.1:8090`
- Docs tương tác (Swagger UI) tự sinh: `http://127.0.0.1:8090/docs`
- Không có xác thực (auth) — xem cảnh báo ở cuối tài liệu.

## Danh sách endpoint

| Method | Path | Mô tả |
|---|---|---|
| GET | `/health` | Kiểm tra server còn sống |
| POST | `/api/scan` | Xử lý một file (PDF hoặc ảnh) |
| POST | `/api/scan/batch` | Xử lý nhiều file cùng lúc |
| GET | `/api/download/{job_id}` | Tải file kết quả (attachment) |
| GET | `/api/view/{job_id}` | Xem trực tiếp file kết quả (inline) |
| GET | `/api/debug/{job_id}?page=N` | Xem ảnh debug (contour + góc crop) |

---

## GET `/health`

Kiểm tra server đang chạy.

**Response** `200 OK`
```json
{ "status": "ok" }
```

---

## POST `/api/scan`

Xử lý một file PDF hoặc ảnh, trả về trạng thái xử lý kèm đường dẫn tải kết quả.

### Đầu vào

Gửi dạng `multipart/form-data`.

| Field | Kiểu | Bắt buộc | Mặc định | Mô tả |
|---|---|---|---|---|
| `file` | file | ✅ | — | File PDF hoặc ảnh cần xử lý. Định dạng ảnh hỗ trợ: `.jpg`, `.jpeg`, `.png`, `.bmp`, `.tiff`, `.webp`, `.heic`. Ngoài ra hỗ trợ `.pdf`. |
| `mode` | string enum | ❌ | `color` | Chế độ xử lý màu đầu ra: `color` (giữ nguyên màu), `scan` (tăng tương phản kiểu máy scan), `bw` (đen trắng). |
| `rotate` | int | ❌ | `0` | Góc xoay ảnh/trang theo chiều kim đồng hồ (độ). Chỉ nhận `0`, `90`, `180`, `270`. |
| `sharpness` | float | ❌ | `0.7` | Độ làm nét (unsharp mask), khoảng `0`–`3`. `0` = không làm nét, càng cao càng nét. |
| `min_area_ratio` | float | ❌ | `0.2` | Tỷ lệ diện tích tối thiểu (khoảng `0`–`1`, không gồm 2 đầu mút) của vùng giấy so với toàn ảnh để được coi là tài liệu cần crop. |
| `crop` | bool | ❌ | `true` | Có tự động tìm và crop viền giấy hay không. `false` = giữ nguyên khung ảnh gốc (vẫn làm rõ nội dung). |
| `dpi` | int | ❌ | `200` | Độ phân giải khi render trang PDF thành ảnh trước khi xử lý, khoảng `72`–`600`. Chỉ áp dụng cho PDF. |
| `jpeg_quality` | int | ❌ | `92` | Chất lượng nén JPEG đầu ra, khoảng `1`–`100`. Càng cao càng nét nhưng dung lượng càng lớn. |
| `blur_threshold` | float | ❌ | `100.0` | Ngưỡng độ nét (variance of Laplacian, `≥0`) để gắn cờ `is_blurry`. Điểm ảnh thấp hơn ngưỡng này bị coi là mờ. |
| `solidity_threshold` | float | ❌ | `0.85` | Ngưỡng độ đặc (solidity, khoảng `0`–`1`) của viền giấy để gắn cờ `is_damaged`. Solidity thấp hơn ngưỡng này bị nghi là rách/nát. |
| `debug` | bool | ❌ | `false` | Nếu `true`, sinh thêm ảnh debug (contour vùng giấy, 4 góc crop, chỉ số `blur_score`/`solidity`) — xem qua `/api/debug/{job_id}`. |

Nếu không tìm đủ 4 góc tài liệu, chương trình giữ nguyên toàn trang/ảnh và vẫn làm rõ nội dung (không lỗi).

**Ví dụ request:**
```bash
curl -X POST http://127.0.0.1:8090/api/scan \
  -F "file=@input.pdf" \
  -F "mode=scan" \
  -F "rotate=0"
```

### Đầu ra

**Response** `200 OK`, body kiểu `ScanResult`:

| Field | Kiểu | Mô tả |
|---|---|---|
| `filename` | string | Tên file gốc đã upload. |
| `status` | `"success"` \| `"warning"` \| `"error"` | `success`: không vấn đề gì. `warning`: xử lý thành công nhưng phát hiện mờ/nghi rách — vẫn có file kết quả. `error`: xử lý thất bại, không có file kết quả. |
| `message` | string \| null | Thông báo lỗi (chỉ có khi `status = "error"`). |
| `warnings` | string[] | Danh sách cảnh báo chất lượng dạng câu, ví dụ `"Trang 1 bị mờ"`. |
| `total_pages` | int \| null | Tổng số trang — **chỉ có với PDF**. |
| `cropped_pages` | int \| null | Số trang crop được viền giấy thành công — **chỉ PDF**. |
| `blurry_pages` | int \| null | Số trang bị gắn cờ mờ — **chỉ PDF**. |
| `damaged_pages` | int \| null | Số trang nghi bị nát/rách — **chỉ PDF**. |
| `blurry_page_numbers` | int[] \| null | Số thứ tự (bắt đầu từ 1) các trang bị mờ — **chỉ PDF**. |
| `damaged_page_numbers` | int[] \| null | Số thứ tự (bắt đầu từ 1) các trang nghi rách — **chỉ PDF**. |
| `cropped` | bool \| null | Có crop được viền giấy hay không — **chỉ ảnh lẻ** (không phải PDF). |
| `is_blurry` | bool \| null | Ảnh có bị mờ hay không — **chỉ ảnh lẻ**. |
| `is_damaged` | bool \| null | Ảnh có nghi bị nát/rách hay không — **chỉ ảnh lẻ**. |
| `download_url` | string \| null | Đường dẫn tải file kết quả (`/api/download/<job_id>`), null nếu lỗi. |
| `view_url` | string \| null | Đường dẫn xem trực tiếp file kết quả (`/api/view/<job_id>`), null nếu lỗi. |
| `saved_path` | string \| null | Đường dẫn tuyệt đối trên đĩa server nơi lưu bản sao kết quả (thư mục `output_scans/`), null nếu lỗi. |
| `debug_url` | string \| null | Đường dẫn xem ảnh debug (`/api/debug/<job_id>`) — chỉ có khi gọi với `debug=true` và có sinh được ảnh debug. |
| `debug_page_count` | int \| null | Số trang có ảnh debug — **chỉ PDF** khi `debug=true`. |
| `processing_time_seconds` | float \| null | Thời gian xử lý **thật trên server** (giây) — chỉ tính thời gian chạy `scan_pdf`/`scan_image`, không tính thời gian upload file lên hay round-trip mạng. `null` nếu xử lý lỗi trước khi bắt đầu. |

Lưu ý: các field theo trang (`total_pages`, `cropped_pages`, `blurry_pages`, `damaged_pages`, `blurry_page_numbers`, `damaged_page_numbers`) chỉ có giá trị khi input là PDF; ngược lại (input là ảnh lẻ) dùng `cropped`/`is_blurry`/`is_damaged`. Trường không áp dụng sẽ là `null`.

**Ví dụ response (PDF, có cảnh báo):**
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
  "debug_page_count": null,
  "processing_time_seconds": 1.842
}
```

**Ví dụ response (lỗi):**
```json
{
  "filename": "input.txt",
  "status": "error",
  "message": "Định dạng file không được hỗ trợ: .txt",
  "warnings": []
}
```

### Mã lỗi HTTP khác

| Status | Khi nào |
|---|---|
| `422` | `rotate` không thuộc `{0, 90, 180, 270}`, hoặc thiếu file/field bắt buộc, hoặc giá trị field ngoài khoảng cho phép. |
| `503` | Server đang quá tải (vượt `SCAN_MAX_CONCURRENT_JOBS` và chờ quá `SCAN_QUEUE_TIMEOUT_SECONDS`). Kèm header `Retry-After` (giây) — nên thử lại sau. |

Lưu ý: lỗi xử lý file riêng lẻ (định dạng sai, file hỏng, quá kích thước...) **không** trả HTTP lỗi — vẫn trả `200 OK` với `status: "error"` và `message` mô tả nguyên nhân trong body.

---

## POST `/api/scan/batch`

Xử lý nhiều file cùng lúc, mỗi file có kết quả riêng — một file lỗi không ảnh hưởng các file còn lại.

### Đầu vào

Cũng dạng `multipart/form-data`, các field xử lý giống hệt `/api/scan` (`mode`, `rotate`, `sharpness`, `min_area_ratio`, `crop`, `dpi`, `jpeg_quality`, `blur_threshold`, `solidity_threshold`, `debug` — áp dụng chung cho toàn bộ các file trong batch), khác ở field file:

| Field | Kiểu | Bắt buộc | Mặc định | Mô tả |
|---|---|---|---|---|
| `files` | file[] | ✅ | — | Danh sách file PDF/ảnh cần xử lý (gửi nhiều field `files` cùng tên). |
| `mode` | string enum | ❌ | `scan` | Giống `/api/scan` (lưu ý mặc định batch là `scan`, không phải `color`). |

Các field còn lại (`rotate`, `sharpness`, `min_area_ratio`, `crop`, `dpi`, `jpeg_quality`, `blur_threshold`, `solidity_threshold`, `debug`) có cùng kiểu/mặc định/ràng buộc như `/api/scan`.

**Ví dụ request:**
```bash
curl -X POST http://127.0.0.1:8090/api/scan/batch \
  -F "files=@a.pdf" -F "files=@b.jpg" \
  -F "mode=bw"
```

### Đầu ra

**Response** `200 OK`, body kiểu `BatchScanResult`:

| Field | Kiểu | Mô tả |
|---|---|---|
| `results` | `ScanResult[]` | Danh sách kết quả, mỗi phần tử có cấu trúc giống hệt response của `/api/scan`, theo đúng thứ tự file đã gửi lên. |

---

## GET `/api/download/{job_id}`

Tải file kết quả về máy (buộc trình duyệt lưu file, `Content-Disposition: attachment`).

### Đầu vào

| Field | Vị trí | Kiểu | Mô tả |
|---|---|---|---|
| `job_id` | path param | string | Lấy từ `download_url` trong response của `/api/scan`/`/api/scan/batch`. |

**Ví dụ:**
```bash
curl -OJ http://127.0.0.1:8090/api/download/<job_id>
```

### Đầu ra

- `200 OK`: trả về nội dung file (binary), tên file lấy theo tên file kết quả trên server.
- `404 Not Found`: `job_id` không hợp lệ, không tồn tại, hoặc không tìm thấy file kết quả.

---

## GET `/api/view/{job_id}`

Xem trực tiếp file kết quả trên trình duyệt (`Content-Disposition: inline`) — dùng làm link nhúng `<img src="...">` hoặc mở nhanh.

### Đầu vào

| Field | Vị trí | Kiểu | Mô tả |
|---|---|---|---|
| `job_id` | path param | string | Giống `/api/download/{job_id}`. |

### Đầu ra

Giống `/api/download/{job_id}` nhưng hiển thị inline thay vì buộc tải về. Cùng mã lỗi `404`.

---

## GET `/api/debug/{job_id}`

Xem ảnh debug: overlay vẽ lên ảnh gốc (trước crop) gồm contour vùng giấy tìm được (viền xanh dương), 4 góc đã chọn để crop nếu có (viền xanh lá + chấm đỏ ở góc), và chỉ số `blur_score`/`solidity` ở góc trên trái. Chỉ có nếu đã gọi `/api/scan` hoặc `/api/scan/batch` với `debug=true`.

### Đầu vào

| Field | Vị trí | Kiểu | Bắt buộc | Mặc định | Mô tả |
|---|---|---|---|---|---|
| `job_id` | path param | string | ✅ | — | Job id từ response lúc xử lý. |
| `page` | query param | int | ❌ | `1` | Số trang cần xem (chỉ áp dụng cho PDF nhiều trang, đánh số từ 1). |

**Ví dụ:**
```bash
curl "http://127.0.0.1:8090/api/debug/<job_id>?page=2"
```

### Đầu ra

- `200 OK`: ảnh JPEG hiển thị inline.
- `404 Not Found`: `job_id` không hợp lệ/không tồn tại, hoặc không có ảnh debug (chưa gọi với `debug=true`, hoặc `page` ngoài phạm vi số trang có debug — xem `debug_page_count`).

---

## Ghi chú chung

- Mỗi kết quả xử lý còn được lưu thêm một bản vào thư mục `output_scans/` (gốc project, đổi qua biến môi trường `SCAN_OUTPUT_DIR`) với tên `<job_id>_<tên_file_gốc>`, để đối soát trực tiếp trên ổ đĩa không cần gọi API — xem field `saved_path` trong response.
- Kết quả tạm (dùng cho `/api/download`, `/api/view`, `/api/debug`) có thời hạn lưu giữ giới hạn (mặc định 24h, biến `SCAN_STORAGE_TTL_HOURS`), quá hạn sẽ bị dọn tự động và `job_id` không còn truy cập được.
- Kích thước file upload tối đa mỗi file: 50MB (biến `SCAN_MAX_UPLOAD_MB`).
- **⚠️ Không có xác thực (auth):** API hiện không yêu cầu API key hay đăng nhập. Chỉ chạy trên máy/mạng tin cậy; mặc định Docker Compose chỉ bind cổng vào `127.0.0.1`. Không expose ra internet nếu chưa thêm xác thực và/hoặc reverse proxy.

Toàn bộ biến môi trường cấu hình chi tiết xem [`.env.example`](.env.example).
