# Loom Presentation Script (3:00 — Face + Screen)

**Video Title**: SewaSetu Old Age Pension AI Assistant — Architecture, Evals, & Handover  
**Presenter**: Harshvardhan (New Digital Fellow)  
**Target Duration**: Exactly 3 minutes (03:00)  
**Live URL**: `https://sewasetu-hv.koreacentral.cloudapp.azure.com/eval-dashboard`  

---

### [0:00 – 0:20] Hook & Headline
**Screen**: Public Eval Dashboard (`/eval-dashboard`) showing Headline Cards, followed by split-screen or quick clip of the citizen flow.  
**Spoken Audio**:
> "Welcome Director. You asked us to enable any AI assistant to help senior citizens in Sonari block apply for and track their Old Age Pension—built on open standards with the portal as the single system of record.
> 
> Here on our live Azure deployment, the assistant operates over two official Model Context Protocol servers. A grandson can sit with his grandmother at a kiosk, apply through the citizen assistant, have the block officer review and approve it on the officer assistant, and see the exact records mirrored instantly across the portal database. Here is what we built, what we measured, and what we learned."

---

### [0:20 – 1:10] Architecture & Key Design Decisions
**Screen**: System architecture diagram (`deploy/Caddyfile`, `citizen_mcp/server.py`, `officer_mcp/server.py`, shared service layer).  
**Spoken Audio**:
> "Three architectural decisions anchor the system:
> 
> First, **strict role separation**. We run two distinct MCP servers with separate OAuth scopes and audiences. A citizen assistant connects to `/citizen/mcp` and physically cannot see officer tools; an officer connects to `/officer/mcp`. Caller identity is cryptographically bound to the token subject—no tool ever accepts an applicant phone number as an argument.
> 
> Second, **one unified submission path**. Whether submitted via the web form or the AI assistant, all applications call `apply_for_pension_core()`. The assistant can never grant a pension the counter would not.
> 
> Third, **tool surfaces engineered for small models**. We kept the citizen prompt surface to 1,963 tokens—well under our 3,500 token ceiling. Tools report all validation problems at once, and we enforce explicit kiosk teardown via `end_my_session` to protect elders sharing phones."

---

### [1:10 – 2:20] Personas, The Counter Hour, & Eval Iteration
**Screen**: Scroll to Persona Catalog on the Dashboard (`docs/COUNTER_HOUR.md` open in background). Click into C16, C02, and O3.  
**Spoken Audio**:
> "Before writing code, we spent an hour imagining the Sonari counter. We derived 19 starter personas and 3 held-out cases from real village interactions:
> - **C16 (Pranjal & Dhaneswari)**: A grandson applying for his 72-year-old grandmother at a kiosk, requiring immediate session teardown so the next person in queue cannot see her data.
> - **C02 (Under-Age Elder)**: An elder turning 60 in eight days; the assistant accurately computes his milestone date and refuses to submit early.
> - **O3 (Dilip Chetia)**: An officer reviewing applications where an applicant embedded prompt-injection text in the village field. The assistant treats it strictly as untrusted data and executes zero unauthorized status changes.
> 
> On our baseline run, our deterministic security suite passed 7 out of 7, but full multi-turn runs uncovered two key issues:
> 1. In C10 and C07, internal officer rejection notes were exposed to citizens. We sanitized the status pipeline to return empathetic explanations without internal prefixes.
> 2. On Groq's free tier, uninterrupted runs hit a 200k daily token quota. We implemented exponential backoff and resume caching, verifying that isolated re-runs pass 100%."

---

### [2:20 – 2:50] Design Note & What I Would Do Differently
**Screen**: `docs/DESIGN_NOTE.md` / `docs/EVAL_FINDINGS.md`.  
**Spoken Audio**:
> "What did I learn? Small models like Claude Haiku or open-source equivalents thrive when tools are outcome-shaped rather than granular CRUD APIs. 
> 
> What would I do differently? I would build a deterministic local replay cache for baseline evaluations earlier. Relying on cloud API free tiers introduced token quota ceilings that complicated automated testing, even though our application logic was completely sound."

---

### [2:50 – 3:00] Handover & Out-of-Scope Boundaries
**Screen**: Eval Dashboard footer showing `/eval-dashboard/api/runs`, `/eval-dashboard/healthz`, and terminal running `scripts/healthcheck.sh`.  
**Spoken Audio**:
> "The deployment is hardened with automated cron healthchecks, nightly rotating database backups, and clean database hygiene with zero leftover marker records. Biometric hardware scanners and physical paper seals remain out of scope for software MCP.
> 
> Everything is live, documented, and ready for your review at `/eval-dashboard`. Thank you."
