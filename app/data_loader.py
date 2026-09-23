"""Loads and validates data/*.json and config.json at startup."""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = BASE_DIR / "config.json"

VALID_SCOPES = {"city", "district"}


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
    if not path.exists():
        raise DataValidationError(f"Файл не найден: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_data(data_dir: Path = DATA_DIR, config_path: Path = CONFIG_PATH) -> AppData:
    config = _load_json(config_path)
    districts = _load_json(data_dir / "districts.json")
    measures = _load_json(data_dir / "measures.json")

    errors: list[str] = []

    indicator_codes = [ind["code"] for ind in config.get("indicators", [])]
    indicator_weight = {ind["code"]: ind["weight"] for ind in config.get("indicators", [])}
    indicator_name = {ind["code"]: ind["name"] for ind in config.get("indicators", [])}
    indicator_direction = {ind["code"]: ind["direction"] for ind in config.get("indicators", [])}
    direction_name = config.get("directions", {})

    weight_sum = sum(indicator_weight.values())
    if abs(weight_sum - 1.0) > 0.001:
        errors.append(f"Сумма весов показателей должна быть 1.0, получено {weight_sum}")

    pop_sum = 0.0
    districts_by_id: dict[str, dict[str, Any]] = {}
    for d in districts:
        pop_sum += d.get("population_share", 0)
        indicators = d.get("indicators", {})
        if set(indicators.keys()) != set(indicator_codes):
            errors.append(f"Район {d.get('id')}: набор показателей не совпадает с config.indicators")
        for code, value in indicators.items():
            if not (0 <= value <= 100):
                errors.append(f"Район {d.get('id')}: показатель {code}={value} вне диапазона 0-100")
        districts_by_id[d["id"]] = d

    if abs(pop_sum - 1.0) > 0.001:
        errors.append(f"Сумма population_share должна быть 1.0, получено {pop_sum}")

    measures_by_id: dict[str, dict[str, Any]] = {}
    for m in measures:
        if m.get("direction") not in direction_name:
            errors.append(f"Мера {m.get('id')}: неизвестное direction {m.get('direction')}")
        if m.get("scope") not in VALID_SCOPES:
            errors.append(f"Мера {m.get('id')}: неизвестный scope {m.get('scope')}")
        lag = m.get("lag")
        if lag is None or not (0 <= lag <= 7):
            errors.append(f"Мера {m.get('id')}: lag={lag} вне диапазона 0-7")
        for code in m.get("effects", {}):
            if code not in indicator_codes:
                errors.append(f"Мера {m.get('id')}: неизвестный показатель эффекта {code}")
        measures_by_id[m["id"]] = m

    for s in config.get("synergies", []):
        for mid in s["pair"]:
            if mid not in measures_by_id:
                errors.append(f"Синергия {s['pair']}: мера {mid} не найдена")
        host = s.get("host")
        if host not in measures_by_id:
            errors.append(f"Синергия {s['pair']}: host {host} не найден")
        elif measures_by_id[host].get("scope") != "district":
            errors.append(f"Синергия {s['pair']}: host {host} должен иметь scope 'district'")
        if s.get("indicator") not in indicator_codes:
            errors.append(f"Синергия {s['pair']}: неизвестный показатель {s.get('indicator')}")

    for inc in config.get("incompatibilities", []):
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
