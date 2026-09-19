"""锁组选择（POST /lock-groups 的核心算法）。

各环境按 /resolve 规则独立求解、候选锁去重、并逐份在所有环境下按
/audit-lock 规则交叉校验之后，本模块负责“用最少锁文件覆盖全部环境”的
精确选择：

* 每份去重候选锁的**覆盖集** = 它在哪些目标环境下通过审计（valid）；
  任一环境至少被自身求出的锁覆盖（求解结果在本环境下必然通过审计），
  因此最小覆盖必然存在；
* 精确枚举候选锁的全部子集，取覆盖全部环境且**数量最少**者
  （环境 2～8 个、去重锁至多 8 份，枚举规模不超过 2^8，成本可忽略）；
* 并列最优时按（锁内容规范化串, 各锁覆盖环境 id 元组）的稳定序取
  字典序最小者——同一输入的选择结果确定，与求解/枚举顺序无关；
* 锁组 id 由锁内容的 SHA-256 派生：同一份锁内容恒得同一 id，
  与请求中环境的个数、顺序无关。
"""

from __future__ import annotations

import hashlib
import itertools
import json


def canonical_lock_key(locked: dict[str, str]) -> str:
    """锁内容的规范化串：键排序的紧凑 JSON，用于去重与并列裁决。

    锁定清单的包名已按 PEP 503 规范化、版本为 PEP 440 规范化字符串，
    因此内容相同的两份锁（可能来自不同环境）得到完全相同的键。
    """
    return json.dumps(
        locked, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def lock_group_id(locked: dict[str, str]) -> str:
    """锁组稳定 id：``lock-`` + 锁内容规范化串的 SHA-256 前 12 位十六进制。"""
    digest = hashlib.sha256(canonical_lock_key(locked).encode("utf-8")).hexdigest()
    return f"lock-{digest[:12]}"


def select_minimum_cover(
    coverage: dict[str, set[str]], universe: set[str]
) -> list[str]:
    """精确最小集合覆盖：返回被选中的锁内容键列表（数量最少）。

    ``coverage`` 为 锁内容键 -> 该锁通过审计的环境 id 集合；``universe``
    为全部环境 id。并列最优时按（锁内容键元组, 各锁覆盖环境 id 元组）
    的字典序取最小者，结果确定。

    不变量：每个环境至少被自身求解出的锁覆盖，因此最小覆盖必然存在；
    若调用方违反该不变量则回退为返回全部候选（仍然覆盖全集）。
    """
    keys = sorted(coverage)
    best: tuple[tuple, tuple[str, ...]] | None = None
    for size in range(1, len(keys) + 1):
        for combo in itertools.combinations(keys, size):
            covered: set[str] = set()
            for key in combo:
                covered |= coverage[key]
            if not universe <= covered:
                continue
            # 稳定裁决键：先比锁内容（组合本身即排序后的内容键元组），
            # 再比各锁覆盖的环境 id 元组；枚举顺序不影响结果。
            tie_key = (
                combo,
                tuple(sorted(tuple(sorted(coverage[key])) for key in combo)),
            )
            if best is None or tie_key < best[0]:
                best = (tie_key, combo)
        if best is not None:
            break
    if best is None:  # pragma: no cover - 不变量保证不可达
        return keys
    return list(best[1])
