# py-project (crop_GCN) — Tổng quan dự án & Roadmap

> Thay cho "project-insight". Đây là **điểm vào** cho người mới: dự án là gì,
> đang ở đâu, đi về đâu. Chi tiết kỹ thuật ở [algorithm.md](algorithm.md);
> việc cần làm ở [features_issues.md](features_issues.md); cách kiểm ở
> [test_eval.md](test_eval.md); việc cần hỏi khách ở
> [need_exchange.md](need_exchange.md); hợp đồng HTTP bàn giao cho khách ở
> [../API.md](../API.md).

---

## 1. Dự án là gì

**py-project** (tên thư mục repo: `crop_GCN`) nhận PDF hoặc ảnh chụp một tài
liệu → **tìm 4 góc tờ giấy** (GrabCut + contour, không phải model học sâu —
xem [ghi chú tên gọi](algorithm.md#0-lưu-ý-tên-gọi-crop_gcn-nhưng-không-có-model-học-sâu))
→ **nắn phối cảnh** → **làm rõ kiểu scanner** (tăng tương phản, khử nhiễu,
tuỳ chọn đen-trắng) → trả file đã crop. Có ba mặt tiền: CLI, FastAPI, thư
viện Python — cùng gọi vào một lõi `document_scanner.py`.

Theo [DEPLOY.md](../DEPLOY.md), dự án nằm trong một hệ hai phần: **app chụp
ảnh** (dự án khác) gọi API của **dự án crop ảnh** (chính là dự án này) ngay
sau khi người dùng chụp — nên lượng request/hiệu năng của dự án này bị chi
phối trực tiếp bởi lượng người dùng của app chụp ảnh.

Ngoài crop, dự án còn có một nhánh **QC ảnh chụp trực tiếp** riêng
(`assess_capture_quality`, chặn cứng: mờ, độ phân giải thấp, tay che, nền
nhiễu) dành cho luồng chụp camera — khác với luồng upload file thường chỉ
cảnh báo mềm (`assess_quality`, mờ/nghi rách) mà vẫn xử lý. Xem
[algorithm.md §2 và §4](algorithm.md).

- **Dự án chị em**: [`qc_scanner`](../../qc_scanner) (thư mục ngang cấp,
  repo git riêng) giải cùng bài toán (crop tài liệu chụp ảnh) bằng một cách
  tiếp cận khác (`rembg`/U²-Net tách nền + contour, có hệ verdict/reason-code
  QC hình thức hoá). Đáng đối chiếu khi ra quyết định thiết kế — nhiều câu
  hỏi khách hàng và bài học ngưỡng ở đó có thể áp dụng trực tiếp cho dự án
  này, xem ghi chú trong [need_exchange.md](need_exchange.md).
- Nguồn gốc/lịch sử trước `first commit` (`0a9c1f7`): **[CẦN ĐIỀN]** — chưa
  rõ dự án bắt đầu từ đâu (viết mới, fork, hay kế thừa code có sẵn).

## 2. Kiến trúc (một đoạn)

```
CLI (batch_scan.py, scan-pdf)      ─┐
FastAPI (scan-api, api.py)          ─┼──►  py_project.document_scanner  ──►  ảnh/PDF đã crop + làm rõ
Library (import document_scanner)  ─┘                │
                                                      ├── _paper_contours()  (GrabCut + lọc contour)
                                                      ├── assess_quality()   (cảnh báo mềm: mờ/rách)
                                                      ├── assess_capture_quality()  (chặn cứng: luồng camera)
                                                      └── enhance_for_scan() (làm rõ kiểu scanner)
```

Không state, không DB, không hàng đợi — kiến trúc request–response đồng bộ,
single-host (xem [features_issues.md OPS-3](features_issues.md#ops-3-sync-arch)).
Job tạm lưu trên đĩa (`STORAGE_DIR`) chỉ để phục vụ tải/xem lại ngay sau khi
xử lý; một bản sao lâu dài hơn nằm ở `OUTPUT_DIR`/`output_scans/` để đối
soát trực tiếp trên đĩa.

Chi tiết đầy đủ: [algorithm.md](algorithm.md).

## 3. Nguyên tắc thiết kế (quan sát được từ code hiện tại)

1. **Không báo lỗi khi không crop được** — giữ nguyên ảnh/trang gốc, chỉ làm
   rõ, thay vì chặn xử lý hay ném lỗi. Ưu tiên trả về thứ dùng được hơn là
   thất bại cứng.
2. **Cảnh báo mềm cho upload, chặn cứng cho camera** — hai chính sách QC
   khác nhau tuỳ luồng, vì luồng camera **luôn chụp lại được ngay lúc đó**
   còn luồng upload thì không chắc. **[CẦN ĐIỀN]** nguyên tắc này đã được
   xác nhận rõ ràng với khách hay là giả định kỹ thuật ban đầu — xem
   [need_exchange.md EX-5](need_exchange.md#ex-5).
3. **Tính một lần, dùng lại nhiều nơi** — `_paper_contours` (GrabCut, bước
   tốn nhất) được tính một lần và truyền lại (`prepared`) cho mọi bước sau
   cần tới nó trên cùng ảnh, tránh chạy lại 2-3 lần.
4. **Luôn cho phép tinh chỉnh qua tham số** — hầu hết ngưỡng chính
   (`min_area_ratio`, `blur_threshold`, `solidity_threshold`, `sharpness`)
   đi qua API/CLI thay vì hardcode, dù bản thân giá trị mặc định vẫn là
   heuristic chưa được đo — xem [QUAL-1](features_issues.md#qual-1-heuristic-thresholds).
5. **Chịu tải có kiểm soát, không sập lặng lẽ** — backpressure trả `503` +
   `Retry-After` khi quá tải thay vì xếp hàng vô hạn hay chặn cả server; dọn
   dẹp tự động tránh phình đĩa. Xem
   [README.md § Performance](../README.md#performance--khả-năng-chịu-tải).

*(Đây là các nguyên tắc suy ra được từ code hiện có, chưa phải một danh sách
đã được đội thống nhất chính thức — nếu có nguyên tắc khác đã thống nhất mà
chưa phản ánh trong code/tài liệu, bổ sung vào đây.)*

## 4. Hiện trạng (2026-08-05)

- 24 commit kể từ `first commit`. Không tag version, không CI
  ([OPS-4](features_issues.md#ops-4-no-ci)).
- **Đã có tích hợp/dùng thử thật**: `output_scans/` chứa ~30 kết quả xử lý
  thật từ luồng chụp ảnh trực tiếp (không phải chỉ chạy test), `input_files/`
  có ảnh mẫu thật của khách. Chưa rõ quy mô/thời điểm của lượt dùng thử này —
  **[CẦN ĐIỀN]**.
- **Đã xong Phase 1 hiệu năng** (theo [DEPLOY.md](../DEPLOY.md)): route xử
  lý đổi từ `async def` chặn event loop sang `def` chạy threadpool, bật
  multi-worker (`WEB_CONCURRENCY`), thêm backpressure có kiểm soát
  (`SCAN_MAX_CONCURRENT_JOBS`, `SCAN_MAX_UPLOAD_MB`) — chi tiết ở
  [README.md § Performance](../README.md#performance--khả-năng-chịu-tải).
- **Chưa làm job queue** — DEPLOY.md ghi rõ lý do: app chụp ảnh hiện chỉ
  chấp nhận request–response đồng bộ. Cân nhắc lại khi cần scale ngang nhiều
  container/host, hoặc traffic vượt khả năng multi-worker + backpressure.
- **Đang có một bug chặn cả bộ test**: `src/py_project/main.py` bị xoá khỏi
  working tree (chưa commit) — xem
  [features_issues.md BUG-1](features_issues.md#bug-1-main-py-missing).
  **Cần xử lý trước khi coi bộ test 27 case hiện có là đáng tin cậy.**
- **Có nhân bản logic dò góc/crop** giữa `document_scanner.py` (nguồn chính,
  có đầy đủ bước lọc contour) và `scan_image_corners.py` ở gốc repo (bản cũ
  hơn/độc lập, thiếu các bước lọc) — xem
  [OPS-2](features_issues.md#ops-2-duplicate-corner-logic).
- **Chưa có tập ảnh vàng có nhãn** của khách để chốt ngưỡng bằng số đo — mọi
  ngưỡng trong [algorithm.md §6](algorithm.md#6-tham-số--ngưỡng-mặc-định)
  hiện là heuristic đặt theo cảm tính ban đầu. Đây là mục chặn nhiều việc
  nhất về lâu dài (giống bài học `qc_scanner` đã rút ra), xem
  [need_exchange.md EX-2](need_exchange.md#ex-2).
- **Không có xác thực (auth)** trên API — đã ghi rõ trong README, giảm thiểu
  bằng bind `127.0.0.1` mặc định trong Docker Compose. Chưa có kế hoạch thêm
  auth nếu cần mở mạng rộng hơn.

## 5. Bắc Nam của bài toán

Cùng tinh thần với `qc_scanner` (dự án chị em): "tốt" với dự án này không chỉ
là "crop được nhiều ảnh", mà là:

- **Tỉ lệ crop đúng** — trong số ảnh crop được, bao nhiêu % nắn đúng biên
  thật. **Chưa đo được** vì thiếu tập vàng có nhãn (EX-2).
- **Không có ảnh crop sai mà báo thành công** (false pass) — nguy hiểm hơn
  hẳn ảnh bị giữ nguyên/bị QC từ chối, vì lỗi im lặng trôi xuống bước sau
  (OCR/lưu trữ) mà không ai biết cho tới lúc phát hiện muộn.
- **QC camera không chặn oan ảnh tốt** (false reject) — vì đây là chặn
  **cứng, không thể vượt qua** từ giao diện hiện tại; chặn oan liên tục sẽ
  gây khó chịu trực tiếp cho người dùng cuối đang cầm điện thoại đứng chờ.

Thời gian xử lý bị chi phối chủ yếu bởi **GrabCut** (bước tách nền) — đã tối
ưu bằng cách resize xuống 450px chỉ cho riêng bước này (từ ~90s xuống còn vài
giây, xem [algorithm.md §2](algorithm.md#2-luồng-xử-lý-một-ảnh--scan_image)).
**Chưa có số đo latency chính thức đã ghi lại** cho môi trường
gần-production — xem [test_eval.md §4](test_eval.md#4-benchmark-hiệu-năng).

## 6. Roadmap

Roadmap chi tiết theo giai đoạn (kiểu `qc_scanner`) **chưa được dựng cho dự
án này** — không có đủ lịch sử quyết định để suy ra các giai đoạn đã qua một
cách đáng tin cậy. Dưới đây là việc cần làm **theo mức ưu tiên**, rút ra từ
hiện trạng thật của code (mục 4) và [features_issues.md](features_issues.md);
**[CẦN ĐIỀN]** nếu đội đã có kế hoạch giai đoạn chính thức khác.

### Ngay bây giờ (chặn máu)
- [ ] Xác nhận & xử lý [BUG-1](features_issues.md#bug-1-main-py-missing)
      (`main.py` bị xoá) — chặn toàn bộ bộ test.
- [ ] Làm rõ [OPS-2](features_issues.md#ops-2-duplicate-corner-logic)
      (`scan_image_corners.py` có còn cần không) — tránh lệch thuật toán âm
      thầm giữa hai bản.
- [ ] Thêm CI chạy `pytest` ([OPS-4](features_issues.md#ops-4-no-ci)) — để
      lần vỡ test tiếp theo (như BUG-1) không lọt qua âm thầm.

### Kế tiếp (cần dữ liệu khách để làm đúng)
- [ ] Xin tập ảnh thật ≥100 ảnh, có ca xấu, xin gán nhãn 4 góc
      ([need_exchange.md EX-2](need_exchange.md#ex-2)).
- [ ] Chốt định nghĩa "đạt" (IoU tối thiểu, cân bằng false-pass/false-fail —
      [need_exchange.md EX-3, EX-6](need_exchange.md#ex-3)).
- [ ] Quét ngưỡng bằng số đo trên tập vàng khi có
      ([QUAL-1](features_issues.md#qual-1-heuristic-thresholds)).
- [ ] Chốt quy mô/hạ tầng triển khai thật
      ([need_exchange.md EX-1](need_exchange.md#ex-1)) — quyết định có cần
      chuyển sang kiến trúc hàng đợi hay đa-host không
      ([OPS-3](features_issues.md#ops-3-sync-arch)).

### Khi rảnh / backlog
- Tham số hoá nốt các ngưỡng còn hardcode
  ([N-2](features_issues.md#d-features--đề-xuất-backlog)).
- Auth cơ bản nếu triển khai ngoài mạng tin cậy
  ([need_exchange.md EX-9](need_exchange.md#ex-9)).
- Tiêu chí phát hiện ảnh chụp lại từ màn hình
  ([QUAL-2](features_issues.md#qual-2-screen-recapture)).
- Khảo sát hướng hồi quy góc trực tiếp nếu GrabCut+contour không đủ
  ([QUAL-3](features_issues.md#qual-3-corner-regression)).

## 7. Rủi ro & phụ thuộc

| Rủi ro | Ảnh hưởng | Giảm thiểu |
| --- | --- | --- |
| Bộ test đang vỡ (BUG-1) mà không ai biết vì không có CI | Bug mới lọt qua âm thầm | Xử lý BUG-1 + thêm CI (OPS-4) |
| Hai bản logic dò góc lệch nhau (OPS-2) | Sửa một chỗ, quên chỗ kia, hai luồng cho kết quả khác nhau | Gộp về một nguồn, hoặc xác nhận file cũ có thể xoá |
| Không có tập ảnh vàng | Không chốt được ngưỡng bằng số đo, không chứng minh chất lượng lúc nghiệm thu | need_exchange.md EX-2 |
| False pass (crop sai mà báo thành công) | Dữ liệu bẩn trôi xuống OCR/lưu trữ, phát hiện muộn | Cần tập vàng để đo; nguyên tắc "giữ nguyên nếu không chắc" đã có sẵn trong code |
| API không có auth | Ai truy cập được cổng cũng upload/xử lý được | Bind `127.0.0.1` mặc định; cần auth nếu mở mạng rộng hơn (EX-9) |
| Kiến trúc đồng bộ single-host | Không scale ngang được, có thể nghẽn khi traffic tăng | Theo dõi qua benchmark.py; chuyển sang hàng đợi khi cần (OPS-3) |
| `rembg`/model bên ngoài đổi API — *(không áp dụng, dự án này không dùng model học sâu, xem algorithm.md §0)* | — | — |

## 8. Tài liệu liên quan

- [algorithm.md](algorithm.md) — thuật toán từng bước + tham số/ngưỡng.
- [features_issues.md](features_issues.md) — sổ bug/issue/tính năng (mã BUG-*/OPS-*/QUAL-*/N-*).
- [test_eval.md](test_eval.md) — smoke test + cách eval chất lượng.
- [need_exchange.md](need_exchange.md) — câu hỏi cần làm rõ với khách hàng.
- [../README.md](../README.md) — giới thiệu & cách dùng.
- [../API.md](../API.md) — hợp đồng HTTP đầy đủ.
- [../DEPLOY.md](../DEPLOY.md) — bối cảnh triển khai (2 dự án: chụp ảnh + crop).
- [../src/api_3rd/README.md](../src/api_3rd/README.md) — trang test thủ công cho QC Scanner **bên ngoài** (dự án chị em `qc_scanner`), không phải API của dự án này.
- [`../../qc_scanner`](../../qc_scanner) — dự án chị em, cùng bài toán, cách tiếp cận khác, đã có lịch sử trao đổi khách hàng chi tiết đáng tham khảo.
