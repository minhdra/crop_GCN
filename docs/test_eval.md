# Test & Eval — py-project (crop_GCN)

> Cách chạy smoke test tay, bộ test tự động hiện có, và cách eval chất lượng
> khi có dữ liệu thật. Chi tiết thuật toán/ngưỡng ở [algorithm.md](algorithm.md);
> danh sách issue liên quan ở [features_issues.md](features_issues.md).

---

## 0. Chuẩn bị môi trường

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env   # chỉnh nếu cần, xem mô tả từng biến trong file này
```

## 1. Bộ test tự động (`pytest`)

```bash
python -m pytest
```

⚠️ **Ở trạng thái hiện tại, lệnh trên KHÔNG chạy được** — `src/py_project/main.py`
đang bị thiếu khỏi working tree, làm vỡ collection của `tests/test_main.py`
(và do đó chặn toàn bộ các file test khác trong cùng lượt chạy). Xem
[features_issues.md BUG-1](features_issues.md#bug-1-main-py-missing) trước
khi chạy bộ test. Sau khi khôi phục file, chạy lại `python -m pytest` để xác
nhận cả 27 test qua.

Ba file test hiện có:

| File | Số test | Phạm vi |
| --- | --- | --- |
| `tests/test_main.py` | 1 | Console script `py-project` in đúng chuỗi chào |
| `tests/test_capture_quality.py` | 7 | `assess_capture_quality` — ảnh tổng hợp (synthetic), từng mã lý do (`low_resolution`, `blurry`, `hand_covering`, `no_document_detected`), ngưỡng tuỳ chỉnh được |
| `tests/test_api.py` | 19 | FastAPI qua `TestClient`: `/health`, `/api/scan` (ảnh/PDF/lỗi định dạng/quá kích thước/503 khi quá tải), `/api/scan/batch`, `/api/download`, `/api/debug`, `/api/capture/check`, `/api/capture/scan` |

Chạy riêng một file/nhóm:

```bash
python -m pytest tests/test_capture_quality.py -v
python -m pytest tests/test_api.py -k capture -v
```

Ghi chú về cách test hiện tại: `test_capture_quality.py` dựng ảnh **tổng
hợp** bằng `cv2.rectangle`/`cv2.ellipse` (nền xám + "giấy" sáng màu + vài
dòng "chữ" giả) thay vì ảnh thật — đủ để kiểm logic từng nhánh
(`hand_covering`, `blurry`, ...) độc lập với nhau, nhưng **không thay được
việc test trên ảnh thật** để hiệu chỉnh ngưỡng (xem mục 5).

## 2. Smoke test tay

### 2a. Qua CLI

```bash
# Ảnh lẻ
python batch_scan.py input_files/gcn_1_1783321893242.jpg /tmp/out.jpg --mode scan
# Kiểm ảnh debug (contour + góc crop)
python batch_scan.py input_files/gcn_1_1783321893242.jpg /tmp/out.jpg --debug
open /tmp/out_debug.jpg   # (hoặc trình xem ảnh tương ứng hệ điều hành)

# Thư mục ảnh
python batch_scan.py input_files ./tmp_scanned --sharpness 1.2

# PDF (nếu có file PDF mẫu)
python batch_scan.py input.pdf /tmp/out.pdf --mode scan --dpi 250
```

### 2b. Qua API

```bash
uvicorn py_project.api:app --reload
# hoặc: scan-api
```

```bash
curl http://127.0.0.1:8090/health
curl -X POST http://127.0.0.1:8090/api/scan -F "file=@input_files/gcn_1_1783321893242.jpg" -F "mode=scan"
curl -X POST http://127.0.0.1:8090/api/capture/check -F "file=@input_files/gcn_1_1783321893242.jpg"
```

So khớp `saved_path` trong response JSON với file thật trong `output_scans/`
trên đĩa — cách nhanh nhất để đối soát kết quả mà không cần gọi
`/api/download`.

### 2c. Qua giao diện web

Mở `http://127.0.0.1:8090/` — upload file hoặc bấm "Chụp ảnh
(điện thoại/laptop)" (cần HTTPS hoặc `localhost` để trình duyệt cho mở
camera). Dùng để kiểm bằng mắt luồng QC chặn cứng: cố tình che tay lên tài
liệu hoặc chụp mờ, xác nhận app từ chối kèm thông báo đúng, không cho crop.

### 2d. Qua Docker

```bash
docker compose up -d --build
curl http://127.0.0.1:8090/health
curl -X POST http://127.0.0.1:8090/api/scan -F "file=@input_files/gcn_1_1783321893242.jpg"
docker compose logs -f
```

## 3. Kiểm mắt thường trên dữ liệu thật đã có

Repo đã có dữ liệu thật để soi bằng mắt, không cần chờ tập vàng:

- `input_files/` — ảnh mẫu thật của khách (vd `gcn_1_1783321893242.jpg`).
- `output_scans/` — ~30 kết quả xử lý thật từ luồng chụp ảnh trực tiếp
  (`<job_id>_capture_<timestamp>.jpg`), sinh ra từ dùng thử/tích hợp thật,
  không phải test tổng hợp.

