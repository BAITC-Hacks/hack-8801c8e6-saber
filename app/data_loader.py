"""Loads and validates data/*.json and config.json at startup."""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = BASE_DIR / "config.json"

# Validate source files before building indexes or performing arithmetic. In
# particular, JSON's NaN/Infinity and Python's bool-as-int must not enter scores.
Number = Annotated[float, Field(strict=True, allow_inf_nan=False)]
UnitShare = Annotated[Number, Field(ge=0, le=1)]
Name = Annotated[str, Field(strict=True, min_length=1, pattern=r"\S")]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
Pair = Annotated[list[Name], Field(min_length=2, max_length=2)]


class _DataModel(BaseModel):
    model_config = ConfigDict(strict=True)


class _Indicator(_DataModel):
    code: Name
    direction: Name
    name: Name
    weight: UnitShare


class _Synergy(_DataModel):
    pair: Pair
    host: Name
    indicator: Name
    bonus: Number


class _Incompatibility(_DataModel):
    pair: Pair
    same_district_only: bool = False
    reason: str = ""


class _Config(_DataModel):
    budget: Annotated[Number, Field(gt=0)]
    currency: Name
    decisions_count: PositiveInt
    max_per_direction: PositiveInt
    horizon_quarters: PositiveInt
    score_weight: UnitShare = Field(alias="lambda")
    crit_threshold: Annotated[Number, Field(ge=0, le=100)]
    crit_penalty: Annotated[Number, Field(ge=0)]
    directions: Annotated[dict[Name, Name], Field(min_length=1)]
    indicators: Annotated[list[_Indicator], Field(min_length=1)]
    synergies: list[_Synergy] = Field(default_factory=list)
    incompatibilities: list[_Incompatibility] = Field(default_factory=list)


class _District(_DataModel):
    id: Name
    name: Name
    population_share: UnitShare
    indicators: dict[Name, Annotated[Number, Field(ge=0, le=100)]]


class _Measure(_DataModel):
    id: Name
    direction: Name
    name: Name
    scope: Literal["city", "district"]
    cost: Annotated[Number, Field(gt=0)]
    lag: Annotated[int, Field(strict=True, ge=0, le=7)]
    effects: Annotated[dict[Name, Number], Field(min_length=1)]


@dataclass
class AppData:
    config: dict[str, Any]
    districts: list[dict[str, Any]]
    measures: list[dict[str, Any]]
    districts_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    measures_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    indicator_codes: list[str] = field(default_factory=list)
    indicator_weight: dict[str, float] = field(default_factory=dict)
    indicator_name: dict[str, str] = field(default_factory=dict)
    direction_name: dict[str, str] = field(default_factory=dict)


class DataValidationError(Exception):
    pass


def _load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, UnicodeError, ValueError) as exc:
        raise DataValidationError(f"Не удалось прочитать {path}: {exc}") from exc


def _validate_structure(value: Any, schema: Any, filename: str) -> None:
    try:
        TypeAdapter(schema).validate_python(value)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        raise DataValidationError(f"{filename}: {details}") from exc


def load_data(data_dir: Path = DATA_DIR, config_path: Path = CONFIG_PATH) -> AppData:
    config = _load_json(config_path)
    districts = _load_json(data_dir / "districts.json")
    measures = _load_json(data_dir / "measures.json")

    _validate_structure(config, _Config, "config.json")
    _validate_structure(districts, Annotated[list[_District], Field(min_length=1)], "districts.json")
    _validate_structure(measures, Annotated[list[_Measure], Field(min_length=1)], "measures.json")

    errors: list[str] = []

    indicator_codes = [ind["code"] for ind in config.get("indicators", [])]
    indicator_weight = {ind["code"]: ind["weight"] for ind in config.get("indicators", [])}
    indicator_name = {ind["code"]: ind["name"] for ind in config.get("indicators", [])}
    direction_name = config.get("directions", {})

    if len(set(indicator_codes)) != len(indicator_codes):
        errors.append("Коды показателей должны быть уникальны")
    for indicator in config["indicators"]:
        if indicator["direction"] not in direction_name:
            errors.append(f"Показатель {indicator['code']}: неизвестное direction {indicator['direction']}")

    weight_sum = sum(indicator_weight.values())
    if abs(weight_sum - 1.0) > 0.001:
        errors.append(f"Сумма весов показателей должна быть 1.0, получено {weight_sum}")

    pop_sum = 0.0
    districts_by_id: dict[str, dict[str, Any]] = {}
    for d in districts:
        if d["id"] in districts_by_id:
            errors.append(f"Повторный id района: {d['id']}")
        pop_sum += d.get("population_share", 0)
        indicators = d.get("indicators", {})
        if set(indicators.keys()) != set(indicator_codes):
            errors.append(f"Район {d.get('id')}: набор показателей не совпадает с config.indicators")
        districts_by_id[d["id"]] = d

    if abs(pop_sum - 1.0) > 0.001:
        errors.append(f"Сумма population_share должна быть 1.0, получено {pop_sum}")

    measures_by_id: dict[str, dict[str, Any]] = {}
    for m in measures:
        if m["id"] in measures_by_id:
            errors.append(f"Повторный id мероприятия: {m['id']}")
        if m.get("direction") not in direction_name:
            errors.append(f"Мера {m.get('id')}: неизвестное direction {m.get('direction')}")
        if m["lag"] >= config["horizon_quarters"]:
            errors.append(f"Мера {m['id']}: lag должен быть меньше horizon_quarters")
        for code in m.get("effects", {}):
            if code not in indicator_codes:
                errors.append(f"Мера {m.get('id')}: неизвестный показатель эффекта {code}")
        measures_by_id[m["id"]] = m

    for s in config.get("synergies", []):
        if len(set(s["pair"])) != 2:
            errors.append(f"Синергия {s['pair']}: нужны две разные меры")
        for mid in s["pair"]:
            if mid not in measures_by_id:
                errors.append(f"Синергия {s['pair']}: мера {mid} не найдена")
        host = s.get("host")
        if host not in s["pair"]:
            errors.append(f"Синергия {s['pair']}: host {host} должен входить в pair")
        if host not in measures_by_id:
            errors.append(f"Синергия {s['pair']}: host {host} не найден")
        elif measures_by_id[host].get("scope") != "district":
            errors.append(f"Синергия {s['pair']}: host {host} должен иметь scope 'district'")
        if s.get("indicator") not in indicator_codes:
            errors.append(f"Синергия {s['pair']}: неизвестный показатель {s.get('indicator')}")

    for inc in config.get("incompatibilities", []):
        if len(set(inc["pair"])) != 2:
            errors.append(f"Несовместимость {inc['pair']}: нужны две разные меры")
        for mid in inc["pair"]:
            if mid not in measures_by_id:
                errors.append(f"Несовместимость {inc['pair']}: мера {mid} не найдена")

    if errors:
        raise DataValidationError("; ".join(errors))

    return AppData(
        config=config,
        districts=districts,
        measures=measures,
        districts_by_id=districts_by_id,
        measures_by_id=measures_by_id,
        indicator_codes=indicator_codes,
        indicator_weight=indicator_weight,
        indicator_name=indicator_name,
        direction_name=direction_name,
    )


def load_data_or_exit(data_dir: Path = DATA_DIR, config_path: Path = CONFIG_PATH) -> AppData:
    try:
        return load_data(data_dir, config_path)
    except DataValidationError as e:
        print(f"Ошибка загрузки данных: {e}", file=sys.stderr)
        sys.exit(1)
