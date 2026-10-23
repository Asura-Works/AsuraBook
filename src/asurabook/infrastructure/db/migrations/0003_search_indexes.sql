-- schema v3: 金額検索・状態別年度集計の索引 (01章4節の性能目標)。
CREATE INDEX journal_lines_amount ON journal_lines (amount_jpy);
CREATE INDEX journal_entries_year_status ON journal_entries (fiscal_year_id, status, journal_date);
