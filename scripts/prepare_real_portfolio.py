"""Download and review the supported real bank/window pool."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.portfolio_real_data import prepare_real_data


def main():
    parser=argparse.ArgumentParser(description="接入组合训练真实日线及核验材料")
    parser.add_argument("--data-dir",required=True)
    args=parser.parse_args()
    print(json.dumps(prepare_real_data(args.data_dir),ensure_ascii=False,indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout,"reconfigure"):sys.stdout.reconfigure(encoding="utf-8")
    main()
