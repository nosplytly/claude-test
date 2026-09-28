from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from .money import D, plain
from .utils import utcnow


class DecText(TypeDecorator):
    """Exact Decimal stored as text (SQLite has no real decimal type)."""

    impl = String(80)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else plain(D(value))

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    balance_micro: Mapped[int] = mapped_column(BigInteger, default=0)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    bot_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    discount: Mapped[Decimal | None] = mapped_column(DecText)  # personal discount %, overrides CLIENT_DISCOUNT
    webhook_url: Mapped[str | None] = mapped_column(String(500))
    webhook_secret: Mapped[str | None] = mapped_column(String(80))

    @property
    def display_name(self) -> str:
        if self.username:
            return "@" + self.username
        return " ".join(x for x in (self.first_name, self.last_name) if x) or f"id{self.tg_id}"


class WebSession(Base):
    __tablename__ = "web_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))


class LoginRequest(Base):
    """Telegram deep-link login: site creates it, the bot confirms it."""

    __tablename__ = "login_requests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # token placed into t.me/bot?start=login_<id>
    browser_hash: Mapped[str] = mapped_column(String(64))  # sha256 of secret kept in browser cookie
    link_key_hash: Mapped[str] = mapped_column(String(64))  # sha256 of one-time key for the "back to site" button
    code: Mapped[str] = mapped_column(String(4))  # shown on site, picked in bot
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|confirmed|used|cancelled
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    link_used: Mapped[bool] = mapped_column(Boolean, default=False)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class Order(TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (Index("ux_order_external", "user_id", "external_id", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    source: Mapped[str | None] = mapped_column(String(8))  # web | api
    external_id: Mapped[str | None] = mapped_column(String(64))  # partner's own id (API idempotency)
    steam_login: Mapped[str] = mapped_column(String(64))
    currency: Mapped[str] = mapped_column(String(3))
    amount: Mapped[Decimal] = mapped_column(DecText)  # Steam nominal in `currency`
    fx_rate: Mapped[Decimal] = mapped_column(DecText)  # `currency` per 1 USD (nervixy rate at creation)
    nominal_micro: Mapped[int] = mapped_column(BigInteger)  # nominal in USD
    price_micro: Mapped[int] = mapped_column(BigInteger)  # what the customer pays, USD
    cost_micro: Mapped[int] = mapped_column(BigInteger)  # expected nervixy charge, USD
    client_discount: Mapped[Decimal] = mapped_column(DecText)
    supplier_discount: Mapped[Decimal] = mapped_column(DecText)
    method: Mapped[str | None] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(20), index=True, default="awaiting_payment")
    # awaiting_payment -> paid -> submitting -> processing -> delivered
    #                                        \-> queued (supplier balance low) -> submitting
    #                                        \-> uncertain (unknown submit outcome, needs reconcile/admin)
    #                              rejected (refunded to balance) | expired | cancelled
    charged_micro: Mapped[int] = mapped_column(BigInteger, default=0)  # debited from user balance for this order
    nervixy_order_id: Mapped[str | None] = mapped_column(String(64), index=True)
    nervixy_paid_micro: Mapped[int | None] = mapped_column(BigInteger)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    admin_alerted: Mapped[bool] = mapped_column(Boolean, default=False)
    admin_msgs: Mapped[str | None] = mapped_column(Text)  # JSON [[chat_id, message_id]]: the admin card, edited later


class Invoice(TimestampMixin, Base):
    __tablename__ = "invoices"
    __table_args__ = (Index("ix_invoice_match", "method", "units"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str | None] = mapped_column(String(16), index=True)  # set for balance deposits
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    method: Mapped[str] = mapped_column(String(24))
    address: Mapped[str] = mapped_column(String(128))
    amount: Mapped[Decimal] = mapped_column(DecText)  # exact crypto amount to send
    units: Mapped[str] = mapped_column(String(80))  # same in base units (integer as text; wei overflows int64)
    coin_price: Mapped[Decimal] = mapped_column(DecText)  # USD per coin used for this invoice
    usd_micro: Mapped[int] = mapped_column(BigInteger)  # USD value this invoice is meant to cover
    comment: Mapped[str | None] = mapped_column(String(64))  # memo for TON
    status: Mapped[str] = mapped_column(String(16), index=True, default="pending")
    # pending -> detected -> paid | partial ; pending -> expired | cancelled
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    reserved_until: Mapped[datetime] = mapped_column(DateTime)  # unique amount is not reused before this
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    received_micro: Mapped[int] = mapped_column(BigInteger, default=0)


class Transfer(TimestampMixin, Base):
    """Every incoming on-chain transfer to one of our addresses (matched or not)."""

    __tablename__ = "transfers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uid: Mapped[str] = mapped_column(String(200), unique=True)  # method:txid:idx
    method: Mapped[str] = mapped_column(String(24), index=True)
    txid: Mapped[str] = mapped_column(String(128), index=True)
    from_address: Mapped[str | None] = mapped_column(String(128))
    to_address: Mapped[str] = mapped_column(String(128))
    units: Mapped[str] = mapped_column(String(80))
    amount: Mapped[Decimal] = mapped_column(DecText)
    comment: Mapped[str | None] = mapped_column(String(256))
    block_number: Mapped[int | None] = mapped_column(BigInteger)
    block_time: Mapped[datetime | None] = mapped_column(DateTime)
    confirmations: Mapped[int] = mapped_column(Integer, default=0)
    final: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), index=True, default="new")
    # new -> matched -> credited ; new -> unmatched -> (claimed ->) credited ; ignored
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    credited_micro: Mapped[int | None] = mapped_column(BigInteger)
    note: Mapped[str | None] = mapped_column(Text)
    admin_alerted: Mapped[bool] = mapped_column(Boolean, default=False)


class LedgerEntry(Base):
    __tablename__ = "ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    delta_micro: Mapped[int] = mapped_column(BigInteger)
    balance_after_micro: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(16))  # deposit | order | refund | adjust
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    transfer_id: Mapped[int | None] = mapped_column(ForeignKey("transfers.id"))
    comment: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Claim(TimestampMixin, Base):
    """User says "I paid, here's my tx hash" when automatic matching failed."""

    __tablename__ = "claims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    txid: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(16), default="waiting")  # waiting | pending | approved | rejected
    transfer_id: Mapped[int | None] = mapped_column(ForeignKey("transfers.id"))
    note: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)


class ApiKey(Base):
    """Partner API key. Only a hash is stored; the key is shown once when created."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(16))  # first chars, to recognise the key in the UI
    allowed_ips: Mapped[str | None] = mapped_column(String(500))  # comma separated; empty = any
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_ip: Mapped[str | None] = mapped_column(String(64))


class WebhookEvent(Base):
    """Outgoing webhook to a partner (order finished, deposit credited), delivered with retries."""

    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    event: Mapped[str] = mapped_column(String(40))
    payload: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), index=True, default="pending")  # pending | sent | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class KV(Base):
    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
