# ResolutionAgent — IT Support Swarm

An agentic IT support and incident-resolution platform that transforms a 45-minute manual triage process into a sub-5-minute automated resolution pipeline. Built with LangGraph, FastAPI, Supabase, Qdrant, LiveKit, and deployed on Azure Container Apps.

---

LiveUrl = https://resolutionagent-api.kindsea-4ab9fc28.southeastasia.azurecontainerapps.io/

## Architecture Overview

```
Browser / Voice Client
        │
        ▼
┌─────────────────────────────────────────────────────┐
│                  FastAPI Server                      │
│  /chat  /chat/stream  /voice/token  /health         │
└──────────────┬──────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────┐
│              Decision Graph (LangGraph)              │
│                                                      │
│   Guardrail → CAG Cache Check                       │
│        │              │                              │
│    [blocked]      [cache hit]    [cache miss]        │
│        │              │               │              │
│       END          Return         Inner Swarm        │
└───────────────────────┬─────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│              Agent Swarm (LangGraph)                 │
│                                                      │
│  L1 Triage → L2 Investigator → L3 Resolver         │
│      │              │               │                │
│  Classify       Query metrics   Retrieve runbook     │
│  Create ticket  Search Qdrant   Execute mock action  │
│  (Ticketing MCP)(Observability  (Action MCP)         │
│                    MCP)         Write-back to memory │
└─────────────────────────────────────────────────────┘
```

---

## Two-Layer Pipeline

### Outer Layer — Decision Graph
Every request passes through the decision graph before anything else:

1. **Guardrail node** — classifies the message as a valid IT support request or off-topic input. Off-topic requests are blocked before reaching the swarm.
2. **CAG router** — checks the Qdrant `cag_cache` collection for a semantically similar cached answer (cosine similarity ≥ 0.45). Cache hits return in under 1 second and bypass the swarm entirely.
3. **Follow-up node** — if the request has an existing `ticket_id`, short-term memory is loaded and a conversational reply is generated without re-triaging.
4. **Swarm** — cache misses with no existing ticket go to the full L1→L2→L3 swarm.

### Inner Layer — Agent Swarm
Three sequential LangGraph nodes share a single `AgentState` TypedDict:

- **L1 Triage** — classifies ticket type (1-4) and severity (low/medium/critical) using the configured Groq small model. Creates a ticket record via the Ticketing MCP.
- **L2 Investigator** — queries live server metrics from Supabase via the Observability MCP. Searches Qdrant `procedural_memory` (1,037 distilled SOPs) for similar past incidents. Synthesises findings using `llama-3.3-70b-versatile`.
- **L3 Resolver** — retrieves the relevant runbook from Qdrant via CRAG (Corrective RAG with a 0.60 validation threshold). Returns a JSON-structured response separating the user-facing message from the internal action call. Executes mock remediation via the Action MCP and writes back to memory and the CAG cache.

---

## Voice Pipeline

```
Browser mic → LiveKit Room → Deepgram STT (Nova-3)
                                    │
                          LangGraphLLMAdapter
                                    │
                    AgentOrchestrator.achat_stream_fast()
                      (Groq llama-3.3-70b, direct stream)
                                    │
                          Deepgram TTS (aura-asteria-en)
                                    │
                    LiveKit Room → Browser speaker
                                    │
                    Data channel (topic="transcript")
                                    │
                         Transcript displayed in chat UI
```

The voice button connects the browser to a LiveKit room. A separate voice worker process handles the full pipeline: Deepgram Nova-3 transcribes speech in real time, the LangGraph adapter calls `achat_stream_fast()` on the orchestrator, and Deepgram TTS converts the answer to audio played back through the browser speaker. Transcripts for both user and agent turns are published over the LiveKit data channel and displayed in the chat window.

This means:

