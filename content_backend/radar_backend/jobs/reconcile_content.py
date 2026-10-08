"""CLI for the bounded catalog reconciliation job."""
import argparse
from pathlib import Path
from radar_backend.bootstrap import reconcile as production_reconcile

def main(*, reconcile=production_reconcile):
    parser = argparse.ArgumentParser()
    parser.add_argument('--master', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--max-enrich', type=int, default=60)
    parser.add_argument('--max-seconds', type=int, default=2400)
    parser.add_argument('--cache-dir', type=Path)
    args = parser.parse_args()
    if args.max_enrich < 0 or args.max_seconds <= 0:
        raise SystemExit('Invalid batch limits')
    reconcile(args.master, args.data_dir, args.max_enrich, max_seconds=args.max_seconds, cache_dir=args.cache_dir)


if __name__ == "__main__":
    main()
