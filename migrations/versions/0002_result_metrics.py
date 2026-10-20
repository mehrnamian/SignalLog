"""Record automated HTTP response metrics and index result ranges.

Revision ID: 0002_result_metrics
Revises: 0001_http_checks
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_result_metrics"
down_revision = "0001_http_checks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("check_results", sa.Column("status_code", sa.Integer(), nullable=True))
    op.add_column("check_results", sa.Column("latency_ms", sa.Integer(), nullable=True))
    op.create_index("ix_check_results_recorded_at", "check_results", ["recorded_at"])
    op.create_index(
        "ix_check_results_check_id_recorded_at",
        "check_results",
        ["check_id", "recorded_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_check_results_check_id_recorded_at", table_name="check_results")
    op.drop_index("ix_check_results_recorded_at", table_name="check_results")
    with op.batch_alter_table("check_results", recreate="always") as batch_op:
        batch_op.drop_column("latency_ms")
        batch_op.drop_column("status_code")
