-- schema v2: 税務profileの不変化、点検結果の識別キー、家事按分、固定資産台帳。

-- 税務profileは版ごとに不変。変更は新versionで行い、過去年度へ遡及上書きしない (11章1節)
CREATE TRIGGER tax_profiles_no_update BEFORE UPDATE ON tax_profiles
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY: tax_profiles'); END;
CREATE TRIGGER tax_profiles_no_delete BEFORE DELETE ON tax_profiles
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY: tax_profiles'); END;

-- 点検結果: 同じ問題を再点検で同定するキー
ALTER TABLE findings ADD COLUMN finding_key TEXT;
ALTER TABLE findings ADD COLUMN resolved_reason TEXT;
CREATE UNIQUE INDEX findings_active_key ON findings (fiscal_year_id, origin, finding_key)
    WHERE status <> 'resolved' AND finding_key IS NOT NULL;
CREATE INDEX findings_year_status ON findings (fiscal_year_id, status);

-- 家事按分 (04章6節): 期間・割合・根拠・対象科目・端数配賦を保存し、同じ対象へ重ねない
CREATE TABLE apportionment_rules (
    id                 TEXT PRIMARY KEY,
    org_id             TEXT NOT NULL REFERENCES org(id),
    fiscal_year_id     TEXT NOT NULL REFERENCES fiscal_years(id),
    account_id         TEXT NOT NULL REFERENCES accounts(id),
    subaccount_id      TEXT,
    period_start       TEXT NOT NULL,
    period_end         TEXT NOT NULL,
    business_ratio     TEXT NOT NULL,
    method             TEXT NOT NULL CHECK (method IN ('year_end')),
    rounding           TEXT NOT NULL CHECK (rounding IN ('floor_business')),
    basis              TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('active','withdrawn')),
    created_by         TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    CHECK (period_start <= period_end)
) STRICT;

CREATE TABLE apportionment_applications (
    id                 TEXT PRIMARY KEY,
    rule_id            TEXT NOT NULL REFERENCES apportionment_rules(id),
    entry_id           TEXT NOT NULL UNIQUE REFERENCES journal_entries(id),
    base_amount_jpy    INTEGER NOT NULL,
    private_amount_jpy INTEGER NOT NULL CHECK (private_amount_jpy >= 0),
    input_manifest     TEXT NOT NULL,
    created_at         TEXT NOT NULL
) STRICT;

-- 固定資産台帳 (11章5節)。耐用年数・償却率は利用者が公式資料から入力し、アプリは名称から決めない
CREATE TABLE fixed_assets (
    id                   TEXT PRIMARY KEY,
    org_id               TEXT NOT NULL REFERENCES org(id),
    name                 TEXT NOT NULL,
    asset_account_id     TEXT NOT NULL REFERENCES accounts(id),
    expense_account_id   TEXT NOT NULL REFERENCES accounts(id),
    acquired_on          TEXT NOT NULL,
    in_service_on        TEXT,
    acquisition_cost_jpy INTEGER NOT NULL CHECK (acquisition_cost_jpy > 0),
    quantity             INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),
    method               TEXT NOT NULL CHECK (method IN ('straight_line','lump_sum_3y','immediate_small')),
    useful_life_years    INTEGER,
    rate_decimal         TEXT,
    business_ratio       TEXT NOT NULL DEFAULT '1',
    disposed_on          TEXT,
    basis                TEXT NOT NULL,
    source_entry_id      TEXT REFERENCES journal_entries(id),
    revision             INTEGER NOT NULL DEFAULT 1,
    created_by           TEXT NOT NULL,
    created_at           TEXT NOT NULL
) STRICT;

CREATE TABLE asset_depreciations (
    id                   TEXT PRIMARY KEY,
    asset_id             TEXT NOT NULL REFERENCES fixed_assets(id),
    fiscal_year_id       TEXT NOT NULL REFERENCES fiscal_years(id),
    entry_id             TEXT NOT NULL UNIQUE REFERENCES journal_entries(id),
    months               INTEGER NOT NULL,
    depreciation_jpy     INTEGER NOT NULL CHECK (depreciation_jpy >= 0),
    business_jpy         INTEGER NOT NULL CHECK (business_jpy >= 0),
    book_value_end_jpy   INTEGER NOT NULL,
    calculation_json     TEXT NOT NULL,
    created_at           TEXT NOT NULL
) STRICT;
CREATE INDEX asset_depreciations_asset_year ON asset_depreciations (asset_id, fiscal_year_id);
