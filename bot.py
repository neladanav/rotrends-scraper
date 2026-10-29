"""Existing cron-triggered RoTrends collector -> private Scoutbase ingestion."""
import hashlib
import json
import os
import re
import signal
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse
from urllib.request import Request, urlopen

BASE_URL = 'https://www.rotrends.com/games?page=1&page_size=50&playing_min=100&visits_max=500000'
INGEST_URL = 'https://xxuwqkscmxhjgibuhpwc.supabase.co/functions/v1/dt-scout-ingest'
AUDIENCE = 'dt-scoutbase-ingest'
STATE_FILE = Path(os.environ.get('SCAN_CURSOR_FILE', 'scan_cursor.json'))
IDENTITY_FILE = Path(os.environ.get('IDENTITY_CACHE_FILE', 'identity_cache.json'))
MAX_PAGES = max(1, min(20, int(os.environ.get('MAX_PAGES', '3'))))
ROTATING_MAX_PAGE = max(0, int(os.environ.get('ROTATING_MAX_PAGE', '0')))
RUN_BUDGET_SECONDS = max(30, min(420, int(os.environ.get('RUN_BUDGET_SECONDS', '420'))))
_deadline = None
DRY_RUN = os.environ.get('DRY_RUN', '').lower() in {'1', 'true', 'yes'}
EXCLUSION_WORDS = ['modded', 'mod', 'reuploaded', 'reupload', 'nsfw', '18+', 'test', 'español', 'русский']

def is_excluded(name):
    normalized = re.sub(r'\s+', ' ', str(name)).strip().casefold()
    for word in EXCLUSION_WORDS:
        if word in {'español', 'русский', 'nsfw', '18+'}:
            if word in normalized:return True
        elif re.search(r'(?<!\w)' + re.escape(word) + r'(?!\w)', normalized):return True
    return False

class CollectionError(RuntimeError):
    pass


class BudgetReached(CollectionError):
    pass


def remaining_seconds():
    return float('inf') if _deadline is None else _deadline-time.monotonic()


def ensure_budget(reserve=0):
    if remaining_seconds() <= reserve:
        raise BudgetReached('Collection time budget reached; the next unacknowledged page will resume next run.')


def alarm_budget(_signum, _frame):
    raise BudgetReached('Collection time budget reached; the next unacknowledged page will resume next run.')


def request_json(url, headers=None, body=None):
    payload = None if body is None else json.dumps(body).encode()
    request = Request(url, data=payload, headers={'Accept':'application/json', **({'Content-Type':'application/json'} if payload else {}), **(headers or {})})
    for attempt in range(3):
        ensure_budget()
        try:
            with urlopen(request, timeout=min(25, max(0.1,remaining_seconds()))) as response:
                return json.load(response)
        except HTTPError as error:
            retry = error.headers.get('Retry-After', '60')
            try: seconds = max(1, int(retry))
            except ValueError: seconds = 60
            if error.code == 429 and attempt < 2 and seconds <= 30:
                ensure_budget(seconds+5)
                print(f'{urlparse(url).hostname} is rate limited; respecting its {seconds}s retry delay.')
                time.sleep(seconds)
                continue
            # Never include request headers, tokens or provider bodies in logs.
            raise CollectionError(f'{urlparse(url).hostname} returned HTTP {error.code}; retry after {retry}s') from None



def identity_token():
    url = os.environ.get('ACTIONS_ID_TOKEN_REQUEST_URL', '')
    credential = os.environ.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN', '')
    if not url.startswith('https://') or not credential:
        raise CollectionError('Run this collector through the existing GitHub Action; its short-lived identity is required.')
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query)); query['audience'] = AUDIENCE
    response = request_json(urlunparse(parsed._replace(query=urlencode(query))), {'Authorization':'Bearer '+credential})
    token = response.get('value')
    if not isinstance(token, str) or token.count('.') != 2:
        raise CollectionError('GitHub did not return a valid identity token.')
    return token


def parse_number(value):
    raw = str(value or '').strip().rstrip('+')
    if ',' in raw and not re.fullmatch(r'\d{1,3}(?:,\d{3})+(?:\.\d+)?\s*[KMB]?', raw, re.I):
        return None
    match = re.fullmatch(r'(\d*\.?\d+)\s*([KMB]?)', raw.replace(',', ''), re.I)
    if not match:
        return None
    try:
        number = Decimal(match[1]) * {'':1, 'K':1000, 'M':1000000, 'B':1000000000}[match[2].upper()]
        return int(number) if number == int(number) and 0 <= number <= 9007199254740991 else None
    except (InvalidOperation, ValueError, OverflowError):
        return None


