import re
from urllib.parse import quote, urljoin, urlparse

import requests
from django.conf import settings

from apps.sources.connectors.base import SourceConnector, SourceResult
from apps.sources.models import SourceConfiguration, UserSourceIdentity


class LegalServerError(RuntimeError):
    def __init__(self, message, *, status_code=None, detail=""):
        self.status_code = status_code
        self.detail = detail
        super().__init__(message)


def _transport_error(error):
    """Turn a `requests` transport failure into our own error type.

    `requests` raises its own exceptions for a DNS failure, a refused
    connection, or a read timeout, and none of them are `LegalServerError`. So
    every caller that carefully handles an unreachable LegalServer -- the case
    list, the account status, the document fetch -- was handling only the
    HTTP-status half of "unreachable", and a site that simply did not answer
    escaped as an unhandled exception and a bare 500 instead of the sync error
    the UI already knows how to show.

    No status code: nothing was answered, so there is no status to report.
    """
    return LegalServerError(f"LegalServer could not be reached: {error}")


def _clean_base_url(base_url):
    return base_url.rstrip("/") + "/" if base_url else ""


def _first_value(payload, *keys, default=""):
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return default


LEGALSERVER_PRIMARY_MATTER_ID_FIELDS = (
    "matter_identification_number",
    "case_number",
    "matter_id",
    "case_id",
    "external_id",
)

LEGALSERVER_FALLBACK_MATTER_ID_FIELDS = (
    "id",
    "matter_uuid",
    "uuid",
)


def legalserver_matter_identifier(payload):
    value = _first_value(payload, *LEGALSERVER_PRIMARY_MATTER_ID_FIELDS, default="")
    if value not in (None, ""):
        return str(value)
    value = _first_value(payload, *LEGALSERVER_FALLBACK_MATTER_ID_FIELDS, default="")
    return str(value) if value not in (None, "") else ""


UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def legalserver_matter_uuid(payload):
    """Return the matter UUID that v2 write endpoints address a matter by.

    The v2 API identifies a matter by UUID, not by the case number this app
    stores as a matter's external id. Anything that is not a UUID is rejected
    rather than sent: posting a case number into a `module_id` field would
    either fail confusingly or, worse, match a different record.
    """
    if not isinstance(payload, dict):
        return ""
    for key in ("matter_uuid", "uuid"):
        value = payload.get(key)
        if isinstance(value, str) and UUID_RE.match(value.strip()):
            return value.strip()
    return ""


def legalserver_matter_identifier_candidates(payload):
    seen = set()
    for key in (*LEGALSERVER_PRIMARY_MATTER_ID_FIELDS, *LEGALSERVER_FALLBACK_MATTER_ID_FIELDS):
        value = payload.get(key)
        if value in (None, ""):
            continue
        value = str(value)
        if value in seen:
            continue
        seen.add(value)
        yield value


def _display_value(value):
    if value in (None, ""):
        return ""
    if isinstance(value, dict):
        for key in (
            "lookup_value_name",
            "name",
            "label",
            "case_title",
            "user_name",
            "organization_name",
            "value",
        ):
            if value.get(key) not in (None, ""):
                return str(value[key])
        return ""
    if isinstance(value, list):
        return ""
    return str(value)


def _legalserver_response_detail(response):
    try:
        payload = response.json()
    except ValueError:
        payload = getattr(response, "text", "")
    if isinstance(payload, dict):
        for key in ("detail", "error", "message", "non_field_errors"):
            value = payload.get(key)
            if value not in (None, ""):
                payload = value
                break
    if isinstance(payload, list):
        payload = "; ".join(str(item) for item in payload)
    elif isinstance(payload, dict):
        payload = "; ".join(f"{key}: {value}" for key, value in payload.items() if value not in (None, ""))
    detail = str(payload or "").strip()
    return detail[:500]


MATTER_DATABASE_ID_FIELDS = ("database_id", "matter_database_id", "case_id", "id")


