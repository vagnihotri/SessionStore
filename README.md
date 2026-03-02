# Session Management Service with Aerospike

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Architecture](#2-architecture)
3. [Core Components](#3-core-components)
4. [Session Lifecycle](#4-session-lifecycle)
5. [API Reference](#5-api-reference)
6. [Data Model](#6-data-model)
7. [Deployment](#7-deployment)
8. [Configuration Reference](#8-configuration-reference)
9. [Using This as a Base for a Production Session Service](#9-using-this-as-a-base-for-a-production-session-service)
10. [Scaling Strategies: Maintaining p90 < 10ms](#10-scaling-strategies-maintaining-p90--10ms)

---

## 1. Project Overview

This project is a **session management service** built with [FastAPI](https://fastapi.tiangolo.com/) and [Aerospike](https://aerospike.com/), a distributed, high-performance NoSQL database purpose-built for real-time workloads at internet scale.

The service uses the [starsessions](https://github.com/alex-oleshkevich/starsessions) middleware for cookie-based session handling, with a **custom `AerospikeSessionStore`** backend that replaces the default in-memory store. It enforces a **one-session-per-user** policy: when a user logs in from a new browser or device, any previous session is automatically invalidated.

### Why Aerospike for Session Management?

| Property | Aerospike Advantage |
|---|---|
| **Latency** | Sub-millisecond reads/writes for in-memory namespaces; single-digit ms for SSD-backed |
| **Throughput** | Millions of TPS per cluster on commodity hardware |
| **TTL-based expiry** | Native record-level TTL with background namespace supervisor (nsup) — no application-side garbage collection |
| **Horizontal scaling** | Automatic data rebalancing via Smart Partitions™; add nodes with zero downtime |
| **Strong consistency** | Optional strong-consistency mode (SC) per namespace since Aerospike 4.0 |
| **Cross-datacenter replication** | Built-in XDR (Cross-Datacenter Replication) for geo-distributed session stores |

### Key Features

- **One-session-per-user enforcement** — prevents session sprawl and supports "log out everywhere" semantics
- **Cookie-based sessions** via starsessions middleware (HMAC-signed, configurable lifetime)
- **Dual interface** — HTML login/logout UI for browser clients, plus JSON REST API for programmatic session management
- **Dockerized** — single `docker compose up` brings up both Aerospike and the FastAPI app
- **TTL-driven expiry** — session records expire automatically via Aerospike's namespace-level `default-ttl`, eliminating the need for application-side session reaping

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Client (Browser / API)                   │
│                                                                 │
│   Cookie: session_id=<hex>       POST /api/sessions {username}  │
└───────────────┬──────────────────────────────┬──────────────────┘
                │                              │
                ▼                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      FastAPI Application                        │
│                                                                 │
│  ┌──────────────────────┐  ┌─────────────────────────────────┐  │
│  │  SessionMiddleware   │  │       Route Handlers            │  │
│  │  (starsessions)      │  │  GET /  POST /login  GET /logout│  │
│  │  SessionAutoload     │  │  POST /api/sessions             │  │
│  └──────────┬───────────┘  │  GET  /api/sessions/{username}  │  │
│             │              └──────────────┬──────────────────┘  │
│             ▼                             ▼                     │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │              AerospikeSessionStore                         │ │
│  │                                                            │ │
│  │  read()   write()   remove()   (SessionStore interface)    │ │
│  │  create_user_session()   get_user_session()  (extensions)  │ │
│  └─────────────────────────┬──────────────────────────────────┘ │
└────────────────────────────┼────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Aerospike Database                           │
│                                                                 │
│  Namespace: "test"                                              │
│  ┌──────────────────────┐  ┌──────────────────────────────────┐ │
│  │ Set: fastapi_sessions│  │ Set: user_sessions               │ │
│  │                      │  │                                  │ │
│  │ Key: session_id      │  │ Key: username                    │ │
│  │ Bin: data (bytes)    │  │ Bin: session_id (string)         │ │
│  │                      │  │                                  │ │
│  │ (session payload)    │  │ (username → session_id mapping)  │ │
│  └──────────────────────┘  └──────────────────────────────────┘ │
│                                                                 │
│  default-ttl: 86400s (24h)    nsup-period: 120s                 │
└─────────────────────────────────────────────────────────────────┘
```

### Request Flow

1. A client sends a request with (or without) a `session_id` cookie.
2. `SessionMiddleware` intercepts the request, extracts the session ID from the cookie, and calls `AerospikeSessionStore.read()` to load session data.
3. The route handler accesses `request.session` (a dict) to read/write session data.
4. On response, the middleware calls `AerospikeSessionStore.write()` to persist any changes.
5. The `write()` method enforces one-session-per-user by checking the `user_sessions` mapping set before writing.

---

## 3. Core Components

### 3.1 `aerospike_store.py` — AerospikeSessionStore

The heart of the project. Extends `starsessions.SessionStore` (from `starsessions.stores.base`) and implements:

#### SessionStore Interface Methods (required by starsessions)

| Method | Signature | Description |
|---|---|---|
| `read` | `async read(session_id, lifetime) -> bytes` | Retrieves serialized session data from Aerospike. Returns empty bytes if the record doesn't exist. |
| `write` | `async write(session_id, data, lifetime, ttl) -> str` | Persists session data. Extracts the username from the JSON payload and enforces the one-session-per-user invariant before writing. Returns the session_id. |
| `remove` | `async remove(session_id) -> None` | Deletes the session record and cleans up the corresponding user→session mapping. |

#### Extension Methods

| Method | Signature | Description |
|---|---|---|
| `create_user_session` | `create_user_session(username) -> dict` | Generates a new session ID (`secrets.token_hex(16)`), invalidates any previous session for the user, and writes the session directly to Aerospike. Returns `{"session_id": ..., "data": ...}`. |
| `get_user_session` | `get_user_session(username) -> dict \| None` | Looks up the user's active session via the `user_sessions` mapping and returns session data, ID, and remaining TTL. Returns `None` if no active session exists. |

#### Internal Helpers

| Method | Purpose |
|---|---|
| `_connect_with_retry` | Retries Aerospike connection on startup (configurable retries and delay) to handle Docker container startup ordering. |
| `_extract_username` | Parses the JSON-serialized session bytes to extract the `username` field. |
| `_invalidate_previous_session` | Looks up the old session_id for a username, removes the old session record, and updates the `user_sessions` mapping to point to the new session_id. |
| `_remove_user_mapping` | Conditionally removes the `user_sessions` entry only if it still points to the given session_id (prevents race conditions). |
| `_key` / `_user_key` | Constructs Aerospike key tuples for the session set and the user-mapping set respectively. |

### 3.2 `app.py` — FastAPI Application

Configures FastAPI with starsessions middleware, defines HTML routes for browser-based login/logout, and exposes JSON API endpoints for programmatic session management.

**Middleware stack:**
1. `SessionMiddleware` — cookie-based session management with the Aerospike store
2. `SessionAutoloadMiddleware` — automatically loads session data on every request

**User database:** Currently a hardcoded dict (`USERS_DB`). In production, replace with a real authentication provider.

---

## 4. Session Lifecycle

### Login Flow

```
User submits credentials (POST /login)
        │
        ▼
Validate against USERS_DB
        │
        ▼ (success)
store.create_user_session(username)
        │
        ├─► Look up user_sessions[username] → old_session_id
        │       │
        │       ▼ (if exists and different)
        │   DELETE fastapi_sessions[old_session_id]
        │
        ├─► PUT user_sessions[username] = new_session_id
        │
        └─► PUT fastapi_sessions[new_session_id] = {"username": ...}
        │
        ▼
Set request.session["username"] and request.session["aerospike_session_id"]
        │
        ▼
SessionMiddleware writes cookie: session_id=<hex>
        │
        ▼
Redirect to /  (302)
```

### Session Read (every request)

```
Incoming request with session_id cookie
        │
        ▼
SessionMiddleware extracts session_id
        │
        ▼
store.read(session_id, lifetime)
        │
        ├─► GET fastapi_sessions[session_id]
        │       │
        │       ▼
        │   Return bin "data" (bytes) or b""
        │
        ▼
request.session populated as dict
```

### Logout Flow

```
GET /logout
        │
        ▼
request.session.clear()
        │
        ▼
SessionMiddleware calls store.remove(session_id)
        │
        ├─► GET fastapi_sessions[session_id] → extract username
        │       │
        │       ▼
        │   DELETE user_sessions[username] (if still points to this session)
        │
        └─► DELETE fastapi_sessions[session_id]
        │
        ▼
Cookie cleared, redirect to /login
```

### Session Expiry

Records expire automatically when their age exceeds the Aerospike namespace's `default-ttl` (86400 seconds = 24 hours). The namespace supervisor (`nsup`) runs every `NSUP_PERIOD` (120 seconds) to evict expired records. No application code is required for session cleanup.

---

## 5. API Reference

### HTML Routes

| Method | Path | Description | Auth Required |
|---|---|---|---|
| `GET` | `/` | Home page. Redirects to `/login` if not authenticated. | Yes |
| `GET` | `/login` | Login form. Redirects to `/` if already authenticated. | No |
| `POST` | `/login` | Processes login. Form fields: `username`, `password`. | No |
| `GET` | `/logout` | Clears session and redirects to `/login`. | Yes |

### JSON API Endpoints

#### `POST /api/sessions`

Create a new session for a user. Invalidates any previous session.

**Request:**
```json
{
    "username": "alice"
}
```

**Response (201 Created):**
```json
{
    "session_id": "a1b2c3d4e5f6...",
    "data": {
        "username": "alice"
    }
}
```

#### `GET /api/sessions/{username}`

Retrieve the active session for a user.

**Response (200 OK):**
```json
{
    "session_id": "a1b2c3d4e5f6...",
    "data": {
        "username": "alice"
    },
    "ttl": 85231
}
```

**Response (404 Not Found):**
```json
{
    "detail": "No active session for user 'bob'"
}
```

---

## 6. Data Model

### Aerospike Namespace: `test`

#### Set: `fastapi_sessions`

| Field | Type | Description |
|---|---|---|
| **Key** | String | Session ID (32-char hex string from `secrets.token_hex(16)`) |
| **Bin: `data`** | Bytes | JSON-serialized session payload (e.g., `{"username": "alice"}`) |
| **TTL** | Integer | Managed by Aerospike namespace `default-ttl` (86400s) |

#### Set: `user_sessions`

| Field | Type | Description |
|---|---|---|
| **Key** | String | Username (e.g., `"alice"`) |
| **Bin: `session_id`** | String | The active session ID for this user |
| **TTL** | Integer | Managed by Aerospike namespace `default-ttl` (86400s) |

### Invariant

At any point in time, for each active user `U`:
```
user_sessions[U].session_id == S  ⟺  fastapi_sessions[S].data.username == U
```

This bidirectional mapping enables both session-to-user and user-to-session lookups.

---

## 7. Deployment

### Prerequisites

- Docker and Docker Compose

### Quick Start

```bash
cd /path/to/SessionStore
docker compose up --build
```

This starts:
- **Aerospike CE 8.1.1.1** on ports 3000–3002
- **FastAPI app** on port 8001

Visit `http://localhost:8001/login` and log in with `admin`/`secret` or `alice`/`password123`.

### Local Development (without Docker)

```bash
# Start Aerospike separately (e.g., via Docker)
docker run -d --name aerospike -p 3000:3000 aerospike:ce-8.1.1.1

# Install dependencies
pip install -r requirements.txt

# Run the app
uvicorn app:app --reload --port 8001
```

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `AEROSPIKE_HOST` | `127.0.0.1` | Aerospike server hostname |
| `AEROSPIKE_PORT` | `3000` | Aerospike server port |

---

## 8. Configuration Reference

### Aerospike Namespace Configuration (via docker-compose.yml)

| Parameter | Value | Description |
|---|---|---|
| `NAMESPACE` | `test` | Namespace name for session data |
| `DEFAULT_TTL` | `86400` | Record TTL in seconds (24 hours) |
| `NSUP_PERIOD` | `120` | Namespace supervisor cycle interval (seconds). Controls how frequently expired records are reaped. |

### FastAPI Session Configuration (in app.py)

| Parameter | Value | Description |
|---|---|---|
| `SESSION_LIFETIME` | `86400` | starsessions session lifetime (seconds) |
| `cookie_https_only` | `False` | Set to `True` in production with TLS |
| `cookie_name` | `session_id` | Name of the session cookie |

### AerospikeSessionStore Constructor

| Parameter | Default | Description |
|---|---|---|
| `hosts` | `[("127.0.0.1", 3000)]` | List of Aerospike seed nodes |
| `namespace` | `"test"` | Aerospike namespace |
| `set_name` | `"fastapi_sessions"` | Aerospike set for session records |
| `connect_retries` | `10` | Number of connection attempts on startup |
| `retry_delay` | `2.0` | Seconds between retry attempts |

---

## 9. Using This as a Base for a Production Session Service

This project is deliberately minimal to serve as a **foundation**. Below is a roadmap to transform it into a production-grade, high-performance session management service.

### 9.1 Authentication

Replace `USERS_DB` with a real identity provider:
- **Database-backed auth** (PostgreSQL/MySQL with hashed passwords via `bcrypt` or `argon2`)
- **OAuth 2.0 / OpenID Connect** (delegate auth to Google, Okta, Auth0, etc.)
- **LDAP / Active Directory** for enterprise environments

### 9.2 Session Security

- **Enable `cookie_https_only=True`** and enforce TLS termination (via a reverse proxy like Nginx, Envoy, or a cloud load balancer).
- **Set `cookie_samesite="lax"`** or `"strict"` to prevent CSRF.
- **Sign session IDs** — starsessions supports HMAC signing. Configure a strong secret key.
- **Rotate session IDs** on privilege escalation (login, password change) to prevent session fixation.

### 9.3 API Authentication

Protect the `/api/sessions` endpoints with API key authentication, JWT bearer tokens, or mutual TLS. Currently, they are unauthenticated, which is acceptable for internal services behind a firewall but unsuitable for public-facing deployments.

### 9.4 Async Aerospike Client

The current implementation uses the synchronous `aerospike` Python client inside `async` methods. For true non-blocking I/O at high concurrency:
- Use `asyncio.to_thread()` or `loop.run_in_executor()` to offload blocking Aerospike calls to a thread pool.
- Alternatively, evaluate the [aerospike-vector-search](https://pypi.org/project/aerospike-vector-search/) async client or community async wrappers as they mature.

### 9.5 Connection Pooling

The Aerospike Python client maintains an internal connection pool, but for multi-worker deployments:
- Each Uvicorn worker gets its own `AerospikeSessionStore` instance and connection.
- Tune `max_conns_per_node` in the Aerospike client config for high-concurrency workloads.

### 9.6 Observability

Add:
- **Structured logging** (e.g., `structlog` or `python-json-logger`)
- **Metrics** — export latency histograms and counters to Prometheus via `prometheus-fastapi-instrumentator`
- **Distributed tracing** — OpenTelemetry integration for end-to-end request tracing
- **Health checks** — add a `/health` endpoint that verifies Aerospike connectivity

### 9.7 Rate Limiting

Protect login and session-creation endpoints from brute-force attacks:
- Use `slowapi` or a reverse proxy rate limiter
- Implement progressive delays or account lockout after N failed attempts

### 9.8 Graceful Shutdown

The `lifespan` handler already calls `store.close()`. For production:
- Handle `SIGTERM` gracefully in Kubernetes/ECS environments
- Drain in-flight requests before closing connections

---

## 10. Scaling Strategies: Maintaining p90 < 10ms

Aerospike is uniquely suited for session management at scale because it was designed from the ground up for predictable, low-latency access to billions of keys. Below are concrete strategies for each scale tier, focused on maintaining **p90 response times in the single-digit milliseconds**.

### Understanding the Latency Budget

For an end-to-end session read/write to complete in under 10ms (p90), the breakdown is roughly:

| Component | Budget |
|---|---|
| Network hop (client → app) | 1–3ms (same region) |
| App processing (FastAPI + middleware) | 0.5–1ms |
| Aerospike read or write | 0.5–2ms (in-memory), 1–4ms (SSD) |
| Network hop (app → Aerospike) | 0.2–1ms (same AZ) |
| **Total** | **~3–7ms** |

The strategies below ensure each component stays within budget as load increases.

---

### Tier 1: 1,000 Users

**Profile:** Small internal tool, startup MVP, or dev/staging environment.

**Infrastructure:**
- 1 Aerospike node (single server or container)
- 1–2 FastAPI workers (Uvicorn)
- Single-region deployment

**Strategies:**
- **No special optimization needed.** The default configuration in this project handles 1K users trivially.
- Aerospike on a single node comfortably handles tens of thousands of TPS.
- In-memory namespace (`storage-engine memory`) for lowest latency, or the default `storage-engine device` with SSD is sufficient.
- Session data fits entirely in RAM at this scale (assuming ~1KB per session: 1,000 × 1KB = 1MB).

**Expected p90:** < 2ms

---

### Tier 2: 100,000 Users

**Profile:** Growing SaaS product, mid-size e-commerce site, corporate intranet.

**Infrastructure:**
- 1–2 Aerospike nodes (replication factor 2 for HA)
- 4–8 FastAPI workers behind a load balancer
- Single-region, multi-AZ deployment

**Strategies:**

1. **Use in-memory storage with persistence:**
   Configure the Aerospike namespace with `storage-engine memory` and enable persistence via `file` to survive restarts without paying SSD read latency on the hot path.
   ```
   namespace test {
       replication-factor 2
       memory-size 1G
       default-ttl 86400
       nsup-period 120
       storage-engine memory
   }
   ```
   At 1KB per session, 100K sessions = 100MB — comfortably fits in 1GB with room for indexes.

2. **Run multiple Uvicorn workers:**
   ```bash
   uvicorn app:app --workers 4 --host 0.0.0.0 --port 8001
   ```
   Each worker maintains its own Aerospike client connection. Distributes CPU load across cores.

3. **Wrap blocking calls with `asyncio.to_thread()`:**
   Prevents the synchronous Aerospike client from blocking the event loop under concurrent load.
   ```python
   async def read(self, session_id: str, lifetime: int) -> bytes:
       return await asyncio.to_thread(self._sync_read, session_id)
   ```

4. **Deploy behind a reverse proxy (Nginx/Envoy):**
   Handles TLS termination, connection pooling, and request buffering outside the Python process.

**Expected p90:** < 3ms

---

### Tier 3: 1,000,000 Users (1M)

**Profile:** Popular consumer app, large e-commerce platform, high-traffic media site.

**Infrastructure:**
- 3–4 Aerospike nodes (replication factor 2)
- 8–16 FastAPI instances (containerized, behind ALB/NLB)
- Single-region, multi-AZ, with warm standby in a second region

**Strategies:**

1. **Horizontal app scaling with Kubernetes:**
   Deploy FastAPI as a Kubernetes Deployment with a Horizontal Pod Autoscaler (HPA) based on CPU and request latency. Each pod maintains its own Aerospike client connection.
   ```yaml
   apiVersion: autoscaling/v2
   kind: HorizontalPodAutoscaler
   spec:
     minReplicas: 8
     maxReplicas: 32
     metrics:
       - type: Resource
         resource:
           name: cpu
           target:
             averageUtilization: 60
   ```

2. **Aerospike cluster sizing:**
   - 1M sessions × 1KB ≈ 1GB data. With replication factor 2: 2GB across the cluster.
   - 3 nodes, each with 2GB RAM allocated to the namespace, provides ample headroom.
   - Aerospike Smart Partitions (4096 partitions) distribute data evenly across nodes automatically.

3. **Tune Aerospike client policies:**
   ```python
   client = aerospike.client({
       "hosts": hosts,
       "policies": {
           "read": {"total_timeout": 10, "max_retries": 1},
           "write": {"total_timeout": 10, "max_retries": 1},
       }
   }).connect()
   ```
   Short timeouts prevent slow nodes from inflating tail latency. Failed reads return empty sessions gracefully.

4. **Connection tuning:**
   Set `max_conns_per_node` to match your concurrency level (e.g., 64–128 per Uvicorn worker).

5. **Batch session validation:**
   For internal services that need to validate multiple sessions, use Aerospike's batch read API to validate many session IDs in a single network round-trip.

**Expected p90:** < 5ms

---

### Tier 4: 10,000,000 Users (10M)

**Profile:** Large-scale consumer platform, gaming service, financial trading platform.

**Infrastructure:**
- 5–8 Aerospike nodes (NVMe SSDs for cost-efficient capacity, with all indexes in RAM)
- 32–64 FastAPI instances across multiple AZs
- Multi-region active-passive with Aerospike XDR

**Strategies:**

1. **Hybrid storage (index in RAM, data on SSD):**
   At 10M sessions, pure in-memory becomes expensive. Aerospike's `storage-engine device` keeps the primary index in RAM (~64 bytes/record) and data on NVMe SSDs.
   ```
   namespace test {
       replication-factor 2
       memory-size 4G          # for primary index
       default-ttl 86400
       nsup-period 120
       storage-engine device {
           file /opt/aerospike/data/test.dat
           filesize 20G
           data-in-memory false  # reads from SSD
       }
   }
   ```
   - Primary index: 10M records × 64 bytes = 640MB (fits in 4GB with headroom)
   - Data: 10M × 1KB = 10GB (well within 20GB filesize)
   - NVMe SSD reads: ~100μs per read — still sub-millisecond

2. **Enable `data-in-memory true` for latency-critical namespaces:**
   If budget permits, `data-in-memory true` keeps data cached in RAM while persisting to SSD. This gives in-memory read performance with SSD durability.

3. **Aerospike Rack Awareness:**
   Configure rack-aware replication to ensure replicas are in different availability zones, preventing data loss from AZ failures while keeping read latency low by preferring local replicas.
   ```
   namespace test {
       ...
       rack-id 1  # Set per node, matching AZ
       prefer-uniform-balance true
   }
   ```

4. **Application-level session caching:**
   Add a thin in-process LRU cache (e.g., `cachetools.TTLCache`) for recently-read sessions to avoid hitting Aerospike on every request for the same session within a short window (e.g., 5 seconds).
   ```python
   from cachetools import TTLCache
   _cache = TTLCache(maxsize=50_000, ttl=5)

   async def read(self, session_id, lifetime):
       if session_id in _cache:
           return _cache[session_id]
       data = await asyncio.to_thread(self._sync_read, session_id)
       _cache[session_id] = data
       return data
   ```
   This is safe because session data is write-rarely, read-often. A 5-second stale window is acceptable for most session use cases.

5. **Cross-datacenter replication (XDR) for disaster recovery:**
   Configure Aerospike XDR to asynchronously replicate session data to a standby cluster in another region. Failover becomes a DNS switch with minimal session loss.

**Expected p90:** < 5ms (with data-in-memory), < 8ms (SSD-only)

---

### Tier 5: 100,000,000 Users (100M)

**Profile:** Global social network, large-scale messaging platform, major e-commerce marketplace.

**Infrastructure:**
- 20–40 Aerospike nodes (per region), NVMe SSDs
- 100–200+ FastAPI instances (multi-region)
- Active-active multi-region with Aerospike XDR

**Strategies:**

1. **Multi-region active-active deployment:**
   Run independent Aerospike clusters in each major region (e.g., US-East, EU-West, AP-Southeast) with XDR replicating session data bi-directionally. Route users to the nearest region via GeoDNS or Anycast.
   ```
   ┌──────────────┐     XDR      ┌──────────────┐
   │  US-East     │◄────────────►│  EU-West     │
   │  20 AS nodes │              │  20 AS nodes │
   │  80 app pods │              │  60 app pods │
   └──────────────┘              └──────────────┘
          ▲           XDR              ▲
          └────────────┬───────────────┘
                       ▼
                ┌──────────────┐
                │ AP-Southeast │
                │  15 AS nodes │
                │  40 app pods │
                └──────────────┘
   ```
   **Latency benefit:** Users always hit a local cluster (1–3ms network hop instead of 100–200ms cross-region).

2. **Partition-aware client routing:**
   The Aerospike client is partition-aware — it routes requests directly to the node that owns the data partition, eliminating intra-cluster proxy hops. At 100M records, this is critical: no single node is a bottleneck.

3. **All-flash namespace configuration:**
   ```
   namespace test {
       replication-factor 2
       memory-size 50G             # primary index
       default-ttl 86400
       nsup-period 60              # more aggressive eviction
       storage-engine device {
           device /dev/nvme0n1     # raw NVMe device
           device /dev/nvme1n1     # striped for throughput
           write-block-size 128K
           data-in-memory false
       }
       index-type flash {          # optional: spill index to flash
           mount /mnt/pmem0
           mounts-budget 20G
       }
   }
   ```
   - Primary index: 100M × 64 bytes = 6.4GB (fits in 50GB RAM)
   - Data: 100M × 1KB = 100GB (striped across NVMe devices)

4. **Separate read and write paths in the application:**
   - Reads go to the nearest Aerospike node using `policy.replica = PREFER_MASTER` or `SEQUENCE`.
   - Writes go to the master with strong consistency if needed.
   - This distributes read load across replicas and reduces master hot-spotting.

5. **Dedicated Uvicorn worker pools:**
   Separate deployments for HTML routes (browser traffic, bursty) and API routes (service-to-service, steady). This prevents browser-traffic spikes from starving internal session validation calls.

6. **Kernel and OS tuning:**
   - Set NVMe I/O scheduler to `none` (bypass unnecessary scheduling for NVMe)
   - Increase `net.core.somaxconn` and `net.ipv4.tcp_max_syn_backlog`
   - Disable swap entirely
   - Pin Aerospike to dedicated CPU cores via `cpuset` or `taskset`

**Expected p90:** < 5ms (per-region)

---

### Tier 6: 1,000,000,000 Users (1B)

**Profile:** Global-scale identity platform (akin to session management for Google, Facebook, or WeChat).

**Infrastructure:**
- 100–200+ Aerospike nodes per region, across 5–8 regions
- 500–1000+ FastAPI instances (globally)
- Active-active-active multi-region mesh
- Dedicated network fabric, bare-metal or large VM instances

**Strategies:**

1. **Aerospike All-Flash with index on flash (Aerospike 7+):**
   At 1B records, the primary index alone is 64GB per node (assuming even distribution across 100 nodes: 10M records/node × 64 bytes = 640MB — but with replication and growth buffer, much more). Use Aerospike's **index-on-flash** feature to spill the primary index to Persistent Memory (PMem) or NVMe, keeping only a fraction in DRAM.
   ```
   namespace test {
       replication-factor 2
       partition-tree-sprigs 1M
       memory-size 16G
       default-ttl 86400
       nsup-period 30              # aggressive eviction at this scale
       storage-engine device {
           device /dev/nvme0n1
           device /dev/nvme1n1
           device /dev/nvme2n1
           device /dev/nvme3n1
           write-block-size 128K
           post-write-queue 1024
           data-in-memory false
       }
       index-type flash {
           mount /mnt/nvme4
           mount /mnt/nvme5
           mounts-budget 80G
       }
   }
   ```

2. **Shard by user geography:**
   Rather than replicating all sessions everywhere, shard users by geography. A US user's session lives in the US cluster; an EU user's session in the EU cluster. Cross-region lookup is only needed for roaming users (rare).
   ```
   User login → GeoIP lookup → Route to regional cluster
                                      │
                                      ▼
                              Regional Aerospike Cluster
                              (owns this user's session)
   ```
   This reduces cross-region replication volume from O(N) to O(roaming users) — typically <1% of traffic.

3. **Hot-key mitigation:**
   At 1B scale, certain users (power users, bots, API integrators) may generate disproportionate session reads. Strategies:
   - **Application-side LRU cache** (as described in Tier 4) absorbs repeat reads.
   - **Aerospike rack-aware reads** distribute reads across replicas.
   - **Rate limiting** per session_id at the edge/CDN layer prevents abuse.

4. **Tiered session storage:**
   Not all sessions are equal. Implement session tiers:
   - **Hot tier (in-memory):** Active sessions (accessed in last 5 minutes) — Aerospike in-memory namespace
   - **Warm tier (NVMe):** Idle sessions (not accessed in 5–60 minutes) — Aerospike all-flash namespace
   - **Cold tier (archive):** Expired sessions for audit — offload to S3/GCS via Aerospike Connect
   ```
   Active user request → Check hot tier (in-memory, <1ms)
                                │
                                ▼ (miss)
                         Check warm tier (NVMe, <3ms)
                                │
                                ▼ (miss)
                         Session expired / not found
   ```

5. **Dedicated infrastructure per concern:**
   | Component | Dedicated Infrastructure |
   |---|---|
   | Session reads | Aerospike replicas, partition-aware routing |
   | Session writes | Aerospike masters, strong-consistency mode |
   | User→session mapping | Separate Aerospike namespace or set with higher replication factor |
   | Session creation (login) | Separate FastAPI deployment with burst scaling |
   | Session validation (middleware) | Collocated with app servers, with local cache |

6. **Protocol optimization:**
   - Use **Aerospike's binary wire protocol** directly (the Python client already does this).
   - Consider switching the app layer to a compiled language (Go, Rust) for the session proxy/gateway to reduce per-request CPU overhead. Python app servers sit behind this gateway and handle business logic.
   - Use **gRPC or Unix domain sockets** between the app layer and session proxy to minimize serialization overhead.

7. **Network topology:**
   - Place Aerospike nodes and app servers in the **same rack/availability zone** within each region.
   - Use **25Gbps+ network interfaces** with DPDK or io_uring for kernel-bypass networking.
   - Enable Aerospike's **fabric compression** for inter-node replication traffic.

8. **Monitoring and SLO enforcement:**
   At this scale, p90 < 10ms is an SLO that requires continuous enforcement:
   - **Real-time latency dashboards** with Aerospike's built-in histogram logging and Prometheus integration
   - **Automated alerts** when p90 crosses 7ms (early warning before SLO breach)
   - **Chaos engineering** (kill nodes, simulate network partitions) to validate failover latency
   - **Canary deployments** for any Aerospike config or client version changes

**Expected p90:** < 8ms (per-region, with flash index), < 3ms (hot-tier cache hit)

---

### Scaling Summary Table

| Scale | Aerospike Nodes | App Instances | Storage | Data Size | Key Strategies | Expected p90 |
|---|---|---|---|---|---|---|
| **1K** | 1 | 1–2 | Memory | ~1MB | Default config, no tuning needed | < 2ms |
| **100K** | 1–2 | 4–8 | Memory | ~100MB | `asyncio.to_thread`, multiple workers, replication | < 3ms |
| **1M** | 3–4 | 8–16 | Memory / SSD | ~1GB | Kubernetes HPA, client policy tuning, batch reads | < 5ms |
| **10M** | 5–8 | 32–64 | NVMe SSD | ~10GB | App-level caching, rack awareness, XDR warm standby | < 5–8ms |
| **100M** | 20–40/region | 100–200 | NVMe SSD | ~100GB | Multi-region active-active, geo-routing, flash index | < 5ms/region |
| **1B** | 100–200/region | 500–1000 | NVMe + PMem | ~1TB | Geo-sharding, tiered storage, protocol optimization, SLO automation | < 8ms/region |

### Universal Principles (All Tiers)

1. **Keep session payloads small.** Store only identifiers (user_id, role) in the session; fetch full user profiles from a separate service. Smaller records = faster I/O, less memory, less replication traffic.

2. **Measure before optimizing.** Use Aerospike's built-in latency histograms (`asinfo -v 'latencies:'`) and application-level metrics to identify the actual bottleneck before adding complexity.

3. **Prefer reads from replicas.** Session data is read-heavy (100:1 read/write ratio is typical). Configure the client to prefer the nearest replica to spread load.

4. **Avoid cross-datacenter reads in the hot path.** Route users to the nearest regional cluster. Use XDR for async replication, not synchronous cross-region reads.

5. **Fail open for session reads.** If Aerospike is temporarily unreachable, treat it as "no session" and redirect to login rather than returning a 500 error. Sessions are recoverable state.

6. **Automate capacity planning.** Monitor records-per-node, memory usage, and disk usage. Set alerts at 70% capacity and scale out proactively — Aerospike rebalances automatically when new nodes join.

---

## Appendix: Project File Structure

```
fastapi_login_app/
├── aerospike_store.py      # Custom AerospikeSessionStore (core session logic)
├── app.py                  # FastAPI application (routes, middleware, config)
├── docker-compose.yml      # Aerospike + FastAPI service definitions
├── Dockerfile              # Python 3.12-slim, uvicorn entrypoint
├── requirements.txt        # fastapi, uvicorn, starsessions, aerospike, python-multipart
├── .dockerignore            # Excludes __pycache__, .venv, .idea
└── DOCUMENTATION.md        # This file
```