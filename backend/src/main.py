import os,logging
from contextlib import asynccontextmanager
import redis
from fastapi import FastAPI,Depends,HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select,func
from sqlalchemy.exc import IntegrityError
from .db import Base,engine,db_session
from .models import User,Mould,Estimate,MachineSlot,TrialHistory,Audit,EstimateRelease
from .auth import current,writers,admin,verify,token
from .schemas import Login,MouldInput,EstimateInput,QuoteInput,SlotChoice,SlotInput
from .services import item,serialize,record,require_edit,locked_estimate,request_capacity,commit_capacity
from .release_service import QUEUE,ensure_columns,backfill_releases,open_calculation,invalidate_for_revision
from .middlewares import RequestTrace,BurstGuard
from .constants import ROLES,ESTIMATE_STATES,STEELS,RELEASE_STATES,EXECUTION_STATES,REVIEW_STATES,MAINTENANCE_STATES,RESIN_STATES,MATERIAL_STATES,DELIVERY_STATES
r=redis.Redis.from_url(os.environ['REDIS_URL'],decode_responses=True)
@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    ensure_columns()
    if os.getenv('SEED_DEMO','true').lower()=='true':
        try:
            from scripts.seed_demo import seed
            seed()
        except Exception: logging.exception('demo seed failed')
    backfill_releases()
    yield
app=FastAPI(title='锐模 · 试模工时估价',lifespan=lifespan)
from .execution_routes import router as execution_router
app.include_router(execution_router)
from .workshop_routes import router as workshop_router
app.include_router(workshop_router)
app.add_middleware(RequestTrace);app.add_middleware(BurstGuard)
app.add_middleware(CORSMiddleware,allow_origins=['http://localhost:18222'],allow_methods=['*'],allow_headers=['*'])
@app.exception_handler(IntegrityError)
async def conflict(req,exc):return JSONResponse({'detail':'编号重复或关联条件不满足，请核对后重试'},409)
@app.get('/health')
def health(s=Depends(db_session)):
    s.execute(select(1));r.ping();return {'status':'ok'}
@app.post('/auth/login')
def login(p:Login,s=Depends(db_session)):
    u=s.scalar(select(User).where(User.email==p.email))
    if not u or not verify(p.password,u.password_hash):raise HTTPException(401,'邮箱或口令不正确')
    return {'token':token(u),'user':{'email':u.email,'role':u.role}}
@app.get('/me')
def me(u=Depends(current)):return {'email':u.email,'role':u.role}
@app.get('/enums')
def enums(u=Depends(current)):return {'roles':ROLES,'estimate_states':ESTIMATE_STATES,'steels':STEELS,'release_states':RELEASE_STATES,'execution_states':EXECUTION_STATES,'review_states':REVIEW_STATES,'maintenance_states':MAINTENANCE_STATES,'resin_states':RESIN_STATES,'material_states':MATERIAL_STATES,'delivery_states':DELIVERY_STATES}
@app.get('/moulds')
def moulds(s=Depends(db_session),u=Depends(current)):return [serialize(x) for x in s.scalars(select(Mould).order_by(Mould.id))]
@app.post('/moulds')
def add_mould(p:MouldInput,s=Depends(db_session),u=Depends(writers)):
    m=Mould(**p.model_dump());s.add(m);record(s,u,'登记模具 '+p.code);s.commit();return serialize(m)
@app.put('/moulds/{mid}')
def edit_mould(mid:int,p:MouldInput,s=Depends(db_session),u=Depends(writers)):
    m=item(s,Mould,mid)
    # Revision is allowed even mid-calculation: old estimates and requests are invalidated,
    # stale releases voided (late worker results are rejected); committed estimates are kept.
    kept,voided=invalidate_for_revision(s,m.id)
    for k,v in p.model_dump().items():setattr(m,k,v)
    m.spec_version=Mould.spec_version+1
    record(s,u,f'修订模具 {p.code} 规格升版（作旧发布 {voided} 版，保留已承诺 {kept} 单）');s.commit();return serialize(m)
@app.get('/estimates')
def estimates(s=Depends(db_session),u=Depends(current)):return [serialize(x) for x in s.scalars(select(Estimate).order_by(Estimate.id.desc()))]
@app.post('/estimates')
def add_estimate(p:EstimateInput,s=Depends(db_session),u=Depends(writers)):
    item(s,Mould,p.mould_id);e=Estimate(**p.model_dump());s.add(e);record(s,u,'建立估价 '+p.code);s.commit();return serialize(e)
@app.patch('/estimates/{eid}/quote')
def quote(eid:int,p:QuoteInput,s=Depends(db_session),u=Depends(writers)):
    e=item(s,Estimate,eid);require_edit(e);e.quote_amount=p.quote_amount
    if e.status=='requested':e.status='estimated';e.slot_id=None
    record(s,u,'修订报价 '+e.code);s.commit();return serialize(e)
@app.post('/estimates/{eid}/calculate')
def calculate(eid:int,s=Depends(db_session),u=Depends(writers)):
    e=locked_estimate(s,eid)
    rel,created=open_calculation(s,e,u);s.commit()
    try:r.rpush(QUEUE,rel.id)
    except Exception:logging.exception('enqueue push failed; recovery sweeper will retry')
    return serialize(e)
@app.get('/estimates/{eid}/releases')
def releases(eid:int,s=Depends(db_session),u=Depends(current)):
    item(s,Estimate,eid)
    return [serialize(x) for x in s.scalars(select(EstimateRelease).where(EstimateRelease.estimate_id==eid).order_by(EstimateRelease.version.desc()))]
@app.post('/estimates/{eid}/request')
def request(eid:int,p:SlotChoice,s=Depends(db_session),u=Depends(writers)):
    e=locked_estimate(s,eid);request_capacity(s,e,p.slot_id);record(s,u,'申请机时 '+e.code);s.commit();return serialize(e)
@app.post('/estimates/{eid}/commit')
def commit(eid:int,s=Depends(db_session),u=Depends(admin)):
    e=commit_capacity(s,eid);record(s,u,'承诺排程 '+e.code);s.commit();return serialize(e)
@app.get('/slots')
def slots(s=Depends(db_session),u=Depends(current)):return [serialize(x) for x in s.scalars(select(MachineSlot).order_by(MachineSlot.id))]
@app.post('/slots')
def add_slot(p:SlotInput,s=Depends(db_session),u=Depends(admin)):
    v=MachineSlot(**p.model_dump(),reserved_hours=0);s.add(v);record(s,u,'开放机时 '+p.machine);s.commit();return serialize(v)
@app.get('/history')
def history(s=Depends(db_session),u=Depends(current)):return {'count':s.scalar(select(func.count()).select_from(TrialHistory)),'rows':[serialize(x) for x in s.scalars(select(TrialHistory).limit(20))]}
@app.get('/audits')
def audits(s=Depends(db_session),u=Depends(current)):return [serialize(x) for x in s.scalars(select(Audit).order_by(Audit.id.desc()).limit(30))]
