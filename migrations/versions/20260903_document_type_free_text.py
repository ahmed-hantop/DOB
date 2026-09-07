"""Stop enforcing a closed enum for driver_documents.document_type.

This table is written by two independent systems with two different
vocabularies: DOBS itself (iqama/passport/visa/license/medical/contract/
mobile/qiwa_contract/other) and DMS/Laravel, which additionally writes
company_contract/promissory_note (its own document_type column has always
been a free-text varchar, for exactly this reason -- see DMS's own migration
history). Enforcing a closed list on the DOBS side only ever validates on
READ, never on write, so it cannot stop a bad value from being stored -- it
can only crash every dashboard load that touches a driver holding one.

That is exactly what happened, twice: 20260831_add_qiwa_contract_document_type
widened the list once already for this same symptom (LookupError on
qiwa_contract), and production broke again 2026-09-03 the moment DMS wrote
company_contract/promissory_note. The live column was already varchar(100)
at that point -- some prior process (most likely a DMS-side migration) had
already converted it away from a real MySQL ENUM, which is exactly why the
bad values were readable as raw strings by everything except SQLAlchemy's
Python-side type coercion. This migration formally catches Alembic's tracked
history up to that reality, so a fresh environment provisioned from these
migrations alone does not silently recreate the closed enum and reintroduce
the bug.

Revision ID: 20260903_document_type_free_text
Revises: 20260831_add_qiwa_contract_document_type
Create Date: 2026-09-03 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260903_document_type_free_text"
down_revision = "20260831_add_qiwa_contract_document_type"
branch_labels = None
depends_on = None


def _document_type_column(conn):
    return next(
        (
            column
            for column in sa.inspect(conn).get_columns("driver_documents")
            if column["name"] == "document_type"
        ),
        None,
    )


def upgrade():
    conn = op.get_bind()
    column = _document_type_column(conn)
    if column is None or conn.dialect.name != "mysql":
        return

    # Only a real ENUM needs converting -- if it is already a VARCHAR (the
    # production state confirmed 2026-09-03), there is nothing to do.
    if column.get("type").enums is None:
        return

    op.alter_column(
        "driver_documents",
        "document_type",
        existing_type=column["type"],
        type_=sa.String(100),
        existing_nullable=False,
    )


def downgrade():
    # Reverting to a closed enum would make every non-DOBS-vocabulary value
    # already in the table (company_contract, promissory_note, and whatever
    # DMS writes next) unreadable again -- the exact bug this migration
    # exists to fix. Never do that; matches the sibling migration's stance.
    pass
