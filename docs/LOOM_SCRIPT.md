# Loom Presentation Script (3:00 — Face + Screen)

**Title**: SewaSetu Old Age Pension AI Assistant — Architecture, Security, & Handover  
**Presenter**: Harshvardhan  
**Target Duration**: Exactly 3 minutes (03:00)  
**Live URL**: `https://sewasetu-hv.koreacentral.cloudapp.azure.com/eval-dashboard`  

---

### [0:00 – 0:25] Hook & Headline (Spoken: ~60 words)
**Screen**: Public Eval Dashboard (`/eval-dashboard`), showing live endpoints and security status cards.  
**Spoken Audio**:
> "Welcome Director. You asked us to enable any AI assistant to help citizens in Sonari block apply for and track their Old Age Pension—using open Model Context Protocol standards with the government portal as the single system of record.
> 
> Today, our dual MCP architecture is live on Azure, providing strict statutory enforcement, cryptographic identity isolation, and verified database integrity. Here is our architecture and demonstration."

---

### [0:25 – 1:15] Core Architecture Decisions (Spoken: ~115 words)
**Screen**: Architecture diagram in `README.md` and Caddy reverse proxy routing.  
**Spoken Audio**:
> "Three architectural decisions anchor the platform:
> 
> First, **strict role separation**. We run two isolated MCP servers on separate endpoints: `/citizen/mcp` and `/officer/mcp`. They enforce distinct OAuth scopes and audiences. Citizen assistants physically cannot see administrative tools. Identity is extracted directly from the verified JWT token subject—no tool ever accepts an applicant phone number as an argument.
> 
> Second, **one unified submission path**. Both the web portal and the citizen assistant call the shared `apply_for_pension_core()` domain service. The AI cannot approve or submit anything the physical counter would reject.
> 
> Third, **compact tool surfaces**. Citizen MCP uses 10 tools taking 1,963 prompt tokens, well under the 3,500 token ceiling."

---

### [1:15 – 2:05] Counter Personas & Security Verification (Spoken: ~115 words)
**Screen**: Eval Dashboard persona catalog showing C01, C02, C16, and O3, followed by `evals/security.py` results.  
**Spoken Audio**:
> "We derived 19 starter personas directly from rural counter scenarios:
> - **C16**: A grandson applying for his grandmother at a kiosk, triggering explicit session teardown via `end_my_session` to protect elder privacy.
> - **C02**: An applicant under 60 whose application is stopped before early submission.
> - **O3**: An officer reviewing records where applicants entered prompt injections into text fields; untrusted input is safely contained.
> 
> Our deterministic security suite passed 7 out of 7 tests across role separation, audience binding, and perimeter isolation. Persona evaluations run via the official MCP Python SDK loop against open models on Groq, serving as an open proxy for small-model benchmarks."

---

### [2:05 – 2:40] Free-Tier Learnings & What We'd Do Differently (Spoken: ~80 words)
**Screen**: `docs/DESIGN_NOTE.md` and token monitoring logs.  
**Spoken Audio**:
> "What did we learn? Small models need outcome-shaped tools that return unified error payloads rather than chatty CRUD calls.
> 
> What would I do differently? I would introduce an offline replay cache for evaluation runs earlier. Multi-turn benchmarks hit Groq's free-tier ceiling of 200,000 tokens per day. We engineered exponential backoff, rate pacing, and checkpoint resume to maintain database and test integrity without artificial failures."

---

### [2:40 – 3:00] Handover & Clean Database Hygiene (Spoken: ~45 words)
**Screen**: Terminal showing `scripts/healthcheck.sh` passing and database count at 100,000 records.  
**Spoken Audio**:
> "The production environment is hardened with daily rotating backups, container restart policies, and zero leftover marker records—preserving the baseline 100,000 applications.
> 
> All code, tests, and documentation are committed and live at `/eval-dashboard`. The system is ready for review."
