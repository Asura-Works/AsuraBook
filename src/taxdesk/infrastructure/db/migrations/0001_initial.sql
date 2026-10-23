-- TaxDesk schema v1. 正本契約: docs/design/03_data_model.md
-- 金額は INTEGER 円 (*_jpy)、率は Decimal 文字列、UTC時刻は ISO8601、業務日は YYYY-MM-DD。
-- 金額が不明な欄は NULL。0 と対象外を区別する。

CREATE TABLE schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

-- ---------------------------------------------------------------- マスター・年度

CREATE TABLE org (
    id               TEXT PRIMARY KEY,
    display_name     TEXT NOT NULL,
    timezone         TEXT NOT NULL DEFAULT 'Asia/Tokyo' CHECK (timezone = 'Asia/Tokyo'),
    currency         TEXT NOT NULL DEFAULT 'JPY' CHECK (currency = 'JPY'),
    ledger_revision  INTEGER NOT NULL DEFAULT 0 CHECK (ledger_revision >= 0),
    created_at       TEXT NOT NULL
) STRICT;

-- 1DB1事業者
CREATE UNIQUE INDEX org_single ON org ((1));

CREATE TABLE fiscal_years (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL REFERENCES org(id),
    year            INTEGER NOT NULL,
    start_date      TEXT NOT NULL,
    end_date        TEXT NOT NULL,
    workflow_state  TEXT NOT NULL DEFAULT 'open'
                    CHECK (workflow_state IN ('open','preclose','adjusted','reviewed','frozen')),
    revision        INTEGER NOT NULL DEFAULT 1,
    UNIQUE (org_id, year),
    CHECK (start_date <= end_date)
) STRICT;

CREATE TABLE period_closes (
    id              TEXT PRIMARY KEY,
    fiscal_year_id  TEXT NOT NULL REFERENCES fiscal_years(id),
    period_start    TEXT NOT NULL,
    period_end      TEXT NOT NULL,
    workflow_state  TEXT NOT NULL CHECK (workflow_state IN ('open','reviewing','soft_closed')),
    reason          TEXT,
    actor           TEXT NOT NULL,
    revision        INTEGER NOT NULL DEFAULT 1,
    updated_at      TEXT NOT NULL,
    UNIQUE (fiscal_year_id, period_start),
    CHECK (period_start <= period_end)
) STRICT;

CREATE TABLE accounts (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL REFERENCES org(id),
    code            TEXT NOT NULL,
    name            TEXT NOT NULL,
    type            TEXT NOT NULL CHECK (type IN ('asset','liability','equity','revenue','expense')),
    normal_side     TEXT NOT NULL CHECK (normal_side IN ('debit','credit')),
    -- 債権債務として open item を発生させる科目
    open_item_type  TEXT CHECK (open_item_type IN ('receivable','payable')),
    -- 繰越計算で意味を持つ科目 (元入金・事業主貸・事業主借)
    role            TEXT CHECK (role IN ('owner_capital','owner_draw','owner_contribution')),
    active          INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    revision        INTEGER NOT NULL DEFAULT 1,
    UNIQUE (org_id, code)
) STRICT;

CREATE TABLE subaccounts (
    id          TEXT PRIMARY KEY,
    account_id  TEXT NOT NULL REFERENCES accounts(id),
    code        TEXT NOT NULL,
    name        TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    UNIQUE (account_id, code),
    UNIQUE (account_id, id)
) STRICT;

CREATE TABLE counterparties (
    id                   TEXT PRIMARY KEY,
    org_id               TEXT NOT NULL REFERENCES org(id),
    display_name         TEXT NOT NULL,
    registration_number  TEXT CHECK (registration_number IS NULL OR registration_number GLOB 'T[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'),
    revision             INTEGER NOT NULL DEFAULT 1
) STRICT;

CREATE TABLE source_accounts (
    id            TEXT PRIMARY KEY,
    org_id        TEXT NOT NULL REFERENCES org(id),
    code          TEXT NOT NULL,
    provider      TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('bank','card','platform','other')),
    display_name  TEXT NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'JPY',
    account_id    TEXT REFERENCES accounts(id),
    subaccount_id TEXT,
    purpose       TEXT,
    active        INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    UNIQUE (org_id, code),
    FOREIGN KEY (account_id, subaccount_id) REFERENCES subaccounts(account_id, id)
) STRICT;

