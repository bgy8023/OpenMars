# =============================================
# OpenMars | 环境变量配置收口
# 除 LLM_EXTRACT_MODEL_NAME 由 cyber_printer_ultimate.py 直读外，全部环境变量统一在本文件读取，
# 业务代码一律通过 get_config() 获取，避免散落各处的 os.getenv（openmars_core 内不应再直读环境变量）
# 显式保留清单（13 项收口在 AppConfig，LLM_EXTRACT_MODEL_NAME 直读于 cyber_printer_ultimate.py，共 14 个，与 .env.example 对应）：
#   活变量 5 个：LLM_API_KEY / LLM_BASE_URL / LLM_MODEL_NAME / MAX_RETRY / WEBHOOK_URL
#   生成引擎 3 个：LLM_TEMPERATURE / LLM_MAX_TOKENS / LLM_TIMEOUT（已接入生成引擎）
#   质量门禁 3 个（已接入 validators，r2 语义）：
#                 MAX_CHAPTER_WORDS（platform_hint 建议边界 + target 未提供时的字数门禁兜底基准）
#                 TOMATO_AUDIT_COMPATIBLE（门禁总开关：开启才做「不达标定向重写」闭环）
#                 CONTENT_SAFETY_CHECK（一致性审校开关：关闭时零额外 LLM 调用）
#   成本估算 2 个：LLM_PRICE_IN_PER_1M / LLM_PRICE_OUT_PER_1M（每章运行报告估算成本，0=只记token）
#   直读 1 个：LLM_EXTRACT_MODEL_NAME（cyber_printer_ultimate.py 直读，写后记忆抽取便宜模型）
# =============================================
import os
from dataclasses import dataclass

from dotenv import load_dotenv

# 在读取环境变量前加载 .env
load_dotenv()


def _to_int(value: str, default: int) -> int:
    """环境变量整型解析，非法值回退默认值"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _to_float(value: str, default: float) -> float:
    """环境变量浮点解析，非法值回退默认值"""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _to_bool(value: str, default: bool) -> bool:
    """环境变量布尔解析，非法值回退默认值"""
    normalized = str(value).strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    return default


# 开箱默认指向智谱 GLM-4-Flash（永久免费），与 .env.example 模板、README 推荐三处统一；
# 面板等业务代码取默认值一律引用这两个常量，禁止各写各的（历史坑：面板默认 openai.com，配智谱 key 直接 401）
DEFAULT_LLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_LLM_MODEL_NAME = "glm-4-flash"


@dataclass
class AppConfig:
    """应用配置：13 项收口在本类，LLM_EXTRACT_MODEL_NAME 由 cyber_printer_ultimate.py 直读，
    共 14 个环境变量，与 .env.example 对应"""

    # ---- 大模型连接（活变量） ----
    llm_api_key: str = ""                              # 大模型 API Key
    llm_base_url: str = DEFAULT_LLM_BASE_URL  # 大模型接口地址（绝大多数服务商以 /v1 或 /v4 结尾）
    llm_model_name: str = DEFAULT_LLM_MODEL_NAME       # 模型名称
    max_retry: int = 3                                 # 生成最大重试次数
    webhook_url: str = ""                              # 移动端告警 Webhook（Bark/Server酱/飞书/钉钉）

    # ---- 生成参数（重新接线） ----
    llm_temperature: float = 0.7   # 模型温度系数（0-1，网文创作推荐0.7-0.8），传入 chat.completions.create
    llm_max_tokens: int = 0        # 单轮最大输出Token数，0 表示不传、交由服务商按模型上限决定
    llm_timeout: float = 0.0       # 模型调用超时时间（秒），0 表示使用 OpenAI SDK 默认超时

    # ---- 番茄审核门禁（已接入 validators，r2 语义） ----
    max_chapter_words: int = 4500          # 番茄建议区间上限：platform_hint 的建议边界；target 未提供时作字数门禁兜底基准
    tomato_audit_compatible: bool = True   # 番茄审核兼容模式总开关：开启时门禁不达标触发定向重写（≤2轮），关闭时门禁仅出报告不强制
    content_safety_check: bool = True      # 一致性审校开关：开启时每章生成后追加一次独立审校调用输出可疑点清单；关闭时零额外LLM调用

    # ---- 成本估算（每章运行报告用；0=未配置单价，只记 token 不算钱） ----
    llm_price_in_per_1m: float = 0.0    # 输入单价（元/百万Token）
    llm_price_out_per_1m: float = 0.0   # 输出单价（元/百万Token）

    @classmethod
    def from_env(cls) -> "AppConfig":
        """实时读取环境变量构造配置（不做进程级缓存，保证保存 .env 后立即生效）"""
        return cls(
            llm_api_key=os.getenv("LLM_API_KEY", ""),
            llm_base_url=os.getenv("LLM_BASE_URL", DEFAULT_LLM_BASE_URL),
            llm_model_name=os.getenv("LLM_MODEL_NAME", DEFAULT_LLM_MODEL_NAME),
            max_retry=_to_int(os.getenv("MAX_RETRY", "3"), 3),
            webhook_url=os.getenv("WEBHOOK_URL", ""),
            llm_temperature=_to_float(os.getenv("LLM_TEMPERATURE", "0.7"), 0.7),
            llm_max_tokens=_to_int(os.getenv("LLM_MAX_TOKENS", "0"), 0),
            llm_timeout=_to_float(os.getenv("LLM_TIMEOUT", "0"), 0.0),
            max_chapter_words=_to_int(os.getenv("MAX_CHAPTER_WORDS", "4500"), 4500),
            tomato_audit_compatible=_to_bool(os.getenv("TOMATO_AUDIT_COMPATIBLE", "true"), True),
            content_safety_check=_to_bool(os.getenv("CONTENT_SAFETY_CHECK", "true"), True),
            llm_price_in_per_1m=_to_float(os.getenv("LLM_PRICE_IN_PER_1M", "0"), 0.0),
            llm_price_out_per_1m=_to_float(os.getenv("LLM_PRICE_OUT_PER_1M", "0"), 0.0),
        )


def get_config() -> AppConfig:
    """获取当前应用配置。每次调用实时读取环境变量"""
    return AppConfig.from_env()
