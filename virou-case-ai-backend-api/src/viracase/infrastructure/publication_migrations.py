import re
import unicodedata
from hashlib import sha256
from pathlib import PurePosixPath

from viracase.domain.publication_no_effect import EXECUTION_PROTOCOL
from viracase.domain.rules import RuleError, canonical_hash
from viracase.infrastructure.publication_d1_diagnostics import (
    sanitize_provider_diagnostic,
    sql_result_evidence,
)
from viracase.infrastructure.publication_logging import safe_text
from viracase.infrastructure.publication_migration_validation import validate_migration_plan
from viracase.infrastructure.publication_sql_review import (
    ANALYSIS_METHOD,
    null_backfill_target,
    split_sql_statements,
    sql_review_findings,
)

LEDGER = "__viroucaseai_migrations"
MAX_ISSUES = 12
MESSAGES = {
    "MIGRATION_CHECKSUM_CONFLICT": (
        "Uma migration aplicada foi alterada ou removida, ou seu histórico diverge do recibo remoto.",
        (
            "Restaure o conteúdo exato da migration aplicada e crie uma nova migration incremental para a mudança. "
            "Não substitua checksums nem repita SQL já aplicado."
        ),
    ),
    "MIGRATION_RESULT_UNKNOWN": (
        "O histórico da migration ainda não foi confirmado pelo recibo remoto.",
        "Confira o banco e seus recibos antes de qualquer novo envio. Uma migration sem resultado confirmado não será repetida.",
    ),
    "MIGRATION_REVIEW_REQUIRED": (
        "Migration exige revisão de segurança antes da publicação.",
        "Confira o comando e a localização indicados e preserve os dados existentes antes de publicar.",
    ),
    "MIGRATION_SQL_REJECTED": (
        "O banco recusou a migration antes de executar a instrução.",
        "Confira a causa do banco e corrija o plano preservando a história de migrations. Depois da correção, envie novamente.",
    ),
    "MIGRATION_RECEIPT_PENDING": (
        "A execução SQL foi confirmada, mas o recibo da migration ainda precisa ser confirmado.",
        "O responsável pode reconciliar o recibo com a evidência da execução e do schema. O SQL da migration não será repetido.",
    ),
    "MIGRATION_VALIDATION_FAILED": (
        "A migration nova não passou na validação isolada do schema.",
        "Corrija somente a migration incremental ainda em preparação e preserve os arquivos históricos.",
    ),
    "MIGRATION_BASELINE_UNVERIFIABLE": (
        "Não foi possível reconstruir o schema a partir do histórico preservado.",
        "Confira a consistência da história e os recursos SQL específicos do provedor antes de publicar.",
    ),
    "MIGRATION_ORDER_INVALID": (
        "Uma migration nova foi inserida antes do histórico já aplicado.",
        "Use um novo nome posterior à última migration, mantendo a ordem lexicográfica do projeto.",
    ),
}


def _migration_path(value):
    return (
        isinstance(value, str)
        and len(value) <= 180
        and value.startswith("migrations/")
        and value.endswith(".sql")
        and "\\" not in value
        and ":" not in value
        and not any(part in {"", ".", ".."} for part in value.split("/"))
        and "/".join(PurePosixPath(value).parts) == value
        and not any(unicodedata.category(char) in {"Cf", "Cc"} for char in value)
    )


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _issue(code, reason, source, name=None, expected=None, actual=None):
    message, hint = MESSAGES[code]
    if reason == "receipt_conflict":
        hint = "Confira a divergência entre o inventário e o recibo remoto antes de publicar. Não altere checksums nem repita a migration."
    issue = {
        "code": code,
        "reason": reason,
        "receipt_source": source,
        "message": message,
        "hint": hint,
    }
    if _migration_path(name) and safe_text(name, limit=181) == name:
        issue["path"] = name
    if _digest(expected):
        issue["expected_sha256"] = expected
    if _digest(actual) or actual is None:
        issue["actual_sha256"] = actual
    return issue