- Voice worker must be running alongside the API server (`python -m voice.run start`)
- Requires Chrome or Edge with microphone permission granted
- Uses a fast-path direct Groq LLM stream — not the full L1→L2→L3 swarm — to keep first-token latency under 2 seconds
- Short-term memory context (last 6 turns) is loaded before each voice turn for follow-up support
- Silero VAD detects barge-in — partial responses are saved to memory tagged `[interrupted]`
- Latency metrics (first_token_ms, agent_total_ms) published to the UI via LiveKit data channel

---

## 4-Tier Memory Architecture

| Tier | Storage | Purpose | Lifetime |
|---|---|---|---|
| Short-Term | Supabase `st_memory` | Ring buffer, last 20 turns per ticket | Cleared on ticket close |
| Long-Term | Supabase pgvector | Durable facts extracted by LLM Distiller | Permanent, user-scoped |
| Episodic | Supabase `episodes` | Full incident snapshots, audit trail | Permanent, user-scoped |
| Procedural | Qdrant `procedural_memory` | 1,037 distilled SOPs from historical transcripts | Rebuilt by ingest pipeline |

All memory tiers are scoped to `user_id`. The Recaller assembles context from all four tiers within a 1,500-token budget before each L2/L3 call.

---

## MCP Servers

Three MCP servers expose IT operations tools:

- **Ticketing MCP** — CRUD on the `tickets` table. L1 creates tickets, L3 closes them with resolution text and runbook reference.
- **Observability MCP** — queries `server_metrics` and `services` tables. Extracts server names from ticket text via regex and fetches relevant metrics for L2.
- **Action MCP** — executes mock remediation actions (restart, scale, flush cache, rotate credentials). All actions are logged to `incident_history` with `ticket_id` and `user_id`. All results are prefixed `[MOCK]`.

---

## Qdrant Collections

| Collection | Points | Dimensions | Distance | Purpose |
|---|---|---|---|---|
| `procedural_memory` | 1,037 | 1,536 | Cosine | Distilled SOPs from 1,050 historical transcripts |
| `cag_cache` | 7 | 1,536 | Cosine | Pre-built FAQ answers for high-frequency ticket types |
| `runbooks` | 58 | 1,536 | Cosine | Operational runbooks for L3 CRAG retrieval |

---

## Model Routing

| Task | Model | Reason |
|---|---|---|
| Guardrail, triage, distillation | `openai/gpt-oss-20b` (Groq) | Fast, low-cost model |
| Investigation, resolution | `llama-3.3-70b-versatile` (Groq) | Higher reasoning quality |
| Voice fast-path | `llama-3.3-70b-versatile` (Groq) | Streaming, sub-2s latency |
| Embeddings | `text-embedding-3-small` (OpenRouter) | 1,536 dims, consistent across all collections |

---

## Ticket Types

| Type | Category | Example |
|---|---|---|
| T1 | Access & Identity | VPN reset, MFA failure, account lockout |
| T2 | Asset Provisioning | Broken hardware, new device request |
| T3 | Service Degradation | Slow API, high latency, partial outage |
| T4 | Critical Outage | Database down, Redis OOM, auth service failure |

---

## What It Does

- Processes support requests through a multi-agent resolution flow.
- Stores and retrieves ticket history, memory, and incident data.
- Uses Qdrant for procedural memory, CAG cache, and runbook retrieval.
- Serves a lightweight web UI from the FastAPI app root.
- Exposes a voice token endpoint for browser-based LiveKit clients.
- Publishes live latency telemetry to the browser UI via LiveKit data channel.

---

## Requirements

- Python 3.10 or newer
- Supabase project and service key
- Qdrant Cloud instance
- Azure Blob Storage connection string
- API keys for the configured LLM providers
- Optional: Langfuse, LiveKit, Deepgram, ElevenLabs depending on the features you enable

---

## Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Create your environment file:

```bash
copy .env.example .env
```

3. Fill in the required values in `.env`:

