FROM python:3.13.15-slim

# psycopg is included so DATABASE_URL can point at Postgres later without
# rebuilding; SQLite needs nothing extra.
RUN pip install --no-cache-dir "SQLAlchemy>=2.0" "psycopg[binary]>=3.1" "discord.py>=2.3"

WORKDIR /app
# CHANGELOG.md is read by the website's what's-new page, from the WORKDIR.
COPY pyproject.toml README.md CHANGELOG.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir --no-deps . && rm -rf /root/.cache

# State lives here so it survives a restart; mount a volume.
RUN install -d -o nobody -g nogroup /data
VOLUME /data
ENV DATABASE_URL=sqlite:////data/idlerpg.db \
    PYTHONUNBUFFERED=1

USER nobody
ENTRYPOINT ["python", "-m", "idlerpg"]
