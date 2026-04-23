FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bybit_autotrading.py .

VOLUME ["/app/logs"]

CMD ["python", "bybit_autotrading.py"]
