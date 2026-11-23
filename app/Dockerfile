# Book Recommender — web app image (FastAPI + uvicorn; also serves the React SPA).
#
# This image is the deployable artifact (e.g. on its own EC2). The repo-root
# docker-compose.yml is for LOCAL DEV only (app + Postgres together).
#
#   Build:  docker build -t bookrec-app ./app
#   Run:    docker run -p 8000:8000 --env-file app/.env \
#             -e DATABASE_URL=postgresql://user:pass@db-host:5432/bookrec bookrec-app
#
# See app/README.md "Deploy (standalone / EC2)" for the required env + IAM notes.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App source.
COPY . .

# Run as a non-root user.
RUN useradd --create-home appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

# Container health via the API's own probe (also verifies DB connectivity).
# slim has no curl/wget, so use Python's urllib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health').read()" || exit 1

# ASGI entrypoint — `app = FastAPI()` lives in api.py.
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
