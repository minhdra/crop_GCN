# Thuật toán luồng xử lý — py-project (crop_GCN)

> Chi tiết từng bước của lõi crop/làm rõ ảnh. Bức tranh tổng quan + roadmap ở
> [overall_roadmap.md](overall_roadmap.md); danh sách issue liên quan tới
> thuật toán ở [features_issues.md](features_issues.md).

---

## 0. Lưu ý tên gọi: "crop_GCN" nhưng KHÔNG có model học sâu

Thư mục gốc của dự án tên `crop_GCN` (gợi ý Graph Convolutional Network),
nhưng **toàn bộ pipeline hiện tại là OpenCV thuần** — không có bất kỳ model
học sâu/GCN nào được huấn luyện hay gọi tới trong code (`grep -ri gcn` trong
`src/` không ra kết quả nào ngoài chính chuỗi trong `README.md`). Nếu tên gọi
này phản ánh một kế hoạch dùng model GCN để dò góc tài liệu mà chưa triển
khai, cần ghi rõ trong roadmap — hiện tại nó bị bỏ trống.
**[CẦN ĐIỀN]**: tên "GCN" có phải viết tắt khác (vd tên khách hàng/dự án),
hay đúng là dự định dùng Graph Conv Network cho bước dò góc?

## 1. Bức tranh tổng thể

Ba mặt tiền cùng gọi vào **một** module lõi
[`src/py_project/document_scanner.py`](../src/py_project/document_scanner.py):

```
CLI (batch_scan.py, scan-pdf) ─┐
FastAPI (scan-api, api.py)     ─┼──►  document_scanner.py  ──►  ảnh/PDF đã crop + làm rõ
Library (import document_scanner) ─┘        │
                                            ├── _paper_contours()   (GrabCut + lọc contour)
                                            ├── find_document_corners()  (tứ giác 4 góc)
                                            ├── assess_quality()   (blur + solidity — cảnh báo mềm)
                                            ├── assess_capture_quality()  (QC cứng cho luồng camera)
                                            └── enhance_for_scan() (làm rõ: CLAHE + sharpen + mode)
```

Không state, không DB. Mỗi request/lệnh xử lý độc lập một file; job tạm lưu
trên đĩa (`STORAGE_DIR`) chỉ để phục vụ `/api/download`, `/api/view`,
`/api/debug` ngay sau đó (xem [API.md](../API.md)).

