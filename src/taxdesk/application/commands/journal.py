"""仕訳 command (04章)。draft → validated → posted、訂正は元1 → 反対1 → 置換N。"""

from __future__ import annotations

import sqlite3
from typing import Any

from ...domain.errors import TaxDeskError, invalid
from ...domain.journal import Issue, Line, reversed_side, structural_issues, totals, validation_hash
from ...domain.values import new_id, parse_amount_jpy, parse_date, parse_optional_amount, parse_ratio
from ..core import CommandContext, command
from ..ledger import (active_allocations, allocate, cancel_allocation, create_open_items_for_entry,
                      open_item_balance, settlement_allocated)
from ..lookups import (assert_postable, get_entry, resolve_account, resolve_subaccount, resolve_year,
                       year_for_date)

USER_KINDS = ("normal", "adjustment")
MAX_PAGE = 1000


# ------------------------------------------------------------------ 入力の解析

def _parse_lines(conn: sqlite3.Connection, payload_lines: Any) -> tuple[list[Line], list[dict]]:
    if not isinstance(payload_lines, list) or not payload_lines:
        raise invalid("lines は1行以上の配列です")
    lines: list[Line] = []
    links: list[dict] = []
    for i, raw in enumerate(payload_lines, start=1):
        if not isinstance(raw, dict):
            raise invalid("lines の各要素は object です", line_no=i)
        account = resolve_account(conn, raw.get("account"))
        sub = resolve_subaccount(conn, account["id"], raw.get("subaccount"))
        side = raw.get("side")
        if side not in ("debit", "credit"):
            raise invalid("side は debit または credit です", line_no=i)
        status = raw.get("tax_amount_status", "unknown")
        tax_amount = parse_optional_amount(raw.get("tax_amount_jpy"), "tax_amount_jpy", allow_zero=True)
        line = Line(
            line_no=i,
            account_id=account["id"],
            subaccount_id=None if sub is None else sub["id"],
            side=side,
            amount_jpy=parse_amount_jpy(raw.get("amount_jpy"), f"lines[{i}].amount_jpy"),
            tax_code=raw.get("tax_code"),
            tax_amount_jpy=tax_amount,
            tax_amount_status=status,
            business_ratio=parse_ratio(raw.get("business_ratio")),
            counterparty_id=raw.get("counterparty_id"),
            description=str(raw.get("description") or ""),
        )
        lines.append(line)
        for link in raw.get("source_links") or []:
            role = link.get("role")
            if role not in ("recognition", "settlement", "support", "history"):
                raise invalid("source_links.role は recognition/settlement/support/history です", line_no=i)
            event = conn.execute("SELECT id FROM source_transactions WHERE id=?", (link.get("event_id"),)).fetchone()
            if event is None:
                raise TaxDeskError("NOT_FOUND", "source event が見つかりません", {"event_id": link.get("event_id")})
            effect = 0
            if role in ("recognition", "settlement"):
                effect = parse_amount_jpy(link.get("amount_jpy", line.amount_jpy), "source_links.amount_jpy")
            links.append({"line_no": i, "event_id": event["id"], "role": role, "amount_jpy": effect})
    issues = [x for x in structural_issues(lines) if x.code not in ("UNBALANCED", "TOO_FEW_LINES")]
    if issues:
        raise TaxDeskError("INVALID_INPUT", "仕訳明細が不正です", {"issues": [x.to_dict() for x in issues]})
    return lines, links


def _insert_lines(conn: sqlite3.Connection, entry_id: str, lines: list[Line], links: list[dict],
                  now: str, correction_group_id: str | None = None) -> dict[int, str]:
    ids: dict[int, str] = {}
    for line in lines:
        line_id = new_id()
        ids[line.line_no] = line_id
        conn.execute(
            "INSERT INTO journal_lines (id, entry_id, line_no, account_id, subaccount_id, side, amount_jpy, tax_code, "
            "tax_amount_jpy, tax_amount_status, business_ratio, counterparty_id, description) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (line_id, entry_id, line.line_no, line.account_id, line.subaccount_id, line.side, line.amount_jpy,
             line.tax_code, line.tax_amount_jpy, line.tax_amount_status,
             None if line.business_ratio is None else str(line.business_ratio), line.counterparty_id,
             line.description),
        )
    for link in links:
        slot_id = None
        if link["role"] in ("recognition", "settlement"):
            slot = conn.execute("SELECT id FROM source_usage_slots WHERE event_id=? AND role=?",
                                (link["event_id"], link["role"])).fetchone()
            if slot is None:
                raise TaxDeskError("SOURCE_ROLE_UNAVAILABLE", "この source event にはその会計役割の利用枠がありません",
                                   {"event_id": link["event_id"], "role": link["role"]})
            slot_id = slot["id"]
        conn.execute(
            "INSERT INTO source_links (id, journal_line_id, event_id, role, usage_slot_id, signed_effect_jpy, "
            "correction_group_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (new_id(), ids[link["line_no"]], link["event_id"], link["role"], slot_id,
             link.get("signed_effect", link["amount_jpy"]), correction_group_id, now),
        )
    return ids


