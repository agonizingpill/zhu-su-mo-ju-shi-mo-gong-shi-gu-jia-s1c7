"""Estimate releases: prediction, training-sample snapshot and explanation evidence
are published as one complete fenced version. Revision voids stale releases; recovery
re-enqueues interrupted calculations without duplicating a version."""
import logging
from datetime import datetime, timezone
from fastapi import HTTPException
from sqlalchemy import select, func, text, inspect
from .db import engine, Session
from .models import Estimate, Mould, EstimateRelease
from .services import record

QUEUE='mould-estimates'
def utcnow(): return datetime.now(timezone.utc).isoformat()

def ensure_columns():
    """Non-destructive column upgrades for databases created before releases existed."""
    with engine.begin() as c:
        insp=inspect(c)
        tables=set(insp.get_table_names())
        if 'moulds' in tables and 'spec_version' not in {x['name'] for x in insp.get_columns('moulds')}:
            c.execute(text('ALTER TABLE moulds ADD COLUMN spec_version INTEGER NOT NULL DEFAULT 1'))
        if 'estimates' in tables:
            cols={x['name'] for x in insp.get_columns('estimates')}
            if 'current_release_id' not in cols: c.execute(text('ALTER TABLE estimates ADD COLUMN current_release_id INTEGER'))
            if 'active_release_id' not in cols: c.execute(text('ALTER TABLE estimates ADD COLUMN active_release_id INTEGER'))

def spec_snapshot(m):
    return {'mould_code':m.code,'name':m.name,'cavities':m.cavities,'slides':m.slides,'steel':m.steel,'weight_kg':m.weight_kg,'spec_version':m.spec_version}

def open_calculation(s,e,u):
    """Idempotent enqueue: a calculating estimate resumes its active pending release
    instead of opening a duplicate version. Returns (release, created)."""
    if e.status=='committed': raise HTTPException(409,'已承诺的估价不能修改')
    if e.status=='requested': raise HTTPException(409,'已申请机时，请先修改报价撤回申请')
    if e.status=='calculating':
        rel=s.get(EstimateRelease,e.active_release_id) if e.active_release_id else None
        if rel and rel.status=='pending':
            rel.last_enqueued_at=utcnow(); record(s,u,'恢复估算 '+e.code+' v'+str(rel.version))
            return rel,False
        e.status='failed'  # inconsistent calculating state: fall through and open a fresh version
    m=s.get(Mould,e.mould_id)
    version=(s.scalar(select(func.max(EstimateRelease.version)).where(EstimateRelease.estimate_id==e.id)) or 0)+1
    rel=EstimateRelease(estimate_id=e.id,version=version,spec_version=m.spec_version,spec_snapshot=spec_snapshot(m),status='pending',created_at=utcnow(),last_enqueued_at=utcnow())
    s.add(rel); s.flush()
    e.status='calculating'; e.predicted_hours=None; e.model_result=None; e.active_release_id=rel.id
    record(s,u,'估算入队 '+e.code+' v'+str(version))
    return rel,True

def invalidate_for_revision(s,mould_id):
    """Spec revision invalidates old estimates and requests; pending/published releases of
    non-committed estimates are voided so late computation results are rejected. Committed
    estimates and their actual-performance basis are retained. Locks estimates (id order)
    before the mould row is bumped, matching the estimate→mould lock order elsewhere."""
    kept=voided=0
    estimates=s.scalars(select(Estimate).where(Estimate.mould_id==mould_id).order_by(Estimate.id).with_for_update()).all()
    for e in estimates:
        if e.status=='committed': kept+=1; continue
        e.status='draft'; e.predicted_hours=None; e.model_result=None; e.slot_id=None
        e.current_release_id=None; e.active_release_id=None
        for rel in s.scalars(select(EstimateRelease).where(EstimateRelease.estimate_id==e.id,EstimateRelease.status.in_(['pending','published']))):
            rel.status='voided'; rel.reason='模具规格修订，本次发布作废'; voided+=1
    return kept,voided

def backfill_releases():
    """Legacy estimates holding a bare prediction get a published v1 release so the
    request/commit invariant (complete published version only) holds for old rows."""
    with Session() as s:
        n=0
        for e in s.scalars(select(Estimate).where(Estimate.predicted_hours.isnot(None),Estimate.current_release_id.is_(None))):
            m=s.get(Mould,e.mould_id)
            version=(s.scalar(select(func.max(EstimateRelease.version)).where(EstimateRelease.estimate_id==e.id)) or 0)+1
            rel=EstimateRelease(estimate_id=e.id,version=version,spec_version=m.spec_version if m else 1,
                spec_snapshot=spec_snapshot(m) if m else {},sample_snapshot={'count':0,'rows':[],'note':'历史估价回溯补录，样本快照不可得'},
                predicted_hours=e.predicted_hours,evidence=e.model_result or {'note':'历史估价回溯补录'},
                status='published',created_at=utcnow(),published_at=utcnow(),attempts=1)
            s.add(rel); s.flush(); e.current_release_id=rel.id; n+=1
        if n: logging.info('backfilled %s legacy estimate releases',n)
        s.commit()
