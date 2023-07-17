import json
from typing import Any

try:
    import orjson
except ModuleNotFoundError:
    HAS_ORJSON = False
else:
    HAS_ORJSON = True


__all__ = (
    "from_json",
    "MISSING",
    "to_json",
)



class _MissingSentinel:
    def __eq__(self, other: Any) -> bool:
        return False

    def __hash__(self) -> int:
        return 0

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "..."


MISSING: Any = _MissingSentinel()



if HAS_ORJSON:

    def to_json(obj: Any) -> str:
        return orjson.dumps(obj).decode("utf-8")

    from_json = orjson.loads  # type: ignore

else:

    def to_json(obj: Any) -> str:
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=True)

    from_json = json.loads
