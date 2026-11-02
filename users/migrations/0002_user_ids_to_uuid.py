"""Convert user primary/foreign keys from integers to UUIDs.

The live database was created before the dedicated ``users`` app existed, so
``auth_user.id`` (and every ``user_id`` foreign key pointing at it) is an
integer. The model now uses a UUID primary key. A plain ``AlterField`` cannot
do this conversion because PostgreSQL has no implicit cast from ``integer`` to
``uuid``, and the existing rows must keep their relationships.

This migration:

1. builds a temporary ``old integer id -> new UUID`` map for every existing
   user;
2. drops the foreign keys that point at ``auth_user(id)``;
3. converts the referencing columns and ``auth_user.id`` with that map;
4. restores the foreign keys exactly as they were (names, DELETE behaviour and
   deferrability all come from ``pg_get_constraintdef``).

It is intentionally irreversible: the original integer values are gone once the
UUIDs are assigned, so "migrating back" would have to invent new ids and could
silently break external references.
"""

import uuid

from django.db import migrations, models


def _quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def _table_name(regclass_text):
    """Return the bare table name from a ``regclass`` rendering.

    ``conrelid::regclass::text`` normally returns ``forex_profile`` (public is
    in the search path). Strip a schema prefix/quotes defensively so the name
    can be used against ``information_schema``.
    """
    name = regclass_text.rsplit('.', 1)[-1]
    return name.strip('"')


def convert_user_ids_to_uuid(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != 'postgresql':
        # The live/production database is PostgreSQL. SQLite (used by some
        # local runs) is dynamically typed and cannot express this conversion
        # portably; leave it untouched rather than corrupting mixed id types.
        return

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT data_type
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'auth_user'
              AND column_name = 'id'
            """
        )
        row = cursor.fetchone()
        if row is None or row[0] == 'uuid':
            return

        # Capture every foreign key that references auth_user(id) *before*
        # dropping it, so the exact definition can be restored afterwards.
        cursor.execute(
            """
            SELECT con.conname,
                   con.conrelid::regclass::text,
                   a.attname,
                   pg_get_constraintdef(con.oid)
            FROM pg_constraint con
            JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
              ON TRUE
            JOIN pg_attribute a
              ON a.attrelid = con.conrelid AND a.attnum = k.attnum
            WHERE con.contype = 'f'
              AND con.confrelid = 'auth_user'::regclass
            ORDER BY con.conname, k.ord
            """
        )
        foreign_keys = cursor.fetchall()

        # A single old-id -> new-UUID map is shared by auth_user.id and every
        # foreign key, so relationships are preserved.
        cursor.execute(
            'CREATE TEMP TABLE _user_uuid_map '
            '(old_id integer PRIMARY KEY, new_id uuid NOT NULL)'
        )
        cursor.execute(
            'INSERT INTO _user_uuid_map (old_id, new_id) '
            'SELECT id, gen_random_uuid() FROM auth_user'
        )
        cursor.execute(
            """
            CREATE FUNCTION pg_temp._user_uuid(integer) RETURNS uuid
            LANGUAGE sql STABLE AS $$
                SELECT new_id FROM pg_temp._user_uuid_map WHERE old_id = $1
            $$
            """
        )

        for conname, table, _column, _definition in foreign_keys:
            cursor.execute(
                f'ALTER TABLE {table} DROP CONSTRAINT {_quote(conname)}'
            )

        for _conname, table, column, _definition in foreign_keys:
            cursor.execute(
                f'ALTER TABLE {table} ALTER COLUMN {_quote(column)} '
                f'TYPE uuid USING pg_temp._user_uuid({_quote(column)})'
            )

        # AutoField on PostgreSQL is an identity column; remove the integer
        # generator (and any serial default) before changing the type.
        cursor.execute(
            "SELECT pg_get_serial_sequence('auth_user', 'id')"
        )
        sequence = cursor.fetchone()[0]
        cursor.execute(
            'ALTER TABLE auth_user ALTER COLUMN id DROP IDENTITY IF EXISTS'
        )
        cursor.execute('ALTER TABLE auth_user ALTER COLUMN id DROP DEFAULT')
        cursor.execute(
            'ALTER TABLE auth_user ALTER COLUMN id '
            'TYPE uuid USING pg_temp._user_uuid(id)'
        )
        if sequence:
            cursor.execute(f'DROP SEQUENCE IF EXISTS {sequence}')

        for conname, table, _column, definition in foreign_keys:
            cursor.execute(
                f'ALTER TABLE {table} ADD CONSTRAINT '
                f'{_quote(conname)} {definition}'
            )

        cursor.execute('DROP FUNCTION IF EXISTS pg_temp._user_uuid(integer)')
        cursor.execute('DROP TABLE IF EXISTS _user_uuid_map')


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0001_initial'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name='user',
                    name='id',
                    field=models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
            ],
            database_operations=[
                migrations.RunPython(convert_user_ids_to_uuid),
            ],
        ),
    ]
