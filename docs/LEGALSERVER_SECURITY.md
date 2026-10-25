# LegalServer permission mapping

LegalServer APIs authenticate as a LegalServer user, not as the application's signed-in Office365 user. LegalServer supports Bearer token and Basic authentication for API calls. The token or username/password represents the underlying LegalServer API user and has that user's permissions.

LegalServer recommends creating a separate API user and a separate API Access user role for each integration, granting only the API permissions needed for that integration. Administrator accounts with API access are disfavored. This application should therefore be configured with the narrowest LegalServer API account that can support the intended workflow.

Because LegalServer API calls do not enforce SSO requirements, this application must explicitly map the signed-in Office365/Django user to a LegalServer identity and enforce app-side permission boundaries.

## Access levels

The app derives a LegalServer access profile for each signed-in user.

- Django superusers and members of explicitly configured `LEGALSERVER_SUPERUSER_GROUPS` have locally administered all-case read access. Generic Django staff status grants no case access.
- Every other user needs an enabled, administrator-verified **User source identity**. Self-service remapping is disabled. A unique exact remote lookup must succeed, its account must not be disabled, and its email must match the verified local login email before any remote roles are accepted.
- A matching, active remote account with a configured `LEGALSERVER_SUPERUSER_ROLES` value may receive all-case read access. A mismatched administrator account never grants roles.
- Ordinary users need a current upstream assignment for each sensitive case read. Cached assignments alone never authorize access. Upstream lookup failures deny access; there is no offline permission grace period. Session listing uses a bounded current assignment search rather than one remote request for every cached case.
- LegalServer writeback requires a responding upstream and current case access at execution time, plus a separate Django permission for the operation: `matters.write_legalserver_documents`, `matters.write_legalserver_notes`, or `matters.write_legalserver_properties`. Read access and generic staff status do not imply any of these permissions. Failed authorization is recorded as a skipped delivery and never prevents downloading the exported document.

## LegalServer API authentication

Set `LEGALSERVER_BASE_URL` and one of the following credential options:

- Preferred: `LEGALSERVER_API_TOKEN` for Bearer token authentication.
- Alternative: `LEGALSERVER_API_USERNAME` and `LEGALSERVER_API_PASSWORD` for Basic authentication.

Bearer token authentication is preferred because credentials stay in the `Authorization` header and are not sent in query strings. Basic authentication is supported for deployments that do not use personal access tokens.

## Approving and migrating identities

Office365 sign-in requires a concrete tenant UUID and an enabled **Office365 principal** mapping the validated `(tenant_id, object_id)` to an existing active Django user. Verify these immutable identifiers in Entra; never select the local user solely because an email claim matches. Keep a local administrator login available during migration.

Existing LegalServer mappings migrate as **unverified**. Before rollout, an administrator must verify each user's local login email and LegalServer account, then enable and mark the mapping verified under **User source identities**. Retargeting a verified mapping clears verification; confirm the new mapping in a separate save. Django admin records these changes in its audit log. The verification filter helps locate pending rows.

Grant the separate write permissions to the intended users or groups. No migration automatically grants write permissions to ordinary users. Remove mappings and locally granted all-case permissions when access is revoked.

The old `LEGALSERVER_AUTO_MAP_OFFICE365_EMAIL` and `LEGALSERVER_REQUIRE_OFFICE365_EMAIL_MATCH` switches are removed: SSO does not create mappings, and administrators cannot disable the remote email-match check through configuration.

## Configurable LegalServer API shape

The defaults assume conventional endpoints and payloads, but deployments may differ.

- `LEGALSERVER_MATTERS_PATH` defaults to `/api/v2/matters`.
- `LEGALSERVER_MATTERS_RESULTS` defaults to `full`, which requests the v2 full matter result shape.
- `LEGALSERVER_MATTER_DOCUMENTS_PATH` defaults to `/api/v1/matters/{matter_id}/documents`.
- `LEGALSERVER_USERS_PATH` defaults to `/api/v1/users`.

Matter search sends only the v2 documented matter-search parameters: `page_size`, `results`, and text search keys such as `case_number`, `case_title`, `external_id`, `first`, and `last`. User access filtering is applied inside the app after LegalServer returns authorized records.

The integration account must be able to read the users endpoint. If it is unavailable, ordinary-user authorization and remote-role elevation fail closed. Locally administered all-case read permission remains independent of upstream assignments, but writes still require a successful online check.

## Premium API note

Search and get operations are available outside the Premium API set. Creating or updating matters, events, timeslips, or clinics usually requires Premium API access, so this prototype's read/search workflow should stay separate from any future writeback integration.
