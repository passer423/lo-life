# 怎么加一个品牌的尺码表

先判断一件事：**官方给的是品牌级 S/M/L，还是商品级实测尺寸？**

- 品牌级 S/M/L：按下面的步骤建 `<品牌 id>.json`；
- 商品级实测：**不要**硬拆成 S/M/L，建一个 `kind: "item_level"` 的条目，
  `categories` 留空，尺寸写进 `content/newarrivals/<id>.json` 的 `size` 字段。

AP 和 Baby 属于后者：AP 官方以 JIS 9 号为基准、商品页给单件实测；Baby 官方服装是
单一尺码。三家官网都没有"某品牌 JSK 的 S/M/L 表"，硬编出来只会让用户按不存在的码下单。

## 品牌级尺码表

1. 复制 `_template.json` 成 `<品牌 id>.json`（文件名会作为品牌 key）；
2. 按**官方尺码表**逐项填 `sizes`（单位：厘米；填的是**成衣实测尺寸**，不是建议身体尺寸）；
3. 把 `fit` 设成 `正码` / `偏小` / `偏大` —— 这一项直接影响结论，别乱填；
4. `source` 写清依据（官方页面 URL + 取数日期），将来对不上能回溯；
5. 不用重启：读表是每次请求现读的。

## 字段说明

| 字段 | 说明 |
|---|---|
| `brand` | 唯一 id，接口里用它查表 |
| `display_name` | 界面上显示的名字 |
| `kind` | `size_chart`（品牌级 S/M/L）或 `item_level`（只有商品级实测） |
| `fit` | `正码` / `偏小` / `偏大`。偏小会把门槛抬高 2cm，偏大降低 2cm |
| `chart_version` | 你在什么时候录的这份表 |
| `source` | 依据来源。**空着等于不可信** |
| `note` | 给用户看的补充说明；`item_level` 时写清"请指定具体商品" |
| `verified` | 是否按官方来源核对过。没声明就是 `false`，界面会标"未核对" |
| `categories.<品类>.key` | 该品类**判断时看哪几维**。JSK 看胸腰，OP 加肩宽，SK 只看腰 |
| `categories.<品类>.ease` | 每维富余量，默认 胸/臀 4cm、腰 2cm、肩 0 |
| `categories.<品类>.sizes` | 每个码的成衣尺寸 |

## 商品级尺寸怎么来

写进 `content/newarrivals/<id>.json` 的 `size` 字段：

```json
{
  "size": {
    "kind": "free",
    "label": "F",
    "bust": [91, 101],
    "waist": [74, 86],
    "length": [89, 89],
    "source": "AP 官网商品页 https://…（2026-11-02 取）",
    "note": "单一尺码；区间来自抽褶结构。"
  }
}
```

用脚本从官网商品页抓，默认 dry-run：

```bash
python scripts/fetch_sizes.py --id 2026-10-ap-bainiao
python scripts/fetch_sizes.py --brand ap
python scripts/fetch_sizes.py --brand baby --write
python scripts/fetch_sizes.py --id <id> --from-html 页面.html --write   # 淘宝/登录墙
```

AP 商品页是 HTML，Baby 走 Shopify `.js`（只含当前商品）；淘宝用 `--from-html`。
脚本只在 `size` 为空时写回，已有数据要覆盖必须加 `--overwrite`。

## 四条容易搞错的地方

- **填成衣尺寸，不是身体尺寸。** 官方表有时给"适合胸围 86"，那是身体尺寸，要换算成成衣尺寸，或者干脆别用那一栏。
- **肩宽不要加富余量。** 加多了会塌肩；肩宽不够也没法改，所以它是硬约束。
- **同一个品牌不同系列可能版型不同。** 遇到这种情况就拆成两个 id（`xx-经典`、`xx-新系列`），别硬塞进一份表 —— `note` 里写清差异。
- **没有官方来源就不要填数字。** `verified: false` 的表只能显示"未核对"，不能当事实推荐；宁可说"没有表"，也不要编一个码。
