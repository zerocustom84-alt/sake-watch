#!/usr/bin/env python3
"""최근 N일 신상품 상세 목록을 recent_products.json 으로 저장 (분석용, 수동 실행)"""
import json
import os
from datetime import datetime, timedelta, timezone

import sake_watch as sw

DAYS = int(os.environ.get("DAYS", "60"))
KST = timezone(timedelta(hours=9))


def main():
    cutoff = datetime.now(KST) - timedelta(days=DAYS)
    rows = []
    for domain, name in sw.SHOPS.items():
        for page in range(1, sw.MAX_PAGES + 1):
            data = json.loads(sw.http_get(f"https://{domain}/products.json?limit=250&page={page}"))
            prods = data.get("products", [])
            if not prods:
                break
            for p in prods:
                pub = p.get("published_at") or p.get("created_at")
                if not pub:
                    continue
                t = datetime.fromisoformat(pub).astimezone(KST)
                if t < cutoff:
                    continue
                variants = p.get("variants") or []
                rows.append({
                    "shop": name,
                    "published": t.strftime("%Y-%m-%d %H:%M"),
                    "title": (p.get("title") or "").strip(),
                    "vendor": p.get("vendor") or "",
                    "type": p.get("product_type") or "",
                    "tags": p.get("tags") or [],
                    "variants": [{"name": v.get("title"), "price": v.get("price"),
                                  "available": v.get("available")} for v in variants],
                    "available": any(v.get("available") for v in variants),
                    "url": f"https://{domain}/products/{p.get('handle')}",
                })
            if len(prods) < 250:
                break
            sw.time.sleep(1)
    rows.sort(key=lambda r: r["published"], reverse=True)
    with open("recent_products.json", "w", encoding="utf-8") as f:
        json.dump({"generated": datetime.now(KST).strftime("%Y-%m-%d %H:%M"), "days": DAYS, "rows": rows},
                  f, ensure_ascii=False, indent=0)
    print(f"{len(rows)}개 저장")


if __name__ == "__main__":
    main()