def page_url(page):
    if not isinstance(page, int) or page < 1:
        raise CollectionError('Invalid page number')
    parsed = urlparse(BASE_URL); query = dict(parse_qsl(parsed.query)); query['page'] = str(page)
    return urlunparse(parsed._replace(query=urlencode(query)))


def place_from_url(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname not in {'www.roblox.com','roblox.com','m.roblox.com'}:
        return None
    match = re.fullmatch(r'/(?:[a-z]{2}/)?games/([1-9]\d*)(?:/.*)?', parsed.path)
    return int(match[1]) if match and int(match[1]) <= 9007199254740991 else None


def read_state(path, fallback):
    try:
        body = json.loads(path.read_text())
        return body if isinstance(body, dict) else fallback
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback


def atomic_save(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name+'.')
    try:
        with os.fdopen(descriptor,'w') as handle:
            json.dump(body, handle, indent=2);handle.write('\n');handle.flush();os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def select_pages(next_page, count=MAX_PAGES, maximum=ROTATING_MAX_PAGE):
    cursor = max(1, int(next_page))
    if maximum and cursor > maximum:cursor = 1
    pages = []
    for _ in range(min(maximum or count, count)):
        pages.append(cursor); cursor = 1 if maximum and cursor >= maximum else cursor+1
    return pages


def get_game_rows(page):
    page.wait_for_selector('table', timeout=15000)
    rows = page.locator('table tbody tr.ant-table-row').all()
    if not rows:
        # Ant Design also renders a seven-cell sizing row on empty pages.
        # It has no game identity and must never enter the legacy-row fallback.
        layout_classes = {'ant-table-measure-row', 'ant-table-placeholder'}
        rows = [row for row in page.locator('table tbody tr').all()
                if not layout_classes.intersection((row.get_attribute('class') or '').split())
                and row.locator('td').count() >= 5]
    if rows:return rows
    placeholders = page.locator('.ant-table-placeholder,.ant-empty-description').all_text_contents()
    if any(text.strip().lower() in {'no data','no games found','no results'} for text in placeholders):return []
    raise CollectionError('No game rows or reliable empty-page marker; retaining the cursor.')


def parse_row(row, headers):
    cells = row.locator('td').all()
    if len(cells)<5:raise CollectionError('Invalid table row')
    links = cells[0].locator('a').all()
    place = next((place_from_url(link.get_attribute('href') or '') for link in links if place_from_url(link.get_attribute('href') or '')), None)
    name = next((link.inner_text().strip() for link in links if link.inner_text().strip() and link.inner_text().strip().lower() not in {'[object object]','no data'}), cells[0].inner_text().strip())
    if not place or not name:raise CollectionError('A game has no reliable Roblox link or title; no title-based identity was invented.')
    column=lambda pattern, fallback: next((i for i,label in enumerate(headers) if re.search(pattern,label,re.I)),fallback)
    visits_index=column('visits',1);active_index=column('playing|active|ccu|pcu|peak',2);favorite_index=column('favorites|favourites',4)
    raw={key:cells[index].inner_text().strip() for key,index in [('visits',visits_index),('active',active_index),('favorites',favorite_index)]}
    creator=next((line.strip()[1:] for line in cells[0].inner_text().splitlines() if re.fullmatch(r'@[A-Za-z0-9_]{3,20}',line.strip())),None)
    label=headers[active_index] if active_index<len(headers) else ''
    return {'placeId':place,'name':name[:200],'creator':creator,**{key:parse_number(value) for key,value in raw.items()},'raw':raw,'retrievedAt':datetime.now(timezone.utc).isoformat(),'metricDefinition':'Unknown' if not re.search('playing|active|ccu',label,re.I) or re.search('pcu|peak',label,re.I) else 'Concurrent players'}


def resolve_candidate(candidate, cache):
    ensure_budget()
    place=str(candidate['placeId']);universe=cache.get(place)
    if not isinstance(universe,int) or universe<=0:
        universe=request_json('https://apis.roblox.com/universes/v1/places/'+place+'/universe').get('universeId')
        if not isinstance(universe,int) or not 0<universe<=9007199254740991:raise CollectionError('Roblox could not resolve the canonical experience.')
        cache[place]=universe
    return {**candidate,'universeId':universe}


def deliver(payload):
    token=identity_token()
    return request_json(INGEST_URL,{'Authorization':'Bearer '+token},payload)


def main():
    global _deadline
    from playwright.sync_api import sync_playwright
    state=read_state(STATE_FILE,{'version':3,'next_page':1});cache=read_state(IDENTITY_FILE,{})
    planned=select_pages(state.get('next_page',1));fingerprints=set();pages_read=[];queued=0
    _deadline=time.monotonic()+RUN_BUDGET_SECONDS
    # GitHub's Linux runner also enforces a hard in-process deadline around slow
    # browser/response calls. Keep the workflow timeout as a final safety net.
    previous_alarm = signal.signal(signal.SIGALRM,alarm_budget)
    signal.alarm(RUN_BUDGET_SECONDS)
    print('Collecting pages '+str(planned)+' with the original visit/player filters. Previously collected games are refreshed again.')
    try:
        with sync_playwright() as runtime:
            browser=runtime.chromium.launch(headless=True)
            try:
                page=browser.new_page()
                page.set_default_timeout(5000)
                for number in planned:
                    ensure_budget(45)
                    endpoint=page_url(number)
                    response=page.goto(endpoint,timeout=30000,wait_until='domcontentloaded')
                    if response and response.status>=400:raise CollectionError(f'RoTrends returned HTTP {response.status}; no retry burst was started.')
                    page.wait_for_timeout(1500)
                    rows=get_game_rows(page);headers=page.locator('table thead th').all_text_contents()
                    parsed_rows=[]
                    for row in rows:
                        ensure_budget()
                        parsed_rows.append(parse_row(row,headers))
                    candidates=[candidate for candidate in parsed_rows if not is_excluded(candidate['name'])]
                    fingerprint=hashlib.sha256('|'.join(sorted(str(c['placeId']) for c in parsed_rows)).encode()).hexdigest()
                    previous=state.get('last_deep_fingerprint')
                    if parsed_rows and (fingerprint in fingerprints or number>1 and number!=state.get('last_deep_page') and fingerprint==previous):raise CollectionError('Repeated source page detected; scan remains partial.')
                    fingerprints.add(fingerprint)
                    candidates=list({c['placeId']:c for c in candidates}.values())
                    candidates=[resolve_candidate(c,cache) for c in candidates]
                    payload={'page':number,'endpoint':endpoint,'candidates':candidates,'rowCount':len(rows),'endDetected':not rows,'status':'Page collected' if rows else 'Empty page'}
                    if DRY_RUN:
                        print(f'DRY RUN page {number}: {len(candidates)} canonical candidates; nothing delivered or saved.')
                        continue
                    result=deliver(payload)
                    if result.get('status')=='Paused':print('Scoutbase collection is paused. Cursor retained.');return
                    if result.get('status')!='Queued':raise CollectionError('Scoutbase did not confirm this page; cursor retained.')
                    queued+=result.get('queued',0);pages_read.append(number)
                    if not rows:state['next_page']=1
                    else:
                        state['next_page']=1 if ROTATING_MAX_PAGE and number>=ROTATING_MAX_PAGE else number+1
                        if number>1:
                            state['last_deep_page']=number;state['last_deep_fingerprint']=fingerprint
                    state.update(version=3,status='Completed batch',last_page=number,pages_read=pages_read,end_detected=not rows,scan_complete=not rows,last_success=datetime.now(timezone.utc).isoformat(),error=None)
                    atomic_save(STATE_FILE,state);atomic_save(IDENTITY_FILE,cache)
                    print(f'Page {number}: Scoutbase committed {result.get("queued",0)} canonical candidates for official verification (duplicate delivery: {result.get("duplicate",False)}).')
                    if not rows:break
            finally:browser.close()
        print(f'Batch finished: {len(pages_read)} acknowledged page(s), {queued} candidates staged. Eligibility is decided by Scoutbase using Roblox metrics.')
    except BudgetReached as error:
        if not DRY_RUN:
            state.update(status='Time budget reached',pages_read=pages_read,scan_complete=False,error=None,last_attempt=datetime.now(timezone.utc).isoformat())
            atomic_save(STATE_FILE,state);atomic_save(IDENTITY_FILE,cache)
        print(str(error))
    except Exception as error:
        if not DRY_RUN:
            state.update(status='Partial',scan_complete=False,error=str(error)[:500],last_attempt=datetime.now(timezone.utc).isoformat());atomic_save(STATE_FILE,state);atomic_save(IDENTITY_FILE,cache)
        raise CollectionError(str(error)) from None
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM,previous_alarm)
        _deadline=None

if __name__=='__main__':
    try:main()
    except CollectionError as error:
        print('Collection failed: '+str(error));raise SystemExit(1)

