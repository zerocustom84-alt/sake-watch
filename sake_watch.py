#!/usr/bin/env python3
"""오사케다 · 사케무라 신상품/재입고 알림 (sake-watch v2, 상주형)

한 번 실행되면 RUN_MINUTES 동안 켜진 채로 직접 반복한다.
- 빠른 확인 (낮 1분 / 새벽 5분): 가게의 RESTOCK 코너 + 신상품 코너(최신순 앞부분)
    · RESTOCK 코너에 새로 들어온 상품 → 재입고 (가게 분류 그대로)
    · 처음 보는 상품 → 신상품
    · RESTOCK 코너에서 빠진 상품은 상품 페이지로 품절 여부 확인 → 품절이면 기록
- 전체 확인 (낮 10분 / 새벽 30분): 전체 상품 목록으로 빠진 변화 보완
- 알림: ntfy (재입고·신상품만). 품절은 history.csv 기록만.
- 저장: state.json(현재 상태), history.csv(사건 일지), status.json(실행 상태)
    · 30분마다, 그리고 종료 시 GitHub 저장소에 커밋

환경변수
  NTFY_TOPIC, KEYWORDS, NOTIFY_SOLDOUT_NEW, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  RUN_MINUTES (기본 330), ONCE=1 (한 번만 돌고 종료, 테스트용), NO_GIT=1 (커밋 안 함)
"""
import csv
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

SHOPS = {
    "osakeda.com": {"name": "오사케다", "restock": "재입고", "new": "전-상품-1"},
    "sakemura.com": {"name": "사케무라", "restock": "재입고", "new": "전-상품"},
}
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
HISTORY_FILE = os.environ.get("HISTORY_FILE", "history.csv")
STATUS_FILE = os.environ.get("STATUS_FILE", "status.json")
UA = "Mozilla/5.0 (compatible; sake-watch personal restock notifier; low-frequency)"

RUN_MINUTES = float(os.environ.get("RUN_MINUTES", "330"))
FAST_DAY, FAST_NIGHT = 60, 300          # 빠른 확인 간격(초)
FAST_PEAK = 30                          # 입고가 몰리는 시간대 간격(초)
PEAK = [(8, 50, 12, 30), (17, 0, 19, 0)]  # 평일 KST (시작 시,분 ~ 끝 시,분)
FULL_DAY, FULL_NIGHT = 600, 1800        # 전체 확인 간격(초)
COMMIT_EVERY = 1800                     # 저장소 커밋 간격(초)
NIGHT = (2, 8)                          # 새벽 시간대 (KST, 시)
NEW_LIMIT = 60                          # 신상품 코너에서 볼 최신 상품 수
MAX_PAGES = 40
MIN_RATIO = 0.7
MAX_INDIVIDUAL = 30
LIST_CHUNK = 15
CURRENCY_SYMBOL = {"JPY": "¥", "KRW": "₩", "USD": "$"}

_backoff_until = 0.0                    # 429 등으로 쉬는 중이면 그 시각까지


def now_kst():
    return datetime.now(KST)


def is_peak():
    n = now_kst()
    if n.weekday() >= 5:
        return False
    m = n.hour * 60 + n.minute
    return any(a * 60 + b <= m < c * 60 + d for a, b, c, d in PEAK)


def fast_interval():
    if is_night():
        return FAST_NIGHT
    return FAST_PEAK if is_peak() else FAST_DAY


def is_night():
    return NIGHT[0] <= now_kst().hour < NIGHT[1]


# ---------------------------------------------------------------- 네트워크
class Throttled(Exception):
    pass


def http_get(url, tries=3):
    global _backoff_until
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 430):
                # 가게가 "너무 많다"고 함 → 10분 쉬고 이번 확인은 포기
                _backoff_until = time.time() + 600
                raise Throttled(url)
            if e.code in (500, 502, 503, 504) and i < tries - 1:
                time.sleep(10 * (i + 1))
                continue
            raise
        except Exception:
            if i < tries - 1:
                time.sleep(5)
                continue
            raise


def get_json(url):
    return json.loads(http_get(url))


def parse_product(p):
    variants = p.get("variants") or []
    prices = []
    for v in variants:
        try:
            prices.append(float(v.get("price")))
        except (TypeError, ValueError):
            pass
    return {
        "title": (p.get("title") or "").strip(),
        "handle": p.get("handle") or "",
        "available": any(v.get("available") for v in variants),
        "price": min(prices) if prices else None,
    }