CREATE TABLE provider_profiles (
    id            TEXT PRIMARY KEY,
    provider      TEXT NOT NULL,
    data_kind     TEXT NOT NULL,
    version       TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    payload_hash  TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (provider, data_kind, version)
) STRICT;

CREATE TABLE rule_versions (
    id                 TEXT PRIMARY KEY,
    rule_key           TEXT NOT NULL,
    version            TEXT NOT NULL,
    effective_from     TEXT NOT NULL,
    effective_to       TEXT,
    algorithm_version  TEXT NOT NULL,
    payload_json       TEXT NOT NULL,
    payload_hash       TEXT NOT NULL,
    source_url         TEXT,
    confirmed_at       TEXT,
    approved_at        TEXT,
    UNIQUE (rule_key, version)
) STRICT;

-- ---------------------------------------------------------------- 仕訳

CREATE TABLE journal_entries (
    id               TEXT PRIMARY KEY,
    org_id           TEXT NOT NULL REFERENCES org(id),
    fiscal_year_id   TEXT NOT NULL REFERENCES fiscal_years(id),
    journal_date     TEXT NOT NULL,
    number           INTEGER,
    kind             TEXT NOT NULL DEFAULT 'normal'
                     CHECK (kind IN ('normal','reversal','replacement','opening','opening_adjustment','adjustment')),
    description      TEXT NOT NULL DEFAULT '',
    counterparty_id  TEXT REFERENCES counterparties(id),
    status           TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','validated','posted')),
    revision         INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    validation_hash  TEXT,
    created_by       TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    posted_by        TEXT,
    posted_at        TEXT,
    posted_audit_seq INTEGER,
    UNIQUE (fiscal_year_id, number),
    CHECK ((status = 'posted') = (number IS NOT NULL)),
    CHECK (status <> 'posted' OR (posted_by IS NOT NULL AND posted_at IS NOT NULL)),
    CHECK (status = 'draft' OR validation_hash IS NOT NULL)
) STRICT;

CREATE TABLE journal_lines (
    id                 TEXT PRIMARY KEY,
    entry_id           TEXT NOT NULL REFERENCES journal_entries(id),
    line_no            INTEGER NOT NULL CHECK (line_no >= 1),
    account_id         TEXT NOT NULL REFERENCES accounts(id),
    subaccount_id      TEXT,
    side               TEXT NOT NULL CHECK (side IN ('debit','credit')),
    amount_jpy         INTEGER NOT NULL CHECK (amount_jpy > 0),
    tax_code           TEXT,
    tax_amount_jpy     INTEGER,
    tax_amount_status  TEXT NOT NULL DEFAULT 'unknown'
                       CHECK (tax_amount_status IN ('unknown','known','not_applicable')),
    business_ratio     TEXT,
    counterparty_id    TEXT REFERENCES counterparties(id),
    description        TEXT NOT NULL DEFAULT '',
    UNIQUE (entry_id, line_no),
    FOREIGN KEY (account_id, subaccount_id) REFERENCES subaccounts(account_id, id),
    CHECK ((tax_amount_status = 'known' AND tax_amount_jpy IS NOT NULL AND tax_amount_jpy >= 0)
        OR (tax_amount_status <> 'known' AND tax_amount_jpy IS NULL))
) STRICT;

CREATE INDEX journal_entries_year_date ON journal_entries (fiscal_year_id, journal_date, number);
CREATE INDEX journal_entries_status ON journal_entries (status);
CREATE INDEX journal_lines_account ON journal_lines (account_id, entry_id);

-- posted は不変。状態遷移は draft -> validated -> posted、validated -> draft のみ。
CREATE TRIGGER journal_entries_posted_no_update
BEFORE UPDATE ON journal_entries
WHEN OLD.status = 'posted'
BEGIN
    SELECT RAISE(ABORT, 'POSTED_IMMUTABLE: posted journal entry cannot be updated');
END;

CREATE TRIGGER journal_entries_transition
BEFORE UPDATE OF status ON journal_entries
WHEN NOT (
       OLD.status = NEW.status
    OR (OLD.status = 'draft'     AND NEW.status = 'validated')
    OR (OLD.status = 'validated' AND NEW.status IN ('draft','posted'))
)
BEGIN
    SELECT RAISE(ABORT, 'INVALID_TRANSITION: journal status transition not allowed');
