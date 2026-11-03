"""端到端冒烟测试：对一个**正在运行**的服务跑一遍真实流程。

为什么要有它：每一层坏掉症状都长得很像（页面空白、数字不对、按钮无反应），
把它固化成一条命令，改完 10 秒就知道有没有打穿。

五条纪律，都是踩出来的：

* **只清理自己造的数据**：冒烟会注册一个临时账号（随机用户名），结束时**删除账号**，
  连带它的关注与偏好一起消失——绝不碰你真实的账号。
* **拒答是正常结果**：知识库外的问题必须返回 count=0，这不是失败。
* **不带 charset 就当真坏了**：显式检查响应头里有 utf-8，否则命令行验证中文必乱码。
* **不断言依赖"今天"的数字**：新鲜度随日期变化，写死"7 天内必须有 N 条"过两周就红。
* **授权与业务分开断言**：401（没登录）和 404/400（校验失败）是两件事。

用法：
    python -m app.main          # 一个终端
    python scripts/smoke.py     # 另一个终端
"""

from __future__ import annotations

import base64
import json
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8787"
ROOT = Path(__file__).resolve().parents[1]
BOUNDARY = "----lolifesmoke"
PROBE_BRAND = "baby"
PROBE_STYLE = "甜系"
PROBE_MISS = "量子计算"     # 内容里肯定没有，用来验命中回报

failures: list[str] = []
checks = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if condition:
        print(f"  [ok] {name}")
        return
    failures.append(name)
    print(f"  [FAIL] {name}" + (f"  —— {detail}" if detail else ""))


def _send(path: str, *, method: str = "GET", data: bytes | None = None,
          content_type: str | None = None, token: str = "") -> tuple[int, bytes, dict]:
    headers: dict[str, str] = {}
    if data is not None and content_type:
        headers["Content-Type"] = content_type
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=180) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def header(headers: dict, name: str) -> str:
    """报文头的键大小写不固定（uvicorn 发出来是小写），必须忽略大小写地查。"""
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return ""


def _json(payload: bytes) -> dict:
    try:
        return json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError:
        return {}


def items_source(feed: dict) -> dict:
    """从 feed 里挑一条做探针：优先第一条（有 id 就能验商品级尺码/关键词）。"""
    items = feed.get("items") or []
    return items[0] if items else {}


def get_json(path: str, token: str = "") -> tuple[int, dict, dict]:
    status, payload, headers = _send(path, token=token)
    return status, _json(payload), headers


def post_json(path: str, body: dict, token: str = "") -> tuple[int, dict]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    status, payload, _ = _send(path, method="POST", data=data,
                               content_type="application/json; charset=utf-8", token=token)
    return status, _json(payload)


def put_json(path: str, body: dict, token: str = "") -> tuple[int, dict]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    status, payload, _ = _send(path, method="PUT", data=data,
                               content_type="application/json; charset=utf-8", token=token)
    return status, _json(payload)


def post_png(path: str, filename: str = "smoke.png") -> tuple[int, dict]:
    """最小 multipart 请求体。PowerShell 5.1 没有 -Form，requests 又不是应用依赖。"""
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    body = (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    ).encode("utf-8") + png + f"\r\n--{BOUNDARY}--\r\n".encode("utf-8")
    status, payload, _ = _send(path, method="POST", data=body,
                               content_type=f"multipart/form-data; boundary={BOUNDARY}")
    return status, _json(payload)


def citations_valid(payload: dict) -> bool:
    """``[n]`` 必须落在 1..命中条数 内——上游 verify 的核心判据，这里独立复核。"""
    count = int(payload.get("count") or 0)
    for index in re.findall(r"\[(\d+)\]", str(payload.get("answer", ""))):
        if not 1 <= int(index) <= count:
            return False
    return True


