import asyncio
import time
from typing import cast, Any
from uuid import uuid4
from langgraph.types import StreamWriter
import structlog
from prometheus_client import Counter, Histogram, Gauge
from langchain_core.runnables import RunnableConfig

from ee.hogai.graph.base import AssistantNode
from ee.hogai.session_summaries.constants import SESSION_SUMMARIES_STREAMING_MODEL, GROUP_SUMMARIES_MIN_SESSIONS
from ee.hogai.session_summaries.session_group.patterns import EnrichedSessionGroupSummaryPatternsList
from ee.hogai.session_summaries.session_group.summarize_session_group import find_sessions_timestamps
from ee.hogai.session_summaries.session_group.summary_notebooks import create_summary_notebook
from ee.hogai.utils.types import AssistantState, PartialAssistantState, AssistantNodeName
from posthog.schema import MaxRecordingUniversalFilters, RecordingsQuery, AssistantToolCallMessage
from posthog.sync import database_sync_to_async
from posthog.temporal.ai.session_summary.summarize_session import execute_summarize_session
from posthog.temporal.ai.session_summary.summarize_session_group import execute_summarize_session_group
from langgraph.config import get_stream_writer
from langchain_core.messages import AIMessageChunk


# Performance metrics for session summarization workflow
SESSION_SUMMARY_WORKFLOW_DURATION = Histogram(
    "session_summary_workflow_duration_seconds",
    "Time spent in session summarization workflow",
    labelnames=["workflow_type", "session_count_range", "team_id"]
)

SESSION_SUMMARY_WORKFLOW_COUNT = Counter(
    "session_summary_workflow_total",
    "Total session summarization workflows executed", 
    labelnames=["workflow_type", "session_count_range", "status"]
)

SESSION_SUMMARY_SESSION_COUNT = Gauge(
    "session_summary_sessions_processed",
    "Number of sessions processed in summarization workflow",
    labelnames=["workflow_type", "team_id"]
)

SESSION_SUMMARY_DB_QUERY_DURATION = Histogram(
    "session_summary_db_query_duration_seconds", 
    "Time spent querying database for session data",
    labelnames=["query_type", "team_id"]
)


