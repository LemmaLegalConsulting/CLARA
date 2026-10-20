"""PostgreSQL SQL checks for revision row locks.

SQLite ignores ``select_for_update()``, so it cannot catch a lock that reaches
through an outer join to a nullable relation. Compile the production queryset
with Django's PostgreSQL backend without opening a database connection.
"""

from unittest.mock import patch

from django.conf import settings
from django.db.backends.postgresql.base import DatabaseWrapper
from django.test import SimpleTestCase

from apps.drafting.models import DraftDocument
from apps.drafting.revisions import _locked_draft_queryset


class RevisionLockPostgreSQLSQLTests(SimpleTestCase):
    def test_draft_revision_lock_targets_only_the_draft_row(self):
        postgres = DatabaseWrapper(
            {
                **settings.DATABASES["default"],
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "compile_only",
                "USER": "",
                "PASSWORD": "",
                "HOST": "",
                "PORT": "",
                "OPTIONS": {},
            },
            alias="postgresql_compile_only",
        )
        # The compiler only needs the backend features/ops. Compilation checks
        # autocommit state, so report an active transaction without connecting.
        try:
            with patch.object(postgres, "get_autocommit", return_value=False):
                queryset = _locked_draft_queryset().filter(pk=1)
                sql, _params = queryset.query.get_compiler(connection=postgres).as_sql()
        finally:
            postgres.close()

        self.assertIn("LEFT OUTER JOIN", sql)
        self.assertTrue(
            sql.endswith(
                f"FOR UPDATE OF {postgres.ops.quote_name(DraftDocument._meta.db_table)}"
            ),
            sql,
        )

