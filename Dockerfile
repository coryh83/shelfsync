FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY app/sync.py ./sync.py

RUN useradd -r -u 10001 app && mkdir -p /data && chown app /data
USER app

EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=5s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

ENTRYPOINT ["python", "-I", "/app/sync.py"]
