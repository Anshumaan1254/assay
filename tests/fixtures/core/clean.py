from core.money import Money


def add_fee(total: Money, fee: Money) -> Money:
    combined = total.add(fee)
    return combined


def scale(total: Money, count: int) -> Money:
    return total.multiply(count)