def fetch_catalog(domain):
    """전체 상품 {id: item}"""
    items = {}
    for page in range(1, MAX_PAGES + 1):
        prods = get_json(f"https://{domain}/products.json?limit=250&page={page}").get("products", [])
        if not prods:
            break
        for p in prods:
            items[str(p["id"])] = parse_product(p)
        if len(prods) < 250:
            break
        time.sleep(1.5)
    return items


def fetch_collection(domain, handle, limit=250):
    h = urllib.parse.quote(handle)
    prods = get_json(f"https://{domain}/collections/{h}/products.json?limit={limit}").get("products", [])
    return {str(p["id"]): parse_product(p) for p in prods}


def fetch_available(domain, handle):
    d = get_json(f"https://{domain}/products/{urllib.parse.quote(handle)}.js")
    return bool(d.get("available"))


def fetch_currency(domain):
    try:
        return get_json(f"https://{domain}/cart.js").get("currency", "")
    except Exception:
        return ""


# ---------------------------------------------------------------- 알림
def notify(title, message, click=None):
    sent = False
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        body = {"topic": topic, "title": title, "message": message, "tags": ["sake"], "priority": 4}
        if click:
            body["click"] = click
        req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20).read()
        sent = True
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if tg_token and tg_chat:
        text = f"{title}\n{message}" + (f"\n{click}" if click else "")
        data = urllib.parse.urlencode({"chat_id": tg_chat, "text": text}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{tg_token}/sendMessage", data=data, timeout=20).read()
        sent = True
    if not sent:
        print("[알림 미설정]", title, "|", message.replace("\n", " / "))


def fmt_price(price, currency):
    if price is None:
        return ""
    sym = CURRENCY_SYMBOL.get(currency, "")
    num = f"{price:,.0f}" if float(price).is_integer() else f"{price:,.2f}"
    return f"{sym}{num}" if sym else f"{num} {currency}".strip()


def keyword_list():
    raw = os.environ.get("KEYWORDS", "")
    return [k.strip().lower() for k in raw.split(",") if k.strip()]


def matches(title, keywords):
    return not keywords or any(k in title.lower() for k in keywords)


def send_alerts(domain, name, events, currency):
    """events: [(종류, item)] 중 신상품·재입고만 알림"""
    keywords = keyword_list()
    ev = [(k, it) for k, it in events if k in ("신상품", "재입고") and matches(it["title"], keywords)]
    if not ev:
        return
    if len(ev) > MAX_INDIVIDUAL:
        n_new = sum(1 for k, _ in ev if k == "신상품")
        chunks = [ev[i:i + LIST_CHUNK] for i in range(0, len(ev), LIST_CHUNK)]
        for ci, chunk in enumerate(chunks, 1):
            lines = []
            for kind, it in chunk:
                price = fmt_price(it["price"], currency)
                lines.append(("🆕 " if kind == "신상품" else "🔁 ") + it["title"] + (f" ({price})" if price else ""))
            notify(f"[{name}] 신상품 {n_new} · 재입고 {len(ev) - n_new} ({ci}/{len(chunks)})",
                   "\n".join(lines), click=f"https://{domain}/collections/all")
            time.sleep(1)
    else:
        for kind, it in ev:
            price = fmt_price(it["price"], currency)
            notify(f"[{name}] {kind}", it["title"] + (f"\n{price}" if price else ""),
                   click=f"https://{domain}/products/{urllib.parse.quote(it['handle'])}")
            time.sleep(0.5)


# ---------------------------------------------------------------- 상태·기록
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"shops": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=0, sort_keys=True)


def append_history(rows):
    """rows: [(가게, 종류, 상품명, 가격)] — 키워드 필터와 무관하게 전부 기록"""
    if not rows:
        return
    stamp = now_kst().strftime("%Y-%m-%d %H:%M")
    new_file = not os.path.exists(HISTORY_FILE)
    with open(HISTORY_FILE, "a", encoding="utf-8-sig" if new_file else "utf-8", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["시각(KST)", "가게", "종류", "상품명", "가격"])
        for shop, kind, title, price in rows:
            w.writerow([stamp, shop, kind, title, price])


def record(domain, shop, events):
    """사건 기록 + 알림"""
    if not events:
        return
    cur = shop.get("currency", "")
    name = SHOPS[domain]["name"]
    append_history([(name, k, it["title"], fmt_price(it["price"], cur)) for k, it in events])
    for k, it in events:
        print(f"  [{name}] {k}: {it['title']}")
    send_alerts(domain, name, events, cur)


