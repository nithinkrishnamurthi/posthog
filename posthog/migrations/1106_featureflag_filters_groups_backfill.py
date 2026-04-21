from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("posthog", "1105_featureflag_filters_groups_default")]

    operations = [
        # Idempotent backfill enforcing the groups-key invariant on existing rows.
        migrations.RunSQL(
            sql="""
                UPDATE posthog_featureflag
                SET filters = COALESCE(filters, '{}'::jsonb) || '{"groups": []}'::jsonb
                WHERE filters IS NULL
                   OR NOT (filters ? 'groups');
            """,
            reverse_sql=migrations.RunSQL.noop,
            elidable=True,
        ),
    ]
