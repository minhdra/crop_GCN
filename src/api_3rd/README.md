# api_3rd — Trang test thủ công cho QC Scanner API (bên thứ 3)

Thư mục này **không phải** là API crop/scan của dự án này (xem [API.md](../../API.md)
ở gốc repo cho API đó). Đây là một trang tĩnh (`index.html`) để người dùng
chụp/tải ảnh tài liệu bằng tay và gọi sang một **QC Scanner API bên ngoài**
(third-party) để kiểm tra chất lượng + nắn phẳng ảnh, phục vụ test/demo thủ
công. Trình duyệt không gọi thẳng API đó mà đi qua một reverse proxy nginx
đóng gói cùng image, vì lý do HTTPS/mixed-content nêu bên dưới.

## Các file

| File | Vai trò |
|---|---|
| `index.html` | Giao diện: mở camera chụp ảnh hoặc kéo-thả/tải nhiều ảnh **hoặc PDF**, gửi từng file sang API, hiện verdict (`pass`/`warn`/`fail`) + lý do + ảnh kết quả (PDF nhiều trang hiện một dòng/trang), có màn so sánh ảnh gốc/kết quả, và nút "Xem PDF"/"Tải PDF" để lấy kết quả dạng PDF gộp (xem [Xem PDF kết quả](#xem-pdf-ket-qua)). |
| `config.js.template` | Template sinh `config.js` (đặt `window.QC_SCANNER_API_KEY`) lúc container khởi động - xem [Cấu hình API key](#cau-hinh-api-key). |
| `docker-entrypoint.d/40-inject-api-key.sh` | Script tự chạy lúc container khởi động (theo cơ chế `docker-entrypoint.d/` của image `nginx:alpine`), envsubst `config.js.template` → `config.js` từ biến môi trường `QC_SCANNER_API_KEY`. |
| `Dockerfile.web` | Build image `nginx:alpine` phục vụ `index.html` tĩnh + cấu hình reverse proxy. |
| `nginx.web.conf` | Cấu hình nginx: serve `index.html` ở `/`, reverse-proxy `/api/` sang API thật. |
| `docker-compose.web.yml` | Compose để build + chạy container ở trên, expose cổng `8090`, đọc `QC_SCANNER_API_KEY` từ `.env`. |
| `.env.example` | Mẫu file biến môi trường - sao chép thành `.env` rồi điền key thật (không commit `.env`). |

## Vì sao cần reverse proxy thay vì gọi thẳng IP API

Trang này thường được host sau HTTPS (vd một domain staging), trong khi QC
Scanner API thật chỉ chạy HTTP thuần trong LAN, không TLS. Trình duyệt chặn
đứt request HTTP thuần từ một trang HTTPS ("mixed content") — không liên
quan gì tới CORS, và bật CORS ở API cũng không giải quyết được. Cho nginx
của container này reverse-proxy `/api/` sang API thật giải quyết cả hai:
mọi request từ trình duyệt đều same-origin (chỉ có HTTPS), còn chặng
nginx → API là server-to-server nên HTTP thuần không sao.

## Cấu hình API key {#cau-hinh-api-key}

QC Scanner API thật **bắt buộc** một API key ở header `Authorization: Bearer
<key>` cho mọi request xử lý ảnh (xem
[`qc_scanner/docs/api.md` §2 - Xác thực](https://github.com/Jester6136/qc_scanner/blob/main/docs/api.md#xac-thuc)).
Thiếu/sai key → `401` + `{"error": {"code": "UNAUTHORIZED", ...}}`.

`index.html` là file tĩnh, không có backend riêng để giữ key ngoài mã chạy
trên trình duyệt — nhưng vẫn không hardcode key thẳng vào `index.html` để
key không lọt vào lịch sử git. Thay vào đó, key được truyền qua biến môi
trường và ghép vào lúc container khởi động (không phải lúc build image):

```bash
cd src/api_3rd
cp .env.example .env
# sửa .env, điền QC_SCANNER_API_KEY=qcs-... (key cấp cho client "app-web")
```

Lúc container khởi động, `docker-entrypoint.d/40-inject-api-key.sh` chạy
`envsubst` trên `config.js.template` để sinh `config.js` chứa
`window.QC_SCANNER_API_KEY`; `index.html` nạp file đó qua `<script
src="config.js">` trước khi script chính chạy. Thiếu `QC_SCANNER_API_KEY` thì
`docker compose up` báo lỗi rõ ràng và container **không khởi động**, thay vì
chạy "thành công" với key rỗng rồi mọi request từ trình duyệt âm thầm bị
`401`.

> ⚠️ Trang vẫn chạy trong trình duyệt — ai mở DevTools/View Source đều đọc
> được `config.js` và thấy key thật (đúng cảnh báo trong api.md §2: gọi từ
> trình duyệt nghĩa là key nằm trong mã chạy ở máy người dùng). Cách trên chỉ
> giải quyết việc **key không bị commit vào git**, không giấu được key khỏi
> người dùng cuối. Chỉ chấp nhận được vì trang này dùng nội bộ trong LAN —
> đừng expose ra Internet mà không có thêm một lớp xác thực khác phía trước.
> Đổi key (thu hồi/xoay) thì sửa `.env` rồi `docker compose up --build -d`
> lại — key được đọc một lần lúc container khởi động, không đổi được khi
> đang chạy.

## Build & chạy

```bash
cd src/api_3rd
docker compose -f docker-compose.web.yml up --build -d
```

Trang test sẽ chạy ở `http://<IP-máy-này>:8090`.

Kiểm tra container:

```bash
docker compose -f docker-compose.web.yml ps
docker compose -f docker-compose.web.yml logs -f
```

Dừng:

```bash
docker compose -f docker-compose.web.yml down
```

## Trỏ sang API thật

Mặc định `nginx.web.conf` proxy `/api/` sang `http://192.168.120.9:5000` —
đây chỉ là placeholder. Trước khi build, sửa đúng một chỗ:

```
# nginx.web.conf
location /api/ {
    ...
    proxy_pass http://<IP>:<PORT>;   # <-- đổi thành địa chỉ QC Scanner API thật
}
```

Rồi build lại image (`docker compose -f docker-compose.web.yml up --build -d`).
Không sửa `API_BASE` trong `index.html` — hằng số đó luôn trỏ `/api` (cùng
origin, qua proxy), không trỏ thẳng IP API.

Nếu chỉ muốn mở cổng `8090` trên chính máy này (có reverse proxy/ingress
khác đứng trước lo TLS), đổi `"8090:80"` thành `"127.0.0.1:8090:80"` trong
`docker-compose.web.yml`.

## Hợp đồng API mà trang này gọi

Trang gọi cùng một endpoint `POST /` với hai kiểu `format` khác nhau tuỳ mục
đích, multipart field `file`, mỗi request một **file** — ảnh hoặc PDF, định
dạng do API tự dò từ nội dung file (không dựa vào tên file hay
`Content-Type`). Mọi request kèm header `Authorization: Bearer <key>` (xem
[Cấu hình API key](#cau-hinh-api-key)).

- `POST /?format=json[&audience=capturer|operator][&pre_cropped=1]` — phán
  quyết đầy đủ (`verdict`/`reasons`/`image` base64 PNG từng trang), dùng để
  render bảng kết quả. `200`/`422` kèm JSON.
- `POST /?format=pdf[&audience=...][&pre_cropped=1]` — MỘT file PDF gộp toàn
  bộ trang đã nắn (ảnh rời ra PDF một trang, PDF nhiều trang ra PDF nhiều
  trang), dùng cho nút "Xem PDF"/"Tải PDF" (xem [Xem PDF kết
  quả](#xem-pdf-ket-qua)). Cùng cặp file `file` gửi hai lần với hai `format`
  khác nhau — server không cho gộp `json` và `pdf` trong một request.

Chi tiết đầy đủ + toàn bộ danh mục mã lý do nằm trong tài liệu của QC Scanner
API (không thuộc repo này):
[`qc_scanner/docs/api.md`](https://github.com/Jester6136/qc_scanner/blob/main/docs/api.md).

## Upload PDF

API thật nhận PDF trực tiếp ở cùng endpoint/field `file` như ảnh (tối đa 50
trang, giới hạn `PDF_TOO_MANY_PAGES`) — `index.html` gửi thẳng file PDF gốc
sang API, **không** tự xử lý/tách trang ở trình duyệt.

Hình dạng response khác nhau tuỳ input, và `index.html` xử lý cả hai
(`normalizeApiResult()` trong script):

- Ảnh rời, hoặc PDF **một trang**: response phẳng như ảnh thường —
  `{ verdict, reasons[], image (base64 PNG) }`.
- PDF **nhiều trang**: luôn ra JSON (kể cả không có `?format=json`) với hình
  dạng khác — `{ source: "pdf", verdict, page_count, pages: [{ page, verdict,
  reasons[], metrics, image }, ...] }`. `verdict` gộp là **trang tệ nhất**,
  không phải trang đầu. Trang này tách kết quả thành **một dòng/trang** trong
  bảng kết quả, thay vì chỉ hiện verdict gộp — để không giấu mất trang lỗi
  đứng sau các trang tốt.

Với PDF, mỗi trang scan được API chấm với `pre_cropped` bật sẵn (trang PDF
coi như tờ giấy đã cắt sát) — không liên quan tới checkbox "Ảnh đã cắt sát từ
trước" trên trang này, checkbox đó chỉ áp dụng cho ảnh/PDF do người dùng tự
đánh dấu qua tham số `pre_cropped` gửi kèm request.

Nút "So sánh" (ảnh gốc/kết quả) không hiện với kết quả từ PDF — `<img>` không
tự render được file PDF làm vế "ảnh gốc", nên trang bỏ qua so sánh cho case
này; "Xem"/"Tải" ảnh kết quả từng trang vẫn hoạt động bình thường.

## Xem PDF kết quả {#xem-pdf-ket-qua}

Bảng kết quả có thêm hai nút ở dòng đầu tiên của mỗi file: **"Xem PDF"** (mở
tab mới) và **"Tải PDF"** (tải về máy). Cả hai gọi lại API với
`?format=pdf` (xem [Hợp đồng API](#hợp-đồng-api-mà-trang-này-gọi)) để lấy
**một** file PDF gộp toàn bộ trang đã nắn — khác với "Xem"/"Tải" ở mỗi
dòng/trang vốn chỉ cho ra ảnh PNG rời từng trang.

Vì sao gọi lại thay vì dùng luôn ảnh đã có: response `?format=json` (dùng để
render bảng) chỉ có ảnh PNG base64 từng trang, không có file PDF gộp sẵn —
phải xin riêng bằng `?format=pdf`. Trang **không** tự ghép các PNG đó thành
PDF ở trình duyệt (dễ lệch màu/nén so với PDF server sinh ra bằng pdfium,
xem api.md §3). Gọi lại cũng chỉ tốn thêm một request **khi người dùng thật
sự bấm xem/tải**, không tốn gì nếu không ai cần đến PDF.

Nút chỉ gắn khi lần gọi `?format=json` ban đầu không phải lỗi cục bộ
(mạng/HTTP) — verdict `fail` (422) vẫn có nút, bấm vào sẽ nhận đúng lỗi
`verdict: fail` từ `?format=pdf` (server không trả file PDF cho ảnh không
đạt, đúng quy tắc PNG mặc định).

## Lưu ý

- `client_max_body_size 32m` trong `nginx.web.conf` phải khớp giới hạn
  upload thật của API — mặc định nginx chỉ nhận 1 MB, thấp hơn nhiều, sẽ tự
  trả `413` trước khi API kịp thấy. Đổi cả hai cùng lúc nếu giới hạn thay
  đổi.
- Chưa build/chạy thử trên máy thật; đã build + chạy thử cục bộ (curl tới
  container) lúc viết các file này — xem comment trong `docker-compose.web.yml`.
- Đã kiểm chứng cục bộ: container dừng đúng lúc thiếu `QC_SCANNER_API_KEY`,
  và `config.js` sinh đúng key khi có biến môi trường. **Chưa** gọi thử
  request thật kèm key này tới QC Scanner API (không có sẵn instance để
  test) — kiểm tra lại bằng `curl -H "Authorization: Bearer $KEY" ...` (xem
  ví dụ trong api.md §8) trước khi coi là đã xác thực xong với server thật.