def _load_lines(conn: sqlite3.Connection, entry_id: str) -> list[Line]:
    rows = conn.execute("SELECT * FROM journal_lines WHERE entry_id=? ORDER BY line_no", (entry_id,)).fetchall()
    return [Line(
        line_no=r["line_no"], account_id=r["account_id"], subaccount_id=r["subaccount_id"], side=r["side"],
        amount_jpy=r["amount_jpy"], tax_code=r["tax_code"], tax_amount_jpy=r["tax_amount_jpy"],
        tax_amount_status=r["tax_amount_status"],
        business_ratio=None if r["business_ratio"] is None else parse_ratio(r["business_ratio"]),
        counterparty_id=r["counterparty_id"], description=r["description"],
    ) for r in rows]


def _load_links(conn: sqlite3.Connection, entry_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT l.line_no, s.event_id, s.role, s.signed_effect_jpy FROM source_links s "
        "JOIN journal_lines l ON l.id = s.journal_line_id WHERE l.entry_id=?", (entry_id,)).fetchall()
    return [{"line_no": r["line_no"], "event_id": r["event_id"], "role": r["role"],
             "amount_jpy": r["signed_effect_jpy"]} for r in rows]


def _entry_hash(conn: sqlite3.Connection, entry: sqlite3.Row) -> str:
    return validation_hash(entry["journal_date"], entry["description"], entry["counterparty_id"],
                           entry["fiscal_year_id"], entry["kind"], _load_lines(conn, entry["id"]),
                           _load_links(conn, entry["id"]))


# ------------------------------------------------------------------ 検証

def _full_issues(conn: sqlite3.Connection, entry: sqlite3.Row) -> list[Issue]:
    lines = _load_lines(conn, entry["id"])
    issues = structural_issues(lines)
    for line in lines:
        acc = conn.execute("SELECT active, code FROM accounts WHERE id=?", (line.account_id,)).fetchone()
        if not acc["active"]:
            issues.append(Issue("ACCOUNT_INACTIVE", f"科目 {acc['code']} は利用終了しています", line.line_no))
        if line.subaccount_id:
            sub = conn.execute("SELECT active FROM subaccounts WHERE id=?", (line.subaccount_id,)).fetchone()
            if not sub["active"]:
                issues.append(Issue("SUBACCOUNT_INACTIVE", "補助科目は利用終了しています", line.line_no))
    year = conn.execute("SELECT * FROM fiscal_years WHERE id=?", (entry["fiscal_year_id"],)).fetchone()
    if not (year["start_date"] <= entry["journal_date"] <= year["end_date"]):
        issues.append(Issue("DATE_OUTSIDE_YEAR", "仕訳日が年度の範囲外です"))
    try:
        assert_postable(conn, year, entry["journal_date"])
    except TaxDeskError as exc:
        issues.append(Issue(exc.code, exc.message))
    issues.extend(_capacity_issues(conn, entry["id"]))
    return issues