END;

CREATE TRIGGER journal_entries_insert_status
BEFORE INSERT ON journal_entries
WHEN NEW.status <> 'draft'
BEGIN
    SELECT RAISE(ABORT, 'INVALID_TRANSITION: journal entry must be created as draft');
END;

CREATE TRIGGER journal_entries_posted_no_delete
BEFORE DELETE ON journal_entries
WHEN OLD.status = 'posted'
BEGIN
    SELECT RAISE(ABORT, 'POSTED_IMMUTABLE: posted journal entry cannot be deleted');
END;

-- 明細は draft の間だけ追加・変更・削除できる
CREATE TRIGGER journal_lines_insert_draft_only
BEFORE INSERT ON journal_lines
WHEN (SELECT status FROM journal_entries WHERE id = NEW.entry_id) IS NOT 'draft'
BEGIN
    SELECT RAISE(ABORT, 'POSTED_IMMUTABLE: lines can only be added to draft entries');
END;

CREATE TRIGGER journal_lines_update_draft_only
BEFORE UPDATE ON journal_lines
WHEN (SELECT status FROM journal_entries WHERE id = OLD.entry_id) IS NOT 'draft'
BEGIN
    SELECT RAISE(ABORT, 'POSTED_IMMUTABLE: lines of non-draft entries cannot be updated');
END;

CREATE TRIGGER journal_lines_delete_draft_only
BEFORE DELETE ON journal_lines
WHEN (SELECT status FROM journal_entries WHERE id = OLD.entry_id) IS NOT 'draft'
BEGIN
    SELECT RAISE(ABORT, 'POSTED_IMMUTABLE: lines of non-draft entries cannot be deleted');
END;

-- ---------------------------------------------------------------- 訂正

CREATE TABLE correction_groups (
    id                 TEXT PRIMARY KEY,
    original_entry_id  TEXT NOT NULL UNIQUE REFERENCES journal_entries(id),
    reversal_entry_id  TEXT NOT NULL UNIQUE REFERENCES journal_entries(id),
    status             TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active')),
    reason             TEXT NOT NULL,
    actor              TEXT NOT NULL,
    created_at         TEXT NOT NULL
) STRICT;

CREATE TABLE correction_replacements (
    correction_group_id  TEXT NOT NULL REFERENCES correction_groups(id),
    entry_id             TEXT NOT NULL UNIQUE REFERENCES journal_entries(id),
    PRIMARY KEY (correction_group_id, entry_id)
) STRICT;

CREATE TRIGGER correction_groups_no_update BEFORE UPDATE ON correction_groups
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY: correction_groups'); END;
CREATE TRIGGER correction_groups_no_delete BEFORE DELETE ON correction_groups
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY: correction_groups'); END;
CREATE TRIGGER correction_replacements_no_update BEFORE UPDATE ON correction_replacements
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY: correction_replacements'); END;
CREATE TRIGGER correction_replacements_no_delete BEFORE DELETE ON correction_replacements
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY: correction_replacements'); END;

-- ---------------------------------------------------------------- 原資料・観測・経済イベント

CREATE TABLE source_files (
    id             TEXT PRIMARY KEY,
    org_id         TEXT NOT NULL REFERENCES org(id),
    sha256         TEXT NOT NULL,
    size_bytes     INTEGER NOT NULL CHECK (size_bytes >= 0),
    mime           TEXT NOT NULL,
    local_ref      TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE (org_id, sha256)
) STRICT;

CREATE TABLE source_file_receipts (
    id              TEXT PRIMARY KEY,
    source_file_id  TEXT NOT NULL REFERENCES source_files(id),
    original_name   TEXT,
    received_at     TEXT NOT NULL,
    actor           TEXT NOT NULL
) STRICT;

CREATE TABLE import_batches (
    id                 TEXT PRIMARY KEY,
    org_id             TEXT NOT NULL REFERENCES org(id),
    source_account_id  TEXT NOT NULL REFERENCES source_accounts(id),
    source_file_id     TEXT NOT NULL REFERENCES source_files(id),
    profile_id         TEXT NOT NULL REFERENCES provider_profiles(id),
    acceptance_key     TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('staged','needs_review','reviewed','failed')),
    coverage_json      TEXT NOT NULL,
    created_by         TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    UNIQUE (org_id, acceptance_key)
) STRICT;

