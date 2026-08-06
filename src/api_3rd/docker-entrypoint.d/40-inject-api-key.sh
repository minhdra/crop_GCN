#!/bin/sh
# Chạy tự động bởi entrypoint gốc của image nginx:alpine (mọi script thực thi
# trong /docker-entrypoint.d/ được chạy trước khi nginx khởi động) - sinh
# config.js thật từ config.js.template bằng envsubst, thay QC_SCANNER_API_KEY
# đọc từ biến môi trường container (xem docker-compose.web.yml + README.md).
#
# Dừng hẳn nếu thiếu key thay vì âm thầm sinh ra "${QC_SCANNER_API_KEY}" trần
# vào config.js - lỗi đó chỉ lộ ra sau khi mọi request từ trình duyệt bị 401,
# khó truy hơn nhiều so với container không khởi động được.
set -eu

: "${QC_SCANNER_API_KEY:?Thiếu biến môi trường QC_SCANNER_API_KEY - xem README.md (mục Cấu hình API key)}"

envsubst '${QC_SCANNER_API_KEY}' \
  < /usr/share/nginx/html/config.js.template \
  > /usr/share/nginx/html/config.js
