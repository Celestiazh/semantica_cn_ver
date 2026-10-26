# -*- coding: utf-8 -*-
"""配置装载：把 config/*.yaml 读成对象，代码里不硬编码任何关系名与路径。

设计口径：
  - **人工决定是数据、不是代码** —— 名册 / 负向表 / 关系定义都是可改的 YAML，重跑即可生效
  - 配置缺失要**显式告警**，不能静默走默认值（尤其名册：没有名册就等于没有消解防线）
"""
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]      # kg/
CONFIG_DIR = PROJECT_ROOT / "config"


class ConfigError(RuntimeError):
    pass


class Config:
    def __init__(self, root: Path = None):
        self.root = Path(root) if root else PROJECT_ROOT
        self.config_dir = self.root / "config"
        self.warnings = []

        self.settings = self._load("settings.yaml", required=True)
        self.relations = self._load("relations.yaml", required=True)
        self.roster = self._load("roster.yaml", required=False)
        self.denied = self._load("denied_pairs.yaml", required=False)

    # ---------------- 装载 ----------------
    def _load(self, name, required):
        path = self.config_dir / name
        if not path.is_file():
            if required:
                raise ConfigError("缺少必需配置：%s" % path)
            self.warnings.append("缺少可选配置：%s（已按空处理）" % name)
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data or {}

    # ---------------- 路径 ----------------
    def _path(self, key) -> Path:
        raw = (self.settings.get("paths") or {}).get(key, "")
        p = Path(raw)
        return p if p.is_absolute() else (self.root / p)

    @property
    def input_dir(self) -> Path:
        return self._path("input_dir")

    @property
    def work_dir(self) -> Path:
        return self._path("work_dir")

    @property
    def output_dir(self) -> Path:
        return self._path("output_dir")

    # ---------------- 取用 ----------------
    def get(self, dotted, default=None):
        """settings.yaml 里的点号取值：cfg.get('parse.dpi', 200)"""
        cur = self.settings
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    @property
    def node_types(self):
        return [n.get("type") for n in (self.relations.get("nodes") or [])]

    @property
    def relation_types(self):
        return [r.get("type") for r in (self.relations.get("relations") or [])]

    def relation_defs(self, layer=None):
        out = []
        for r in (self.relations.get("relations") or []):
            if layer is None or r.get("layer") == layer:
                out.append(r)
        return out

    @property
    def units(self):
        return self.roster.get("units") or []

    def alias_map(self):
        """别名 / 全称 → 权威全称。消解第 1 步查这张表。"""
        m = {}
        for u in self.units:
            name = u.get("name")
            if not name:
                continue
            m[name] = name
            for a in (u.get("aliases") or []):
                m[a] = name
        return m

    def denied_pairs(self):
        """驳回过的一对 → set of frozenset，dedup 提候选后直接过滤。"""
        out = set()
        for d in (self.denied.get("denied") or []):
            a, b = d.get("a"), d.get("b")
            if a and b:
                out.add(frozenset((a, b)))
        return out

    def pending_pairs(self):
        return [(d.get("a"), d.get("b")) for d in (self.denied.get("pending") or [])]

    # ---------------- 自检 ----------------
    def check(self):
        """返回 (问题列表, 别名表)。run.py 与测试都用它。"""
        problems = list(self.warnings)
        if not self.units:
            problems.append(
                "名册为空 —— 消解判定没有依据，"
                "输出报告里必须显式标注「未启用名册校验」"
            )
        if not (self.relations.get("nodes") or []):
            problems.append("relations.yaml 里没有任何节点类型")
        if not (self.relations.get("relations") or []):
            problems.append("relations.yaml 里没有任何关系定义")
        dup = {}
        amap = self.alias_map()
        for u in self.units:
            for a in (u.get("aliases") or []):
                if a in dup:
                    problems.append("别名重复：%s 同时出现在 %s 与 %s"
                                    % (a, dup[a], u.get("name")))
                dup[a] = u.get("name")
        return problems, amap


def load(root: Path = None) -> Config:
    return Config(root)