CREATE TABLE import_attempts (
    id               TEXT PRIMARY KEY,
    batch_id         TEXT NOT NULL REFERENCES import_batches(id),
    parser_version   TEXT NOT NULL,
    profile_version  TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('succeeded','partial','failed')),
    row_count        INTEGER NOT NULL,
    new_events       INTEGER NOT NULL,
    known_events     INTEGER NOT NULL,
    held_rows        INTEGER NOT NULL,
    created_at       TEXT NOT NULL
) STRICT;

CREATE TABLE source_observations (
    id               TEXT PRIMARY KEY,
    attempt_id       TEXT NOT NULL REFERENCES import_attempts(id),
    source_file_id   TEXT NOT NULL REFERENCES source_files(id),
    locator          TEXT NOT NULL,
    raw_json         TEXT NOT NULL,
    raw_external_id  TEXT,
    observed_at      TEXT NOT NULL
) STRICT;
CREATE INDEX source_observations_file ON source_observations (source_file_id, locator);

CREATE TABLE source_transactions (
    id                       TEXT PRIMARY KEY,
    org_id                   TEXT NOT NULL REFERENCES org(id),
    source_account_id        TEXT NOT NULL REFERENCES source_accounts(id),
    event_type               TEXT NOT NULL,
    raw_provider_id          TEXT,
    normalized_identity_key  TEXT,
    transaction_date         TEXT,
    recognition_date         TEXT,
    settlement_date          TEXT,
    period_start             TEXT,
    period_end               TEXT,
    grain                    TEXT NOT NULL DEFAULT 'detail' CHECK (grain IN ('detail','monthly','net_only')),
    recognition_scope        TEXT,
    currency                 TEXT NOT NULL DEFAULT 'JPY',
    amount_jpy               INTEGER,
    provider_status          TEXT,
    description              TEXT NOT NULL DEFAULT '',
    created_at               TEXT NOT NULL
) STRICT;
-- 安定IDを持つイベントだけ一意。IDなし fingerprint には UNIQUE を付けない。
CREATE UNIQUE INDEX source_transactions_identity
    ON source_transactions (org_id, normalized_identity_key)
    WHERE normalized_identity_key IS NOT NULL;
CREATE INDEX source_transactions_account_date ON source_transactions (source_account_id, transaction_date);

CREATE TABLE event_observation_links (
    event_id        TEXT NOT NULL REFERENCES source_transactions(id),
    observation_id  TEXT NOT NULL REFERENCES source_observations(id),
    basis           TEXT NOT NULL,
    PRIMARY KEY (event_id, observation_id)
) STRICT;

CREATE TABLE source_components (
    id              TEXT PRIMARY KEY,
    event_id        TEXT NOT NULL REFERENCES source_transactions(id),
    kind            TEXT NOT NULL CHECK (kind IN ('gross','fee','withholding','refund','net','tax','amount')),
    amount_jpy      INTEGER,
    observation_id  TEXT REFERENCES source_observations(id)
) STRICT;

CREATE TABLE source_usage_slots (
    id            TEXT PRIMARY KEY,
    event_id      TEXT NOT NULL REFERENCES source_transactions(id),
    role          TEXT NOT NULL CHECK (role IN ('recognition','settlement')),
    capacity_jpy  INTEGER NOT NULL CHECK (capacity_jpy >= 0),
    UNIQUE (event_id, role)
) STRICT;

CREATE TABLE source_links (
    id                   TEXT PRIMARY KEY,
    journal_line_id      TEXT NOT NULL REFERENCES journal_lines(id),
    event_id             TEXT NOT NULL REFERENCES source_transactions(id),
    role                 TEXT NOT NULL CHECK (role IN ('recognition','settlement','support','history')),
    usage_slot_id        TEXT REFERENCES source_usage_slots(id),
    signed_effect_jpy    INTEGER NOT NULL,
    correction_group_id  TEXT REFERENCES correction_groups(id),
    created_at           TEXT NOT NULL,
    CHECK ((role IN ('support','history') AND usage_slot_id IS NULL AND signed_effect_jpy = 0)
        OR (role IN ('recognition','settlement') AND usage_slot_id IS NOT NULL))
) STRICT;
CREATE INDEX source_links_event ON source_links (event_id, role);
CREATE INDEX source_links_line ON source_links (journal_line_id);

