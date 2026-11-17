from sqlalchemy import String, Float, Integer, ForeignKey, JSON, CheckConstraint, UniqueConstraint, Index, text
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base
class User(Base):
    __tablename__='users'
    id: Mapped[int]=mapped_column(primary_key=True)
    email: Mapped[str]=mapped_column(String(120),unique=True)
    password_hash: Mapped[str]=mapped_column(String(180))
    role: Mapped[str]=mapped_column(String(20))
class Mould(Base):
    __tablename__='moulds'
    __table_args__=(CheckConstraint('cavities > 0 AND slides >= 0 AND weight_kg > 0'),)
    id: Mapped[int]=mapped_column(primary_key=True)
    code: Mapped[str]=mapped_column(String(40),unique=True)
    name: Mapped[str]=mapped_column(String(120))
    cavities: Mapped[int]=mapped_column(Integer)
    slides: Mapped[int]=mapped_column(Integer)
    steel: Mapped[str]=mapped_column(String(20))
    weight_kg: Mapped[float]=mapped_column(Float)
    spec_version: Mapped[int]=mapped_column(Integer,default=1)
class MachineSlot(Base):
    __tablename__='machine_slots'
    __table_args__=(UniqueConstraint('machine','start_hour'),CheckConstraint('capacity_hours > 0 AND reserved_hours >= 0 AND reserved_hours <= capacity_hours'),)
    id: Mapped[int]=mapped_column(primary_key=True)
    machine: Mapped[str]=mapped_column(String(40))
    start_hour: Mapped[str]=mapped_column(String(40))
    capacity_hours: Mapped[float]=mapped_column(Float)
    reserved_hours: Mapped[float]=mapped_column(Float,default=0)
class Estimate(Base):
    __tablename__='estimates'
    __table_args__=(CheckConstraint('rate > 0 AND setup_hours >= 0 AND quote_amount >= 0'),)
    id: Mapped[int]=mapped_column(primary_key=True)
    code: Mapped[str]=mapped_column(String(40),unique=True)
    mould_id: Mapped[int]=mapped_column(ForeignKey('moulds.id'))
    slot_id: Mapped[int|None]=mapped_column(ForeignKey('machine_slots.id'),nullable=True)
    rate: Mapped[float]=mapped_column(Float)
    setup_hours: Mapped[float]=mapped_column(Float)
    predicted_hours: Mapped[float|None]=mapped_column(Float,nullable=True)
    quote_amount: Mapped[float]=mapped_column(Float)
    status: Mapped[str]=mapped_column(String(20),default='draft')
    model_result: Mapped[dict|None]=mapped_column(JSON,nullable=True)
    current_release_id: Mapped[int|None]=mapped_column(Integer,nullable=True)
    active_release_id: Mapped[int|None]=mapped_column(Integer,nullable=True)
class EstimateRelease(Base):
    """Prediction, training-sample snapshot and explanation evidence publish as one fenced version."""
    __tablename__='estimate_releases'
    __table_args__=(UniqueConstraint('estimate_id','version'),
                    Index('uq_estimate_releases_pending','estimate_id',unique=True,postgresql_where=text("status='pending'")),
                    CheckConstraint('predicted_hours IS NULL OR predicted_hours > 0'),)
    id: Mapped[int]=mapped_column(primary_key=True)
    estimate_id: Mapped[int]=mapped_column(ForeignKey('estimates.id'))
    version: Mapped[int]=mapped_column(Integer)
    spec_version: Mapped[int]=mapped_column(Integer)
    spec_snapshot: Mapped[dict]=mapped_column(JSON)
    sample_snapshot: Mapped[dict|None]=mapped_column(JSON,nullable=True)
    predicted_hours: Mapped[float|None]=mapped_column(Float,nullable=True)
    evidence: Mapped[dict|None]=mapped_column(JSON,nullable=True)
    status: Mapped[str]=mapped_column(String(20),default='pending')
    reason: Mapped[str]=mapped_column(String(600),default='')
    artifact_path: Mapped[str|None]=mapped_column(String(200),nullable=True)
    attempts: Mapped[int]=mapped_column(Integer,default=0)
    created_at: Mapped[str]=mapped_column(String(40))
    published_at: Mapped[str|None]=mapped_column(String(40),nullable=True)
    last_enqueued_at: Mapped[str|None]=mapped_column(String(40),nullable=True)
class TrialHistory(Base):
    __tablename__='trial_history'
    id: Mapped[int]=mapped_column(primary_key=True)
    trial_code: Mapped[str]=mapped_column(String(40),unique=True)
    cavities: Mapped[int]=mapped_column(Integer)
    slides: Mapped[int]=mapped_column(Integer)
    steel: Mapped[str]=mapped_column(String(20))
    weight_kg: Mapped[float]=mapped_column(Float)
    actual_hours: Mapped[float]=mapped_column(Float)
class Audit(Base):
    __tablename__='audits'
    id: Mapped[int]=mapped_column(primary_key=True)
    actor: Mapped[str]=mapped_column(String(120))
    action: Mapped[str]=mapped_column(String(200))
    stamp: Mapped[str]=mapped_column(String(40))
