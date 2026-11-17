FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --create-home app

ENV TALENT_MCP_DATA_DIR=/data
RUN mkdir -p /data && chown app /data
VOLUME ["/data"]
USER app

EXPOSE 8000
CMD ["talent-sourcing-mcp", "--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
