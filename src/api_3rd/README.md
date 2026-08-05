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
| `index.html` | Giao diện: mở camera chụp ảnh hoặc kéo-thả/tải nhiều ảnh **hoặc PDF**, gửi từng ảnh sang API, hiện verdict (`pass`/`warn`/`fail`) + lý do + ảnh kết quả, có màn so sánh ảnh gốc/kết quả. |
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
multipart field `file`, mỗi request một ảnh. Response `200`/`422` kèm JSON
`{ verdict, reasons[], image (base64 PNG) }`. Chi tiết đầy đủ nằm trong tài
liệu của QC Scanner API (không thuộc repo này).

## Upload PDF

API thật không có endpoint nhận PDF (chỉ nhận một ảnh mỗi request, xem mục
trên) — nên khi người dùng tải lên một file `.pdf`, `index.html` tự tách từng
trang thành ảnh JPEG **ngay trên trình duyệt** (thư viện
[`pdf.js`](https://mozilla.github.io/pdf.js/), tải qua CDN `cdnjs`, phiên bản
`3.11.174`) rồi đưa từng trang vào cùng danh sách file/luồng xử lý như ảnh
chụp thường (một request/trang sang API). File PDF gốc không được gửi lên
server — mọi việc đọc PDF diễn ra cục bộ.

Vì phụ thuộc CDN ngoài, trang cần Internet ra ngoài khi build/host (không chỉ
mạng nội bộ tới API) để tải được `pdf.min.js`/`pdf.worker.min.js`. Nếu triển
khai trong mạng cô lập hoàn toàn, cần tự host hai file đó thay vì trỏ CDN.

## Lưu ý

- `client_max_body_size 32m` trong `nginx.web.conf` phải khớp giới hạn
  upload thật của API — mặc định nginx chỉ nhận 1 MB, thấp hơn nhiều, sẽ tự
  trả `413` trước khi API kịp thấy. Đổi cả hai cùng lúc nếu giới hạn thay
  đổi.
- Chưa build/chạy thử trên máy thật; đã build + chạy thử cục bộ (curl tới
  container) lúc viết các file này — xem comment trong `docker-compose.web.yml`.
