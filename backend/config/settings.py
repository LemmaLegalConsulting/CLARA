import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured


BASE_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BASE_DIR.parent


def load_dotenv(path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).lower() in {"1", "true", "yes", "on"}


def env_list(name, default):
    value = os.environ.get(name)
    if not value:
        return default
    return [item.strip() for item in value.split(",") if item.strip()]


load_dotenv(REPO_DIR / ".env")

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
if not DEBUG and SECRET_KEY in {"", "change-me", "dev-only-change-me"}:
    raise ImproperlyConfigured("Set DJANGO_SECRET_KEY to a unique secret before running with DJANGO_DEBUG=false.")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", ["localhost", "127.0.0.1", "testserver"])
# The readiness probe reaches Django through nginx over the container's
# loopback and so presents 127.0.0.1 rather than the public hostname. Without
# this the probe is rejected as a DisallowedHost, the replica never reports
# ready, and the deployment stalls. Loopback is not a name any client outside
# the container can usefully claim, so allowing it costs nothing.
if "127.0.0.1" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append("127.0.0.1")
DEV_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5174",
]
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", DEV_ORIGINS if DEBUG else [])
CORS_ALLOWED_ORIGINS = env_list("DJANGO_CORS_ALLOWED_ORIGINS", DEV_ORIGINS if DEBUG else [])
FRONTEND_SITE_URL = os.environ.get("FRONTEND_SITE_URL", "http://localhost:5173")


def _origin(url):
    parts = urlsplit(url or "")
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


# Origins allowed to embed a document this API serves -- a decision scan, a
# library source PDF, a case document -- in a frame. The app previews those in
# an iframe, and here it is a sibling subdomain of the API rather than the same
# one, which X-Frame-Options cannot express: SAMEORIGIN means the API's own
# origin, so the browser refused to display a PDF the app had already fetched.
# apps.core.http.allow_document_framing sends these as `frame-ancestors`.
FRAME_ANCESTORS = env_list(
    "DJANGO_FRAME_ANCESTORS",
    list(dict.fromkeys(filter(None, (_origin(url) for url in [*CORS_ALLOWED_ORIGINS, FRONTEND_SITE_URL])))),
)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.core",
    "apps.matters",
    "apps.sources",
    "apps.caselaw",
    "apps.templates_app",
    "apps.drafting",
    "apps.facts",
    "apps.issues",
    "apps.rules",
    "apps.ai",
    "apps.argument_gym",
]

ENABLE_REMOTE_USER_AUTH = env_bool("ENABLE_REMOTE_USER_AUTH", False)

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # Only when some origin is actually allowed. In development that is the
    # Vite dev server; in production it is the static host the single-page app
    # is served from, and an empty list means the app is same-origin and no
    # cross-origin handling should exist at all.
    *( ["apps.core.middleware.CorsMiddleware"] if CORS_ALLOWED_ORIGINS else [] ),
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    *(
        ["django.contrib.auth.middleware.RemoteUserMiddleware"]
        if ENABLE_REMOTE_USER_AUTH
        else []
    ),
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# RemoteUserBackend trusts an upstream proxy to have authenticated the request.
# It is only sound when the matching middleware is enabled, so the two are
# configured from the same switch.
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    *(["django.contrib.auth.backends.RemoteUserBackend"] if ENABLE_REMOTE_USER_AUTH else []),
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

WSGI_APPLICATION = "config.wsgi.application"

if os.environ.get("POSTGRES_HOST"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("POSTGRES_DB", "agentic_housing"),
            "USER": os.environ.get("POSTGRES_USER", "agentic_housing"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "HOST": os.environ["POSTGRES_HOST"],
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": int(os.environ.get("POSTGRES_CONN_MAX_AGE", "60")),
            # Managed Postgres (Azure Flexible Server, RDS) requires TLS, and
            # the libpq default of "prefer" downgrades without complaint. The
            # default keeps a sidecar Postgres container working; deployments
            # against a managed server set POSTGRES_SSLMODE=require.
            "OPTIONS": {"sslmode": os.environ.get("POSTGRES_SSLMODE", "prefer")},
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            # Local development only (production is Postgres). A transaction
            # that reads, then writes -- a save that checks its revision first
            # -- fails at once on SQLite if another connection is writing when
            # it tries to upgrade its lock. IMMEDIATE takes the write lock at
            # the start and waits for it; the timeout says how long.
            "OPTIONS": {"transaction_mode": "IMMEDIATE", "timeout": 20},
        }
    }