def _capacity_issues(conn: sqlite3.Connection, entry_id: str) -> list[Issue]:
    """source利用枠: 同じ event+役割の有効 signed effect 合計を 0〜上限に保つ (03章4節)。"""
    issues = []
    rows = conn.execute(
        "SELECT s.usage_slot_id, SUM(s.signed_effect_jpy) AS pending FROM source_links s "
        "JOIN journal_lines l ON l.id = s.journal_line_id WHERE l.entry_id=? AND s.usage_slot_id IS NOT NULL "
        "GROUP BY s.usage_slot_id", (entry_id,)).fetchall()
    for r in rows:
        slot = conn.execute("SELECT * FROM source_usage_slots WHERE id=?", (r["usage_slot_id"],)).fetchone()
        used = conn.execute(
            "SELECT COALESCE(SUM(s.signed_effect_jpy),0) FROM source_links s JOIN journal_lines l "
            "ON l.id=s.journal_line_id JOIN journal_entries e ON e.id=l.entry_id "
            "WHERE s.usage_slot_id=? AND e.status='posted'", (slot["id"],)).fetchone()[0]
        total = int(used) + int(r["pending"])
        if total > slot["capacity_jpy"] or total < 0:
            issues.append(Issue("SOURCE_CAPACITY_EXCEEDED",
                                "同じ原資料の同じ会計役割を上限を超えて利用しています (二重計上の可能性)",
                                data={"event_id": slot["event_id"], "role": slot["role"],
                                      "capacity_jpy": slot["capacity_jpy"], "used_jpy": int(used),
                                      "requested_jpy": int(r["pending"])}))
    return issues


# ------------------------------------------------------------------ 表示

def entry_view(conn: sqlite3.Connection, entry: sqlite3.Row, *, with_lines: bool = True) -> dict:
    d = dict(entry)
    year = conn.execute("SELECT year FROM fiscal_years WHERE id=?", (entry["fiscal_year_id"],)).fetchone()
    d["year"] = year["year"]
    lines = conn.execute(
        "SELECT l.*, a.code AS account_code, a.name AS account_name, s.code AS subaccount_code, "
        "s.name AS subaccount_name FROM journal_lines l JOIN accounts a ON a.id=l.account_id "
        "LEFT JOIN subaccounts s ON s.id=l.subaccount_id WHERE l.entry_id=? ORDER BY l.line_no",
        (entry["id"],)).fetchall()
    debit = sum(r["amount_jpy"] for r in lines if r["side"] == "debit")
    credit = sum(r["amount_jpy"] for r in lines if r["side"] == "credit")
    d["debit_total_jpy"] = debit
    d["credit_total_jpy"] = credit
    d["difference_jpy"] = debit - credit
    group = conn.execute("SELECT * FROM correction_groups WHERE original_entry_id=? OR reversal_entry_id=?",
                         (entry["id"], entry["id"])).fetchone()
    replaced = conn.execute(
        "SELECT g.* FROM correction_replacements r JOIN correction_groups g ON g.id=r.correction_group_id "
        "WHERE r.entry_id=?", (entry["id"],)).fetchone()
    d["reversed"] = bool(group and group["original_entry_id"] == entry["id"])
    d["correction_group_id"] = (group["id"] if group else (replaced["id"] if replaced else None))
    if with_lines:
        out_lines = []
        for r in lines:
            ld = dict(r)
            ld["source_links"] = [dict(x) for x in conn.execute(
                "SELECT event_id, role, signed_effect_jpy FROM source_links WHERE journal_line_id=?",
                (r["id"],)).fetchall()]
            item = conn.execute("SELECT id FROM open_items WHERE origin_line_id=?", (r["id"],)).fetchone()
            ld["open_item_id"] = item["id"] if item else None
            out_lines.append(ld)
        d["lines"] = out_lines
        d["evidence"] = [dict(x) for x in conn.execute(
            "SELECT document_id, object_id, index_row_id FROM evidence_links WHERE journal_entry_id=?",
            (entry["id"],)).fetchall()]
    return d


# ------------------------------------------------------------------ 作成・更新

def create_draft(ctx: CommandContext, payload: dict, *, kind: str | None = None) -> sqlite3.Row:
    conn = ctx.db
    journal_date = payload.get("journal_date")
    parse_date(journal_date, "journal_date")
    year = year_for_date(conn, journal_date, payload.get("year"))
    kind = kind or payload.get("kind", "normal")
    if kind not in USER_KINDS + ("reversal", "replacement", "opening", "opening_adjustment"):
        raise invalid("kind が不正です", kind=kind)
    lines, links = _parse_lines(conn, payload.get("lines"))
    entry_id = new_id()
    conn.execute(
        "INSERT INTO journal_entries (id, org_id, fiscal_year_id, journal_date, kind, description, counterparty_id, "
        "status, revision, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?, 'draft', 1, ?,?,?)",
        (entry_id, ctx.org_id, year["id"], journal_date, kind, str(payload.get("description") or ""),
         payload.get("counterparty_id"), ctx.actor, ctx.now, ctx.now),
    )
    _insert_lines(conn, entry_id, lines, links, ctx.now)
    _link_evidence(ctx, entry_id, payload.get("evidence_document_ids") or [])
    return conn.execute("SELECT * FROM journal_entries WHERE id=?", (entry_id,)).fetchone()


