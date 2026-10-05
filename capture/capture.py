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
    CANDIDATE_CACHE_HOURS             候補一覧のキャッシュを使う時間（既定 20）
    FRESH_PAGES                       キャッシュ利用時に取り直す新着のページ数（既定 5）
    TIME_BUDGET_MIN                   プロセス開始からこの分数を過ぎたら撮影を打ち切る（既定 300）

TARGETS の例:

    [{"site":"<site>","service":"<service>","floor":"<floor>"}]

`site` / `service` / `floor` は FloorList API が返す値をそのまま使う。
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

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

# 全件の候補一覧は3フロアで約3,000リクエスト・約2時間かかる（2026-09-15 実測）。
# 毎回取り直すと 330分のジョブ上限に当たって撮影が中断するので、R2 に1日1回ぶんだけ保存して使い回す。
CANDIDATE_CACHE_HOURS = float(os.getenv("CANDIDATE_CACHE_HOURS") or "20")
FRESH_PAGES = int(os.getenv("FRESH_PAGES") or "5")
# ジョブの timeout-minutes(330) より先に自分で止め、索引を保存して正常終了する（準備に数分かかる分を残す）
TIME_BUDGET_MIN = float(os.getenv("TIME_BUDGET_MIN") or "300")
_T_START = time.time()


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


def fetch_candidates(target: Dict[str, str], pages: int,
                     sorts: Tuple[str, ...] = ("date", "rank")) -> Tuple[List[Tuple[str, str]], bool]:
    """(content_id, tachiyomi_url) の一覧と、API失敗なしで取り切れたかを返す。

    新着(date)とランキング(rank)の両方を見る。

    ★ 2026-09-11: 以前は pages=5(=新着上位500件)で打ち切っていたが、これだと
    「新着500件」の枠は日々の新規追加で1週間ほどで入れ替わるため、公開から
    1週間経ってランキング外の作品は候補に二度と出てこない恒久的な穴になっていた
    （実測: 試し読みありの7,104件中、画像を持つのは905件＝13%）。
    「撮影済みはindexに残るので回を重ねれば自然に埋まる」は誤りで、
    そもそも候補にすら上がっていなかった。

    pages はAPIのoffset上限50,000件に届くまで大きく取る。空になった時点で
    自動的に打ち切るので（下のbreak）、フロアの実サイズを超えるコストはかからない。
    重いのは撮影(1件約40秒)であって一覧取得ではないので、一覧を広く見ること自体は安価。
    """
    out: Dict[str, str] = {}
    complete = True
    for sort in sorts:
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
                complete = False
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
    return list(out.items()), complete


def _cache_key(target: Dict[str, str]) -> str:
    return (f"{KEY_PREFIX}_cache/candidates_{target.get('site', '')}_"
            f"{target.get('service', '')}_{target['floor']}.json.gz")


def load_candidates(s3, target: Dict[str, str], pages: int, save: bool = True) -> List[Tuple[str, str]]:
    """候補一覧を返す。新しいキャッシュがあれば、それに新着だけ取り直して前に足す。"""
    key = _cache_key(target)
    try:
        o = s3.get_object(Bucket=core.R2_BUCKET, Key=key)
        d = json.loads(gzip.decompress(o["Body"].read()).decode("utf-8"))
        age_h = (datetime.now(JST) - datetime.fromisoformat(d["generated_at"])).total_seconds() / 3600
        cached = [(str(c), str(u)) for c, u in d.get("items") or []]
        if cached and 0 <= age_h < CANDIDATE_CACHE_HOURS:
            fresh, _ = fetch_candidates(target, FRESH_PAGES, sorts=("date",))
            merged: Dict[str, str] = dict(fresh)
            for c, u in cached:
                merged.setdefault(c, u)
            print(f"[capture]   {target['floor']}: 候補一覧はキャッシュを使用（{age_h:.1f}時間前・"
                  f"{len(cached)}件）＋新着の取り直し {len(fresh)}件")
            return list(merged.items())
    except Exception:
        pass

    items, complete = fetch_candidates(target, pages)
    if save and complete and items:
        try:
            body = gzip.compress(json.dumps({"generated_at": _now(), "items": items}, ensure_ascii=False).encode("utf-8"))
            s3.put_object(Bucket=core.R2_BUCKET, Key=key, Body=body, ContentType="application/gzip")
        except Exception as e:
            print(f"[capture]   候補一覧のキャッシュ保存に失敗（撮影は続行）: {str(e)[:60]}")
    elif not complete:
        print(f"[capture]   {target['floor']}: API失敗で一覧が途中までなのでキャッシュしない")
    return items


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


