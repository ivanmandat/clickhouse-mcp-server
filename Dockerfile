FROM python:3.12-slim

# Не root: сервер ходит в базы, но сам ничего не должен уметь на хосте
RUN useradd --create-home --uid 10001 mcp

WORKDIR /app

# Зависимости отдельным слоем: пересобираются только при правке pyproject
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .

# sql нужен для scripts/ и справки, reports — каталог вывода отчётов
COPY sql ./sql
RUN mkdir -p /app/reports && chown -R mcp:mcp /app/reports

USER mcp

ENV MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8765 \
    PYTHONUNBUFFERED=1

EXPOSE 8765

# /health не требует токена и не трогает базы: контейнер считается живым,
# даже когда ClickHouse временно недоступен
HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=3).status==200 else 1)"

CMD ["mcp-dwh"]
