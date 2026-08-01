FROM python:3.11-slim

WORKDIR /app

# opencv-python-headless vẫn cần vài shared lib hệ thống này để import được.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)" || exit 1

# WEB_CONCURRENCY chọn số uvicorn worker process (multi-process, tận dụng
# nhiều CPU core cho workload OpenCV/PyMuPDF vốn CPU-bound). Mặc định 2 nếu
# không đặt biến môi trường; nên đặt bằng số CPU core khả dụng cho container.
CMD ["sh", "-c", "uvicorn py_project.api:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY:-2}"]
