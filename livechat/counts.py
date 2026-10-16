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
import time
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


def fetch(url: str, tries: int = 3) -> str | None:
    """取得する。403が返ることがあるので間を空けて数回試す。

    DXLIVEは断続的に403を返す（2026-10-16 実測で7回中2回）。
    ブラウザに近いヘッダを付けると通りやすい。
    """
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "ja,en;q=0.8",
        "Referer": re.sub(r"^(https?://[^/]+).*", r"\1/", url),
    }
    last = ""
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=40) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            last = str(e)[:80]
            if i < tries - 1:
                time.sleep(5 * (i + 1))
    print(f"[counts] 取得失敗 {url}: {last}", file=sys.stderr)
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


def last_recorded() -> datetime | None:
    """最後に記録できた時刻。ファイルが無ければ None。"""
    if not OUT_DIR.is_dir():
        return None
    times = []
    for f in sorted(OUT_DIR.glob("*.jsonl"))[-2:]:
        for line in f.read_text(encoding="utf-8").splitlines()[-200:]:
            if not line.strip():
                continue
            try:
                t = json.loads(line).get("t")
                if t:
                    times.append(datetime.fromisoformat(t))
            except Exception:
                continue
    return max(times) if times else None


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
        # 一時的な403で毎回赤くすると、本当に壊れたときに気づけなくなる。
        # 直近24時間に1件も取れていない場合だけエラーにする。
        last = last_recorded()
        stale = last is None or (now - last).total_seconds() > 24 * 3600
        msg = (f"直近の記録は {last.isoformat(timespec='minutes') if last else 'なし'}。"
               "取得先がブロックしている可能性があります。")
        if stale:
            print(f"::error title=人数を24時間以上記録できていません::{msg}", file=sys.stderr)
            return 1
        print(f"::warning title=今回の人数取得に失敗::{msg}", file=sys.stderr)
        print("[counts] 今回は取れませんでした（直近は取れているので続行）")
        return 0
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