CREATE TRIGGER source_links_posted_immutable_u
BEFORE UPDATE ON source_links
WHEN (SELECT e.status FROM journal_lines l JOIN journal_entries e ON e.id = l.entry_id WHERE l.id = OLD.journal_line_id) = 'posted'
BEGIN SELECT RAISE(ABORT, 'POSTED_IMMUTABLE: source link of posted line'); END;
CREATE TRIGGER source_links_posted_immutable_d
BEFORE DELETE ON source_links
WHEN (SELECT e.status FROM journal_lines l JOIN journal_entries e ON e.id = l.entry_id WHERE l.id = OLD.journal_line_id) = 'posted'
BEGIN SELECT RAISE(ABORT, 'POSTED_IMMUTABLE: source link of posted line'); END;

CREATE TABLE import_issues (
    id          TEXT PRIMARY KEY,
    attempt_id  TEXT NOT NULL REFERENCES import_attempts(id),
    code        TEXT NOT NULL,
    severity    TEXT NOT NULL CHECK (severity IN ('error','warning','needs_review')),
    locator     TEXT,
    field       TEXT,
    raw_masked  TEXT,
    message     TEXT NOT NULL,
    remedy      TEXT
) STRICT;

CREATE TRIGGER source_observations_no_update BEFORE UPDATE ON source_observations
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY: source_observations'); END;
CREATE TRIGGER source_observations_no_delete BEFORE DELETE ON source_observations
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY: source_observations'); END;
CREATE TRIGGER source_components_no_update BEFORE UPDATE ON source_components
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY: source_components'); END;
CREATE TRIGGER source_components_no_delete BEFORE DELETE ON source_components
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY: source_components'); END;

-- ---------------------------------------------------------------- open item と日付付き消込

CREATE TABLE open_items (
    id                     TEXT PRIMARY KEY,
    org_id                 TEXT NOT NULL REFERENCES org(id),
    origin_line_id         TEXT NOT NULL UNIQUE REFERENCES journal_lines(id),
    type                   TEXT NOT NULL CHECK (type IN ('receivable','payable')),
    account_id             TEXT NOT NULL REFERENCES accounts(id),
    subaccount_id          TEXT,
    counterparty_id        TEXT REFERENCES counterparties(id),
    currency               TEXT NOT NULL DEFAULT 'JPY',
    created_business_date  TEXT NOT NULL
) STRICT;
CREATE INDEX open_items_account_date ON open_items (account_id, created_business_date);

CREATE TABLE open_item_movements (
    id                       TEXT PRIMARY KEY,
    open_item_id             TEXT NOT NULL REFERENCES open_items(id),
    kind                     TEXT NOT NULL CHECK (kind IN ('origin','adjustment','reversal')),
    signed_amount_jpy        INTEGER NOT NULL,
    effective_business_date  TEXT NOT NULL,
    source_journal_line_id   TEXT REFERENCES journal_lines(id),
    correction_group_id      TEXT REFERENCES correction_groups(id),
    recorded_audit_seq       INTEGER NOT NULL
) STRICT;
CREATE INDEX open_item_movements_item ON open_item_movements (open_item_id, effective_business_date);

CREATE TABLE reconciliation_allocations (
    id                       TEXT PRIMARY KEY,
    open_item_id             TEXT NOT NULL REFERENCES open_items(id),
    settlement_line_id       TEXT NOT NULL REFERENCES journal_lines(id),
    amount_jpy               INTEGER NOT NULL CHECK (amount_jpy > 0),
    effect                   TEXT NOT NULL CHECK (effect IN ('allocate','cancel')),
    reverses_allocation_id   TEXT UNIQUE REFERENCES reconciliation_allocations(id),
    effective_business_date  TEXT NOT NULL,
    recorded_audit_seq       INTEGER NOT NULL,
    actor                    TEXT NOT NULL,
    created_at               TEXT NOT NULL,
    CHECK ((effect = 'cancel') = (reverses_allocation_id IS NOT NULL))
) STRICT;
CREATE INDEX reconciliation_allocations_item ON reconciliation_allocations (open_item_id, effective_business_date);
CREATE INDEX reconciliation_allocations_settlement ON reconciliation_allocations (settlement_line_id);

