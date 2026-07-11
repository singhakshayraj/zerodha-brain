import os

# One image, two Railway services: the brain (default) and the external
# watchdog that alerts when the brain dies. SERVICE_ROLE=watchdog on the
# watchdog service selects the role; everything else boots the brain.
if __name__ == '__main__':
    if os.getenv('SERVICE_ROLE') == 'watchdog':
        import watchdog
        watchdog.main()
    else:
        import config
        from scheduler import run
        # Refuses to boot on catastrophic env combos: QA_MODE pointed at the
        # production DB, or real trading without the explicit confirm var.
        config.assert_safe_boot()
        # One-off news backfill on boot when NEWS_BACKFILL_WINDOW is set — lets
        # a historical fill run without shell access (set the var + restart).
        try:
            import news_jobs
            news_jobs.run_backfill_from_env()
        except Exception as e:
            print(f"[main] news backfill hook failed (non-fatal): {e}")
        run()
