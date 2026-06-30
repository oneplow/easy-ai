FROM python:3.12-slim

WORKDIR /app

# System deps:
#   - tor: optional, only needed if you enable PROXY_TOR for signup-IP rotation
#     inside the container. The bundled start_tor.bat is Windows-only and does
#     NOT run in Linux containers; instead run tor as a daemon (or a separate
#     tor sidecar container) and point TOR_SOCKS at it. Left commented out to
#     keep the image lean for the default headless (no-proxy) path.
# RUN apt-get update && apt-get install -y --no-install-recommends tor \
#     && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# SQLite databases live under ./bank/ at runtime; mount a volume there so
# users, KIs, context history and harvested accounts persist across restarts:
#   docker run -v easyai_bank:/app/bank -p 8000:8000 easy-ai
# All stores use WAL journal mode, so it is safe to run multiple uvicorn
# workers (e.g. --workers 4) against the same mounted volume.
EXPOSE 8000
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
