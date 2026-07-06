import config
from scheduler import run

if __name__ == '__main__':
    # Refuses to boot on catastrophic env combos: QA_MODE pointed at the
    # production DB, or real trading without the explicit confirm var.
    config.assert_safe_boot()
    run()
