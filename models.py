# models.py
from datetime import datetime

from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    Boolean,
    ForeignKey,
    Text,
    UniqueConstraint,
)
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    full_name = Column(String, nullable=True)
    hashed_password = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    
    # NOVO CAMPO PARA VERIFICAÇÃO DE E-MAIL
    is_verified = Column(Boolean, default=False, nullable=False)

    # créditos para correção de redações
    credits = Column(Integer, default=0)

    # relacionamento com assinaturas (se quiser evoluir)
    subscriptions = relationship("Subscription", back_populates="user")

    # novo: relacionamento com redações
    essays = relationship(
        "Essay",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    essay_reviews = relationship(
        "EssayReview",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    mp_payments = relationship(
        "MercadoPagoPayment",
        back_populates="user",
        cascade="all, delete-orphan",
    )


class Plan(Base):
    __tablename__ = "plans"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    credits = Column(Integer, nullable=False, default=0)
    price_cents = Column(Integer, nullable=False, default=0)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=False)
    active = Column(Boolean, default=True)
    started_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="subscriptions")


class Essay(Base):
    """
    Redação corrigida do aluno.
    Guarda:
    - tema
    - tipo de entrada (texto ou arquivo)
    - texto (original ou extraído)
    - caminho do arquivo (quando for foto/PDF)
    - notas por competência + nota final
    - JSON bruto retornado pela IA
    """

    __tablename__ = "essays"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    tema = Column(String, nullable=False)
    input_type = Column(String, nullable=False)  # "texto" ou "arquivo"

    texto = Column(Text, nullable=True)         # texto da redação (digitado ou extraído)
    
    # Este campo agora salvará a URL completa (ex: do S3)
    arquivo_path = Column(String, nullable=True)  # caminho/URL do arquivo salvo (imagem/pdf)

    nota_final = Column(Integer, nullable=True)
    c1_nota = Column(Integer, nullable=True)
    c2_nota = Column(Integer, nullable=True)
    c3_nota = Column(Integer, nullable=True)
    c4_nota = Column(Integer, nullable=True)
    c5_nota = Column(Integer, nullable=True)

    # JSON bruto retornado pela IA (para reaproveitar no front)
    resultado_json = Column(Text, nullable=False)

    user = relationship("User", back_populates="essays")
    reviews = relationship(
        "EssayReview",
        back_populates="essay",
        cascade="all, delete-orphan",
    )


class EssayReview(Base):
    __tablename__ = "essay_reviews"
    __table_args__ = (
        UniqueConstraint("user_id", "essay_id", name="uq_essay_reviews_user_essay"),
    )

    id = Column(Integer, primary_key=True, index=True)
    essay_id = Column(Integer, ForeignKey("essays.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    stars = Column(Integer, nullable=False)
    comment = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    user = relationship("User", back_populates="essay_reviews")
    essay = relationship("Essay", back_populates="reviews")


class MercadoPagoPayment(Base):
    __tablename__ = "mercadopago_payments"

    id = Column(Integer, primary_key=True, index=True)
    payment_id = Column(String, unique=True, index=True, nullable=False)
    preference_id = Column(String, nullable=True)

    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    credits = Column(Integer, nullable=False, default=0)
    status = Column(String, nullable=True)
    status_detail = Column(String, nullable=True)
    credited = Column(Boolean, nullable=False, default=False)

    raw_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    user = relationship("User", back_populates="mp_payments")

class DemoKeyUsage(Base):
    __tablename__ = "demo_key_usage"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, unique=True, index=True, nullable=False)
    used = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
