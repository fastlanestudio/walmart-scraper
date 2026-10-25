#!/usr/bin/env python3
"""
run.py — Walmart scraper CLI entrypoint.

Usage:
    python run.py --zipcode 10001 --csv Walmart_Category_URLs.csv [options]

Options:
    --zipcode       Store zip code (required)
    --csv           Path to Walmart_Category_URLs.csv (required)
    --db            SQLite database path (default: data/walmart.db)
    --checkpoint    Checkpoint file path (default: data/checkpoint.json)
    --log-dir       Log directory (default: logs/)
    --mode          'live' or 'simulated' (default: live)
    --canary-url    Category URL to use as canary for silent-block detection
    --canary-min    Minimum expected items from canary (default: 50)
    --proxy-host    Proxy host (overrides PROXY_HOST env)
    --proxy-port    Proxy port (overrides PROXY_PORT env)
    --proxy-user    Proxy username (overrides PROXY_USER env)
    --proxy-pass    Proxy password (overrides PROXY_PASS env)
"""
import argparse
import asyncio
import logging
import sys
import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(description="Walmart.com production scraper")
    parser.add_argument("--serve", action="store_true", help="Launch localhost web interface")
    parser.add_argument("--port", type=int, default=8000, help="Localhost server port (default: 8000)")
    parser.add_argument("--zipcode", default="10001", help="Store zip code (required for CLI crawl)")
    parser.add_argument("--csv", default="Walmart_Category_URLs.csv", help="Path to Walmart_Category_URLs.csv")
    parser.add_argument("--db", default="data/walmart.db", help="SQLite DB path")
    parser.add_argument(
        "--checkpoint", default="data/checkpoint.json", help="Checkpoint file path"
    )
    parser.add_argument("--log-dir", default="logs", help="Log directory")
    parser.add_argument("--mode", choices=["live", "simulated"], default="live")
    parser.add_argument("--canary-url", default="", help="Canary category URL")
    parser.add_argument(
        "--canary-min", type=int, default=50, help="Canary minimum expected items"
    )
    parser.add_argument("--proxy-host", default="", help="Proxy host (overrides PROXY_HOST env)")
    parser.add_argument(
        "--proxy-port", type=int, default=0, help="Proxy port (overrides PROXY_PORT env)"
    )
    parser.add_argument(
        "--proxy-user", default="", help="Proxy username (overrides PROXY_USER env)"
    )
    parser.add_argument(
        "--proxy-pass", default="", help="Proxy password (overrides PROXY_PASS env)"
    )
    return parser.parse_args()


