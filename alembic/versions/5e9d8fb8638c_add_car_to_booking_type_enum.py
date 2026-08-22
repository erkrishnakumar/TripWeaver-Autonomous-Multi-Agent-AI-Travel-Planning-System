"""add car to booking_type enum

Revision ID: 5e9d8fb8638c
Revises: 4bedf8413dbb
Create Date: 2026-08-22 21:48:51.098288

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5e9d8fb8638c"
down_revision: str | Sequence[str] | None = "4bedf8413dbb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE booking_type ADD VALUE 'car'")


def downgrade() -> None:
    # Postgres cannot remove a single enum value directly. A downgrade
    # would require rebuilding the type (rename old, create new without
    # 'car', migrate the column, drop old) - not implemented since this
    # project has no rows using 'car' yet to migrate away from.
    raise NotImplementedError(
        "Removing an enum value requires manually rebuilding the booking_type "
        "type in Postgres - not implemented."
    )
