FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

COPY alembic.ini ./
COPY migrations ./migrations

USER app

EXPOSE 8000

CMD ["uvicorn", "triagem.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS test

USER root
RUN pip install ".[dev]"
COPY tests ./tests
USER app

CMD ["python", "-m", "pytest", "-p", "no:cacheprovider"]