# ---------------------------------------------------------------- 확인 로직
def fast_check(domain, shop):
    """RESTOCK 코너 + 신상품 코너 확인"""
    cfg = SHOPS[domain]
    items = shop["items"]
    restock = fetch_collection(domain, cfg["restock"])
    time.sleep(1)
    newest = fetch_collection(domain, cfg["new"], NEW_LIMIT)
    prev_members = shop.get("restock_members")
    events = []

    if prev_members is None:
        # 처음: 현재 코너 구성을 기준으로만 저장
        shop["restock_members"] = sorted(restock)
        for pid, it in list(restock.items()) + list(newest.items()):
            items.setdefault(pid, it)
            items[pid].update(it)
        return events

    prev_set = set(prev_members)
    # 1) RESTOCK 코너에 새로 들어온 상품
    for pid, it in restock.items():
        old = items.get(pid)
        if pid not in prev_set and (old is None or not old.get("available")):
            # 코너에 올라왔는데 이미 다 팔렸으면 기록만 (1분 안에 품절된 한정 수량)
            events.append(("재입고" if it["available"] else "재입고(즉시품절)", it))
        elif pid in prev_set and old is not None:
            # 코너에 계속 있는 상품의 재고 변화 (사케무라는 품절돼도 코너에 남겨 둠)
            if old.get("available") and not it["available"]:
                events.append(("품절", it))
            elif not old.get("available") and it["available"]:
                events.append(("재입고", it))
        items[pid] = {**(old or {}), **it}
    # 2) 신상품 코너의 처음 보는 상품 / 그 밖의 재고 변화
    for pid, it in newest.items():
        old = items.get(pid)
        if old is None:
            events.append(("신상품" if it["available"] else "신상품(품절)", it))
        elif not old.get("available") and it["available"]:
            events.append(("재입고", it))
        elif old.get("available") and not it["available"]:
            events.append(("품절", it))
        items[pid] = {**(old or {}), **it}
    # 3) RESTOCK 코너에서 빠진 상품 → 품절인지 확인
    for pid in prev_set - set(restock):
        old = items.get(pid)
        if not old or pid in newest:
            continue
        try:
            time.sleep(1)
            avail = fetch_available(domain, old["handle"])
        except Throttled:
            raise
        except Exception:
            continue
        if old.get("available") and not avail:
            events.append(("품절", old))
        old["available"] = avail
    shop["restock_members"] = sorted(restock)
    return events


def full_check(domain, shop):
    """전체 상품 목록으로 보완"""
    current = fetch_catalog(domain)
    prev = shop["items"]
    if len(prev) >= 20 and len(current) < len(prev) * MIN_RATIO:
        print(f"  [{SHOPS[domain]['name']}] 상품 수 급감({len(prev)}→{len(current)}), 일시 오류로 보고 건너뜀")
        return []
    restock_set = set(shop.get("restock_members") or [])
    events = []
    for pid, it in current.items():
        old = prev.get(pid)
        if old is None:
            if pid in restock_set:
                events.append(("재입고", it))
            else:
                events.append(("신상품" if it["available"] else "신상품(품절)", it))
        elif not old.get("available") and it["available"]:
            events.append(("재입고", it))
        elif old.get("available") and not it["available"]:
            events.append(("품절", it))
    shop["items"] = current
    return events