PUBLISHED_SITEMAP = (os.getenv("PUBLISHED_SITEMAP") or "https://manga.sakuhin-navi.com/sitemap.xml").strip()


def load_published_ids() -> Optional[Set[str]]:
    """サイトが実際に公開している作品の content_id を集める。

    ## なぜ要るか

    撮影はDMMのフロア一覧の順に進むので、サイトに載っていない作品まで撮っていた。
    2026-10-05 時点で撮影済み 37,003 作品に対し、catalog-2 の公開は 16,626 作品。
    差分の2万作品ぶんの画像（R2で57GB・月110円）は1枚も使われていない。

    公開ページはサイトマップに出るので、そこから拾えば**公開が増えれば自動で追従**する。
    取得に失敗したら None を返し、呼び出し側は従来どおり全件を対象にする（撮影を止めない）。

    PUBLISHED_SITEMAP="" を渡すと、この絞り込みを無効にできる。
    """
    if not PUBLISHED_SITEMAP:
        return None
    try:
        import urllib.request

        def get(u: str) -> str:
            req = urllib.request.Request(u, headers={"User-Agent": "catalog-tools/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8", "replace")

        body = get(PUBLISHED_SITEMAP)
        locs = re.findall(r"<loc>([^<]+)</loc>", body)
        pages = [u for u in locs if u.endswith(".xml")] or [PUBLISHED_SITEMAP]
        ids: Set[str] = set()
        for u in pages:
            doc = body if u == PUBLISHED_SITEMAP else get(u)
            for w in re.findall(r"<loc>[^<]*/works/([^/<]+)/?</loc>", doc):
                ids.add(w.strip())
        if len(ids) < 1000:
            print(f"[capture] 公開作品の取得が少なすぎます（{len(ids)}件）。絞り込みは行いません", file=sys.stderr)
            return None
        print(f"[capture] 公開中の作品 {len(ids):,} 件をサイトマップから取得（これ以外は撮らない）")
        return ids
    except Exception as e:
        print(f"[capture] 公開作品の取得に失敗（絞り込みなしで続行）: {str(e)[:70]}", file=sys.stderr)
        return None


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
    ap.add_argument("--pages", type=int, default=500,
                     help="APIを何ページ見るか(1ページ100件)。offset>50000で自動打ち切りなので"
                          "大きくしてもフロアの実サイズ以上のコストは掛からない")
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

    published = load_published_ids()

    todo: List[Tuple[str, str]] = []
    for t in targets:
        cands = load_candidates(s3, t, args.pages, save=not args.dry_run)
        new = [(c, u) for c, u in cands if c not in done]
        skipped = 0
        if published is not None:
            before = len(new)
            new = [(c, u) for c, u in new if c in published]
            skipped = before - len(new)
        print(f"[capture] {t.get('site')}/{t.get('service')}/{t['floor']}: "
              f"候補 {len(cands)} / 未撮影 {len(new) + skipped}"
              + (f" / うちサイト未掲載のため除外 {skipped}" if skipped else ""))
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
        elapsed_min = (time.time() - _T_START) / 60
        if elapsed_min > TIME_BUDGET_MIN:
            print(f"[capture] 開始から{elapsed_min:.0f}分（上限 {TIME_BUDGET_MIN:.0f}分）に達したので打ち切り。"
                  f"残り {len(todo) - i + 1} 件は次回")
            break
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
