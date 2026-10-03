"""Baseline: the schema as it was when migrations were introduced (2026-10-02).

A new database is created from here. A database made earlier by the app itself (create_all plus the small
"add missing columns" step) is brought to this schema and stamped at this revision by app.db.init_db.

Revision ID: 0001
Revises:
Created: 2026-10-02
"""
from alembic import op
import sqlalchemy as sa


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('kv',
    sa.Column('key', sa.String(length=100), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('promos',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('kind', sa.String(length=8), nullable=False),
    sa.Column('value', sa.String(length=80).with_variant(sa.Numeric(), "postgresql"), nullable=False),
    sa.Column('max_uses', sa.Integer(), nullable=True),
    sa.Column('used', sa.Integer(), nullable=False),
    sa.Column('per_user', sa.Integer(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_by', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('promos', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_promos_code'), ['code'], unique=True)

    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tg_id', sa.BigInteger(), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=True),
    sa.Column('first_name', sa.String(length=128), nullable=True),
    sa.Column('last_name', sa.String(length=128), nullable=True),
    sa.Column('balance_micro', sa.BigInteger(), nullable=False),
    sa.Column('is_banned', sa.Boolean(), nullable=False),
    sa.Column('bot_blocked', sa.Boolean(), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(), nullable=True),
    sa.Column('discount', sa.String(length=80).with_variant(sa.Numeric(), "postgresql"), nullable=True),
    sa.Column('webhook_url', sa.String(length=500), nullable=True),
    sa.Column('webhook_secret', sa.String(length=80), nullable=True),
    sa.Column('lang', sa.String(length=8), nullable=True),
    sa.Column('tg_lang', sa.String(length=16), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_users_tg_id'), ['tg_id'], unique=True)

    op.create_table('waitlist',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('topic', sa.String(length=32), nullable=False),
    sa.Column('tg_id', sa.BigInteger(), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=True),
    sa.Column('notified_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('waitlist', schema=None) as batch_op:
        batch_op.create_index('ux_waitlist_topic_tg', ['topic', 'tg_id'], unique=True)

    op.create_table('api_keys',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('key_hash', sa.String(length=64), nullable=False),
    sa.Column('prefix', sa.String(length=16), nullable=False),
    sa.Column('allowed_ips', sa.String(length=500), nullable=True),
    sa.Column('revoked', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('last_used_at', sa.DateTime(), nullable=True),
    sa.Column('last_ip', sa.String(length=64), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key_hash')
    )
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_api_keys_user_id'), ['user_id'], unique=False)

    op.create_table('login_requests',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('browser_hash', sa.String(length=64), nullable=False),
    sa.Column('link_key_hash', sa.String(length=64), nullable=False),
    sa.Column('code', sa.String(length=4), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('link_used', sa.Boolean(), nullable=False),
    sa.Column('ip', sa.String(length=64), nullable=True),
    sa.Column('user_agent', sa.String(length=300), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('orders',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('public_id', sa.String(length=16), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=8), nullable=True),
    sa.Column('external_id', sa.String(length=64), nullable=True),
    sa.Column('steam_login', sa.String(length=64), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('amount', sa.String(length=80).with_variant(sa.Numeric(), "postgresql"), nullable=False),
    sa.Column('fx_rate', sa.String(length=80).with_variant(sa.Numeric(), "postgresql"), nullable=False),
    sa.Column('nominal_micro', sa.BigInteger(), nullable=False),
    sa.Column('price_micro', sa.BigInteger(), nullable=False),
    sa.Column('cost_micro', sa.BigInteger(), nullable=False),
    sa.Column('client_discount', sa.String(length=80).with_variant(sa.Numeric(), "postgresql"), nullable=False),
    sa.Column('supplier_discount', sa.String(length=80).with_variant(sa.Numeric(), "postgresql"), nullable=False),
    sa.Column('method', sa.String(length=24), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('charged_micro', sa.BigInteger(), nullable=False),
    sa.Column('nervixy_order_id', sa.String(length=64), nullable=True),
    sa.Column('nervixy_paid_micro', sa.BigInteger(), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('next_check_at', sa.DateTime(), nullable=True),
    sa.Column('paid_at', sa.DateTime(), nullable=True),
    sa.Column('submitted_at', sa.DateTime(), nullable=True),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('admin_alerted', sa.Boolean(), nullable=False),
    sa.Column('admin_msgs', sa.Text(), nullable=True),
    sa.Column('promo_id', sa.Integer(), nullable=True),
    sa.Column('promo_pct', sa.String(length=80).with_variant(sa.Numeric(), "postgresql"), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_orders_nervixy_order_id'), ['nervixy_order_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_orders_public_id'), ['public_id'], unique=True)
        batch_op.create_index(batch_op.f('ix_orders_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_orders_user_id'), ['user_id'], unique=False)
        batch_op.create_index('ux_order_external', ['user_id', 'external_id'], unique=True)

    op.create_table('outbox',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('chat_id', sa.BigInteger(), nullable=True),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('markup', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(), nullable=False),
    sa.Column('last_error', sa.String(length=300), nullable=True),
    sa.Column('message_id', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('sent_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('outbox', schema=None) as batch_op:
        batch_op.create_index('ix_outbox_due', ['status', 'next_attempt_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_outbox_user_id'), ['user_id'], unique=False)

    op.create_table('promo_uses',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('promo_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('order_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['promo_id'], ['promos.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('promo_uses', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_promo_uses_order_id'), ['order_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_promo_uses_promo_id'), ['promo_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_promo_uses_user_id'), ['user_id'], unique=False)

    op.create_table('web_sessions',
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('ip', sa.String(length=64), nullable=True),
    sa.Column('user_agent', sa.String(length=300), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('token_hash')
    )
    with op.batch_alter_table('web_sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_web_sessions_user_id'), ['user_id'], unique=False)

    op.create_table('webhook_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('event', sa.String(length=40), nullable=False),
    sa.Column('payload', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(), nullable=False),
    sa.Column('last_error', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('webhook_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_webhook_events_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_webhook_events_user_id'), ['user_id'], unique=False)

    op.create_table('invoices',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('public_id', sa.String(length=16), nullable=True),
    sa.Column('order_id', sa.Integer(), nullable=True),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('method', sa.String(length=24), nullable=False),
    sa.Column('address', sa.String(length=128), nullable=False),
    sa.Column('amount', sa.String(length=80).with_variant(sa.Numeric(), "postgresql"), nullable=False),
    sa.Column('units', sa.String(length=80), nullable=False),
    sa.Column('coin_price', sa.String(length=80).with_variant(sa.Numeric(), "postgresql"), nullable=False),
    sa.Column('usd_micro', sa.BigInteger(), nullable=False),
    sa.Column('comment', sa.String(length=64), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('reserved_until', sa.DateTime(), nullable=False),
    sa.Column('paid_at', sa.DateTime(), nullable=True),
    sa.Column('received_micro', sa.BigInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.create_index('ix_invoice_match', ['method', 'units'], unique=False)
        batch_op.create_index(batch_op.f('ix_invoices_order_id'), ['order_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_invoices_public_id'), ['public_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_invoices_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_invoices_user_id'), ['user_id'], unique=False)

    op.create_table('transfers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uid', sa.String(length=200), nullable=False),
    sa.Column('method', sa.String(length=24), nullable=False),
    sa.Column('txid', sa.String(length=128), nullable=False),
    sa.Column('from_address', sa.String(length=128), nullable=True),
    sa.Column('to_address', sa.String(length=128), nullable=False),
    sa.Column('units', sa.String(length=80), nullable=False),
    sa.Column('amount', sa.String(length=80).with_variant(sa.Numeric(), "postgresql"), nullable=False),
    sa.Column('comment', sa.String(length=256), nullable=True),
    sa.Column('block_number', sa.BigInteger(), nullable=True),
    sa.Column('block_time', sa.DateTime(), nullable=True),
    sa.Column('confirmations', sa.Integer(), nullable=False),
    sa.Column('final', sa.Boolean(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('invoice_id', sa.Integer(), nullable=True),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('credited_micro', sa.BigInteger(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('admin_alerted', sa.Boolean(), nullable=False),
    sa.Column('xcheck_ok', sa.Boolean(), nullable=False),
    sa.Column('xcheck_tries', sa.Integer(), nullable=False),
    sa.Column('xcheck_since', sa.DateTime(), nullable=True),
    sa.Column('xcheck_after', sa.DateTime(), nullable=True),
    sa.Column('xcheck_alerted', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['invoice_id'], ['invoices.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uid')
    )
    with op.batch_alter_table('transfers', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_transfers_invoice_id'), ['invoice_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_transfers_method'), ['method'], unique=False)
        batch_op.create_index(batch_op.f('ix_transfers_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_transfers_txid'), ['txid'], unique=False)

    op.create_table('claims',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('order_id', sa.Integer(), nullable=True),
    sa.Column('txid', sa.String(length=128), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('transfer_id', sa.Integer(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('resolved_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ),
    sa.ForeignKeyConstraint(['transfer_id'], ['transfers.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('claims', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_claims_txid'), ['txid'], unique=False)
        batch_op.create_index(batch_op.f('ix_claims_user_id'), ['user_id'], unique=False)

    op.create_table('ledger',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('delta_micro', sa.BigInteger(), nullable=False),
    sa.Column('balance_after_micro', sa.BigInteger(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('order_id', sa.Integer(), nullable=True),
    sa.Column('transfer_id', sa.Integer(), nullable=True),
    sa.Column('comment', sa.String(length=300), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ),
    sa.ForeignKeyConstraint(['transfer_id'], ['transfers.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('ledger', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_ledger_user_id'), ['user_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('ledger', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ledger_user_id'))

    op.drop_table('ledger')
    with op.batch_alter_table('claims', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_claims_user_id'))
        batch_op.drop_index(batch_op.f('ix_claims_txid'))

    op.drop_table('claims')
    with op.batch_alter_table('transfers', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_transfers_txid'))
        batch_op.drop_index(batch_op.f('ix_transfers_status'))
        batch_op.drop_index(batch_op.f('ix_transfers_method'))
        batch_op.drop_index(batch_op.f('ix_transfers_invoice_id'))

    op.drop_table('transfers')
    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_invoices_user_id'))
        batch_op.drop_index(batch_op.f('ix_invoices_status'))
        batch_op.drop_index(batch_op.f('ix_invoices_public_id'))
        batch_op.drop_index(batch_op.f('ix_invoices_order_id'))
        batch_op.drop_index('ix_invoice_match')

    op.drop_table('invoices')
    with op.batch_alter_table('webhook_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_webhook_events_user_id'))
        batch_op.drop_index(batch_op.f('ix_webhook_events_status'))

    op.drop_table('webhook_events')
    with op.batch_alter_table('web_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_web_sessions_user_id'))

    op.drop_table('web_sessions')
    with op.batch_alter_table('promo_uses', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_promo_uses_user_id'))
        batch_op.drop_index(batch_op.f('ix_promo_uses_promo_id'))
        batch_op.drop_index(batch_op.f('ix_promo_uses_order_id'))

    op.drop_table('promo_uses')
    with op.batch_alter_table('outbox', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_outbox_user_id'))
        batch_op.drop_index('ix_outbox_due')

    op.drop_table('outbox')
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.drop_index('ux_order_external')
        batch_op.drop_index(batch_op.f('ix_orders_user_id'))
        batch_op.drop_index(batch_op.f('ix_orders_status'))
        batch_op.drop_index(batch_op.f('ix_orders_public_id'))
        batch_op.drop_index(batch_op.f('ix_orders_nervixy_order_id'))

    op.drop_table('orders')
    op.drop_table('login_requests')
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_api_keys_user_id'))

    op.drop_table('api_keys')
    with op.batch_alter_table('waitlist', schema=None) as batch_op:
        batch_op.drop_index('ux_waitlist_topic_tg')

    op.drop_table('waitlist')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_users_tg_id'))

    op.drop_table('users')
    with op.batch_alter_table('promos', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_promos_code'))

    op.drop_table('promos')
    op.drop_table('kv')
