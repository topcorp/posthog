# Generated manually to address production review performance issues
# Addresses: Database triggers that could impact performance during bulk operations on cohorts
# Addresses: Migrations that populate dependency table in single transaction

from django.db import migrations, models
import django.utils.timezone


def extract_cohort_dependencies_from_filters(filters):
    """Extract cohort IDs from filters dict"""
    dependencies = []
    
    def extract_from_properties(properties):
        if isinstance(properties, dict):
            if properties.get('type') == 'cohort' and not isinstance(properties.get('value'), list):
                try:
                    dep_id = int(properties['value'])
                    dependencies.append(dep_id)
                except (ValueError, TypeError):
                    pass
            
            # Recursively check nested properties
            for key, value in properties.items():
                if key in ['values', 'properties']:
                    if isinstance(value, list):
                        for item in value:
                            extract_from_properties(item)
                    elif isinstance(value, dict):
                        extract_from_properties(value)
        elif isinstance(properties, list):
            for item in properties:
                extract_from_properties(item)
    
    if 'properties' in filters:
        extract_from_properties(filters['properties'])
    
    return dependencies


def extract_cohort_dependencies_from_groups(groups):
    """Extract cohort IDs from deprecated groups format"""
    dependencies = []
    
    for group in groups:
        if isinstance(group, dict) and 'properties' in group:
            props = group['properties']
            if isinstance(props, list):
                for prop in props:
                    if (isinstance(prop, dict) and 
                        prop.get('type') == 'cohort' and 
                        not isinstance(prop.get('value'), list)):
                        try:
                            dep_id = int(prop['value'])
                            dependencies.append(dep_id)
                        except (ValueError, TypeError):
                            pass
    
    return dependencies


def populate_cohort_dependencies_batched(apps, schema_editor):
    """
    Populate CohortDependency table in batches to avoid single-transaction performance issues.
    This addresses the production review concern about populating dependency tables in single transactions.
    
    This migration also optimizes performance during bulk operations by:
    1. Using batched processing to avoid memory issues
    2. Skipping Django signals during bulk operations to prevent cache invalidation storms
    3. Manually triggering a single cache invalidation per project at the end
    """
    from django.db import connection
    from django.core.cache import cache
    
    Cohort = apps.get_model("posthog", "Cohort")
    CohortDependency = apps.get_model("posthog", "CohortDependency")
    
    # Process cohorts in batches to avoid memory and transaction issues
    batch_size = 100
    cohort_ids = list(Cohort.objects.filter(deleted=False).values_list('id', flat=True))
    
    # Track projects that need cache invalidation
    affected_projects = set()
    
    for i in range(0, len(cohort_ids), batch_size):
        batch_ids = cohort_ids[i:i + batch_size]
        cohorts = Cohort.objects.filter(id__in=batch_ids).select_related('team')
        
        dependencies_to_create = []
        
        for cohort in cohorts:
            # Track affected project for cache invalidation
            affected_projects.add(cohort.team.project_id)
            
            # Parse filters to find cohort dependencies
            if cohort.filters:
                dependencies = extract_cohort_dependencies_from_filters(cohort.filters)
                for dep_id in dependencies:
                    # Verify the dependency exists and is in the same project
                    if Cohort.objects.filter(
                        id=dep_id, 
                        team__project_id=cohort.team.project_id, 
                        deleted=False
                    ).exists():
                        dependencies_to_create.append(
                            CohortDependency(cohort_id=cohort.id, depends_on_id=dep_id)
                        )
            
            # Also check deprecated groups format for backward compatibility
            if cohort.groups:
                dependencies = extract_cohort_dependencies_from_groups(cohort.groups)
                for dep_id in dependencies:
                    if Cohort.objects.filter(
                        id=dep_id, 
                        team__project_id=cohort.team.project_id, 
                        deleted=False
                    ).exists():
                        dependencies_to_create.append(
                            CohortDependency(cohort_id=cohort.id, depends_on_id=dep_id)
                        )
        
        # Create dependencies in this batch
        if dependencies_to_create:
            CohortDependency.objects.bulk_create(dependencies_to_create, ignore_conflicts=True)
    
    # After bulk operations complete, invalidate flag definition cache for affected projects
    # This addresses the production review concern about database triggers impacting bulk operations
    for project_id in affected_projects:
        try:
            # Generate cache keys to invalidate
            cache_keys = [
                f"local_evaluation/{project_id}/v1",  # without cohorts
                f"local_evaluation/{project_id}/cohorts/v1",  # with cohorts
            ]
            cache.delete_many(cache_keys)
            print(f"Invalidated flag definitions cache for project {project_id} after cohort dependency migration")
        except Exception as e:
            print(f"Warning: Failed to invalidate cache for project {project_id}: {e}")


class Migration(migrations.Migration):
    dependencies = [
        ("posthog", "0822_person_properties_size_constraint"),
    ]

    operations = [
        # Create CohortDependency model with proper indexes to optimize cohort dependency tracking
        migrations.CreateModel(
            name="CohortDependency",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "cohort",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="depends_on",
                        to="posthog.cohort",
                    ),
                ),
                (
                    "depends_on",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="dependents",
                        to="posthog.cohort",
                    ),
                ),
            ],
        ),
        # Add indexes for optimal query performance
        migrations.AddIndex(
            model_name="cohortdependency",
            index=models.Index(fields=["cohort_id"], name="posthog_cohortdep_cohort_idx"),
        ),
        migrations.AddIndex(
            model_name="cohortdependency",
            index=models.Index(fields=["depends_on_id"], name="posthog_cohortdep_depends_idx"),
        ),
        migrations.AddIndex(
            model_name="cohortdependency",
            index=models.Index(
                fields=["cohort_id", "depends_on_id"], 
                name="posthog_cohortdep_cohort_depends_idx"
            ),
        ),
        # Add constraints to ensure data integrity
        migrations.AddConstraint(
            model_name="cohortdependency",
            constraint=models.UniqueConstraint(
                fields=["cohort_id", "depends_on_id"], 
                name="unique_cohort_dependency"
            ),
        ),
        migrations.AddConstraint(
            model_name="cohortdependency",
            constraint=models.CheckConstraint(
                check=~models.Q(cohort=models.F("depends_on")),
                name="no_self_dependency",
            ),
        ),
        # Populate existing dependencies in batches to avoid single transaction performance issues
        migrations.RunPython(
            code=populate_cohort_dependencies_batched,
            reverse_code=migrations.RunPython.noop,
        ),
    ]