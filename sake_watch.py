#!/usr/bin/env python3
"""오사케다 · 사케무라 신상품/재입고 알림 (sake-watch)

두 쇼핑몰(Shopify)의 전체 상품 목록을 읽어 지난번 상태와 비교하고,
- 새 상품이 올라왔거나(구매 가능한 것만)
- 품절이던 상품이 다시 구매 가능해지면
ntfy(및 선택적으로 텔레그램)로 폰에 알림을 보냅니다.

환경변수
  NTFY_TOPIC          ntfy 토픽 이름 (필수)
  KEYWORDS            쉼표 구분 키워드. 지정하면 상품명에 포함된 것만 알림 (선택)
  NOTIFY_SOLDOUT_NEW  1 이면 품절 상태로 등록된 신상품도 알림 (기본 0)
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID  텔레그램도 받고 싶을 때 (선택)
  STATE_FILE          상태 저장 파일 (기본 state.json)
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SHOPS = {"osakeda.com": "오사케다", "sakemura.com": "사케무라"}
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
UA = "Mozilla/5.0 (compatible; sake-watch personal restock notifier)"
MAX_PAGES = 40          # 250개 x 40쪽 = 최대 1만 개
MIN_RATIO = 0.7         # 받은 상품 수가 지난번의 70% 미만이면 일시 오류로 보고 이번 회차는 건너뜀
MAX_INDIVIDUAL = 8      # 한 가게에서 변화가 이보다 많으면 1건으로 묶어서 알림
CURRENCY_SYMBOL = {"JPY": "¥", "KRW": "₩", "USD": "$"}


# ---------------------------------------------------------------- 네트워크
def http_get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 430, 500, 502, 503) and i < tries - 1:
                time.sleep(10 * (i + 1))
                continue
            raise
        except Exception:
            if i < tries - 1:
                time.sleep(5)
                continue
            raise


def fetch_catalog(domain):
    """{상품id: {title, handle, available, price}}"""
    items = {}
    for page in range(1, MAX_PAGES + 1):
        data = json.loads(http_get(f"https://{domain}/products.json?limit=250&page={page}"))
        prods = data.get("products", [])
        if not prods:
            break
        for p in prods:
            variants = p.get("variants") or []
            prices = []
            for v in variants:
                try:
                    prices.append(float(v.get("price")))
                except (TypeError, ValueError):
                    pass
            items[str(p["id"])] = {
                "title": (p.get("title") or "").strip(),
                "handle": p.get("handle") or "",
                "available": any(v.get("available") for v in variants),
                "price": min(prices) if prices else None,
            }
        if len(prods) < 250:
            break
        time.sleep(1)
    return items


def fetch_currency(domain):
    try:
        return json.loads(http_get(f"https://{domain}/cart.js")).get("currency", "")
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
        req = urllib.request.Request(
            "https://ntfy.sh/",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
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
        print("[알림 미설정] NTFY_TOPIC 이 비어 있어 콘솔에만 출력:", title, "|", message)


def fmt_price(price, currency):
    if price is None:
        return ""
    sym = CURRENCY_SYMBOL.get(currency, "")
    num = f"{price:,.0f}" if float(price).is_integer() else f"{price:,.2f}"
    return f"{sym}{num}" if sym else f"{num} {currency}".strip()


# ---------------------------------------------------------------- 상태
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"shops": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=0, sort_keys=True)


def keyword_list():
    raw = os.environ.get("KEYWORDS", "")
    return [k.strip().lower() for k in raw.split(",") if k.strip()]


def matches(title, keywords):
    if not keywords:
        return True
    t = title.lower()
    return any(k in t for k in keywords)


# ---------------------------------------------------------------- 메인
def main():
    state = load_state()
    keywords = keyword_list()
    notify_soldout_new = os.environ.get("NOTIFY_SOLDOUT_NEW", "0") == "1"
    first_run_report = []

    for domain, name in SHOPS.items():
        try:
            current = fetch_catalog(domain)
        except Exception as e:
            print(f"[{name}] 상품 목록을 못 받음 → 이번 회차 건너뜀: {e}")
            continue

        shop_state = state["shops"].get(domain)
        currency = fetch_currency(domain) or (shop_state or {}).get("currency", "")

        if not shop_state:
            state["shops"][domain] = {"items": current, "currency": currency}
            first_run_report.append(f"{name} {len(current):,}개")
            print(f"[{name}] 첫 실행: 상품 {len(current)}개 기준 저장 (알림 없음)")
            continue

        prev = shop_state.get("items", {})
        if len(prev) >= 20 and len(current) < len(prev) * MIN_RATIO:
            print(f"[{name}] 상품 수 급감({len(prev)}→{len(current)}), 일시 오류로 보고 건너뜀")
            continue

        changes = []  # (종류, 상품)
        for pid, item in current.items():
            old = prev.get(pid)
            if old is None:
                if item["available"] or notify_soldout_new:
                    changes.append(("신상품", item))
            elif not old.get("available") and item["available"]:
                changes.append(("재입고", item))

        changes = [c for c in changes if matches(c[1]["title"], keywords)]
        print(f"[{name}] 상품 {len(current)}개, 알림 대상 {len(changes)}건")

        if len(changes) > MAX_INDIVIDUAL:
            n_new = sum(1 for k, _ in changes if k == "신상품")
            n_re = len(changes) - n_new
            lines = [f"· {it['title']}" for _, it in changes[:10]]
            if len(changes) > 10:
                lines.append(f"…외 {len(changes) - 10}개")
            notify(
                f"[{name}] 신상품 {n_new} · 재입고 {n_re}",
                "\n".join(lines),
                click=f"https://{domain}/collections/all",
            )
        else:
            for kind, it in changes:
                price = fmt_price(it["price"], currency)
                notify(
                    f"[{name}] {kind}",
                    it["title"] + (f"\n{price}" if price else ""),
                    click=f"https://{domain}/products/{urllib.parse.quote(it['handle'])}",
                )

        state["shops"][domain] = {"items": current, "currency": currency}

    if first_run_report:
        notify("sake-watch 연결 완료 🍶",
               "기준 저장: " + ", ".join(first_run_report) + "\n이제부터 신상품·재입고 시 알려드려요.")

    save_state(state)


if __name__ == "__main__":
    sys.exit(main())
