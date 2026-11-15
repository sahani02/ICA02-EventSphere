import asyncio, os, secrets, hashlib, hmac, json, io, base64
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from typing import Literal
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import FileResponse
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, MetaData, Table, Column, Integer, String, Text, Boolean, ForeignKey, select, update, insert, func
import qrcode

UTC = timezone.utc
now = lambda: datetime.now(UTC)
def stamp(t=None): return (t or now()).astimezone(UTC).isoformat(timespec='seconds')
def dt(s):
    d = datetime.fromisoformat(s.replace('Z','+00:00'))
    if d.tzinfo is None: raise ValueError('Time zone required')
    return d.astimezone(UTC)

def password_hash(p):
    salt = secrets.token_hex(16)
    return salt + ':' + hashlib.pbkdf2_hmac('sha256', p.encode(), bytes.fromhex(salt), 300000).hex()
def password_ok(p, stored):
    salt, hashed = stored.split(':')
    return hmac.compare_digest(hashed, hashlib.pbkdf2_hmac('sha256', p.encode(), bytes.fromhex(salt), 300000).hex())

if os.getenv('RENDER') and not all(os.getenv(k) for k in ['DATABASE_URL','SECRET_KEY','DEMO_PASSWORD']):
    raise RuntimeError('Render requires DATABASE_URL, SECRET_KEY and DEMO_PASSWORD')
url = os.getenv('DATABASE_URL','sqlite:///eventsphere.db')
if url.startswith('postgres://'): url = url.replace('postgres://','postgresql+psycopg://',1)
if url.startswith('postgresql://'): url = url.replace('postgresql://','postgresql+psycopg://',1)
engine = create_engine(url, pool_pre_ping=True, **({'connect_args':{'check_same_thread':False,'timeout':30}} if url.startswith('sqlite') else {}))
m = MetaData()
def tab(name,*cols): return Table(name,m,Column('id',Integer,primary_key=True),*cols)
def S(n, default=None): return Column(n,String(500),nullable=False,default=default)
def I(n, default=0): return Column(n,Integer,nullable=False,default=default)
def FK(n,t): return Column(n,Integer,ForeignKey(t+'.id'),nullable=False)
users=tab('users',Column('email',String(250),unique=True,nullable=False),S('password'),S('role'))
venues=tab('venues',S('name'),S('location'),I('capacity'),S('description',''),S('image',''),I('rate'),Column('hire_open',Boolean,nullable=False,default=True))
events=tab('events',S('title'),S('category'),FK('venue_id','venues'),S('start'),S('end'),S('description',''),S('status','draft'))
categories=tab('categories',FK('event_id','events'),S('name'),I('price'),I('quantity'),I('sold'),I('held'))
bookings=tab('bookings',FK('user_id','users'),FK('category_id','categories'),I('quantity'),I('total'),S('payment_ref'),S('status','confirmed'),I('refund'))
tickets=tab('tickets',FK('booking_id','bookings'),FK('event_id','events'),Column('code',String(100),unique=True,nullable=False),S('status','valid'),I('scans'),S('used_at',''))
hires=tab('hires',FK('user_id','users'),FK('venue_id','venues'),S('start'),S('end'),S('purpose'),I('guests'),I('price'),S('status'))
waitlist=tab('waitlist',FK('user_id','users'),FK('category_id','categories'),I('quantity'),S('status','waiting'),S('expires',''))
notifications=tab('notifications',FK('user_id','users'),S('text'),S('created'))
logs=tab('messages',S('sender'),S('receiver'),S('performative'),Column('content',Text,nullable=False),S('conversation_id'),S('created'))
mutex=tab('allocation_mutex',I('version'))

def rows(c,t,*filters): return [dict(r) for r in c.execute(select(t).where(*filters).order_by(t.c.id)).mappings()]
def one(c,t,i): return c.execute(select(t).where(t.c.id==i)).mappings().first()
def add(c,t,**kw): return c.execute(insert(t).values(**kw)).inserted_primary_key[0]
def change(c,t,i,**kw): c.execute(update(t).where(t.c.id==i).values(**kw))
def lock(c): c.execute(update(mutex).where(mutex.c.id==1).values(version=mutex.c.version+1))

