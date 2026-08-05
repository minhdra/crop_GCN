# Features & Issues — py-project (crop_GCN)

> Sổ tính năng + issue của dự án. Chi tiết thuật toán ở [algorithm.md](algorithm.md);
> tổng quan/roadmap ở [overall_roadmap.md](overall_roadmap.md); cách test ở
> [test_eval.md](test_eval.md).
>
> Quy ước trạng thái: 🔴 mở/chưa làm · 🟡 đang làm/đã đo chưa sửa · 🟢 đã sửa/xong ·
> ⚪ backlog, chưa ưu tiên. Mức ưu tiên P0 (chặn) → P3 (khi rảnh).

---

## Tình trạng (cập nhật 2026-08-05)

- 24 commit, chưa có CI/pipeline test tự động (xem [OPS-4](#ops-4-no-ci)).
- 27 test (`tests/test_api.py`, `test_capture_quality.py`, `test_main.py`) —
  nhưng **bộ test hiện KHÔNG chạy được** ở trạng thái working tree hiện tại,
  xem [BUG-1](#bug-1-main-py-missing).
- Đã có dữ liệu thật: `input_files/` chứa ảnh mẫu thật của khách (vd
  `gcn_1_*.jpg`); `output_scans/` có ~30 kết quả xử lý thật từ luồng chụp
  ảnh (`capture_*.jpg`), cho thấy đã có thử nghiệm/tích hợp thật với luồng
  chụp ảnh, không chỉ test nội bộ.
- Hai luồng QC tách biệt trong cùng codebase: cảnh báo mềm (`assess_quality`,
  áp dụng cho upload) và chặn cứng (`assess_capture_quality`, chỉ áp dụng cho
  chụp ảnh trực tiếp) — xem [algorithm.md §4](algorithm.md#4-qc-ảnh-chụp-trực-tiếp-từ-camera--assess_capture_quality).

## A. ISSUES — Bug & rủi ro đang mở

### 🐞 BUG-1 · P0 · 🔴 MỞ · `src/py_project/main.py` bị xoá khỏi working tree {#bug-1-main-py-missing}

`git status` cho thấy `src/py_project/main.py` đang ở trạng thái **deleted,
chưa commit** (` D src/py_project/main.py`). File này được entrypoint
`py-project = "py_project.main:main"` trong `pyproject.toml` và
`tests/test_main.py` tham chiếu tới — thiếu nó thì:
- `python -m pytest` **không chạy được cả bộ test** (lỗi collection ngay ở
  `tests/test_main.py`, chặn luôn các test khác trong cùng lượt chạy).
- Lệnh CLI `py-project` (nếu cài qua `pip install .`) sẽ vỡ khi gọi.

**Chưa tự khôi phục file này** vì đây là thay đổi cục bộ chưa commit, có thể
đang dở dang — cần xác nhận trước: đây là xoá **có chủ đích** (dọn dẹp
entrypoint không dùng tới) hay xoá nhầm? Nếu có chủ đích, cần dọn luôn
`pyproject.toml` (bỏ `py-project` script) và `tests/test_main.py` cho khớp.

### 🐞 OPS-2 · P1 · 🔴 MỞ · Logic dò góc/crop bị nhân bản ở `scan_image_corners.py` {#ops-2-duplicate-corner-logic}

[`scan_image_corners.py`](../scan_image_corners.py) ở gốc repo định nghĩa lại
độc lập `load_image_with_exif`, `order_corners`, `_grabcut_foreground_contour`,
`find_document_corners`, `four_point_crop` — **không import** từ
`py_project.document_scanner`. Hai bản đã bắt đầu lệch nhau (bản trong
`document_scanner.py` có thêm lọc contour theo diện tích/chạm cạnh/độ đặc —
xem [algorithm.md §2](algorithm.md#2-luồng-xử-lý-một-ảnh--scan_image), bản ở
`scan_image_corners.py` thì không). Rủi ro: sửa ngưỡng/thuật toán ở một chỗ,
quên chỗ kia, hai luồng cho kết quả khác nhau trên cùng một ảnh.

Đề xuất: xác nhận `scan_image_corners.py` còn được dùng thật (dev thử
nghiệm độc lập?) hay là bản nháp đã lỗi thời — nếu lỗi thời, xoá hoặc chuyển
thành wrapper mỏng gọi vào `py_project.document_scanner` giống cách
`batch_scan.py` đang làm.

### ⚠️ OPS-1 · P1 · 🟢 ĐÃ GHI NHẬN, CHƯA GIẢI QUYẾT · API không có xác thực {#ops-1-no-auth}

Đã ghi rõ trong [README.md § Không có xác thực](../README.md#️-không-có-xác-thực-auth):
bất kỳ ai truy cập được cổng đang chạy đều upload/xử lý file được. Giảm
thiểu hiện tại: mặc định Docker Compose chỉ bind `127.0.0.1`. **Chưa có kế
hoạch thêm auth** nếu cần mở ra mạng rộng hơn (LAN/internet) — xem
[need_exchange.md](need_exchange.md).

### ⚠️ OPS-3 · P2 · 🟡 MỘT PHẦN · Kiến trúc vẫn request–response đồng bộ, single-host {#ops-3-sync-arch}

Đã ghi trong README (["Giới hạn hiện tại / khi nào cần queue"](../README.md#giới-hạn-hiện-tại--khi-nào-cần-queue)):
`STORAGE_DIR`/`OUTPUT_DIR` dùng filesystem cục bộ của container, client chờ
đồng bộ trong một request. Đủ dùng khi traffic còn trong khả năng của
multi-worker + backpressure (`WEB_CONCURRENCY` × `SCAN_MAX_CONCURRENT_JOBS`).
Cần chuyển sang mô hình hàng đợi (`job_id` + polling, storage dùng chung như
S3/MinIO) nếu: (a) cần scale ngang nhiều container/host, hoặc (b) traffic
vượt khả năng chịu tải dù đã tăng worker hợp lý. **Chưa đo được ngưỡng traffic
thật cần chuyển** — phụ thuộc [EX-1](need_exchange.md#ex-1) (quy mô thật).

### 🔒 OPS-4 · P2 · 🔴 MỞ · Chưa có CI {#ops-4-no-ci}

Không tìm thấy cấu hình CI (`.github/workflows`, v.v.) trong repo. 27 test đã
có nhưng không có gì tự chạy chúng khi push/PR — càng quan trọng vì
[BUG-1](#bug-1-main-py-missing) cho thấy trạng thái "test không chạy được"
có thể lọt qua nhiều commit mà không ai biết.

## B. ISSUES — Chất lượng thuật toán (đã biết, cần dữ liệu thật để chốt)

### 🎯 QUAL-1 · P1 · ⚪ BACKLOG · Ngưỡng heuristic chưa được hiệu chỉnh bằng số đo {#qual-1-heuristic-thresholds}

Toàn bộ ngưỡng liệt kê ở [algorithm.md §6](algorithm.md#6-tham-số--ngưỡng-mặc-định)
(`blur_threshold=100.0`, `solidity_threshold=0.85`, `min_resolution=500`,
`skin_coverage_threshold=0.10`, `clutter_edge_ratio_threshold=0.20`, các hằng
số GrabCut/contour) đều được đặt theo cảm tính/quan sát ban đầu trong code,
**không có tập ảnh vàng có nhãn** để đo false-positive/false-negative. Chặn
bởi cùng vấn đề mà `qc_scanner` (dự án chị em, xem
[../../qc_scanner](../../../qc_scanner)) đã gặp và giải quyết bằng cách xin
tập ảnh vàng từ khách — xem [need_exchange.md EX-2](need_exchange.md#ex-2).

### 🎯 QUAL-2 · P2 · ⚪ BACKLOG · Chưa có tiêu chí "ảnh chụp lại từ màn hình" {#qual-2-screen-recapture}

Xem [algorithm.md §4](algorithm.md#4-qc-ảnh-chụp-trực-tiếp-từ-camera--assess_capture_quality) —
hai hướng heuristic đã thử (FFT tần số, lệch chroma) đều không đủ tin cậy.
Cần tập ảnh mẫu chụp màn hình thật để hiệu chỉnh lại.

### 🔬 QUAL-3 · P3 · ⚪ BACKLOG · Chưa khảo sát hướng hồi quy góc trực tiếp {#qual-3-corner-regression}

`qc_scanner` (dự án chị em) đã khảo sát và đang thử DocAligner (hồi quy 4
góc trực tiếp, ONNXRuntime) làm đường thay thế cho contour truyền thống —
xem `../../qc_scanner/docs/algorithm.md §8`. Dự án này (`py-project`) chưa
làm khảo sát tương đương; đáng cân nhắc nếu GrabCut+contour không đủ chính
xác trên tập ảnh vàng của khách khi có ([QUAL-1](#qual-1-heuristic-thresholds)).

## C. FEATURES — Đã có (đã ship)

| Tính năng | Ghi chú |
| --- | --- |
| Crop 4 góc tự động (GrabCut + contour + perspective transform) | [algorithm.md §2](algorithm.md#2-luồng-xử-lý-một-ảnh--scan_image) |
| Làm rõ kiểu scanner (`color`/`scan`/`bw`) | CLAHE + bilateral filter + unsharp mask |
| Xử lý PDF nhiều trang | `scan_pdf`, giữ số trang, xuất PDF mới |
| Xử lý ảnh đơn lẻ và hàng loạt (thư mục) | CLI (`batch_scan.py`) + `scan_images` |
| Cảnh báo mờ/nghi rách (không chặn xử lý) | `assess_quality`, áp dụng cho `/api/scan`, `/api/scan/batch` |
| QC chặn cứng cho ảnh chụp trực tiếp | `assess_capture_quality`, 5 mã lý do — [algorithm.md §4](algorithm.md#4-qc-ảnh-chụp-trực-tiếp-từ-camera--assess_capture_quality) |
| Endpoint gộp QC + crop một lượt (`/api/capture/scan`) | Tránh upload lại + chạy lại GrabCut lần 2 |
| Ảnh debug overlay (contour + góc + chỉ số) | `debug=true`, `/api/debug/{job_id}` |
| Giao diện web chụp ảnh trực tiếp từ camera | `src/py_project/static/index.html`, `getUserMedia` |
| Đa xử lý (multi-worker) + backpressure (503 có kiểm soát) | `WEB_CONCURRENCY`, `SCAN_MAX_CONCURRENT_JOBS` — xem [README §Performance](../README.md#performance--khả-năng-chịu-tải) |
| Dọn dẹp tự động file tạm/kết quả lâu dài | `SCAN_STORAGE_TTL_HOURS`, `SCAN_OUTPUT_TTL_DAYS`, khoá bằng `flock` để không chạy trùng giữa các worker |
| Đọc EXIF, xoay ảnh đúng chiều | `load_image_with_exif`, cả `.heic` |
| Docker Compose, giới hạn CPU/RAM, healthcheck | [docker-compose.yml](../docker-compose.yml) |
| Trang test thủ công gọi QC Scanner bên thứ 3 | [src/api_3rd/](../src/api_3rd/README.md) — dự án chị em, tách biệt |
| Script benchmark tải (`benchmark.py`) | Đo latency/throughput qua `/api/scan`, xuất CSV |

## D. FEATURES — Đề xuất (backlog)

- [ ] **N-1** Job queue (`job_id` + polling) nếu vượt khả năng
      request–response đồng bộ — xem [OPS-3](#ops-3-sync-arch).
- [ ] **N-2** Tham số hoá các ngưỡng "cứng trong code" ở
      [algorithm.md §6](algorithm.md#6-tham-số--ngưỡng-mặc-định) (hiện chỉ
      `min_area_ratio`/`blur_threshold`/`solidity_threshold`/`sharpness` đi
      qua API/CLI, còn `min_resolution`/`skin_coverage_threshold`/
      `clutter_edge_ratio_threshold`/hằng số GrabCut thì không).
- [ ] **N-3** Auth cơ bản cho API nếu triển khai ngoài mạng tin cậy — xem
      [OPS-1](#ops-1-no-auth), [need_exchange.md EX-4](need_exchange.md#ex-4).
- [ ] **N-4** CI chạy `pytest` khi push/PR — [OPS-4](#ops-4-no-ci).
- [ ] **N-5** Tiêu chí phát hiện ảnh chụp lại từ màn hình —
      [QUAL-2](#qual-2-screen-recapture), cần tập ảnh mẫu thật.
- [ ] **N-6** Công cụ/quy trình đo false-pass / false-fail trên tập ảnh vàng
      của khách một khi có ([QUAL-1](#qual-1-heuristic-thresholds)) — có thể
      tham khảo trực tiếp `qc_scanner.eval` (dự án chị em đã dựng công cụ
      tương đương).

## Cách dùng file này

- Thêm issue mới: chọn tiền tố phù hợp (`BUG-` lỗi code, `OPS-` vận
  hành/bảo mật/hạ tầng, `QUAL-` chất lượng thuật toán), gán P0–P3, trạng thái
  🔴/🟡/🟢/⚪.
- Sửa xong: đổi 🔴/🟡 → 🟢, ghi ngày + commit/PR liên quan.
- Quyết định ảnh hưởng ngưỡng thuật toán → cập nhật thẳng
  [algorithm.md §6](algorithm.md#6-tham-số--ngưỡng-mặc-định) kèm nguồn số đo.
- Câu hỏi cần khách trả lời trước khi làm → thêm vào
  [need_exchange.md](need_exchange.md), không tự đoán rồi ghi thẳng vào đây.