def _link_evidence(ctx: CommandContext, entry_id: str, document_ids: list) -> None:
    for doc_id in document_ids:
        doc = ctx.db.execute("SELECT id FROM evidence_documents WHERE id=?", (doc_id,)).fetchone()
        if doc is None:
            raise TaxDeskError("NOT_FOUND", "証憑が見つかりません", {"document_id": doc_id})
        ctx.db.execute(
            "INSERT OR IGNORE INTO evidence_links (id, document_id, journal_entry_id, actor, created_at) "
            "VALUES (?,?,?,?,?)", (new_id(), doc_id, entry_id, ctx.actor, ctx.now))


def _draft_response(conn: sqlite3.Connection, entry: sqlite3.Row) -> dict:
    view = entry_view(conn, entry)
    view["issues"] = [x.to_dict() for x in structural_issues(_load_lines(conn, entry["id"]))]
    return view


@command("journal.draft", min_profile="assistant", mutating=True)
def journal_draft(ctx: CommandContext, payload: dict) -> dict:
    """仕訳の下書きを作る。貸借不一致でも下書きは保存できる。"""
    kind = payload.get("kind", "normal")
    if kind not in USER_KINDS:
        raise invalid("下書きで指定できる kind は normal/adjustment です")
    entry = create_draft(ctx, payload, kind=kind)
    ctx.audit(target_type="journal_entry", target_id=entry["id"], after_revision=1, input_value=payload)
    return _draft_response(ctx.db, entry)


@command("journal.update", min_profile="assistant", mutating=True)
def journal_update(ctx: CommandContext, payload: dict) -> dict:
    """下書き/検証済み仕訳を更新する。検証済みは下書きへ戻る。postedは訂正案へ。"""
    conn = ctx.db
    entry = get_entry(conn, payload.get("id"))
    if entry["status"] == "posted":
        raise TaxDeskError("POSTED_IMMUTABLE", "確定済み仕訳は編集できません。journal reverse で訂正してください",
                           {"id": entry["id"]})
    ctx.require_expected_revision(entry["revision"], "仕訳")
    if payload.get("expected_revision") is not None and payload["expected_revision"] != entry["revision"]:
        raise TaxDeskError("REVISION_CONFLICT", "仕訳の revision が変わっています",
                           {"expected_revision": payload["expected_revision"], "actual_revision": entry["revision"]})
    journal_date = payload.get("journal_date", entry["journal_date"])
    parse_date(journal_date, "journal_date")
    year = year_for_date(conn, journal_date, payload.get("year"))
    if entry["status"] == "validated":
        conn.execute("UPDATE journal_entries SET status='draft', validation_hash=NULL WHERE id=?", (entry["id"],))
    if "lines" in payload:
        lines, links = _parse_lines(conn, payload["lines"])
        conn.execute("DELETE FROM source_links WHERE journal_line_id IN (SELECT id FROM journal_lines WHERE entry_id=?)",
                     (entry["id"],))
        conn.execute("DELETE FROM journal_lines WHERE entry_id=?", (entry["id"],))
        _insert_lines(conn, entry["id"], lines, links, ctx.now)
    conn.execute(
        "UPDATE journal_entries SET journal_date=?, fiscal_year_id=?, description=?, counterparty_id=?, "
        "revision=revision+1, updated_at=? WHERE id=?",
        (journal_date, year["id"], str(payload.get("description", entry["description"]) or ""),
         payload.get("counterparty_id", entry["counterparty_id"]), ctx.now, entry["id"]),
    )
    _link_evidence(ctx, entry["id"], payload.get("evidence_document_ids") or [])
    updated = conn.execute("SELECT * FROM journal_entries WHERE id=?", (entry["id"],)).fetchone()
    ctx.audit(target_type="journal_entry", target_id=entry["id"], before_revision=entry["revision"],
              after_revision=updated["revision"], input_value=payload)
    return _draft_response(conn, updated)


