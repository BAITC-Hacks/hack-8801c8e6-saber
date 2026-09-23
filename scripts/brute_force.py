"""Prints the global Score maximum and the top-5 valid decision sets."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data_loader import load_data_or_exit
from app.optimizer import brute_force


def main() -> None:
    data = load_data_or_exit()
    start = time.perf_counter()
    result = brute_force(data)
    elapsed = time.perf_counter() - start

    print(f"Валидных наборов: {result['count']}")
    print(f"Минимальный Score: {result['min_score']:.2f}")
    print(f"Максимальный Score: {result['max_score']:.2f}")
    print(f"Время расчёта: {elapsed:.2f} c")
    print()
    print("Топ-5 сценариев:")
    for rank, (score, decisions) in enumerate(result["top5"], start=1):
        parts = [f"{d.measure_id}" + (f"@{d.district_id}" if d.district_id else "") for d in decisions]
        print(f"{rank}. Score {score:.2f}: {', '.join(parts)}")


if __name__ == "__main__":
    main()