⚠️ **Có hai bản sao logic dò góc/crop độc lập**: [`scan_image_corners.py`](../scan_image_corners.py)
ở gốc repo tự định nghĩa lại `load_image_with_exif`, `order_corners`,
`_grabcut_foreground_contour`, `find_document_corners`, `four_point_crop` —
**không import** từ `py_project.document_scanner`. Sửa thuật toán ở một chỗ
dễ quên chỗ kia → hai luồng lệch nhau âm thầm. Xem
[OPS-2](features_issues.md#ops-2-duplicate-corner-logic).
[`batch_scan.py`](../batch_scan.py) thì ngược lại — chỉ là CLI wrapper mỏng,
**có import** đúng từ `py_project.document_scanner`, không trùng lặp logic.

## 2. Luồng xử lý một ảnh — `scan_image()`

1. **Đọc ảnh + tự xoay theo EXIF** (`load_image_with_exif`) — dùng Pillow +
   `pillow-heif` (đọc được cả `.heic` từ iPhone), không dùng `cv2.imread`
   trực tiếp vì nó bỏ qua thẻ EXIF orientation → ảnh chụp dọc từ điện thoại
   dễ bị lưu sai chiều ngang.
2. **Tách vùng giấy khỏi nền** (`_paper_contours` → `_grabcut_foreground_contour`):
   - Resize ảnh về tối đa 1000px cạnh dài để tính contour, rồi resize thêm
     một lần nữa xuống **450px** (`_GRABCUT_WORKING_SIZE`) chỉ cho riêng bước
     GrabCut — chạy GrabCut trực tiếp trên ảnh full-size từng đo được tới
     **~90 giây/ảnh**; xuống 450px giữ ở mức vài giây mà kết quả contour
     không đổi đáng kể.
   - `cv2.grabCut` khởi tạo bằng rect = toàn ảnh trừ margin 5%
     (`_GRABCUT_MARGIN_RATIO`), coi phần ngoài rect là nền chắc chắn.
   - **Vì sao GrabCut chứ không phải Canny/threshold**: tài liệu không phẳng
     (sổ/sách đang mở, có nếp gấp) khiến viền ngoài không tạo thành một
     đường cạnh liền mạch — Canny dò theo gradient sáng/tối nên bị vỡ contour
     thành nhiều mảnh tại chỗ gấp. GrabCut phân loại từng pixel theo phân bố
     màu (tiền cảnh/nền), không phụ thuộc cạnh có liền mạch hay không.
   - `morphologyEx(MORPH_CLOSE)` nối các mảnh contour rời rạc do nếp gấp.
3. **Lọc contour hợp lệ** (`_filter_candidate_contours`):
   - Diện tích phải trong khoảng `[min_area_ratio, 0.92]` lần diện tích ảnh —
     quá nhỏ thì bỏ qua (không phải tài liệu), quá lớn (>92%) gần như chắc
     chắn là nền/toàn khung dính vào nhau (ảnh ít tương phản) chứ không phải
     tài liệu có lề — thà giữ nguyên ảnh gốc còn hơn crop nhầm vào nền.
   - Loại contour **chạm ≥3/4 cạnh ảnh**: tài liệu thật luôn có lề nhìn thấy
     được ít nhất ở 2 phía đối diện.
   - Loại contour không "đặc" theo hình chữ nhật: tỉ lệ diện tích
     `minAreaRect / contourArea > 1.2` nghĩa là contour bị dính "cành" nhiễu
     nền (vd vân gỗ nối vào viền giấy) kéo phình `minAreaRect` ra.
4. **Xấp xỉ tứ giác 4 góc** (`_approximate_quad` trong `find_document_corners`):
   - `cv2.approxPolyDP` với epsilon quét dần từ chặt (`0.01×chu vi`) đến lỏng
     (`0.08×chu vi`) — viền giấy thực tế (bo tròn nhẹ, mờ, có bóng) thường
     không xẹp gọn về đúng 4 điểm ngay ở epsilon đầu tiên.
   - Nếu không epsilon nào ra đúng 4 điểm lồi: **fallback** —
     `cv2.minAreaRect` (hình chữ nhật xoay nhỏ nhất bao quanh contour) — vẫn
     lật thẳng được tài liệu nghiêng thay vì bỏ qua không crop.
5. **Đánh giá chất lượng trên ảnh GỐC** (`assess_quality`, chạy **trước** khi
   crop — sau `four_point_crop` tài liệu luôn là hình chữ nhật sạch, mất tín
   hiệu viền rách):
   - `is_blurry`: phương sai Laplacian (`blur_score`) trên ảnh đã chuẩn hoá
     về cạnh dài 1200px trước khi đo — **bắt buộc chuẩn hoá kích thước**, vì
     cùng một ảnh chỉ resize khác đi điểm số có thể lệch hàng chục-hàng trăm
     lần (ảnh nhỏ hơn khiến biên mềm bị "nén" thành biên cứng hơn so với
     lưới điểm ảnh mới, đẩy điểm số lên cao dù ảnh không nét hơn).
   - `is_damaged`: `solidity = contourArea / convexHullArea` của contour giấy
     — thấp nghĩa là viền lồi lõm bất thường (nghi rách/nát). Ưu tiên contour
     xấp xỉ được thành tứ giác sạch để tính, tránh dùng contour lớn nhất
     (có thể chỉ là biên bị giãn/đứt quãng do chính bước xử lý ảnh tạo ra).
   - **Đây chỉ là cảnh báo mềm** (`warnings` trong response) — không chặn xử
     lý. Áp dụng cho cả luồng upload lẫn luồng camera.
6. **Crop phối cảnh** (`four_point_crop`): sắp 4 góc theo thứ tự
   top-left/top-right/bottom-right/bottom-left (`order_corners`, dựa vào tổng
   và hiệu toạ độ x,y), rồi `cv2.getPerspectiveTransform` + `warpPerspective`
   ra hình chữ nhật đúng tỉ lệ cạnh đo được. **Nếu không tìm được góc, giữ
   nguyên ảnh gốc** — không báo lỗi, chỉ bỏ qua bước crop.
7. **Làm rõ kiểu scanner** (`enhance_for_scan`):
   - `bilateralFilter` khử nhiễu mà giữ biên chữ (nhanh hơn ~25× so với
     `fastNlMeansDenoising` ở cùng độ phân giải).
   - `CLAHE` (`clipLimit=1.4`, tile `16×16`) tăng tương phản cục bộ — giữ
     được dấu mộc/nét mờ, không như threshold cứng làm bản photocopy mất chữ.
   - Unsharp mask (nếu `sharpness > 0`) tăng tương phản biên chữ mà không đổi
     độ sáng tổng thể.
   - `mode`: `color` (giữ ảnh gốc, bỏ qua enhance) · `scan` (ảnh xám đã tăng
     tương phản) · `bw` (thêm `adaptiveThreshold` → đen trắng).
8. **Xoay** theo `rotation` (0/90/180/270, đơn giản là `cv2.rotate`).

## 3. Luồng xử lý PDF — `scan_pdf()`

Giống hệt luồng ảnh, lặp lại từng trang:

1. `pdf_page_to_bgr`: PyMuPDF render trang thành ảnh ở `dpi` chỉ định
   (`fitz.Matrix(dpi/72, dpi/72)`), chuyển RGB(A)→BGR cho OpenCV.
2. `process_pdf_page`: gọi đúng chuỗi bước ở mục 2 (dùng chung `_paper_contours`
   / `assess_quality` / `find_document_corners` / `enhance_for_scan` / `rotate`).
3. `encode_pdf_page`: mã hoá lại thành JPEG (mode `color`/`scan`) hoặc PNG nén
   nhẹ (mode `bw`), rồi chèn vào trang PDF mới có chiều rộng cố định ≈ khổ A4
   (595pt), chiều cao theo đúng tỉ lệ ảnh.
4. Gộp lại thành file PDF output, giữ nguyên số trang đầu vào.

`PdfScanSummary` cộng dồn `cropped_pages`/`blurry_pages`/`damaged_pages` +
danh sách số trang bị gắn cờ (đánh số từ 1).

## 4. QC ảnh chụp trực tiếp từ camera — `assess_capture_quality()`

Khác `assess_quality` (chỉ cảnh báo, vẫn xử lý), đây là **QC chặn cứng**,
chỉ áp dụng cho luồng chụp ảnh (`/api/capture/check`, `/api/capture/scan`),
**không** áp dụng cho upload file thường:

| `code` | Điều kiện | Ngưỡng mặc định |
| --- | --- | --- |
| `no_document_detected` | `_paper_contours` trả `None` — không tách được vùng giấy | — |
| `low_resolution` | Cạnh dài ảnh < `min_resolution` | `500` px |
| `blurry` | `blur_score` (cùng phép đo với `assess_quality`) < ngưỡng | `blur_threshold=100.0` |
| `hand_covering` | Tỉ lệ pixel màu da (YCrCb) đè lên vùng tài liệu > ngưỡng | `skin_coverage_threshold=0.10` |
| `cluttered_background` | Mật độ biên Canny ở nền quanh tài liệu > ngưỡng | `clutter_edge_ratio_threshold=0.20` (đo thực tế: nền gạch/đá hoa vẫn qua ở ~13-14%) |

Mỗi tiêu chí độc lập, ảnh có thể vi phạm nhiều mã cùng lúc. `passed=False`
nếu có ≥1 vi phạm.

Màu da dò trong không gian **YCrCb** (không phải RGB/HSV) vì tách riêng độ
chói (Y) khỏi thông tin màu (Cr, Cb) — ổn định hơn trước thay đổi ánh sáng.

**Chưa có, đã thử và bỏ**: tiêu chí "ảnh chụp lại từ màn hình thiết bị khác"
(chụp màn hình thay vì tài liệu thật). Hai hướng heuristic đã thử:
- Đỉnh phổ tần số (FFT) trên ảnh xám — nhận nhầm giấy kẻ ô ly/bảng biểu
  (rất phổ biến với tài liệu thật) thành ảnh màn hình.
- Lệch màu giữa các kênh RGB kiểu chroma — chính xác hơn trên ảnh gốc nhưng
  tín hiệu gần như bị nén JPEG (chroma subsampling) xoá sạch, trong khi ảnh
  chụp từ camera trình duyệt luôn được nén JPEG trước khi gửi lên.

Cần tập ảnh mẫu chụp màn hình **thật** để hiệu chỉnh lại, thay vì đoán ngưỡng
trên ảnh tổng hợp. Xem [need_exchange.md](need_exchange.md).

## 5. Tối ưu hiệu năng trong thuật toán

- `prepared = _paper_contours(...)` được tính **một lần** rồi truyền lại
  (tham số `prepared`) cho `assess_quality`/`find_document_corners`/
  `generate_debug_image`/QC camera — GrabCut là bước tốn nhất (hàng trăm ms
  đến vài giây), gọi lặp lại 2-3 lần trên cùng ảnh sẽ rất lãng phí. Sentinel
  `_PREPARED_NOT_COMPUTED` phân biệt "chưa tính" với "đã tính và kết quả là
  `None`" — nếu dùng `None` cho cả hai, các lần gọi sau sẽ tưởng nhầm là
  "chưa tính" và chạy lại GrabCut từ đầu.
- Endpoint gộp `/api/capture/scan` (QC + crop trong 1 lượt upload) tái dùng
  `prepared` từ bước QC cho bước crop ngay sau — tránh chạy GrabCut hai lần
  trên cùng ảnh và tránh upload lại ảnh lần thứ hai (xem [API.md](../API.md)).
- `cv2.setNumThreads(1)` mặc định (biến `CV2_NUM_THREADS`) — tránh OpenCV tự
  nhân thread nội bộ chồng lên số worker process (`WEB_CONCURRENCY`) × số job
  đồng thời (`SCAN_MAX_CONCURRENT_JOBS`), gây tranh chấp CPU. Chi tiết ở
  [README.md § Performance](../README.md#performance--khả-năng-chịu-tải).

## 6. Tham số & ngưỡng mặc định

| Tham số | Mặc định | Ý nghĩa | Nguồn |
| --- | --- | --- | --- |
| `min_area_ratio` | `0.2` | Diện tích tối thiểu của contour giấy / diện tích ảnh | API/CLI param |
| `blur_threshold` | `100.0` | Ngưỡng `blur_score` (Laplacian variance) coi là mờ | API/CLI param |
| `solidity_threshold` | `0.85` | Ngưỡng solidity coi là nghi rách/nát | API/CLI param |
| `sharpness` | `0.7` | Hệ số unsharp mask (0–3) | API/CLI param |
| `min_resolution` (camera) | `500` px | Cạnh dài tối thiểu để không bị `low_resolution` | Cứng trong `document_scanner.py` |
| `skin_coverage_threshold` | `0.10` | Tỉ lệ da tay tối đa trên vùng tài liệu | Cứng trong `document_scanner.py` |
| `clutter_edge_ratio_threshold` | `0.20` | Mật độ biên tối đa ở nền | Cứng trong `document_scanner.py` |
| `_GRABCUT_MARGIN_RATIO` | `0.05` | Margin quanh ảnh coi là nền chắc chắn khi khởi tạo GrabCut rect | Hằng số nội bộ |
| `_GRABCUT_WORKING_SIZE` | `450` px | Kích thước resize riêng cho bước GrabCut | Hằng số nội bộ, đã đo (90s → vài giây) |
| Diện tích contour tối đa | `0.92` × diện tích ảnh | Loại contour gần trùng cả khung hình | Hằng số nội bộ |
| Ngưỡng chạm cạnh | `≥3/4` cạnh | Loại contour dính nền | Hằng số nội bộ |
| Ngưỡng "đặc hình chữ nhật" | `minAreaRect ≤ 1.2×contourArea` | Loại contour dính nhiễu | Hằng số nội bộ |
| `approxPolyDP` epsilon | `0.01–0.08 × chu vi`, 6 mức | Xấp xỉ tứ giác | Hằng số nội bộ |

Các ngưỡng "Cứng trong `document_scanner.py`" và hằng số nội bộ **chưa expose
qua API/CLI** — đổi phải sửa code, không tham số hoá được từ ngoài. Ngưỡng
nào cũng là **heuristic đặt theo cảm tính/quan sát ban đầu**, chưa được chốt
bằng số đo trên tập ảnh thật của khách — xem
[need_exchange.md](need_exchange.md) và [test_eval.md § 5](test_eval.md).

## 7. Endpoint liên quan tới thuật toán

Hợp đồng HTTP đầy đủ nằm ở [API.md](../API.md); tham chiếu nhanh:

| Endpoint | Bước thuật toán chạy |
| --- | --- |
| `POST /api/scan`, `POST /api/scan/batch` | Toàn bộ luồng mục 2/3, cảnh báo mềm qua `assess_quality` |
| `POST /api/capture/check` | Chỉ `assess_capture_quality` (mục 4), không crop |
| `POST /api/capture/scan` | `assess_capture_quality` rồi crop luôn nếu `passed=True`, tái dùng `prepared` |
| `GET /api/debug/{job_id}` | Ảnh overlay từ `generate_debug_image` (contour + góc + chỉ số) |

## 8. Tài liệu liên quan

- [overall_roadmap.md](overall_roadmap.md) — tổng quan dự án + roadmap.
- [features_issues.md](features_issues.md) — sổ bug/issue/tính năng liên quan tới thuật toán.
- [test_eval.md](test_eval.md) — cách test/eval các ngưỡng ở mục 6.
- [need_exchange.md](need_exchange.md) — câu hỏi cần chốt với khách để thay ngưỡng heuristic bằng số đo.
- [../API.md](../API.md) — hợp đồng HTTP đầy đủ.
- [../README.md](../README.md) — giới thiệu & cách dùng.
