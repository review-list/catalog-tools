#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ビューアの構造を調べ、ページ送りの効く操作を突き止める。

撮影が1ページで止まる原因を切り分けるための使い捨て診断。
本番の撮影経路には影響しない。

やること:
  1. ビューアを開いて構造を出す（URL / canvas / iframe / ボタン類）
  2. ページ送りの候補操作を順に試し、**canvas の中身が変わったか**を見る
  3. 効いた操作を報告する

使い方:
    python capture/debug_viewer.py            # TARGETS の先頭から1件
    python capture/debug_viewer.py --cid xxx  # content_id 指定
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

API_ID = (os.getenv("DMM_API_ID") or "").strip()
AFFILIATE_ID = (os.getenv("DMM_AFFILIATE_ID") or "").strip()
ENDPOINT = "https://api.dmm.com/affiliate/v3/ItemList"


def pick_target(cid: str = "") -> tuple[str, str]:
    """(content_id, tachiyomi_url) を1件返す。"""
    targets = json.loads(os.getenv("TARGETS") or "[]")
    for t in targets:
        q = {
            "api_id": API_ID, "affiliate_id": AFFILIATE_ID, "output": "json",
            "site": t.get("site", ""), "service": t.get("service", ""),
            "floor": t["floor"], "hits": 100, "sort": "rank",
        }
        with urllib.request.urlopen(ENDPOINT + "?" + urllib.parse.urlencode(q), timeout=40) as r:
            items = ((json.loads(r.read().decode()).get("result") or {}).get("items")) or []
        for it in items:
            c = str(it.get("content_id") or "")
            tach = it.get("tachiyomi") or {}
            u = str(tach.get("affiliateURL") or tach.get("URL") or "") if isinstance(tach, dict) else ""
            if not u:
                continue
            if cid and c != cid:
                continue
            return c, u
    return "", ""


def direct(u: str) -> str:
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
        lurl = (q.get("lurl") or [""])[0]
        if lurl:
            return urllib.parse.unquote(lurl)
    except Exception:
        pass
    return u


def canvas_hash(page) -> str:
    """今表示されている canvas の中身のハッシュ。ページが変われば変わる。"""
    try:
        el = page.query_selector("canvas")
        if not el:
            return ""
        return hashlib.md5(el.screenshot()).hexdigest()[:12]
    except Exception as e:
        return "ERR:" + str(e)[:20]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cid", default="")
    ap.add_argument("--url", default="", help="URLを直接指定（TARGETSを使わない）")
    args = ap.parse_args()

    if args.url:
        cid, tach = "(指定URL)", args.url
    else:
        cid, tach = pick_target(args.cid)
    if not tach:
        print("[debug] 対象が見つかりません")
        sys.exit(1)
    url = direct(tach)
    print(f"[debug] content_id : {cid}")
    print(f"[debug] 開くURL    : {url[:110]}")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1200, "height": 2000})
        ctx.add_cookies([
            {"name": n, "value": v, "domain": d, "path": "/"}
            for d in (".dmm.co.jp", ".dmm.com")
            for n, v in (("age_check_done", "1"), ("cklg", "ja"), ("ckcy", "1"))
        ])
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        page.wait_for_timeout(10000)

        print(f"[debug] 最終URL    : {page.url[:110]}")
        print(f"[debug] title      : {page.title()[:70]}")

        # --- 構造 ---
        for sel in ("canvas", "iframe", "img", "button", "[role=button]", "a"):
            try:
                els = page.query_selector_all(sel)
                print(f"[debug] {sel:<14} {len(els)} 個")
                if sel == "canvas":
                    for i, e in enumerate(els[:4]):
                        b = e.bounding_box()
                        print(f"[debug]     canvas[{i}] visible={e.is_visible()} box={b}")
            except Exception as e:
                print(f"[debug] {sel:<14} 取得失敗 {str(e)[:40]}")

        # ページ番号らしき表示
        try:
            txt = page.inner_text("body")[:400].replace("\n", " ")
            print(f"[debug] body先頭   : {txt[:200]}")
        except Exception:
            pass

        # --- ページ送りの候補を順に試す ---
        base = canvas_hash(page)
        print(f"[debug] 初期canvas : {base}")
        if not base:
            print("[debug] canvas が取れないので以降は無意味。ここで終了")
            browser.close()
            return

        box = None
        try:
            el = page.query_selector("canvas")
            box = el.bounding_box() if el else None
        except Exception:
            pass
        cx = (box["x"] + box["width"] / 2) if box else 600
        cy = (box["y"] + box["height"] / 2) if box else 1000
        lx = (box["x"] + box["width"] * 0.15) if box else 150
        rx = (box["x"] + box["width"] * 0.85) if box else 1050

        attempts = [
            ("現行: mouse.click(150,600)", lambda: page.mouse.click(150, 600)),
            ("canvas左15%をクリック", lambda: page.mouse.click(lx, cy)),
            ("canvas右85%をクリック", lambda: page.mouse.click(rx, cy)),
            ("canvas中央をクリック", lambda: page.mouse.click(cx, cy)),
            ("キー ArrowLeft", lambda: page.keyboard.press("ArrowLeft")),
            ("キー ArrowRight", lambda: page.keyboard.press("ArrowRight")),
            ("キー Space", lambda: page.keyboard.press("Space")),
            ("キー PageDown", lambda: page.keyboard.press("PageDown")),
            ("ホイール下スクロール", lambda: page.mouse.wheel(0, 800)),
        ]
        prev = base
        for name, act in attempts:
            try:
                act()
                page.wait_for_timeout(2500)
                h = canvas_hash(page)
                changed = (h != prev and h and not h.startswith("ERR"))
                print(f"[debug] {'★変化あり' if changed else '  変化なし'}  {name:<26} -> {h}")
                if changed:
                    prev = h
            except Exception as e:
                print(f"[debug]   失敗      {name:<26} {str(e)[:50]}")

        browser.close()


if __name__ == "__main__":
    main()