CREATE TRIGGER open_item_movements_no_update BEFORE UPDATE ON open_item_movements
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY: open_item_movements'); END;
CREATE TRIGGER open_item_movements_no_delete BEFORE DELETE ON open_item_movements
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY: open_item_movements'); END;
CREATE TRIGGER reconciliation_allocations_no_update BEFORE UPDATE ON reconciliation_allocations
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY: reconciliation_allocations'); END;
CREATE TRIGGER reconciliation_allocations_no_delete BEFORE DELETE ON reconciliation_allocations
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY: reconciliation_allocations'); END;

-- ---------------------------------------------------------------- 証憑

CREATE TABLE evidence_documents (
    id                TEXT PRIMARY KEY,
    org_id            TEXT NOT NULL REFERENCES org(id),
    receipt_kind      TEXT NOT NULL CHECK (receipt_kind IN ('paper_photo','paper_scan','electronic','screenshot','unknown')),
    acquired_from     TEXT,
    retention_policy  TEXT,
    retention_hold    INTEGER NOT NULL DEFAULT 1 CHECK (retention_hold IN (0,1)),
    retain_until      TEXT,
    created_at        TEXT NOT NULL
) STRICT;

CREATE TABLE evidence_objects (
    id                TEXT PRIMARY KEY,
    org_id            TEXT NOT NULL REFERENCES org(id),
    document_id       TEXT NOT NULL REFERENCES evidence_documents(id),
    role              TEXT NOT NULL CHECK (role IN ('original','derived')),
    parent_object_id  TEXT REFERENCES evidence_objects(id),
    sha256            TEXT NOT NULL,
    mime              TEXT NOT NULL,
    size_bytes        INTEGER NOT NULL CHECK (size_bytes >= 0),
    local_ref         TEXT NOT NULL,
    cloud_key         TEXT,
    cloud_version     TEXT,
    sync_state        TEXT NOT NULL DEFAULT 'pending'
                      CHECK (sync_state IN ('pending','uploading','uploaded_unverified','verified','retry_wait','failed','not_configured')),
    created_at        TEXT NOT NULL,
    CHECK ((role = 'derived') = (parent_object_id IS NOT NULL))
) STRICT;
CREATE UNIQUE INDEX evidence_objects_original_hash ON evidence_objects (org_id, sha256) WHERE role = 'original';

CREATE TABLE evidence_receipts (
    id             TEXT PRIMARY KEY,
    object_id      TEXT NOT NULL REFERENCES evidence_objects(id),
    original_name  TEXT,
    received_at    TEXT NOT NULL,
    actor          TEXT NOT NULL
) STRICT;

CREATE TABLE evidence_index_rows (
    id           TEXT PRIMARY KEY,
    document_id  TEXT NOT NULL REFERENCES evidence_documents(id),
    created_at   TEXT NOT NULL
) STRICT;

CREATE TABLE evidence_index_versions (
    id                TEXT PRIMARY KEY,
    index_row_id      TEXT NOT NULL REFERENCES evidence_index_rows(id),
    version           INTEGER NOT NULL CHECK (version >= 1),
    business_date     TEXT,
    amount_jpy        INTEGER,
    counterparty      TEXT,
    transaction_kind  TEXT,
    page_from         INTEGER,
    page_to           INTEGER,
    raw_text          TEXT,
    status            TEXT NOT NULL CHECK (status IN ('proposed','confirmed','withdrawn')),
    reason            TEXT,
    actor             TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    UNIQUE (index_row_id, version)
) STRICT;
CREATE INDEX evidence_index_versions_search ON evidence_index_versions (business_date, amount_jpy, counterparty);

CREATE TRIGGER evidence_index_versions_no_update BEFORE UPDATE ON evidence_index_versions
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY: evidence_index_versions'); END;
CREATE TRIGGER evidence_index_versions_no_delete BEFORE DELETE ON evidence_index_versions
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY: evidence_index_versions'); END;

CREATE TABLE evidence_links (
    id                TEXT PRIMARY KEY,
    document_id       TEXT NOT NULL REFERENCES evidence_documents(id),
    object_id         TEXT REFERENCES evidence_objects(id),
    index_row_id      TEXT REFERENCES evidence_index_rows(id),
    journal_entry_id  TEXT NOT NULL REFERENCES journal_entries(id),
    actor             TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    UNIQUE (document_id, journal_entry_id)
) STRICT;

