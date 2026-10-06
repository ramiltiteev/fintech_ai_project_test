FROM python:3.13.5-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-runtime.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements-runtime.txt
COPY src/ ./src/
RUN pip install --no-cache-dir --no-deps . \
    && useradd --create-home --uid 10001 copilot \
    && mkdir -p /app/artifacts && chown copilot:copilot /app/artifacts
USER copilot
ENTRYPOINT ["python", "-m", "fintech_copilot"]
CMD ["demo", "--company", "all", "--provider", "mock", "--seed", "42"]
