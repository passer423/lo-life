# 店铺登记表

一家店一份 JSON：`content/shops/<brand>.json`，`brand` 和 `content/newarrivals/*.json`
里的 `brand` 对齐。加一家店不用改代码、不用重启（每次请求现读）。

## 为什么要有这张表

「这家店的页面在哪」是**品牌级**的事实，不随系列变化。抄进每一条上新等于两处维护
同一份事实——改一次名要改几十个文件，迟早就漏一个。

有它之后两件事才成立：

1. **取图入口**：`scripts/fetch_images.py --brand ap` 用 `page_url` 去抓候选图。
2. **链接兜底**：条目没写 `link` 时用 `shop_url`。但界面会标成「去店铺」而不是
   「去商品」——店铺页和商品页指向的东西不一样，不能混。

## 字段

| 字段 | 说明 |
|---|---|
| `brand` | 唯一 id，必须和 `newarrivals` / `size` 里的 `brand` 一致 |
| `brand_name` | 显示名 |
| `shop_url` | 店铺首页地址。**填不带 `?` 的地址**（原因见下） |
| `page_url` | 取图用的页面：品牌官网新作列表，或店铺的「上新」列表页 |
| `catalog` | `html`（默认）或 `shopify`。Shopify 店优先用官方公开的商品 JSON，字段齐全不用猜 DOM |
| `item_href_pattern` | **哪条 `<a>` 算商品页**。淘宝是 `item.htm`，AP 官网是 `ProductDetail.aspx`，Baby 是 `/products/` |
| `note` | 备注，比如"日本官网 / 淘宝店 / 微店" |
| `source` | 依据：地址是从哪抄来的、什么时候 |

## 已登记的两家

| 品牌 | 来源 | 解析方式 |
|---|---|---|
| `ap` | `angelicpretty.com` 的新作列表（`cat=ITENEW`） | HTML。列表页里**图和标题是两个指向同一商品的 `<a>`**，按地址归并才不会得到两条残废记录 |
| `baby` | `store.babyssb.co.jp/products.json` | Shopify 官方商品 JSON。标题、主图、商品地址都是结构化的 |

两家官网都**没有中文系列名**，所以条目里要写日文原名，或者把中文名放进 `aliases`。
`aliases` 也会参与检索，旧名字照样搜得到。


## 三条要注意的

- **别填带查询参数的地址。** 淘宝/天猫的 robots.txt 是 `Disallow: /*?*`，带 `?`
  的地址一律不允许抓——脚本会直接拒绝并告诉你原因。
- **`page_url` 指向列表页时，靠"图旁边的文字里有系列名"来配对。** 所以系列名要和
  页面上写的一致（多一个空格没关系，多了括号或英文缩写就会配不上）。配不上时
  脚本会说"没配上"，可以用 `--pick N` 手动指定，或者干脆改用 `--from-html`。
- **这里只存地址，不存图片。** 图片落在 `data/media/`（不进 git、进备份 zip）。

## 取图怎么跑

```bash
# 先看一眼候选，不下载、不改文件
python scripts/fetch_images.py --brand ap --from-page https://某个商品页

# 确认没问题再落盘
python scripts/fetch_images.py --brand ap --write

# 淘宝店铺页抓不动时：浏览器里打开 → 另存为 HTML → 零请求解析
python scripts/fetch_images.py --id 2026-10-ap-bainiao --from-html 白鸟笼.html --write
```