class Message(BaseModel):
    sender:str; receiver:str
    performative:Literal['REQUEST','AGREE','REFUSE','PROPOSE','INFORM','FAILURE']='REQUEST'
    content:dict
    conversation_id:str=Field(default_factory=lambda:secrets.token_hex(12))

def result(p='AGREE',**data): return {'performative':p,**data}
def refuse(reason): return result('REFUSE',reason=reason)
class Bus:
    def __init__(self): self.agents={}
    def send(self,c,msg):
        msg=Message.model_validate_json(msg.model_dump_json())
        self.log(c,msg)
        agent=self.agents[msg.receiver]
        response=agent.handle(c,msg,self)
        reply=Message(sender=msg.receiver,receiver=msg.sender,performative=response['performative'],content={k:v for k,v in response.items() if k!='performative'},conversation_id=msg.conversation_id)
        self.log(c,reply)
        return response
    def log(self,c,msg): add(c,logs,sender=msg.sender,receiver=msg.receiver,performative=msg.performative,content=json.dumps(msg.content),conversation_id=msg.conversation_id,created=stamp())
    def ask(self,c,parent,receiver,action,**data): return self.send(c,Message(sender=parent.receiver,receiver=receiver,content={'action':action,**data},conversation_id=parent.conversation_id))
    def inform(self,c,parent,user_id,text):
        broadcast=Message(sender=parent.receiver,receiver='*',performative='INFORM',content={'user_id':user_id,'text':text},conversation_id=parent.conversation_id)
        self.log(c,broadcast)
        for subscriber in ['NotificationAgent']:
            self.send(c,broadcast.model_copy(update={'receiver':subscriber}))

class VenueAgent:
    goal='Avoid scheduling conflicts and price venue hire'
    buffer=timedelta(minutes=30)
    def available(self,c,vid,start,end,exclude_event=0,exclude_hire=0):
        if end<=start: return False
        for t,states,exc in [(events,['published'],exclude_event),(hires,['confirmed'],exclude_hire)]:
            for r in rows(c,t,t.c.venue_id==vid,t.c.status.in_(states),t.c.id!=exc):
                if start<dt(r['end'])+self.buffer and end>dt(r['start'])-self.buffer: return False
        return True
    def alternatives(self,c,v,start,end,guests):
        out=[]
        for other in rows(c,venues,venues.c.hire_open==True,venues.c.capacity>=guests):
            for offset in [0,1,2,3,4,24,48]:
                a=start+timedelta(hours=offset); b=end+timedelta(hours=offset)
                if a>now() and self.available(c,other['id'],a,b):
                    out.append({'venue_id':other['id'],'venue':other['name'],'start':stamp(a),'end':stamp(b),'price':int((b-a).total_seconds()/3600*other['rate'])})
                    if len(out)>=3: return out
        return out
    def handle(self,c,msg,bus):
        d=msg.content; action=d['action']
        if action=='event':
            v=one(c,venues,d['venue_id']); a,b=dt(d['start']),dt(d['end'])
            if not v or a>=b or a<=now(): return refuse('Invalid venue or event time')
            if sum(x['quantity'] for x in d['categories'])>v['capacity']: return refuse('Tickets exceed venue capacity')
            if d['status']=='published' and not self.available(c,v['id'],a,b): return refuse('Venue occupied or 30-minute turnaround buffer violated')
            eid=add(c,events,**{k:d[k] for k in ['title','category','venue_id','description','status']},start=stamp(a),end=stamp(b))
            for item in d['categories']: add(c,categories,event_id=eid,**item)
            return result(event_id=eid)
        if action=='publish':
            e=one(c,events,d['event_id'])
            if not e or e['status']!='draft': return refuse('Only draft events may be published')
            v=one(c,venues,e['venue_id']); qty=sum(x['quantity'] for x in rows(c,categories,categories.c.event_id==e['id']))
            if qty>v['capacity'] or dt(e['start'])<=now() or not self.available(c,e['venue_id'],dt(e['start']),dt(e['end']),e['id']): return refuse('Capacity, start time or venue conflict')
            change(c,events,e['id'],status='published'); return result(event_id=e['id'])
        if action=='hire':
            v=one(c,venues,d['venue_id']); a,b=dt(d['start']),dt(d['end']); guests=d['guests']
            if not v or a>=b or a<=now(): return refuse('Invalid venue or dates')
            if not v['hire_open'] or guests>v['capacity'] or not self.available(c,v['id'],a,b):
                alt=self.alternatives(c,v,a,b,guests)
                return result('PROPOSE',reason='Venue unavailable, closed or insufficient capacity',alternatives=alt) if alt else refuse('No available alternative found')
            price=int((b-a).total_seconds()/3600*v['rate']); status='pending' if guests>100 or b-a>timedelta(hours=6) else 'confirmed'
            hid=add(c,hires,**{k:d[k] for k in ['user_id','venue_id','purpose','guests']},start=stamp(a),end=stamp(b),price=price,status=status)
            bus.inform(c,msg,d['user_id'],f'Venue hire #{hid}: {status}; LKR {price}')
            return result(hire_id=hid,price=price,status=status)
        if action=='decide':
            h=one(c,hires,d['hire_id'])
            if not h or h['status']!='pending': return refuse('No pending hire')
            v=one(c,venues,h['venue_id'])
            if d['approve'] and (not v['hire_open'] or h['guests']>v['capacity'] or dt(h['start'])<=now() or not self.available(c,h['venue_id'],dt(h['start']),dt(h['end']),exclude_hire=h['id'])): return refuse('Venue no longer available')
            status='confirmed' if d['approve'] else 'rejected'; change(c,hires,h['id'],status=status)
            bus.inform(c,msg,h['user_id'],f'Venue hire #{h["id"]}: {status}'); return result(status=status)
        return refuse('Unknown venue action')