def _failure(code, inventory, issues):
    diagnostic = {
        "format_version": 1,
        "category": "migration",
        "code": code,
        "issues": issues[:MAX_ISSUES],
        "issue_count": len(issues),
    }
    if code == "MIGRATION_REVIEW_REQUIRED":
        diagnostic["analysis_method"] = ANALYSIS_METHOD
    revision = inventory.get("published_revision_id")
    if isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", revision):
        diagnostic["published_revision_id"] = revision
    raise RuleError(code, MESSAGES[code][0], details={"migration": diagnostic})


def _history(value, inventory, source):
    if not isinstance(value, dict) or any(
        not _migration_path(name) or not _digest(digest) for name, digest in value.items()
    ):
        _failure(
            "MIGRATION_RESULT_UNKNOWN",
            inventory,
            [_issue("MIGRATION_RESULT_UNKNOWN", "invalid_receipt", source)],
        )
    return dict(value)


def _check_history(files, inventory, migrations, source):
    issues = []
    for name, expected in sorted(migrations.items()):
        actual = sha256(files[name].encode()).hexdigest() if name in files else None
        if actual != expected:
            issues.append(
                _issue(
                    "MIGRATION_CHECKSUM_CONFLICT",
                    "checksum_changed" if name in files else "missing_file",
                    source,
                    name,
                    expected,
                    actual,
                )
            )
    if issues:
        _failure("MIGRATION_CHECKSUM_CONFLICT", inventory, issues)


def confirmed_migrations(inventory):
    return _history(inventory.get("migrations", {}), inventory, "inventory")


def preflight_migrations(files, inventory):
    _check_history(files, inventory, confirmed_migrations(inventory), "inventory")


def _remote_history(rows, inventory):
    observed = {}
    if isinstance(rows, list) and rows:
        for result in rows:
            if not isinstance(result, dict) or not isinstance(result.get("results"), list):
                break
            for item in result["results"]:
                if (
                    not isinstance(item, dict)
                    or not _migration_path(item.get("name"))
                    or not _digest(item.get("sha256"))
                    or item["name"] in observed
                ):
                    break
                observed[item["name"]] = item["sha256"]
            else:
                continue
            break
        else:
            return observed
    _failure(
        "MIGRATION_RESULT_UNKNOWN",
        inventory,
        [_issue("MIGRATION_RESULT_UNKNOWN", "invalid_receipt", "remote")],
    )


def verify_remote_migration_history(gateway, database_id, files, inventory, *, require_complete=False):
    expected = confirmed_migrations(inventory)
    preflight_migrations(files, inventory)
    try:
        remote = _remote_history(gateway.query(database_id, f"SELECT name, sha256 FROM {LEDGER}"), inventory)
    except RuleError as exc:
        if getattr(exc, "provider_diagnostic", {}).get("reason") == "missing_table":
            if not expected and not inventory.get("migration_in_progress") and not require_complete:
                return {}
            _failure("MIGRATION_RESULT_UNKNOWN", inventory, [_issue("MIGRATION_RESULT_UNKNOWN", "receipt_missing", "remote")])
        raise
    _check_history(files, inventory, remote, "remote")
    for name, digest in expected.items():
        if remote.get(name) != digest:
            code = "MIGRATION_CHECKSUM_CONFLICT" if name in remote else "MIGRATION_RESULT_UNKNOWN"
            _failure(code, inventory, [_issue(code, "receipt_conflict" if name in remote else "receipt_missing", "remote", name, digest, remote.get(name))])
    if require_complete:
        for name, content in files.items():
            if _migration_path(name) and remote.get(name) != sha256(content.encode()).hexdigest():
                _failure("MIGRATION_RESULT_UNKNOWN", inventory, [_issue("MIGRATION_RESULT_UNKNOWN", "receipt_missing", "remote", name, sha256(content.encode()).hexdigest(), remote.get(name))])
    return remote