@command("journal.validate", min_profile="assistant", mutating=True)
def journal_validate(ctx: CommandContext, payload: dict) -> dict:
    """確定条件を検査し、満たせば validated にする。"""
    conn = ctx.db
    entry = get_entry(conn, payload.get("id"))
    if entry["status"] == "posted":
        raise TaxDeskError("POSTED_IMMUTABLE", "確定済みです", {"id": entry["id"]})
    ctx.require_expected_revision(entry["revision"], "仕訳")
    issues = _full_issues(conn, entry)
    if issues:
        raise TaxDeskError("VALIDATION_FAILED", "確定条件を満たしていません",
                           {"id": entry["id"], "issues": [x.to_dict() for x in issues]})
    digest = _entry_hash(conn, entry)
    conn.execute("UPDATE journal_entries SET status='validated', validation_hash=?, revision=revision+1, "
                 "updated_at=? WHERE id=?", (digest, ctx.now, entry["id"]))
    updated = conn.execute("SELECT * FROM journal_entries WHERE id=?", (entry["id"],)).fetchone()
    ctx.audit(target_type="journal_entry", target_id=entry["id"], before_revision=entry["revision"],
              after_revision=updated["revision"], result_value=digest)
    return entry_view(conn, updated)


def post_entry(ctx: CommandContext, entry: sqlite3.Row) -> sqlite3.Row:
    """validated 仕訳を transaction 内で再点検して確定する。伝票番号はここで採番する。"""
    conn = ctx.db
    if entry["status"] != "validated":
        raise TaxDeskError("NOT_VALIDATED", "検証済み (validated) の仕訳だけを確定できます",
                           {"id": entry["id"], "status": entry["status"]})
    digest = _entry_hash(conn, entry)
    if digest != entry["validation_hash"]:
        raise TaxDeskError("REVISION_CONFLICT", "検証後に内容が変わっています。再検証してください", {"id": entry["id"]})
    issues = _full_issues(conn, entry)
    if issues:
        raise TaxDeskError("VALIDATION_FAILED", "確定条件を満たしていません",
                           {"id": entry["id"], "issues": [x.to_dict() for x in issues]})
    number = conn.execute("SELECT COALESCE(MAX(number),0)+1 FROM journal_entries WHERE fiscal_year_id=?",
                          (entry["fiscal_year_id"],)).fetchone()[0]
    seq = ctx.audit(target_type="journal_entry", target_id=entry["id"], before_revision=entry["revision"],
                    after_revision=entry["revision"] + 1, result_value={"number": number, "hash": digest},
                    command="journal.post")
    conn.execute(
        "UPDATE journal_entries SET status='posted', number=?, revision=revision+1, posted_by=?, posted_at=?, "
        "posted_audit_seq=?, updated_at=? WHERE id=?",
        (number, ctx.actor, ctx.now, seq, ctx.now, entry["id"]),
    )
    posted = conn.execute("SELECT * FROM journal_entries WHERE id=?", (entry["id"],)).fetchone()
    create_open_items_for_entry(ctx, posted, seq)
    ctx.bump_ledger_revision()
    return posted


@command("journal.post", min_profile="bookkeeper_scoped", mutating=True, capability_scope="journal.post")
def journal_post(ctx: CommandContext, payload: dict) -> dict:
    """検証済み仕訳を確定する。複数指定時は全件を同じtransactionで確定する。"""
    conn = ctx.db
    ids = payload.get("ids") or ([payload["id"]] if payload.get("id") else [])
    if not ids:
        raise invalid("id または ids を指定してください")
    if len(ids) != len(set(ids)):
        raise invalid("同じ仕訳が重複指定されています")
    posted = []
    for ref in ids:
        entry = get_entry(conn, ref)
        if len(ids) == 1:
            ctx.require_expected_revision(entry["revision"], "仕訳")
        if ctx.capability is not None:
            target = ctx.capability_target(entry["id"])
            if target.get("revision") != entry["revision"] or target.get("validation_hash") != entry["validation_hash"]:
                raise TaxDeskError("CAPABILITY_INVALID", "承認後に仕訳が変更されています", {"id": entry["id"]})
        posted.append(entry_view(conn, post_entry(ctx, entry)))
    return {"posted": posted}


