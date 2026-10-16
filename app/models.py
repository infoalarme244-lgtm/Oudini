"""Modèle de données. Toute table métier porte un `shop_id` : c'est la base de l'isolation."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def token(n: int = 24) -> str:
    return secrets.token_urlsafe(n)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# ---------------------------------------------------------------- comptes / boutiques
class User(TimestampMixin, Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    memberships: Mapped[list["Membership"]] = relationship(back_populates="user", cascade="all, delete-orphan")


ROLES = ("owner", "admin", "staff", "viewer")
ROLE_RANK = {r: i for i, r in enumerate(reversed(ROLES))}  # viewer=0 … owner=3


class Membership(TimestampMixin, Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "shop_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20), default="owner")
    user: Mapped[User] = relationship(back_populates="memberships")
    shop: Mapped["Shop"] = relationship()


class Shop(TimestampMixin, Base):
    __tablename__ = "shops"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | published | paused
    default_language: Mapped[str] = mapped_column(String(8), default="fr")
    languages: Mapped[list] = mapped_column(JSON, default=lambda: ["fr"])
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    countries: Mapped[list] = mapped_column(JSON, default=lambda: ["FR"])
    preview_token: Mapped[str] = mapped_column(String(64), default=token)
    # Informations réelles du commerçant (pages légales) : jamais inventées.
    legal: Mapped[dict] = mapped_column(JSON, default=dict)
    # Paramètres de coûts pour le copilote de rentabilité.
    cost_settings: Mapped[dict] = mapped_column(JSON, default=dict)
    current_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    payment_mode: Mapped[str] = mapped_column(String(10), default="test")  # test | live
    ai_budget_usd: Mapped[float] = mapped_column(Float, default=5.0)
    ai_spent_usd: Mapped[float] = mapped_column(Float, default=0.0)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)


class Brief(TimestampMixin, Base):
    """Demande initiale du commerçant + hypothèses retenues (modifiables)."""
    __tablename__ = "briefs"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    prompt: Mapped[str] = mapped_column(Text, default="")
    competitor_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    extracted: Mapped[dict] = mapped_column(JSON, default=dict)
    assumptions: Mapped[list] = mapped_column(JSON, default=list)   # [{key,label,value,origin,editable}]
    questions: Mapped[list] = mapped_column(JSON, default=list)     # [{key,question,why,answer}]
    positioning_options: Mapped[list] = mapped_column(JSON, default=list)
    chosen_positioning: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")


class ShopVersion(TimestampMixin, Base):
    """Instantané versionné du contenu (thème, pages, collections, navigation, traductions)."""
    __tablename__ = "shop_versions"
    __table_args__ = (UniqueConstraint("shop_id", "number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)
    message: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(20), default="draft")  # proposed | draft | published | discarded
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)


# ---------------------------------------------------------------- catalogue
class Supplier(TimestampMixin, Base):
    __tablename__ = "suppliers"
    __table_args__ = (UniqueConstraint("shop_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    email: Mapped[str] = mapped_column(String(255), default="")
    lead_time_days: Mapped[int] = mapped_column(Integer, default=7)
    notes: Mapped[str] = mapped_column(Text, default="")


class SizeGuide(TimestampMixin, Base):
    __tablename__ = "size_guides"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    brand: Mapped[str] = mapped_column(String(80))
    model: Mapped[str] = mapped_column(String(160), default="")   # vide = guide générique de la marque
    vertical: Mapped[str] = mapped_column(String(40), default="")  # footwear, watches, jewelry… (vide = déduit du produit)
    measure_label: Mapped[str] = mapped_column(String(120), default="")  # « Tour de poignet (mm) »…
    allowance: Mapped[float | None] = mapped_column(Float, nullable=True)  # aisance ; vide = valeur du profil
    rows: Mapped[list] = mapped_column(JSON, default=list)         # [{size, eq, measure}] — measure = borne haute
    source_url: Mapped[str] = mapped_column(String(1000), default="")
    notes: Mapped[str] = mapped_column(Text, default="")


class Product(TimestampMixin, Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("shop_id", "handle"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    handle: Mapped[str] = mapped_column(String(160))
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    brand: Mapped[str] = mapped_column(String(80), default="")
    model: Mapped[str] = mapped_column(String(160), default="")
    category: Mapped[str] = mapped_column(String(80), default="")   # clé de verticale : footwear, watches, jewelry…
    usage: Mapped[str] = mapped_column(String(80), default="")      # sous-type : running, plongee, bague, sac…
    gender: Mapped[str] = mapped_column(String(20), default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)    # caractéristiques propres à la verticale (mouvement, métal, INCI…)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | draft | archived
    images: Mapped[list] = mapped_column(JSON, default=list)        # [{url, alt, license}]
    origin: Mapped[str] = mapped_column(Text, default="")           # provenance déclarée
    authenticity: Mapped[str] = mapped_column(Text, default="")     # justificatif (facture fournisseur, etc.)
    authenticity_evidence: Mapped[str] = mapped_column(String(1000), default="")
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    variants: Mapped[list["Variant"]] = relationship(back_populates="product", cascade="all, delete-orphan",
                                                     order_by="Variant.id")
    supplier: Mapped[Supplier | None] = relationship()


class Variant(TimestampMixin, Base):
    __tablename__ = "variants"
    __table_args__ = (UniqueConstraint("shop_id", "sku"),
                      CheckConstraint("stock >= 0", name="stock_non_negative"),
                      CheckConstraint("reserved >= 0", name="reserved_non_negative"),
                      CheckConstraint("reserved <= stock", name="reserved_le_stock"))
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    sku: Mapped[str] = mapped_column(String(80))
    size: Mapped[str] = mapped_column(String(20), default="")
    color: Mapped[str] = mapped_column(String(60), default="")
    price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)  # TTC
    cost_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)   # coût d'achat HT
    stock: Mapped[int] = mapped_column(Integer, default=0)
    reserved: Mapped[int] = mapped_column(Integer, default=0)
    reorder_point: Mapped[int] = mapped_column(Integer, default=2)
    product: Mapped[Product] = relationship(back_populates="variants")

    @property
    def available(self) -> int:
        return max(0, self.stock - self.reserved)

    @property
    def label(self) -> str:
        return " / ".join(x for x in (self.color, self.size if self.size and self.size != "Unique" else "") if x)


class StockAlert(TimestampMixin, Base):
    __tablename__ = "stock_alerts"
    __table_args__ = (UniqueConstraint("variant_id", "email"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(255))
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ---------------------------------------------------------------- clients / commandes
class Customer(TimestampMixin, Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("shop_id", "email"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(160), default="")
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    marketing_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consent_source: Mapped[str] = mapped_column(String(120), default="")
    unsubscribed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    unsubscribe_token: Mapped[str] = mapped_column(String(64), default=token)


class Cart(TimestampMixin, Base):
    __tablename__ = "carts"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, default=token)
    items: Mapped[list] = mapped_column(JSON, default=list)  # [{variant_id, qty}]
    discount_code: Mapped[str] = mapped_column(String(60), default="")


ORDER_STATUSES = ("pending_payment", "paid", "payment_failed", "cancelled", "fulfilled",
                  "partially_refunded", "refunded")


class Order(TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("shop_id", "number"), UniqueConstraint("shop_id", "idempotency_key"))
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    number: Mapped[str] = mapped_column(String(30))
    public_token: Mapped[str] = mapped_column(String(64), default=token)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True)
    email: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(30), default="pending_payment")
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    subtotal_cents: Mapped[int] = mapped_column(Integer, default=0)
    discount_cents: Mapped[int] = mapped_column(Integer, default=0)
    shipping_cents: Mapped[int] = mapped_column(Integer, default=0)
    tax_cents: Mapped[int] = mapped_column(Integer, default=0)   # TVA incluse dans le total
    total_cents: Mapped[int] = mapped_column(Integer, default=0)
    refunded_cents: Mapped[int] = mapped_column(Integer, default=0)
    country: Mapped[str] = mapped_column(String(2), default="FR")
    address: Mapped[dict] = mapped_column(JSON, default=dict)
    shipping_method: Mapped[dict] = mapped_column(JSON, default=dict)
    discount_code: Mapped[str] = mapped_column(String(60), default="")
    idempotency_key: Mapped[str] = mapped_column(String(80))
    payment_mode: Mapped[str] = mapped_column(String(10), default="test")
    provider: Mapped[str] = mapped_column(String(20), default="simulator")
    provider_ref: Mapped[str] = mapped_column(String(255), default="")
    channel: Mapped[str] = mapped_column(String(60), default="direct")
    stock_state: Mapped[str] = mapped_column(String(20), default="reserved")  # reserved | committed | released
    reservation_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    tracking_number: Mapped[str] = mapped_column(String(120), default="")
    used_size_advisor: Mapped[bool] = mapped_column(Boolean, default=False)
    status_history: Mapped[list] = mapped_column(JSON, default=list)
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan")
    refunds: Mapped[list["Refund"]] = relationship(back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    variant_id: Mapped[int | None] = mapped_column(ForeignKey("variants.id", ondelete="SET NULL"), nullable=True)
    product_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(255))
    variant_label: Mapped[str] = mapped_column(String(120), default="")
    sku: Mapped[str] = mapped_column(String(80), default="")
    qty: Mapped[int] = mapped_column(Integer)
    unit_price_cents: Mapped[int] = mapped_column(Integer)
    unit_cost_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    order: Mapped[Order] = relationship(back_populates="items")


class PaymentEvent(Base):
    """Journal des événements prestataires : l'unicité (provider, event_id) empêche tout double traitement."""
    __tablename__ = "payment_events"
    __table_args__ = (UniqueConstraint("provider", "event_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(20))
    event_id: Mapped[str] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(80))
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[str] = mapped_column(String(255), default="")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Refund(TimestampMixin, Base):
    __tablename__ = "refunds"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | succeeded | failed
    provider_ref: Mapped[str] = mapped_column(String(255), default="")
    restock: Mapped[bool] = mapped_column(Boolean, default=True)
    items: Mapped[list] = mapped_column(JSON, default=list)  # [{order_item_id, qty}]
    order: Mapped[Order] = relationship(back_populates="refunds")


class ReturnRequest(TimestampMixin, Base):
    __tablename__ = "return_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    reason: Mapped[str] = mapped_column(String(40), default="other")  # size | defect | changed_mind | other
    comment: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="requested")  # requested | received | refunded | rejected