-- ---------------------------------------------------------------- queue・outbox・AI

CREATE TABLE tasks (
    id            TEXT PRIMARY KEY,
    org_id        TEXT NOT NULL REFERENCES org(id),
    kind          TEXT NOT NULL,
    state         TEXT NOT NULL CHECK (state IN ('queued','running','retry_wait','succeeded','failed',
                                                 'cancel_requested','cancelled','dead_letter','outcome_unknown')),
    payload_json  TEXT NOT NULL,
    input_hash    TEXT NOT NULL,
    attempt       INTEGER NOT NULL DEFAULT 0,
    lease_token   TEXT,
    lease_until   TEXT,
    deadline      TEXT,
    next_run_at   TEXT,
    result_ref    TEXT,
    error         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
) STRICT;
CREATE INDEX tasks_state_next ON tasks (state, next_run_at);

CREATE TABLE sync_outbox (
    id               TEXT PRIMARY KEY,
    org_id           TEXT NOT NULL REFERENCES org(id),
    object_id        TEXT NOT NULL REFERENCES evidence_objects(id),
    operation        TEXT NOT NULL CHECK (operation IN ('upload')),
    idempotency_key  TEXT NOT NULL UNIQUE,
    state            TEXT NOT NULL CHECK (state IN ('queued','running','retry_wait','succeeded','failed','outcome_unknown','held')),
    attempt          INTEGER NOT NULL DEFAULT 0,
    lease_token      TEXT,
    lease_until      TEXT,
    next_attempt_at  TEXT,
    last_error       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
) STRICT;

CREATE TABLE agent_jobs (
    id                 TEXT PRIMARY KEY,
    org_id             TEXT NOT NULL REFERENCES org(id),
    provider           TEXT NOT NULL CHECK (provider IN ('claude_cli','codex_cli')),
    job_kind           TEXT NOT NULL CHECK (job_kind IN ('receipt.extract','journal.suggest','law.discover',
                                                         'law.assess','filing.review','filing.followup')),
    model              TEXT,
    cli_version        TEXT,
    prompt_version     TEXT NOT NULL,
    schema_version     TEXT NOT NULL,
    policy_version     TEXT NOT NULL,
    input_hash         TEXT NOT NULL,
    snapshot_revision  INTEGER,
    process_identity   TEXT,
    status             TEXT NOT NULL CHECK (status IN ('queued','preparing','running','validating','succeeded',
                                                       'auth_required','quota_wait','timed_out','cancelled',
                                                       'failed','invalid_output','outcome_unknown','held')),
    attempt            INTEGER NOT NULL DEFAULT 1,
    usage_json         TEXT,
    result_hash        TEXT,
    result_json        TEXT,
    error_code         TEXT,
    error_message      TEXT,
    created_at         TEXT NOT NULL,
    started_at         TEXT,
    finished_at        TEXT
) STRICT;

CREATE TABLE ocr_jobs (
    id               TEXT PRIMARY KEY,
    org_id           TEXT NOT NULL REFERENCES org(id),
    object_id        TEXT NOT NULL REFERENCES evidence_objects(id),
    provider         TEXT NOT NULL CHECK (provider IN ('claude_cli','codex_cli','manual')),
    state            TEXT NOT NULL CHECK (state IN ('queued','preprocessing','running','extracted','needs_review',
                                                    'reviewed','retryable_failed','permanent_failed','cancelled',
                                                    'outcome_unknown','held')),
    agent_job_id     TEXT REFERENCES agent_jobs(id),
    extraction_json  TEXT,
    validation_json  TEXT,
    reviewed_json    TEXT,
    reviewed_by      TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
) STRICT;

CREATE TABLE findings (
    id                 TEXT PRIMARY KEY,
    org_id             TEXT NOT NULL REFERENCES org(id),
    fiscal_year_id     TEXT REFERENCES fiscal_years(id),
    origin             TEXT NOT NULL CHECK (origin IN ('deterministic','ai_suggestion')),
    topic              TEXT NOT NULL,
    code               TEXT NOT NULL,
    severity           TEXT NOT NULL CHECK (severity IN ('info','warning','error')),
    status             TEXT NOT NULL CHECK (status IN ('open','acknowledged','resolved')),
    snapshot_revision  INTEGER NOT NULL,
    message            TEXT NOT NULL,
    evidence_json      TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
) STRICT;

