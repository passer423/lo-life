"""应用配置。

与上游（lolita-rag）的分工：
* 本文件只管**应用层**：绑定地址、数据目录、知识库索引目录、LLM 凭据；
* 检索/生成的全部参数（阈值、闸门、融合方式）留在上游 `Settings` 里，
  由 bridge.py 显式覆盖少数几项（data_dir / 密钥），其余一个不动——
  那些数字是上游在真实数据上标定出来的，这里没有资格改。
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic_settings import BaseSettings, SettingsConfigDict

APP_ROOT = Path(__file__).resolve().parents[1]

# 上游仓库的位置。允许用 .env 覆盖，这样整个项目搬到别的机器/盘符也不用改代码。
DEFAULT_RAG_ROOT = Path(r"D:\Users\50436\Documents\Trae-CN\temp_20261012_01\lolita-rag")


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=APP_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # 这两项一旦进日志就等于泄露账号会话 / 账单，上游踩过（pytest 打印 fixture）。
    SECRET_FIELDS: ClassVar[tuple[str, ...]] = ("llm_api_key",)

    def __repr__(self) -> str:
        parts: list[str] = []
        for name in type(self).model_fields:
            value = getattr(self, name, None)
            if name in self.SECRET_FIELDS:
                length = len(str(value or "").strip())
                parts.append(f"{name}={'<未设置>' if not length else f'<已设置 {length} 字符>'}")
            else:
                parts.append(f"{name}={value!r}")
        return f"{type(self).__name__}({', '.join(parts)})"

    bind_host: str = "127.0.0.1"
    bind_port: int = 8787

    # 数据目录。做成**字段**而不是硬编码属性，有两个理由：
    # ① 想把衣柜照片放到另一块盘/网盘同步目录时，只改 .env 的 DATA_DIR 就行；
    # ② 测试必须能把数据指到临时目录——上游踩过「跑一次测试往真实查询日志灌几十条」，
    #    这里从根上做到「测试碰不到真实数据」。
    data_dir: Path = APP_ROOT / "data"

    # 内容目录（知识库 / 尺码表 / 洗护规则）。做成字段同样是为了测试隔离：
    # 硬编码成 APP_ROOT/content 时，测试会**读到真实知识库**，
    # 于是"重复 id 要报错"这类用例会因为根本没读到临时文件而假通过或假失败。
    content_dir: Path = APP_ROOT / "content"

    lolita_rag_root: Path = DEFAULT_RAG_ROOT

    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"

    # 检索只取**最相关的一条**。
    #
    # 为什么是 1：知识库的一条就是"一个主题写完整"的段落，5 条里通常只有 1 条真正回
    # 答问题，其余只是同话题的邻居，展示出来就是噪声——而且用户会以为系统"没看懂"。
    #
    # 两个必须说清的边界：
    #   1. 这里的 1 是**单个问句**的默认值；并列多意图问句（「烫金和蕾丝怎么洗」）
    #      由 app/bridge.py 拆成子问题各取一条再合并，不需要把 TOP_K 调大。
    #   2. **不能只在前端隐藏多余结果**。答案里的 `[n]` 与返回的条目一一对应，前端
    #      藏掉第 2 条之后，答案引用 `[2]` 就点不到任何东西，`verify` 的越界检查也会
    #      因为口径不一致而误判。所以收敛必须发生在检索层（就是这里）。
    top_k: int = 1

    # ---------- 派生路径 ----------
    #
    # 刻意分成两类目录：
    #   content/  —— **内容**，是产品真正的价值，进 git、可评审、可回溯
    #   data/     —— **运行时数据**，不进 git（用户衣柜、照片、索引）
    # 早先知识库放在 data/kb/，而 data/ 整个被 gitignore —— 等于产品最值钱的东西
    # 不在版本控制里，一次 git clean 就没了。这个划分就是为了堵住它。
    @property
    def kb_dir(self) -> Path:
        """知识库源文件（纯文本 .md，进 git）。"""
        return self.content_dir / "kb"

    @property
    def size_dir(self) -> Path:
        """品牌尺码表（结构化 JSON，进 git）。"""
        return self.content_dir / "size"

    @property
    def care_dir(self) -> Path:
        """洗护规则（结构化 JSON，进 git）。"""
        return self.content_dir / "care"

    @property
    def newarrivals_dir(self) -> Path:
        """上新条目（结构化 JSON，进 git）。你自己维护，不从任何平台抓取。"""
        return self.content_dir / "newarrivals"

    @property
    def shops_dir(self) -> Path:
        """店铺登记表（结构化 JSON，进 git）：brand → 店铺页/品牌官网地址。

        刻意**不放在 newarrivals 里**：同一家店会跨很多条上新，把它抄在每一条上是
        两处维护同一份事实——改一次名要改几十个文件，迟早就漏一个。
        """
        return self.content_dir / "shops"

    @property
    def media_dir(self) -> Path:
        """衣柜照片。**不进索引、不进 git**，只做备份对象。"""
        return self.data_dir / "media"

    @property
    def db_path(self) -> Path:
        """用户数据（衣柜/出勤/清单）。唯一不可再生的东西，备份的核心。"""
        return self.data_dir / "app.db"

    @property
    def kb_index_dir(self) -> Path:
        """知识库索引的家。**与用户数据分开**：删索引不会碰到衣柜。"""
        return self.data_dir / "kbindex"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key.strip())

    def ensure_dirs(self) -> None:
        """只建运行时目录。content/ 不自动创建——那样会把「路径写错了」变成空目录。"""
        for path in (self.data_dir, self.media_dir, self.kb_index_dir):
            path.mkdir(parents=True, exist_ok=True)


settings = AppSettings()
