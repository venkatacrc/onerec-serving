FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY config.example.yaml ./

EXPOSE 9000
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:9000/healthz', timeout=2)" || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9000"]
