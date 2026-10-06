#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""サイトに載っていない作品の試し読み画像を R2 から消す。

## なぜ要るか

撮影はDMMのフロア一覧の順に進んでいたので、サイトに載っていない作品まで撮っていた。
2026-10-05 の実測で **249,806オブジェクト・57GB**（無料枠10GB）。撮影済み 37,003作品に対し、
catalog-2 の公開は 16,970作品で、**差分の約2万作品ぶんは1枚も使われていない**。

公開中の作品はサイトマップから取る（capture.py と同じ方法）。それ以外を消す。
撮影側は `226e7d1` で「公開中の作品だけ撮る」ようになっているので、消したものが
すぐ撮り直されることはない。将来その作品を公開したら、自動でまた撮られる。

## 安全側の作り

- 既定は **--dry-run**（何件・何GB消えるかを出すだけ）。消すときは明示的に --apply
- 公開作品の取得に失敗、または1,000件を下回ったら**何もしない**
- 索引（samples_index.json）にある作品だけを対象にする。索引に無いキーは触らない
- 消した作品は索引からも外す（次回の撮影対象の判定が狂わないように）

    python capture/cleanup.py                 # 試算のみ
    python capture/cleanup.py --apply         # 実際に削除
    python capture/cleanup.py --apply --limit 5000
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

sys.path.insert(0, str(Path(__file__).resolve().parent))

import capture_core as core  # noqa: E402
from capture import INDEX_KEY, load_index, load_published_ids, save_index  # noqa: E402


def iter_objects(s3, prefix: str):
    token = None
    while True:
        kw = {"Bucket": core.R2_BUCKET, "MaxKeys": 1000}
        if prefix:
            kw["Prefix"] = prefix
        if token:
            kw["ContinuationToken"] = token
        r = s3.list_objects_v2(**kw)
        for o in r.get("Contents", []):
            yield o
        if not r.get("IsTruncated"):
            return
        token = r.get("NextContinuationToken")


def main() -> int:
    ap = argparse.ArgumentParser(description="サイトに載っていない作品の画像をR2から消す")
    ap.add_argument("--apply", action="store_true", help="実際に削除する（既定は試算のみ）")
    ap.add_argument("--limit", type=int, default=0, help="消す作品数の上限（0で全部）")
    ap.add_argument("--stats", action="store_true", help="バケット全体の件数と容量を数えて終わる")
    args = ap.parse_args()

    ak = (os.getenv("R2_ACCESS_KEY_ID") or "").strip()
    sk = (os.getenv("R2_SECRET_ACCESS_KEY") or "").strip()
    if not (ak and sk and core.R2_BUCKET and core.R2_ACCOUNT_ID):
        print("[cleanup] R2の設定が足りません", file=sys.stderr)
        return 1

    if args.stats:
        s3 = core._make_s3(ak, sk)
        n = size = 0
        for o in iter_objects(s3, ""):
            n += 1
            size += o["Size"]
        print(f"[cleanup] バケット全体: {n:,} オブジェクト / {size/1024**3:.2f} GB")
        return 0

    published = load_published_ids()
    if published is None:
        print("[cleanup] 公開中の作品が取れないので、安全のため何もしません", file=sys.stderr)
        return 1

    s3 = core._make_s3(ak, sk)
    idx = load_index(s3)
    items: Dict[str, dict] = idx.get("items") or {}
    if not items:
        print("[cleanup] 索引が空です", file=sys.stderr)
        return 1

    unused = [cid for cid in items if cid not in published]
    if args.limit > 0:
        unused = unused[: args.limit]
    print(f"[cleanup] 撮影済み {len(items):,} 作品 / 公開中 {len(published):,} 作品 / "
          f"未使用 {len(unused):,} 作品")

    # 未使用作品のオブジェクトを数える（prefix ごとに list する）
    from capture import KEY_PREFIX
    prefix = KEY_PREFIX
    targets: List[str] = []
    total_bytes = 0
    for i, cid in enumerate(unused, 1):
        for o in iter_objects(s3, f"{prefix}{cid}/"):
            targets.append(o["Key"])
            total_bytes += o["Size"]
        if i % 2000 == 0:
            print(f"[cleanup]   {i:,}/{len(unused):,} 作品を走査（{total_bytes/1024**3:.1f} GB）", flush=True)

    print(f"[cleanup] 削除対象 {len(targets):,} オブジェクト / {total_bytes/1024**3:.2f} GB")
    if not args.apply:
        print("[cleanup] 試算のみ（削除するには --apply）")
        return 0
    if not targets:
        return 0

    deleted = 0
    for i in range(0, len(targets), 1000):
        chunk = [{"Key": k} for k in targets[i:i + 1000]]
        s3.delete_objects(Bucket=core.R2_BUCKET, Delete={"Objects": chunk, "Quiet": True})
        deleted += len(chunk)
        if deleted % 10000 == 0:
            print(f"[cleanup]   {deleted:,} / {len(targets):,} 削除", flush=True)

    for cid in unused:
        items.pop(cid, None)
    idx["items"] = items
    save_index(s3, idx)
    print(f"[cleanup] 完了: {deleted:,} オブジェクト（{total_bytes/1024**3:.2f} GB）を削除 / "
          f"索引は {len(items):,} 作品になりました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
