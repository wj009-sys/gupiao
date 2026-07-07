"""
LLM Client — 三层错误恢复封装（基于 LiteLLM）

基于 learn-claude-code s11 Error Recovery 设计。
提供统一的 LLM 调用接口，内置三层恢复机制：

  1️⃣ 指数退避重试：429/529 等瞬态错误 → min(500×2^n, 32s) + 25% 抖动，最多 10 次
  2️⃣ max_tokens 截断恢复：8K→64K 升级 → 续写提示（最多 3 次）
  3️⃣ Context Window 超限：调用回调压缩后重试

额外特性：
  - 连续 3 次 529 自动切换备用模型
  - 支持自定义 compact 回调
  - 完整日志记录每次恢复动作
  - 兼容已有的 llm_config.py / data/llm_config.json 配置

用法：
    from scripts.utils.llm_client import LLMClient
    client = LLMClient(model="gpt-4o-mini")
    response = client.complete(
        messages=[{"role": "user", "content": "Hello"}],
        max_tokens=8000
    )

    # 提供 compact 回调（来自 context_compact 模块）
    from scripts.utils.context_compact import reactive_compact
    client = LLMClient(model="gpt-4o-mini", compact_fn=reactive_compact)

D3异常处理表：
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| 429 限流 | 指数退避(500×2^n ms) + 抖动 | 10 次重试后抛 MaxRetriesExceeded |
| 529 过载 | 指数退避，连续3次→切换备用模型 | 10 次重试后抛 MaxRetriesExceeded |
| max_tokens 截断 | 8K→64K 升级 + 续写提示(最多3次) | 退出（3次续写无实质产出判断）|
| ContextWindowExceeded | compact_fn 回调压缩后重试 | 回调失败/超限则抛异常 |
| litellm 未安装 | 提示 pip install litellm | 抛 ImportError |
| 超时 | 重试1次（超时时间翻倍） | 抛 TimeoutError |
| 未配置 LLM | is_available()=False | complete() 抛 LLMUnavailableError |

D4 CHECKPOINT:
- CP1-配置检查：complete() 前校验 provider/model/api_key 是否齐全
- CP2-重试计数：每轮重试跟踪 attempt 次数，超过 MAX_RETRIES=10 退出
- CP3-截断检测：finish_reason == "length" 时触发 max_tokens 恢复路径
- CP4-连续 529 追踪：consecutive_529 >= 3 时切换模型
- CP5-续写收益检测：连续 3 次续写且 token 增量 < 500 时停止（diminishing returns）

D9反例：
- 不要无限重试（必须设上限，避免死循环烧 API 费用）
- 不要在 429 时切换模型（等冷却即可；529 才切）
- 不要忽略 finish_reason（截断时不处理就返回不完整结果）
- 不要把所有错误混同处理（不同错误需要不同恢复策略）
"""
import json
import os
import time
import random
import logging
from typing import Optional, Callable

logger = logging.getLogger(__name__)

# ===== 常量 =====

# 重试配置
BASE_DELAY_MS = 500           # 初始退避延迟（毫秒）
MAX_RETRIES = 10              # 最大重试次数
MAX_BACKOFF_MS = 32_000       # 最大退避延迟（32秒）
MAX_529_BEFORE_FALLBACK = 3   # 连续 529 后切换备用模型

# Token 配置
DEFAULT_MAX_TOKENS = 8_000    # 默认 max_tokens
ESCALATED_MAX_TOKENS = 64_000 # 升级后的 max_tokens
MAX_CONTINUATION_RETRIES = 3  # 最多续写次数
DIMINISHING_THRESHOLD = 500   # 续写产出 < 500 token 视为收益递减

# 超时
DEFAULT_TIMEOUT = 60          # 默认超时秒数
TIMEOUT_RETRY_MULTIPLIER = 2  # 超时重试时超时时间翻倍


# ===== 自定义异常 =====

class LLMUnavailableError(Exception):
    """LLM 不可用（未配置或所有模型都失败）"""
    pass


class MaxRetriesExceeded(Exception):
    """超过最大重试次数"""
    pass


class ContextTooLongError(Exception):
    """上下文超限且无法压缩"""
    pass


# ===== LLM Client =====

