# ARCHITECTURE.md

# Media Intelligence Platform (MVP)

## Software Architecture Document

**Version:** 1.0
**Status:** Approved for MVP Development

---

# 1. Architecture Principles

The MVP is designed following these principles:

* Keep the architecture as simple as possible.
* Build only what is required for the MVP.
* Make every component replaceable.
* Prefer managed AWS services whenever they reduce operational complexity.
* Design for future scalability without introducing unnecessary complexity today.

Guiding principle:

> Build a simple system that can evolve, instead of a complex system that hopes to become simple.

---

# 2. High-Level Architecture

```
                   Internet
                       │
                  Cloudflare
                       │
                    Nginx
                       │
             Docker Compose (EC2)
 ┌──────────────────────────────────────────┐
 │                                          │
 │  Next.js Frontend                        │
 │  FastAPI Backend                         │
 │                                          │
 │  Ingestion Worker                        │
 │  AI Analysis Worker                      │
 │  Report Worker                           │
 │  Notification Worker                     │
 │                                          │
 │  PostgreSQL                              │
 └──────────────────────────────────────────┘
             │
     ┌───────┼─────────────┐
     │       │             │
     ▼       ▼             ▼
 Amazon S3  Amazon SQS   EC2 Spot GPU
                          (Transcription)
```

---

# 3. Architectural Style

## Modular Monolith

The application will be developed as a Modular Monolith.

Each business capability will be isolated in its own module.

Examples:

* Authentication
* Media Sources
* Recording Management
* Transcription
* AI Analysis
* Editorial Review
* Reports
* Clients
* Notifications

This approach provides:

* Low operational complexity
* Fast development
* Easy testing
* Easy future migration to microservices if necessary

---

# 4. Architecture Pattern

The backend follows:

* Hexagonal Architecture (Ports & Adapters)
* Clean Architecture principles

Dependencies always point inward.

```
API
 ↓
Application
 ↓
Domain
 ↑
Infrastructure
```

Business rules never depend on infrastructure.

---

# 5. Technology Stack

## Backend

* Python 3.12+
* FastAPI
* SQLAlchemy
* Alembic
* Pydantic

---

## Frontend

* Next.js
* React
* TypeScript

---

## Database

* PostgreSQL 17+
* pgvector enabled
* PostgreSQL Full Text Search

---

## Storage

Amazon S3

Stores:

* Audio recordings
* Generated reports
* Attachments
* Temporary files

---

## Queue

Amazon SQS

Queues include:

* transcription_jobs
* segmentation_jobs
* analysis_jobs
* report_jobs
* notification_jobs

Dead Letter Queues (DLQ) should be enabled.

---

# 6. Transcription Architecture

Transcription is completely decoupled from the application.

The platform interacts only through a Transcription Provider interface.

```
Application
      │
      ▼
Transcription Provider
      │
      ├── Faster Whisper API
      ├── EC2 Spot GPU
      ├── Mac Mini MLX
      ├── OpenAI
      └── Future providers
```

Initial implementation:

* EC2 Spot GPU
* Created on demand
* Processes queued jobs
* Saves transcripts
* Terminates automatically

This minimizes GPU costs.

---

# 7. Processing Pipeline

```
New Recording
      │
      ▼
Amazon S3
      │
      ▼
Ingestion Worker
      │
      ▼
Amazon SQS
      │
      ▼
Transcription Worker
      │
      ▼
Segmentation Worker
      │
      ▼
AI Analysis Worker
      │
      ▼
Editorial Queue
      │
      ▼
Reports
```

Every stage is asynchronous.

---

# 8. Authentication

Authentication method:

* JWT Access Token
* Refresh Token
* RBAC (Role-Based Access Control)

User information is stored in PostgreSQL.

External identity providers are intentionally excluded from the MVP.

---

# 9. Deployment

Infrastructure:

* Ubuntu EC2
* Docker Compose
* Nginx Reverse Proxy

Deployment process:

```
git pull

docker compose build

docker compose up -d
```

Deployment is manual during the MVP.

---

# 10. Components Excluded from MVP

The following technologies are intentionally excluded:

* Redis
* Kubernetes
* ECS/Fargate
* RabbitMQ
* Kafka
* OpenSearch
* Auth0
* Amazon Cognito
* Prometheus
* Grafana
* ELK Stack

They can be introduced later if justified by real operational needs.

---

# 11. Observability

The MVP includes:

* Structured JSON logs
* Global exception handling
* Health endpoint

```
GET /health
```

Every asynchronous job should include a unique `job_id` for traceability across workers.

---

# 12. Recommended Project Structure

```
src/
│
├── modules/
│   ├── auth/
│   ├── clients/
│   ├── media/
│   ├── recordings/
│   ├── transcription/
│   ├── ai/
│   ├── editorial/
│   ├── reports/
│   └── notifications/
│
├── shared/
│
├── infrastructure/
│
├── api/
│
└── main.py
```

---

# 13. Design Principles

* Single Responsibility Principle
* Dependency Injection
* Interface-based design
* Event-driven processing using SQS
* Stateless API
* Immutable processing jobs
* Infrastructure isolated from business logic

---

# 14. Future Evolution

Potential future improvements include:

* Amazon ECS/Fargate
* Kubernetes
* OpenSearch
* Redis
* SSO (Azure AD, Google Workspace)
* Multi-region deployment
* Auto-scaling workers
* CI/CD with GitHub Actions
* Real-time dashboards
* Advanced semantic search using pgvector

---

# 15. Architecture Decision Summary

| Decision       | Selected                    |
| -------------- | --------------------------- |
| Architecture   | Modular Monolith            |
| Pattern        | Hexagonal Architecture      |
| Backend        | Python + FastAPI            |
| Frontend       | Next.js                     |
| Database       | PostgreSQL                  |
| Vector Search  | pgvector                    |
| Storage        | Amazon S3                   |
| Queue          | Amazon SQS                  |
| Transcription  | EC2 Spot GPU                |
| Deployment     | Docker Compose on EC2       |
| Reverse Proxy  | Nginx                       |
| Authentication | JWT                         |
| Search         | PostgreSQL Full Text Search |
| Monitoring     | JSON Logs + /health         |
| CI/CD          | Manual deployment           |
| Redis          | Not included in MVP         |
| Kubernetes     | Not included in MVP         |

---

# 16. Final Philosophy

The objective of the MVP is to validate the product quickly while maintaining a clean architecture that can grow over time.

The platform prioritizes:

* Simplicity over unnecessary complexity.
* Low operational cost.
* Clear separation of concerns.
* Replaceable infrastructure.
* Incremental scalability based on real customer demand.

This architecture provides a solid foundation for evolving from an MVP into an enterprise-grade media intelligence platform without requiring a complete redesign.
