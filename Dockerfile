# Stage 1: Build Frontend
FROM node:26.12.1-slim AS frontend

# eMudhra certificate incident: refresh each stage's system trust store.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && update-ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2: Python dependencies, built and slimmed in a stage that is thrown away
FROM python:3.12-slim AS pydeps

# eMudhra certificate incident: refresh each stage's system trust store.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && update-ca-certificates && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir --upgrade "pip @ https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70" \
    && python -m pip install --no-cache-dir --upgrade "certifi>=2026.10.19"

# Dependencies are installed into a virtualenv in a throwaway stage, slimmed
# there, and only the result is copied into the runtime image. Doing the removal
# in a later layer of a single-stage build saves nothing: layers are additive, a
# delete writes a whiteout, and the bytes stay in the image. That mistake cost
# this file a round trip -- the slimming script reported 24MB removed and the
# image did not change size at all.
#
# What is NOT removed, deliberately: babel's locale-data (docxcompose formats
# document properties through it), nltk (textstat imports it) and setuptools
# (textstat declares it at runtime). They are the largest things left and all
# three are load-bearing. Alpine was measured too and is a wash -- its smaller
# base is cancelled out by larger musl wheels.
COPY requirements.txt .
COPY docker/slim_site_packages.py /tmp/
RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir --upgrade "pip @ https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70" \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt gunicorn \
    && /opt/venv/bin/python /tmp/slim_site_packages.py

# Stage 3: the image that actually runs
FROM python:3.12-slim

# eMudhra certificate incident: refresh each stage's system trust store.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && update-ca-certificates && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir --upgrade "pip @ https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70" \
    && python -m pip install --no-cache-dir --upgrade "certifi>=2026.10.19"
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    nginx \
    && rm -rf /var/lib/apt/lists/*

COPY --from=pydeps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy all project files
COPY . .

# Copy compiled frontend from Stage 1
COPY --from=frontend /app/frontend/dist /app/frontend/dist

# Configure Nginx
COPY nginx.conf /etc/nginx/sites-available/default

# Set permissions for entrypoint scripts
RUN chmod +x /app/docker/bootstrap.sh /app/docker/web.sh

# Collect Django's static files at build time. They are the same for every
# replica and depend only on the code, so baking them in keeps startup fast and
# keeps replicas from racing to write the same directory. Settings are only
# imported here, and no database is configured during the build.
RUN cd /app/backend \
    && DJANGO_DEBUG=true POSTGRES_HOST= python manage.py collectstatic --noinput

# Bake the bytecode in too. __pycache__ is excluded from the build context, so
# without this every replica compiles the application's own modules on its first
# import — work that is identical on every start and that a cold start pays for
# while someone is waiting on the request that woke the container up.
RUN python -m compileall -q /app/backend /app/scripts || true

# Expose HTTP port
EXPOSE 80

# Serving is the default. Container Apps names the command explicitly for both
# the app and the bootstrap job, so this only decides what a bare `docker run`
# of the image does -- and serving without migrating is the safe answer, since
# bootstrap.sh writes to a database and must run exactly once per deployment.
CMD ["/app/docker/web.sh"]
