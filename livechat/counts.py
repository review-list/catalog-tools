#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ライブチャットの「いま何人オンラインか」を記録する。

## なぜこのリポジトリに居るか

このリポジトリは **public なので Actions が無制限**。時間帯別に取るには1日に何回も
実行する必要があり、private リポジトリでやると月240分かかって無料枠（2,000分）を
超える。中身はDMMとは無関係で、catalog-engine の `livechat/` と対になっている。

## 何を記録するか（そして何を記録しないか）

DXLIVE は `/json/performer_jp` で配信者一覧を公開しており、robots.txt でも明示的に
許可されている（`# Disallow: /json/` をコメントアウトし「un-disallowed」と註記）。

ただし DTI の宣伝禁止ポリシー8条が「ライブチャットサイトでは、特定のパフォーマーに
ついて宣伝目的の使用」を禁じているので、**個人名は1件も保存せず人数だけ**を残す。
「何時なら人がいるか」は利用者が本当に知りたいことで、公式はどこにも出していない。

出力: data/counts/YYYY-MM.jsonl（1行1回・追記のみ）

    python livechat/counts.py            # 取得して追記
    python livechat/counts.py --dry-run  # 取得するが書かない
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "counts"
JST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# robots.txt で許可されているものだけ。個人名を保存しないので一覧の中身は数えるだけ。
TARGETS = [
    {"service": "dxlive", "scope": "jp", "url": "https://www.dxlive.com/json/performer_jp"},
    {"service": "dxlive", "scope": "all", "url": "https://www.dxlive.com/json/performer"},
]


def fetch(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ja"})
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        print(f"[counts] 取得失敗 {url}: {str(e)[:80]}", file=sys.stderr)
        return None


def parse_js_var(text: str):
    """`var x = {...};` の形で返るので中のJSONだけ取り出す。"""
    m = re.search(r"=\s*(\{.*\})\s*;?\s*$", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def size_of(v) -> int:
    if isinstance(v, list):
        return len(v)
    if isinstance(v, dict):
        return len(v)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="ライブチャットのオンライン人数を記録する")
    ap.add_argument("--dry-run", action="store_true", help="書かずに表示だけ")
    args = ap.parse_args()

    now = datetime.now(JST)
    rows = []
    for t in TARGETS:
        row = {"t": now.isoformat(timespec="seconds"), "hour": now.hour, "dow": now.weekday(),
               "service": t["service"], "scope": t["scope"]}
        text = fetch(t["url"])
        data = parse_js_var(text) if text else None
        if data is None:
            row["error"] = "取得できない/形が変わった"
        else:
            row["online"] = size_of(data.get("online"))
            row["free"] = size_of(data.get("free"))    # いま無料で見られる人数
            row["busy"] = size_of(data.get("busy"))    # 接客中
        rows.append(row)
        print(f"[counts] {t['service']}/{t['scope']}: " +
              (f"オンライン {row.get('online')} / 無料 {row.get('free')} / 接客中 {row.get('busy')}"
               if "error" not in row else row["error"]))

    if all("error" in r for r in rows):
        print("[counts] 全部失敗したので書きません", file=sys.stderr)
        return 1
    if args.dry_run:
        print("[counts] --dry-run のため書きません")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    f = OUT_DIR / f"{now.strftime('%Y-%m')}.jsonl"
    with f.open("a", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"[counts] {f.relative_to(ROOT)} に {len(rows)} 行を追記")
    return 0


if __name__ == "__main__":
    sys.exit(main())