class TicketAgent:
    goal='Issue unique tickets and admit exactly once'
    def handle(self,c,msg,bus):
        d=msg.content; a=d['action']
        if a=='issue':
            codes=[]
            for _ in range(d['quantity']):
                code=secrets.token_urlsafe(32); add(c,tickets,booking_id=d['booking_id'],event_id=d['event_id'],code=code); codes.append(code)
            return result(codes=codes)
        if a=='cancel':
            c.execute(update(tickets).where(tickets.c.booking_id==d['booking_id']).values(status='cancelled')); return result()
        if a=='scan':
            t=c.execute(select(tickets).where(tickets.c.code==d['code'])).mappings().first()
            if not t: return refuse('Unknown ticket')
            change(c,tickets,t['id'],scans=t['scans']+1)
            if t['status']=='cancelled': return refuse('Cancelled ticket')
            if t['event_id']!=d['event_id']: return refuse('Wrong event')
            if t['status']=='used': return refuse('Already used')
            e=one(c,events,t['event_id'])
            if e['status']!='published' or not (dt(e['start'])-timedelta(hours=2)<=now()<=dt(e['end'])): return refuse('Outside check-in time')
            change(c,tickets,t['id'],status='used',used_at=stamp()); return result(reason='Admit')
        return refuse('Unknown ticket action')

