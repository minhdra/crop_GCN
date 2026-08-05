# Cần trao đổi / làm rõ với khách hàng — py-project (crop_GCN)

> Nơi ghi các điểm **cần dựa vào tài liệu/dữ liệu khách hàng cung cấp** để đặt
> câu hỏi và làm rõ trước khi quyết định thiết kế/nghiệm thu. Mỗi mục: **câu
> hỏi**, **vì sao cần**, **ảnh hưởng nếu chưa rõ**, **trạng thái**
> (❓ chờ hỏi · 💬 đã hỏi chờ trả lời · ✅ đã chốt).
>
> ⚠️ Toàn bộ mục dưới đây đang ở trạng thái **❓ chưa hỏi** — tài liệu này mới
> được dựng khung (2026-08-05), chưa có buổi trao đổi chính thức nào với
> khách được ghi lại cho riêng dự án `py-project`/`crop_GCN`. Dự án chị em
> `qc_scanner` (`../../qc_scanner/docs/need_exchange.md`) đã chốt được
> 12/13 câu hỏi tương tự cho bài toán tương tự (crop tài liệu) — nhiều câu
> dưới đây **có thể đã có câu trả lời ở đó**, cần đối chiếu trước khi hỏi lại
> khách để tránh hỏi trùng.

---

## A. Dữ liệu & bối cảnh sử dụng

### EX-1 · ❓ Quy mô, tốc độ, hình thức triển khai thật

- **Hỏi**: Bao nhiêu ảnh/ngày qua hệ này? Xử lý theo lô (batch) hay thời gian
  thực lúc người dùng chụp? Chạy trên hạ tầng nào (CPU/GPU, số core, RAM)?
- **Vì sao**: [DEPLOY.md](../DEPLOY.md) ghi "lượng user/request sử dụng chụp
  ảnh lớn" và cấu hình `cpus: "4"` / `memory: 4g` trong
  [docker-compose.yml](../docker-compose.yml) đang giả định một ngân sách cụ
  thể (4 core) — nhưng **không rõ số này dựa trên đo đạc thật hay ước lượng**.
