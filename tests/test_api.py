from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from py_project import api as api_module
from py_project.api import app

FIXTURES_DIR = Path(__file__).parent.parent / "input_pdf"
client = TestClient(app)


@pytest.fixture(autouse=True)
def _redirect_output_dir(tmp_path, monkeypatch) -> None:
    """Đừng để test ghi file thật vào output_scans/ của repo."""
    monkeypatch.setattr(api_module, "OUTPUT_DIR", tmp_path)


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_upload_page_served_at_root() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "file-input" in response.text
    assert "multiple" in response.text


def test_scan_single_image_success() -> None:
    with (FIXTURES_DIR / "1.jpeg").open("rb") as file_obj:
        response = client.post(
            "/api/scan",
            files={"file": ("1.jpeg", file_obj, "image/jpeg")},
        )

    assert response.status_code == 200
    body = response.json()
    # "warning" is a successful scan that also flagged a quality issue
    # (blurry / possibly torn) - both are non-failing outcomes here.
    assert body["status"] in ("success", "warning")
    assert body["download_url"] is not None

    download = client.get(body["download_url"])
    assert download.status_code == 200
    assert len(download.content) > 0


def test_scan_single_pdf_success() -> None:
    with (FIXTURES_DIR / "1.pdf").open("rb") as file_obj:
        response = client.post(
            "/api/scan",
            files={"file": ("1.pdf", file_obj, "application/pdf")},
            data={"dpi": "150"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] in ("success", "warning")
    assert body["total_pages"] is not None
    assert body["download_url"] is not None


def test_scan_unsupported_extension_reports_error_without_failing_request() -> None:
    response = client.post(
        "/api/scan",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert body["download_url"] is None


def test_scan_invalid_rotate_returns_422() -> None:
    with (FIXTURES_DIR / "1.jpeg").open("rb") as file_obj:
        response = client.post(
            "/api/scan",
            files={"file": ("1.jpeg", file_obj, "image/jpeg")},
            data={"rotate": "45"},
        )

    assert response.status_code == 422


def test_scan_batch_reports_per_file_status() -> None:
    with (
        (FIXTURES_DIR / "1.jpeg").open("rb") as image_file,
        (FIXTURES_DIR / "2.pdf").open("rb") as pdf_file,
    ):
        response = client.post(
            "/api/scan/batch",
            files=[
                ("files", ("1.jpeg", image_file, "image/jpeg")),
                ("files", ("2.pdf", pdf_file, "application/pdf")),
                ("files", ("bad.txt", b"hello", "text/plain")),
            ],
        )

    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) == 3
    assert results[0]["status"] in ("success", "warning")
    assert results[1]["status"] in ("success", "warning")
    assert results[2]["status"] == "error"


def test_scan_pdf_flags_low_quality_pages_as_warning() -> None:
    # 1.pdf at 150 DPI has one page with low sharpness and one page with a
    # jagged (non-rectangular) paper contour - both should be surfaced.
    with (FIXTURES_DIR / "1.pdf").open("rb") as file_obj:
        response = client.post(
            "/api/scan",
            files={"file": ("1.pdf", file_obj, "application/pdf")},
            data={"dpi": "150"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "warning"
    assert body["blurry_pages"] >= 1
    assert body["damaged_pages"] >= 1
    # page numbers must identify exactly which pages triggered each flag
    assert body["blurry_page_numbers"] == [1]
    assert body["damaged_page_numbers"] == [2]
    assert any("mờ" in w for w in body["warnings"])
    assert any("nát" in w or "rách" in w for w in body["warnings"])


def test_scan_image_quality_thresholds_are_tunable() -> None:
    # An unreasonably high blur threshold must flag even a sharp image as blurry.
    with (FIXTURES_DIR / "1.jpeg").open("rb") as file_obj:
        response = client.post(
            "/api/scan",
            files={"file": ("1.jpeg", file_obj, "image/jpeg")},
            data={"blur_threshold": "999999"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "warning"
    assert body["is_blurry"] is True


def test_download_missing_job_returns_404() -> None:
    response = client.get("/api/download/does-not-exist")
    assert response.status_code == 404


def test_scan_image_without_debug_has_no_debug_url() -> None:
    with (FIXTURES_DIR / "1.jpeg").open("rb") as file_obj:
        response = client.post(
            "/api/scan",
            files={"file": ("1.jpeg", file_obj, "image/jpeg")},
        )

    body = response.json()
    assert body["debug_url"] is None

    job_id = body["download_url"].rsplit("/", 1)[-1]
    missing_debug = client.get(f"/api/debug/{job_id}")
    assert missing_debug.status_code == 404


def test_scan_image_debug_returns_overlay() -> None:
    with (FIXTURES_DIR / "1.jpeg").open("rb") as file_obj:
        response = client.post(
            "/api/scan",
            files={"file": ("1.jpeg", file_obj, "image/jpeg")},
            data={"debug": "true"},
        )

    body = response.json()
    assert body["debug_url"] is not None

    debug_response = client.get(body["debug_url"])
    assert debug_response.status_code == 200
    assert debug_response.headers["content-type"] == "image/jpeg"
    assert len(debug_response.content) > 0


def test_scan_pdf_debug_returns_per_page_overlay() -> None:
    with (FIXTURES_DIR / "1.pdf").open("rb") as file_obj:
        response = client.post(
            "/api/scan",
            files={"file": ("1.pdf", file_obj, "application/pdf")},
            data={"debug": "true", "dpi": "150"},
        )

    body = response.json()
    assert body["debug_url"] is not None
    assert body["debug_page_count"] == 2

    page_1 = client.get(body["debug_url"], params={"page": 1})
    page_2 = client.get(body["debug_url"], params={"page": 2})
    page_3 = client.get(body["debug_url"], params={"page": 3})

    assert page_1.status_code == 200
    assert page_2.status_code == 200
    assert page_3.status_code == 404
