# jellyfin-ondemand — stdlib-only Python: no pip install, small image, runs on CPUs without AVX.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    ONDEMAND_CONFIG=/config/config.toml ONDEMAND_SECRETS=/config/secrets.toml
WORKDIR /app
COPY ondemand /app/ondemand
COPY tools /app/tools

# 8097 = video (only Jellyfin reads it); 8098 = the search API (publish it next to Jellyfin, under /ondemand/)
EXPOSE 8097 8098
# both ports: the search API is a separate thread and could die on its own
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 CMD \
    python3 -c "import urllib.request as u; [u.urlopen(x, timeout=5) for x in ('http://127.0.0.1:8097/health', 'http://127.0.0.1:8098/api/health')]"
CMD ["python3", "-m", "ondemand.service"]
