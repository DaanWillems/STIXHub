FROM python:3.13-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY config.py .
COPY log.py .
COPY dependencies.py .
COPY database/ ./database/
COPY server/ ./server/
COPY worker/ ./worker/
COPY models/ ./models/
COPY routes/ ./routes/
COPY processors/ ./processors/

ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["uvicorn", "server.__main__:app", "--host", "0.0.0.0", "--port", "8000"]
