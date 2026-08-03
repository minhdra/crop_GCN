#!/usr/bin/env python3
"""Benchmark API /api/scan bằng cách gửi lần lượt (hoặc song song) các file
trong một thư mục input tới server đang chạy.

Chỉ dùng thư viện chuẩn (urllib) - không cần cài thêm gói gì để chạy trên
server.

Ví dụ:
    python benchmark.py
    python benchmark.py --dir input_files --url http://127.0.0.1:8090/api/scan
    python benchmark.py --concurrency 8 --repeat 3 --mode scan
    python benchmark.py --output results.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from urllib import error, request

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp", ".heic"}
VALID_EXTENSIONS = IMAGE_EXTENSIONS | {".pdf"}


def build_multipart_body(file_path: Path, fields: dict[str, str]) -> tuple[bytes, str]:
    """Tự dựng multipart/form-data body (không phụ thuộc thư viện ngoài)."""
    boundary = uuid.uuid4().hex
    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    parts: list[bytes] = []

    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )

    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n".encode()
    )
    parts.append(file_path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())

    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


@dataclass
class BenchResult:
    filename: str
    ok: bool
    http_status: int | None
    api_status: str | None
    elapsed: float
    warnings: list[str]
    error: str | None


def run_one(url: str, file_path: Path, fields: dict[str, str], timeout: float) -> BenchResult:
    body, content_type = build_multipart_body(file_path, fields)
    req = request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", content_type)

    start = time.perf_counter()
    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            elapsed = time.perf_counter() - start
            payload = json.loads(raw)
            return BenchResult(
                filename=file_path.name,
                ok=payload.get("status") != "error",
                http_status=response.status,
                api_status=payload.get("status"),
                elapsed=elapsed,
                warnings=payload.get("warnings", []),
                error=payload.get("message"),
            )
    except error.HTTPError as exc:
        elapsed = time.perf_counter() - start
        detail = exc.read().decode(errors="replace")
        return BenchResult(
            filename=file_path.name,
            ok=False,
            http_status=exc.code,
            api_status=None,
            elapsed=elapsed,
            warnings=[],
            error=detail,
        )
    except (error.URLError, TimeoutError, OSError) as exc:
        elapsed = time.perf_counter() - start
        return BenchResult(
            filename=file_path.name,
            ok=False,
            http_status=None,
            api_status=None,
            elapsed=elapsed,
            warnings=[],
            error=str(exc),
        )


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[index]


def print_summary(results: list[BenchResult], wall_time: float) -> None:
    total = len(results)
    ok = [r for r in results if r.ok]
    errors = [r for r in results if not r.ok]
    warnings = [r for r in ok if r.api_status == "warning"]
    latencies = [r.elapsed for r in results]

    print("\n" + "=" * 60)
    print("KẾT QUẢ BENCHMARK")
    print("=" * 60)
    print(f"Tổng số request     : {total}")
    print(f"  success            : {len(ok) - len(warnings)}")
    print(f"  warning            : {len(warnings)}")
    print(f"  error              : {len(errors)}")
    print(f"Tổng thời gian chạy  : {wall_time:.2f}s")
    print(f"Throughput           : {total / wall_time:.2f} request/s" if wall_time > 0 else "")
    if latencies:
        print("-" * 60)
        print(f"Latency min          : {min(latencies):.3f}s")
        print(f"Latency avg          : {statistics.mean(latencies):.3f}s")
        print(f"Latency p50          : {percentile(latencies, 50):.3f}s")
        print(f"Latency p95          : {percentile(latencies, 95):.3f}s")
        print(f"Latency p99          : {percentile(latencies, 99):.3f}s")
        print(f"Latency max          : {max(latencies):.3f}s")

    if errors:
        print("-" * 60)
        print("Chi tiết lỗi:")
        for r in errors:
            detail = (r.error or "")[:200]
            print(f"  [{r.http_status}] {r.filename}: {detail}")
    print("=" * 60)


def write_csv(results: list[BenchResult], path: Path) -> None:
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["filename", "ok", "http_status", "api_status", "elapsed_s", "warnings", "error"])
        for r in results:
            writer.writerow(
                [r.filename, r.ok, r.http_status, r.api_status, f"{r.elapsed:.3f}", "; ".join(r.warnings), r.error or ""]
            )
    print(f"Đã ghi chi tiết từng request vào {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark endpoint /api/scan với các file trong một thư mục.")
    parser.add_argument("--dir", default="input_files", help="Thư mục chứa file input (mặc định: input_files)")
    parser.add_argument("--url", default="http://127.0.0.1:8090/api/scan", help="URL endpoint /api/scan")
    parser.add_argument("--mode", default="scan", choices=["color", "scan", "bw"])
    parser.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270])
    parser.add_argument("--sharpness", type=float, default=0.7)
    parser.add_argument("--min-area-ratio", type=float, default=0.2, dest="min_area_ratio")
    parser.add_argument("--crop", type=lambda s: s.lower() != "false", default=True)
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--jpeg-quality", type=int, default=92, dest="jpeg_quality")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--concurrency", type=int, default=1, help="Số request chạy song song (giả lập tải thật)")
    parser.add_argument("--repeat", type=int, default=1, help="Số lần lặp lại toàn bộ danh sách file")
    parser.add_argument("--timeout", type=float, default=120.0, help="Timeout mỗi request (giây)")
    parser.add_argument("--output", type=Path, default=None, help="Ghi chi tiết từng request ra file CSV")
    args = parser.parse_args()

    input_dir = Path(args.dir)
    if not input_dir.is_dir():
        print(f"Không tìm thấy thư mục: {input_dir}", file=sys.stderr)
        sys.exit(1)

    files = sorted(p for p in input_dir.iterdir() if p.is_file() and p.suffix.lower() in VALID_EXTENSIONS)
    if not files:
        print(f"Thư mục {input_dir} không có file PDF/ảnh hợp lệ.", file=sys.stderr)
        sys.exit(1)

    jobs = files * args.repeat
    fields = {
        "mode": args.mode,
        "rotate": str(args.rotate),
        "sharpness": str(args.sharpness),
        "min_area_ratio": str(args.min_area_ratio),
        "crop": "true" if args.crop else "false",
        "dpi": str(args.dpi),
        "jpeg_quality": str(args.jpeg_quality),
        "debug": "true" if args.debug else "false",
    }

    print(f"Đang benchmark {len(jobs)} request ({len(files)} file x {args.repeat} lần) "
          f"tới {args.url} với concurrency={args.concurrency} ...")
    print(f"Tham số: {fields}")

    results: list[BenchResult] = []
    start = time.perf_counter()

    if args.concurrency <= 1:
        for path in jobs:
            result = run_one(args.url, path, fields, args.timeout)
            results.append(result)
            print(f"  {result.filename:40s} {result.api_status or 'ERROR':10s} {result.elapsed:.3f}s")
    else:
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            future_to_path = {executor.submit(run_one, args.url, path, fields, args.timeout): path for path in jobs}
            for future in as_completed(future_to_path):
                result = future.result()
                results.append(result)
                print(f"  {result.filename:40s} {result.api_status or 'ERROR':10s} {result.elapsed:.3f}s")

    wall_time = time.perf_counter() - start
    print_summary(results, wall_time)

    if args.output:
        write_csv(results, args.output)


if __name__ == "__main__":
    main()