@command("journal.discard", min_profile="assistant", mutating=True)
def journal_discard(ctx: CommandContext, payload: dict) -> dict:
    """下書きを破棄する。内容は監査へ残す。postedには使えない。"""
    conn = ctx.db
    entry = get_entry(conn, payload.get("id"))
    if entry["status"] == "posted":
        raise TaxDeskError("POSTED_IMMUTABLE", "確定済み仕訳は破棄できません。訂正してください", {"id": entry["id"]})
    ctx.require_expected_revision(entry["revision"], "仕訳")
    snapshot = entry_view(conn, entry)
    if entry["status"] == "validated":
        conn.execute("UPDATE journal_entries SET status='draft', validation_hash=NULL WHERE id=?", (entry["id"],))
    conn.execute("DELETE FROM source_links WHERE journal_line_id IN (SELECT id FROM journal_lines WHERE entry_id=?)",
                 (entry["id"],))
    conn.execute("DELETE FROM evidence_links WHERE journal_entry_id=?", (entry["id"],))
    conn.execute("DELETE FROM journal_lines WHERE entry_id=?", (entry["id"],))
    conn.execute("DELETE FROM journal_entries WHERE id=?", (entry["id"],))
    ctx.audit(target_type="journal_entry", target_id=entry["id"], before_revision=entry["revision"],
              reason=payload.get("reason"), detail={"discarded": snapshot})
    return {"discarded": entry["id"]}


# ------------------------------------------------------------------ 訂正

