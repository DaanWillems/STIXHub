# Build from the repo root: docker build -f src/Dockerfile .
FROM python:3.13-slim

WORKDIR /app

COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir .

COPY src/ ./

ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["uvicorn", "server.__main__:app", "--host", "0.0.0.0", "--port", "8000"]
