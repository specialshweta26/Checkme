# Architecture diagrams

Mermaid diagrams for Checkme. Pair with [HLD](HLD.md) and [LLD](LLD.md). GitHub and most Markdown previews render these in place.

## 1. System context

Who talks to Checkme. The browser never calls Gmail, Graph, or OpenAI directly.

```mermaid
flowchart LR
  User["User"] -->|"HTTPS + session cookie"| Web["Checkme web<br/>Next.js PWA"]
  Web -->|"same-origin /api/*"| API["Checkme API<br/>FastAPI"]
  API --> Google["Google<br/>OAuth, Gmail, Calendar, Meet"]
  API --> Microsoft["Microsoft<br/>OAuth, Graph mail, calendar, Teams"]
  API --> OpenAI["OpenAI<br/>chat + embeddings"]
  API --> MCP["User MCP servers"]
```

## 2. Containers

Processes and stores at runtime.

```mermaid
flowchart TB
  subgraph Client
    PWA["apps/web<br/>Chat, Inbox, Connectors, Tasks"]
  end

  subgraph API["apps/api"]
    HTTP["HTTP routers"]
    Agent["Deep agent"]
    InboxW["Inbox worker 8s"]
    Sched["Task scheduler 20s"]
    Runtime["Connector runtime"]
  end

  subgraph Data
    PG[(Postgres + pgvector)]
    Redis[(Redis)]
    Files[(Disk or MinIO)]
  end

  PWA -->|"cookie proxy"| HTTP
  HTTP --> Agent
  HTTP --> Runtime
  Agent --> Runtime
  InboxW --> Runtime
  Sched --> Agent
  HTTP --> PG
  HTTP --> Redis
  HTTP --> Files
  Agent --> PG
  InboxW --> PG
  Runtime --> Gmail["Gmail + Calendar"]
  Runtime --> Graph["Microsoft Graph"]
  Runtime --> MCP["MCP HTTP"]
  Agent --> OpenAI["OpenAI"]
```

## 3. Local vs Render

```mermaid
flowchart TB
  subgraph Local["local branch — Docker Compose"]
    LWeb["web :3000"] --> LApi["api :8001"]
    LApi --> LPg["postgres :5432"]
    LApi --> LRedis["redis :6379"]
    LApi --> LMinio["MinIO :9000"]
  end

  subgraph Render["main branch — Render"]
    RWeb["checkme-web"] -->|"API_HOSTPORT"| RApi["checkme-api"]
    RApi --> RPg["checkme-postgres"]
    RApi --> RRedis["checkme-redis"]
    RApi --> RDisk["disk /var/data/attachments"]
  end
```

## 4. Request path

```mermaid
sequenceDiagram
  participant B as Browser
  participant N as Next.js
  participant A as FastAPI
  B->>N: fetch /api/... (cookie)
  alt POST /api/chat
    N->>A: apps/web/app/api/chat/route.ts
  else GET /api/inbox/events
    N->>A: SSE proxy maxDuration 300
  else other /api/*
    N->>A: [...path] catch-all proxy
  end
  A-->>B: JSON / SSE / file bytes
```

## 5. Sign-in

```mermaid
sequenceDiagram
  participant B as Browser
  participant A as FastAPI
  participant IdP as Google or Microsoft
  B->>A: GET /api/auth/{provider}/login
  A-->>B: 302 + PKCE state cookie
  B->>IdP: authorize
  IdP-->>B: callback?code
  B->>A: GET /api/auth/{provider}/callback
  A->>IdP: exchange code + verifier
  A->>A: upsert User, Account, encrypt refresh
  A->>A: ensure builtin gmail/outlook + index tools
  A-->>B: session JWT cookie, 302 /chat
```

## 6. Chat agent

The model never receives the full MCP catalog. It uses a small harness, then `call_tool`.

```mermaid
sequenceDiagram
  participant U as User
  participant UI as Chat UI
  participant A as FastAPI
  participant M as OpenAI
  participant C as Gmail/Outlook/MCP
  U->>UI: message + optional files
  UI->>A: POST /api/chat
  A->>M: deep agent + harness tools
  loop until answer
    M->>A: list_connectors / search_tools / get_tool_schema
    M->>A: call_tool(slug, name, args)
    alt destructive, not confirmed
      A-->>UI: pending_confirms
      U->>UI: Confirm
      UI->>A: POST /api/chat confirmations
    else mail open/summarize
      A->>C: search then get_thread
      A->>C: summarize_attachments if files
    else normal
      A->>C: named tool
    end
    A-->>M: compact result or result_id
  end
  A-->>UI: text, attachments, mail_threads
  UI-->>U: answer + See full email
```

