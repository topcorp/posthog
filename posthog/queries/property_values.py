import time
from contextlib import contextmanager
from typing import Optional

from django.utils import timezone
from django.core.cache import cache
from clickhouse_driver.errors import Error as ClickHouseError

from posthog.models.event.sql import SELECT_PROP_VALUES_SQL_WITH_FILTER
from posthog.models.person.sql import (
    SELECT_PERSON_PROP_VALUES_SQL,
    SELECT_PERSON_PROP_VALUES_SQL_WITH_FILTER,
)
from posthog.models.property.util import get_property_string_expr
from posthog.models.team import Team
from posthog.queries.insight import insight_sync_execute
from posthog.utils import relative_date_parse
from posthog.clickhouse.client.limit import get_api_personal_rate_limiter


class PropertyValueCircuitBreaker:
    """Circuit breaker for property value queries to prevent resource exhaustion"""

    def __init__(self, team_id: int):
        self.team_id = team_id
        self.failure_threshold = 5  # Number of failures before opening circuit
        self.timeout = 300  # Seconds to keep circuit open (5 minutes)
        self.cache_key_base = f"property_value_circuit_breaker:{team_id}"

    @contextmanager
    def protect(self):
        """Context manager that implements circuit breaker pattern"""
        if self._is_circuit_open():
            raise ClickHouseError("Property value search temporarily unavailable due to repeated failures")

        try:
            yield
            self._record_success()
        except (ClickHouseError, Exception) as e:
            self._record_failure()
            # Check if we should open the circuit after this failure
            if self._should_open_circuit():
                self._open_circuit()
            raise e

    def _is_circuit_open(self) -> bool:
        """Check if circuit is currently open"""
        open_until = cache.get(f"{self.cache_key_base}:open_until")
        return open_until is not None and time.time() < open_until

    def _should_open_circuit(self) -> bool:
        """Check if we should open circuit based on failure count"""
        failure_count = cache.get(f"{self.cache_key_base}:failures", 0)
        return failure_count >= self.failure_threshold

    def _open_circuit(self):
        """Open the circuit for the timeout period"""
        cache.set(f"{self.cache_key_base}:open_until", time.time() + self.timeout, self.timeout)
        cache.delete(f"{self.cache_key_base}:failures")

    def _record_failure(self):
        """Record a failure and increment counter"""
        cache_key = f"{self.cache_key_base}:failures"
        current_failures = cache.get(cache_key, 0)
        cache.set(cache_key, current_failures + 1, 3600)  # Keep for 1 hour

    def _record_success(self):
        """Clear failure count on successful execution"""
        cache.delete(f"{self.cache_key_base}:failures")


def get_property_values_for_key(
    key: str,
    team: Team,
    event_names: Optional[list[str]] = None,
    value: Optional[str] = None,
):
    circuit_breaker = PropertyValueCircuitBreaker(team.pk)
    rate_limiter = get_api_personal_rate_limiter()

    with circuit_breaker.protect():
        with rate_limiter.run(team_id=team.pk, org_id=str(team.organization_id), is_api=True):
            property_field, mat_column_exists = get_property_string_expr("events", key, "%(key)s", "properties")
            parsed_date_from = "AND timestamp >= '{}'".format(
                relative_date_parse("-7d", team.timezone_info).strftime("%Y-%m-%d 00:00:00")
            )
            parsed_date_to = "AND timestamp <= '{}'".format(timezone.now().strftime("%Y-%m-%d 23:59:59"))
            property_exists_filter = ""
            event_filter = ""
            value_filter = ""
            order_by_clause = ""
            extra_params = {}

            if mat_column_exists:
                property_exists_filter = "AND notEmpty({})".format(property_field)
            else:
                property_exists_filter = "AND JSONHas(properties, %(key)s)"
                extra_params["key"] = key

            if event_names is not None and len(event_names) > 0:
                event_conditions_list = []
                for index, event_name in enumerate(event_names):
                    event_conditions_list.append(f"event = %(event_{index})s")
                    extra_params[f"event_{index}"] = event_name

                event_conditions = " OR ".join(event_conditions_list)
                event_filter = "AND ({})".format(event_conditions)

            if value:
                value_filter = "AND {} ILIKE %(value)s".format(property_field)
                extra_params["value"] = "%{}%".format(value)

                order_by_clause = f"order by length({property_field})"

            return insight_sync_execute(
                SELECT_PROP_VALUES_SQL_WITH_FILTER.format(
                    parsed_date_from=parsed_date_from,
                    parsed_date_to=parsed_date_to,
                    property_field=property_field,
                    event_filter=event_filter,
                    value_filter=value_filter,
                    property_exists_filter=property_exists_filter,
                    order_by_clause=order_by_clause,
                ),
                {"team_id": team.pk, "key": key, **extra_params},
                query_type="get_property_values_with_value",
                team_id=team.pk,
            )


def get_person_property_values_for_key(key: str, team: Team, value: Optional[str] = None):
    circuit_breaker = PropertyValueCircuitBreaker(team.pk)
    rate_limiter = get_api_personal_rate_limiter()

    with circuit_breaker.protect():
        with rate_limiter.run(team_id=team.pk, org_id=str(team.organization_id), is_api=True):
            property_field, _ = get_property_string_expr("person", key, "%(key)s", "properties")

            if value:
                return insight_sync_execute(
                    SELECT_PERSON_PROP_VALUES_SQL_WITH_FILTER.format(property_field=property_field),
                    {"team_id": team.pk, "key": key, "value": "%{}%".format(value)},
                    query_type="get_person_property_values_with_value",
                    team_id=team.pk,
                )
            return insight_sync_execute(
                SELECT_PERSON_PROP_VALUES_SQL.format(property_field=property_field),
                {"team_id": team.pk, "key": key},
                query_type="get_person_property_values",
                team_id=team.pk,
            )
