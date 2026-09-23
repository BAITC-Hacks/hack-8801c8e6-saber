"""Reject broken datasets at startup, before they affect budget or score."""
import copy
import json

import pytest

from app.data_loader import BASE_DIR, DataValidationError, load_data, load_data_or_exit
from app.engine import simulate


@pytest.fixture
def source_files(tmp_path):
    paths = {
        "config": BASE_DIR / "config.json",
        "districts": BASE_DIR / "data" / "districts.json",
        "measures": BASE_DIR / "data" / "measures.json",
    }
    source = {name: json.loads(path.read_text(encoding="utf-8")) for name, path in paths.items()}

    def write(change=None):
        values = copy.deepcopy(source)
        if change is not None:
            change(values)
        for name, value in values.items():
            (tmp_path / f"{name}.json").write_text(json.dumps(value), encoding="utf-8")
        return tmp_path, tmp_path / "config.json"

    return write


def set_value(section, key, value, index=None):
    def change(source):
        target = source[section] if index is None else source[section][index]
        target[key] = value
    return change


@pytest.mark.parametrize("change", [
    set_value("config", "horizon_quarters", 0),
    set_value("config", "horizon_quarters", 1),
    set_value("config", "horizon_quarters", True),
    set_value("config", "budget", float("nan")),
    set_value("config", "budget", -1),
    set_value("config", "lambda", float("inf")),
    set_value("config", "lambda", 1.5),
    set_value("config", "crit_threshold", 101),
    set_value("config", "crit_penalty", -1),
    set_value("config", "decisions_count", 0),
    set_value("config", "max_per_direction", "2"),
    set_value("measures", "cost", -10, 0),
    set_value("measures", "cost", 0, 0),
    set_value("measures", "cost", float("nan"), 0),
    set_value("measures", "cost", True, 0),
    set_value("measures", "effects", {"T1": float("inf")}, 0),
    set_value("measures", "effects", {"T1": "6"}, 0),
    set_value("measures", "effects", {"UNKNOWN": 6}, 0),
    set_value("measures", "scope", "unknown", 0),
    set_value("measures", "lag", 8, 0),
    set_value("measures", "lag", 1.5, 0),
    set_value("measures", "id", "M2", 0),
    set_value("districts", "id", "almaty", 0),
    set_value("districts", "population_share", float("nan"), 0),
    set_value("districts", "indicators", {"T1": 50}, 0),
    lambda s: s["districts"][0]["indicators"].update(T1=float("nan")),
    lambda s: s["config"]["indicators"][0].update(weight=float("nan")),
    lambda s: s["config"]["indicators"][0].update(direction="missing"),
    lambda s: s["config"]["indicators"].append(dict(s["config"]["indicators"][0])),
    lambda s: s["config"]["synergies"][0].update(host="M10"),
    lambda s: s["config"]["synergies"][0].update(pair=["M1"]),
    lambda s: s["config"]["synergies"][0].update(pair=["M1", "M1"]),
    lambda s: s["config"]["synergies"][0].update(bonus=float("inf")),
    lambda s: s["config"]["incompatibilities"][0].update(pair=["M1", "M1"]),
    lambda s: s["config"]["incompatibilities"][0].update(same_district_only="false"),
    lambda s: s.update(districts=[]),
    lambda s: s.update(measures={}),
    lambda s: s.update(config=[]),
])
def test_invalid_dataset_is_rejected_before_simulation(source_files, change):
    data_dir, config_path = source_files(change)
    with pytest.raises(DataValidationError):
        load_data(data_dir, config_path)


def test_negative_shares_cannot_cancel_positive_shares(source_files):
    def invalid_shares(source):
        # Previously these passed the sum=1 check and produced an invalid model.
        source["districts"][0]["population_share"] = -0.27
        source["districts"][1]["population_share"] += 0.54

    with pytest.raises(DataValidationError, match="population_share"):
        load_data(*source_files(invalid_shares))


def test_negative_weights_cannot_cancel_positive_weights(source_files):
    def invalid_weights(source):
        source["config"]["indicators"][0]["weight"] = -0.1
        source["config"]["indicators"][1]["weight"] = 0.3

    with pytest.raises(DataValidationError, match="weight"):
        load_data(*source_files(invalid_weights))


def test_valid_source_preserves_original_values_and_score(source_files):
    validated = load_data(*source_files())
    original = load_data()
    assert validated == original
    assert simulate([], validated)["score_after"] == pytest.approx(52.55768)


@pytest.mark.parametrize("contents", ['{"budget":', "\ud800"])
def test_unreadable_json_has_startup_diagnostic(source_files, contents, capsys):
    data_dir, config_path = source_files()
    config_path.write_bytes(contents.encode("utf-8", errors="surrogatepass"))
    with pytest.raises(SystemExit) as exc:
        load_data_or_exit(data_dir, config_path)
    assert exc.value.code == 1
    diagnostic = capsys.readouterr().err
    assert "config.json" in diagnostic
    assert "Ошибка загрузки данных" in diagnostic


def test_missing_file_has_startup_diagnostic(tmp_path):
    with pytest.raises(DataValidationError, match="config.json"):
        load_data(tmp_path, tmp_path / "config.json")