```mermaid
flowchart TD
  Q[User query] --> L[list_connectors]
  L --> S[search_tools]
  S --> G[get_tool_schema]
  G --> C[call_tool]
  C --> D{destructive?}
  D -->|yes| P[Confirm in UI]
  P --> C
  D -->|no| R[RuntimeConnector]
  R --> H{mail open?}
  H -->|yes| T[hydrate get_thread]
  T --> Z[store results, compact]
  H -->|no| Z
  Z --> Big{payload too large?}
  Big -->|yes| I[inspect_result]
  I --> A[model writes answer]
  Big -->|no| A
```

## 7. Outlook / Gmail full thread

```mermaid
flowchart TD
  Hit["search/list hit"] --> Ids["thread_id and/or message_id"]
  Ids --> Resolve["_resolve_conversation"]
  Resolve --> Msg{"message_id or AAMk id?"}
  Msg -->|yes| GET["GET message → conversationId"]
  Msg -->|subject-like| Search["search then GET"]
  Msg -->|AAQk / Gmail threadId| CID["use as conversation id"]
  GET --> Page["_messages_by_conversation"]
  Search --> Page
  CID --> Page
  Page --> F["filter conversationId, paginate"]
  F --> Few{"count ≤ 1?"}
  Few -->|yes| Folders["inbox + sent + deleted + archive"]
  Few -->|no| Out["messages oldest-first"]
  Folders --> Out
  Out --> Quote{"one message?"}
  Quote -->|yes| Keep["keep quoted history"]
  Quote -->|no| Clip["clip each body"]
```

## 8. Inbox realtime

```mermaid
flowchart LR
  subgraph Background
    W[Worker 8s] --> List[list_messages + cc:me]
    List --> Upsert[upsert inbox_items + role]
    Upsert --> Trim[newest 40 pending]
    Trim --> LLM[get_thread + classify]
  end
  subgraph Foreground
    SSE["/inbox/events 2s"] --> Bell[AppShell bell]
    SSE --> Cards[Inbox cards]
    Badge["/inbox/badge 3s fallback"] --> Bell
    Badge --> Cards
  end
  Trim --> SSE
  Trim --> Badge
```

## 9. Files

```mermaid
sequenceDiagram
  participant U as User
  participant UI as Chat or Inbox
  participant A as FastAPI
  participant S as Disk or MinIO
  participant P as Gmail/Graph
  U->>UI: attach files
  UI->>A: POST /api/files/upload
  A->>S: put_bytes
  A-->>UI: keys + preview URLs
  U->>UI: Send / reply
  UI->>A: attachment_keys
  A->>S: get_bytes at send time
  A->>P: send_message / reply with files
```

## 10. Scheduler

```mermaid
sequenceDiagram
  participant U as User in chat
  participant A as FastAPI
  participant DB as Postgres
  participant Sch as Scheduler 20s
  U->>A: "email this in 20 minutes"
  A->>DB: insert scheduled_tasks
  loop every 20s
    Sch->>DB: due active rows, limit 6
    Sch->>A: run_deep_agent auto_confirm
    A->>DB: last_result, next_run_at
  end
```

## 11. Data model

```mermaid
erDiagram
  User ||--o{ Account : has
  User ||--o{ AuthSession : has
  User ||--o{ Connector : has
  User ||--o{ ChatSession : has
  User ||--o{ InboxItem : has
  User ||--o{ ScheduledTask : has
  User ||--o{ WorkspaceAgent : has
  WorkspaceAgent ||--o{ ScheduledTask : owns
  WorkspaceAgent ||--o{ MemoryBlock : owns
  Account ||--o| Connector : builtin
  Connector }o--o| ServerCard : server_key
  ServerCard ||--o{ ToolIndex : catalogs
  ChatSession ||--o{ ChatMessage : contains
```

## 12. Security boundaries

```mermaid
flowchart TB
  Browser["Browser<br/>no provider tokens"]
  Next["Next.js<br/>cookie proxy only"]
  API["FastAPI<br/>Fernet refresh tokens<br/>session JWT"]
  IdP["Google / Microsoft"]
  LLM["OpenAI<br/>text only, no raw bytes"]
  Store["Object storage<br/>signed URLs"]

  Browser --> Next --> API
  API --> IdP
  API --> LLM
  API --> Store
```

## 13. To, Cc, and Bcc

Mail on **Cc** (and **Bcc** when the mailbox still delivers it) is ingested, notified, triaged, and briefed. Send can add extra people.

```mermaid
flowchart TD
  Inbox["list_messages folder=inbox"] --> Merge["merge_message_lists with cc:me"]
  Merge --> Role["recipient_role to / cc / bcc"]
  Role --> Card["inbox_items + bell badge"]
  Role --> Staff["today_briefing: CC'd to you"]
  Card --> UI["Inbox card: Cc'd to you"]
  UI --> Send["POST /inbox/id/send"]
  Chat["Chat send_message / reply"] --> Confirm["confirm preview to, cc, bcc, reply_all"]
  Send --> Reply["reply cc, bcc, reply_all"]
  Confirm --> Reply
  Reply --> Gmail["Gmail compose Cc/Bcc"]
  Reply --> Graph["Graph reply or createReply + PATCH"]
```
