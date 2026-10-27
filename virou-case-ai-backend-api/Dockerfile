FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
COPY src ./src
RUN pip install --no-cache-dir --no-deps . && useradd --uid 10001 --create-home app && mkdir -p /app/var/sources && chown -R app:app /app/var
COPY content ./content
COPY config ./config
COPY migrations ./migrations
COPY script ./script
COPY alembic.ini ./
RUN chmod -R u=rwX,go=rX /app/src /app/migrations /app/content /app/config /app/script /app/alembic.ini
USER 10001:10001
RUN python script/check-migrations.py
EXPOSE 8000 8001
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=2)"
CMD ["uvicorn", "viracase.interfaces.public:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
