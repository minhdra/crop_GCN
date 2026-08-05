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
| `index.html` | Giao diện: mở camera chụp ảnh hoặc kéo-thả/tải nhiều ảnh **hoặc PDF**, gửi từng file sang API, hiện verdict (`pass`/`warn`/`fail`) + lý do + ảnh kết quả (PDF nhiều trang hiện một dòng/trang), có màn so sánh ảnh gốc/kết quả. |
| `Dockerfile.web` | Build image `nginx:alpine` phục vụ `index.html` tĩnh + cấu hình reverse proxy. |
| `nginx.web.conf` | Cấu hình nginx: serve `index.html` ở `/`, reverse-proxy `/api/` sang API thật. |
| `docker-compose.web.yml` | Compose để build + chạy container ở trên, expose cổng `8090`. |

## Vì sao cần reverse proxy thay vì gọi thẳng IP API

Trang này thường được host sau HTTPS (vd một domain staging), trong khi QC
Scanner API thật chỉ chạy HTTP thuần trong LAN, không TLS. Trình duyệt chặn
đứt request HTTP thuần từ một trang HTTPS ("mixed content") — không liên
quan gì tới CORS, và bật CORS ở API cũng không giải quyết được. Cho nginx
của container này reverse-proxy `/api/` sang API thật giải quyết cả hai:
mọi request từ trình duyệt đều same-origin (chỉ có HTTPS), còn chặng
nginx → API là server-to-server nên HTTP thuần không sao.

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

Trang gọi một endpoint duy nhất: `POST /?format=json[&audience=capturer|operator][&pre_cropped=1]`,
multipart field `file`, mỗi request một **file** — ảnh hoặc PDF, định dạng do
API tự dò từ nội dung file (không dựa vào tên file hay `Content-Type`).
Response `200`/`422` kèm JSON. Chi tiết đầy đủ + toàn bộ danh mục mã lý do
nằm trong tài liệu của QC Scanner API (không thuộc repo này):
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

## Lưu ý

- `client_max_body_size 32m` trong `nginx.web.conf` phải khớp giới hạn
  upload thật của API — mặc định nginx chỉ nhận 1 MB, thấp hơn nhiều, sẽ tự
  trả `413` trước khi API kịp thấy. Đổi cả hai cùng lúc nếu giới hạn thay
  đổi.
- Chưa build/chạy thử trên máy thật; đã build + chạy thử cục bộ (curl tới
  container) lúc viết các file này — xem comment trong `docker-compose.web.yml`.
