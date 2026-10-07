FROM python:3.13.5-slim

ARG INSTALL_LIVE=false
ARG INSTALL_API=false
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-runtime.txt requirements-api.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements-runtime.txt
RUN if [ "$INSTALL_API" = "true" ]; then \
        pip install --no-cache-dir -r requirements-api.txt; \
    fi
COPY src/ ./src/
RUN if [ "$INSTALL_LIVE" = "true" ]; then \
        pip install --no-cache-dir '.[live]'; \
    else \
        pip install --no-cache-dir --no-deps .; \
    fi \
    && useradd --create-home --uid 10001 copilot \
    && mkdir -p /app/artifacts && chown copilot:copilot /app/artifacts
USER copilot
ENTRYPOINT ["python", "-m", "fintech_copilot"]
CMD ["demo", "--company", "all", "--provider", "mock", "--seed", "42"]