- **Nếu chưa rõ**: không biết `WEB_CONCURRENCY`/`SCAN_MAX_CONCURRENT_JOBS`
  hiện tại có đủ hay thiếu cho tải thật, và không biết khi nào cần chuyển
  sang kiến trúc hàng đợi — xem
  [features_issues.md OPS-3](features_issues.md#ops-3-sync-arch).

### EX-2 · ❓ Tập ảnh thật + tập vàng có nhãn

- **Hỏi**: Xin ≥100 ảnh thật đại diện (bao gồm cả ca xấu: mờ, nghiêng, nền
  lẫn, thiếu góc, ánh sáng kém). Khách gán nhãn 4 góc được không, hay bên làm
  gán rồi khách duyệt?
- **Vì sao**: `input_files/` hiện chỉ có một vài ảnh mẫu
  (vd `gcn_1_1783321893242.jpg`), **chưa có nhãn**. Không có tập vàng thì
  không chốt được ngưỡng nào trong
  [algorithm.md §6](algorithm.md#6-tham-số--ngưỡng-mặc-định) bằng số đo —
  toàn bộ đang là heuristic đặt theo cảm tính.
- **Nếu chưa rõ**: không đo được crop rate / false pass / false fail thật,
  không chứng minh được chất lượng lúc nghiệm thu. Xem
  [test_eval.md §5](test_eval.md#5-eval-chất-lượng-khi-có-tập-ảnh-vàng-khi-có-nhãn-thật).

### EX-3 · ❓ "Đạt" nghĩa là gì — tiêu chí nghiệm thu

- **Hỏi**: Ảnh đầu ra thế nào thì được coi là đạt? IoU tối thiểu so với biên
  tài liệu thật là bao nhiêu? Nghiêng bao nhiêu độ vẫn chấp nhận? Có bắt buộc
  thấy trọn 4 góc không?
- **Vì sao**: quyết định ngưỡng chấp nhận IoU trong quy trình eval
  ([test_eval.md §5](test_eval.md#5-eval-chất-lượng-khi-có-tập-ảnh-vàng-khi-có-nhãn-thật))
  và cân bằng false-pass/false-fail khi quét ngưỡng (`min_area_ratio`,
  `blur_threshold`, `solidity_threshold`).
- **Nếu chưa rõ**: chấm điểm theo chuẩn tự đặt, nghiệm thu theo chuẩn khách →
  lệch nhau lúc bàn giao.

### EX-4 · ❓ Loại tài liệu & đặc điểm giấy

- **Hỏi**: Loại giấy tờ nào (CCCD, sổ đỏ/GCN, hoá đơn, biểu mẫu A4...)? Khổ
  cố định hay đa dạng? Giấy màu hay trắng? Có khung viền in sẵn/bảng kẻ
  không?
- **Vì sao**: `min_area_ratio=0.2` và các ngưỡng contour hiện đặt chung cho
  mọi loại giấy. Tài liệu có khung viền in sẵn dễ khiến contour bắt nhầm
  đường kẻ trong tài liệu thay vì mép giấy thật (cùng vấn đề dự án chị em
  `qc_scanner` từng gặp — `../../qc_scanner/docs/need_exchange.md EX-4`).
- **Nếu chưa rõ**: ngưỡng tối ưu cho loại giấy này có thể sai hoàn toàn cho
  loại giấy khác.

### EX-5 · ❓ Ảnh vào từ đâu, đi tiếp vào đâu

- **Hỏi**: Ảnh tới từ app chụp ảnh (mô tả trong [DEPLOY.md](../DEPLOY.md):
  "2 dự án: Chụp ảnh và crop/phát hiện mờ" — dự án hiện tại là phía crop) hay
  còn nguồn nào khác (kho ảnh cũ, máy scan)? Kết quả crop đi tiếp vào hệ nào
  (OCR/VLM, lưu trữ, người đọc)?
- **Vì sao**: nếu người chụp **có mặt tại chỗ** lúc QC chặn (`assess_capture_quality`)
  từ chối, hint "chụp lại" hành động được ngay — đúng như luồng camera hiện
  tại đã giả định. Nếu về sau có thêm nguồn ảnh tồn kho (không chụp lại
  được), QC chặn cứng kiểu hiện tại **không áp dụng được** cho nguồn đó, cần
  một mô hình cảnh báo mềm giống `assess_quality` thay vì chặn.
- **Nếu chưa rõ**: giả định "luôn chụp lại được" trong thiết kế QC camera có
  thể sai khi có nguồn ảnh khác.

## B. Chính sách QC & ngưỡng

### EX-6 · ❓ Cân bằng false pass vs false fail

- **Hỏi**: Khách sợ cái nào hơn — ảnh crop sai lọt qua rồi sinh dữ liệu sai
  (*false pass*), hay ảnh dùng được bị từ chối/giữ nguyên không crop (*false
  fail*)? Chi phí mỗi bên ước chừng ra sao?
- **Vì sao**: quyết định hướng quét ngưỡng khi có tập vàng
  ([QUAL-1](features_issues.md#qual-1-heuristic-thresholds)). Hiện tại thiết
  kế nghiêng về "an toàn hơn" (giữ nguyên ảnh gốc nếu không tìm được góc,
  thay vì crop liều) nhưng **chưa có xác nhận từ khách** đây có đúng hướng
  ưu tiên không.
- **Nếu chưa rõ**: siết/nới ngưỡng theo cảm tính, dễ cãi nhau lúc nghiệm thu.

### EX-7 · ❓ Ảnh cảnh báo (`warning`) thì xử lý ra sao

- **Hỏi**: Khi `/api/scan` trả `status=warning` (mờ hoặc nghi rách nhưng vẫn
  crop) — ai xem cảnh báo này? Có hàng chờ người soi không, hay hệ gọi tự
  quyết định dùng/bỏ ảnh?
- **Vì sao**: quyết định `warnings` trong response hiện tại có đủ hay cần
  thêm cơ chế khác (webhook, hàng đợi review) — xem
  [features_issues.md D §N-1](features_issues.md#d-features--đề-xuất-backlog).
- **Nếu chưa rõ**: làm ra cảnh báo mà không ai tiêu thụ được, hoặc không đủ
  cơ chế cho quy trình vận hành thật.

### EX-8 · ❓ Ảnh chụp trực tiếp bị QC từ chối thì quy trình ra sao

- **Hỏi**: Khi `/api/capture/scan`/`/api/capture/check` trả `passed=false` —
  giao diện có bắt buộc chụp lại ngay không, hay cho phép người dùng bỏ qua
  (dùng ảnh dù chưa đạt)? Có giới hạn số lần chụp lại không?
- **Vì sao**: hiện tại QC camera là **chặn cứng tuyệt đối** (không có cách
  nào vượt qua từ phía API) — cần xác nhận đây đúng là hành vi mong muốn,
  không phải "cảnh báo, người dùng tự quyết".
- **Nếu chưa rõ**: có thể đang chặn cứng ở nơi lẽ ra chỉ cần cảnh báo, gây
  khó chịu cho người dùng thật.

## C. Vận hành & phi chức năng

### EX-9 · ❓ Bảo mật & lưu trữ ảnh tài liệu

- **Hỏi**: Ảnh chứa thông tin cá nhân (CCCD, sổ đỏ, chữ ký...)? Được lưu tạm
  bao lâu là hợp lý? Server đặt ở mạng nội bộ hay có thể lộ ra ngoài? Có yêu
  cầu xác thực (auth) không?
- **Vì sao**: API hiện **không có auth**
  ([OPS-1](features_issues.md#ops-1-no-auth)), và có lưu bản sao lâu dài vào
  `OUTPUT_DIR`/`output_scans/` (mặc định `SCAN_OUTPUT_TTL_DAYS=1`,
  nhưng đang lưu **trên đĩa, không mã hoá**, phục vụ đối soát).
- **Nếu chưa rõ**: có thể vi phạm yêu cầu bảo mật dữ liệu cá nhân của khách
  (đặc biệt nếu tài liệu là giấy tờ tuỳ thân) mà không ai phát hiện cho tới
  lúc nghiệm thu/audit.

### EX-10 · ❓ Giới hạn upload & timeout có khớp thực tế phía gọi không

- **Hỏi**: App chụp ảnh (phía gọi vào, xem [DEPLOY.md](../DEPLOY.md)) có
  giới hạn kích thước ảnh gửi lên là bao nhiêu? Timeout client chấp nhận chờ
  một request xử lý là bao lâu?
- **Vì sao**: `SCAN_MAX_UPLOAD_MB=50` (mặc định) và `SCAN_QUEUE_TIMEOUT_SECONDS=20`
  hiện đặt theo phỏng đoán, cần khớp với giá trị thật của app gọi vào — nếu
  có reverse proxy đứng trước (như mô hình ở
  [src/api_3rd/README.md](../src/api_3rd/README.md) của dự án chị em) thì
  còn phải khớp thêm `client_max_body_size` của proxy đó.
- **Nếu chưa rõ**: client bị từ chối oan (413/503) hoặc ngược lại, server
  nhận file quá khổ do giới hạn đặt lỏng hơn thực tế cần.

### EX-11 · ❓ Định dạng đầu vào cần hỗ trợ đầy đủ chưa

- **Hỏi**: Ngoài `.jpg/.jpeg/.png/.bmp/.tiff/.webp/.heic` và `.pdf` đã hỗ trợ
  — có định dạng nào khác cần thêm không (vd `.pdf` nhiều trang scan lẫn ảnh
  chụp, PDF có mật khẩu)?
- **Vì sao**: `scan_pdf` hiện **từ chối thẳng** PDF có mật khẩu
  (`source_pdf.needs_pass`) — cần biết đây có phải ca thực tế sẽ gặp không.
- **Nếu chưa rõ**: có thể phải xử lý thêm ca này sau khi đã bàn giao.

## Cách dùng file này

- Trước mỗi buổi làm việc với khách: lọc mục ❓, chuẩn bị câu hỏi + tài
  liệu/dữ liệu cần xin, **đối chiếu trước với
  `../../qc_scanner/docs/need_exchange.md`** — nhiều câu hỏi ở đây là bản
  song song của câu hỏi đã chốt bên đó cho cùng loại bài toán.
- Sau khi có câu trả lời: đổi trạng thái ❓ → 💬 (đã hỏi, chờ) → ✅ (đã chốt),
  ghi rõ **ngày** + **nội dung đã chốt** ngay dưới tiêu đề mục (theo đúng mẫu
  đang dùng ở `qc_scanner`).
- Quyết định chốt ảnh hưởng ngưỡng/code → tạo hoặc cập nhật issue tương ứng
  trong [features_issues.md](features_issues.md); nếu chốt một **ngưỡng cụ
  thể**, ghi thẳng vào [algorithm.md §6](algorithm.md#6-tham-số--ngưỡng-mặc-định)
  kèm nguồn.
