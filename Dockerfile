FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
WORKDIR /app
LABEL org.opencontainers.image.title="Signal" \
      org.opencontainers.image.description="AI news collector, signal dashboard and Telegram publisher"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONFAULTHANDLER=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 DASHBOARD_HTML=data/dashboard.html
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt \
    && groupadd --gid 10001 signal \
    && useradd --uid 10001 --gid signal --no-create-home signal \
    && mkdir -p /app/data /backups \
    && chown signal:signal /app/data /backups
COPY *.py config.json ./
COPY web ./web
USER 10001:10001
EXPOSE 8765
CMD ["gunicorn", "--config", "gunicorn.conf.py", "production:create_app()"]