class DiscountCode(TimestampMixin, Base):
    __tablename__ = "discount_codes"
    __table_args__ = (UniqueConstraint("shop_id", "code"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(60))
    kind: Mapped[str] = mapped_column(String(20), default="percent")  # percent | fixed | free_shipping
    value: Mapped[int] = mapped_column(Integer, default=0)  # % ou centimes
    min_subtotal_cents: Mapped[int] = mapped_column(Integer, default=0)
    usage_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ShippingZone(TimestampMixin, Base):
    __tablename__ = "shipping_zones"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    countries: Mapped[list] = mapped_column(JSON, default=list)
    # [{id, name, carrier, price_cents, free_over_cents, min_days, max_days, cost_cents}]
    rates: Mapped[list] = mapped_column(JSON, default=list)


class TaxSetting(TimestampMixin, Base):
    __tablename__ = "tax_settings"
    __table_args__ = (UniqueConstraint("shop_id", "country"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    country: Mapped[str] = mapped_column(String(2))
    rate_bp: Mapped[int] = mapped_column(Integer)  # 2100 = 21 %
    prices_include_tax: Mapped[bool] = mapped_column(Boolean, default=True)
    validated: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(String(500), default="")


class PaymentAccount(TimestampMixin, Base):
    __tablename__ = "payment_accounts"
    __table_args__ = (UniqueConstraint("shop_id", "provider", "mode"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(20))  # stripe | simulator
    mode: Mapped[str] = mapped_column(String(10))      # test | live
    status: Mapped[str] = mapped_column(String(30), default="not_connected")
    secret_enc: Mapped[str] = mapped_column(Text, default="")
    webhook_secret_enc: Mapped[str] = mapped_column(Text, default="")
    publishable_key: Mapped[str] = mapped_column(String(255), default="")
    account_label: Mapped[str] = mapped_column(String(255), default="")
    methods: Mapped[list] = mapped_column(JSON, default=list)


# ---------------------------------------------------------------- analyse concurrentielle
class CompetitorAnalysis(TimestampMixin, Base):
    __tablename__ = "competitor_analyses"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    brief_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | running | done | failed
    selection_criteria: Mapped[list] = mapped_column(JSON, default=list)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    competitors: Mapped[list["Competitor"]] = relationship(back_populates="analysis", cascade="all, delete-orphan",
                                                           order_by="Competitor.id")


class Competitor(TimestampMixin, Base):
    __tablename__ = "competitors"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("competitor_analyses.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    url: Mapped[str] = mapped_column(String(1000))
    origin: Mapped[str] = mapped_column(String(40), default="merchant")  # merchant | candidate_list | ai_search
    selection_reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | analysed | blocked | failed
    error: Mapped[str] = mapped_column(Text, default="")
    profile: Mapped[dict] = mapped_column(JSON, default=dict)  # synthèse structurée
    security_flags: Mapped[list] = mapped_column(JSON, default=list)
    analysis: Mapped[CompetitorAnalysis] = relationship(back_populates="competitors")
    facts: Mapped[list["SourcedFact"]] = relationship(cascade="all, delete-orphan", order_by="SourcedFact.id")
    financials: Mapped[list["FinancialFigure"]] = relationship(cascade="all, delete-orphan")


FACT_STATUSES = ("verified", "observed", "estimated", "unavailable")


class SourcedFact(TimestampMixin, Base):
    """Fait observé : toujours une URL source et une date de collecte."""
    __tablename__ = "sourced_facts"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id", ondelete="CASCADE"), index=True)
    dimension: Mapped[str] = mapped_column(String(40))  # positioning, pricing, shipping, returns, payment, seo, social, mobile, ux, reviews, ads
    label: Mapped[str] = mapped_column(String(160))
    value: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(String(1000))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    method: Mapped[str] = mapped_column(String(40), default="html_extraction")
    status: Mapped[str] = mapped_column(String(20), default="observed")
    confidence: Mapped[str] = mapped_column(String(10), default="medium")


class FinancialFigure(TimestampMixin, Base):
    __tablename__ = "financial_figures"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id", ondelete="CASCADE"), index=True)
    metric: Mapped[str] = mapped_column(String(40))  # revenue | net_income | online_revenue | operating_income
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    source_url: Mapped[str] = mapped_column(String(1000), default="")
    source_title: Mapped[str] = mapped_column(String(255), default="")
    published_on: Mapped[str] = mapped_column(String(20), default="")
    period: Mapped[str] = mapped_column(String(60), default="")
    legal_entity: Mapped[str] = mapped_column(String(255), default="")
    scope: Mapped[str] = mapped_column(String(255), default="")  # ex. « groupe consolidé », « filiale France »
    status: Mapped[str] = mapped_column(String(20), default="unavailable")  # verified | estimated | unavailable
    method: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[str] = mapped_column(String(10), default="low")


class Simulation(TimestampMixin, Base):
    __tablename__ = "simulations"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    competitor_id: Mapped[int | None] = mapped_column(ForeignKey("competitors.id", ondelete="CASCADE"), nullable=True)
    name: Mapped[str] = mapped_column(String(160))
    traffic_source: Mapped[str] = mapped_column(Text, default="")
    scenarios: Mapped[list] = mapped_column(JSON, default=list)


class ReviewInsight(TimestampMixin, Base):
    __tablename__ = "review_insights"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id", ondelete="CASCADE"), index=True)
    source_url: Mapped[str] = mapped_column(String(1000))
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    themes: Mapped[dict] = mapped_column(JSON, default=dict)  # {theme: {neg, pos, examples}}


class Recommendation(TimestampMixin, Base):
    __tablename__ = "recommendations"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    analysis_id: Mapped[int | None] = mapped_column(ForeignKey("competitor_analyses.id", ondelete="CASCADE"), nullable=True)
    title: Mapped[str] = mapped_column(String(255))
    rationale: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, default="")
    impact: Mapped[str] = mapped_column(String(10))
    effort: Mapped[str] = mapped_column(String(10))
    confidence: Mapped[str] = mapped_column(String(10))
    score: Mapped[float] = mapped_column(Float, default=0)
    evidence: Mapped[list] = mapped_column(JSON, default=list)  # [{competitor, url, fact}]
    status: Mapped[str] = mapped_column(String(20), default="proposed")


# ---------------------------------------------------------------- gouvernance
ENGAGING_ACTIONS = {
    "publish_store": "Publication de la boutique",
    "enable_live_payments": "Activation des paiements réels",
    "buy_domain": "Achat d'un domaine",
    "launch_paid_campaign": "Lancement d'une campagne payante",
    "send_marketing": "Envoi d'une communication marketing",
    "bulk_price_change": "Modification massive des prix",
}


class ApprovalRequest(TimestampMixin, Base):
    __tablename__ = "approval_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(40))
    summary: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | approved | rejected | executed | failed
    requested_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decided_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    auto_permission_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result: Mapped[str] = mapped_column(Text, default="")


class AutoPermission(TimestampMixin, Base):
    __tablename__ = "auto_permissions"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(40))
    limits: Mapped[dict] = mapped_column(JSON, default=dict)  # {max_change_pct, max_products, max_recipients, max_budget_cents}
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(80))
    entity: Mapped[str] = mapped_column(String(60), default="")
    entity_id: Mapped[str] = mapped_column(String(60), default="")
    before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AiTrace(Base):
    """Traçabilité IA / analyses : sources, hypothèses, dates, sorties, coût."""
    __tablename__ = "ai_traces"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    feature: Mapped[str] = mapped_column(String(60))
    engine: Mapped[str] = mapped_column(String(60))  # "rules" ou nom du modèle
    input_summary: Mapped[str] = mapped_column(Text, default="")
    sources: Mapped[list] = mapped_column(JSON, default=list)
    assumptions: Mapped[list] = mapped_column(JSON, default=list)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued | running | done | failed
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    run_after: Mapped[datetime] = mapped_column(DateTime, default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ---------------------------------------------------------------- croissance
class StoreEvent(Base):
    __tablename__ = "store_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    type: Mapped[str] = mapped_column(String(30))  # view_product | add_to_cart | begin_checkout | purchase | exposure
    product_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    experiment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    arm: Mapped[str] = mapped_column(String(20), default="")
    channel: Mapped[str] = mapped_column(String(60), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ChannelSpend(TimestampMixin, Base):
    __tablename__ = "channel_spend"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(60))
    month: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    spend_cents: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(255), default="saisie commerçant")


class Experiment(TimestampMixin, Base):
    __tablename__ = "experiments"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    hypothesis: Mapped[str] = mapped_column(Text, default="")
    target: Mapped[str] = mapped_column(String(40), default="product_title")  # product_title | hero_title
    product_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    arms: Mapped[list] = mapped_column(JSON, default=list)  # [{key:"A", value:"…"}, …]
    metric: Mapped[str] = mapped_column(String(40), default="add_to_cart_rate")
    min_sample_per_arm: Mapped[int] = mapped_column(Integer, default=400)
    min_days: Mapped[int] = mapped_column(Integer, default=7)
    alpha: Mapped[float] = mapped_column(Float, default=0.05)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | running | stopped
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MarketingDraft(TimestampMixin, Base):
    __tablename__ = "marketing_drafts"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # email | seo | social | paid_campaign
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(String(60), default="")
    budget_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | pending_approval | approved | sent
    sent_count: Mapped[int] = mapped_column(Integer, default=0)


class EmailOutbox(TimestampMixin, Base):
    __tablename__ = "email_outbox"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    to: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(30), default="transactional")
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued | sent | failed | held
    headers: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    attempts: Mapped[int | None] = mapped_column(Integer, default=0, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(20), nullable=True)
    provider_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WatchTarget(TimestampMixin, Base):
    __tablename__ = "watch_targets"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(160))
    url: Mapped[str] = mapped_column(String(1000))
    my_product_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    frequency_hours: Mapped[int] = mapped_column(Integer, default=24)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class WatchAlert(TimestampMixin, Base):
    __tablename__ = "watch_alerts"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("watch_targets.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(30))
    old_value: Mapped[str] = mapped_column(String(255), default="")
    new_value: Mapped[str] = mapped_column(String(255), default="")
    source_url: Mapped[str] = mapped_column(String(1000))
    suggestion: Mapped[dict] = mapped_column(JSON, default=dict)
    read: Mapped[bool] = mapped_column(Boolean, default=False)


class SupportTicket(TimestampMixin, Base):
    __tablename__ = "support_tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(255), default="")
    question: Mapped[str] = mapped_column(Text)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="open")


class SizeRecommendation(TimestampMixin, Base):
    __tablename__ = "size_recommendations"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(Integer)
    session_id: Mapped[str] = mapped_column(String(64), default="")
    measure: Mapped[float] = mapped_column(Float)
    recommended: Mapped[str] = mapped_column(String(20), default="")
    confidence: Mapped[str] = mapped_column(String(10), default="low")
