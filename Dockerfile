FROM python:3.12-slim-bookworm
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 \
    && python -m pip install --no-cache-dir --upgrade pip==26.3 \
    && pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /ms-playwright \
    && useradd --create-home webrelay && mkdir data && chown webrelay:webrelay data
COPY webrelay/ webrelay/
USER webrelay
EXPOSE 8000
HEALTHCHECK CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/api/health', timeout=3)"]
CMD ["python", "-m", "uvicorn", "webrelay.app:from_env", "--factory", "--host", "0.0.0.0"]
