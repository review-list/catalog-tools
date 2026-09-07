#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DMM の試し読みビューアを撮影して R2 に置く。

## これは何か

DMM の商品情報API(ItemList) は、電子書籍について `tachiyomi.affiliateURL`
（試し読みビューアへのリンク）を返す。一方 `sampleImageURL` は返さない。
つまり「試し読みはできるが、静的なサンプル画像は API から取れない」。

このツールはビューアを Playwright で開いてページを撮影し、画像を R2 に置く。
結果は `samples_index.json` として R2 に書き出す。利用側はこの1ファイルを読めばよい。

## なぜ独立したリポジトリなのか

Playwright での撮影は1作品あたり約40秒かかる。対象が数千件あると
GitHub Actions の無料枠(private 2,000分/月)では回せない。
public リポジトリは Actions が無制限なので、重い撮影だけをここに分離している。

**このリポジトリには、どのサイトのためのものかが分かる情報を置かない。**
バケット名・アカウントID・公開URL・対象フロアはすべて環境変数で受け取る。

## 出力

    R2://{bucket}/{prefix}{content_id}/sample_{n}.jpg
    R2://{bucket}/{index_key}          ← samples_index.json

`samples_index.json` の形:

    {
      "updated_at": "2026-09-07T12:00:00+09:00",
      "items": {
        "b104atint02672": {"images": ["https://.../sample_1.jpg", ...], "on": "2026-09-07"}
      }
    }

利用側は content_id で引いて画像URLを得る。

## 環境変数

    DMM_API_ID / DMM_AFFILIATE_ID     商品情報APIの認証
    CLOUDFLARE_ACCOUNT_ID             R2 エンドポイントの組み立てに使う
    R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY
    R2_BUCKET                         保存先バケット
    R2_PUBLIC_BASE                    公開URLのベース（https://pub-xxxx.r2.dev）
    TARGETS                           対象フロアのJSON（下記）
    KEY_PREFIX                        R2キーの接頭辞（省略可）
    INDEX_KEY                         インデックスのキー（既定 samples_index.json）
    MAX_WORKS                         1回の撮影上限（既定 300）

TARGETS の例:

    [{"site":"<site>","service":"<service>","floor":"<floor>"}]

`site` / `service` / `floor` は FloorList API が返す値をそのまま使う。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

import capture_core as core

JST = timezone(timedelta(hours=9))

API_ID = (os.getenv("DMM_API_ID") or "").strip()
AFFILIATE_ID = (os.getenv("DMM_AFFILIATE_ID") or "").strip()
ENDPOINT = "https://api.dmm.com/affiliate/v3/ItemList"

KEY_PREFIX = (os.getenv("KEY_PREFIX") or "").strip()
INDEX_KEY = (os.getenv("INDEX_KEY") or "samples_index.json").strip()
MAX_WORKS = int(os.getenv("MAX_WORKS") or "300")

# APIの取得上限。1クエリ 50,000件・offset>50000 は HTTP 400。
API_HITS = 100
API_SLEEP = 0.4


def _now() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(JST).date().isoformat()


def _targets() -> List[Dict[str, str]]:
    raw = (os.getenv("TARGETS") or "").strip()
    if not raw:
        print("[capture] TARGETS が未設定です。何もしません。", file=sys.stderr)
        return []
    try:
        t = json.loads(raw)
    except Exception as e:
        print(f"[capture] TARGETS のJSONが壊れています: {e}", file=sys.stderr)
        return []
    return [x for x in t if isinstance(x, dict) and x.get("floor")]


def fetch_candidates(target: Dict[str, str], pages: int) -> List[Tuple[str, str]]:
    """(content_id, tachiyomi_url) を集める。

    新着(date)とランキング(rank)の両方を見る。古い作品まで遡る必要は無い
    ―― 撮影済みは index に残るので、回を重ねれば自然に埋まっていく。
    """
    out: Dict[str, str] = {}
    for sort in ("date", "rank"):
        for page in range(pages):
            offset = 1 + page * API_HITS
            if offset > 50000:
                break
            q = {
                "api_id": API_ID, "affiliate_id": AFFILIATE_ID, "output": "json",
                "site": target.get("site", ""),
                "service": target.get("service", ""),
                "floor": target["floor"],
                "hits": API_HITS, "sort": sort, "offset": offset,
            }
            url = ENDPOINT + "?" + urllib.parse.urlencode(q)
            try:
                with urllib.request.urlopen(url, timeout=40) as r:
                    d = json.loads(r.read().decode("utf-8"))
            except Exception as e:
                print(f"[capture]   API失敗 {target['floor']}/{sort}/{offset}: {str(e)[:60]}")
                break
            items = ((d.get("result") or {}).get("items")) or []
            if not items:
                break
            for it in items:
                cid = str(it.get("content_id") or "")
                tach = it.get("tachiyomi") or {}
                u = ""
                if isinstance(tach, dict):
                    u = str(tach.get("affiliateURL") or tach.get("URL") or "")
                # cid はURLエンコードされている場合がある。両方見る。
                if cid and u and ("cid=" in u.lower() or "cid%3d" in u.lower()):
                    out[cid] = u
            time.sleep(API_SLEEP)
    return list(out.items())