AUTH_PASSWORD_VALIDATORS = [] if DEBUG else [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Django's default logging config sends request errors to the console only when
# DEBUG is on, and otherwise only to `mail_admins` -- which needs an ADMINS list
# and a mail backend this deployment does not have. So in production every
# unhandled exception became a bare 500 with its traceback dropped on the floor:
# thirty days of container logs held eighteen 500s and not one stack to read
# them by. Container Apps collects stdout, so that is where they go now.
#
# The root logger stays at WARNING to keep third-party chatter (urllib3 opening
# a connection, and so on) out of the log; `django.request` is named at ERROR
# rather than WARNING so that ordinary 404s do not drown the 500s.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "[{levelname}] {asctime} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "apps": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}


LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/New_York"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = REPO_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# When TLS terminates upstream (Container Apps ingress, nginx-proxy, App
# Gateway), Django only learns the request was HTTPS from X-Forwarded-Proto.
# Without this, secure cookies, CSRF referer checks, and absolute URLs all
# behave as though the site were plain HTTP. Only trust the header when the
# deployment actually puts a proxy in front, since a client can forge it.
if env_bool("DJANGO_TRUST_PROXY_SSL_HEADER", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SESSION_COOKIE_SECURE = env_bool("DJANGO_SESSION_COOKIE_SECURE", not DEBUG)
CSRF_COOKIE_SECURE = env_bool("DJANGO_CSRF_COOKIE_SECURE", not DEBUG)
SESSION_COOKIE_HTTPONLY = True

# Set to the parent domain (".example.org") when the app and the API are served
# from sibling subdomains, which is how the split deployment runs: the app comes
# from a static host that is warm while the API's container is still waking.
#
# The CSRF cookie is the one that has to be scoped. It is deliberately readable
# by JavaScript -- the frontend copies it into the X-CSRFToken header -- and a
# cookie set by the API host is invisible to script on the app host unless it
# names the parent domain. Without this every unsafe request fails 403 while
# every GET keeps working, which reads as "saving is broken" rather than as a
# cookie problem.
#
# The session cookie needs no such help: it is HttpOnly and only has to be sent
# to the API, which the browser does for a same-site request. It is settable
# anyway for deployments that want one scope for both.
CSRF_COOKIE_DOMAIN = os.environ.get("DJANGO_CSRF_COOKIE_DOMAIN") or None
SESSION_COOKIE_DOMAIN = os.environ.get("DJANGO_SESSION_COOKIE_DOMAIN") or None
CSRF_COOKIE_HTTPONLY = env_bool("DJANGO_CSRF_COOKIE_HTTPONLY", False)
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", False)
SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_SECURE_HSTS_SECONDS", "0" if DEBUG else "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", not DEBUG)
SECURE_HSTS_PRELOAD = env_bool("DJANGO_SECURE_HSTS_PRELOAD", False)
SECURE_REFERRER_POLICY = os.environ.get("DJANGO_SECURE_REFERRER_POLICY", "same-origin")
X_FRAME_OPTIONS = "DENY"

OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL") or os.environ.get("OPENAI_MISTRAL_MODEL", "gpt-5.4-mini")
AI_DRAFTING_ENABLED = env_bool("AI_DRAFTING_ENABLED", bool(OPENAI_API_KEY))
# An Argument Gym run is eight sequential model calls and several retrieval
# rounds, which is far longer than a worker may hold a request. Runs execute
# on a background thread and the client polls the run; tests turn this off to
# keep assertions on a finished run.
ARGUMENT_GYM_BACKGROUND_RUNS = env_bool("ARGUMENT_GYM_BACKGROUND_RUNS", True)
# A run whose worker died leaves a row claiming to be running forever.
ARGUMENT_GYM_RUN_TIMEOUT_SECONDS = int(os.environ.get("ARGUMENT_GYM_RUN_TIMEOUT_SECONDS", "1800"))
CASE_ACTION_MODEL = os.environ.get("CASE_ACTION_MODEL", OPENAI_MODEL)
PROMPT_CATALOG_DIR = Path(os.environ.get("PROMPT_CATALOG_DIR", REPO_DIR / "prompts"))
CONTENT_LIBRARY_DIR = Path(os.environ.get("CONTENT_LIBRARY_DIR", REPO_DIR / "content"))
if not CONTENT_LIBRARY_DIR.is_absolute():
    CONTENT_LIBRARY_DIR = REPO_DIR / CONTENT_LIBRARY_DIR
ORGANIZATION_CONTENT_LIBRARY_DIR = Path(
    os.environ.get("ORGANIZATION_CONTENT_LIBRARY_DIR", REPO_DIR / "private-content")
)
if not ORGANIZATION_CONTENT_LIBRARY_DIR.is_absolute():
    ORGANIZATION_CONTENT_LIBRARY_DIR = REPO_DIR / ORGANIZATION_CONTENT_LIBRARY_DIR
# Public content that is generated rather than authored -- the local-ordinance
# corpus -- is published to the document store and read from there, so it can be
# refreshed without rebuilding the image.
PUBLISHED_CONTENT_LIBRARY_DIR = Path(
    os.environ.get("PUBLISHED_CONTENT_LIBRARY_DIR", REPO_DIR / "private-content" / "storage" / "published" / "content")
)
if not PUBLISHED_CONTENT_LIBRARY_DIR.is_absolute():
    PUBLISHED_CONTENT_LIBRARY_DIR = REPO_DIR / PUBLISHED_CONTENT_LIBRARY_DIR
# Side-loaded document storage. See apps.core.storage: every store is split into
# a raw/ area an operator uploads into and a published/ area the application
# reads. "filesystem" points at a local directory or a mounted file share;
# "s3" targets any S3-compatible endpoint and needs only a bucket and keys.
DOCUMENT_STORAGE_BACKEND = os.environ.get("DOCUMENT_STORAGE_BACKEND", "filesystem")
DOCUMENT_STORAGE_ROOT = Path(os.environ.get("DOCUMENT_STORAGE_ROOT", REPO_DIR / "private-content" / "storage"))
if not DOCUMENT_STORAGE_ROOT.is_absolute():
    DOCUMENT_STORAGE_ROOT = REPO_DIR / DOCUMENT_STORAGE_ROOT
DOCUMENT_STORAGE_BUCKET = os.environ.get("DOCUMENT_STORAGE_BUCKET", "")
DOCUMENT_STORAGE_ENDPOINT_URL = os.environ.get("DOCUMENT_STORAGE_ENDPOINT_URL", "")
DOCUMENT_STORAGE_ACCESS_KEY_ID = os.environ.get("DOCUMENT_STORAGE_ACCESS_KEY_ID", "")
DOCUMENT_STORAGE_SECRET_ACCESS_KEY = os.environ.get("DOCUMENT_STORAGE_SECRET_ACCESS_KEY", "")
DOCUMENT_STORAGE_REGION = os.environ.get("DOCUMENT_STORAGE_REGION", "")
DOCUMENT_STORAGE_AZURE_CONTAINER = os.environ.get("DOCUMENT_STORAGE_AZURE_CONTAINER", "")
DOCUMENT_STORAGE_AZURE_CONNECTION_STRING = os.environ.get("DOCUMENT_STORAGE_AZURE_CONNECTION_STRING", "")
DOCUMENT_STORAGE_AZURE_ACCOUNT_URL = os.environ.get("DOCUMENT_STORAGE_AZURE_ACCOUNT_URL", "")
DOCUMENT_STORAGE_AZURE_CREDENTIAL = os.environ.get("DOCUMENT_STORAGE_AZURE_CREDENTIAL", "")

# Key prefixes inside the published area. Case-law artifacts keep the "caselaw"
# prefix they already carry in CaseLawArtifact.storage_key rows.
CASELAW_STORAGE_PREFIX = os.environ.get("CASELAW_STORAGE_PREFIX", "caselaw")
PRIVATE_CONTENT_STORAGE_PREFIX = os.environ.get("PRIVATE_CONTENT_STORAGE_PREFIX", "private-content")
CASELAW_IMPORT_REQUIRE_VERIFIED = env_bool("CASELAW_IMPORT_REQUIRE_VERIFIED", False)
CASELAW_IMPORT_APPROVE_VERIFIED_FOR_SEARCH = env_bool("CASELAW_IMPORT_APPROVE_VERIFIED_FOR_SEARCH", True)
CASELAW_IMPORT_APPROVE_UNVERIFIED_FOR_SEARCH = env_bool("CASELAW_IMPORT_APPROVE_UNVERIFIED_FOR_SEARCH", False)
DOCUMENT_TEXT_EXTRACTOR = os.environ.get("DOCUMENT_TEXT_EXTRACTOR", "stdlib")
# Fallback only. Administrators can override it in Organization settings, and
# users can choose a personal default in their profile.
DEFAULT_JURISDICTION = os.environ.get("DEFAULT_JURISDICTION", "Ohio")

LEGALSERVER_BASE_URL = os.environ.get("LEGALSERVER_BASE_URL", "")
LEGALSERVER_API_TOKEN = os.environ.get("LEGALSERVER_API_TOKEN", "")
LEGALSERVER_API_USERNAME = os.environ.get("LEGALSERVER_API_USERNAME", "")
LEGALSERVER_API_PASSWORD = os.environ.get("LEGALSERVER_API_PASSWORD", "")
LEGALSERVER_MATTERS_PATH = os.environ.get("LEGALSERVER_MATTERS_PATH", "/api/v2/matters")
LEGALSERVER_MATTERS_RESULTS = os.environ.get("LEGALSERVER_MATTERS_RESULTS", "full")
LEGALSERVER_MATTER_DOCUMENTS_PATH = os.environ.get(
    "LEGALSERVER_MATTER_DOCUMENTS_PATH", "/api/v1/matters/{matter_id}/documents"
)
LEGALSERVER_MATTER_PROFILE_PATH = os.environ.get(
    "LEGALSERVER_MATTER_PROFILE_PATH", "/matter/dynamic-profile/view/{matter_id}"
)
# Write-side endpoints, following the published v2 contracts. All three address
# a matter by its UUID, which is how v2 identifies one. Each needs its own role
# permission on the site: API Create Note, API Create Document, and the Premium
# API Matter: Update.
LEGALSERVER_NOTES_PATH = os.environ.get("LEGALSERVER_NOTES_PATH", "/api/v2/notes")
LEGALSERVER_DOCUMENTS_PATH = os.environ.get("LEGALSERVER_DOCUMENTS_PATH", "/api/v2/documents")
LEGALSERVER_MATTER_UPDATE_PATH = os.environ.get("LEGALSERVER_MATTER_UPDATE_PATH", "/api/v2/matters/{case_uuid}")
LEGALSERVER_MATTER_UPDATE_METHOD = os.environ.get("LEGALSERVER_MATTER_UPDATE_METHOD", "PATCH")
# Optional document-type lookup value applied to uploads, e.g. "Brief". Blank
# leaves the site's own default in place.
LEGALSERVER_DOCUMENT_TYPE = os.environ.get("LEGALSERVER_DOCUMENT_TYPE", "")
# Note-type lookup value. The notes endpoint requires one, so this is not
# optional; "Case Notes" is a system lookup present on a stock site.
LEGALSERVER_CASE_NOTE_TYPE = os.environ.get("LEGALSERVER_CASE_NOTE_TYPE", "Case Notes")
# A generated document that is never uploaded is lost when the browser tab
# closes, so document delivery is opt-out. A research answer or triage
# assessment is a working note the advocate may not want on the file, so those
# are opt-in.
LEGALSERVER_SAVE_DOCUMENTS_DEFAULT = env_bool("LEGALSERVER_SAVE_DOCUMENTS_DEFAULT", True)
LEGALSERVER_SAVE_RESEARCH_DEFAULT = env_bool("LEGALSERVER_SAVE_RESEARCH_DEFAULT", False)
LEGALSERVER_SAVE_TRIAGE_DEFAULT = env_bool("LEGALSERVER_SAVE_TRIAGE_DEFAULT", False)
# Which field map under content/legalserver-field-maps/ translates a triage
# outcome into case properties. Blank disables the mapping entirely.
LEGALSERVER_TRIAGE_FIELD_MAP = os.environ.get("LEGALSERVER_TRIAGE_FIELD_MAP", "triage-outcome")
# A developer's .env points at a real LegalServer site, so a test run must never
# be able to write to one: a document uploaded to a client's case file cannot be
# taken back. Tests that exercise the write path turn this back on with
# override_settings and a fake session. Reads are unaffected.
TESTING = "test" in sys.argv
LEGALSERVER_ALLOW_WRITES = env_bool("LEGALSERVER_ALLOW_WRITES", True) and not TESTING
# A case's document list is fetched from LegalServer on every materials load,
# preview and fact recommendation; the demo site rate-limits after about ten in
# a row. Cached per process for this long. Off under test, where one case's
# list must never answer for another's.
# Draft generation runs on a background thread and the client polls it (see
# apps.drafting.generation_jobs). Tests run it inline to assert on the result.
TEMPLATE_FILL_BACKGROUND = env_bool("TEMPLATE_FILL_BACKGROUND", True) and not TESTING
DRAFT_GENERATION_BACKGROUND = env_bool("DRAFT_GENERATION_BACKGROUND", True) and not TESTING
DRAFT_GENERATION_TIMEOUT_SECONDS = int(os.environ.get("DRAFT_GENERATION_TIMEOUT_SECONDS", "900"))
LEGALSERVER_DOCUMENT_CACHE_SECONDS = 0 if TESTING else int(os.environ.get("LEGALSERVER_DOCUMENT_CACHE_SECONDS", "120"))
LEGALSERVER_USERS_PATH = os.environ.get("LEGALSERVER_USERS_PATH", "/api/v1/users")
LEGALSERVER_USER_FILTER_PARAM = os.environ.get("LEGALSERVER_USER_FILTER_PARAM", "")
LEGALSERVER_SUPERUSER_GROUPS = env_list("LEGALSERVER_SUPERUSER_GROUPS", ["LegalServer Superusers"])
LEGALSERVER_SUPERUSER_ROLES = env_list(
    "LEGALSERVER_SUPERUSER_ROLES",
    ["admin", "administrator", "superuser", "super user", "site administrator"],
)
ENABLE_DEMO_MATTERS = env_bool("ENABLE_DEMO_MATTERS", False)

SHAREPOINT_SITE_ID = os.environ.get("SHAREPOINT_SITE_ID", "")
SHAREPOINT_DRIVE_ID = os.environ.get("SHAREPOINT_DRIVE_ID", "")
SHAREPOINT_ACCESS_TOKEN = os.environ.get("SHAREPOINT_ACCESS_TOKEN", "")
SHAREPOINT_CASE_FOLDER_TEMPLATE = os.environ.get("SHAREPOINT_CASE_FOLDER_TEMPLATE", "Cases/{matter_id}")

OFFICE365_TENANT_ID = os.environ.get("OFFICE365_TENANT_ID", "")
OFFICE365_CLIENT_ID = os.environ.get("OFFICE365_CLIENT_ID", "")
OFFICE365_CLIENT_SECRET = os.environ.get("OFFICE365_CLIENT_SECRET", "")
OFFICE365_REDIRECT_URI = os.environ.get("OFFICE365_REDIRECT_URI", "http://localhost:5173/api/auth/office365/callback/")
OFFICE365_SCOPES = os.environ.get(
    "OFFICE365_SCOPES",
    "openid profile email offline_access User.Read Sites.Read.All Files.Read.All",
)
