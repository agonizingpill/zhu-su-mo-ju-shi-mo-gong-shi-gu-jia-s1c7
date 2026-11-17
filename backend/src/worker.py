"""Computes an estimate release, writes its evidence artifact, then publishes atomically.

A release stays 'pending' until the fenced publish commits. Interruptions (artifact write
or result persistence) leave it pending and the recovery sweeper re-enqueues it, so the
same version is resumed and never published twice. Concurrent executors are fenced by the
estimate row lock plus the pending-status check: a late return from a previous executor
is discarded and cannot overwrite a newer publish.
"""
import os,time,json,logging,secrets
from pathlib import Path
from datetime import datetime,timezone,timedelta
import redis
from sqlalchemy import select
from .db import Session
from .models import Estimate,EstimateRelease
from .ml import predict_trial
from .release_service import QUEUE,utcnow

r=redis.Redis.from_url(os.environ['REDIS_URL'],decode_responses=True)
ART=Path('/artifacts'); ART.mkdir(exist_ok=True)
STALE_AFTER=30   # seconds before a pending release counts as abandoned
SWEEP_EVERY=10   # seconds between recovery sweeps

def locked_pair(s,release_id):
    """Lock estimate then release (consistent order everywhere) and verify this executor
    still owns a live calculation; otherwise the delivery is stale and must be dropped."""
    rel=s.get(EstimateRelease,release_id)
    if not rel: return None,None
    e=s.query(Estimate).filter_by(id=rel.estimate_id).with_for_update().one_or_none()
    if not e: return None,None
    rel=s.query(EstimateRelease).filter_by(id=release_id).with_for_update().execution_options(populate_existing=True).one()
    if rel.status!='pending' or e.status!='calculating' or e.active_release_id!=rel.id: return None,None
    return e,rel

def claim(release_id):
    with Session() as s:
        e,rel=locked_pair(s,release_id)
        if not e: return None
        rel.attempts+=1; rel.last_enqueued_at=utcnow(); s.commit()
        return {'eid':e.id,'rid':rel.id,'code':e.code,'version':rel.version,'setup':e.setup_hours,'spec':dict(rel.spec_snapshot)}

def fail(release_id,reason):
    with Session() as s:
        e,rel=locked_pair(s,release_id)
        if not e: return
        rel.status='failed'; rel.reason=reason[:590]; e.status='failed'; s.commit()

def publish(job,predicted,evidence,samples,artifact):
    with Session() as s:
        e,rel=locked_pair(s,job['rid'])
        if not e:
            Path(artifact).unlink(missing_ok=True)  # another executor won; drop the late artifact
            return False
        rel.status='published'; rel.predicted_hours=predicted
        rel.evidence=evidence; rel.sample_snapshot={'count':len(samples),'rows':samples}
        rel.artifact_path=str(artifact); rel.published_at=utcnow()
        e.predicted_hours=predicted; e.model_result=evidence; e.current_release_id=rel.id; e.status='estimated'
        s.commit(); return True

def handle(release_id):
    job=claim(release_id)
    if not job: return
    try:
        with Session() as s:
            predicted,evidence,samples=predict_trial(s,job['spec'],job['setup'])
    except ValueError as ex:
        fail(release_id,str(ex)); return
    payload={'estimate_code':job['code'],'estimate_id':job['eid'],'version':job['version'],'spec_snapshot':job['spec'],'sample_snapshot':{'count':len(samples),'rows':samples},'predicted_hours':predicted,'evidence':evidence}
    artifact=ART/f"estimate-{job['eid']}-v{job['version']}-{secrets.token_hex(4)}.json"
    tmp=ART/f".{artifact.name}.tmp"
    tmp.write_text(json.dumps(payload,ensure_ascii=False))  # failure here leaves the release pending for recovery
    os.replace(tmp,artifact)
    publish(job,predicted,evidence,samples,artifact)

def sweep():
    """Re-enqueue releases whose calculation was interrupted; retries reuse the same
    release row, so resuming never duplicates a published version."""
    cutoff=(datetime.now(timezone.utc)-timedelta(seconds=STALE_AFTER)).isoformat()
    due=[]
    with Session() as s:
        for rel in s.scalars(select(EstimateRelease).where(EstimateRelease.status=='pending')):
            e=s.query(Estimate).filter_by(id=rel.estimate_id).with_for_update().one_or_none()
            if not e or e.status!='calculating' or e.active_release_id!=rel.id:
                rel.status='voided'; rel.reason='估单状态已变更，本次计算作废'; continue
            if rel.last_enqueued_at and rel.last_enqueued_at>cutoff: continue
            rel.last_enqueued_at=utcnow(); due.append(rel.id)
        s.commit()
    for rid in due:
        try: r.rpush(QUEUE,rid)
        except Exception: logging.exception('re-enqueue failed')

last_sweep=0.0
while True:
    try:
        Path('/tmp/worker-heartbeat').touch()
        if time.monotonic()-last_sweep>SWEEP_EVERY:
            sweep(); last_sweep=time.monotonic()
        entry=r.blpop(QUEUE,timeout=2)
        if entry: handle(int(entry[1]))
    except Exception:
        logging.exception('worker loop error'); time.sleep(2)