def direct_url(u: str) -> str:
    """撮影に使う「直接」URLを返す。

    APIが返すのは al.fanza.co.jp / al.dmm.com のアフィリエイト用リダイレクトURLで、
    実体は lurl パラメータに入っている。Playwright でリダイレクトを踏ませると
    ページ送りが効かず1枚しか撮れなかったため、撮影時は展開した実URLを直接開く。
    （アフィリエイトIDが必要なのは利用側が出すリンクであって、撮影には要らない）
    """
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
        lurl = (q.get("lurl") or [""])[0]
        if lurl:
            return urllib.parse.unquote(lurl)
    except Exception:
        pass
    return u


def load_index(s3) -> Dict[str, Any]:
    try:
        o = s3.get_object(Bucket=core.R2_BUCKET, Key=INDEX_KEY)
        d = json.loads(o["Body"].read().decode("utf-8"))
        if isinstance(d, dict) and isinstance(d.get("items"), dict):
            return d
    except Exception:
        pass
    return {"updated_at": "", "items": {}}


def save_index(s3, idx: Dict[str, Any]) -> None:
    idx["updated_at"] = _now()
    s3.put_object(
        Bucket=core.R2_BUCKET, Key=INDEX_KEY,
        Body=json.dumps(idx, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="試し読みビューアを撮影して R2 に置く")
    ap.add_argument("--max", type=int, default=MAX_WORKS, help="1回の撮影上限")
    ap.add_argument("--pages", type=int, default=5, help="APIを何ページ見るか(1ページ100件)")
    ap.add_argument("--dry-run", action="store_true", help="撮影も保存もしない")
    args = ap.parse_args()

    missing = [k for k, v in (
        ("DMM_API_ID", API_ID), ("DMM_AFFILIATE_ID", AFFILIATE_ID),
        ("R2_BUCKET", core.R2_BUCKET), ("CLOUDFLARE_ACCOUNT_ID", core.R2_ACCOUNT_ID),
        ("R2_PUBLIC_BASE", core.R2_PUBLIC),
    ) if not v]
    if missing:
        print("[capture] 環境変数が未設定: " + ", ".join(missing), file=sys.stderr)
        sys.exit(1)

    ak = (os.getenv("R2_ACCESS_KEY_ID") or "").strip()
    sk = (os.getenv("R2_SECRET_ACCESS_KEY") or "").strip()
    if not ak or not sk:
        print("[capture] R2の認証情報が未設定です。", file=sys.stderr)
        sys.exit(1)

    targets = _targets()
    if not targets:
        return

    s3 = core._make_s3(ak, sk)
    idx = load_index(s3)
    done = set(idx["items"].keys())
    print(f"[capture] 撮影済み {len(done)} 件（index: {INDEX_KEY}）")

    todo: List[Tuple[str, str]] = []
    for t in targets:
        cands = fetch_candidates(t, args.pages)
        new = [(c, u) for c, u in cands if c not in done]
        print(f"[capture] {t.get('site')}/{t.get('service')}/{t['floor']}: "
              f"候補 {len(cands)} / 未撮影 {len(new)}")
        todo.extend(new)

    # 重複除去（フロアをまたいで同じ作品が出ることがある）
    seen = set()
    uniq: List[Tuple[str, str]] = []
    for c, u in todo:
        if c in seen:
            continue
        seen.add(c)
        uniq.append((c, u))
    todo = uniq[: max(args.max, 0)]

    print(f"[capture] 今回の対象 {len(todo)} 件（上限 {args.max}）")
    if args.dry_run:
        for c, _ in todo[:10]:
            print(f"[capture]   {c}")
        print("[capture] --dry-run のため撮影しません")
        return
    if not todo:
        print("[capture] 対象なし")
        return

    t0 = time.time()
    ok = ng = 0
    for i, (cid, tach) in enumerate(todo, 1):
        try:
            shots = core._screenshot_pages(direct_url(tach))
        except Exception as e:
            print(f"[capture] {i}/{len(todo)} {cid} 撮影失敗: {str(e)[:60]}")
            ng += 1
            continue
        if not shots:
            # 撮れなかった作品は index に残さない。次回また対象になる。
            print(f"[capture] {i}/{len(todo)} {cid} 0枚")
            ng += 1
            continue
        urls = []
        for n, data in enumerate(shots, 1):
            try:
                urls.append(core._upload(s3, cid, n, data, prefix=KEY_PREFIX))
            except Exception as e:
                print(f"[capture]   アップロード失敗 {cid}#{n}: {str(e)[:50]}")
        if not urls:
            ng += 1
            continue
        idx["items"][cid] = {"images": urls, "on": _today()}
        ok += 1
        print(f"[capture] {i}/{len(todo)} {cid} {len(urls)}枚  (経過 {time.time()-t0:.0f}秒)", flush=True)

        # 20件ごとに index を保存する。ジョブが時間切れで落ちても成果を失わない。
        if ok % 20 == 0:
            save_index(s3, idx)

    save_index(s3, idx)
    print(f"[capture] 完了: 成功 {ok} / 失敗 {ng} / 累計 {len(idx['items'])} 件 "
          f"/ 所要 {time.time()-t0:.0f}秒")


if __name__ == "__main__":
    main()
