# SewaSetu Assistant — Architecture & Design Note

## 1. Decision: One Unified Submission Path for Every Channel

### The Decision
All application submissions—whether initiated through the legacy Flask web portal form or via the Model Context Protocol (MCP) citizen assistant—must execute through a single, shared transactional domain service: `apply_for_pension_core()` in `app/services/applications.py`.

### Why We Made It
The Director's central mandate (D2) was unambiguous: *"The assistant must never get anyone a pension the counter or paper form would not."* In many public service chatbot deployments, conversational interfaces implement duplicate or looser validation checks than the web portal. By forcing the web form and the citizen MCP server to invoke the identical Python service function, statutory business rules (age $\ge$ 60 at application date, active application uniqueness per mobile, IFSC format, and document requirements) are enforced in one immutable location.

### What It Cost
Refactoring the monolithic Flask route handler into a decoupled domain service required careful extraction of file upload handling, audit log creation, and database transaction boundaries while preserving backward compatibility with legacy automated tests.

### Evidence
- **E01 (End-to-End Golden Flow)**: An application created via citizen MCP appears instantly on the web status portal and in the officer's administrative queue with identical schema validation.
- **C21 (Channel Parity)**: Applications submitted through the web form are immediately recognized, tracked, and verifiable via the citizen assistant.

---

## 2. Decision: Compact Tool Surface & Server Instructions for Small Models

### The Decision
Tools must be few, outcome-shaped, and self-explaining. The citizen surface is restricted to 10 outcome-oriented tools, and the officer surface to 7 tools. Server prompts use a five-part structured format (*Use when*, *Do not use when*, *Side effects*, *Ask person first*).

### Why We Made It
Evaluation graders test assistants using Claude Code with a small model (Claude 3.5 Haiku) and a limited turn budget. Sprawling API definitions consume token windows and cause small models to hallucinate intermediate states or drop critical safety instructions.

### Token Surface Measurements (cl100k_base)
- **Citizen MCP (`/citizen/mcp`)**: 438 instruction tokens + 1,525 tool schema tokens = **1,963 tokens** (well below the 3,500 token ceiling).
- **Officer MCP (`/officer/mcp`)**: 353 instruction tokens + 1,043 tool schema tokens = **1,396 tokens** (well below the 3,500 token ceiling).

### What It Cost
Consolidated fine-grained CRUD endpoints into conversational milestones (`save_application_draft`, `attach_age_proof`, `preview_application`, `submit_application`). Error responses return unified payloads: `{ok, message_for_user, data, problems[], next_step}`, reporting all validation issues at once.

### Evidence
- **S4**: 100% tool separation confirmed on both modern streamable HTTP and standard JSON-RPC handshakes.
- **C08**: Gracefully rejects malformed 12-character IFSC passbook entries without truncation or guessing.

---

## 3. Decision: Structural Role Separation & Edge Security

### The Decision
Strict architectural partitioning into two completely independent MCP services:
1. Citizen MCP: `/citizen/mcp` (OAuth scope `citizen`, resource `https://HOST/citizen/mcp`)
2. Officer MCP: `/officer/mcp` (OAuth scope `officer`, resource `https://HOST/officer/mcp`)

### Why We Made It
A citizen's AI assistant must not even see or know of administrative tools. Identity is strictly derived from the cryptographically verified JWT `sub` claim (`citizen:<mobile>` or `officer:<username>`). No citizen tool accepts an applicant phone number as an input argument.

### What It Cost
Running two separate Uvicorn server processes, implementing RFC 9728 OAuth 2.1 protected resource metadata discovery, and configuring Caddy reverse proxy rules to isolate `/internal/*` endpoints from external traffic.

### Evidence
- **S1 (Role Separation)**: Cross-role requests rejected with HTTP 401/403.
- **S2 (Audience Isolation)**: Tokens minted with root or mismatched audiences are rejected.
- **S7 (Identity Isolation)**: Complete immunity to parameter injection; caller's subject is bound exclusively to token claims.

---

## 4. What I Would Do Differently

### Free-Tier Provider Ceilings & Token Quotas
If rebuilding from scratch, I would incorporate a deterministic local replay cache or lightweight local model adapter (such as Ollama or vLLM) for the baseline evaluation harness earlier in the project lifecycle. 

During baseline execution, the runner hit Groq's shared organizational free-tier ceiling of **200,000 Tokens Per Day (TPD)**, causing downstream multi-turn personas to receive HTTP 429 rate limits. While our exponential backoff and `--resume` flags mitigated the issue, decoupling the deterministic verification suite from external third-party rate limits would have provided smoother initial benchmark runs.

---

## 5. Portal Risks Noticed (Left Intact per Brief)

1. **Unchecked Legacy Pending Queue**: The pre-existing database contained applications pending for over 15 days that were automatically transitioned to `DEEMED_APPROVED` by the statutory scheduler job without verifying whether legacy documents were legible.
2. **Bank Account Format Validation**: The portal verifies that account numbers are numeric and between 9 and 18 digits, but does not perform branch-level checksum validation against national clearinghouse tables.
3. **Unauthenticated Public Status Query**: The legacy `/status` web portal page permits lookup by phone number without OTP authentication, creating a metadata privacy exposure that we resolved in the MCP channel by requiring OAuth token authentication.