async def main(args):
    from core.logging_setup import setup_logging
    from core.checkpoint import RunCheckpoint, CategoryCheckpoint, CategoryStatus
    from core.store import StoreDB
    from core.base_scraper import CanaryConfig
    from core.schema import validate_batch
    from walmart.config import load_category_urls, load_proxy_from_env, ProxyConfig
    from walmart.scraper import WalmartScraper

    # ── Paths ─────────────────────────────────────────────────────────────────
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    Path(args.checkpoint).parent.mkdir(parents=True, exist_ok=True)
    Path(args.log_dir).mkdir(parents=True, exist_ok=True)

    run_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + args.zipcode

    # ── Logging ───────────────────────────────────────────────────────────────
    log_path = setup_logging(args.log_dir, run_id, args.mode)

    logger.info("Run started", extra={
        "run_id": run_id,
        "mode": args.mode,
        "store_zipcode": args.zipcode,
    })

    # ── Categories ────────────────────────────────────────────────────────────
    categories = load_category_urls(args.csv)
    logger.info(f"Loaded {len(categories)} categories from CSV")

    # ── Proxy ─────────────────────────────────────────────────────────────────
    proxy_cfg = load_proxy_from_env()
    if args.proxy_host:
        proxy_cfg = ProxyConfig(
            host=args.proxy_host,
            port=args.proxy_port,
            username=args.proxy_user,
            password=args.proxy_pass,
        )

    proxy_playwright = proxy_cfg.to_playwright_proxy() if proxy_cfg else None

    # ── Store DB ──────────────────────────────────────────────────────────────
    db = StoreDB(args.db)
    db.begin_run(run_id, args.mode, args.zipcode)

    # ── Checkpoint ────────────────────────────────────────────────────────────
    cat_entries = [
        CategoryCheckpoint(c.category, c.sub_category, c.url)
        for c in categories
    ]
    checkpoint = RunCheckpoint.load_or_create(
        args.checkpoint, run_id, args.zipcode, cat_entries
    )

    pending = checkpoint.pending_categories()
    logger.info(
        f"Resuming: {len(pending)} categories pending, "
        f"{len(checkpoint.categories) - len(pending)} already done"
    )

    # ── Scraper ───────────────────────────────────────────────────────────────
    async with WalmartScraper(proxy_config=proxy_playwright, mode=args.mode) as scraper:

        # Set canary
        if args.canary_url:
            scraper.canary = CanaryConfig(
                category_url=args.canary_url,
                min_expected_items=args.canary_min,
            )

        # ── Store selection ───────────────────────────────────────────────────
        try:
            store_info = await scraper.select_store(args.zipcode)
            logger.info("Store selected", extra={
                "run_id": run_id,
                "store_name": store_info.get("store_name"),
                "store_location": store_info.get("store_location"),
            })
        except NotImplementedError:
            logger.warning("select_store() not yet implemented — store_info will be empty")
            store_info = {"store_name": "", "store_location": "", "store_logo": ""}

        # ── Canary check ──────────────────────────────────────────────────────
        if scraper.canary:
            try:
                await scraper.check_canary()
                logger.info("Canary check passed", extra={"run_id": run_id, "block_detected": False})
            except Exception as e:
                logger.critical(f"CANARY ALERT: {e}", extra={
                    "run_id": run_id, "block_detected": True
                })
                sys.exit(1)

        # ── Category loop ─────────────────────────────────────────────────────
        for cat in pending:
            checkpoint.mark_in_progress(cat.category, cat.sub_category)
            checkpoint.save(args.checkpoint)

            logger.info("Scraping category", extra={
                "run_id": run_id,
                "category": cat.category,
                "sub_category": cat.sub_category,
            })

            products_this_cat = 0
            try:
                async for product_url in scraper.iter_category_product_urls(cat.url):
                    try:
                        record = await scraper.scrape_product(
                            product_url, store_info,
                            cat.category, cat.sub_category, args.zipcode
                        )
                        if record:
                            db.upsert_product(
                                db._product_key(record.to_dict()),
                                run_id,
                                record.to_dict()
                            )
                            products_this_cat += 1
                            scraper.stats.products_scraped += 1
                    except Exception as e:
                        logger.warning(f"Product scrape error: {e}", extra={
                            "product_url": product_url, "run_id": run_id
                        })
                        scraper.stats.errors.append((product_url, str(e)))

                checkpoint.mark_done(cat.category, cat.sub_category, products_this_cat)
                checkpoint.save(args.checkpoint)
                scraper.stats.categories_done += 1
                logger.info("Category done", extra={
                    "run_id": run_id,
                    "category": cat.category,
                    "sub_category": cat.sub_category,
                    "products_scraped": products_this_cat,
                })

            except NotImplementedError:
                logger.warning(
                    f"Skipping category — scraper not yet implemented: {cat.url}"
                )
                checkpoint.mark_failed(cat.category, cat.sub_category, "NotImplementedError")
                checkpoint.save(args.checkpoint)
            except Exception as e:
                logger.error(f"Category failed: {e}", extra={
                    "run_id": run_id,
                    "category": cat.category,
                    "sub_category": cat.sub_category,
                })
                checkpoint.mark_failed(cat.category, cat.sub_category, str(e))
                checkpoint.save(args.checkpoint)
                scraper.stats.categories_failed += 1

        # ── Post-run merge ────────────────────────────────────────────────────
        db.mark_unseen(run_id)
        db.finish_run(run_id)

        logger.info("Run complete", extra={
            "run_id": run_id,
            "mode": args.mode,
            "products_scraped": scraper.stats.products_scraped,
            "products_excluded_approx_weight": scraper.stats.products_excluded_approx_weight,
            "products_failed_validation": scraper.stats.products_failed_validation,
            "categories_done": scraper.stats.categories_done,
            "categories_failed": scraper.stats.categories_failed,
        })


if __name__ == "__main__":
    args = parse_args()
    if args.serve:
        from app import create_app
        from aiohttp import web
        app = create_app(args.db)
        print("================================================================")
        print(f"  Walmart Scraper Dashboard running at: http://localhost:{args.port}")
        print(f"  Database: {args.db}")
        print("================================================================")
        web.run_app(app, host="127.0.0.1", port=args.port)
    else:
        asyncio.run(main(args))