class SessionSummarizationNode(AssistantNode):
    logger = structlog.get_logger(__name__)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def _get_session_count_range(self, count: int) -> str:
        """Categorize session count for metrics tracking."""
        if count == 1:
            return "single"
        elif count <= GROUP_SUMMARIES_MIN_SESSIONS:
            return f"small_batch_1_{GROUP_SUMMARIES_MIN_SESSIONS}"
        elif count <= 20:
            return f"medium_batch_{GROUP_SUMMARIES_MIN_SESSIONS + 1}_20"
        elif count <= 100:
            return "large_batch_21_100"
        else:
            return "extra_large_batch_100_plus"

    def _get_workflow_type(self, session_count: int) -> str:
        """Determine workflow type based on session count."""
        return "individual" if session_count <= GROUP_SUMMARIES_MIN_SESSIONS else "group"

    def _track_workflow_metrics(self, workflow_type: str, session_count: int, 
                               duration_seconds: float, status: str = "success") -> None:
        """Track workflow performance metrics."""
        session_count_range = self._get_session_count_range(session_count)
        team_id_str = str(getattr(self._team, "id", "unknown"))
        
        SESSION_SUMMARY_WORKFLOW_DURATION.labels(
            workflow_type=workflow_type, 
            session_count_range=session_count_range,
            team_id=team_id_str
        ).observe(duration_seconds)
        
        SESSION_SUMMARY_WORKFLOW_COUNT.labels(
            workflow_type=workflow_type,
            session_count_range=session_count_range, 
            status=status
        ).inc()
        
        SESSION_SUMMARY_SESSION_COUNT.labels(
            workflow_type=workflow_type,
            team_id=team_id_str
        ).set(session_count)

    def _get_stream_writer(self) -> StreamWriter | None:
        """Get the stream writer for custom events"""
        try:
            return get_stream_writer()
        except Exception as err:
            self.logger.warning(
                "Failed to get stream writer for session summarization",
                extra={"node": "SessionSummarizationNode", "error": str(err)},
            )
            # Fallback if stream writer is not available
            return None

    def _stream_progress(self, progress_message: str, writer: StreamWriter | None) -> None:
        """Push summarization progress as reasoning messages"""
        if not writer:
            self.logger.warning(
                "Stream writer is not available, cannot stream progress",
                extra={"node": "SessionSummarizationNode", "message": progress_message},
            )
            return
        message_chunk = AIMessageChunk(
            content="",
            additional_kwargs={"reasoning": {"summary": [{"text": f"**{progress_message}**"}]}},
        )
        message = (message_chunk, {"langgraph_node": AssistantNodeName.SESSION_SUMMARIZATION})
        writer(("session_summarization_node", "messages", message))
        return

    async def _generate_replay_filters(self, plain_text_query: str) -> MaxRecordingUniversalFilters | None:
        """Generates replay filters to get session ids by querying a compiled Universal filters graph."""
        from products.replay.backend.max_tools import SessionReplayFilterOptionsGraph

        graph = SessionReplayFilterOptionsGraph(self._team, self._user).compile_full_graph()
        # Call with your query
        result = await graph.ainvoke(
            {
                "change": plain_text_query,
                "current_filters": {},  # Empty state, as we need results from the query-to-filter
            }
        )
        if not result or not isinstance(result, dict) or not result.get("output"):
            self.logger.error(
                f"Invalid result from filter options graph: {result}",
                extra={
                    "team_id": getattr(self._team, "id", "unknown"),
                    "user_id": getattr(self._user, "id", "unknown"),
                    "result": result,
                },
            )
            return None
        # Extract the generated filters
        filters_data = result["output"]
        if not filters_data:
            return None
        max_filters = cast(MaxRecordingUniversalFilters, filters_data)
        return max_filters

    def _get_session_ids_with_filters(self, replay_filters: MaxRecordingUniversalFilters) -> list[str] | None:
        from posthog.session_recordings.queries.session_recording_list_from_query import SessionRecordingListFromQuery

        team_id_str = str(getattr(self._team, "id", "unknown"))
        query_start_time = time.time()
        
        try:
            # Convert Max filters into recordings query format
            properties = []
            if replay_filters.filter_group and replay_filters.filter_group.values:
                for inner_group in replay_filters.filter_group.values:
                    if hasattr(inner_group, "values"):
                        properties.extend(inner_group.values)
            recordings_query = RecordingsQuery(
                date_from=replay_filters.date_from,
                date_to=replay_filters.date_to,
                properties=properties,
                filter_test_accounts=replay_filters.filter_test_accounts,
                order=replay_filters.order,
                # Handle duration filters
                having_predicates=(
                    [
                        {"key": "duration", "type": "recording", "operator": dur.operator, "value": dur.value}
                        for dur in (replay_filters.duration or [])
                    ]
                    if replay_filters.duration
                    else None
                ),
            )
            # Execute the query to get session IDs
            query_runner = SessionRecordingListFromQuery(
                team=self._team, query=recordings_query, hogql_query_modifiers=None
            )
            results = query_runner.run()
            # Extract session IDs
            session_ids = [recording["session_id"] for recording in results.results]
            
            # Track query performance
            query_duration = time.time() - query_start_time
            SESSION_SUMMARY_DB_QUERY_DURATION.labels(
                query_type="session_ids_query", 
                team_id=team_id_str
            ).observe(query_duration)
            
            self.logger.info("Database query for session IDs completed",
                           query_duration_seconds=query_duration,
                           session_count=len(session_ids) if session_ids else 0,
                           team_id=team_id_str)
            
            return session_ids if session_ids else None
        except Exception as e:
            query_duration = time.time() - query_start_time
            SESSION_SUMMARY_DB_QUERY_DURATION.labels(
                query_type="session_ids_query_failed",
                team_id=team_id_str  
            ).observe(query_duration)
            raise

    async def _summarize_sessions_individually(self, session_ids: list[str], writer: StreamWriter | None) -> str:
        """Summarize sessions individually with progress updates."""
        total = len(session_ids)
        completed = 0

        async def _summarize(session_id: str) -> str:
            nonlocal completed
            result = await execute_summarize_session(
                session_id=session_id,
                user_id=self._user.id,
                team=self._team,
                model_to_use=SESSION_SUMMARIES_STREAMING_MODEL,
            )
            completed += 1
            # Update the user on the progress
            self._stream_progress(progress_message=f"Watching sessions ({completed}/{total})", writer=writer)
            return result

        # Run all tasks concurrently
        tasks = [_summarize(sid) for sid in session_ids]
        summaries = await asyncio.gather(*tasks)
        # TODO: Add layer to convert JSON into more readable text for Max to returns to user
        self._stream_progress(progress_message=f"Generating a summary, almost there", writer=writer)
        return "\n".join(summaries)

    async def _summarize_sessions_as_group(self, session_ids: list[str], writer: StreamWriter | None) -> str:
        """Summarize sessions as a group (for larger sets)."""
        min_timestamp, max_timestamp = find_sessions_timestamps(session_ids=session_ids, team=self._team)
        summary = None
        async for update in execute_summarize_session_group(
            session_ids=session_ids,
            user_id=self._user.id,
            team=self._team,
            min_timestamp=min_timestamp,
            max_timestamp=max_timestamp,
            extra_summary_context=None,
            local_reads_prod=False,
        ):
            if isinstance(update, str):
                # Status message - stream to user
                self._stream_progress(progress_message=update, writer=writer)
            elif isinstance(update, EnrichedSessionGroupSummaryPatternsList):
                # Final summary
                summary = update
            else:
                raise ValueError(
                    f"Unexpected update type ({type(update)}) in session group summarization (session_ids: {session_ids})."
                )
        if summary:
            await database_sync_to_async(create_summary_notebook)(
                session_ids=session_ids, user=self._user, team=self._team, summary=summary
            )
            return summary.model_dump_json(exclude_none=True)
        else:
            raise ValueError(f"No summary was generated from session group summarization (session_ids: {session_ids})")

    async def arun(self, state: AssistantState, config: RunnableConfig) -> PartialAssistantState | None:
        start_time = time.time()
        conversation_id = config.get("configurable", {}).get("thread_id", "unknown")
        writer = self._get_stream_writer()
        # If query was not provided for some reason
        if not state.session_summarization_query:
            self._log_failure(
                f"Session summarization query is not provided: {state.session_summarization_query}",
                conversation_id,
                start_time,
            )
            return self._create_error_response(self._base_error_instructions, state.root_tool_call_id)
        try:
            # Generate filters to get session ids from DB
            replay_filters = await self._generate_replay_filters(state.session_summarization_query)
            if not replay_filters:
                self._log_failure(
                    f"No Replay filters were generated for session summarization: {state.session_summarization_query}",
                    conversation_id,
                    start_time,
                )
                return self._create_error_response(self._base_error_instructions, state.root_tool_call_id)
            # Query the filters to get session ids
            session_ids = await database_sync_to_async(self._get_session_ids_with_filters)(replay_filters)
            if not session_ids:
                return PartialAssistantState(
                    messages=[
                        AssistantToolCallMessage(
                            content="No sessions were found.",
                            tool_call_id=state.root_tool_call_id or "unknown",
                            id=str(uuid4()),
                        ),
                    ],
                    session_summarization_query=None,
                    root_tool_call_id=None,
                )
            # Process sessions based on count
            session_count = len(session_ids)
            workflow_type = self._get_workflow_type(session_count)
            workflow_start_time = time.time()
            
            base_message = f"Found sessions ({session_count})"
            try:
                if session_count <= GROUP_SUMMARIES_MIN_SESSIONS:
                    # If small amount of sessions - there are no patterns to extract, so summarize them individually and return as is
                    self._stream_progress(
                        progress_message=f"{base_message}. We will do a quick summary, as the scope is small (DB-based approach for <{GROUP_SUMMARIES_MIN_SESSIONS + 1} sessions)",
                        writer=writer,
                    )
                    summaries_content = await self._summarize_sessions_individually(session_ids=session_ids, writer=writer)
                else:
                    # For large groups, process in detail, searching for patterns
                    # TODO: Allow users to define the pattern themselves (or rather catch it from the query)
                    self._stream_progress(
                        progress_message=f"{base_message}. We will analyze in detail, and store the report in a notebook (pattern-based group analysis)",
                        writer=writer,
                    )
                    summaries_content = await self._summarize_sessions_as_group(session_ids=session_ids, writer=writer)
                
                # Track successful workflow completion
                workflow_duration = time.time() - workflow_start_time
                self._track_workflow_metrics(workflow_type, session_count, workflow_duration, "success")
                
                self.logger.info("Session summarization workflow completed successfully",
                               workflow_type=workflow_type,
                               session_count=session_count,
                               duration_seconds=workflow_duration,
                               team_id=str(getattr(self._team, "id", "unknown")))
                
            except Exception as e:
                # Track failed workflow
                workflow_duration = time.time() - workflow_start_time
                self._track_workflow_metrics(workflow_type, session_count, workflow_duration, "error")
                raise
            return PartialAssistantState(
                messages=[
                    AssistantToolCallMessage(
                        content=summaries_content,
                        tool_call_id=state.root_tool_call_id or "unknown",
                        id=str(uuid4()),
                    ),
                ],
                session_summarization_query=None,
                root_tool_call_id=None,
            )
        except Exception as err:
            # Track failure metrics if we have session data
            if 'session_ids' in locals() and session_ids:
                session_count = len(session_ids)
                workflow_type = self._get_workflow_type(session_count)
                failure_duration = time.time() - start_time
                self._track_workflow_metrics(workflow_type, session_count, failure_duration, "failure")
            
            self._log_failure("Session summarization failed", conversation_id, start_time, err)
            return self._create_error_response(self._base_error_instructions, state.root_tool_call_id)

    def _create_error_response(self, message: str, root_tool_call_id: str | None) -> PartialAssistantState:
        return PartialAssistantState(
            messages=[
                AssistantToolCallMessage(
                    content=message,
                    tool_call_id=root_tool_call_id or "unknown",
                    id=str(uuid4()),
                ),
            ],
            session_summarization_query=None,
            root_tool_call_id=None,
        )

    def _log_failure(self, message: str, conversation_id: str, start_time: float, error: Any = None):
        self.logger.exception(
            message,
            extra={
                "team_id": getattr(self._team, "id", "unknown"),
                "conversation_id": conversation_id,
                "execution_time_ms": round(time.time() - start_time, 2) * 1000,
                "error": str(error) if error else None,
            },
        )

    @property
    def _base_error_instructions(self) -> str:
        return "INSTRUCTIONS: Tell the user that you encountered an issue while summarizing the session and suggest they try again with a different question."
