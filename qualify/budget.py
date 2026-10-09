"""check_budget_fit — gate 4 (budget band), used only when a caller VOLUNTEERS a number.

This is the ONLY place in the system that reads pricing figures. The output is a label
("aligned" / "misaligned" / "unclear"), never a number, so nothing here can reach the caller.

Floors are the LOW end of context/pricing.md. "Clearly below" = under half of that floor.
PROPOSED threshold — confirm with Nikhil.
"""

# context/pricing.md — low end of each band (INR)
SINGLE_ROOM_FLOOR_INR = 350_000          # "Single room redesign (all-in): ₹3.5 lakh – ..."
RESIDENTIAL_PER_SQFT_FLOOR_INR = 1_800   # "Standard specification ₹1,800 – ₹2,400 per sq ft"
COMMERCIAL_PER_SQFT_FLOOR_INR = 1_200    # "Basic fitout ₹1,200 – ₹1,800 per sq ft"

CLEARLY_BELOW_RATIO = 0.5


def scope_floor_inr(property_category: str, scope_unit: str, rooms: list[str], size_sqft: float | None) -> float | None:
    if property_category == "office_clinic_studio" and size_sqft:
        return size_sqft * COMMERCIAL_PER_SQFT_FLOOR_INR
    if scope_unit == "full_home" and size_sqft:
        return size_sqft * RESIDENTIAL_PER_SQFT_FLOOR_INR
    if rooms:
        return len(rooms) * SINGLE_ROOM_FLOOR_INR
    return None


def check_budget_fit(budget_max_inr: float | None, property_category: str, scope_unit: str,
                     rooms: list[str], size_sqft: float | None) -> str:
    if not budget_max_inr:
        return "unclear"
    floor = scope_floor_inr(property_category, scope_unit, rooms, size_sqft)
    if floor is None:
        return "unclear"
    return "misaligned" if budget_max_inr < CLEARLY_BELOW_RATIO * floor else "aligned"
