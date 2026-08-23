import anthropic
import openai
from llm.provider import LLMProvider
from datagen.generator import plant_discrepancy
import my_llm_helper


def compute_fee(total: "Money", rate: float) -> "Money":
    mixed = total + rate
    with_literal = total + float(2)
    with_call = total + 1.5
    halved = total / 2
    floored = total // 2
    rounded = round(total)
    return total
