#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
두산 베어스 시즌 대시보드 빌더 (GitHub Actions 용)

동작:
  1) Playwright(헤드리스 Chromium)로 다음스포츠·KBO에서 순위/득실/최근10/연속/상대전적/일정을 수집
  2) 각 팀 talent = 0.5*피타고리안(1.83) + 0.5*실제승률 → 잔여경기 몬테카를로 10만회 → 가을야구 확률
  3) template.html 의 데이터 주입 지점에 JSON 을 넣어 index.html 생성

주의(정직 원칙):
  - 수집이 부분 실패하면 해당 항목만 비우고(빈 문자열/0) 나머지는 진행한다. 절대 값을 지어내지 않는다.
  - 어떤 소스든 팀 10개 순위를 못 얻으면 에러로 종료(오래된/깨진 대시보드를 밀지 않기 위해).
  - 최초 실행 때 사이트 DOM 구조에 따라 셀렉터 미세조정이 필요할 수 있다. --debug 로 수집 원문을 출력한다.
"""
import sys, json, re, datetime, pathlib
import numpy as np
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).parent
TEMPLATE = ROOT / "template.html"
OUT = ROOT / "index.html"

KST = datetime.timezone(datetime.timedelta(hours=9))
TODAY = datetime.datetime.now(KST).date()

# 다음/KBO 표기가 섞여도 하나로 정규화
TEAM_ALIASES = {
    "KT":"KT","kt":"KT","kt wiz":"KT","KT 위즈":"KT",
    "삼성":"삼성","삼성 라이온즈":"삼성",
    "LG":"LG","엘지":"LG","LG 트윈스":"LG",
    "두산":"두산","두산 베어스":"두산",
    "KIA":"KIA","기아":"KIA","KIA 타이거즈":"KIA","기아 타이거즈":"KIA",
    "한화":"한화","한화 이글스":"한화",
    "NC":"NC","엔씨":"NC","NC 다이노스":"NC",
    "롯데":"롯데","롯데 자이언츠":"롯데",
    "SSG":"SSG","에스에스지":"SSG","SSG 랜더스":"SSG",
    "키움":"키움","키움 히어로즈":"키움",
}
TEAMS = ["KT","삼성","LG","두산","KIA","한화","NC","롯데","SSG","키움"]
STADIUM = {
    "삼성":"대구 삼성 라이온즈 파크","LG":"잠실야구장","두산":"잠실야구장",
    "KT":"수원 KT 위즈파크","KIA":"광주-기아 챔피언스필드","한화":"대전 한화생명 볼파크",
    "NC":"창원 NC파크","롯데":"부산 사직야구장","SSG":"인천 SSG 랜더스필드","키움":"고척 스카이돔",
}
DEBUG = "--debug" in sys.argv

def norm_team(s):
    s = (s or "").strip()
    if s in TEAM_ALIASES: return TEAM_ALIASES[s]
    for k,v in TEAM_ALIASES.items():
        if k and k in s: return v
    return None

def read_tables(page):
    """페이지의 모든 <table> 를 [header列, rows(각 행 = 셀 텍스트 리스트)] 로 반환."""
    return page.eval_on_selector_all("table", """(tables)=>tables.map(t=>{
        const head=[...t.querySelectorAll('thead th')].map(e=>e.innerText.trim());
        const rows=[...t.querySelectorAll('tbody tr')].map(tr=>[...tr.querySelectorAll('th,td')].map(td=>td.innerText.trim()));
        return {head, rows};
    })""")

def col_index(head, *keywords):
    for i,h in enumerate(head):
        for kw in keywords:
            if kw in h: return i
    return None

def find_col(head, includes, excludes=()):
    """공백 제거 후 includes 중 하나라도 포함하고 excludes 는 전부 미포함인 첫 열."""
    for i,h in enumerate(head):
        hs=(h or "").replace(" ","")
        if any(k in hs for k in includes) and not any(x in hs for x in excludes):
            return i
    return None

# ───────────────────────── 수집 ─────────────────────────
def fetch_standings(page):
    """다음스포츠 순위 페이지 → 팀별 g,w,d,l,pct,gb,rs,ra,(f,st 있으면)"""
    page.goto("https://sports.daum.net/record/kbo/team", wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(2500)
    tables = read_tables(page)
    if DEBUG:
        for i,t in enumerate(tables):
            print(f"[debug] table#{i} head={t['head']}")
            for r in t["rows"][:2]: print("   row=",r)
    data = {}
    for t in tables:
        head, rows = t["head"], t["rows"]
        if not rows: continue
        ci = {
            "g":  col_index(head,"경기"),
            "w":  col_index(head,"승"),
            "d":  col_index(head,"무"),
            "l":  col_index(head,"패"),
            "pct":col_index(head,"승률"),
            "gb": col_index(head,"게임","차"),
            # 득점(R): '득점'만 매칭하고 '득점권'·'타점(RBI)'·'실점'은 제외 → RBI 열 오독 방지
            "rs": find_col(head, ["득점"], ["권","타점","실"]),
            # 실점(총실점 R): '실점' 매칭, '자책(ER)'은 제외
            "ra": find_col(head, ["실점"], ["자책"]),
            "f":  col_index(head,"최근"),
            "st": col_index(head,"연속"),
        }
        # 팀명이 들어있는 열 찾기
        name_col = None
        for r in rows:
            for j,c in enumerate(r):
                if norm_team(c): name_col=j; break
            if name_col is not None: break
        if name_col is None: continue
        for r in rows:
            tm = norm_team(r[name_col]) if name_col < len(r) else None
            if not tm: continue
            rec = data.setdefault(tm, {})
            def gv(key, cast):
                idx = ci[key]
                if idx is None or idx>=len(r): return None
                v = r[idx].replace(",","").strip()
                try: return cast(v)
                except: return None
            for key in ("g","w","d","l"):
                v = gv(key,int)
                if v is not None: rec[key]=v
            for key in ("rs","ra"):
                v = gv(key,int)
                if v is not None: rec[key]=v
            p = gv("pct",str)
            if p:
                if not p.startswith("."): p = ("."+p.split(".")[-1]) if "." in p else p
                rec["pct"]=p if p.startswith(".") else f".{p}"
            gb = gv("gb",str)
            if gb: rec["gb"]=gb
            f = gv("f",str)
            if f and re.search(r"\d+(승|무|패)", f): rec.setdefault("f",f)
            st = gv("st",str)
            if st and re.search(r"\d+(승|무|패)", st): rec.setdefault("st",st)
    return data

def fetch_recent_streak_h2h(page):
    """KBO 일별 순위 → 최근10(f)·연속(st), 그리고 두산 기준 상대전적."""
    recent = {}
    h2h = {}
    try:
        page.goto("https://www.koreabaseball.com/record/teamrank/teamrankdaily.aspx",
                  wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)
        tables = read_tables(page)
        for t in tables:
            head, rows = t["head"], t["rows"]
            fi = col_index(head,"최근"); si = col_index(head,"연속")
            name_col=None
            for r in rows:
                for j,c in enumerate(r):
                    if norm_team(c): name_col=j; break
                if name_col is not None: break
            if name_col is None: continue
            for r in rows:
                tm = norm_team(r[name_col]) if name_col<len(r) else None
                if not tm: continue
                if fi is not None and fi<len(r) and re.search(r"\d+(승|무|패)", r[fi]):
                    recent.setdefault(tm,{})["f"]=r[fi].strip()
                if si is not None and si<len(r) and re.search(r"\d+(승|무|패|)", r[si]):
                    recent.setdefault(tm,{})["st"]=r[si].strip()
    except Exception as e:
        print(f"[warn] KBO 최근10/연속 수집 실패: {e}")
    # 상대전적 매트릭스(두산 행)
    try:
        page.goto("https://www.koreabaseball.com/record/teamrank/teamrank.aspx",
                  wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)
        tables = read_tables(page)
        for t in tables:
            head, rows = t["head"], t["rows"]
            # 헤더에 상대 팀명이 여럿 있으면 상대전적표로 간주
            opp_cols = {}
            for i,h in enumerate(head):
                nt = norm_team(h)
                if nt: opp_cols[i]=nt
            if len(opp_cols) < 5: continue
            for r in rows:
                if not r: continue
                tm = norm_team(r[0])
                if tm != "두산": continue
                for i,nt in opp_cols.items():
                    if i<len(r):
                        m = re.match(r"(\d+)\D+(\d+)\D+(\d+)", r[i])  # 승-패-무 or 승-무-패
                        if m:
                            a,b,c = map(int,m.groups())
                            # KBO 표기는 보통 '승-패-무'
                            h2h[nt] = {"w":a,"l":b,"d":c}
    except Exception as e:
        print(f"[warn] KBO 상대전적 수집 실패: {e}")
    return recent, h2h

DOW = ["월","화","수","목","금","토","일"]

def parse_game_text(tx, dd):
    """경기 한 건의 텍스트를 (date, opp, ha) 로 파싱. 확신 없으면 None.
    엄격 규칙: 두산 + 정확히 한 상대팀만 등장(다경기 블록 배제), 날짜 확보 가능."""
    if "두산" not in tx: return None
    present = [tm for tm in TEAMS if tm in tx]
    if len(present) != 2: return None            # 두산 + 상대 1팀만 허용
    opp = next(tm for tm in present if tm != "두산")
    # 날짜: data-date(YYYYMMDD) 우선, 없으면 텍스트의 M/D
    y=mo=da=None
    if dd and re.match(r"\d{8}$", dd):
        y,mo,da = int(dd[:4]),int(dd[4:6]),int(dd[6:8])
    else:
        m = re.search(r"(\d{1,2})[.\-/](\d{1,2})", tx)
        if not m: return None
        mo,da = int(m.group(1)),int(m.group(2)); y=TODAY.year
    try: dt=datetime.date(y,mo,da)
    except: return None
    # 홈/원정: 매치업 표기에서 두산이 상대보다 먼저 나오면 홈(다음스포츠 '홈 원정' 순서), 아니면 원정
    i_du, i_op = tx.find("두산"), tx.find(opp)
    ha = "홈" if i_du < i_op else "원정"
    return dt, opp, ha

def fetch_schedule(page):
    """다음스포츠 일정 → 두산 향후 9경기(dt/op/ha/v). 날짜별 1경기로 dedup.
    확신 없는 항목은 버리고, 전부 실패하면 빈 배열(일정 섹션만 비고 나머지 정상)."""
    games=[]
    try:
        ymd = TODAY.strftime("%Y%m%d")
        page.goto(f"https://sports.daum.net/schedule/kbo?date={ymd}",
                  wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2500)
        # 되도록 '작은' 경기 요소만: 텍스트 길이 짧은 것 우선. data-date 동반 수집.
        items = page.evaluate("""() => {
          const out=[];
          const els=document.querySelectorAll('a, li, tr, [class*=game], [class*=match]');
          els.forEach(r=>{
            const tx=(r.innerText||'').replace(/\\s+/g,' ').trim();
            if(tx && tx.length<=60 && tx.includes('두산')){
              const dd=r.closest('[data-date]')?.getAttribute('data-date')||'';
              out.push(dd+'||'+tx);
            }
          });
          return out;
        }""")
        by_date={}
        for it in items:
            dd, tx = it.split("||",1)
            parsed = parse_game_text(tx, dd)
            if not parsed: continue
            dt, opp, ha = parsed
            if dt < TODAY: continue
            if dt in by_date: continue           # 날짜별 1경기(두산은 하루 1경기)
            v = STADIUM["두산"] if ha=="홈" else STADIUM.get(opp,"")
            by_date[dt] = {"dt":f"{dt.month}/{dt.day} {DOW[dt.weekday()]}","op":opp,"ha":ha,"v":v}
        games=[by_date[d] for d in sorted(by_date)][:9]
        if DEBUG: print(f"[debug] 일정 {len(games)}건:", games)
    except Exception as e:
        print(f"[warn] 일정 수집 실패(일정 섹션 비움): {e}")
        games=[]
    return games

# ───────────────────────── 계산 ─────────────────────────
def pythag(rs, ra, exp=1.83):
    if not rs or not ra: return 0.5
    return rs**exp / (rs**exp + ra**exp)

def monte_carlo_po(teams, sims=100000, seed=20260807):
    rng = np.random.default_rng(seed)
    talents, cur_w, cur_l, rem = [], [], [], []
    for t in teams:
        w,l = t["w"], t["l"]; g=t["g"]
        tal = 0.5*pythag(t["rs"],t["ra"]) + 0.5*(w/max(w+l,1))
        talents.append(min(max(tal,0.05),0.95))
        cur_w.append(w); cur_l.append(l); rem.append(max(144-g,0))
    talents=np.array(talents); cur_w=np.array(cur_w); rem=np.array(rem)
    n=len(teams)
    sim_w = np.empty((n,sims))
    for i in range(n):
        sim_w[i] = cur_w[i] + rng.binomial(rem[i], talents[i], size=sims)
    final_pct = sim_w / (cur_w[:,None] + np.array([144]*n)[:,None]*0 + (cur_w[:,None]+rem[:,None]))  # w/(w+l+rem)=w/games_total
    # games_total = cur_w+cur_l+rem 은 팀마다 다를 수 있으므로 정확히:
    games_total = np.array([teams[i]["w"]+teams[i]["l"]+rem[i] for i in range(n)])
    final_pct = sim_w / games_total[:,None]
    # 각 시뮬에서 승률 상위5 = 진출. 동률은 미세 난수로 처리.
    jitter = rng.uniform(0,1e-6,size=(n,sims))
    ranks = np.argsort(-(final_pct+jitter), axis=0)
    top5 = ranks[:5,:]
    counts = np.zeros(n)
    for i in range(n):
        counts[i] = np.sum(np.any(top5==i, axis=0))
    return (counts/sims*100.0)

# ───────────────────────── 조립 ─────────────────────────
def build():
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(locale="ko-KR", user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"))
        stand = fetch_standings(page)
        recent, h2h = fetch_recent_streak_h2h(page)
        sched = fetch_schedule(page)
        browser.close()

    # 순위 검증: 10팀 승/패 필수
    ok = [tm for tm in TEAMS if stand.get(tm,{}).get("w") is not None and stand.get(tm,{}).get("l") is not None]
    if len(ok) < 10:
        print(f"[error] 순위 수집 불완전: {ok} → 대시보드 갱신 중단(기존 유지).")
        sys.exit(1)

    # recent(최근10·연속) 병합
    for tm in TEAMS:
        if tm in recent:
            if recent[tm].get("f"): stand[tm]["f"]=recent[tm]["f"]
            if recent[tm].get("st"): stand[tm]["st"]=recent[tm]["st"]
        stand[tm].setdefault("f","-")
        stand[tm].setdefault("st","-")
        stand[tm].setdefault("d",0)
        stand[tm].setdefault("rs",0)
        stand[tm].setdefault("ra",0)
        stand[tm].setdefault("gb","0.0")
        if "pct" not in stand[tm]:
            w,l=stand[tm]["w"],stand[tm]["l"]
            stand[tm]["pct"]=f"{w/max(w+l,1):.3f}".replace("0.",".")

    # 순위 정렬(승률 desc → 승 desc)
    order = sorted(TEAMS, key=lambda tm:(-(stand[tm]["w"]/max(stand[tm]["w"]+stand[tm]["l"],1)), -stand[tm]["w"]))
    teams=[]
    for r,tm in enumerate(order, start=1):
        s=stand[tm]
        teams.append({"r":r,"n":tm,"g":s["w"]+s["l"]+s["d"],"w":s["w"],"d":s["d"],"l":s["l"],
                      "pct":s["pct"],"gb":s["gb"],"rs":s["rs"],"ra":s["ra"],
                      "f":s["f"],"st":s["st"],"po":0.0,"me":(tm=="두산")})
    # 경기수는 순위표 g 를 우선 신뢰
    for t in teams:
        if stand[t["n"]].get("g"): t["g"]=stand[t["n"]]["g"]

    po = monte_carlo_po(teams)
    for i,t in enumerate(teams): t["po"]=round(float(po[i]),1)

    # 상대전적: 수집 실패 팀은 빈 값(0-0-0)으로 두되 두산 제외
    h2h_list=[]
    for tm in TEAMS:
        if tm=="두산": continue
        rec=h2h.get(tm,{"w":0,"d":0,"l":0})
        h2h_list.append({"t":tm,"w":rec.get("w",0),"d":rec.get("d",0),"l":rec.get("l",0)})

    # 일정: 수집 성공하면 사용, 아니면 빈 배열(템플릿이 빈 일정 표시)
    games = sched if sched else []

    data = {
        "asOf": TODAY.strftime("%Y-%m-%d"),
        "seasonVs": 16,
        "schedNote": "🗓️ 일정·취소 정보는 KBO 공지 기준. 폭염 등으로 취소된 경기는 재편성 전까지 미확정입니다.",
        "teams": teams, "games": games, "h2h": h2h_list,
    }

    tpl = TEMPLATE.read_text(encoding="utf-8")
    html = tpl.replace("/*__INJECT_DATA__*/ null", json.dumps(data, ensure_ascii=False))
    OUT.write_text(html, encoding="utf-8")
    doosan = next(t for t in teams if t["me"])
    print(f"[ok] {data['asOf']} · 두산 {doosan['r']}위 {doosan['w']}-{doosan['d']}-{doosan['l']} · 가을 {doosan['po']}% · index.html 생성")

if __name__ == "__main__":
    build()
