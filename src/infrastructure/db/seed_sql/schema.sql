-- ============================================================================
-- IT Support Swarm — Supabase Schema
-- Run this in the Supabase SQL editor before starting the app.
-- ============================================================================

-- Enable pgvector for long-term memory semantic search
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================================
-- 1. tickets — Incident Tracking (Ticketing MCP)
-- ============================================================================
CREATE TABLE IF NOT EXISTS tickets (
    id              TEXT PRIMARY KEY,
    ticket_type     INTEGER NOT NULL,
    severity        TEXT NOT NULL DEFAULT 'low',
    status          TEXT NOT NULL DEFAULT 'open',
    title           TEXT,
    description     TEXT,
    employee_id     TEXT,
    assigned_to     TEXT,
    resolution      TEXT,
    runbook_used    TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tickets_status     ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_type       ON tickets(ticket_type);
CREATE INDEX IF NOT EXISTS idx_tickets_severity   ON tickets(severity);
CREATE INDEX IF NOT EXISTS idx_tickets_created_at ON tickets(created_at DESC);

-- ============================================================================
-- 2. server_metrics — Observability (Observability MCP)
-- ============================================================================
CREATE TABLE IF NOT EXISTS server_metrics (
    id               SERIAL PRIMARY KEY,
    server_id        TEXT NOT NULL,
    cpu_percent      FLOAT,
    memory_percent   FLOAT,
    disk_percent     FLOAT,
    network_in_mbps  FLOAT,
    network_out_mbps FLOAT,
    status           TEXT DEFAULT 'healthy',
    region           TEXT,
    recorded_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_metrics_server_id   ON server_metrics(server_id);
CREATE INDEX IF NOT EXISTS idx_metrics_recorded_at ON server_metrics(recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_metrics_status      ON server_metrics(status);

-- ============================================================================
-- 3. services — Service Registry (Observability MCP)
-- ============================================================================
CREATE TABLE IF NOT EXISTS services (
    service_id         TEXT PRIMARY KEY,
    service_name       TEXT NOT NULL,
    server_id          TEXT,
    status             TEXT DEFAULT 'healthy',
    error_rate_percent FLOAT DEFAULT 0.0,
    p99_latency_ms     INTEGER DEFAULT 0,
    last_checked       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_services_name   ON services(service_name);
CREATE INDEX IF NOT EXISTS idx_services_status ON services(status);
CREATE INDEX IF NOT EXISTS idx_services_server ON services(server_id);

-- ============================================================================
-- 4. incident_history — Past Resolutions + Action Audit Log
-- ============================================================================
CREATE TABLE IF NOT EXISTS incident_history (
    id          SERIAL PRIMARY KEY,
    ticket_id   TEXT,
    server_id   TEXT,
    service_id  TEXT,
    ticket_type INTEGER,
    description TEXT,
    root_cause  TEXT,
    resolution  TEXT,
    action      TEXT,
    target      TEXT,
    result      TEXT,
    is_mock     BOOLEAN DEFAULT TRUE,
    executed_at TIMESTAMPTZ,
    occurred_at TIMESTAMPTZ DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_history_ticket_id ON incident_history(ticket_id);
CREATE INDEX IF NOT EXISTS idx_history_server_id ON incident_history(server_id);
CREATE INDEX IF NOT EXISTS idx_history_type      ON incident_history(ticket_type);
CREATE INDEX IF NOT EXISTS idx_history_occurred  ON incident_history(occurred_at DESC);

-- ============================================================================
-- 5. st_memory — Short-Term Memory (ring buffer per ticket)
-- ============================================================================
CREATE TABLE IF NOT EXISTS st_memory (
    id        SERIAL PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    role      TEXT NOT NULL,
    content   TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_st_ticket_id ON st_memory(ticket_id);
CREATE INDEX IF NOT EXISTS idx_st_timestamp ON st_memory(timestamp DESC);

-- ============================================================================
-- 6. lt_facts — Long-Term Memory (pgvector semantic facts)
-- ============================================================================
CREATE TABLE IF NOT EXISTS lt_facts (
    id         SERIAL PRIMARY KEY,
    fact       TEXT NOT NULL,
    embedding  VECTOR(1536),
    confidence FLOAT DEFAULT 0.8,
    category   TEXT DEFAULT 'pattern',
    source     TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_lt_category   ON lt_facts(category);
CREATE INDEX IF NOT EXISTS idx_lt_created_at ON lt_facts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_lt_embedding  ON lt_facts USING hnsw (embedding vector_cosine_ops);

-- ============================================================================
-- 7. episodes — Episodic Memory (full incident snapshots)
-- ============================================================================
CREATE TABLE IF NOT EXISTS episodes (
    id                      SERIAL PRIMARY KEY,
    session_id              TEXT UNIQUE NOT NULL,
    ticket_id               TEXT NOT NULL,
    ticket_type             INTEGER,
    resolution              TEXT,
    resolved                BOOLEAN DEFAULT FALSE,
    resolution_time_minutes INTEGER,
    turns                   JSONB DEFAULT '[]',
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_episodes_ticket_id  ON episodes(ticket_id);
CREATE INDEX IF NOT EXISTS idx_episodes_type       ON episodes(ticket_type);
CREATE INDEX IF NOT EXISTS idx_episodes_resolved   ON episodes(resolved);
CREATE INDEX IF NOT EXISTS idx_episodes_created_at ON episodes(created_at DESC);

-- ============================================================================
-- pgvector RPC for LT memory semantic search (called by lt_store.py)
-- ============================================================================
CREATE OR REPLACE FUNCTION match_lt_facts(
    query_embedding  VECTOR(1536),
    match_threshold  FLOAT,
    match_count      INT
)
RETURNS TABLE (
    fact       TEXT,
    confidence FLOAT,
    category   TEXT,
    source     TEXT,
    created_at TEXT
)
LANGUAGE sql STABLE AS $$
    SELECT fact, confidence, category, source, created_at::TEXT
    FROM lt_facts
    WHERE 1 - (embedding <=> query_embedding) > match_threshold
    ORDER BY embedding <=> query_embedding
    LIMIT match_count;
$$;

-- ============================================================================
-- Seed mock data for development
-- ============================================================================
INSERT INTO server_metrics (server_id, cpu_percent, memory_percent, disk_percent, status, region)
VALUES
    ('prod-cache-01',  45.2, 98.7, 60.1, 'critical', 'eu-west-1'),
    ('prod-api-01',    72.1, 65.3, 45.0, 'degraded',  'eu-west-1'),
    ('prod-db-01',     30.5, 55.0, 80.2, 'healthy',   'eu-west-1'),
    ('prod-worker-01', 88.4, 70.1, 35.0, 'degraded',  'us-east-1'),
    ('staging-api-01', 20.0, 30.0, 25.0, 'healthy',   'eu-west-1')
ON CONFLICT DO NOTHING;

INSERT INTO services (service_id, service_name, server_id, status, error_rate_percent, p99_latency_ms)
VALUES
    ('svc-auth-01',     'auth-service',  'prod-api-01',    'degraded', 12.5, 4200),
    ('svc-checkout-01', 'checkout-api',  'prod-api-01',    'degraded',  5.2, 2800),
    ('svc-redis-01',    'redis',         'prod-cache-01',  'down',     100.0,    0),
    ('svc-postgres-01', 'postgres',      'prod-db-01',     'healthy',    0.1,  120),
    ('svc-worker-01',   'task-worker',   'prod-worker-01', 'degraded',   8.3, 1500)
ON CONFLICT DO NOTHING;