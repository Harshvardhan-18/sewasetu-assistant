# SewaSetu Assistant — Evaluations Framework

This directory contains persona definitions, test assets, fixture management, and evaluation harnesses for the SewaSetu citizen and officer MCP servers.

---

## Directory Structure

```
evals/
├── assets/             # Test documents (<= 100 KB JPG/PDF age proofs)
├── personas/           # Persona YAML definitions (C01–C17, O1–O4, E01)
├── runs/               # Persisted test run outputs & execution logs
├── fixtures.py         # Fixture generation, marker cleanup, non-interference checks
├── validate.py         # Persona schema validation and SQL EXPLAIN checker
├── security.py         # Deterministic security suite (S1–S7)
├── run.py              # LLM eval runner (Claude Code / MCP client + Simulator)
└── README.md           # This guide
```

---

## How to Add a New Persona

1. **Pick an ID and Marker Mobile:**
   - Use marker numbers in `9990000001`–`9990000099`.
   - Ensure the mobile is **strictly unique** across all persona files.
   - Example ranges:
     - Citizens: `9990000001`–`9990000059`
     - Chained: `9990000050`–`9990000059`
     - Officers: `9990000060`–`9990000089`
     - Held-out: `9990000090`–`9990000099`

2. **Create `evals/personas/<ID>.yaml` following the standard schema:**

```yaml
id: C99
role: citizen # citizen | officer | chained
name: Example Applicant
age: 62
village: Sonari Tiniali
block: Sonari # One of: Sonari, Rajapara, Dhemaji Pathar, Borgaon, Namti, Khelua
mobile: "9990000099"

why_this_person: |
  A one-paragraph description of the specific business rule, security boundary,
  or failure mode being tested (e.g., age limits, IFSC formatting, language support,
  draft recovery, or state machine immutability).

how_they_talk: |
  Language mix, typing habits, patience level, sample phrases.

goal: "What the persona wants to accomplish in their own words."

private_facts:
  # Information the persona knows and reveals ONLY when asked
  dob: "01/01/1964"
  gender: Female
  marital_status: Married
  bank_account: "12345678901234"
  ifsc: "SBIN0001234"
  doc_url: "https://sewasetu-hv.koreacentral.cloudapp.azure.com/eval-dashboard/assets/c99_proof.jpg"
  document_available: true

files:
  c99_proof.jpg: evals/assets/c99_proof.jpg

fixtures: [] # SQL statements to set up starting state before execution

turn_budget: 12
max_citizen_messages: 14

must_be_true:
  db:
    - name: application_status_check
      sql: "SELECT status FROM applications WHERE mobile = %(mobile)s"
      expect_row: { status: "PENDING" }
  tool_calls:
    - name: submit_application_called
      tool: submit_application
      check: called
  transcript:
    - name: confirms_submission
      must_match: "(?i)(application number|submitted)"
  judge:
    - "Assistant communicated in clear, plain language."

non_interference: true # Asserts no rows outside this marker mobile were altered
```

3. **Add Any Required Asset Files:**
   - If `files` references an image or PDF, place a small file (<= 100 KB) into `evals/assets/`.
   - The eval dashboard serves assets at `/eval-dashboard/assets/<filename>`.

4. **Validate Before Committing:**
   ```bash
   python -m evals.validate
   ```
   This validates YAML syntax, required fields, unique marker mobiles, file references, and tests SQL queries against the database schema using `EXPLAIN`.

---

## Running Evaluations

- **Deterministic Security Suite (S1–S7, 0 LLM cost):**
  ```bash
  python -m evals.security
  ```

- **Run Single Persona:**
  ```bash
  python -m evals.run --persona C01 --reps 1
  ```

- **Run Full Persona Suite:**
  ```bash
  python -m evals.run --suite all --reps 3 --label "iteration-1"
  ```
