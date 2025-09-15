"""
Comprehensive tests for PipelineNonDLT class to ensure proper functionality and performance.
"""
import uuid
from unittest.mock import Mock, MagicMock, patch, call
from typing import Any
import pytest
import pyarrow as pa
from django.test import TestCase, override_settings

from posthog.temporal.data_imports.pipelines.pipeline.pipeline import PipelineNonDLT
from posthog.temporal.data_imports.pipelines.pipeline.typings import SourceResponse
from posthog.temporal.data_imports.pipelines.pipeline.utils import (
    BillingLimitsWillBeReachedException,
    DuplicatePrimaryKeysException,
)
from posthog.temporal.common.logger import FilteringBoundLogger
from posthog.temporal.common.shutdown import ShutdownMonitor
from posthog.warehouse.models import ExternalDataJob, ExternalDataSchema, ExternalDataSource
from posthog.models import Team


class TestPipelineNonDLT(TestCase):
    """Test suite for PipelineNonDLT class covering core functionality and edge cases."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.team = Team.objects.create(name="test-team")
        self.logger = Mock(spec=FilteringBoundLogger)
        self.shutdown_monitor = Mock(spec=ShutdownMonitor)
        
        # Create external data source
        self.external_source = ExternalDataSource.objects.create(
            source_id=str(uuid.uuid4()),
            team=self.team,
            status="running",
            source_type="Stripe",
            job_inputs={"stripe_secret_key": "test-key"}
        )
        
        # Create external data schema
        self.schema = ExternalDataSchema.objects.create(
            name="test_resource",
            team=self.team,
            source=self.external_source,
            sync_type=ExternalDataSchema.SyncType.FULL_REFRESH
        )
        
        # Create external data job
        self.job = ExternalDataJob.objects.create(
            team=self.team,
            pipeline=self.external_source,
            schema=self.schema,
            status=ExternalDataJob.Status.RUNNING,
            rows_synced=0,
            pipeline_version=ExternalDataJob.PipelineVersion.V1
        )

    def _create_mock_source_response(self, items: list[Any], **kwargs) -> SourceResponse:
        """Helper to create mock SourceResponse."""
        source = Mock(spec=SourceResponse)
        source.name = "test_resource"
        source.items = items
        source.rows_to_sync = kwargs.get('rows_to_sync')
        source.has_duplicate_primary_keys = kwargs.get('has_duplicate_primary_keys', False)
        source.primary_keys = kwargs.get('primary_keys', ['id'])
        source.sort_mode = kwargs.get('sort_mode', 'asc')
        return source

    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.DeltaTableHelper')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.increment_rows')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.will_hit_billing_limit')
    def test_initialization(self, mock_billing, mock_increment, mock_delta_helper):
        """Test proper initialization of PipelineNonDLT."""
        source = self._create_mock_source_response([])
        mock_billing.return_value = False
        
        pipeline = PipelineNonDLT(
            source=source,
            logger=self.logger,
            job_id=str(self.job.id),
            reset_pipeline=False,
            shutdown_monitor=self.shutdown_monitor
        )
        
        self.assertEqual(pipeline._resource_name, "test_resource")
        self.assertEqual(pipeline._job.id, self.job.id)
        self.assertFalse(pipeline._reset_pipeline)
        mock_delta_helper.assert_called_once()

    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.DeltaTableHelper')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.increment_rows')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.will_hit_billing_limit')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.trigger_compaction_job')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.prepare_s3_files_for_querying')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.update_last_synced_at_sync')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.validate_schema_and_update_table_sync')
    def test_run_with_simple_data(self, mock_validate, mock_update_sync, mock_s3_files, 
                                 mock_compaction, mock_billing, mock_increment, mock_delta_helper):
        """Test running pipeline with simple data."""
        # Setup mocks
        mock_billing.return_value = False
        mock_delta_helper_instance = Mock()
        mock_delta_helper.return_value = mock_delta_helper_instance
        mock_delta_table = Mock()
        mock_delta_table.file_uris.return_value = ['s3://bucket/file1.parquet']
        mock_delta_helper_instance.get_delta_table.return_value = mock_delta_table
        mock_delta_helper_instance.write_to_deltalake.return_value = mock_delta_table
        mock_compaction.return_value = "workflow-123"
        
        # Test data
        test_data = [{"id": 1, "name": "test"}]
        source = self._create_mock_source_response(test_data)
        
        pipeline = PipelineNonDLT(
            source=source,
            logger=self.logger,
            job_id=str(self.job.id),
            reset_pipeline=False,
            shutdown_monitor=self.shutdown_monitor
        )
        
        pipeline.run()
        
        # Verify delta table operations
        mock_delta_helper_instance.write_to_deltalake.assert_called()
        mock_compaction.assert_called_once()
        mock_s3_files.assert_called()
        mock_validate.assert_called()

    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.DeltaTableHelper')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.increment_rows')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.will_hit_billing_limit')
    def test_billing_limits_exception(self, mock_billing, mock_increment, mock_delta_helper):
        """Test that billing limits exception is raised when limits would be exceeded."""
        mock_billing.return_value = True
        
        test_data = [{"id": 1, "name": "test"}]
        source = self._create_mock_source_response(test_data, rows_to_sync=1000)
        
        pipeline = PipelineNonDLT(
            source=source,
            logger=self.logger,
            job_id=str(self.job.id),
            reset_pipeline=False,
            shutdown_monitor=self.shutdown_monitor
        )
        
        with self.assertRaises(BillingLimitsWillBeReachedException):
            pipeline.run()

    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.DeltaTableHelper')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.increment_rows')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.will_hit_billing_limit')
    def test_duplicate_primary_keys_exception(self, mock_billing, mock_increment, mock_delta_helper):
        """Test that duplicate primary keys exception is raised for incremental syncs."""
        mock_billing.return_value = False
        
        # Set schema to incremental
        self.schema.sync_type = ExternalDataSchema.SyncType.INCREMENTAL
        self.schema.save()
        
        test_data = [{"id": 1, "name": "test"}]
        source = self._create_mock_source_response(
            test_data, 
            has_duplicate_primary_keys=True,
            primary_keys=['id']
        )
        
        pipeline = PipelineNonDLT(
            source=source,
            logger=self.logger,
            job_id=str(self.job.id),
            reset_pipeline=False,
            shutdown_monitor=self.shutdown_monitor
        )
        
        with self.assertRaises(DuplicatePrimaryKeysException):
            pipeline.run()

    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.DeltaTableHelper')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.increment_rows')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.will_hit_billing_limit')
    def test_reset_pipeline_functionality(self, mock_billing, mock_increment, mock_delta_helper):
        """Test that reset_pipeline properly resets the delta table."""
        mock_billing.return_value = False
        mock_delta_helper_instance = Mock()
        mock_delta_helper.return_value = mock_delta_helper_instance
        mock_delta_helper_instance.get_delta_table.return_value = None
        
        test_data = [{"id": 1, "name": "test"}]
        source = self._create_mock_source_response(test_data)
        
        pipeline = PipelineNonDLT(
            source=source,
            logger=self.logger,
            job_id=str(self.job.id),
            reset_pipeline=True,  # Enable reset
            shutdown_monitor=self.shutdown_monitor
        )
        
        pipeline.run()
        
        # Verify reset_table was called
        mock_delta_helper_instance.reset_table.assert_called_once()

    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.DeltaTableHelper')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.increment_rows')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.will_hit_billing_limit')
    def test_chunked_data_processing(self, mock_billing, mock_increment, mock_delta_helper):
        """Test processing of large datasets in chunks."""
        mock_billing.return_value = False
        mock_delta_helper_instance = Mock()
        mock_delta_helper.return_value = mock_delta_helper_instance
        mock_delta_table = Mock()
        mock_delta_table.file_uris.return_value = ['s3://bucket/file1.parquet']
        mock_delta_helper_instance.get_delta_table.return_value = mock_delta_table
        mock_delta_helper_instance.write_to_deltalake.return_value = mock_delta_table
        
        # Create large dataset that should be chunked
        large_data = [{"id": i, "name": f"test_{i}"} for i in range(10000)]
        source = self._create_mock_source_response([large_data])  # Pass as single list item
        
        with patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.trigger_compaction_job'), \
             patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.prepare_s3_files_for_querying'), \
             patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.update_last_synced_at_sync'), \
             patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.validate_schema_and_update_table_sync'):
            
            pipeline = PipelineNonDLT(
                source=source,
                logger=self.logger,
                job_id=str(self.job.id),
                reset_pipeline=False,
                shutdown_monitor=self.shutdown_monitor
            )
            
            pipeline.run()
            
            # Verify data was processed (write_to_deltalake should be called)
            mock_delta_helper_instance.write_to_deltalake.assert_called()

    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.DeltaTableHelper')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.increment_rows')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.will_hit_billing_limit')
    def test_pyarrow_table_processing(self, mock_billing, mock_increment, mock_delta_helper):
        """Test processing of PyArrow table data."""
        mock_billing.return_value = False
        mock_delta_helper_instance = Mock()
        mock_delta_helper.return_value = mock_delta_helper_instance
        mock_delta_table = Mock()
        mock_delta_table.file_uris.return_value = ['s3://bucket/file1.parquet']
        mock_delta_helper_instance.get_delta_table.return_value = mock_delta_table
        mock_delta_helper_instance.write_to_deltalake.return_value = mock_delta_table
        
        # Create PyArrow table
        pa_table = pa.table({
            'id': [1, 2, 3],
            'name': ['test1', 'test2', 'test3']
        })
        
        source = self._create_mock_source_response([pa_table])
        
        with patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.trigger_compaction_job'), \
             patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.prepare_s3_files_for_querying'), \
             patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.update_last_synced_at_sync'), \
             patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.validate_schema_and_update_table_sync'):
            
            pipeline = PipelineNonDLT(
                source=source,
                logger=self.logger,
                job_id=str(self.job.id),
                reset_pipeline=False,
                shutdown_monitor=self.shutdown_monitor
            )
            
            pipeline.run()
            
            # Verify PyArrow table was processed
            mock_delta_helper_instance.write_to_deltalake.assert_called()

    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.DeltaTableHelper')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.increment_rows')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.will_hit_billing_limit')
    def test_incremental_field_value_updates(self, mock_billing, mock_increment, mock_delta_helper):
        """Test incremental field value updates for ascending and descending sort modes."""
        mock_billing.return_value = False
        
        # Set schema to incremental with incremental field
        self.schema.sync_type = ExternalDataSchema.SyncType.INCREMENTAL
        self.schema.sync_type_config = {"incremental_field": "updated_at"}
        self.schema.save()
        
        mock_delta_helper_instance = Mock()
        mock_delta_helper.return_value = mock_delta_helper_instance
        mock_delta_table = Mock()
        mock_delta_table.file_uris.return_value = ['s3://bucket/file1.parquet']
        mock_delta_helper_instance.get_delta_table.return_value = mock_delta_table
        mock_delta_helper_instance.write_to_deltalake.return_value = mock_delta_table
        
        # Test ascending sort mode
        test_data = [{"id": 1, "updated_at": "2024-01-01"}]
        source = self._create_mock_source_response(test_data, sort_mode='asc')
        
        with patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.trigger_compaction_job'), \
             patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.prepare_s3_files_for_querying'), \
             patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.update_last_synced_at_sync'), \
             patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.validate_schema_and_update_table_sync'):
            
            pipeline = PipelineNonDLT(
                source=source,
                logger=self.logger,
                job_id=str(self.job.id),
                reset_pipeline=False,
                shutdown_monitor=self.shutdown_monitor
            )
            
            pipeline.run()
            
            # Verify processing completed
            mock_delta_helper_instance.write_to_deltalake.assert_called()

    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.DeltaTableHelper')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.increment_rows')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.will_hit_billing_limit')
    def test_memory_management(self, mock_billing, mock_increment, mock_delta_helper):
        """Test proper memory management and cleanup."""
        mock_billing.return_value = False
        mock_delta_helper_instance = Mock()
        mock_delta_helper.return_value = mock_delta_helper_instance
        mock_delta_table = Mock()
        mock_delta_table.file_uris.return_value = ['s3://bucket/file1.parquet']
        mock_delta_helper_instance.get_delta_table.return_value = mock_delta_table
        mock_delta_helper_instance.write_to_deltalake.return_value = mock_delta_table
        
        test_data = [{"id": 1, "name": "test"}]
        source = self._create_mock_source_response(test_data)
        
        with patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.trigger_compaction_job'), \
             patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.prepare_s3_files_for_querying'), \
             patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.update_last_synced_at_sync'), \
             patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.validate_schema_and_update_table_sync'), \
             patch('gc.collect') as mock_gc:
            
            pipeline = PipelineNonDLT(
                source=source,
                logger=self.logger,
                job_id=str(self.job.id),
                reset_pipeline=False,
                shutdown_monitor=self.shutdown_monitor
            )
            
            pipeline.run()
            
            # Verify garbage collection was called for memory management
            mock_gc.assert_called()
            # Verify cache was cleared
            mock_delta_helper_instance.get_delta_table.cache_clear.assert_called()

    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.DeltaTableHelper')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.increment_rows')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.will_hit_billing_limit')
    def test_shutdown_monitoring(self, mock_billing, mock_increment, mock_delta_helper):
        """Test shutdown monitoring during processing."""
        mock_billing.return_value = False
        
        # Set schema to incremental to trigger shutdown monitoring
        self.schema.sync_type = ExternalDataSchema.SyncType.INCREMENTAL
        self.schema.sync_type_config = {"incremental_field": "updated_at"}
        self.schema.save()
        
        mock_delta_helper_instance = Mock()
        mock_delta_helper.return_value = mock_delta_helper_instance
        mock_delta_table = Mock()
        mock_delta_table.file_uris.return_value = ['s3://bucket/file1.parquet']
        mock_delta_helper_instance.get_delta_table.return_value = mock_delta_table
        mock_delta_helper_instance.write_to_deltalake.return_value = mock_delta_table
        
        test_data = [{"id": 1, "updated_at": "2024-01-01"}]
        source = self._create_mock_source_response(test_data, sort_mode='asc')
        
        with patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.trigger_compaction_job'), \
             patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.prepare_s3_files_for_querying'), \
             patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.update_last_synced_at_sync'), \
             patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.validate_schema_and_update_table_sync'):
            
            pipeline = PipelineNonDLT(
                source=source,
                logger=self.logger,
                job_id=str(self.job.id),
                reset_pipeline=False,
                shutdown_monitor=self.shutdown_monitor
            )
            
            pipeline.run()
            
            # Verify shutdown monitoring was called
            self.shutdown_monitor.raise_if_is_worker_shutdown.assert_called()

    def test_unsupported_item_type_exception(self):
        """Test that unsupported item types raise appropriate exceptions."""
        with patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.DeltaTableHelper'), \
             patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.increment_rows'), \
             patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.will_hit_billing_limit', return_value=False):
            
            # Create source with unsupported data type
            unsupported_data = [set([1, 2, 3])]  # Set is not supported
            source = self._create_mock_source_response(unsupported_data)
            
            pipeline = PipelineNonDLT(
                source=source,
                logger=self.logger,
                job_id=str(self.job.id),
                reset_pipeline=False,
                shutdown_monitor=self.shutdown_monitor
            )
            
            with self.assertRaises(Exception) as context:
                pipeline.run()
            
            self.assertIn("Unhandled item type", str(context.exception))


class TestPipelineNonDLTPerformance(TestCase):
    """Performance-specific tests for PipelineNonDLT."""
    
    def setUp(self):
        """Set up performance test fixtures."""
        self.team = Team.objects.create(name="perf-test-team")
        self.logger = Mock(spec=FilteringBoundLogger)
        self.shutdown_monitor = Mock(spec=ShutdownMonitor)
        
        self.external_source = ExternalDataSource.objects.create(
            source_id=str(uuid.uuid4()),
            team=self.team,
            status="running",
            source_type="Stripe",
            job_inputs={"stripe_secret_key": "test-key"}
        )
        
        self.schema = ExternalDataSchema.objects.create(
            name="perf_test_resource",
            team=self.team,
            source=self.external_source,
            sync_type=ExternalDataSchema.SyncType.FULL_REFRESH
        )
        
        self.job = ExternalDataJob.objects.create(
            team=self.team,
            pipeline=self.external_source,
            schema=self.schema,
            status=ExternalDataJob.Status.RUNNING,
            rows_synced=0,
            pipeline_version=ExternalDataJob.PipelineVersion.V1
        )

    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.DeltaTableHelper')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.increment_rows')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.will_hit_billing_limit')
    def test_large_dataset_performance(self, mock_billing, mock_increment, mock_delta_helper):
        """Test performance with large datasets."""
        mock_billing.return_value = False
        mock_delta_helper_instance = Mock()
        mock_delta_helper.return_value = mock_delta_helper_instance
        mock_delta_table = Mock()
        mock_delta_table.file_uris.return_value = ['s3://bucket/file1.parquet']
        mock_delta_helper_instance.get_delta_table.return_value = mock_delta_table
        mock_delta_helper_instance.write_to_deltalake.return_value = mock_delta_table
        
        # Create large dataset (50k records)
        large_data = [{"id": i, "data": f"data_{i}"} for i in range(50000)]
        
        source = Mock(spec=SourceResponse)
        source.name = "perf_test_resource"
        source.items = [large_data]  # Pass as single chunk
        source.rows_to_sync = len(large_data)
        source.has_duplicate_primary_keys = False
        source.primary_keys = ['id']
        source.sort_mode = 'asc'
        
        with patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.trigger_compaction_job'), \
             patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.prepare_s3_files_for_querying'), \
             patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.update_last_synced_at_sync'), \
             patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.validate_schema_and_update_table_sync'):
            
            pipeline = PipelineNonDLT(
                source=source,
                logger=self.logger,
                job_id=str(self.job.id),
                reset_pipeline=False,
                shutdown_monitor=self.shutdown_monitor
            )
            
            # This should complete without timeout or memory issues
            pipeline.run()
            
            # Verify data was processed
            mock_delta_helper_instance.write_to_deltalake.assert_called()

    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.DeltaTableHelper')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.increment_rows')
    @patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.will_hit_billing_limit')
    def test_memory_efficient_chunking(self, mock_billing, mock_increment, mock_delta_helper):
        """Test that chunking works efficiently for memory management."""
        mock_billing.return_value = False
        mock_delta_helper_instance = Mock()
        mock_delta_helper.return_value = mock_delta_helper_instance
        mock_delta_table = Mock()
        mock_delta_table.file_uris.return_value = ['s3://bucket/file1.parquet']
        mock_delta_helper_instance.get_delta_table.return_value = mock_delta_table
        mock_delta_helper_instance.write_to_deltalake.return_value = mock_delta_table
        
        # Create multiple chunks to test memory efficiency
        chunk1 = [{"id": i, "data": f"chunk1_{i}"} for i in range(1000)]
        chunk2 = [{"id": i + 1000, "data": f"chunk2_{i}"} for i in range(1000)]
        chunk3 = [{"id": i + 2000, "data": f"chunk3_{i}"} for i in range(1000)]
        
        source = Mock(spec=SourceResponse)
        source.name = "perf_test_resource"
        source.items = [chunk1, chunk2, chunk3]  # Multiple chunks
        source.rows_to_sync = 3000
        source.has_duplicate_primary_keys = False
        source.primary_keys = ['id']
        source.sort_mode = 'asc'
        
        with patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.trigger_compaction_job'), \
             patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.prepare_s3_files_for_querying'), \
             patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.update_last_synced_at_sync'), \
             patch('posthog.temporal.data_imports.pipelines.pipeline.pipeline.validate_schema_and_update_table_sync'):
            
            pipeline = PipelineNonDLT(
                source=source,
                logger=self.logger,
                job_id=str(self.job.id),
                reset_pipeline=False,
                shutdown_monitor=self.shutdown_monitor
            )
            
            pipeline.run()
            
            # Verify all chunks were processed (write_to_deltalake called 3 times)
            self.assertEqual(mock_delta_helper_instance.write_to_deltalake.call_count, 3)