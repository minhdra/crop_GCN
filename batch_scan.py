"""Crop và làm rõ PDF / Ảnh (lẻ hoặc thư mục) theo kiểu scanner.
Ví dụ:
# Xử lý PDF
python batch_scan.py input.pdf output_scan.pdf --mode scan --dpi 250

# Xử lý 1 ảnh lẻ
python batch_scan.py photo.jpg scan_result.jpg --mode bw

# Xử lý hàng loạt ảnh trong thư mục
python batch_scan.py ./mobile_photos ./scanned_photos --sharpness 1.2
"""
from __future__ import annotations
import argparse
from pathlib import Path
import sys

# Đảm bảo import được từ cấu trúc thư mục dự án của bạn
sys.path.insert(
    0,
    str(Path(__file__).parent / "src"),
)
from py_project.document_scanner import scan_pdf, scan_image, scan_images  # noqa: E402

# Các định dạng ảnh được hỗ trợ
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp", ".heic"}

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Crop và làm rõ PDF / Ảnh theo kiểu scanner. "
            "Hỗ trợ xử lý PDF, ảnh lẻ, hoặc toàn bộ thư mục chứa ảnh."
        )
    )
    parser.add_argument(
        "input",
        type=Path,
        help="Đường dẫn file PDF, file ảnh lẻ, hoặc thư mục chứa ảnh",
    )
    parser.add_argument(
        "output",
        type=Path,
        help="Đường dẫn file PDF, file ảnh lẻ, hoặc thư mục đầu ra",
    )
    parser.add_argument(
        "--mode",
        choices=("color", "scan", "bw"),
        default="scan",
        help="color: giữ màu; scan: ảnh xám sáng rõ chữ; bw: ảnh đen trắng",
    )
    parser.add_argument(
        "--rotate",
        type=int,
        choices=(0, 90, 180, 270),
        default=0,
        help="Xoay toàn bộ trang/ảnh theo chiều kim đồng hồ",
    )
    parser.add_argument(
        "--sharpness",
        type=float,
        default=0.7,
        help="Mức làm sắc nét, từ 0 đến 3 (mặc định: 0.7)",
    )
    parser.add_argument(
        "--min-area-ratio",
        type=float,
        default=0.2,
        help="Tỉ lệ diện tích tối thiểu của tài liệu để crop (mặc định: 0.2)",
    )
    parser.add_argument(
        "--no-crop",
        action="store_true",
        help="Không tự tìm viền tài liệu; chỉ làm rõ toàn bộ trang/ảnh",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Xuất thêm ảnh debug (contour vùng giấy, góc crop, blur_score/solidity) để soi vì sao crop/chất lượng ra như vậy",
    )

    # Các tham số riêng cho PDF
    parser.add_argument(
        "--dpi",
        type=int,
        default=200,
        help="[Chỉ áp dụng cho PDF] Độ phân giải khi chuyển PDF thành ảnh (mặc định: 200)",
    )
    parser.add_argument(
        "--jpeg-quality",
        type=int,
        default=92,
        help="[Chỉ áp dụng cho PDF] Chất lượng ảnh JPEG trong PDF đầu ra (mặc định: 92)",
    )

    args = parser.parse_args()

    # Validate tham số chung
    if not 0 < args.min_area_ratio < 1:
        parser.error("--min-area-ratio phải nằm trong khoảng 0 đến 1")
    if not 0 <= args.sharpness <= 3:
        parser.error("--sharpness phải nằm trong khoảng 0 đến 3")

    input_path = args.input
    output_path = args.output
    
    # Xác định loại đầu vào
    is_dir = input_path.is_dir()
    is_pdf = input_path.suffix.lower() == ".pdf"
    is_image = input_path.suffix.lower() in IMAGE_EXTENSIONS

    if not (is_dir or is_pdf or is_image):
        parser.error("Đầu vào phải là file PDF, file ảnh, hoặc thư mục chứa ảnh.")

    try:
        # =====================================================================
        # TRƯỜNG HỢP 1: ĐẦU VÀO LÀ THƯ MỤC (Xử lý hàng loạt ảnh)
        # =====================================================================
        if is_dir:
            if output_path.suffix.lower() == ".pdf":
                parser.error("Đầu ra không thể là file PDF khi đầu vào là thư mục ảnh.")
            
            summary = scan_images(
                input_dir=input_path,
                output_dir=output_path,
                min_area_ratio=args.min_area_ratio,
                mode=args.mode,
                rotation=args.rotate,
                sharpness=args.sharpness,
                crop=not args.no_crop,
                debug=args.debug,
            )
            print(f"\nĐã lưu vào thư mục: {output_path}")
            print(f"Tổng số ảnh: {summary.total_images}")
            print(f"Số ảnh tìm thấy viền và crop: {summary.cropped_images}")
            print(f"Số ảnh giữ nguyên: {summary.total_images - summary.cropped_images}")
            print(f"Số ảnh nghi bị mờ: {summary.blurry_images}")
            if summary.blurry_filenames:
                print(f"  -> {', '.join(summary.blurry_filenames)}")
            print(f"Số ảnh nghi bị nát/rách: {summary.damaged_images}")
            if summary.damaged_filenames:
                print(f"  -> {', '.join(summary.damaged_filenames)}")
            if summary.debug_paths:
                print(f"Ảnh debug: {len(summary.debug_paths)} file, cạnh mỗi ảnh gốc (hậu tố _debug)")

        # =====================================================================
        # TRƯỜNG HỢP 2: ĐẦU VÀO LÀ FILE PDF
        # =====================================================================
        elif is_pdf:
            if output_path.suffix.lower() != ".pdf":
                parser.error("Đầu ra phải có phần mở rộng .pdf khi đầu vào là PDF.")
            if not 72 <= args.dpi <= 600:
                parser.error("--dpi phải nằm trong khoảng 72 đến 600")
            if not 1 <= args.jpeg_quality <= 100:
                parser.error("--jpeg-quality phải nằm trong khoảng 1 đến 100")

            summary = scan_pdf(
                input_path=input_path,
                output_path=output_path,
                min_area_ratio=args.min_area_ratio,
                mode=args.mode,
                rotation=args.rotate,
                sharpness=args.sharpness,
                dpi=args.dpi,
                crop=not args.no_crop,
                jpeg_quality=args.jpeg_quality,
                debug=args.debug,
            )
            print(f"\nĐã lưu PDF: {output_path}")
            print(f"Tổng số trang: {summary.total_pages}")
            print(f"Số trang tìm thấy viền và crop: {summary.cropped_pages}")
            print(f"Số trang giữ nguyên toàn trang: {summary.total_pages - summary.cropped_pages}")
            print(f"Số trang nghi bị mờ: {summary.blurry_pages}")
            if summary.blurry_page_numbers:
                print(f"  -> Trang {', '.join(map(str, summary.blurry_page_numbers))}")
            print(f"Số trang nghi bị nát/rách: {summary.damaged_pages}")
            if summary.damaged_page_numbers:
                print(f"  -> Trang {', '.join(map(str, summary.damaged_page_numbers))}")
            if summary.debug_dir is not None:
                print(f"Ảnh debug từng trang: {summary.debug_dir}")

        # =====================================================================
        # TRƯỜNG HỢP 3: ĐẦU VÀO LÀ FILE ẢNH LẺ
        # =====================================================================
        elif is_image:
            if output_path.suffix.lower() == ".pdf":
                parser.error("Đầu ra không thể là file PDF khi đầu vào là ảnh lẻ.")
            if output_path.suffix.lower() not in IMAGE_EXTENSIONS:
                parser.error("Đầu ra phải có định dạng ảnh (jpg, png, ...) khi đầu vào là ảnh lẻ.")

            cropped, quality, debug_path = scan_image(
                input_path=input_path,
                output_path=output_path,
                min_area_ratio=args.min_area_ratio,
                mode=args.mode,
                rotation=args.rotate,
                sharpness=args.sharpness,
                crop=not args.no_crop,
                debug=args.debug,
            )
            print(f"\nĐã lưu ảnh: {output_path}")
            print(f"Tìm thấy viền và crop: {'Có' if cropped else 'Không (giữ nguyên ảnh gốc)'}")
            print(f"Nghi bị mờ: {'Có' if quality.is_blurry else 'Không'}")
            print(f"Nghi bị nát/rách: {'Có' if quality.is_damaged else 'Không'}")
            if debug_path is not None:
                print(f"Ảnh debug: {debug_path}")

    except ValueError as error:
        parser.exit(
            1,
            f"Lỗi: {error}\n",
        )

if __name__ == "__main__":
    main()