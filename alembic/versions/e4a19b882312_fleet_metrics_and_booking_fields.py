"""fleet metrics and booking fields

Revision ID: e4a19b882312
Revises: 996eb08d5559
Create Date: 2026-09-17 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'e4a19b882312'
down_revision: Union[str, None] = '996eb08d5559'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('vehicles', sa.Column('battery_level_pct', sa.Integer(), nullable=True))
    op.add_column('vehicles', sa.Column('odometer_km', sa.Integer(), nullable=True))
    op.add_column('vehicles', sa.Column('next_maintenance_date', sa.Date(), nullable=True))

    op.add_column('bookings', sa.Column('payment_method', sa.String(length=32), nullable=True))
    op.add_column('bookings', sa.Column('payment_reference', sa.String(length=128), nullable=True))
    op.add_column('bookings', sa.Column('rescheduled_from_booking_id', sa.UUID(), nullable=True))
    op.add_column('bookings', sa.Column('cancellation_reason', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('bookings', 'cancellation_reason')
    op.drop_column('bookings', 'rescheduled_from_booking_id')
    op.drop_column('bookings', 'payment_reference')
    op.drop_column('bookings', 'payment_method')
    op.drop_column('vehicles', 'next_maintenance_date')
    op.drop_column('vehicles', 'odometer_km')
    op.drop_column('vehicles', 'battery_level_pct')
