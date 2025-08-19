# PostHog Engineering Knowledge Graph Analysis

## Repository Overview

**Repository**: PostHog (https://github.com/posthog/posthog.git)
**Type**: Multi-service analytics platform with microservices architecture
**Primary Languages**: Python, TypeScript, JavaScript, Rust, Node.js
**Primary Frameworks**: Django, React, Next.js, Axum (Rust), Tokio (Rust)

## Architecture Summary

PostHog is a complex multi-service platform for product analytics, consisting of:
- Django-based backend API and web interface
- React-based frontend application
- Multiple Rust microservices for high-performance data ingestion
- Node.js plugin server for extensibility
- Event streaming and data processing pipeline

## Services Identified

### Core Application Services

1. **web_service** (Python/Django)
   - Main application backend and frontend serving
   - Port: 8000
   - Framework: Django + Django REST Framework
   - Command: `./bin/start-backend & ./bin/start-frontend`
   - Interfaces: REST API endpoints (extensive router structure)

2. **capture_service** (Rust)
   - High-performance event capture service
   - Port: 3000 (internal), 3307 (dev routing)
   - Mode: Events capture
   - Binary: Built from rust/capture

3. **replay_capture_service** (Rust) 
   - Session replay data capture service
   - Port: 3000 (internal), 3306 (dev routing)
   - Mode: Recording capture
   - Binary: Built from rust/capture (same binary, different mode)

4. **feature_flags_service** (Rust)
   - Feature flag evaluation service
   - Port: 3001
   - Binary: Built from rust/feature-flags

5. **plugins_service** (Node.js/TypeScript)
   - Plugin execution and webhook handling
   - Port: 6738
   - Framework: Custom Node.js server
   - Command: `./bin/plugin-server --no-restart-loop`

6. **property_defs_service** (Rust)
   - Property definitions service
   - Binary: Built from rust/property-defs-rs

### Infrastructure Services

7. **livestream_service**
   - Real-time data streaming service
   - Port: 8080 (internal), 8666 (dev)
   - Framework: Go-based service

8. **temporal_django_worker** (Python)
   - Temporal workflow worker for Django
   - Command: `./bin/temporal-django-worker`

9. **cyclotron_janitor** (Rust)
   - Background job cleanup service
   - Binary: Built from rust/cyclotron-janitor

### Support Services

10. **proxy_service** (Caddy)
    - Reverse proxy and load balancer
    - Routes traffic to appropriate services
    - Port: 8000 (external)

11. **worker_service** (Python/Celery)
    - Background task processing
    - Command: `./bin/docker-worker-celery --with-scheduler`

12. **flower_service** (Python)
    - Celery task monitoring
    - Port: 5555

## Resources Identified

### Databases

1. **postgres_db** (PostgreSQL 12)
   - Primary application database
   - Port: 5432
   - Provider: self-managed (containerized)
   - Database: posthog

2. **counters_db** (PostgreSQL 12)
   - Separate database for counters
   - Port: 5433 (dev), 5432 (internal)
   - Provider: self-managed (containerized)
   - Database: counters

3. **clickhouse_db** (ClickHouse 25.3.6.56)
   - Analytics and time-series data storage
   - Ports: 8123 (HTTP), 9000 (native), 9440 (secure)
   - Provider: self-managed (containerized)

### Caching & Message Queues

4. **redis_cache** (Redis 6.2.7)
   - Primary caching layer
   - Port: 6379
   - Provider: self-managed (containerized)

5. **redis7_cache** (Redis 7.2)
   - Secondary Redis instance
   - Port: 6479 (dev), 6379 (internal)
   - Provider: self-managed (containerized)

6. **kafka_queue** (RedPanda/Kafka compatible)
   - Event streaming and message queue
   - Ports: 9092 (internal), 19092 (external)
   - Provider: self-managed (RedPanda container)

7. **zookeeper_coordinator**
   - Coordination service for Kafka
   - Port: 2181
   - Provider: self-managed (containerized)

### Object Storage & Workflow

8. **object_storage** (MinIO)
   - S3-compatible object storage
   - Ports: 19000 (API), 19001 (console)
   - Provider: self-managed (MinIO container)

9. **temporal_workflow** (Temporal)
   - Workflow orchestration engine
   - Port: 7233
   - Provider: self-managed (containerized)

10. **elasticsearch_search**
    - Search and indexing for Temporal
    - Port: 9200
    - Provider: self-managed (containerized)

### Monitoring & Observability

11. **jaeger_tracing**
    - Distributed tracing
    - Port: 16686 (UI)
    - Provider: self-managed (containerized)

12. **otel_collector**
    - OpenTelemetry collector
    - Ports: 4317 (gRPC), 4318 (HTTP)
    - Provider: self-managed (containerized)

## Build System & Package Management

### Monorepo Structure (pnpm workspace)
- Root package manager: pnpm 9.15.5
- Python dependency management: uv/pip with pyproject.toml
- Rust workspace: Cargo workspace with 29 crates
- Multiple product packages under products/*

### Build Tools

1. **turbo_build** (Turbo)
   - Monorepo build system
   - Version: 2.4.2
   - Purpose: Coordinating builds across packages

2. **esbuild_bundler**
   - JavaScript/TypeScript bundling
   - Custom build configuration in build.mjs

3. **cargo_build**
   - Rust compilation system
   - Workspace configuration in rust/Cargo.toml

4. **django_collectstatic**
   - Static asset collection for Django
   - Integrated with frontend build process

## CI/CD Pipelines

### GitHub Actions Workflows

1. **backend_ci** (ci-backend.yml)
   - Triggers: push to master, pull requests
   - Jobs: migration checks, Django tests (40 parallel groups), async migrations
   - Testing: pytest with ClickHouse, PostgreSQL, Redis, Kafka
   - Matrix: Python 3.11.9, ClickHouse 25.3.6.56

2. **frontend_ci** (ci-frontend.yml)
   - Triggers: push to master, pull requests
   - Jobs: formatting, toolbar checks, TypeScript checks, Jest tests
   - Testing: Jest with React Testing Library
   - Matrix: FOSS/EE segments, 3 parallel chunks

3. **container_images_cd** (container-images-cd.yml)
   - Triggers: push to master
   - Builds and pushes Docker images
   - Platforms: linux/arm64, linux/amd64
   - Registries: DockerHub, GHCR, AWS ECR
   - Auto-deployment to PostHog Cloud

### Other CI Workflows
- Rust CI (ci-rust.yml)
- Node.js CI (ci-nodejs.yml)  
- E2E Playwright tests (ci-e2e-playwright.yml)
- Security scanning (ci-security.yaml)
- Storybook deployment (storybook-deploy.yml)

## Key Dependencies & Relationships

### Service Dependencies

```
proxy_service → routes to → web_service, capture_service, replay_capture_service, feature_flags_service, plugins_service
web_service → uses → postgres_db, redis_cache, clickhouse_db
capture_service → uses → kafka_queue, redis_cache
replay_capture_service → uses → kafka_queue
feature_flags_service → uses → postgres_db, redis_cache
plugins_service → uses → postgres_db, clickhouse_db, kafka_queue, redis_cache
worker_service → uses → postgres_db, redis_cache, clickhouse_db
temporal_django_worker → uses → temporal_workflow, postgres_db
cyclotron_janitor → uses → postgres_db, kafka_queue
kafka_queue → uses → zookeeper_coordinator
clickhouse_db → uses → kafka_queue, zookeeper_coordinator
temporal_workflow → uses → postgres_db, elasticsearch_search
```

### Build Dependencies

```
posthog_repo → builds via frontend_build → frontend_artifacts
posthog_repo → builds via backend_build → web_service_image
posthog_repo → builds via rust_build → capture_service_image, feature_flags_service_image, etc.
posthog_repo → builds via plugin_server_build → plugins_service_image
```

### Deployment Flow

```
code_changes → github_actions_ci → docker_builds → container_registry → posthog_cloud_deployment
```

## API Interface Structure

PostHog exposes a comprehensive REST API through Django REST Framework with the following key endpoint categories:

- **Projects/Environments**: `/api/projects/`, `/api/environments/`
- **Analytics**: `/api/environments/{id}/events/`, `/api/environments/{id}/insights/`
- **Feature Management**: `/api/projects/{id}/feature_flags/`, `/api/projects/{id}/experiments/`
- **Session Data**: `/api/environments/{id}/session_recordings/`, `/api/environments/{id}/heatmaps/`
- **Data Pipeline**: `/api/environments/{id}/batch_exports/`, `/api/environments/{id}/hog_functions/`
- **User Management**: `/api/organizations/`, `/api/users/`

## Technology Stack Summary

- **Backend**: Python 3.11, Django 4.2, Django REST Framework
- **Frontend**: React 18, TypeScript 5.2, Tailwind CSS
- **High-Performance Services**: Rust (Axum, Tokio)
- **Plugin System**: Node.js 22, TypeScript
- **Databases**: PostgreSQL 12, ClickHouse 25.3, Redis 6.2/7.2
- **Message Queue**: Kafka (RedPanda)
- **Workflow**: Temporal
- **Monitoring**: Jaeger, OpenTelemetry, Prometheus
- **Build Tools**: pnpm, uv, Cargo, Turbo, esbuild
- **CI/CD**: GitHub Actions
- **Containerization**: Docker, Docker Compose

## Key Architectural Patterns

1. **Microservices Architecture**: Separate services for different concerns (capture, feature flags, plugins)
2. **Event-Driven Architecture**: Kafka for event streaming and service communication
3. **CQRS Pattern**: Separate read/write paths with ClickHouse for analytics queries
4. **API Gateway Pattern**: Caddy proxy routing requests to appropriate services
5. **Monorepo**: Single repository with multiple packages and services
6. **Polyglot Programming**: Python for business logic, Rust for performance, Node.js for plugins
7. **Temporal Workflows**: Complex business processes managed through Temporal
8. **Container-First**: All services containerized with Docker Compose for local development