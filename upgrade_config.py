# Server-side validation helpers used by the upgrade system.
# Chance itself is calculated in database.py from source/target values.
RARITY_ORDER = {'common':0,'rare':1,'epic':2,'legendary':3,'mythic':4,'divine':5,'emerald':6}

def is_valid_target(name, rarity, value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return False
    if not str(name or '').strip() or value < 1:
        return False
    rarity = str(rarity or '').strip().lower()
    return rarity in RARITY_ORDER

def upgrade_chance(source_value, target_value, stake=0):
    source_value = max(0, int(source_value or 0))
    target_value = max(1, int(target_value or 1))
    stake = max(0, int(stake or 0))
    return max(0.0, min(95.0, ((source_value + stake) / target_value) * 100.0))


def upgrade_effective_chance(base_chance, fail_streak=0):
    """Apply soft-pity after consecutive failed upgrades.

    High-probability upgrades should not repeatedly brick because of an unlucky
    streak. The first failure gives +10 percentage points, then another +10
    for each consecutive failure, capped at 99.5%. This is deliberately
    server-side so the displayed result and the actual roll use the same value.
    """
    base_chance = max(0.0, min(95.0, float(base_chance or 0)))
    fail_streak = max(0, int(fail_streak or 0))
    if base_chance < 50.0 or fail_streak <= 0:
        return base_chance
    return min(99.5, base_chance + (10.0 * fail_streak))
