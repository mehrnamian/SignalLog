"""Add scheduled HTTP check fields and mark manual results.

Revision ID: 0001_http_checks
Revises:
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_http_checks"
down_revision = None
branch_labels = None
depends_on = None


def _column_names(connection, table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(connection).get_columns(table_name)}


def _index_names(connection, table_name: str) -> set[str]:
    return set(
        connection.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = :table_name"),
            {"table_name": table_name},
        ).scalars()
    )


def _create_checks() -> None:
    op.create_table(
        "checks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("target_url", sa.String(length=2048), nullable=True),
        sa.Column("interval_seconds", sa.Integer(), server_default="60", nullable=False),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_disabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "interval_seconds >= 60 AND interval_seconds <= 86400",
            name="cc_check_interval_range",
        ),
    )


def _create_results() -> None:
    op.create_table(
        "check_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("check_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("source", sa.String(length=10), server_default="manual", nullable=False),
        sa.Column("message", sa.String(length=1000), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('passed', 'failed')", name="cc_check_result_status"),
        sa.CheckConstraint("source IN ('manual', 'monitor')", name="cc_check_result_source"),
        sa.ForeignKeyConstraint(["check_id"], ["checks.id"], ondelete="RESTRICT"),
    )


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())

    if "checks" not in tables:
        _create_checks()
    else:
        existing = _column_names(connection, "checks")
        if "target_url" not in existing:
            op.add_column("checks", sa.Column("target_url", sa.String(length=2048), nullable=True))
        if "interval_seconds" not in existing:
            op.add_column(
                "checks",
                sa.Column("interval_seconds", sa.Integer(), server_default="60", nullable=False),
            )
        if "next_check_at" not in existing:
            op.add_column("checks", sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True))
        if "last_checked_at" not in existing:
            op.add_column("checks", sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True))

    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    if "check_results" not in tables:
        _create_results()
    elif "source" not in _column_names(connection, "check_results"):
        op.add_column(
            "check_results",
            sa.Column("source", sa.String(length=10), server_default="manual", nullable=False),
        )

    inspector = sa.inspect(connection)
    check_indexes = _index_names(connection, "checks")
    if "ix_checks_name_lower" not in check_indexes:
        op.create_index(
            "ix_checks_name_lower",
            "checks",
            [sa.text("lower(name)")],
            unique=True,
        )
    if "ix_checks_id" not in check_indexes:
        op.create_index("ix_checks_id", "checks", ["id"])

    result_indexes = _index_names(connection, "check_results")
    if "ix_check_results_id" not in result_indexes:
        op.create_index("ix_check_results_id", "check_results", ["id"])
    if "ix_check_results_check_id" not in result_indexes:
        op.create_index("ix_check_results_check_id", "check_results", ["check_id"])


def downgrade() -> None:
    connection = op.get_bind()
    if "check_results" in sa.inspect(connection).get_table_names():
        indexes = _index_names(connection, "check_results")
        for name in ("ix_check_results_check_id", "ix_check_results_id"):
            if name in indexes:
                op.drop_index(name, table_name="check_results")
        if "source" in _column_names(connection, "check_results"):
            op.drop_column("check_results", "source")

    if "checks" in sa.inspect(connection).get_table_names():
        indexes = _index_names(connection, "checks")
        for name in ("ix_checks_name_lower", "ix_checks_id"):
            if name in indexes:
                op.drop_index(name, table_name="checks")
        existing = _column_names(connection, "checks")
        for name in ("last_checked_at", "next_check_at", "interval_seconds", "target_url"):
            if name in existing:
                op.drop_column("checks", name)
