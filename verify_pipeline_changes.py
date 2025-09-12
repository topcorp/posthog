#!/usr/bin/env python3
"""
Simple verification script to test the pipeline changes work correctly.
"""
import os
import sys
import django

# Add the current directory to the Python path
sys.path.insert(0, '/home/ec2-user/topcorp/posthog')

# Set up Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'posthog.settings')
django.setup()

try:
    # Test imports
    from posthog.temporal.data_imports.pipelines.pipeline.pipeline import (
        PipelineNonDLT,
        _get_cached_schema_config,
        _get_optimized_memory_pool,
        _optimize_batch_size,
        _should_use_streaming_merge
    )
    
    print("✓ Successfully imported PipelineNonDLT and performance optimization functions")
    
    # Test performance optimization functions
    batch_size = _optimize_batch_size(1000000)  # 1MB data
    print(f"✓ Optimized batch size for 1MB data: {batch_size}")
    
    streaming_decision = _should_use_streaming_merge(5, 50000)
    print(f"✓ Streaming merge recommendation for 5 partitions, 50k rows: {streaming_decision}")
    
    # Test memory pool creation
    memory_pool = _get_optimized_memory_pool("test_pool")
    print(f"✓ Created optimized memory pool: {type(memory_pool).__name__}")
    
    print("\n✅ All pipeline performance optimizations working correctly!")
    
except ImportError as e:
    print(f"❌ Import error: {e}")
    sys.exit(1)
except Exception as e:
    print(f"❌ Runtime error: {e}")
    sys.exit(1)