# LexTreeRAG for AWS (ECS Fargate). Claude is called through Amazon Bedrock with the task's AWS role.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt requirements-aws.txt ./
RUN pip install -r requirements-aws.txt

COPY . .
RUN useradd --uid 1000 --create-home app \
    && mkdir -p /data/cache /data/pdfs/inbox \
    && chown -R app:app /app /data
USER app

# The law cache and PDF archive live on a shared disk (EFS) mounted at /data.
ENV LLM_PROVIDER=bedrock \
    CACHE_DIR=/data/cache \
    PDF_ARCHIVE_DIR=/data/pdfs

EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health', timeout=4)"

CMD ["streamlit", "run", "app.py"]
