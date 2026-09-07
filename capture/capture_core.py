# -*- coding: utf-8 -*-
"""fetch_samples.py

tachiyomi_url が設定された作品の試し読み画像を Playwright スクリーンショットで取得し
Cloudflare R2 にアップロードする。R2 URL を sample_images_large に保存する。

事前準備:
  pip install playwright boto3
  playwright install chromium

環境変数:
  R2_ACCESS_KEY_ID      : Cloudflare R2 アクセスキー ID
  R2_SECRET_ACCESS_KEY  : Cloudflare R2 シークレットアクセスキー

実行:
  python src/fetch_samples.py
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

# 接続先は環境変数から受け取る。ここに値を書かないこと。
# バケット名・アカウントID・公開URLはいずれも「どのサイト向けか」の手がかりになる。
R2_BUCKET = (os.getenv("R2_BUCKET") or "").strip()
R2_ACCOUNT_ID = (os.getenv("CLOUDFLARE_ACCOUNT_ID") or "").strip()
R2_ENDPOINT = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com" if R2_ACCOUNT_ID else ""
R2_PUBLIC = (os.getenv("R2_PUBLIC_BASE") or "").strip().rstrip("/")
MAX_PAGES = 13
SLEEP_BETWEEN = 2.0

_PAGE_SELECTORS = [
    "canvas",
    ".pageSlider",
    ".p-book-main",
    "#reader",
    ".book-reader",
]


def _trim_margins(data: bytes) -> bytes:
    """上下のグレー/単色余白を動的検出して除去。失敗時は元画像を返す"""
    try:
        from PIL import Image, ImageStat
        import io

        img = Image.open(io.BytesIO(data))
        width, height = img.size
        if width < 50 or height < 50:
            return data
        gray = img.convert("L")

        threshold = 10  # stddev が これ以上なら内容あり
        step = 4  # 何ピクセル刻みで走査するか

        top = 0
        for y in range(0, height, step):
            row = gray.crop((0, y, width, y + 1))
            if ImageStat.Stat(row).stddev[0] > threshold:
                top = max(0, y - step)
                break

        bottom = height
        for y in range(height - 1, -1, -step):
            row = gray.crop((0, y, width, y + 1))
            if ImageStat.Stat(row).stddev[0] > threshold:
                bottom = min(height, y + step + 1)
                break

        # 安全装置: 最大でも上下 20% までしかトリムしない
        max_trim = int(height * 0.2)
        top = min(top, max_trim)
        bottom = max(bottom, height - max_trim)

        if top <= 0 and bottom >= height:
            return data
        if bottom - top < height * 0.5:
            return data  # 切り過ぎは安全のためスキップ

        cropped = img.crop((0, top, width, bottom))
        out = io.BytesIO()
        cropped.convert("RGB").save(out, format="JPEG", quality=85)
        return out.getvalue()
    except Exception as e:
        print(f"  [trim skip] {e}")
        return data


def _looks_like_loading_or_blank(data: bytes) -> bool:
    """Drop viewer loading screens and nearly blank captures."""
    try:
        from PIL import Image, ImageStat
        import io

        img = Image.open(io.BytesIO(data)).convert("RGB")
        width, height = img.size
        if width < 100 or height < 100:
            return True

        gray = img.convert("L")
        stat = ImageStat.Stat(gray)
        center = gray.crop(
            (
                int(width * 0.2),
                int(height * 0.2),
                int(width * 0.8),
                int(height * 0.8),
            )
        )
        center_stat = ImageStat.Stat(center)

        # DMM viewer loading captures are a flat dark gray page with a small
        # progress bar. Real manga pages have far more luminance variation.
        if len(data) < 80000 and stat.stddev[0] < 20 and center_stat.stddev[0] < 25:
            return True
        if stat.stddev[0] < 10 and center_stat.stddev[0] < 15:
            return True
        return False
    except Exception as e:
        print(f"  [image validation skip] {e}")
        return False


def _make_s3(access_key: str, secret_key: str):
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )


def _screenshot_pages(tachiyomi_url: str, retry: int = 0) -> list[bytes]:
    """Playwright でビューアーを開き、各ページのスクリーンショットを撮る。失敗時は最大3回リトライ"""
    from playwright.sync_api import sync_playwright

    screenshots: list[bytes] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1200, "height": 2000},
        )
        # 年齢確認ゲートを抜けるためのクッキー。
        # FANZA のビューアは book.dmm.co.jp、DMM.com一般は book.dmm.com と
        # ドメインが違うので、両方に入れないと片方で弾かれる。
        context.add_cookies([
            {"name": "age_check_done", "value": "1", "domain": ".dmm.co.jp", "path": "/"},
            {"name": "cklg", "value": "ja", "domain": ".dmm.co.jp", "path": "/"},
            {"name": "age_check_done", "value": "1", "domain": ".dmm.com", "path": "/"},
            {"name": "cklg", "value": "ja", "domain": ".dmm.com", "path": "/"},
            {"name": "ckcy", "value": "1", "domain": ".dmm.co.jp", "path": "/"},
            {"name": "ckcy", "value": "1", "domain": ".dmm.com", "path": "/"},
        ])
        page = context.new_page()

        response_status = 0
        try:
            response = page.goto(tachiyomi_url, wait_until="domcontentloaded", timeout=60000)
            response_status = response.status if response else 0
        except Exception as e:
            print(f"  [goto timeout] {str(e)[:60]}")

        try:
            if response_status >= 400:
                print(f"  [viewer missing] HTTP {response_status}")
                return []

            # ビューアの完全な読み込みを待つ
            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                pass
            page.wait_for_timeout(10000)

            try:
                body_text = page.inner_text("body")
                title_text = page.title()
                error_hints = (
                    "404 Not Found",
                    "Not Found",
                    "指定されたページが見つかりません",
                    "ページが見つかりません",
                    "お探しのページは見つかりませんでした",
                )
                if any(h in body_text or h in title_text for h in error_hints):
                    print("  [viewer missing] error page")
                    return []
            except Exception:
                pass

            has_viewer = False
            for sel in _PAGE_SELECTORS:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        has_viewer = True
                        break
                except Exception:
                    continue
            if not has_viewer:
                print("  [viewer missing] no reader element")
                return []

            for i in range(MAX_PAGES):
                # 販売促進ページ（「続きは本編でお楽しみください」）に到達したら終了
                try:
                    body_text = page.inner_text("body")
                    if "続きは本編" in body_text or "本編はこちら" in body_text:
                        break
                except Exception:
                    pass

                shot = None
                small_canvas = False
                # ★ビューアは canvas を複数持ち、画面外に控えを並べている
                #   （実測: 4個のうち3個が x=-1200 の位置にいる）。
                #   query_selector("canvas") は DOM順の先頭を返すだけなので、
                #   ページ送り後に画面外の canvas を掴むと
                #   page.screenshot(clip=負の座標) が失敗し、1枚で打ち切られていた。
                #   → **ビューポート内にある canvas** を選び、要素自身を撮る。
                #   element.screenshot() は座標計算を自前でやらずに済む。
                try:
                    vw = (page.viewport_size or {}).get("width") or 1200
                    for c in page.query_selector_all("canvas"):
                        try:
                            if not c.is_visible():
                                continue
                            box = c.bounding_box()
                            if not box:
                                continue
                            # 画面外（左右どちらか）は控えの canvas。飛ばす。
                            if box["x"] + box["width"] <= 1 or box["x"] >= vw - 1:
                                continue
                            # 小さい canvas は販売促進ページ
                            if box["width"] < 500 or box["height"] < 500:
                                small_canvas = True
                                continue
                            if box["width"] > 100 and box["height"] > 100:
                                shot = c.screenshot(type="jpeg", quality=85)
                                small_canvas = False
                                break
                        except Exception:
                            continue
                except Exception:
                    pass

                if small_canvas:
                    break

                if not shot:
                    for sel in _PAGE_SELECTORS[1:]:
                        try:
                            el = page.query_selector(sel)
                            if el and el.is_visible():
                                shot = el.screenshot(type="jpeg", quality=85)
                                break
                        except Exception:
                            continue

                if not shot:
                    break

                if shot:
                    shot = _trim_margins(shot)
                    if _looks_like_loading_or_blank(shot):
                        print("  [capture skip] loading/blank image")
                        if screenshots:
                            break
                        return []
                    # 前回と同じ画像なら（ページ送りが失敗した）終了
                    if screenshots and shot == screenshots[-1]:
                        break
                    screenshots.append(shot)

                if i < MAX_PAGES - 1:
                    try:
                        page.mouse.click(150, 600)
                        page.wait_for_timeout(2000)
                    except Exception:
                        break

        except Exception as e:
            print(f"  [screenshot error] {e}")
        finally:
            browser.close()

    # 画像が 1 枚も取得できず、リトライ可能な場合
    if not screenshots and retry < 3:
        print(f"  [retry {retry + 1}/3]")
        time.sleep(2)
        return _screenshot_pages(tachiyomi_url, retry + 1)

    # 最後の画像は販売促進ページの可能性があるので削除（2枚以上ある場合のみ）
    if len(screenshots) > 1:
        screenshots.pop()

    return screenshots


def _upload(s3, content_id: str, idx: int, data: bytes, prefix: str = "") -> str:
    """R2 にアップロードして公開 URL を返す"""
    key = f"{prefix}{content_id}/sample_{idx}.jpg"
    s3.put_object(
        Bucket=R2_BUCKET,
        Key=key,
        Body=data,
        ContentType="image/jpeg",
    )
    return f"{R2_PUBLIC}/{key}"