```
SUPABASE_DB_URL
SUPABASE_URL
SUPABASE_SERVICE_KEY
QDRANT_URL
QDRANT_API_KEY
OPENROUTER_API_KEY
GROQ_API_KEY
OPENAI_API_KEY
AZURE_STORAGE_CONNECTION_STRING
LANGFUSE_SECRET_KEY
LANGFUSE_PUBLIC_KEY
LIVEKIT_URL
LIVEKIT_API_KEY
LIVEKIT_API_SECRET
DEEPGRAM_API_KEY
```

4. Review `config/param.yaml` for model, retrieval, memory, API, logging, and voice settings.

---

## Run Locally

Start the API server:

```bash
python api/run.py
```

or:

```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
```

Start the voice worker in a separate terminal (required for 🎙️ voice calls):

```bash
cd src
python -m voice.run start
```

Then open:

- `http://localhost:8000/` for the UI
- `http://localhost:8000/docs` for the Swagger UI

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Health check for Supabase, Qdrant, and Azure Blob |
| `POST` | `/chat` | Process a ticket synchronously through the full decision graph |
| `POST` | `/chat/stream` | Stream ticket processing progress as SSE |
| `GET` | `/tickets` | List tickets with optional filters |
| `GET` | `/tickets/{ticket_id}` | Fetch a single ticket |
| `GET` | `/tickets/stats` | Grouped ticket counts by type, severity, and status |
| `POST` | `/voice/token` | Issue a LiveKit JWT for browser voice clients |

---

## Docker

Build and run the API container:

```bash
docker build -f Dockerfile.api -t resolutionagent-api .
docker run --env-file .env -p 8000:8000 resolutionagent-api
```

Build and run the voice container:

```bash
docker build -f Dockerfile.voice -t resolutionagent-voice .
docker run --env-file .env resolutionagent-voice
```

---

## Production Deployment

The system is deployed on Azure using two Container Apps in the `resolutionagent-env` environment:

- `resolutionagent-api` — FastAPI server with external ingress on port 8000
- `resolutionagent-voice` — LiveKit voice worker with no ingress (outbound-only)

All secrets are stored in Azure Key Vault (`resolution-agent-kv`) and injected as environment variables at deploy time. No secrets appear in source code or Docker images.

CI/CD is handled by GitHub Actions (`.github/workflows/deploy.yml`). Every push to `main` builds both Docker images, pushes them to Azure Container Registry, pulls secrets from Key Vault, and deploys both Container Apps automatically.

---

## Repository Layout

```
ResolutionAgent_FinalHackathon/
├── api/                  FastAPI app, routers, schemas, static UI
├── config/               Runtime configuration (param.yaml, models.yaml)
├── data/                 Transcripts, distilled outputs, SQL data
├── runbooks/             Operational runbooks indexed in Qdrant
├── scripts/              Ingestion, distillation, and utility scripts
├── src/
│   ├── agents/           LangGraph decision graph, swarm nodes, orchestrator
│   ├── infastructure/    Config, DB clients, LLM factory, observability
│   ├── memory/           ST/LT/episodic/procedural stores and ops
│   ├── mcp_servers/      Ticketing, Observability, and Action MCP servers
│   ├── services/         CAG cache service
│   └── voice/            LiveKit agent, adapter, STT, TTS, pipeline
├── Dockerfile.api
├── Dockerfile.voice
├── requirements.txt
└── requirements.deploy.txt
```

---

## Notes

- The app reads most behavioural tuning from `config/param.yaml`.
- Startup checks Supabase, Qdrant, and Azure Blob connections and logs degraded services.
- The `infastructure/` folder spelling is intentional — all imports use this spelling consistently.
- Voice input uses LiveKit + Deepgram STT for real-time transcription. The voice worker (`src/voice/`) must be running separately for voice calls to work.
- Voice uses a fast-path (direct Groq LLM stream) rather than the full swarm to maintain sub-2s perceived latency. The text path runs the full L1→L2→L3 pipeline.
- Barge-in is supported via Silero VAD. Interrupted responses are saved to memory tagged `[interrupted]`.