Quy trình soi nhanh: mở từng cặp input/output tương ứng (đối chiếu qua
`saved_path` hoặc timestamp), kiểm bằng mắt:
- Crop có đúng 4 góc tài liệu, không cắt lẹm chữ, không lẫn nền không?
- Ảnh `bw`/`scan` có còn đọc được chữ mờ/dấu mộc không, hay bị CLAHE/threshold
  làm mất chi tiết?
- Với ảnh nghiêng/nếp gấp — crop có lật thẳng đúng không, hay rơi vào nhánh
  fallback `minAreaRect` (kém chính xác hơn approxPolyDP)?

**Chưa có nhãn** (toạ độ 4 góc đúng) trên các ảnh này — chỉ dùng để soi mắt
thường / so sánh cấu hình, chưa chấm được đúng/sai một cách khách quan
(giống tình trạng "9 ảnh thật, chưa có nhãn" mà `qc_scanner` từng gặp ở
đầu quá trình — xem `../../qc_scanner/docs/need_exchange.md EX-2`).

## 4. Benchmark hiệu năng

```bash
python benchmark.py --dir input_files --url http://127.0.0.1:8090/api/scan
python benchmark.py --concurrency 8 --repeat 3 --mode scan
python benchmark.py --output results.csv
```

`benchmark.py` gửi lần lượt hoặc song song (`--concurrency`) các file trong
một thư mục tới `/api/scan` đang chạy, đo latency/throughput, xuất CSV nếu
cần. Dùng để kiểm cấu hình `WEB_CONCURRENCY`/`SCAN_MAX_CONCURRENT_JOBS` có
chịu tải đúng như kỳ vọng trước khi lên môi trường thật — xem
[README.md § Performance](../README.md#performance--khả-năng-chịu-tải).

**Chưa có** số liệu benchmark chính thức đã ghi lại (thời gian xử lý
trung bình/ảnh, throughput tối đa đo được) trong tài liệu này.
**[CẦN ĐIỀN]** nếu đã từng chạy `benchmark.py` trên môi trường gần giống
production, nên chép kết quả (thời gian/ảnh, cấu hình `WEB_CONCURRENCY` lúc
đo) vào đây để có mốc so sánh khi đổi thuật toán/cấu hình sau này.

## 5. Eval chất lượng khi có tập ảnh vàng (khi có nhãn thật)

Hiện **chưa có** tập ảnh vàng có nhãn (toạ độ 4 góc đúng do khách xác nhận)
để chấm chính xác. Khi có, quy trình đề xuất (mirror cách `qc_scanner` — dự
án chị em — đã làm, xem `../../qc_scanner/docs/test_eval.md §5`):

1. Với mỗi ảnh mẫu, gán nhãn 4 góc thật của tài liệu.
2. Chạy `find_document_corners` trên từng ảnh, tính **IoU** giữa tứ giác dự
   đoán và tứ giác nhãn.
3. Báo cáo tách bạch (giống nguyên tắc ở `algorithm.md`):
   - **Tỉ lệ crop đúng** — trong số ảnh crop được, bao nhiêu % có IoU ≥
     ngưỡng chấp nhận (vd 0.9 — **[CẦN ĐIỀN]** chốt với khách, xem
     [need_exchange.md EX-3](need_exchange.md#ex-3)).
   - **False pass** — báo `status=success`/`warning` (có crop) nhưng crop
     sai biên — nghiêm trọng nhất vì dữ liệu sai lẳng lặng trôi xuống bước
     sau (OCR/lưu trữ).
   - **False fail** (thực chất là "không crop được dù ảnh dùng tốt") — giữ
     nguyên ảnh gốc dù đáng lẽ crop được — ít nghiêm trọng hơn nhưng vẫn
     đáng đo.
4. Với QC chặn cứng (`assess_capture_quality`), đo tương tự trên tập ảnh
   "xấu" biết trước (mờ, thiếu sáng, tay che...): tỉ lệ đúng bị chặn / tỉ lệ
   ảnh tốt bị chặn oan (false reject).

**Chưa xác định** trọng số ưu tiên false-pass so với false-fail cho dự án
này — cần chốt với khách trước khi quét ngưỡng, xem
[need_exchange.md EX-3](need_exchange.md#ex-3).

## 6. Checklist trước khi release / bàn giao

- [ ] `python -m pytest` chạy xanh hoàn toàn (chặn bởi
      [BUG-1](features_issues.md#bug-1-main-py-missing) hiện tại).
- [ ] `docker compose up -d --build` chạy được, `curl /health` trả `200`.
- [ ] Đã kiểm mắt thường ít nhất một lượt trên `input_files/` (mục 3).
- [ ] Đã chạy `benchmark.py` với cấu hình `WEB_CONCURRENCY`/
      `SCAN_MAX_CONCURRENT_JOBS` dự kiến dùng production, xác nhận không có
      request bị `503` ở tải kỳ vọng.
- [ ] Đã xác nhận `SCAN_MAX_UPLOAD_MB` khớp giới hạn thật của phía gọi
      (client/app chụp ảnh) và của mọi reverse proxy đứng trước (vd
      `client_max_body_size` trong nginx nếu có — xem
      [src/api_3rd/README.md](../src/api_3rd/README.md) cho một ví dụ đã gặp
      vấn đề này ở dự án chị em).
- [ ] Đã xác nhận với khách về auth/mạng triển khai — xem
      [need_exchange.md EX-4](need_exchange.md#ex-4).