class BookingAgent:
    goal='Sell fairly without overselling and calculate refunds'
    def handle(self,c,msg,bus):
        d=msg.content; a=d['action']
        if a=='book':
            q=d['quantity']; cat=one(c,categories,d['category_id'])
            if not cat or not 1<=q<=6: return refuse('Choose 1–6 tickets')
            e=one(c,events,cat['event_id'])
            if e['status']!='published' or dt(e['start'])<=now(): return refuse('Booking closed')
            if q>cat['quantity']-cat['sold']-cat['held']: return refuse('Not enough tickets available; join waitlist')
            bid=add(c,bookings,user_id=d['user_id'],category_id=cat['id'],quantity=q,total=q*cat['price'],payment_ref='SIM-'+secrets.token_hex(8))
            change(c,categories,cat['id'],sold=cat['sold']+q)
            r=bus.ask(c,msg,'TicketAgent','issue',booking_id=bid,event_id=e['id'],quantity=q)
            bus.inform(c,msg,d['user_id'],f'Booking #{bid} confirmed; {q} ticket(s) issued')
            return result(booking_id=bid,codes=r['codes'])
        if a=='cancel':
            b=one(c,bookings,d['booking_id'])
            if not b or b['status']!='confirmed' or b['user_id']!=d['user_id']: return refuse('No active booking belonging to you')
            cat=one(c,categories,b['category_id']); e=one(c,events,cat['event_id'])
            if dt(e['start'])<=now() or any(t['status']=='used' for t in rows(c,tickets,tickets.c.booking_id==b['id'])): return refuse('Event started or ticket already used')
            refund=b['total'] if dt(e['start'])-now()>=timedelta(hours=24) else b['total']//2
            self.release(c,msg,bus,b,cat,refund,True); return result(refund=refund)
        if a=='cancel_event':
            e=one(c,events,d['event_id'])
            if not e or e['status']!='published': return refuse('No published event')
            change(c,events,e['id'],status='cancelled')
            for cat in rows(c,categories,categories.c.event_id==e['id']):
                for b in rows(c,bookings,bookings.c.category_id==cat['id'],bookings.c.status=='confirmed'):
                    self.release(c,msg,bus,b,one(c,categories,cat['id']),b['total'],False)
                bus.ask(c,msg,'WaitlistAgent','close',category_id=cat['id'])
            return result()
        return refuse('Unknown booking action')
    def release(self,c,msg,bus,b,cat,refund,offer):
        change(c,bookings,b['id'],status='cancelled',refund=refund); change(c,categories,cat['id'],sold=cat['sold']-b['quantity'])
        bus.ask(c,msg,'TicketAgent','cancel',booking_id=b['id']); bus.inform(c,msg,b['user_id'],f'Booking #{b["id"]} cancelled; refund LKR {refund}')
        if offer: bus.ask(c,msg,'WaitlistAgent','offer',category_id=cat['id'])

class WaitlistAgent:
    goal='Allocate freed seats FIFO and expire timed holds'
    hold=timedelta(minutes=5)
    def offer(self,c,msg,bus,cid):
        cat=one(c,categories,cid); e=one(c,events,cat['event_id'])
        if e['status']!='published' or dt(e['start'])<=now(): return
        available=cat['quantity']-cat['sold']-cat['held']; held=cat['held']
        for w in rows(c,waitlist,waitlist.c.category_id==cid,waitlist.c.status=='waiting'):
            if w['quantity']>available: break
            available-=w['quantity']; held+=w['quantity']
            change(c,waitlist,w['id'],status='offered',expires=stamp(now()+self.hold))
            bus.inform(c,msg,w['user_id'],f'Waitlist #{w["id"]}: seats held for 5 minutes')
        change(c,categories,cid,held=held)
    def handle(self,c,msg,bus):
        d=msg.content; a=d['action']
        if a=='join':
            cat=one(c,categories,d['category_id']); q=d['quantity']
            if not cat or q>cat['quantity'] or not 1<=q<=6: return refuse('Invalid quantity/category')
            e=one(c,events,cat['event_id'])
            if e['status']!='published' or dt(e['start'])<=now(): return refuse('Event closed')
            if rows(c,waitlist,waitlist.c.user_id==d['user_id'],waitlist.c.category_id==cat['id'],waitlist.c.status.in_(['waiting','offered'])): return refuse('Already on waitlist')
            wid=add(c,waitlist,user_id=d['user_id'],category_id=cat['id'],quantity=q); self.offer(c,msg,bus,cat['id']); return result(waitlist_id=wid)
        if a=='offer': self.offer(c,msg,bus,d['category_id']); return result()
        if a=='claim':
            w=one(c,waitlist,d['waitlist_id'])
            if not w or w['user_id']!=d['user_id'] or w['status']!='offered' or dt(w['expires'])<=now(): return refuse('No valid offer')
            cat=one(c,categories,w['category_id']); e=one(c,events,cat['event_id'])
            if e['status']!='published' or dt(e['start'])<=now(): return refuse('Event closed')
            change(c,categories,cat['id'],held=cat['held']-w['quantity']); change(c,waitlist,w['id'],status='claimed')
            return bus.ask(c,msg,'BookingAgent','book',user_id=w['user_id'],category_id=cat['id'],quantity=w['quantity'])
        if a=='sweep':
            affected=set()
            for w in rows(c,waitlist,waitlist.c.status=='offered'):
                if dt(w['expires'])<=now():
                    cat=one(c,categories,w['category_id']); change(c,categories,cat['id'],held=cat['held']-w['quantity']); change(c,waitlist,w['id'],status='expired'); affected.add(cat['id']); bus.inform(c,msg,w['user_id'],'Waitlist offer expired')
            for cid in affected: self.offer(c,msg,bus,cid)
            return result(expired_categories=len(affected))
        if a=='close':
            cid=d['category_id']; c.execute(update(waitlist).where(waitlist.c.category_id==cid,waitlist.c.status.in_(['waiting','offered'])).values(status='cancelled')); change(c,categories,cid,held=0); return result()
        return refuse('Unknown waitlist action')