def legalserver_matter_write_id(payload):
    """Return the matter's numeric id for writes, or "" if it is not stated.

    Deliberately stricter than `legalserver_matter_database_id`, which falls
    back to the digits on the end of a case number. That guess is harmless in a
    profile link -- a wrong link is visibly wrong -- but a write addressed by a
    guessed id attaches a note to someone else's case. Case numbers restart each
    year, so "27-0000009" and "26-0000009" trail the same digits and are
    different matters.
    """
    if not isinstance(payload, dict):
        return ""
    for key in MATTER_DATABASE_ID_FIELDS:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
            return str(int(value))
    return ""


def legalserver_matter_database_id(payload):
    """Return the sequential database ID used by LegalServer profile URLs."""
    if not isinstance(payload, dict):
        return ""
    for key in MATTER_DATABASE_ID_FIELDS:
        value = payload.get(key)
        if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
            return str(int(value))
    case_number = _display_value(
        _first_value(payload, "case_number", "matter_identification_number", "case_id", default="")
    )
    match = re.search(r"(\d+)$", case_number)
    return str(int(match.group(1))) if match else ""


class LegalServerClient:
    search_fields = ("case_number", "case_title", "external_id", "first", "last")
    user_search_fields = ("email", "user_email", "user_name", "username", "login")

    def __init__(self, *, base_url=None, api_token=None, username=None, password=None, session=None):
        config = SourceConfiguration.effective_settings(
            "legalserver",
            {
                "base_url": settings.LEGALSERVER_BASE_URL,
                "api_token": settings.LEGALSERVER_API_TOKEN,
                "api_username": settings.LEGALSERVER_API_USERNAME,
                "api_password": settings.LEGALSERVER_API_PASSWORD,
                "matters_path": settings.LEGALSERVER_MATTERS_PATH,
                "matters_results": settings.LEGALSERVER_MATTERS_RESULTS,
                "matter_documents_path": settings.LEGALSERVER_MATTER_DOCUMENTS_PATH,
                "notes_path": settings.LEGALSERVER_NOTES_PATH,
                "documents_path": settings.LEGALSERVER_DOCUMENTS_PATH,
                "matter_update_path": settings.LEGALSERVER_MATTER_UPDATE_PATH,
                "users_path": settings.LEGALSERVER_USERS_PATH,
                "user_filter_param": settings.LEGALSERVER_USER_FILTER_PARAM,
            },
        )
        self.base_url = _clean_base_url(base_url or config["base_url"])
        self.api_token = api_token or config.get("api_token", "")
        self.api_username = username or config.get("api_username", "")
        self.api_password = password or config.get("api_password", "")
        self.matters_path = config["matters_path"]
        self.matters_results = config.get("matters_results", "")
        self.matter_documents_path = config["matter_documents_path"]
        self.notes_path = config.get("notes_path") or settings.LEGALSERVER_NOTES_PATH
        self.documents_path = config.get("documents_path") or settings.LEGALSERVER_DOCUMENTS_PATH
        self.matter_update_path = config.get("matter_update_path") or settings.LEGALSERVER_MATTER_UPDATE_PATH
        self.matter_update_method = (settings.LEGALSERVER_MATTER_UPDATE_METHOD or "PATCH").upper()
        self.document_type = settings.LEGALSERVER_DOCUMENT_TYPE
        self.case_note_type = settings.LEGALSERVER_CASE_NOTE_TYPE
        self.users_path = config.get("users_path") or settings.LEGALSERVER_USERS_PATH
        self.user_filter_param = config["user_filter_param"]
        self.matter_profile_path = settings.LEGALSERVER_MATTER_PROFILE_PATH
        self.session = session or requests.Session()

    @property
    def configured(self):
        return bool(self.base_url and (self.api_token or (self.api_username and self.api_password)))

    def _url(self, path):
        return urljoin(self.base_url, path.lstrip("/"))

    def matter_profile_url(self, payload):
        """Return a browser URL for the official LegalServer matter profile."""
        if not self.base_url or not isinstance(payload, dict):
            return ""
        base = urlparse(self.base_url)
        for key in ("profile_url", "matter_url", "web_url"):
            value = _display_value(payload.get(key))
            if not value:
                continue
            parsed = urlparse(value)
            if parsed.scheme == base.scheme and parsed.netloc == base.netloc:
                return value
            if not parsed.scheme and not parsed.netloc:
                return self._url(value)
        matter_id = legalserver_matter_database_id(payload)
        if not matter_id:
            return ""
        try:
            path = self.matter_profile_path.format(matter_id=quote(str(matter_id), safe=""))
        except (KeyError, ValueError):
            return ""
        return self._url(path)

    def _headers(self):
        headers = {"Accept": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def _request_kwargs(self):
        if self.api_token:
            return {}
        return {"auth": (self.api_username, self.api_password)}

    def _get(self, path, *, params=None):
        if not self.configured:
            raise LegalServerError("LegalServer is not configured")
        try:
            response = self.session.get(
                self._url(path),
                headers=self._headers(),
                params=params or {},
                timeout=20,
                **self._request_kwargs(),
            )
        except requests.RequestException as error:
            raise _transport_error(error) from error
        if response.status_code >= 400:
            detail = _legalserver_response_detail(response)
            message = f"LegalServer request failed with status {response.status_code}"
            if detail:
                message = f"{message}: {detail}"
            raise LegalServerError(message, status_code=response.status_code, detail=detail)
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            raise LegalServerError("LegalServer returned a non-JSON response")
        return response.json()

    def _write(self, method, path, *, json_body=None, files=None, data=None):
        """Send a write request and return the parsed body, if there is one.

        Unlike a read, a write may legitimately answer with 201 and an empty
        body or a bare id, so a non-JSON response is reported as an empty
        payload rather than treated as a failure.
        """
        if not self.configured:
            raise LegalServerError("LegalServer is not configured")
        try:
            response = self.session.request(
                method,
                self._url(path),
                headers=self._headers(),
                json=json_body,
                files=files,
                data=data,
                timeout=60 if files else 30,
                **self._request_kwargs(),
            )
        except requests.RequestException as error:
            raise _transport_error(error) from error
        if response.status_code >= 400:
            detail = _legalserver_response_detail(response)
            message = f"LegalServer {method} failed with status {response.status_code}"
            if detail:
                message = f"{message}: {detail}"
            raise LegalServerError(message, status_code=response.status_code, detail=detail)
        try:
            payload = response.json()
        except ValueError:
            return {}
        if not isinstance(payload, dict):
            return {"data": payload}
        # v2 wraps a created or updated record in `data`; hand back the record
        # itself so callers read `id` rather than digging through the envelope.
        record = payload.get("data")
        return record if isinstance(record, dict) else payload

    def create_note(
        self,
        matter_database_id,
        *,
        subject,
        body,
        is_html=False,
        note_type="",
        external_id="",
        upsert=False,
        extra_fields=None,
    ):
        """Create a note against a matter.

        Uses the generic v2 `/api/v2/notes` endpoint, which attaches a note to
        any module. Two details differ from the published request schema, and
        both were confirmed against a live site:

        - `module_id` must be the matter's numeric database id. The schema
          documents it as a UUID, but a UUID is rejected with `invalid_values`.
          Note that the document endpoint is the other way round: it takes the
          UUID, in `module_uuid`.
        - `note_type` is required. The schema marks it required only on the
          response, but omitting it fails with `missing_arguments`.

        The site must grant the API user the "API Create Note" role permission,
        or it answers 403.
        """
        payload = {
            "module": "matter",
            "module_id": matter_database_id,
            "subject": subject,
            "body": body,
            "note_type": note_type or self.case_note_type,
            "is_html": bool(is_html),
        }
        if external_id:
            payload["external_id"] = external_id
            if upsert:
                # external_id is ours to choose and unique to one artifact, so
                # it identifies the note to replace exactly.
                payload["update"] = {"external_id": external_id}
        payload.update(extra_fields or {})
        return self._write("POST", self.notes_path, json_body=payload)

    def upload_matter_document(
        self,
        matter_uuid,
        *,
        filename,
        content,
        content_type="",
        title="",
        replace_name="",
        extra_fields=None,
    ):
        """Upload a document and attach it to a matter.

        Follows the documented v2 Upload Document contract: one endpoint that
        attaches a document to any module, sent as multipart/form-data with the
        file alongside its metadata. The module is named explicitly and
        addressed by UUID. Answers 201 for a new document, or 200 when an
        `update` object matched an existing one. Requires the API Create
        Document permission.
        """
        fields = {
            "module": "matter",
            "module_uuid": str(matter_uuid),
            "name": title or filename,
            "title": title or filename,
        }
        if self.document_type:
            fields["type"] = self.document_type
        fields.update({key: str(value) for key, value in (extra_fields or {}).items() if value not in (None, "")})
        if replace_name:
            # Upsert. The match MUST be scoped to this matter: posting an
            # unscoped update[name] against a different matter was observed to
            # match a document on another case and reattach it to the matter in
            # the request, moving one client's document onto another's file.
            # Multipart cannot nest an object, so the API takes bracket keys.
            fields["update[module]"] = "matter"
            fields["update[module_uuid]"] = str(matter_uuid)
            fields["update[name]"] = replace_name
        files = {"file": (filename, content, content_type or "application/octet-stream")}
        return self._write("POST", self.documents_path, files=files, data=fields)

    def update_matter(self, matter_uuid, fields):
        """Set case properties on a matter.

        Update A Matter is a Premium API, addressed by the case UUID. Custom
        fields go in a `custom_fields` object keyed by database name, which is
        what the triage field map produces. Requires the API Matter: Update
        permission.
        """
        if not fields:
            return {}
        path = self.matter_update_path.format(case_uuid=quote(str(matter_uuid), safe=""))
        return self._write(self.matter_update_method, path, json_body=dict(fields))

    def download_document(self, url):
        if not self.configured:
            raise LegalServerError("LegalServer is not configured")
        resolved_url = self._url(url)
        configured_origin = urlparse(self.base_url)
        download_origin = urlparse(resolved_url)
        if (download_origin.scheme, download_origin.netloc) != (configured_origin.scheme, configured_origin.netloc):
            raise LegalServerError("LegalServer returned a document URL on an unexpected host")
        try:
            response = self.session.get(
                resolved_url,
                headers=self._headers(),
                timeout=30,
                **self._request_kwargs(),
            )
        except requests.RequestException as error:
            raise _transport_error(error) from error
        if response.status_code >= 400:
            raise LegalServerError(f"LegalServer document download failed with status {response.status_code}")
        return {
            "content": response.content,
            "content_type": response.headers.get("content-type", ""),
            "filename": resolved_url.rsplit("/", 1)[-1].split("?", 1)[0],
        }

    def _search_params(self, *, user_email="", limit=25):
        params = {"page_size": limit}
        if self.matters_results:
            params["results"] = self.matters_results
        return params

    def search_matters(self, *, query="", user_email="", limit=25):
        params = self._search_params(user_email=user_email, limit=limit)
        if query:
            matters_by_id = {}
            # A case number is already a stable, exact identifier. Trying it
            # against party-name and title fields adds four slow API calls and
            # can exhaust a site's rate limit during ordinary case switching.
            search_fields = ("case_number",) if re.fullmatch(r"\d{2,4}-\d+", query.strip()) else self.search_fields
            for field in search_fields:
                payload = self._get(self.matters_path, params={**params, field: query})
                for matter in self._matter_list_from_payload(payload):
                    matter_key = _first_value(
                        matter,
                        "matter_identification_number",
                        "case_number",
                        "matter_id",
                        "case_id",
                        "external_id",
                        "matter_uuid",
                        "id",
                        "uuid",
                        default=str(len(matters_by_id)),
                    )
                    matters_by_id[str(matter_key)] = matter
            return list(matters_by_id.values())
        payload = self._get(self.matters_path, params=params)
        return self._matter_list_from_payload(payload)

    def _matter_list_from_payload(self, payload):
        if isinstance(payload, list):
            return payload
        return payload.get("results") or payload.get("data") or payload.get("matters") or []

    def _user_list_from_payload(self, payload):
        if isinstance(payload, list):
            return payload
        return payload.get("results") or payload.get("data") or payload.get("users") or []

    def get_matter(self, matter_id):
        path = f"{self.matters_path.rstrip('/')}/{matter_id}"
        payload = self._get(path)
        record = payload.get("data") if isinstance(payload, dict) else None
        return record if isinstance(record, dict) else payload

    def get_matter_documents(self, matter_id):
        # Core API v2 searches documents by the parent module's UUID. The
        # legacy matter-documents route instead expects a site-specific matter
        # id and returns 404 when given the human case number stored as this
        # app's external_id.
        if isinstance(matter_id, str) and UUID_RE.match(matter_id.strip()):
            payload = self._get(
                self.documents_path,
                params={"module": "matter", "module_uuid": matter_id.strip(), "page_size": 100},
            )
            if isinstance(payload, list):
                return payload
            return payload.get("results") or payload.get("data") or payload.get("documents") or []
        path = self.matter_documents_path.format(matter_id=matter_id)
        payload = self._get(path)
        if isinstance(payload, list):
            return payload
        return payload.get("results") or payload.get("data") or payload.get("documents") or []

    def get_matter_notes(self, matter_uuid):
        """Return direct and related case notes through Core API v2."""
        if not isinstance(matter_uuid, str) or not UUID_RE.match(matter_uuid.strip()):
            return []
        payload = self._get(
            f"{self.matters_path.rstrip('/')}/{quote(matter_uuid.strip(), safe='')}/notes",
            params={"page_size": 100},
        )
        if isinstance(payload, list):
            return payload
        return payload.get("results") or payload.get("data") or payload.get("notes") or []

    def find_user(self, identifier):
        if not identifier or not self.users_path:
            return {}
        users_by_id = {}
        rejected = 0
        for field in self.user_search_fields:
            try:
                payload = self._get(self.users_path, params={field: identifier, "page_size": 10})
            except LegalServerError as error:
                # Which user search keys exist varies by LegalServer version and
                # site configuration, and a site rejects an unsupported key -- or
                # a value of the wrong shape, such as a bare username sent to an
                # email field -- with a 400. Skipping that key and trying the
                # rest finds the user; aborting on the first rejection never did.
                if error.status_code == 400:
                    rejected += 1
                    continue
                raise
            for user in self._user_list_from_payload(payload):
                user_key = _first_value(
                    user,
                    "id",
                    "user_id",
                    "uuid",
                    "user_uuid",
                    "email",
                    "user_email",
                    "user_name",
                    "username",
                    default=str(len(users_by_id)),
                )
                users_by_id[str(user_key)] = user
        if not users_by_id and rejected == len(self.user_search_fields):
            raise LegalServerError(
                "LegalServer rejected every configured user search key; "
                "check LEGALSERVER user search configuration.",
                status_code=400,
            )
        normalized = identifier.casefold().strip()
        matches = []
        for user in users_by_id.values():
            values = [
                _first_value(user, "email", "user_email", "email_address", default=""),
                _first_value(user, "user_name", "username", "login", default=""),
            ]
            if any(str(value).casefold().strip() == normalized for value in values if value):
                matches.append(user)
        return matches[0] if len(matches) == 1 else {}


def user_email_for_filter(user):
    if user and getattr(user, "is_authenticated", False):
        return getattr(user, "email", "") or getattr(user, "username", "")
    return ""


def user_identifier_for_filter(user):
    return UserSourceIdentity.identifier_for(user, "legalserver")


def matter_payload_to_defaults(payload):
    client = _display_value(_first_value(payload, "client_name", "client_full_name", "client", "full_name", "name", default="Unknown client"))
    matter_type = _display_value(
        _first_value(
            payload,
            "matter_type",
            "case_type",
            "legal_problem_code",
            "problem_code",
            default="Housing",
        )
    )
    jurisdiction = _display_value(_first_value(payload, "jurisdiction", "court", "county_of_dispute", "county", default=""))
    summary = _display_value(
        _first_value(
            payload,
            "summary",
            "case_summary",
            "case_title",
            "pro_bono_opportunity_summary",
            "description",
            default="",
        )
    )
    return {
        "client_name": client,
        "matter_type": matter_type,
        "jurisdiction": jurisdiction,
        "posture": _display_value(_first_value(payload, "posture", "case_status", "status", "case_disposition", default="")),
        "risk": _display_value(_first_value(payload, "risk", "priority", "emergency", default="")),
        "summary": summary,
        "source_system": "LegalServer",
        "raw_payload": payload,
    }


class LegalServerConnector(SourceConnector):
    kind = "legalserver"
    label = "LegalServer"
    detail = "Matters, case notes, parties, deadlines, and case documents"

    def __init__(self, client=None):
        self.client = client or LegalServerClient()

    @property
    def status(self):
        return "Connected" if self.client.configured else "Configure LEGALSERVER_BASE_URL and LegalServer API credentials"

    def search(self, query, *, matter=None, jurisdiction="", limit=5, user=None, request=None):
        if not self.client.configured:
            return []
        try:
            matter_reference = (
                legalserver_matter_uuid(matter.raw_payload or {}) or matter.external_id
                if matter
                else ""
            )
            documents = self.client.get_matter_documents(matter_reference) if matter else []
            matters = []
            if not matter:
                from apps.matters.services import legalserver_access_profile_for_user, payload_matches_legalserver_identifier

                access_profile = legalserver_access_profile_for_user(user, client=self.client)
                if access_profile.identifier and not access_profile.error:
                    matters = self.client.search_matters(
                        query=query,
                        user_email="" if access_profile.is_superuser else access_profile.identifier,
                        limit=limit,
                    )
                    if not access_profile.is_superuser:
                        matters = [payload for payload in matters if payload_matches_legalserver_identifier(payload, access_profile.identifier)]
        except LegalServerError:
            return []

        results = []
        for doc in documents[:limit]:
            doc_id = _first_value(doc, "id", "document_id", "uuid", default="")
            title = _first_value(doc, "title", "name", "filename", default="LegalServer document")
            storage = _first_value(doc, "storage", "storage_provider", "source", default="LegalServer")
            url = _first_value(doc, "download_url", "url", "web_url", "sharepoint_url", default="")
            results.append(
                SourceResult(
                    id=f"lsdoc:{matter.external_id}:{doc_id or title}",
                    title=title,
                    snippet=_first_value(doc, "summary", "description", "snippet", default=f"Case document stored in {storage}."),
                    source_kind=self.kind,
                    source_label="LegalServer case document",
                    citation=title,
                    url=url,
                    metadata={"matter": matter.external_id, "storage": storage, "raw": doc},
                )
            )

        for payload in matters[:limit]:
            matter_id = legalserver_matter_identifier(payload)
            defaults = matter_payload_to_defaults(payload)
            results.append(
                SourceResult(
                    id=f"ls:{matter_id}",
                    title=f"{defaults['client_name']} - {defaults['matter_type']}",
                    snippet=defaults["summary"] or "LegalServer matter match.",
                    source_kind=self.kind,
                    source_label="LegalServer matter",
                    citation=f"LegalServer matter {matter_id}",
                    metadata={"matter": matter_id, "jurisdiction": defaults["jurisdiction"], "raw": payload},
                )
            )
        return results
