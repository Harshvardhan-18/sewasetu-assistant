# SewaSetu Assistant — Architecture & Design Note

## 1. Decision: One Unified Submission Path
All pension applications—whether initiated via the Flask web portal or via the citizen Model Context Protocol (MCP) assistant—must execute through a single domain service: `apply_for_pension_core()`.
- **Why**: The Director mandated that the assistant must never grant a pension that the counter or paper form would reject. By sharing a single Python transaction path, statutory age validation ($\ge 60$), mobile uniqueness, IFSC format, and document requirements are enforced in one immutable location.
- **Cost**: Refactoring monolithic Flask handlers into domain services while preserving full backwards compatibility with legacy tests.
- **Evidence**: Parity verified between web and MCP paths; applications appear immediately across both channels.

## 2. Decision: Compact Tool Surface for Small Models
The citizen surface is restricted to 10 outcome-oriented tools, and the officer surface to 7 tools, using structured five-part server prompts (*Use when*, *Do not use when*, *Side effects*, *Ask person first*).
- **Why**: Evaluators test assistants with small models and tight turn budgets. Sprawling CRUD tools trigger hallucinations and context exhaustion.
- **Token Measurements**: Citizen MCP prompt surface is **1,963 tokens**; Officer MCP is **1,396 tokens** (measured via `cl100k_base`, well below the 3,500 token ceiling).
- **Evaluation Proxy**: Evals ran on free-tier open models on Groq via the official MCP Python SDK client loop—not Claude Code—because no Anthropic API key was configured in the environment. These open models act as an open-standard proxy for small-model tool execution.

## 3. Decision: Structural Role Separation & Edge Security
We deployed two independent services: Citizen MCP (`/citizen/mcp`) and Officer MCP (`/officer/mcp`) with separate OAuth scopes (`citizen`, `officer`) and audiences.
- **Why**: A citizen assistant must never have access to administrative tools. Identity is derived strictly from cryptographic JWT `sub` claims (`citizen:<mobile>` or `officer:<username>`). No citizen tool accepts a phone number as an input argument.
- **Cost**: Maintaining two FastAPI processes and configuring Caddy reverse-proxy rules to block public access to `/internal/*`.
- **Evidence**: Deterministic security tests S1–S7 passed completely. Persona evaluation runs are recorded as "not run" until full valid execution completes under daily quota limits.

## 4. What I Would Do Differently
I would implement a deterministic local replay cache or lightweight local model adapter earlier in development. Running automated multi-turn evaluations against free-tier cloud APIs hit a strict organizational ceiling of 200,000 Tokens Per Day (TPD), triggering HTTP 429 rate limits. Decoupling test infrastructure from external API quotas would have enabled faster end-to-end iteration.

## 5. Portal Risks Noticed (Left Intact per Brief)
1. **Unchecked Legacy Queue**: Pre-existing applications pending over 15 days are deemed approved by the statutory scheduler job without verifying document legibility.
2. **Bank Account Validation**: Account numbers are checked only for length (9–18 digits) without national branch clearinghouse validation.
3. **Unauthenticated Public Status**: The legacy `/status` web portal allows lookups by phone number without OTP authentication, an exposure resolved in the MCP interface by requiring signed OAuth tokens.