class NotificationAgent:
    goal='Persist user notifications from INFORM messages'
    def handle(self,c,msg,bus):
        if msg.performative!='INFORM': return refuse('INFORM required')
        add(c,notifications,user_id=msg.content['user_id'],text=msg.content['text'],created=stamp()); return result('INFORM',delivered=True)

bus=Bus(); bus.agents={a.__class__.__name__:a for a in [VenueAgent(),BookingAgent(),TicketAgent(),WaitlistAgent(),NotificationAgent()]}

def request_agent(c,receiver,action,**kw): return bus.send(c,Message(sender='Web',receiver=receiver,content={'action':action,**kw}))

def init_db():
    m.create_all(engine)
    with engine.begin() as c:
        if not one(c,mutex,1): add(c,mutex,id=1,version=0)
        lock(c)
        if not rows(c,users):
            for role in ['admin','staff','attendee']:
                add(c,users,email=role+'@example.com',password=password_hash(os.getenv('DEMO_PASSWORD','LocalDemo123!')),role=role)
            vid=add(c,venues,name='University Main Auditorium',location='Colombo',capacity=150,description='Main campus venue',rate=5000,hire_open=True)
            add(c,venues,name='Innovation Lab',location='Colombo',capacity=50,description='Workshop space',rate=2500,hire_open=True)
            eid=add(c,events,title='Innovation & Robotics Expo',category='Workshop',venue_id=vid,start=stamp(now()+timedelta(hours=1)),end=stamp(now()+timedelta(hours=4)),description='Demo event, check-in opens now.',status='published')
            add(c,categories,event_id=eid,name='General',price=1000,quantity=100); add(c,categories,event_id=eid,name='VIP',price=2500,quantity=20)

def sweep():
    with engine.begin() as c:
        lock(c)
        if c.execute(select(waitlist.c.id).where(waitlist.c.status=='offered',waitlist.c.expires<=stamp()).limit(1)).first():
            bus.send(c,Message(sender='Timer',receiver='WaitlistAgent',content={'action':'sweep'}))
async def timer():
    while True:
        await asyncio.sleep(15)
        try: await asyncio.to_thread(sweep)
        except Exception as e: print('Waitlist sweep failed:',e)
@asynccontextmanager
async def lifespan(app):
    init_db(); task=asyncio.create_task(timer())
    try: yield
    finally:
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass

app=FastAPI(title='EventSphere',lifespan=lifespan)
app.add_middleware(SessionMiddleware,secret_key=os.getenv('SECRET_KEY') or secrets.token_hex(32),https_only=bool(os.getenv('RENDER')),same_site='lax')

def csrf(r:Request):
    token=r.session.get('csrf')
    if not token or not hmac.compare_digest(token,r.headers.get('X-CSRF-Token','')): raise HTTPException(403,'Invalid CSRF token')
def auth(r,roles=None):
    uid=r.session.get('uid')
    with engine.connect() as c: u=one(c,users,uid) if uid else None
    if not u: raise HTTPException(401,'Sign in required')
    if roles and u['role'] not in roles: raise HTTPException(403,'Forbidden for this role')
    return dict(u)