-- ---------------------------------------------------------------- 税務 snapshot・繰越

CREATE TABLE tax_profiles (
    id              TEXT PRIMARY KEY,
    fiscal_year_id  TEXT NOT NULL REFERENCES fiscal_years(id),
    version         INTEGER NOT NULL,
    payload_json    TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('unconfirmed','confirmed')),
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (fiscal_year_id, version)
) STRICT;

CREATE TABLE tax_snapshots (
    id                   TEXT PRIMARY KEY,
    fiscal_year_id       TEXT NOT NULL REFERENCES fiscal_years(id),
    ledger_revision      INTEGER NOT NULL,
    audit_cutoff_seq     INTEGER NOT NULL,
    rule_versions_json   TEXT NOT NULL,
    input_manifest_json  TEXT NOT NULL,
    result_json          TEXT NOT NULL,
    content_hash         TEXT NOT NULL,
    created_by           TEXT NOT NULL,
    created_at           TEXT NOT NULL
) STRICT;
CREATE TRIGGER tax_snapshots_no_update BEFORE UPDATE ON tax_snapshots
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY: tax_snapshots'); END;
CREATE TRIGGER tax_snapshots_no_delete BEFORE DELETE ON tax_snapshots
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY: tax_snapshots'); END;

CREATE TABLE carryforward_revisions (
    id                   TEXT PRIMARY KEY,
    source_year_id       TEXT NOT NULL REFERENCES fiscal_years(id),
    target_year_id       TEXT NOT NULL REFERENCES fiscal_years(id),
    source_snapshot_id   TEXT NOT NULL REFERENCES tax_snapshots(id),
    entry_id             TEXT NOT NULL REFERENCES journal_entries(id),
    kind                 TEXT NOT NULL CHECK (kind IN ('opening','opening_adjustment')),
    revision             INTEGER NOT NULL,
    previous_id          TEXT REFERENCES carryforward_revisions(id),
    projection_json      TEXT NOT NULL,
    diff_json            TEXT,
    created_by           TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    UNIQUE (source_year_id, target_year_id, revision)
) STRICT;

-- ---------------------------------------------------------------- 監査・冪等・認可

CREATE TABLE audit_events (
    sequence_no      INTEGER PRIMARY KEY AUTOINCREMENT,
    id               TEXT NOT NULL UNIQUE,
    org_id           TEXT,
    actor            TEXT NOT NULL,
    request_id       TEXT,
    command          TEXT NOT NULL,
    job_id           TEXT,
    target_type      TEXT,
    target_id        TEXT,
    before_revision  INTEGER,
    after_revision   INTEGER,
    input_hash       TEXT,
    result_hash      TEXT,
    reason           TEXT,
    detail_json      TEXT,
    prev_hash        TEXT NOT NULL,
    hash             TEXT NOT NULL,
    created_at       TEXT NOT NULL
) STRICT;
CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY: audit_events'); END;
CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY: audit_events'); END;

CREATE TABLE command_results (
    org_id           TEXT NOT NULL,
    actor_scope      TEXT NOT NULL,
    command          TEXT NOT NULL,
    idempotency_key  TEXT NOT NULL,
    payload_hash     TEXT NOT NULL,
    request_id       TEXT NOT NULL,
    response_json    TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (org_id, actor_scope, command, idempotency_key)
) STRICT;

CREATE TABLE approval_capabilities (
    id                   TEXT PRIMARY KEY,
    token_hash           TEXT NOT NULL UNIQUE,
    principal            TEXT NOT NULL,
    approval_session     TEXT NOT NULL,
    org_id               TEXT NOT NULL REFERENCES org(id),
    scope                TEXT NOT NULL,
    targets_json         TEXT NOT NULL,
    plan_hash            TEXT NOT NULL,
    expires_at           TEXT NOT NULL,
    restore_generation   TEXT NOT NULL,
    consumed_request_id  TEXT,
    consumed_at          TEXT,
    revoked_at           TEXT,
    created_at           TEXT NOT NULL
) STRICT;