@command("journal.reverse", min_profile="bookkeeper_scoped", mutating=True, capability_scope="journal.reverse")
def journal_reverse(ctx: CommandContext, payload: dict) -> dict:
    """確定仕訳を訂正する: 完全反対1件 + 置換N件 + 依存消込の取消/再割当を同じtransactionで行う。"""
    conn = ctx.db
    original = get_entry(conn, payload.get("id"))
    reason = payload.get("reason")
    if not reason:
        raise invalid("訂正理由 reason は必須です")
    if original["status"] != "posted":
        raise TaxDeskError("NOT_POSTED", "下書きは journal update で修正してください", {"id": original["id"]})
    if original["kind"] == "reversal":
        raise TaxDeskError("CANNOT_REVERSE_REVERSAL", "取消仕訳そのものは取り消せません")
    if original["kind"] in ("opening", "opening_adjustment"):
        raise TaxDeskError("CANNOT_REVERSE_OPENING", "期首仕訳は繰越 (year carryforward) で差額調整してください")
    existing = conn.execute("SELECT * FROM correction_groups WHERE original_entry_id=?", (original["id"],)).fetchone()
    if existing is not None:
        raise TaxDeskError("ALREADY_REVERSED", "この仕訳は既に取消済みです",
                           {"correction_group_id": existing["id"], "reversal_entry_id": existing["reversal_entry_id"]})
    ctx.require_expected_revision(original["revision"], "仕訳")
    if ctx.capability is not None:
        target = ctx.capability_target(original["id"])
        if target.get("revision") != original["revision"]:
            raise TaxDeskError("CAPABILITY_INVALID", "承認後に対象が変わっています")

    reversal_date = payload.get("journal_date") or original["journal_date"]
    parse_date(reversal_date, "journal_date")

    # 完全反対
    orig_lines = conn.execute("SELECT * FROM journal_lines WHERE entry_id=? ORDER BY line_no",
                              (original["id"],)).fetchall()
    reversal_payload_lines = [{
        "account": l["account_id"], "subaccount": l["subaccount_id"], "side": reversed_side(l["side"]),
        "amount_jpy": l["amount_jpy"], "tax_code": l["tax_code"], "tax_amount_jpy": l["tax_amount_jpy"],
        "tax_amount_status": l["tax_amount_status"], "business_ratio": l["business_ratio"],
        "counterparty_id": l["counterparty_id"], "description": l["description"],
    } for l in orig_lines]
    reversal = create_draft(ctx, {
        "journal_date": reversal_date, "description": f"取消: {original['description']}",
        "counterparty_id": original["counterparty_id"], "lines": reversal_payload_lines,
    }, kind="reversal")
    group_id = new_id()
    conn.execute(
        "INSERT INTO correction_groups (id, original_entry_id, reversal_entry_id, reason, actor, created_at) "
        "VALUES (?,?,?,?,?,?)", (group_id, original["id"], reversal["id"], reason, ctx.actor, ctx.now))

    # source利用の相殺: 元の有効利用を反対仕訳で打ち消す
    rev_line_ids = {r["line_no"]: r["id"] for r in conn.execute(
        "SELECT id, line_no FROM journal_lines WHERE entry_id=?", (reversal["id"],)).fetchall()}
    for l in orig_lines:
        for link in conn.execute("SELECT * FROM source_links WHERE journal_line_id=?", (l["id"],)).fetchall():
            conn.execute(
                "INSERT INTO source_links (id, journal_line_id, event_id, role, usage_slot_id, signed_effect_jpy, "
                "correction_group_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (new_id(), rev_line_ids[l["line_no"]], link["event_id"],
                 link["role"] if link["role"] in ("recognition", "settlement") else "history",
                 link["usage_slot_id"] if link["role"] in ("recognition", "settlement") else None,
                 -link["signed_effect_jpy"] if link["role"] in ("recognition", "settlement") else 0,
                 group_id, ctx.now))
    _validate_and_post(ctx, reversal)
    seq = ctx.audit(target_type="correction_group", target_id=group_id, reason=reason,
                    detail={"original_entry_id": original["id"], "reversal_entry_id": reversal["id"]})

    # 依存する消込の取消と元本の取消
    cancelled = []
    for l in orig_lines:
        for alloc in active_allocations(conn, settlement_line_id=l["id"]):
            cancelled.append(cancel_allocation(ctx, alloc["id"], max(reversal_date, alloc["effective_business_date"]), seq))
        item = conn.execute("SELECT * FROM open_items WHERE origin_line_id=?", (l["id"],)).fetchone()
        if item is not None:
            for alloc in active_allocations(conn, open_item_id=item["id"]):
                cancelled.append(cancel_allocation(ctx, alloc["id"],
                                                   max(reversal_date, alloc["effective_business_date"]), seq))
            principal = open_item_balance(conn, item["id"])
            if principal != 0:
                conn.execute(
                    "INSERT INTO open_item_movements (id, open_item_id, kind, signed_amount_jpy, "
                    "effective_business_date, source_journal_line_id, correction_group_id, recorded_audit_seq) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (new_id(), item["id"], "reversal", -principal, reversal_date,
                     rev_line_ids[l["line_no"]], group_id, seq))

    # 置換
    replacements = []
    for rp in payload.get("replacements") or []:
        entry = create_draft(ctx, rp, kind="replacement")
        conn.execute("INSERT INTO correction_replacements (correction_group_id, entry_id) VALUES (?,?)",
                     (group_id, entry["id"]))
        replacements.append(_validate_and_post(ctx, entry))

    # 再割当
    allocations = []
    for ra in payload.get("reallocations") or []:
        item_id = ra.get("open_item_id")
        if item_id is None and "replacement_index" in ra:
            idx = int(ra["replacement_index"])
            if not (0 <= idx < len(replacements)):
                raise invalid("replacement_index が範囲外です", replacement_index=idx)
            line = conn.execute("SELECT id FROM journal_lines WHERE entry_id=? AND line_no=?",
                                (replacements[idx]["id"], int(ra.get("line_no", 1)))).fetchone()
            item = conn.execute("SELECT id FROM open_items WHERE origin_line_id=?", (line["id"],)).fetchone() \
                if line else None
            if item is None:
                raise invalid("置換仕訳の指定行に open item がありません", reallocation=ra)
            item_id = item["id"]
        allocations.append(allocate(ctx, open_item_id=item_id, settlement_line_id=ra["settlement_line_id"],
                                    amount_jpy=ra["amount_jpy"], effective_date=ra.get("effective_date"),
                                    audit_seq=seq))
    # 再割当できなかった決済は未決済として明示する (完済状態を偽装しない)
    unallocated = []
    reversed_line_ids = {l["id"] for l in orig_lines}
    for cid in cancelled:
        line_id = conn.execute("SELECT settlement_line_id FROM reconciliation_allocations WHERE id=?",
                               (cid,)).fetchone()[0]
        if line_id in reversed_line_ids or line_id in [u["settlement_line_id"] for u in unallocated]:
            continue
        amount = conn.execute("SELECT amount_jpy FROM journal_lines WHERE id=?", (line_id,)).fetchone()[0]
        left = amount - settlement_allocated(conn, line_id)
        if left > 0:
            unallocated.append({"settlement_line_id": line_id, "unallocated_jpy": left})
    if unallocated:
        ctx.warn("SETTLEMENT_UNALLOCATED", "取消により未割当になった決済行があります。消込を確認してください")
    ctx.bump_ledger_revision()
    return {
        "correction_group_id": group_id,
        "original_entry_id": original["id"],
        "reversal": entry_view(conn, conn.execute("SELECT * FROM journal_entries WHERE id=?",
                                                  (reversal["id"],)).fetchone()),
        "replacements": replacements,
        "cancelled_allocations": cancelled,
        "new_allocations": allocations,
        "unallocated_settlements": unallocated,
    }


