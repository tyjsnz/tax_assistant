"""政策知识库检索（Markdown + YAML frontmatter + 中文 2-gram BM25）。

设计要点
--------
* **不依赖 pyyaml**：手写 frontmatter 解析，支持 ``key: value``、``key: [a, b]``、
  ``key:`` 后跟 ``- 列表项`` 三种写法；
* **中文检索**：把中文连续片段切成 2-gram（「小规模」→「小规」「规模」），英文 / 数字按词切分，
  再用 BM25（k1=1.5, b=0.75）打分，标题与标签加权；
* **无 API Key 可用**：:func:`answer` 无 Key 时输出「检索摘要 + 原文片段 + 来源」，有 Key 时
  基于检索上下文让 DeepSeek 作答并附 citations；
* 政策类回答必须带文号（source）、生效日期（effective_date）与检索出处。

政策文件目录：``config.POLICY_DIR``（默认 ``knowledge/policy_files/``），
每篇格式见 ``docs/CONTRACTS.md`` 5.3 与《报税Agent开发规范.md》5.2。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import config

#: 政策文件目录（对外常量；函数内部通过 :func:`policy_dir` 动态读取 config，便于测试隔离）
POLICY_DIR = config.POLICY_DIR

#: 已加载政策的缓存（含文件签名，文件变化自动失效）
_policy_cache: list[dict[str, Any]] = []
_policy_signature: tuple[tuple[str, float], ...] = ()

_REQUIRED_FRONTMATTER = (
    "title",
    "category",
    "effective_date",
    "expiry_date",
    "source",
    "last_verified",
    "tags",
)


# =============================================================================
# frontmatter 解析
# =============================================================================
def _unquote(value: str) -> str:
    text = (value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1].strip()
    return text


def _split_list(value: str) -> list[str]:
    """把 ``[a, b]`` / ``a、b`` / ``a, b`` 解析为字符串列表。"""
    text = (value or "").strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    parts = re.split(r"[,，、;；]", text)
    return [_unquote(part.strip()) for part in parts if part.strip()]


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """解析 Markdown 的 YAML frontmatter，返回 ``(元数据, 正文)``。

    只支持本项目用到的简单 YAML 子集；frontmatter 缺失或格式错误时返回 ``({}, 原文)``。
    """
    raw = (text or "").lstrip("\ufeff")
    lines = raw.splitlines()
    if not lines or lines[0].strip() not in ("---", "---\ufeff"):
        return {}, raw
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            end = index
            break
    if end is None:
        return {}, raw

    meta: dict[str, Any] = {}
    current_key: str | None = None
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1] in (" ", "\t") and current_key:
            item = stripped.lstrip("-").strip()
            if item:
                if isinstance(meta.get(current_key), list):
                    meta[current_key].append(_unquote(item))
                else:
                    meta[current_key] = _unquote(item)
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if not value:
            meta[key] = []
            current_key = key
            continue
        if value.startswith("[") and value.endswith("]"):
            meta[key] = _split_list(value)
        elif key in ("tags", "keywords", "别名", "aliases") and ("、" in value or "," in value):
            meta[key] = _split_list(value)
        else:
            meta[key] = _unquote(value)
        current_key = key
    body = "\n".join(lines[end + 1 :]).strip()
    return meta, body


# =============================================================================
# 加载
# =============================================================================
def policy_dir() -> Path:
    """政策文件根目录（动态读取 config，便于测试替换）。"""
    try:
        return Path(config.POLICY_DIR)
    except Exception:  # pragma: no cover
        return Path(POLICY_DIR)


def _build_summary(body: str, limit: int = 200) -> str:
    """从正文中提取摘要：第一段有意义的正文（跳过标题与空行）。"""
    for block in re.split(r"\n\s*\n", body or ""):
        text = block.strip()
        if not text or text.startswith("#") or text.startswith("|") or text.startswith(">"):
            continue
        cleaned = re.sub(r"[#*`>]", "", text).strip()
        if cleaned:
            return cleaned[:limit]
    cleaned = re.sub(r"[#*`>]", "", body or "").strip()
    return cleaned[:limit]


def _to_id(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = Path(path.name)
    return relative.with_suffix("").as_posix()


def _build_policy(path: Path, root: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    meta, body = parse_frontmatter(text)
    if not body:
        return None
    tags = meta.get("tags") or []
    if isinstance(tags, str):
        tags = _split_list(tags)
    policy_id = _to_id(path, root)
    category = str(meta.get("category") or (policy_id.split("/")[0] if "/" in policy_id else "未分类")).strip()
    title = str(meta.get("title") or path.stem).strip()
    return {
        "id": policy_id,
        "title": title,
        "category": category,
        "effective_date": str(meta.get("effective_date") or "").strip(),
        "expiry_date": str(meta.get("expiry_date") or "").strip(),
        "source": str(meta.get("source") or "").strip(),
        "last_verified": str(meta.get("last_verified") or "").strip(),
        "tags": [str(tag).strip() for tag in tags if str(tag).strip()],
        "path": str(path),
        "content": body,
        "summary": _build_summary(body),
        "meta": meta,
    }


def _signature(root: Path) -> tuple[tuple[str, float], ...]:
    if not root.exists():
        return ()
    items: list[tuple[str, float]] = []
    try:
        for path in sorted(root.rglob("*.md")):
            try:
                items.append((str(path), path.stat().st_mtime))
            except OSError:
                continue
    except OSError:  # pragma: no cover
        return ()
    return tuple(items)


def load_policies(force: bool = False) -> list[dict[str, Any]]:
    """加载全部政策文档（带缓存；文件变化自动重新解析）。

    返回列表，每项包含：
    ``{id, title, category, effective_date, expiry_date, source, last_verified, tags, path, content, summary, meta}``。
    """
    global _policy_cache, _policy_signature
    root = policy_dir()
    signature = _signature(root)
    if not force and _policy_cache and signature == _policy_signature:
        return [dict(policy) for policy in _policy_cache]
    policies: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.md")) if root.exists() else []:
        policy = _build_policy(path, root)
        if policy:
            policies.append(policy)
    # 分类内按标题排序，稳定输出
    policies.sort(key=lambda item: (item["category"], item["title"]))
    _policy_cache = policies
    _policy_signature = signature
    return [dict(policy) for policy in policies]


def list_policies(category: str | None = None) -> list[dict[str, Any]]:
    """列出政策（可按分类过滤，分类支持包含匹配）。"""
    policies = load_policies()
    if not category:
        return policies
    text = str(category).strip()
    return [
        policy
        for policy in policies
        if text == policy["category"] or text in policy["category"] or policy["category"] in text
    ]


def get_policy(policy_id: str) -> dict[str, Any] | None:
    """按 id / 文件名 / 标题获取单篇政策；不存在返回 None。"""
    text = str(policy_id or "").strip()
    if not text:
        return None
    policies = load_policies()
    for policy in policies:
        if text in (policy["id"], policy["title"], Path(policy["path"]).stem, policy["path"]):
            return policy
    for policy in policies:
        if text in policy["id"] or text in policy["title"]:
            return policy
    return None


# =============================================================================
# 检索（中文 2-gram + BM25）
# =============================================================================
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fa5]+")
_WORD_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_%.\-]*")
#: 汉语虚词：由它们构成的 2-gram（如「关的」「的词」）不具备检索意义，直接丢弃
_PARTICLE_CHARS = set("的了是吗呢吧啊呀嘛")


def tokenize(text: str) -> list[str]:
    """分词：中文按 2-gram，英文 / 数字按词；单字中文也保留（提升短查询召回）。

    含「的 / 了 / 是 / 吗」等虚词的 2-gram 会被过滤（例如「无关的」产生「关的」，
    否则会误命中「相关的」），减少噪声召回。
    """
    raw = str(text or "").lower()
    tokens: list[str] = []
    for run in _CJK_RUN_RE.findall(raw):
        if len(run) == 1:
            if run not in _PARTICLE_CHARS:
                tokens.append(run)
        else:
            for index in range(len(run) - 1):
                gram = run[index : index + 2]
                if any(char in _PARTICLE_CHARS for char in gram):
                    continue
                tokens.append(gram)
    tokens.extend(_WORD_RE.findall(raw))
    return [token for token in tokens if token.strip()]


def _doc_tokens(policy: dict[str, Any]) -> list[str]:
    """文档词频：标题 ×3、标签与来源 ×2、正文 ×1（标题权重高，符合政策检索直觉）。"""
    tokens: list[str] = []
    tokens.extend(tokenize(policy.get("title") or "") * 3)
    tokens.extend(tokenize(" ".join(policy.get("tags") or [])) * 2)
    tokens.extend(tokenize(policy.get("source") or "") * 2)
    tokens.extend(tokenize(policy.get("content") or ""))
    return tokens


def search(query: str, top_k: int = 5, category: str | None = None) -> list[dict[str, Any]]:
    """BM25 检索政策，返回带 ``score`` 的政策列表（降序）；无命中返回空列表。"""
    query_tokens = tokenize(query)
    if not query_tokens:
        return []
    policies = list_policies(category)
    if not policies:
        return []
    doc_token_lists = [_doc_tokens(policy) for policy in policies]
    doc_len = [len(tokens) for tokens in doc_token_lists]
    avg_len = sum(doc_len) / len(doc_len) if doc_len else 0.0
    total_docs = len(policies)

    doc_freq: Counter[str] = Counter()
    for tokens in doc_token_lists:
        doc_freq.update(set(tokens))

    k1 = 1.5
    b = 0.75
    query_set = list(dict.fromkeys(query_tokens))
    results: list[tuple[float, dict[str, Any]]] = []
    for index, policy in enumerate(policies):
        tokens = doc_token_lists[index]
        if not tokens:
            continue
        frequencies = Counter(tokens)
        length = doc_len[index] or 1
        score = 0.0
        for token in query_set:
            tf = frequencies.get(token, 0)
            if tf <= 0:
                continue
            df = doc_freq.get(token, 0)
            idf = math.log(1.0 + (total_docs - df + 0.5) / (df + 0.5))
            denominator = tf + k1 * (1.0 - b + b * length / (avg_len or 1.0))
            score += idf * tf * (k1 + 1.0) / denominator
        # 标题 / 标签直接命中额外加权，避免长文档淹没标题匹配
        title_tokens = set(tokenize(policy.get("title") or ""))
        tag_tokens = set(tokenize(" ".join(policy.get("tags") or [])))
        for token in query_set:
            if token in title_tokens:
                score += 0.8
            elif token in tag_tokens:
                score += 0.4
        if score > 0:
            item = dict(policy)
            item["score"] = round(score, 4)
            results.append((score, item))

    results.sort(key=lambda pair: (-pair[0], pair[1]["title"]))
    return [item for _, item in results[: max(1, int(top_k))]]


# =============================================================================
# 上下文 / 回答
# =============================================================================
def _excerpt(policy: dict[str, Any], limit: int = 600) -> str:
    """正文摘录：去掉标题标记、压缩空行。"""
    body = re.sub(r"^#{1,6}\s*", "", policy.get("content") or "", flags=re.MULTILINE)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body[:limit]


def build_context(query: str, top_k: int = 4) -> str:
    """构建供 LLM 使用的检索上下文（含政策名称、文号、生效日期、来源与原文摘录）。"""
    matched = search(query, top_k=top_k)
    if not matched:
        return f"（政策知识库未检索到与「{query}」相关的政策条目。）"
    blocks: list[str] = []
    for index, policy in enumerate(matched, start=1):
        expiry = policy.get("expiry_date") or "长期"
        blocks.append(
            f"### {index}. {policy['title']}（分类：{policy['category']}）\n"
            f"- 政策文号 / 来源：{policy.get('source') or '未标注'}\n"
            f"- 生效日期：{policy.get('effective_date') or '未标注'}；执行期限：{expiry}\n"
            f"- 最后核验日期：{policy.get('last_verified') or '未标注'}\n"
            f"- 要点：{policy.get('summary') or ''}\n"
            f"- 原文摘录：{_excerpt(policy, 500)}\n"
            f"- 文件：{policy.get('id')}"
        )
    return "\n\n".join(blocks)


def _citations(matched: Iterable[dict[str, Any]], limit: int = 4) -> list[dict[str, Any]]:
    citations = []
    for policy in list(matched)[:limit]:
        citations.append(
            {
                "id": policy.get("id"),
                "title": policy.get("title"),
                "source": policy.get("source"),
                "effective_date": policy.get("effective_date"),
                "expiry_date": policy.get("expiry_date"),
                "last_verified": policy.get("last_verified"),
                "category": policy.get("category"),
                "path": policy.get("path"),
            }
        )
    return citations


def _matched_brief(matched: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"id": policy.get("id"), "title": policy.get("title"), "category": policy.get("category"), "score": policy.get("score")}
        for policy in matched
    ]


def _local_answer(query: str, matched: list[dict[str, Any]]) -> str:
    """无 API Key 时的确定性回答：检索摘要 + 原文片段 + 来源。"""
    if not matched:
        return (
            f"政策知识库中暂未检索到与「{query}」直接相关的政策条目。\n"
            "建议：换用更具体的关键词（如「小规模纳税人 增值税 免税」「研发费用 加计扣除」），"
            "或咨询主管税务机关确认口径。"
        )
    lines = [f"根据政策知识库检索到 {len(matched)} 条相关依据：", ""]
    for index, policy in enumerate(matched[:4], start=1):
        expiry = policy.get("expiry_date") or "长期"
        lines.append(f"{index}. 【{policy['title']}】")
        lines.append(
            f"   来源：{policy.get('source') or '未标注'}；"
            f"生效日期：{policy.get('effective_date') or '未标注'}（执行至 {expiry}）；"
            f"最后核验：{policy.get('last_verified') or '未标注'}"
        )
        summary = policy.get("summary") or ""
        if summary:
            lines.append(f"   要点：{summary}")
        if index <= 2:
            excerpt = _excerpt(policy, 320).replace("\n", " ")
            if excerpt:
                lines.append(f"   原文片段：{excerpt}…")
        lines.append("")
    lines.append("⚠️ 以上为知识库检索摘要，具体执行口径以主管税务机关为准。")
    return "\n".join(lines).strip()


def answer(
    query: str,
    *,
    client: Any | None = None,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """回答政策问题：检索政策并生成带引用的中文回答。

    参数
    ----
    query   : 用户问题
    client  : :class:`agent.llm_client.DeepSeekClient`；为空或无 Key 时走本地检索摘要
    history : 可选历史消息（仅在有 LLM 时作为对话上下文）

    返回
    ----
    ``{"answer", "used_llm", "citations", "matched", "disclaimer", "method"}``
    """
    text = str(query or "").strip()
    disclaimer = config.DISCLAIMER
    matched = search(text, top_k=5)
    citations = _citations(matched)
    matched_brief = _matched_brief(matched)

    if not matched:
        return {
            "answer": _local_answer(text, matched) + "\n\n" + disclaimer,
            "used_llm": False,
            "citations": [],
            "matched": [],
            "method": "retrieval",
            "disclaimer": disclaimer,
        }

    if client is not None and getattr(client, "configured", False):
        try:
            from agent.llm_client import DeepSeekError
            from agent.prompt_templates import POLICY_PROMPT

            context = build_context(text, top_k=4)
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": "你是「税小智」的政策解读助手，只依据给定政策上下文回答，不得编造。"}
            ]
            for message in (history or [])[-6:]:
                if isinstance(message, dict) and message.get("role") in ("user", "assistant") and message.get("content"):
                    messages.append({"role": message["role"], "content": str(message["content"])})
            messages.append({"role": "user", "content": POLICY_PROMPT.format(context=context, question=text)})
            response = client.chat(messages, temperature=0.2)
            content = (response.get("content") or "").strip()
            if content:
                if disclaimer not in content:
                    content = content.rstrip() + "\n\n" + disclaimer
                return {
                    "answer": content,
                    "used_llm": True,
                    "citations": citations,
                    "matched": matched_brief,
                    "method": "llm",
                    "disclaimer": disclaimer,
                }
        except DeepSeekError as exc:
            return {
                "answer": _local_answer(text, matched)
                + f"\n\n（DeepSeek 调用失败：{exc}，已切换为本地检索摘要。）\n"
                + disclaimer,
                "used_llm": False,
                "citations": citations,
                "matched": matched_brief,
                "method": "retrieval_fallback",
                "error": str(exc),
                "disclaimer": disclaimer,
            }
        except Exception as exc:  # noqa: BLE001 - 兜底：绝不因 LLM 异常让政策问答不可用
            return {
                "answer": _local_answer(text, matched)
                + f"\n\n（智能作答暂不可用：{exc}，已切换为本地检索摘要。）\n"
                + disclaimer,
                "used_llm": False,
                "citations": citations,
                "matched": matched_brief,
                "method": "retrieval_fallback",
                "error": str(exc),
                "disclaimer": disclaimer,
            }

    return {
        "answer": _local_answer(text, matched) + "\n\n" + disclaimer,
        "used_llm": False,
        "citations": citations,
        "matched": matched_brief,
        "method": "retrieval",
        "disclaimer": disclaimer,
    }


def categories() -> list[str]:
    """知识库中出现的分类列表（用于 UI 筛选）。"""
    seen: list[str] = []
    for policy in load_policies():
        category = policy["category"]
        if category and category not in seen:
            seen.append(category)
    return seen