def migration_schema_fingerprint(gateway, database_id):
    results = gateway.query(
        database_id,
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        "WHERE type IN ('table', 'index', 'view', 'trigger') "
        "AND name NOT GLOB 'sqlite_*' AND name NOT GLOB '_cf_*' AND name <> ? ORDER BY type, name",
        [LEDGER],
    )
    schema = []
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict) or not isinstance(results[0].get("results"), list):
        raise RuleError("MIGRATION_RECEIPT_PENDING", MESSAGES["MIGRATION_RECEIPT_PENDING"][0])
    for item in results[0]["results"]:
        if (
            not isinstance(item, dict) or not isinstance(item.get("type"), str)
            or item["type"] not in {"table", "index", "view", "trigger"}
            or not isinstance(item.get("name"), str) or not isinstance(item.get("tbl_name"), str)
            or item.get("sql") is not None and not isinstance(item.get("sql"), str)
        ):
            raise RuleError("MIGRATION_RECEIPT_PENDING", MESSAGES["MIGRATION_RECEIPT_PENDING"][0])
        schema.append([item["type"], item["name"], item["tbl_name"], item.get("sql")])
    return {"sha256": canonical_hash(schema), "object_count": len(schema)}


def confirm_migration_receipt(gateway, database_id, pending, *, before_write=None):
    inventory = {"migration_in_progress": pending}
    if not isinstance(pending, dict) or not _migration_path(pending.get("name")) or not _digest(pending.get("sha256")):
        _failure("MIGRATION_RESULT_UNKNOWN", inventory, [_issue("MIGRATION_RESULT_UNKNOWN", "invalid_receipt", "inventory")])
    name, digest = pending["name"], pending["sha256"]
    rows = gateway.query(database_id, f"SELECT name, sha256 FROM {LEDGER} WHERE name = ?", [name])
    observed = _remote_history(rows, inventory)
    if len(observed) > 1 or any(path != name for path in observed):
        _failure("MIGRATION_RESULT_UNKNOWN", inventory, [_issue("MIGRATION_RESULT_UNKNOWN", "invalid_receipt", "remote", name, digest)])
    if name in observed:
        if observed[name] != digest:
            _failure("MIGRATION_CHECKSUM_CONFLICT", inventory, [_issue("MIGRATION_CHECKSUM_CONFLICT", "receipt_conflict", "remote", name, digest, observed[name])])
        return {"name": name, "sha256": digest, "resolution": "existing_receipt", "receipt_confirmed": True}
    sql_result = pending.get("sql_result")
    schema = pending.get("schema_after")
    if pending.get("phase") == "receipt_confirmed":
        _failure("MIGRATION_RESULT_UNKNOWN", inventory, [_issue("MIGRATION_RESULT_UNKNOWN", "receipt_missing", "remote", name, digest)])
    if (
        pending.get("protocol_version") != 2
        or not isinstance(pending.get("phase"), str)
        or pending["phase"] not in {"sql_confirmed", "receipt_sending"}
        or pending.get("effect_outcome") != "confirmed"
        or not isinstance(sql_result, dict) or type(sql_result.get("confirmed_statements")) is not int
        or type(pending.get("statement_count")) is not int or pending["statement_count"] < 1
        or sql_result["confirmed_statements"] != pending["statement_count"]
        or not isinstance(schema, dict) or not _digest(schema.get("sha256"))
    ):
        _failure("MIGRATION_RECEIPT_PENDING", inventory, [_issue("MIGRATION_RECEIPT_PENDING", "execution_evidence_missing", "inventory", name, digest)])
    current = migration_schema_fingerprint(gateway, database_id)
    if current != schema:
        _failure("MIGRATION_RECEIPT_PENDING", inventory, [_issue("MIGRATION_RECEIPT_PENDING", "schema_changed_after_execution", "remote", name, digest)])
    if before_write:
        before_write()
    gateway.query(database_id, f"INSERT INTO {LEDGER} (name, sha256) VALUES (?, ?)", [name, digest])
    observed = _remote_history(
        gateway.query(database_id, f"SELECT name, sha256 FROM {LEDGER} WHERE name = ?", [name]), inventory
    )
    if observed.get(name) != digest:
        code = "MIGRATION_CHECKSUM_CONFLICT" if name in observed else "MIGRATION_RECEIPT_PENDING"
        _failure(code, inventory, [_issue(code, "receipt_conflict" if name in observed else "pending_receipt_missing", "remote", name, digest, observed.get(name))])
    return {"name": name, "sha256": digest, "resolution": "confirmed_execution_receipt", "receipt_confirmed": True}