class Credentials(BaseModel):
    email:str=Field(min_length=3,max_length=250); password:str=Field(min_length=8,max_length=128)
class VenueIn(BaseModel):
    name:str=Field(min_length=1,max_length=200); location:str=Field(min_length=1,max_length=200); capacity:int=Field(gt=0); description:str=''; image:str=''; rate:int=Field(ge=0); hire_open:bool=True
class CategoryIn(BaseModel):
    name:str=Field(min_length=1,max_length=100); price:int=Field(ge=0); quantity:int=Field(gt=0)
class EventIn(BaseModel):
    title:str=Field(min_length=1,max_length=200); category:str; venue_id:int; start:str; end:str; description:str=''; status:Literal['draft','published']='draft'; categories:list[CategoryIn]=Field(min_length=1)
class Qty(BaseModel): category_id:int; quantity:int=Field(ge=1,le=6)
class HireIn(BaseModel): venue_id:int; start:str; end:str; purpose:str=Field(min_length=1,max_length=500); guests:int=Field(gt=0)
class ScanIn(BaseModel): event_id:int; code:str
class Decision(BaseModel): approve:bool

def agent_call(receiver,action,**kw):
    try:
        with engine.begin() as c: lock(c); return request_agent(c,receiver,action,**kw)
    except ValueError as e: raise HTTPException(422,str(e))

@app.get('/')
def home(): return FileResponse(Path(__file__).with_name('index.html'))
@app.get('/api/session')
def session(r:Request):
    if not r.session.get('csrf'): r.session['csrf']=secrets.token_urlsafe(32)
    try: u=auth(r); user={k:u[k] for k in ['id','email','role']}
    except HTTPException: user=None
    return {'csrf':r.session['csrf'],'user':user}
@app.post('/api/register',dependencies=[Depends(csrf)])
def register(data:Credentials,r:Request):
    email=data.email.strip().lower()
    if '@' not in email: raise HTTPException(422,'Invalid email')
    with engine.begin() as c:
        lock(c)
        if rows(c,users,users.c.email==email): raise HTTPException(409,'Email already registered')
        r.session['uid']=add(c,users,email=email,password=password_hash(data.password),role='attendee')
    return {'ok':True}
@app.post('/api/login',dependencies=[Depends(csrf)])
def login(data:Credentials,r:Request):
    with engine.connect() as c: u=c.execute(select(users).where(users.c.email==data.email.strip().lower())).mappings().first()
    if not u or not password_ok(data.password,u['password']): raise HTTPException(401,'Invalid credentials')
    r.session['uid']=u['id']; return {'role':u['role']}
@app.post('/api/logout',dependencies=[Depends(csrf)])
def logout(r:Request): r.session.clear(); return {'ok':True}
@app.get('/api/venues')
def venue_list():
    with engine.connect() as c: return rows(c,venues)
@app.get('/api/events')
def event_list(q:str='',category:str=''):
    with engine.connect() as c:
        out=[]
        for e in rows(c,events,events.c.status=='published'):
            if q.lower() not in (e['title']+' '+e['description']).lower() or category and e['category'].lower()!=category.lower(): continue
            v=one(c,venues,e['venue_id']); e['venue']=v['name']; e['image']=v['image']; e['categories']=rows(c,categories,categories.c.event_id==e['id']); out.append(e)
        return out
@app.post('/api/admin/venues',dependencies=[Depends(csrf)])
def create_venue(d:VenueIn,r:Request):
    auth(r,['admin'])
    with engine.begin() as c: lock(c); return {'id':add(c,venues,**d.model_dump())}
@app.put('/api/admin/venues/{vid}',dependencies=[Depends(csrf)])
def edit_venue(vid:int,d:VenueIn,r:Request):
    auth(r,['admin'])
    with engine.begin() as c:
        lock(c)
        if not one(c,venues,vid): raise HTTPException(404,'Venue not found')
        for e in rows(c,events,events.c.venue_id==vid,events.c.status.in_(['draft','published'])):
            if sum(x['quantity'] for x in rows(c,categories,categories.c.event_id==e['id']))>d.capacity: raise HTTPException(409,'Existing event exceeds new capacity')
        if any(h['guests']>d.capacity for h in rows(c,hires,hires.c.venue_id==vid,hires.c.status=='confirmed')): raise HTTPException(409,'Existing hire exceeds new capacity')
        change(c,venues,vid,**d.model_dump()); return {'ok':True}
