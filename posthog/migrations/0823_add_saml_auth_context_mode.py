# Generated manually for SAML authentication context mode

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("posthog", "0822_person_properties_size_constraint"),
    ]

    operations = [
        migrations.AddField(
            model_name="organizationdomain",
            name="saml_auth_context_mode",
            field=models.CharField(
                blank=True,
                choices=[
                    ("strict", "Strict - Only allow strong authentication contexts"),
                    ("balanced", "Balanced - Allow strong and conditionally acceptable contexts"),
                    ("permissive", "Permissive - Accept most authentication contexts (legacy behavior)"),
                ],
                default="balanced",
                help_text="Controls how strictly SAML authentication contexts are validated. Strict mode enhances security but may cause compatibility issues with some IdPs.",
                max_length=16,
                null=True,
            ),
        ),
    ]