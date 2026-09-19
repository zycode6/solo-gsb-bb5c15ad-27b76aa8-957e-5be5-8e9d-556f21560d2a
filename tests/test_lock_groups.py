"""POST /lock-groups 路由层与锁组选择算法测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.lock_groups import (
    canonical_lock_key,
    lock_group_id,
    select_minimum_cover,
)
from app.main import app

client = TestClient(app)

LINUX_PY39 = {
    "id": "linux-py39",
    "python_version": "3.9",
    "sys_platform": "linux",
    "platform_machine": "x86_64",
}
LINUX_PY311 = {
    "id": "linux-py311",
    "python_version": "3.11",
    "sys_platform": "linux",
    "platform_machine": "x86_64",
}
WIN_PY39 = {
    "id": "win-py39",
    "python_version": "3.9",
    "sys_platform": "win32",
    "platform_machine": "AMD64",
}
LINUX_ARM64_PY39 = {
    "id": "linux-arm64-py39",
    "python_version": "3.9",
    "sys_platform": "linux",
    "platform_machine": "aarch64",
}


def groups_payload(environments, **overrides):
    """示例快照：py39 多一条 shared-lib<2.0 约束，win 多一个 win-only 依赖。

    * linux-py39  -> {app, shared-lib 1.5.0}
    * linux-py311 -> {app, shared-lib 2.0.0}
    * win-py39    -> {app, shared-lib 1.5.0, win-only 1.0.0}
    """
    base = {
        "repository": {
            "app": [
                {"version": "1.0.0",
                 "dependencies": [
                     {"name": "shared-lib", "specifier": ">=1.0"},
                     {"name": "shared-lib", "specifier": "<2.0",
                      "marker": "python_version < '3.10'"},
                     {"name": "win-only", "specifier": "",
                      "marker": "sys_platform == 'win32'"},
                 ]}
            ],
            "shared-lib": [{"version": "1.5.0"}, {"version": "2.0.0"}],
            "win-only": [{"version": "1.0.0"}],
        },
        "root": "app",
        "specifier": "",
        "allow_prereleases": False,
        "target_environments": environments,
    }
    base.update(overrides)
    return base


def test_lock_groups_minimum_cover_shared_lock():
    """py39 的锁在 py311 下同样通过审计：2 份锁覆盖 3 个环境。"""
    r = client.post("/lock-groups", json=groups_payload(
        [LINUX_PY39, LINUX_PY311, WIN_PY39]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["root"] == "app"

    groups = body["groups"]
    assert len(groups) == 2
    # groups 按稳定 id 排序
    assert [g["id"] for g in groups] == sorted(g["id"] for g in groups)
    assert all(g["id"].startswith("lock-") for g in groups)

    by_lock = {
        tuple(sorted(g["locked"].items())): g for g in groups
    }
    linux_lock = by_lock[(("app", "1.0.0"), ("shared-lib", "1.5.0"))]
    # linux-py39 求出的锁在 linux-py311 下同样有效（1.5.0 满足 >=1.0）
    assert linux_lock["environments"] == ["linux-py311", "linux-py39"]
    win_lock = by_lock[
        (("app", "1.0.0"), ("shared-lib", "1.5.0"), ("win-only", "1.0.0"))
    ]
    assert win_lock["environments"] == ["win-py39"]

    # 并集覆盖全部环境
    covered = set()
    for g in groups:
        covered |= set(g["environments"])
    assert covered == {"linux-py39", "linux-py311", "win-py39"}


def test_lock_groups_single_lock_covers_all_environments():
    """无平台专属依赖时，约束更严环境的锁覆盖全部环境：只有 1 个锁组。"""
    r = client.post("/lock-groups", json=groups_payload(
        [LINUX_PY39, LINUX_PY311]))
    assert r.status_code == 200, r.text
    groups = r.json()["groups"]
    assert len(groups) == 1
    assert groups[0]["locked"] == {"app": "1.0.0", "shared-lib": "1.5.0"}
    assert groups[0]["environments"] == ["linux-py311", "linux-py39"]


def test_lock_groups_dedupes_identical_locks():
    """两个环境求出完全相同的锁时去重为一份，覆盖两个环境。"""
    r = client.post("/lock-groups", json=groups_payload(
        [LINUX_PY39, LINUX_ARM64_PY39]))
    assert r.status_code == 200, r.text
    groups = r.json()["groups"]
    assert len(groups) == 1
    assert groups[0]["environments"] == ["linux-arm64-py39", "linux-py39"]
    assert groups[0]["locked"] == {"app": "1.0.0", "shared-lib": "1.5.0"}


def test_lock_groups_platform_locks_not_cross_valid():
    """平台专属包使各锁互不兼容：每个环境各需一份锁。"""
    repo = {
        "app": [
            {"version": "1.0",
             "dependencies": [
                 {"name": "linux-only", "specifier": "",
                  "marker": "sys_platform == 'linux'"},
                 {"name": "win-only", "specifier": "",
                  "marker": "sys_platform == 'win32'"},
             ]}
        ],
        "linux-only": [{"version": "1.0"}],
        "win-only": [{"version": "1.0"}],
    }
    r = client.post("/lock-groups", json=groups_payload(
        [LINUX_PY39, WIN_PY39], repository=repo))
    assert r.status_code == 200, r.text
    groups = r.json()["groups"]
    assert len(groups) == 2
    coverage = {g["environments"][0]: g["locked"] for g in groups}
    assert coverage["linux-py39"] == {"app": "1.0", "linux-only": "1.0"}
    assert coverage["win-py39"] == {"app": "1.0", "win-only": "1.0"}


def test_lock_groups_deterministic_under_environment_permutation():
    """环境提交顺序不影响结果：同一输入逐字节一致。"""
    envs = [LINUX_PY39, LINUX_PY311, WIN_PY39]
    body_fwd = client.post("/lock-groups", json=groups_payload(envs)).json()
    body_rev = client.post(
        "/lock-groups", json=groups_payload(list(reversed(envs)))).json()
    assert body_fwd == body_rev


def test_lock_groups_group_id_is_content_addressed():
    """锁组 id 只取决于锁内容：同一锁内容在不同请求中 id 相同。"""
    lock = {"app": "1.0.0", "shared-lib": "1.5.0"}
    assert lock_group_id(lock) == lock_group_id(dict(reversed(list(lock.items()))))
    body1 = client.post("/lock-groups", json=groups_payload(
        [LINUX_PY39, LINUX_PY311])).json()
    body2 = client.post("/lock-groups", json=groups_payload(
        [LINUX_PY39, LINUX_PY311, WIN_PY39])).json()
    shared = {"app": "1.0.0", "shared-lib": "1.5.0"}
    id1 = [g["id"] for g in body1["groups"] if g["locked"] == shared][0]
    id2 = [g["id"] for g in body2["groups"] if g["locked"] == shared][0]
    assert id1 == id2 == lock_group_id(shared)


def test_lock_groups_409_preserves_conflict_sources():
    """任一环境无解：整体 409，failures 保留该环境的原冲突来源链。"""
    repo = {
        "app": [{"version": "1.0", "dependencies": [
            {"name": "left", "specifier": ""},
            {"name": "right", "specifier": "",
             "marker": "sys_platform == 'win32'"},
        ]}],
        "left": [{"version": "1.0",
                  "dependencies": [{"name": "core", "specifier": "==1.0"}]}],
        "right": [{"version": "1.0",
                   "dependencies": [{"name": "core", "specifier": "==2.0"}]}],
        "core": [{"version": "1.0"}, {"version": "2.0"}],
    }
    r = client.post("/lock-groups", json=groups_payload(
        [LINUX_PY39, WIN_PY39], repository=repo))
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["status"] == "conflict"
    assert "win-py39" in body["message"]
    assert len(body["failures"]) == 1
    failure = body["failures"][0]
    assert failure["id"] == "win-py39"
    assert failure["status"] == "conflict"
    assert failure["conflicts"][0]["subject"] == "core"
    assert failure["conflicts"][0]["kind"] == "incompatible"
    # 成功环境的锁不随 409 返回
    assert "groups" not in body


def test_lock_groups_409_lists_all_failing_environments_sorted():
    repo = {
        "app": [{"version": "1.0", "dependencies": [
            {"name": "core", "specifier": ">=99"}]}],
        "core": [{"version": "1.0"}],
    }
    r = client.post("/lock-groups", json=groups_payload(
        [WIN_PY39, LINUX_PY39, LINUX_PY311], repository=repo))
    assert r.status_code == 409
    body = r.json()
    assert [f["id"] for f in body["failures"]] == [
        "linux-py311", "linux-py39", "win-py39"
    ]


def test_lock_groups_duplicate_id_422():
    r = client.post("/lock-groups", json=groups_payload(
        [LINUX_PY39, {**LINUX_PY311, "id": "linux-py39"}]))
    assert r.status_code == 422
    assert r.json()["status"] == "invalid_snapshot"
    assert "linux-py39" in r.json()["message"]


def test_lock_groups_too_few_environments_422():
    r = client.post("/lock-groups", json=groups_payload([LINUX_PY39]))
    assert r.status_code == 422


def test_lock_groups_too_many_environments_422():
    envs = [{**LINUX_PY39, "id": f"env-{i}"} for i in range(9)]
    r = client.post("/lock-groups", json=groups_payload(envs))
    assert r.status_code == 422


def test_lock_groups_unknown_root_422():
    r = client.post("/lock-groups", json=groups_payload(
        [LINUX_PY39, LINUX_PY311], root="missing"))
    assert r.status_code == 422
    assert r.json()["status"] == "invalid_snapshot"


def test_lock_groups_bad_snapshot_422():
    repo = {"app": [{"version": "oops", "dependencies": []}]}
    r = client.post("/lock-groups", json=groups_payload(
        [LINUX_PY39, LINUX_PY311], repository=repo))
    assert r.status_code == 422
    assert "PEP 440" in r.json()["message"]


def test_lock_groups_bad_environment_value_422():
    bad = {**LINUX_PY39, "python_version": "not-a-version"}
    r = client.post("/lock-groups", json=groups_payload([bad, LINUX_PY311]))
    assert r.status_code == 422
    assert "python_version" in r.json()["message"]


def test_lock_groups_extra_field_forbidden_422():
    r = client.post("/lock-groups", json=groups_payload(
        [LINUX_PY39, LINUX_PY311], unknown_field=1))
    assert r.status_code == 422


def test_lock_groups_allow_prereleases_applies_to_resolve_and_audit():
    """预发布开关同时作用于各环境求解与交叉审计。"""
    repo = {
        "app": [{"version": "1.0", "dependencies": [
            {"name": "lib", "specifier": ""}]}],
        "lib": [{"version": "2.0rc1"}, {"version": "1.9"}],
    }
    r1 = client.post("/lock-groups", json=groups_payload(
        [LINUX_PY39, LINUX_PY311], repository=repo))
    assert r1.status_code == 200, r1.text
    assert len(r1.json()["groups"]) == 1
    assert r1.json()["groups"][0]["locked"]["lib"] == "1.9"

    r2 = client.post("/lock-groups", json=groups_payload(
        [LINUX_PY39, LINUX_PY311], repository=repo, allow_prereleases=True))
    assert r2.status_code == 200, r2.text
    assert len(r2.json()["groups"]) == 1
    assert r2.json()["groups"][0]["locked"]["lib"] == "2.0rc1"


# --------------------------------------------------------------------- #
# 选择算法（纯函数）
# --------------------------------------------------------------------- #
def test_canonical_lock_key_order_independent():
    lock = {"b": "1.0", "a": "2.0"}
    assert canonical_lock_key(lock) == canonical_lock_key(
        {"a": "2.0", "b": "1.0"}
    )
    assert canonical_lock_key(lock) != canonical_lock_key({"a": "2.0"})


def test_select_minimum_cover_prefers_fewest_locks():
    coverage = {
        "lock-a": {"e1", "e2"},
        "lock-b": {"e2"},
        "lock-c": {"e1"},
    }
    # lock-a 一份即可覆盖全部；其余组合都需要两份
    assert select_minimum_cover(coverage, {"e1", "e2"}) == ["lock-a"]


def test_select_minimum_cover_tie_break_by_lock_content():
    """同为两份锁即可覆盖时，按锁内容稳定序取字典序最小组合。"""
    coverage = {
        "lock-a": {"e1", "e2"},
        "lock-b": {"e2", "e3"},
        "lock-c": {"e1"},
        "lock-d": {"e3"},
    }
    universe = {"e1", "e2", "e3"}
    # 可行双锁组合：{a,b}、{a,d}、{b,c}；字典序最小为 {lock-a, lock-b}
    assert select_minimum_cover(coverage, universe) == ["lock-a", "lock-b"]


def test_select_minimum_cover_deterministic_regardless_of_input_order():
    coverage = {
        "lock-b": {"e2", "e3"},
        "lock-a": {"e1", "e2"},
        "lock-d": {"e3"},
        "lock-c": {"e1"},
    }
    universe = {"e1", "e2", "e3"}
    assert select_minimum_cover(coverage, universe) == ["lock-a", "lock-b"]
