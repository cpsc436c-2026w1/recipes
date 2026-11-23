"""Names of everything the experiment makes, from PREFIX (default "recipe"); Glue names use it with "-" as "_"."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import cached_property


@dataclass
class Names:
    prefix: str = field(default_factory=lambda: os.environ.get("PREFIX") or "recipe")
    sess: object = None

    @property
    def glue_prefix(self) -> str:
        return self.prefix.replace("-", "_").lower()

    @property
    def database(self) -> str:
        return f"{self.glue_prefix}_layout"

    @property
    def wg(self) -> str:
        return f"{self.prefix}-wg"

    @property
    def ctas_wg(self) -> str:
        return f"{self.prefix}-ctas-wg"

    @property
    def function(self) -> str:
        return f"{self.prefix}-duckdb-query"

    @property
    def role(self) -> str:
        return f"{self.prefix}-duckdb-role"

    @property
    def log_group(self) -> str:
        return f"/aws/lambda/{self.function}"

    @cached_property
    def bucket(self) -> str:
        import lake

        return lake.bucket_name(self.sess)
