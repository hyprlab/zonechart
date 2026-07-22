FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
# --no-shell: only the full Chromium build — the headless-shell variant is
# unused (Akamai blocks it; refresher.py launches channel="chromium").
# ffmpeg is only for video capture, also unused.
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps --no-shell chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache/ms-playwright/ffmpeg-*

COPY app/ ./app/

WORKDIR /srv/app

EXPOSE 8000

# single worker + threads: the in-memory chart registry stays consistent
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--access-logfile", "-", "app:app"]
