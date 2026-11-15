import os
os.environ['DATABASE_URL']='sqlite:///:memory:'
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
import pytest
import main as a

@pytest.fixture
def db(monkeypatch):
    engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
    monkeypatch.setattr(a,'engine',engine); a.init_db()
    yield engine
    engine.dispose()

def call(db,agent,action,**kw):
    with db.begin() as c:
        a.lock(c); return a.request_agent(c,agent,action,**kw)
def book(db,q=1,category_id=1,uid=3): return call(db,'BookingAgent','book',category_id=category_id,quantity=q,user_id=uid)
def test_booking_and_ticket_issuance(db):
    r=book(db,2); assert r['performative']=='AGREE'; assert len(set(r['codes']))==2
    with db.connect() as c: assert a.one(c,a.categories,1)['sold']==2; assert a.one(c,a.bookings,r['booking_id'])['payment_ref'].startswith('SIM-')
def test_overbooking(db):
    with db.begin() as c: a.change(c,a.categories,1,quantity=1)
    assert book(db)['performative']=='AGREE'; assert book(db)['performative']=='REFUSE'
def test_quantity_limit(db): assert book(db,7)['performative']=='REFUSE'
def test_double_checkin(db):
    code=book(db)['codes'][0]; assert call(db,'TicketAgent','scan',event_id=1,code=code)['performative']=='AGREE'; assert call(db,'TicketAgent','scan',event_id=1,code=code)['reason']=='Already used'
def test_wrong_event(db): assert call(db,'TicketAgent','scan',event_id=99,code=book(db)['codes'][0])['reason']=='Wrong event'
def test_cancellation_refund(db):
    r=book(db); assert call(db,'BookingAgent','cancel',booking_id=r['booking_id'],user_id=3)['refund']==500; assert call(db,'TicketAgent','scan',event_id=1,code=r['codes'][0])['reason']=='Cancelled ticket'
def test_capacity(db):
    r=call(db,'VenueAgent','event',title='Huge',category='Test',venue_id=1,start=a.stamp(a.now()+timedelta(days=1)),end=a.stamp(a.now()+timedelta(days=1,hours=1)),description='',status='published',categories=[{'name':'General','price':10,'quantity':151}]); assert r['performative']=='REFUSE'
def test_venue_alternative(db):
    with db.connect() as c: e=dict(a.one(c,a.events,1))
    r=call(db,'VenueAgent','hire',venue_id=1,start=e['start'],end=e['end'],purpose='Party',guests=10,user_id=3); assert r['performative']=='PROPOSE'; assert r['alternatives']
def test_venue_hire_confirm(db):
    r=call(db,'VenueAgent','hire',venue_id=2,start=a.stamp(a.now()+timedelta(days=2)),end=a.stamp(a.now()+timedelta(days=2,hours=2)),purpose='Party',guests=10,user_id=3); assert r['status']=='confirmed' and r['price']==5000

def test_waitlist_expiry(db):
    with db.begin() as c:
        a.change(c,a.categories,1,quantity=1); a.add(c,a.users,email='second@test.com',password='unused',role='attendee')
    first=call(db,'WaitlistAgent','join',category_id=1,quantity=1,user_id=3)['waitlist_id']; second=call(db,'WaitlistAgent','join',category_id=1,quantity=1,user_id=4)['waitlist_id']
    with db.begin() as c: a.change(c,a.waitlist,first,expires=a.stamp(a.now()-timedelta(seconds=1)))
    a.sweep()
    with db.connect() as c: assert a.one(c,a.waitlist,first)['status']=='expired'; assert a.one(c,a.waitlist,second)['status']=='offered'
def test_notification_and_trace(db):
    book(db)
    with db.connect() as c:
        assert a.rows(c,a.notifications)[0]['user_id']==3; messages=a.rows(c,a.logs); assert len({m['conversation_id'] for m in messages})==1; assert any(m['receiver']=='TicketAgent' for m in messages); assert any(m['performative']=='INFORM' for m in messages)
def test_full_event_refund(db):
    r=book(db); call(db,'BookingAgent','cancel_event',event_id=1)
    with db.connect() as c: assert a.one(c,a.bookings,r['booking_id'])['refund']==1000; assert a.one(c,a.events,1)['status']=='cancelled'
def test_concurrent_last_seat(tmp_path,monkeypatch):
    engine=create_engine('sqlite:///'+str(tmp_path/'race.db'),connect_args={'check_same_thread':False,'timeout':30}); monkeypatch.setattr(a,'engine',engine); a.init_db()
    with engine.begin() as c: a.change(c,a.categories,1,quantity=1)
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(lambda _:book(engine),range(2)))
    assert sorted(x['performative'] for x in results)==['AGREE','REFUSE']; engine.dispose()
def test_roles_csrf(db):
    from fastapi.testclient import TestClient
    # TestClient worker threads share the fixture's StaticPool SQLite database.
    with TestClient(a.app) as client:
        token=client.get('/api/session').json()['csrf']; headers={'X-CSRF-Token':token}
        assert client.post('/api/login',json={'email':'attendee@example.com','password':'LocalDemo123!'}).status_code==403
        assert client.post('/api/login',json={'email':'attendee@example.com','password':'LocalDemo123!'},headers=headers).status_code==200
        assert client.get('/api/admin/dashboard').status_code==403
        assert client.post('/api/staff/scan',json={'event_id':1,'code':'x'},headers=headers).status_code==403
