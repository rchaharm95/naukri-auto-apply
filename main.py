"""
Naukri Auto Apply — CLI entry point
Usage:
    python main.py                        # uses config.json in current dir
    python main.py --config my_cfg.json   # custom config file
    python main.py --dry-run              # search only, no apply
    python main.py --show-log             # print applied jobs log
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from naukri_bot import NaukriBot


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("naukri_bot.log", encoding="utf-8"),
        ],
    )


def load_config(path: str) -> dict:
    cfg_file = Path(path)
    if not cfg_file.exists():
        print(f"[ERROR] Config file not found: {cfg_file.resolve()}")
        sys.exit(1)
    with cfg_file.open(encoding="utf-8") as f:
        return json.load(f)


def validate_config(cfg: dict):
    email = cfg.get("credentials", {}).get("email", "")
    pwd = cfg.get("credentials", {}).get("password", "")
    if not email or email == "your_email@example.com":
        print("[ERROR] Set your Naukri email in config.json under credentials.email")
        sys.exit(1)
    if not pwd or pwd == "your_password":
        print("[ERROR] Set your Naukri password in config.json under credentials.password")
        sys.exit(1)
    search = cfg.get("search", {})
    keyword = search.get("keywords") or [search.get("keyword", "")]
    if not any(keyword):
        print("[ERROR] Set at least one keyword in config.json under search.keywords")
        sys.exit(1)


def show_log(log_file: str = "applied_jobs.json"):
    p = Path(log_file)
    if not p.exists():
        print("No applied jobs log found.")
        return
    data: dict = json.loads(p.read_text(encoding="utf-8"))
    if not data:
        print("Log is empty.")
        return

    statuses = {}
    for entry in data.values():
        s = entry.get("status", "unknown")
        statuses[s] = statuses.get(s, 0) + 1

    print(f"\n{'='*60}")
    print(f"Applied Jobs Log  ({len(data)} total entries)")
    print(f"{'='*60}")
    for status, count in sorted(statuses.items()):
        print(f"  {status:<30} {count}")
    print(f"{'='*60}")

    print("\nLast 10 entries:")
    recent = sorted(data.values(), key=lambda x: x.get("timestamp", ""), reverse=True)[:10]
    for item in recent:
        ts = item.get("timestamp", "")[:16]
        print(f"  [{ts}] {item['status']:<25} {item['title'][:35]:<35} @ {item['company']}")


async def dry_run(bot: NaukriBot):
    await bot.start()
    try:
        if not await bot.login():
            return
        jobs = await bot.search_jobs()
        print(f"\n{'='*60}")
        print(f"DRY RUN - {len(jobs)} jobs found (no applications sent)")
        print(f"{'='*60}")
        for i, job in enumerate(jobs, 1):
            print(f"  {i:>3}. {job['title'][:40]:<40} @ {job['company']}")
            print(f"       {job['url']}")
    finally:
        await bot.stop()


def main():
    parser = argparse.ArgumentParser(
        description="Naukri Auto Apply — automatically apply to jobs on Naukri.com"
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to config JSON file (default: config.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Search jobs but do NOT apply — just list results",
    )
    parser.add_argument(
        "--show-log",
        action="store_true",
        help="Display summary of previously applied jobs",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose/debug logging",
    )
    args = parser.parse_args()

    setup_logging(args.verbose)

    if args.show_log:
        show_log()
        return

    cfg = load_config(args.config)
    validate_config(cfg)

    kw_list = cfg['search'].get('keywords') or [cfg['search'].get('keyword', '')]
    kw_display = ', '.join(kw_list)[:28]
    gcc_flag = "YES" if cfg['search'].get('gcc_only') else "NO"
    print(f"""
+==========================================+
|        Naukri Auto Apply Bot             |
+==========================================+
|  Keywords : {kw_display:<28} |
|  Location : {cfg['search'].get('location', 'Any'):<28} |
|  Exp (yrs): {cfg['search'].get('experience', 0):<28} |
|  GCC Only : {gcc_flag:<28} |
|  Max Apply: {cfg['settings'].get('max_apply_per_run', 20):<28} |
|  Mode     : {'DRY RUN' if args.dry_run else 'LIVE APPLY':<28} |
+==========================================+
""")

    bot = NaukriBot(cfg)

    if args.dry_run:
        asyncio.run(dry_run(bot))
    else:
        asyncio.run(bot.run())


if __name__ == "__main__":
    main()
