FROM python:3.11-slim
WORKDIR /app
COPY app ./app
COPY cli.py ./
COPY pyproject.toml ./
RUN pip install --no-cache-dir .
EXPOSE 8000
