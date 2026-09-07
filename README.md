# catalog-tools

複数のカタログサイトで共用する、重い処理をまとめたリポジトリ。

各サイトの本体リポジトリは private で、GitHub Actions の無料枠（2,000分/月）を
共有している。時間のかかる処理をそこで回すと枠を食い潰すため、
**public リポジトリ（Actions 無制限）にこちらへ切り出している。**

サイト側とは**オブジェクトストレージ経由**でだけやり取りする。
このリポジトリからサイト側のリポジトリへは一切アクセスしない（認証情報も持たない）。

## 収録ツール

### `capture/` — 試し読みビューアの撮影

DMM の商品情報API は電子書籍について `tachiyomi.affiliateURL`（試し読みビューア）を
返すが `sampleImageURL` は返さない。静的なサンプル画像は自前で用意する必要がある。

ビューアを Playwright で開いて各ページを撮影し、画像と索引 `samples_index.json` を
ストレージへ書き出す。利用側は索引を1つ読めばよい。

```json
{
  "updated_at": "2026-09-07T12:00:00+09:00",
  "items": {
    "<content_id>": { "images": ["https://.../sample_1.jpg"], "on": "2026-09-07" }
  }
}
```

1作品あたり約40秒。既定の300件で約3.5時間。毎日 JST 04:00 に自動実行。

#### 設定

接続先と対象は**すべて環境変数で渡す**。リポジトリには何も書かない。

| 変数 | 種別 | 内容 |
|---|---|---|
| `DMM_API_ID` / `DMM_AFFILIATE_ID` | secret | 商品情報APIの認証 |
| `CLOUDFLARE_ACCOUNT_ID` | secret | エンドポイントの組み立て |
| `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | secret | ストレージ認証 |
| `R2_PUBLIC_BASE` | secret | 公開URLのベース |
| `R2_BUCKET` | variable | 保存先バケット |
| `TARGETS` | variable | 対象フロアのJSON配列 |
| `KEY_PREFIX` / `INDEX_KEY` | variable | キーの接頭辞・索引名（省略可） |

`TARGETS` の例:

```json
[{"site":"<site>","service":"<service>","floor":"<floor>"}]
```

`site` / `service` / `floor` は FloorList API が返す値をそのまま使う。

## 追加するときの原則

- **サイトを特定できる情報を置かない。** バケット名・アカウントID・公開URL・
  ドメイン・アフィリエイトIDはすべて Secrets / Variables 経由にする
- **サイト側リポジトリへの認証情報を持たない。** 結果はストレージに書き、
  サイト側が取りに来る
- Actions のログも公開される。出力に識別情報を含めない
