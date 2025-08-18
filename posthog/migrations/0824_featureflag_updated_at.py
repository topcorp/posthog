# Generated manually to add updated_at field to FeatureFlag model

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("posthog", "0823_add_saml_auth_context_mode"),
    ]

    operations = [
        migrations.AddField(
            model_name="featureflag",
            name="updated_at",
            field=models.DateTimeField(auto_now=True, null=True),
        ),
    ]