def apply_migrations(gateway, database_id, files, inventory, checkpoint, *, revision_id=None, source_files=None, validation_evidence=None):
    if source_files is not None:
        expected = {
            name: content for name, content in source_files.items()
            if name.startswith("migrations/") and name.endswith(".sql")
        }
        if files != expected:
            raise RuleError("ARTIFACT_IDENTITY_MISMATCH", "As migrations do artefato não correspondem aos arquivos da revisão autorizada.")
    preflight_migrations(files, inventory)
    migrations = dict(inventory.get("migrations", {}))
    gateway.query(
        database_id, f"CREATE TABLE IF NOT EXISTS {LEDGER} (name TEXT PRIMARY KEY, sha256 TEXT NOT NULL)"
    )
    rows = gateway.query(database_id, f"SELECT name, sha256 FROM {LEDGER}")
    observed = _remote_history(rows, inventory)
    pending = inventory.get("migration_in_progress")
    if pending:
        if (
            not isinstance(pending, dict)
            or not _migration_path(pending.get("name"))
            or not _digest(pending.get("sha256"))
        ):
            _failure(
                "MIGRATION_RESULT_UNKNOWN",
                inventory,
                [_issue("MIGRATION_RESULT_UNKNOWN", "invalid_receipt", "inventory")],
            )
        name, digest = pending["name"], pending["sha256"]
        if observed.get(name) != digest:
            if name not in observed and pending.get("protocol_version") == 2 and isinstance(pending.get("phase"), str) and pending["phase"] in {"sql_confirmed", "receipt_sending"}:
                _check_history(files, inventory, {name: digest}, "inventory")
                confirm_migration_receipt(gateway, database_id, pending)
                observed[name] = digest
            elif name not in observed and pending.get("protocol_version") == 2 and pending.get("phase") == "prepared" and pending.get("effect_outcome") == "not_sent":
                checkpoint("publication_migrating", {"migration_in_progress": None, "migration_last_attempt": pending}, external=False)
                pending = None
            else:
                _failure(
                    "MIGRATION_CHECKSUM_CONFLICT" if name in observed else "MIGRATION_RESULT_UNKNOWN",
                    inventory,
                    [
                        _issue(
                            "MIGRATION_CHECKSUM_CONFLICT" if name in observed else "MIGRATION_RESULT_UNKNOWN",
                            "pending_receipt_conflict" if name in observed else "pending_receipt_missing",
                            "remote",
                            name,
                            digest,
                            observed.get(name),
                        )
                    ],
                )
        if pending:
            migrations.setdefault(name, digest)
            checkpoint(
                "publication_migrating",
                {"migrations": dict(migrations), "migration_in_progress": None, "migration_last_attempt": {
                    **pending, "phase": "receipt_confirmed", "effect_outcome": "confirmed",
                }},
                external=False,
            )
    for name, digest in sorted(migrations.items()):
        if observed.get(name) != digest:
            code = "MIGRATION_CHECKSUM_CONFLICT" if name in observed else "MIGRATION_RESULT_UNKNOWN"
            _failure(
                code,
                inventory,
                [
                    _issue(
                        code,
                        "receipt_conflict" if name in observed else "receipt_missing",
                        "remote",
                        name,
                        digest,
                        observed.get(name),
                    )
                ],
            )
    if migrations != observed:
        migrations = {**migrations, **observed}
        checkpoint("publication_migrating", {"migrations": dict(migrations)}, external=False)
    _check_history(files, inventory, observed, "remote")
    unapplied = {name: sql for name, sql in sorted(files.items()) if name not in migrations}
    if migrations:
        out_of_order = [name for name in unapplied if name <= max(migrations)]
        if out_of_order:
            _failure("MIGRATION_ORDER_INVALID", inventory, [
                _issue("MIGRATION_ORDER_INVALID", "insert_before_history", "candidate", name)
                for name in out_of_order
            ])
    issues, backfills = [], []
    for name, sql in unapplied.items():
        findings = sql_review_findings(sql, LEDGER)
        for finding in findings:
            statements = split_sql_statements(sql)
            index = finding.get("statement_index", 0) - 1
            if finding["rule"] == "update_rows" and 0 <= index < len(statements) and null_backfill_target(statements[index]):
                backfills.append({
                    "path": name, "statement_index": index + 1,
                    "statement_sha256": sha256(statements[index].encode()).hexdigest(),
                    "finding": finding,
                })
                continue
            issues.append({
                **_issue(
                    "MIGRATION_REVIEW_REQUIRED",
                    "unsafe_sql",
                    "candidate",
                    name,
                    actual=sha256(sql.encode()).hexdigest(),
                ),
                **finding,
            })
    if issues:
        _failure("MIGRATION_REVIEW_REQUIRED", inventory, issues)
    validation = validate_migration_plan(
        source_files if source_files is not None else files,
        baseline_files=None if validation_evidence else {name: files[name] for name in migrations if name in files},
        revision_id=revision_id, previous_evidence=validation_evidence or inventory.get("migration_validation"),
    )
    if _unapproved_backfills(backfills, validation):
        validation = validate_migration_plan(
            source_files if source_files is not None else files,
            baseline_files={name: files[name] for name in migrations if name in files},
            revision_id=revision_id,
        )
    unapproved = _unapproved_backfills(backfills, validation)
    if unapproved:
        _failure("MIGRATION_REVIEW_REQUIRED", inventory, [
            {
                **_issue("MIGRATION_REVIEW_REQUIRED", "unsafe_sql", "candidate", item["path"], actual=sha256(files[item["path"]].encode()).hexdigest()),
                **item["finding"],
            }
            for item in unapproved
        ])
    checkpoint("publication_migrating", {
        "migration_validation": validation,
        "migration_plan": {"version": 1, "revision_id": revision_id, "manifest": validation["manifest"], "policy_version": validation["policy_version"]},
    }, external=False)
    for name, sql in unapplied.items():
        digest = sha256(sql.encode()).hexdigest()
        statements = split_sql_statements(sql)
        prefix = {path: files[path] for path in migrations}
        try:
            baseline = validate_migration_plan(prefix, baseline_files=prefix, revision_id=revision_id)
        except RuleError:
            baseline = {}
        execution_unit = {
            "version": 1, "protocol": EXECUTION_PROTOCOL, "database_id": database_id,
            "sql_sha256": digest, "statement_count": len(statements),
            "statement_sha256": [sha256(statement.encode()).hexdigest() for statement in statements],
            "parameters_sha256": canonical_hash([]), "confirmed_prefix_sha256": canonical_hash(migrations),
            "baseline_schema_sha256": baseline.get("schema_sha256"), "transport": "cloudflare_d1_rest",
            "atomicity": "sqlite_single_statement" if len(statements) == 1 else "unproven_batch",
        }
        attempt = {
            "protocol_version": 2, "name": name, "sha256": digest,
            "phase": "prepared", "effect_outcome": "not_sent",
            "statement_count": len(statements), "execution_unit": execution_unit,
            **({"revision_id": revision_id} if revision_id else {}),
        }
        checkpoint("publication_migrating", {"migration_in_progress": dict(attempt)}, external=False)
        attempt.update(phase="sql_sending", effect_outcome="unknown")
        sending = checkpoint("publication_migrating", {"migration_in_progress": dict(attempt)}, external=True)
        persisted = sending.get("inventory", {}).get("migration_in_progress") if isinstance(sending, dict) else None
        if isinstance(persisted, dict) and persisted.get("name") == name and persisted.get("sha256") == digest:
            for key in ("operation_id", "revision_id"):
                if isinstance(persisted.get(key), str):
                    attempt[key] = persisted[key]
        try:
            results = gateway.query(database_id, sql)
        except Exception as exc:
            code = exc.code if isinstance(exc, RuleError) else "PUBLICATION_FAILED"
            provider = sanitize_provider_diagnostic(getattr(exc, "provider_diagnostic", None))
            effect = provider.get("effect_outcome", "unknown")
            if effect in {"none", "not_sent"}:
                attempt.update(phase="sql_rejected", effect_outcome=effect, provider=provider, code=code)
                exc.migration_resolution = dict(attempt)
                if code in {"LEASE_LOST", "OPERATION_CONFLICT", "VERSION_CONFLICT"}:
                    raise
                classified = exc
                if code in {"PUBLICATION_REJECTED", "PUBLICATION_RESULT_UNKNOWN", "MIGRATION_FAILED"}:
                    classified = RuleError("MIGRATION_SQL_REJECTED", provider.get("message") or "A migration não foi enviada ao banco. Corrija a disponibilidade e tente novamente.")
                    classified.provider_diagnostic = provider
                    classified.migration_resolution = dict(attempt)
                try:
                    checkpoint("publication_migrating", {"migration_in_progress": None, "migration_last_attempt": dict(attempt)}, external=False)
                except Exception as checkpoint_error:
                    if isinstance(checkpoint_error, RuleError) and checkpoint_error.code in {"LEASE_LOST", "OPERATION_CONFLICT", "VERSION_CONFLICT"}:
                        raise
                    raise classified from None
                if classified is not exc:
                    raise classified from None
            else:
                attempt.update(provider=provider, code=code)
                if code not in {"LEASE_LOST", "OPERATION_CONFLICT", "PUBLICATION_CANCELLED", "VERSION_CONFLICT"}:
                    checkpoint("publication_migrating", {"migration_in_progress": dict(attempt)}, external=True)
            raise
        attempt.update(phase="sql_confirmed", effect_outcome="confirmed", sql_result=sql_result_evidence(results))
        checkpoint("publication_migrating", {"migration_in_progress": dict(attempt)}, external=False)
        try:
            attempt["schema_after"] = migration_schema_fingerprint(gateway, database_id)
            checkpoint("publication_migrating", {"migration_in_progress": dict(attempt)}, external=False)
            attempt["phase"] = "receipt_sending"
            checkpoint("publication_migrating", {"migration_in_progress": dict(attempt)}, external=True)
            gateway.query(database_id, f"INSERT INTO {LEDGER} (name, sha256) VALUES (?, ?)", [name, digest])
            receipt = _remote_history(
                gateway.query(database_id, f"SELECT name, sha256 FROM {LEDGER} WHERE name = ?", [name]), inventory
            )
            if receipt.get(name) != digest:
                code = "MIGRATION_CHECKSUM_CONFLICT" if name in receipt else "MIGRATION_RECEIPT_PENDING"
                _failure(code, inventory, [_issue(code, "receipt_conflict" if name in receipt else "pending_receipt_missing", "remote", name, digest, receipt.get(name))])
        except RuleError as exc:
            if exc.code in {"LEASE_LOST", "OPERATION_CONFLICT", "PUBLICATION_CANCELLED", "VERSION_CONFLICT", "MIGRATION_CHECKSUM_CONFLICT"}:
                raise
            provider = sanitize_provider_diagnostic(getattr(exc, "provider_diagnostic", None))
            failure = RuleError("MIGRATION_RECEIPT_PENDING", MESSAGES["MIGRATION_RECEIPT_PENDING"][0], details={
                "migration": {
                    "format_version": 1, "category": "migration", "code": "MIGRATION_RECEIPT_PENDING",
                    "issues": [_issue("MIGRATION_RECEIPT_PENDING", "pending_receipt_missing", "remote", name, digest)],
                    "issue_count": 1,
                },
            })
            failure.provider_diagnostic = provider
            raise failure from None
        attempt["phase"] = "receipt_confirmed"
        checkpoint("publication_migrating", {"migration_in_progress": dict(attempt)}, external=False)
        migrations[name] = digest
        checkpoint(
            "publication_migrating",
            {"migrations": dict(migrations), "migration_in_progress": None, "migration_last_attempt": dict(attempt)},
            external=False,
        )


def _unapproved_backfills(backfills, evidence):
    approved = {
        (item["path"], item["statement_index"], item["statement_sha256"])
        for item in evidence.get("approved_backfills", [])
        if isinstance(item, dict)
        and isinstance(item.get("path"), str) and type(item.get("statement_index")) is int
        and _digest(item.get("statement_sha256"))
        and item.get("scope") == "new_column_null_rows" and item.get("predicate") == "target_column_is_null"
    }
    return [item for item in backfills if (item["path"], item["statement_index"], item["statement_sha256"]) not in approved]
