def split_evenly(amount: "Money", parts: int) -> "Money":
    share = amount // parts
    remainder_check = round(amount)
    return share and remainder_check