def main() -> int:
    print(f"目标：{BASE}\n")
    try:
        status, health, headers = get_json("/api/health")
    except (urllib.error.URLError, ConnectionError) as exc:
        print(f"连不上服务：{exc}\n先启动：python -m app.main", file=sys.stderr)
        return 2

    print("[1] 健康检查与编码")
    check("健康检查 200", status == 200, f"got {status}")
    check("响应声明 charset=utf-8",
          "utf-8" in header(headers, "content-type").lower(),
          header(headers, "content-type") or "（没有 Content-Type 头）")
    check("有上新条目", int(health.get("newarrivals") or 0) > 0, str(health.get("newarrivals")))
    check("上新数据无读取失败", int(health.get("newarrival_problems") or 0) == 0,
          str(health.get("newarrival_problems")))

    print("\n[2] 知识库")
    _, kb, _ = get_json("/api/kb/status")
    check("索引已就绪", bool(kb.get("ready")), kb.get("error") or "未建索引")
    check("片段数大于 0", int(kb.get("chunks") or 0) > 0, str(kb.get("chunks")))

    status, hit, _ = get_json("/api/kb/ask?q=" + urllib.parse.quote("烫金和亮片可以机洗吗？"))
    check("命中问题的检索返回 200", status == 200, f"got {status}")
    check("命中至少一条", int(hit.get("count") or 0) > 0, str(hit.get("count")))
    top_k = int(health.get("top_k") or 0)
    check("返回条数不超过 top_k", int(hit.get("count") or 0) <= top_k,
          f"count={hit.get('count')} top_k={top_k}")
    check("答案带引用编号", "[1]" in str(hit.get("answer", "")), "答案里没有 [n]")
    check("引用编号不越界", citations_valid(hit), str(hit.get("answer", ""))[:80])

    _, miss, _ = get_json("/api/kb/ask?q=" + urllib.parse.quote("红烧肉怎么做才不腻"))
    check("知识库外问题按设计拒答（count=0）",
          int(miss.get("count") or 0) == 0, f"count={miss.get('count')}")

    username = "smoke-" + secrets.token_hex(4)
    password = "smoke-" + secrets.token_hex(8)
    token = ""
    media_name = ""
    # 有"无归属店家"待认领时**绝不能注册临时账号**：注册会认领它们，注销会把它们一起删掉。
    # 这个保护是必须的——冒烟脚本本身不该有破坏真实数据的能力。
    orphans = int(health.get("orphan_shops") or 0)
    try:
        print("\n[3] 账号与登录")
        print(f"  （临时账号 {username}，跑完会注销）")
        _, anonymous, _ = get_json("/api/auth/me")
        check("未登录时 me 返回 user=null", anonymous.get("user") is None, str(anonymous))

        if orphans:
            print(f"  [skip] 本机有 {orphans} 条升级前的旧店家待认领，跳过账号相关检查。")
            print("         请先在浏览器里注册/登录一次（让真实账号接手），再跑冒烟。")
        else:
            status_reg, registered = post_json("/api/auth/register",
                                               {"username": username, "password": password})
            check("注册 201", status_reg == 201, f"got {status_reg}")
            token = registered.get("token", "")
            check("注册返回令牌", bool(token), "没有 token")

            _, me, _ = get_json("/api/auth/me", token=token)
            check("me 返回注册的账号", (me.get("user") or {}).get("username") == username, str(me))

            status_dup, _ = post_json("/api/auth/register",
                                      {"username": username, "password": password})
            check("重复注册 409", status_dup == 409, f"got {status_dup}")

            status_bad, _ = post_json("/api/auth/login",
                                      {"username": username, "password": password + "x"})
            check("密码错 401", status_bad == 401, f"got {status_bad}")

            status_relogin, relogin = post_json("/api/auth/login",
                                                {"username": username, "password": password})
            check("重新登录 200 且换发令牌",
                  status_relogin == 200 and relogin.get("token") != token,
                  f"got {status_relogin}")

        print("\n[4] 上新与偏好")
        _, feed, _ = get_json("/api/feed?limit=200")
        check("匿名也能看推荐", int(feed.get("count") or 0) > 0, str(feed.get("count")))
        check("匿名时没有个性化加分",
              all(not item.get("reasons") for item in feed.get("items") or []),
              "匿名请求带上了偏好加权")
        # 示例数据来自内容本身：有 sample 条目时必须标出，没有时不能谎报。
        # 写死 "必须为 True" 会在真实条目替换掉占位数据后变成一条永远红的旧断言。
        sample_count = int((feed.get("summary") or {}).get("sample_count") or 0)
        check("示例数据标记与内容一致",
              feed.get("has_sample_data") is bool(sample_count), f"sample_count={sample_count}")

        if token:
            status_anon_pref, _ = put_json("/api/preferences", {"styles": [PROBE_STYLE]})
            check("未登录保存偏好 401", status_anon_pref == 401, f"got {status_anon_pref}")

            _, options, _ = get_json("/api/preferences/options")
            axes = [axis["key"] for axis in options.get("axes") or []]
            check("偏好轴元数据完整",
                  axes == ["shops", "styles", "keywords", "categories"], str(axes))
            custom = {axis["key"]: axis["allow_custom"] for axis in options.get("axes") or []}
            check("只有自定义标签允许自由输入",
                  custom.get("keywords") is True and
                  not any(custom.get(key) for key in ("shops", "styles", "categories")),
                  str(custom))
            category = next(
                (row["value"] for row in options.get("options", {}).get("categories") or []),
                "",
            )
            check("款式选项来自内容", bool(category), "content 里没有款式")

            status_pref, _ = put_json("/api/preferences", {
                "shops": [PROBE_BRAND], "styles": [PROBE_STYLE], "categories": [category],
            }, token=token)
            check("保存三个轴的偏好 200", status_pref == 200, f"got {status_pref}")

            status_bad_style, _ = put_json("/api/preferences", {"styles": ["甜美"]}, token=token)
            check("不支持的风格 400", status_bad_style == 400, f"got {status_bad_style}")
            status_bad_shop, _ = put_json("/api/preferences", {"shops": ["根本没这牌子"]}, token=token)
            check("不存在的店家 400", status_bad_shop == 400, f"got {status_bad_shop}")

            # 部分更新：只传 shops 时 styles 必须保留
            put_json("/api/preferences", {"shops": [PROBE_BRAND]}, token=token)
            _, after_partial, _ = get_json("/api/preferences", token=token)
            check("部分更新不清空其他轴",
                  after_partial["preferences"].get("styles") == [PROBE_STYLE],
                  str(after_partial["preferences"]))

            # 自定义标签（唯一的自由输入轴）。
            # 关键词从当前内容里取，不写死某个系列名——系列改名/归档后旧断言必然误报。
            probe_keyword = str((items_source(feed) or {}).get("series") or "")
            probe_keyword = probe_keyword.replace(" ", "")[:6] or PROBE_BRAND
            status_kw, kw_saved = put_json(
                "/api/preferences", {"keywords": [probe_keyword, PROBE_MISS]}, token=token
            )
            check("保存自定义标签 200", status_kw == 200, f"got {status_kw}")
            report = (kw_saved.get("match_report") or {}).get("keywords") or {}
            check("命中的自定义标签被识别",
                  probe_keyword in (report.get("matched") or []), str(report))
            check("未命中的自定义标签被标出",
                  PROBE_MISS in (report.get("unmatched") or []), str(report))

            status_long, _ = put_json("/api/preferences", {"keywords": ["字" * 21]}, token=token)
            check("超长自定义标签 400", status_long == 400, f"got {status_long}")

            _, personalized, _ = get_json("/api/feed?limit=200", token=token)
            items = personalized.get("items") or []
            top = items[0] if items else {}
            check("偏好店家排到第一", top.get("brand") == PROBE_BRAND, str(top.get("brand")))
            check("排序原因可解释", "你喜欢的店" in (top.get("reasons") or []),
                  str(top.get("reasons")))
            reasons = [reason for item in items for reason in (item.get("reasons") or [])]
            check("风格匹配计入", any(reason.startswith("风格：") for reason in reasons), str(reasons))
            check("款式匹配计入", any(reason.startswith("款式：") for reason in reasons), str(reasons))
            check("自定义标签匹配计入",
                  any(reason.startswith("关键词：") for reason in reasons), str(reasons))

            _, only, _ = get_json("/api/feed?only_preferred=true&limit=200", token=token)
            check("只看偏好店家时全部命中", bool(only.get("items")) and
                  all(item["preferred_shop"] for item in only["items"]),
                  f"count={len(only.get('items') or [])}")

            status_old, _, _ = _send("/api/follows", method="GET")
            check("旧「关注」接口已移除", status_old in (404, 405), f"got {status_old}")

        print("\n[5] 确定性工具（尺码 / 洗护）")
        _, size_brands, _ = get_json("/api/size/brands")
        check("拿到品牌尺码表", len(size_brands.get("brands") or []) > 0, "")
        # AP/Baby 是 item_level：官方不给品牌级 S/M/L，硬算一个码就是编。
        # 所以这里断言"不返回伪造码数"，再用具体商品验证区间型合身判断。
        status_brand, _ = post_json("/api/size/recommend", {
            "brand": "baby", "category": "JSK", "bust": 86, "waist": 68,
        })
        check("item_level 品牌不编品牌级码数（404）", status_brand == 404, f"got {status_brand}")

        probe_entry = items_source(feed) or {}
        status_fit, fit_body = post_json("/api/feed/sizes", {
            "ids": [probe_entry.get("id")], "bust": 86, "waist": 68, "hip": 90, "shoulder": 38,
        })
        fit = (fit_body.get("sizes") or {}).get(str(probe_entry.get("id")), {})
        check("商品级区间尺码可判断合身",
              status_fit == 200 and fit.get("status") == "ok", f"{status_fit} {fit}")

        status_care, care_body = post_json("/api/care/advice", {
            "materials": ["蕾丝"], "crafts": ["烫金"],
        })
        check("洗护建议 200", status_care == 200, f"got {status_care}")
        check("烫金一票否决机洗", care_body.get("machine_washable") is False,
              str(care_body.get("machine_washable")))

        print("\n[6] 图片上传（上新封面用）")
        status_png, uploaded = post_png("/api/media")
        check("上传图片 201", status_png == 201, f"got {status_png}")
        if status_png == 201:
            media_name = str(uploaded.get("url", "")).rsplit("/", 1)[-1]
            status_img, _, _ = _send("/media/" + media_name)
            check("上传后能取回图片", status_img == 200, f"got {status_img}")

        print("\n[7] PWA 资源")
        for path, needle in (("/", b"<title>"), ("/app.js", b""), ("/style.css", b""),
                             ("/manifest.webmanifest", b"standalone"), ("/sw.js", b"CACHE"),
                             ("/icons/icon-192.png", b"\x89PNG")):
            status_asset, body, _ = _send(path)
            check(f"{path} 可访问", status_asset == 200 and needle in body, f"{status_asset}")

        print("\n[8] 备份导出")
        status_anon_zip, _, _ = _send("/api/export")
        check("匿名导出 401", status_anon_zip == 401, f"got {status_anon_zip}")
        if token:
            status_zip, payload, _ = _send("/api/export", token=token)
            check("登录后导出 200", status_zip == 200, f"got {status_zip}")
            check("导出是 zip", payload[:2] == b"PK", payload[:8].hex())
            check("备份包含数据库", b"app.db" in payload, "zip 里没有 app.db")
            check("备份包含清单", b"MANIFEST.json" in payload, "zip 里没有 MANIFEST.json")
            check("备份包含上新内容", b"content/newarrivals/" in payload, "")
            check("备份包含知识库", b"content/kb/" in payload, "")
        else:
            print("  [skip] 没有临时账号令牌，跳过登录导出检查。")

    finally:
        print("\n[9] 清理")
        if token:
            status_del, _, _ = _send("/api/auth/me", method="DELETE", token=token)
            check("注销测试账号 200", status_del == 200, f"got {status_del}")
            _, after, _ = get_json("/api/auth/me", token=token)
            check("注销后令牌失效", after.get("user") is None, str(after))
        if media_name:
            (ROOT / "data" / "media" / media_name).unlink(missing_ok=True)

    print()
    if failures:
        print(f"失败 {len(failures)}/{checks}：")
        for name in failures:
            print(f"  - {name}")
        return 1
    print(f"全部通过（{checks} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
