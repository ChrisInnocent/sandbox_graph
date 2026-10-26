"""外部条件参数文件（condition profile）——把测试条件从脚本硬编码中解耦。

一个 profile 文件描述一次（或一批）实验的**条件变量**：单节点目标密度、创建并发、
沙箱规格（如 `2v2g`/`2v4g`）、请求量、重复次数、端点/拓扑等。脚本用
`--profile <file>` 读取，而不是在代码里写死；这样可以把条件整体调低
（例如单组、2 沙箱、并发 1、2v2g）来验证**图工程/循环工程**是否打通。

设计约束：
- 严格模型（extra='forbid'）：未知键直接拒绝，不静默忽略拼写错误；
- 规格用 `NvMg` 标签解析为 `vcpus + memory_bytes`，避免两处单位不一致；
- 只描述**条件**，不产出**结论**：不得由 profile 推导测量默认值或填补缺口。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import Field, field_validator, model_validator

from .contracts import StrictModel

GIB = 1024 ** 3
PositiveInt = Annotated[int, Field(strict=True, gt=0)]


def parse_spec_label(label: str) -> tuple[int, int]:
    """`2v2g` / `2v2` -> `(vcpus, memory_bytes)`；其余形式拒绝。"""
    if not isinstance(label, str):
        raise ValueError(f'spec label must be a string, got {type(label).__name__}')
    token = label.strip().lower()
    if token.endswith('g'):
        token = token[:-1]
    if token.count('v') != 1:
        raise ValueError(f'invalid spec label {label!r}; expected e.g. 2v2g')
    vcpus_text, memory_text = token.split('v')
    if not vcpus_text.isdigit() or not memory_text.isdigit():
        raise ValueError(f'invalid spec label {label!r}; expected e.g. 2v2g')
    vcpus, memory_gib = int(vcpus_text), int(memory_text)
    if vcpus < 1 or memory_gib < 1:
        raise ValueError(f'invalid spec label {label!r}; vcpus and memory must be >= 1')
    return vcpus, memory_gib * GIB


class SpecProfile(StrictModel):
    """沙箱规格：`vcpus` + `memory_gib`，可由 `'2v4g'` 字符串构造。"""

    vcpus: PositiveInt
    memory_gib: PositiveInt

    @model_validator(mode='before')
    @classmethod
    def from_label(cls, value):
        if isinstance(value, str):
            vcpus, memory_bytes = parse_spec_label(value)
            return {'vcpus': vcpus, 'memory_gib': memory_bytes // GIB}
        return value

    @property
    def memory_bytes(self) -> int:
        return self.memory_gib * GIB

    @property
    def label(self) -> str:
        return f'{self.vcpus}v{self.memory_gib}g'


class MatrixAxes(StrictModel):
    """可选矩阵轴；为空时按单格处理。`specs` 用 `NvMg` 标签。"""

    specs: tuple[str, ...] = ()
    concurrency: tuple[PositiveInt, ...] = ()
    requests: tuple[PositiveInt, ...] = ()

    @field_validator('specs', 'concurrency', 'requests', mode='before')
    @classmethod
    def as_tuple(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @field_validator('specs')
    @classmethod
    def known_spec_labels(cls, value):
        for label in value:
            parse_spec_label(label)
        return value


class ConditionProfile(StrictModel):
    schema_version: Literal['conditions.v1']
    profile: str
    groups: PositiveInt = 1
    works_per_group: PositiveInt = 1
    # 目标单节点沙箱密度（真实密度场景用；mock 验证可留空）
    target_density_per_node: PositiveInt | None = None
    spec: SpecProfile
    concurrency: PositiveInt = 1
    max_work_concurrency: PositiveInt = 1
    requests: PositiveInt = 1
    repeat: PositiveInt = 1
    methods: tuple[str, ...] = ('B01.TEMPLATE',)
    topology: str = 'node_direct'
    completion_endpoint: str = 'STATE_READY'
    fail_last_group: bool = False
    axes: MatrixAxes = Field(default_factory=MatrixAxes)
    notes: str | None = None

    @field_validator('methods', mode='before')
    @classmethod
    def as_tuple(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @field_validator('methods')
    @classmethod
    def non_empty_methods(cls, value):
        if not value:
            raise ValueError('methods must not be empty')
        return value

    @property
    def total_works(self) -> int:
        return self.groups * self.works_per_group

    def mock_kwargs(self) -> dict:
        """转换为 `scripts/mock_pipeline.run_mock` 的关键字参数。"""
        return {'groups': self.groups,
                'works': self.total_works,
                'requests': self.requests,
                'concurrency': self.concurrency,
                'max_work_concurrency': self.max_work_concurrency,
                'vcpus': self.spec.vcpus,
                'memory_bytes': self.spec.memory_bytes,
                'fail_last_group': self.fail_last_group}

    def cells(self) -> list[dict]:
        """把 `axes` 展开成矩阵格；`axes` 为空则返回单格（主 profile 值）。"""
        specs = self.axes.specs or (self.spec.label,)
        concurrency = self.axes.concurrency or (self.concurrency,)
        requests = self.axes.requests or (self.requests,)
        cells = []
        for spec_label in specs:
            vcpus, memory_bytes = parse_spec_label(spec_label)
            for c in concurrency:
                for q in requests:
                    cells.append({'spec': spec_label, 'vcpus': vcpus, 'memory_bytes': memory_bytes,
                                  'concurrency': c, 'requests': q})
        return cells

    def summary(self) -> str:
        parts = [f"profile={self.profile}", f"groups={self.groups}",
                 f"works={self.total_works}", f"spec={self.spec.label}",
                 f"C={self.concurrency}", f"Q={self.requests}", f"repeat={self.repeat}",
                 f"methods={','.join(self.methods)}"]
        if self.target_density_per_node is not None:
            parts.insert(3, f"target_density/node={self.target_density_per_node}")
        if self.axes.specs or self.axes.concurrency or self.axes.requests:
            parts.append(f"axes={len(self.cells())}格")
        return ' '.join(parts)


def load_profile(path) -> ConditionProfile:
    """读取并校验 profile 文件（`.yaml`/`.yml` → YAML，否则 JSON）。"""
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f'condition profile not found: {source}')
    text = source.read_text(encoding='utf-8')
    if source.suffix.lower() in {'.yaml', '.yml'}:
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError('condition profile must contain a mapping at top level')
    return ConditionProfile.model_validate(data)