@app.post('/api/admin/events',dependencies=[Depends(csrf)])
def create_event(d:EventIn,r:Request):
    auth(r,['admin']); return agent_call('VenueAgent','event',**d.model_dump())
@app.post('/api/admin/events/{eid}/publish',dependencies=[Depends(csrf)])
def publish(eid:int,r:Request): auth(r,['admin']); return agent_call('VenueAgent','publish',event_id=eid)
@app.post('/api/admin/events/{eid}/cancel',dependencies=[Depends(csrf)])
def cancel_event(eid:int,r:Request): auth(r,['admin']); return agent_call('BookingAgent','cancel_event',event_id=eid)
@app.post('/api/admin/hires/{hid}',dependencies=[Depends(csrf)])
def decide(hid:int,d:Decision,r:Request): auth(r,['admin']); return agent_call('VenueAgent','decide',hire_id=hid,approve=d.approve)
@app.get('/api/admin/dashboard')
def dashboard(r:Request):
    auth(r,['admin'])
    with engine.connect() as c:
        es=rows(c,events)
        for e in es:
            cats=rows(c,categories,categories.c.event_id==e['id']); e['capacity']=one(c,venues,e['venue_id'])['capacity']; e['sold']=sum(x['sold'] for x in cats); e['revenue']=sum(b['total']-b['refund'] for x in cats for b in rows(c,bookings,bookings.c.category_id==x['id'])); e['checkins']=len(rows(c,tickets,tickets.c.event_id==e['id'],tickets.c.status=='used'))
        return {'events':es,'hires':rows(c,hires),'messages':rows(c,logs)}
@app.post('/api/book',dependencies=[Depends(csrf)])
def book(d:Qty,r:Request): u=auth(r,['attendee']); return agent_call('BookingAgent','book',user_id=u['id'],**d.model_dump())
@app.post('/api/bookings/{bid}/cancel',dependencies=[Depends(csrf)])
def cancel(bid:int,r:Request): u=auth(r,['attendee']); return agent_call('BookingAgent','cancel',user_id=u['id'],booking_id=bid)
@app.post('/api/waitlist',dependencies=[Depends(csrf)])
def join(d:Qty,r:Request): u=auth(r,['attendee']); return agent_call('WaitlistAgent','join',user_id=u['id'],**d.model_dump())
@app.post('/api/waitlist/{wid}/claim',dependencies=[Depends(csrf)])
def claim(wid:int,r:Request): u=auth(r,['attendee']); return agent_call('WaitlistAgent','claim',user_id=u['id'],waitlist_id=wid)
@app.post('/api/hires',dependencies=[Depends(csrf)])
def hire(d:HireIn,r:Request): u=auth(r,['attendee']); return agent_call('VenueAgent','hire',user_id=u['id'],**d.model_dump())
@app.get('/api/me')
def my_data(r:Request):
    u=auth(r,['attendee']); uid=u['id']
    with engine.connect() as c:
        bs=rows(c,bookings,bookings.c.user_id==uid)
        for b in bs:
            cat=one(c,categories,b['category_id']); b['title']=one(c,events,cat['event_id'])['title']; b['tickets']=rows(c,tickets,tickets.c.booking_id==b['id'])
            for t in b['tickets']:
                buf=io.BytesIO(); qrcode.make(t['code']).save(buf,format='PNG'); t['qr']='data:image/png;base64,'+base64.b64encode(buf.getvalue()).decode()
        return {'bookings':bs,'waitlist':rows(c,waitlist,waitlist.c.user_id==uid),'hires':rows(c,hires,hires.c.user_id==uid),'notifications':rows(c,notifications,notifications.c.user_id==uid)}
@app.post('/api/staff/scan',dependencies=[Depends(csrf)])
def scan(d:ScanIn,r:Request): auth(r,['staff']); return agent_call('TicketAgent','scan',**d.model_dump())