def _validate_and_post(ctx: CommandContext, entry: sqlite3.Row) -> dict:
    conn = ctx.db
    issues = _full_issues(conn, entry)
    if issues:
        raise TaxDeskError("VALIDATION_FAILED", "確定条件を満たしていません",
                           {"id": entry["id"], "kind": entry["kind"], "issues": [x.to_dict() for x in issues]})
    digest = _entry_hash(conn, entry)
    conn.execute("UPDATE journal_entries SET status='validated', validation_hash=?, revision=revision+1 WHERE id=?",
                 (digest, entry["id"]))
    validated = conn.execute("SELECT * FROM journal_entries WHERE id=?", (entry["id"],)).fetchone()
    return entry_view(conn, post_entry(ctx, validated))


# ------------------------------------------------------------------ 参照

@command("journal.show", min_profile="reader", mutating=False)
def journal_show(ctx: CommandContext, payload: dict) -> dict:
    """仕訳1件を明細・根拠・訂正情報付きで返す。"""
    return entry_view(ctx.db, get_entry(ctx.db, payload.get("id")))


@command("journal.list", min_profile="reader", mutating=False)
def journal_list(ctx: CommandContext, payload: dict) -> dict:
    """仕訳一覧。stable sort と cursor で既定100件・上限1000件。"""
    conn = ctx.db
    cond, params = ["1=1"], []
    if payload.get("year") is not None:
        cond.append("e.fiscal_year_id=?")
        params.append(resolve_year(conn, payload["year"])["id"])
    if payload.get("date_from"):
        cond.append("e.journal_date >= ?")
        params.append(payload["date_from"])
    if payload.get("date_to"):
        cond.append("e.journal_date <= ?")
        params.append(payload["date_to"])
    if payload.get("status"):
        cond.append("e.status = ?")
        params.append(payload["status"])
    if payload.get("text"):
        cond.append("(e.description LIKE ? OR EXISTS (SELECT 1 FROM journal_lines x WHERE x.entry_id=e.id "
                    "AND x.description LIKE ?))")
        params += [f"%{payload['text']}%"] * 2
    if payload.get("account"):
        acc = resolve_account(conn, payload["account"])
        cond.append("EXISTS (SELECT 1 FROM journal_lines x WHERE x.entry_id=e.id AND x.account_id=?)")
        params.append(acc["id"])
    if payload.get("amount_jpy") is not None:
        cond.append("EXISTS (SELECT 1 FROM journal_lines x WHERE x.entry_id=e.id AND x.amount_jpy=?)")
        params.append(int(payload["amount_jpy"]))
    if payload.get("cursor"):
        d, cid = str(payload["cursor"]).split("|", 1)
        cond.append("(e.journal_date > ? OR (e.journal_date = ? AND e.id > ?))")
        params += [d, d, cid]
    limit = int(payload.get("limit") or 100)
    if not (1 <= limit <= MAX_PAGE):
        raise invalid(f"limit は1〜{MAX_PAGE}です")
    rows = conn.execute(
        f"SELECT e.* FROM journal_entries e WHERE {' AND '.join(cond)} ORDER BY e.journal_date, e.id LIMIT ?",
        params + [limit + 1]).fetchall()
    items = [entry_view(conn, r) for r in rows[:limit]]
    next_cursor = f"{rows[limit - 1]['journal_date']}|{rows[limit - 1]['id']}" if len(rows) > limit else None
    return {"items": items, "next_cursor": next_cursor}
