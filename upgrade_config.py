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
