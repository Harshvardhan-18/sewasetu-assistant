-- app/migrations.sql — idempotent migrations run by the `migrate` service on every start
-- schema.sql only runs on first boot of an empty volume; these additions run on every start
-- so updates to a running database are applied without re-seeding.

-- ── assistant_drafts: server-side draft for the citizen MCP ──────────────
CREATE TABLE IF NOT EXISTS assistant_drafts (
    mobile            VARCHAR(15) PRIMARY KEY,
    data              JSONB        NOT NULL DEFAULT '{}'::jsonb,
    doc_path          TEXT,
    doc_name          TEXT,
    confirmation_hash TEXT,
    updated_at        TIMESTAMP    NOT NULL DEFAULT now()
);

-- ── one_active_per_mobile: DB-level backstop for the rule parity requirement ──
-- An applicant may hold at most one application in PENDING / APPROVED / DEEMED_APPROVED
-- at a time. The service layer checks this with active_application() and an advisory lock,
-- but a partial unique index makes the constraint visible at the DB level and catches any
-- coding path that bypasses the service layer.
--
-- Verify no violations exist before creating:
--   SELECT mobile, count(*) FROM applications
--   WHERE status IN ('PENDING','APPROVED','DEEMED_APPROVED')
--   GROUP BY mobile HAVING count(*) > 1;
-- Expected result on the seeded database: 0 rows.
CREATE UNIQUE INDEX IF NOT EXISTS one_active_per_mobile
    ON applications(mobile)
    WHERE status IN ('PENDING','APPROVED','DEEMED_APPROVED');

-- ── assistant_signouts: kiosk/shared-phone sign-out cut-off ──────────────
-- POST /internal/api/v1/me/session/end records a cut-off time here.
-- The MCP token verifier rejects any access token whose iat <= revoked_at
-- for the same subject, even if the token has not yet expired.
CREATE TABLE IF NOT EXISTS assistant_signouts (
    subject    VARCHAR(80) PRIMARY KEY,
    revoked_at TIMESTAMP   NOT NULL
);