# ---------------------------------------------------------------- 저장소 커밋 / 이어달리기
def git_commit(msg):
    if os.environ.get("NO_GIT") == "1":
        return
    files = [f for f in (STATE_FILE, HISTORY_FILE, STATUS_FILE) if os.path.exists(f)]
    try:
        subprocess.run(["git", "add", *files], check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
            return
        subprocess.run(["git", "commit", "-q", "-m", msg], check=True)
        for _ in range(3):
            subprocess.run(["git", "pull", "-q", "--rebase", "origin", "main"])
            if subprocess.run(["git", "push", "-q"]).returncode == 0:
                return
            time.sleep(5)
        print("  커밋 push 실패")
    except Exception as e:
        print("  커밋 실패:", e)


def dispatch_next():
    """다음 회차 미리 실행 요청 (현재 회차가 끝나면 이어서 시작)"""
    if os.environ.get("NO_GIT") == "1" or not os.environ.get("GH_TOKEN"):
        return
    r = subprocess.run(["gh", "workflow", "run", "sake-watch.yml", "--ref", "main"], capture_output=True, text=True)
    print("  다음 회차 요청:", "성공" if r.returncode == 0 else r.stderr.strip())


# ---------------------------------------------------------------- 메인 루프
def main():
    state = load_state()
    start = time.time()
    deadline = start + RUN_MINUTES * 60
    once = os.environ.get("ONCE") == "1"
    status = {"run_started": now_kst().strftime("%Y-%m-%d %H:%M:%S"), "fast_checks": 0,
              "full_checks": 0, "errors": 0, "throttled": 0, "fast_times": []}
    next_fast = next_full = 0.0
    next_commit = start + COMMIT_EVERY
    dispatched = False
    first_run_report = []

    while True:
        t = time.time()
        if t >= deadline:
            break
        if not dispatched and not once and deadline - t < 600:
            dispatch_next()
            dispatched = True

        if t < _backoff_until:
            time.sleep(min(30, _backoff_until - t))
            continue

        did = False
        # 0) 처음이면 전체 목록으로 기준 저장
        for domain, cfg in SHOPS.items():
            if state["shops"].get(domain, {}).get("items"):
                continue
            try:
                items = fetch_catalog(domain)
                state["shops"][domain] = {"items": items, "currency": fetch_currency(domain)}
                first_run_report.append(f"{cfg['name']} {len(items):,}개")
                print(f"[{cfg['name']}] 첫 실행: 상품 {len(items)}개 기준 저장")
                next_full = time.time() + (FULL_NIGHT if is_night() else FULL_DAY)
                did = True
            except Throttled:
                status["throttled"] += 1
                break
            except Exception as e:
                status["errors"] += 1
                print(f"[{cfg['name']}] 첫 목록 실패: {e}")
        if first_run_report:
            notify("sake-watch 연결 완료 🍶", "기준 저장: " + ", ".join(first_run_report))
            first_run_report = []

        # 1) 빠른 확인 (RESTOCK·신상품 코너) — 가게 분류를 먼저 반영하려고 전체 확인보다 앞에
        if time.time() >= next_fast and time.time() >= _backoff_until:
            for domain, cfg in SHOPS.items():
                shop = state["shops"].get(domain)
                if not shop:
                    continue
                try:
                    record(domain, shop, fast_check(domain, shop))
                except Throttled:
                    status["throttled"] += 1
                    print(f"[{cfg['name']}] 가게가 요청을 제한함 → 10분 쉼")
                    break
                except Exception as e:
                    status["errors"] += 1
                    print(f"[{cfg['name']}] 빠른 확인 실패: {e}")
                time.sleep(1)
            stamp = now_kst().strftime("%H:%M:%S")
            status["fast_checks"] += 1
            status["last_fast"] = now_kst().strftime("%Y-%m-%d %H:%M:%S")
            status["fast_times"] = (status["fast_times"] + [stamp])[-30:]
            print(f"{stamp} 확인 완료 (빠른 {status['fast_checks']}회, 전체 {status['full_checks']}회)")
            next_fast = time.time() + fast_interval()
            did = True

        # 2) 전체 확인
        if time.time() >= next_full and time.time() >= _backoff_until:
            for domain, cfg in SHOPS.items():
                shop = state["shops"].get(domain)
                if not shop:
                    continue
                try:
                    if not shop.get("currency"):
                        shop["currency"] = fetch_currency(domain)
                    record(domain, shop, full_check(domain, shop))
                except Throttled:
                    status["throttled"] += 1
                    print(f"[{cfg['name']}] 가게가 요청을 제한함 → 10분 쉼")
                    break
                except Exception as e:
                    status["errors"] += 1
                    print(f"[{cfg['name']}] 전체 확인 실패: {e}")
            status["full_checks"] += 1
            status["last_full"] = now_kst().strftime("%Y-%m-%d %H:%M:%S")
            next_full = time.time() + (FULL_NIGHT if is_night() else FULL_DAY)
            did = True

        if did:
            save_state(state)
            with open(STATUS_FILE, "w", encoding="utf-8") as f:
                json.dump(status, f, ensure_ascii=False, indent=1)

        if once:
            break
        if time.time() >= next_commit:
            git_commit("update state")
            next_commit = time.time() + COMMIT_EVERY
        time.sleep(max(1, min(next_fast, next_full, deadline) - time.time()))

    save_state(state)
    status["run_ended"] = now_kst().strftime("%Y-%m-%d %H:%M:%S")
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=1)
    git_commit("update state (end of run)")


if __name__ == "__main__":
    sys.exit(main())