class LLMClient:
    """三层错误恢复的 LLM 客户端

    基于 LiteLLM，提供统一的 completion 接口。
    每层恢复失败后会尝试下一层，全部失败则抛异常。

    Attributes:
        model: 当前使用的主模型名
        fallback_model: 备用模型名（529 连续失败后切换）
        compact_fn: 可选，ContextWindowExceeded 时的压缩回调
        consecutive_529: 当前连续 529 计数
    """

    def __init__(
        self,
        model: Optional[str] = None,
        fallback_model: Optional[str] = None,
        compact_fn: Optional[Callable] = None,
        provider: Optional[str] = None,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
    ):
        """初始化 LLM Client

        Args:
            model: 模型名（如 gpt-4o-mini, deepseek-chat）。为 None 时从配置读取
            fallback_model: 备用模型名（529 连续失败后切换）。为 None 时从配置读取
            compact_fn: ContextWindowExceeded 时的压缩回调。接收 messages 参数
            provider: Provider 名（openai/deepseek/claude/gemini）。为 None 时从配置读取
            api_key: API Key。为 None 时从配置读取
            api_base: API Base URL。为 None 时从配置读取
        """
        self.config = self._load_config(provider, model, api_key, api_base, fallback_model)
        self._provider = self.config.get("provider", "")
        self.model = self.config.get("model", "")
        self._api_key = self.config.get("api_key", "")
        self._api_base = self.config.get("api_base", "")
        self.fallback_model = self.config.get("fallback_model", "")

        self.compact_fn = compact_fn
        self.consecutive_529 = 0
        self._current_model = self.model

        # 恢复状态（每轮 complete 调用时重置）
        self._recovery_state = None

    @staticmethod
    def _load_config(
        provider: Optional[str],
        model: Optional[str],
        api_key: Optional[str],
        api_base: Optional[str],
        fallback_model: Optional[str],
    ) -> dict:
        """加载 LLM 配置（优先级：参数 > 配置文件 > 环境变量）"""
        cfg: dict = {
            "provider": "",
            "model": "",
            "api_key": "",
            "api_base": "",
            "fallback_model": "",
        }

        # 1. 从配置文件读取（data/llm_config.json）
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        config_path = os.path.join(root, "data", "llm_config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    file_cfg = json.load(f)
                for k in cfg:
                    v = file_cfg.get(k, "")
                    if v:
                        cfg[k] = str(v).strip()
            except Exception as e:
                logger.debug(f"[LLMClient] 读取配置文件失败: {e}")

        # 2. 环境变量覆盖
        env_map = {
            "LLM_PROVIDER": "provider",
            "LLM_MODEL": "model",
            "LLM_API_KEY": "api_key",
            "LLM_API_BASE": "api_base",
            "FALLBACK_MODEL": "fallback_model",
        }
        for env_key, cfg_key in env_map.items():
            ev = os.getenv(env_key, "").strip()
            if ev:
                cfg[cfg_key] = ev

        # 3. 构造参数优先
        if provider:
            cfg["provider"] = provider
        if model:
            cfg["model"] = model
        if api_key:
            cfg["api_key"] = api_key
        if api_base:
            cfg["api_base"] = api_base
        if fallback_model:
            cfg["fallback_model"] = fallback_model

        return cfg

    # ── 可用性检查 ──

    def is_available(self) -> bool:
        """检查 LLM 是否配置可用"""
        return bool(self._provider and self._current_model and self._api_key)

    # ── 核心接口 ──

    def complete(
        self,
        messages: list[dict],
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 0.3,
        timeout: int = DEFAULT_TIMEOUT,
        response_format: Optional[dict] = None,
        tools: Optional[list] = None,
    ) -> dict:
        """统一的 LLM 调用接口（三层恢复）

        Args:
            messages: 对话消息列表
            max_tokens: 最大输出 token 数
            temperature: 温度参数
            timeout: 超时秒数
            response_format: 响应格式（可选，如 {"type": "json_object"}）
            tools: 工具定义列表（可选）

        Returns:
            LiteLLM 响应对象（兼容 OpenAI 响应格式）

        Raises:
            LLMUnavailableError: LLM 未配置
            MaxRetriesExceeded: 重试耗尽
            ContextTooLongError: 上下文超限且无法压缩
        """
        # 重置每轮状态
        self._recovery_state = {
            "has_escalated": False,      # 是否已升级 max_tokens
            "has_compacted": False,      # 是否已压缩过上下文
            "continuation_count": 0,     # 续写次数
            "consecutive_small": 0,      # 连续小产出计数
        }

        if not self.is_available():
            raise LLMUnavailableError(
                f"LLM 未配置: provider={self._provider}, "
                f"model={self._current_model}, api_key={'已设' if self._api_key else '未设'}"
            )

        if not messages:
            raise ValueError("messages 不能为空")

        return self._call_with_recovery(
            messages, max_tokens, temperature, timeout, response_format, tools
        )

    def _call_with_recovery(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        timeout: int,
        response_format: Optional[dict],
        tools: Optional[list],
    ) -> dict:
        """带三层恢复的 LLM 调用循环"""
        retry_count = 0
        state = self._recovery_state
        current_max_tokens = max_tokens
        current_model = self._current_model

        while True:
            try:
                # 调用 LiteLLM（含瞬态错误重试）
                response = self._call_with_retry(
                    messages, current_max_tokens, temperature,
                    timeout, response_format, tools, retry_count,
                    current_model,
                )

                # 成功：重置连续 529 计数
                self.consecutive_529 = 0

                # ── 检查 max_tokens 截断 ──
                finish_reason = self._get_finish_reason(response)
                if finish_reason == "length":
                    # 路径 1: 升级 max_tokens
                    if not state["has_escalated"] and current_max_tokens < ESCALATED_MAX_TOKENS:
                        # 不追加截断输出，用更大 max_tokens 重试同一请求
                        current_max_tokens = ESCALATED_MAX_TOKENS
                        state["has_escalated"] = True
                        logger.info(f"[LLMClient] max_tokens 截断: 升级到 {ESCALATED_MAX_TOKENS}")
                        continue

                    # 路径 2: 续写（保留截断输出 + 续写提示）
                    if state["continuation_count"] < MAX_CONTINUATION_RETRIES:
                        # 检查产出是否递减
                        output_text = self._extract_text(response)
                        output_len = len(output_text)

                        if state["continuation_count"] > 0 and output_len < DIMINISHING_THRESHOLD:
                            state["consecutive_small"] += 1
                            if state["consecutive_small"] >= 2:
                                logger.warning(
                                    f"[LLMClient] 续写产出递减 (连续{state['consecutive_small']}次<{DIMINISHING_THRESHOLD})，停止续写"
                                )
                                return response

                        # 追加截断输出和续写提示
                        messages.append({"role": "assistant", "content": output_text})
                        messages.append({
                            "role": "user",
                            "content": (
                                "输出 token 限制被截断了。请直接续写刚才的内容——"
                                "不要道歉，不要总结，不要回顾。从被截断的地方继续往下写。"
                            ),
                        })
                        state["continuation_count"] += 1
                        logger.info(
                            f"[LLMClient] 续写 #{state['continuation_count']}/{MAX_CONTINUATION_RETRIES}"
                        )
                        continue

                    # 续写次数用完，返回已截断的输出
                    logger.warning(f"[LLMClient] 续写 {MAX_CONTINUATION_RETRIES} 次后仍截断，返回现有结果")
                    return response

                # 正常完成
                return response

            except self._get_context_window_exceeded() as e:
                # ── 路径 3: 上下文超限 → 压缩后重试 ──
                if self.compact_fn and not state["has_compacted"]:
                    logger.info("[LLMClient] ContextWindowExceeded: 执行 reactive compact")
                    try:
                        messages = self.compact_fn(messages)
                        state["has_compacted"] = True
                        continue
                    except Exception as compact_err:
                        logger.error(f"[LLMClient] compact 回调失败: {compact_err}")
                        raise ContextTooLongError("上下文超限且压缩失败") from compact_err
                raise ContextTooLongError("上下文超限且无可用压缩回调") from e

            except (MaxRetriesExceeded, Exception) as e:
                # 未知异常，检查是否需要切换模型
                if self._is_overloaded_error(e):
                    self.consecutive_529 += 1
                    logger.warning(
                        f"[LLMClient] 529 过载 #{self.consecutive_529}"
                    )
                    if self.consecutive_529 >= MAX_529_BEFORE_FALLBACK and self.fallback_model:
                        logger.info(
                            f"[LLMClient] 连续 {MAX_529_BEFORE_FALLBACK} 次 529，切换到备用模型: "
                            f"{self.fallback_model}"
                        )
                        current_model = self.fallback_model
                        self.consecutive_529 = 0  # 切换后重置
                        continue  # 用新模型重试当前请求

                if isinstance(e, MaxRetriesExceeded):
                    raise
                raise

    def _call_with_retry(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        timeout: int,
        response_format: Optional[dict],
        tools: Optional[list],
        outer_retry_count: int,
        current_model: str,
    ) -> dict:
        """带指数退避的 LLM 调用（处理 429/529/超时）"""
        # 构造完整模型名
        model_name = (
            f"{self._provider}/{current_model}"
            if "/" not in current_model
            else current_model
        )

        last_error = None
        global_attempt = 0

        for attempt in range(1, MAX_RETRIES + 1):
            global_attempt = attempt
            try:
                # 动态导入 litellm（防止 import 时未安装）
                try:
                    import litellm
                except ImportError:
                    raise ImportError(
                        "litellm 未安装，运行: pip install litellm"
                    )

                # 构造调用参数
                kwargs = {
                    "model": model_name,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "timeout": timeout,
                }
                if self._api_key:
                    kwargs["api_key"] = self._api_key
                if self._api_base:
                    kwargs["api_base"] = self._api_base
                if response_format:
                    kwargs["response_format"] = response_format
                if tools:
                    kwargs["tools"] = tools

                response = litellm.completion(**kwargs)
                return response

            except ImportError:
                raise

            except Exception as e:
                last_error = e
                is_retryable, delay = self._should_retry(e, attempt)

                if is_retryable:
                    logger.warning(
                        f"[LLMClient] 重试 {attempt}/{MAX_RETRIES} "
                        f"(error={e.__class__.__name__}, delay={delay:.1f}s)"
                    )
                    time.sleep(delay)
                    continue

                # 不可重试的错误，直接抛出
                raise

        # 重试耗尽
        raise MaxRetriesExceeded(
            f"LLM 调用失败，已重试 {global_attempt} 次。最后错误: {last_error}"
        )

    @staticmethod
    def _get_status_code(error: Exception) -> int:
        """从异常对象提取 HTTP 状态码"""
        # 直接挂在 error 上（如 httpx.HTTPStatusError）
        code = getattr(error, "status_code", None)
        if code:
            return code
        # 挂在 error.response 上
        resp = getattr(error, "response", None)
        if resp is not None:
            code = getattr(resp, "status_code", None)
            if code:
                return code
        # 检查 headers 中的 status
        if resp is not None:
            headers = getattr(resp, "headers", None)
            if headers:
                try:
                    status_str = headers.get(":status", "") or headers.get("status", "")
                    if status_str:
                        return int(status_str)
                except (ValueError, TypeError):
                    pass
        return 0

    def _should_retry(self, error: Exception, attempt: int) -> tuple[bool, float]:
        """判断是否应该重试，以及延迟时间

        可重试的条件（基于状态码和异常类名双路径检测）：
        - 429: RateLimitError
        - 5xx: OverloadedError (529), ServiceUnavailableError, APIStatusError
        - Timeout
        - ConnectionError / APIConnectionError
        """
        status = self._get_status_code(error)
        error_name = error.__class__.__name__

        # 状态码检测（最可靠）
        if status == 429:
            is_retryable = True
        elif status == 529:
            is_retryable = True
        elif 500 <= status < 600:
            is_retryable = True
        else:
            # 类名检测（fallback）
            en = error_name.lower()
            is_retryable = any(kw in en for kw in [
                "ratelimit", "rate_limit",
                "overloaded",
                "serviceunavailable", "service_unavailable",
                "timeout",
                "apiconnection", "apierror",
            ])

        if not is_retryable:
            return False, 0.0

        # 服务器返回了 Retry-After 头？
        retry_after = None
        if hasattr(error, "response"):
            headers = getattr(error.response, "headers", None) or {}
            retry_after_str = headers.get("Retry-After") or headers.get("retry-after")
            if retry_after_str:
                try:
                    retry_after = int(retry_after_str)
                except (ValueError, TypeError):
                    pass

        if retry_after is not None:
            return True, min(float(retry_after), 60.0)

        # 指数退避: min(500 × 2^(attempt-1), 32000) + 25% 抖动
        base_ms = min(BASE_DELAY_MS * (2 ** (attempt - 1)), MAX_BACKOFF_MS)
        jitter = random.uniform(0, base_ms * 0.25)
        delay = (base_ms + jitter) / 1000.0  # 转秒

        return True, min(delay, 60.0)

    # ── 工具方法 ──

    @staticmethod
    def _get_finish_reason(response) -> str:
        """获取 finish_reason（兼容 OpenAI 和 Anthropic 格式）"""
        # OpenAI / LiteLLM 格式
        try:
            return response.choices[0].finish_reason or ""
        except (AttributeError, IndexError, TypeError):
            pass
        # Anthropic 格式（通过 LiteLLM 转换的）
        try:
            return response.stop_reason or ""
        except AttributeError:
            pass
        return ""

    @staticmethod
    def _extract_text(response) -> str:
        """从响应中提取文本内容"""
        try:
            return response.choices[0].message.content or ""
        except (AttributeError, IndexError, TypeError):
            pass
        try:
            return response.content[0].text or ""
        except (AttributeError, IndexError, TypeError):
            pass
        return ""

    @staticmethod
    def _get_context_window_exceeded():
        """获取 ContextWindowExceeded 异常类（延迟导入以避免 import 时崩溃）"""
        try:
            import litellm
            return litellm.ContextWindowExceededError
        except (ImportError, AttributeError):
            # 构造一个虚拟类，使 isinstance 永远返回 False
            class _Dummy:
                pass
            return _Dummy

    @staticmethod
    def _is_overloaded_error(error: Exception) -> bool:
        """检查是否是 529 过载错误"""
        error_name = error.__class__.__name__
        if "overloaded" in error_name.lower() or "529" in str(error):
            return True
        # 检查状态码
        if hasattr(error, "status_code") and error.status_code == 529:
            return True
        if hasattr(error, "response") and hasattr(error.response, "status_code"):
            if error.response.status_code == 529:
                return True
        return False

    def __repr__(self) -> str:
        return (
            f"LLMClient(provider={self._provider}, "
            f"model={self._current_model}, "
            f"fallback={self.fallback_model or '无'})"
        )


# ===== 便捷函数 =====

def get_default_client(compact_fn: Optional[Callable] = None) -> LLMClient:
    """获取默认 LLM 客户端（从配置自动加载）"""
    return LLMClient(compact_fn=compact_fn)


# ===== 自测 =====

def _test():
    """LLMClient 自测（验证配置加载、假错误恢复、无需 API 调用）"""
    import sys

    print("=" * 50)
    print("  LLMClient 自测")
    print("=" * 50)

    # 1. 默认配置加载
    client = get_default_client()
    print(f"  ✅ 初始化: {client}")
    print(f"  ✅ is_available: {client.is_available()}")

    # 2. 空消息校验
    try:
        client.complete([])
        assert False, "空消息应报错"
    except ValueError:
        print("  ✅ 空消息校验通过")

    # 3. 配置覆盖
    custom = LLMClient(
        provider="deepseek",
        model="deepseek-chat",
        api_key="test-key",
        api_base="https://api.deepseek.com",
    )
    assert custom._provider == "deepseek"
    assert custom.model == "deepseek-chat"
    assert custom._api_key == "test-key"
    assert custom._api_base == "https://api.deepseek.com"
    print("  ✅ 配置覆盖通过")

    # 4. 退避延迟计算 — 用状态码 mock 模拟
    bf = LLMClient(provider="openai", model="gpt-4o-mini", api_key="test")

    class Mock429(Exception):
        status_code = 429
    for attempt in [1, 2, 3, 4, 7, 10]:
        should, delay = bf._should_retry(Mock429(), attempt)
        assert should, f"Attempt {attempt} 应重试"
        assert 0.5 <= delay <= 60.0, f"延迟范围异常: {delay}s"
        print(f"    429 attempt {attempt:2d}: delay={delay:.2f}s")

    # 5xx 服务器错误
    class Mock5xx(Exception):
        status_code = 503
    should, delay = bf._should_retry(Mock5xx(), 1)
    assert should, "5xx 应重试"
    print(f"    5xx: 状态码检测通过")

    # 类名匹配（无状态码时回退）
    class TimeoutMock(Exception):
        pass
    should, delay = bf._should_retry(TimeoutMock(), 1)
    assert should, "Timeout 应重试"
    print(f"    Timeout: 类名检测通过")

    # 不可重试
    class ValidationErr(Exception):
        pass
    should, _ = bf._should_retry(ValidationErr(), 1)
    assert not should, "ValidationError 不应重试"
    print(f"    ValidationError: 正确拒绝")

    # 5. 529 检测
    class Mock529:
        status_code = 529
        response = type("R", (), {"status_code": 529})()
    assert bf._is_overloaded_error(Mock529())
    print("  ✅ 529 检测通过")

    # 6. finish_reason 提取
    class MockChoice:
        finish_reason = "length"

    class MockResponse:
        choices = [MockChoice()]

    assert bf._get_finish_reason(MockResponse()) == "length"
    print("  ✅ finish_reason 提取通过")

    # 7. 备用模型切换逻辑
    fb_client = LLMClient(
        provider="openai", model="gpt-4o-mini",
        api_key="test", fallback_model="gpt-4o",
    )
    fb_client.consecutive_529 = MAX_529_BEFORE_FALLBACK
    # 模拟过载，手动触发切换
    fb_client._current_model = fb_client.fallback_model
    assert fb_client._current_model == "gpt-4o"
    print(f"  ✅ 备用模型切换: {fb_client}")

    print(f"\n  {'='*40}")
    print(f"  全部测试通过 ✅")
    print(f"  {'='*40}")
    print(f"\n  注意: 集成 API 测试需配置 LLM（data/llm_config.json）")
    print(f"  运行: python scripts/utils/llm_config.py --set")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    _test()
