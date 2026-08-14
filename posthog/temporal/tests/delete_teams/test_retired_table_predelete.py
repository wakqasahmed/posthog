from typing import Any

import pytest

from django.db import connection

from posthog.models.organization import Organization
from posthog.models.team import Team
from posthog.models.team.util import RETIRED_TEAM_FK_TABLES, STALE_TEAM_FK_COLUMNS, delete_team_records

# transaction=True is required, not incidental: the foreign keys these tables hold on posthog_team
# are DEFERRABLE INITIALLY DEFERRED, so Postgres only checks them at COMMIT. Under a plain TestCase
# the inner atomic() is a savepoint, releasing it never checks them, and the regression is invisible.
pytestmark = [pytest.mark.django_db(transaction=True)]

_INSERT_DUCKGRES_SERVER = (
    "INSERT INTO posthog_duckgresserver "
    "(created_at, id, host, port, flight_port, database, username, password, "
    "organization_id, catalog_port, catalog_database, team_id) "
    "VALUES (now(), gen_random_uuid(), 'localhost', 5432, 8815, 'duckgres', 'user', 'password', "
    "%s, 5432, 'catalog', %s)"
)


def test_team_deletion_clears_rows_in_a_retired_table():
    _, _, team = Organization.objects.bootstrap(None)
    team_id = team.id
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO ee_teamsessionsummariesconfig (team_id, product_context, custom_tags) "
            "VALUES (%s, '', '{}'::jsonb)",
            [team_id],
        )

    delete_team_records([team_id])

    assert not Team.objects.filter(id=team_id).exists()


def test_team_deletion_clears_a_stale_team_column_but_keeps_the_row():
    _, _, team = Organization.objects.bootstrap(None)
    team_id = team.id
    with connection.cursor() as cursor:
        cursor.execute(_INSERT_DUCKGRES_SERVER, [team.organization_id, team_id])

    delete_team_records([team_id])

    assert not Team.objects.filter(id=team_id).exists()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM posthog_duckgresserver WHERE organization_id = %s AND team_id IS NULL",
            [team.organization_id],
        )
        assert cursor.fetchone()[0] == 1


def _db_tables(*models: Any) -> set[str]:
    return {model._meta.db_table for model in models if hasattr(model, "_meta")}


def test_no_unhandled_table_references_posthog_team():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT DISTINCT conrelid::regclass::text FROM pg_constraint "
            "WHERE contype = 'f' AND confrelid = 'posthog_team'::regclass"
        )
        referencing_tables = {row[0] for row in cursor.fetchall()}

    # include_hidden matches what Django's delete collector walks: relations declared with
    # related_name="+" still cascade even though they are absent from related_objects.
    cascade_reachable: set[str] = set()
    for field in Team._meta.get_fields(include_hidden=True):
        if not field.is_relation or field.concrete:
            continue
        cascade_reachable |= _db_tables(field.related_model, getattr(field, "through", None))
    for many_to_many in Team._meta.many_to_many:
        cascade_reachable |= _db_tables(many_to_many.remote_field.through)

    handled = set(RETIRED_TEAM_FK_TABLES) | {table for table, _ in STALE_TEAM_FK_COLUMNS}
    unhandled = referencing_tables - cascade_reachable - handled

    assert unhandled == set(), (
        f"{sorted(unhandled)} hold a foreign key on posthog_team that Django's cascade cannot see, "
        "so they will fail team deletion at COMMIT. Drop the tables, or add them to "
        "RETIRED_TEAM_FK_TABLES / STALE_TEAM_FK_COLUMNS in posthog/models/team/util.py."
    )
