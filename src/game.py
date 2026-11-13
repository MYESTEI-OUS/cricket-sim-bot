"""Cricket innings rules and randomized over-by-over delivery outcomes."""

import random

BATTING_STYLES = {
    "aggressive": {
        "label": "Aggressive",
        "outcomes": {"wicket": 12, "dot": 17, "one": 24, "two": 13, "four": 22, "six": 12},
    },
    "defensive": {
        "label": "Defensive",
        "outcomes": {"wicket": 4, "dot": 34, "one": 43, "two": 12, "four": 6, "six": 1},
    },
    "ultra": {
        "label": "Ultra aggressive",
        "outcomes": {"wicket": 22, "dot": 10, "one": 12, "two": 8, "four": 23, "six": 25},
    },
    "gaps": {
        "label": "Hit gaps",
        "outcomes": {"wicket": 6, "dot": 10, "one": 28, "two": 27, "four": 25, "six": 4},
    },
    "rotate": {
        "label": "Rotate strike",
        "outcomes": {"wicket": 4, "dot": 12, "one": 55, "two": 21, "four": 8, "six": 0},
    },
}

BOWLING_STYLES = {
    "aggressive": {
        "label": "Aggressive",
        "modifiers": {"wicket": 1.35, "dot": 0.9, "one": 0.95, "two": 0.9, "four": 0.92, "six": 0.9},
    },
    "defensive": {
        "label": "Defensive",
        "modifiers": {"wicket": 0.8, "dot": 1.3, "one": 1.1, "two": 1, "four": 0.8, "six": 0.65},
    },
    "variation": {
        "label": "Variation",
        "modifiers": {"wicket": 1.15, "dot": 1.05, "one": 0.95, "two": 0.95, "four": 0.9, "six": 0.8},
    },
    "knuckle": {
        "label": "Knuckle ball",
        "modifiers": {"wicket": 1.3, "dot": 1.1, "one": 1, "two": 0.95, "four": 0.85, "six": 0.75},
    },
}

RUNS = {"wicket": 0, "dot": 0, "one": 1, "two": 2, "four": 4, "six": 6}


def create_match(overs_limit: int) -> dict:
    if not isinstance(overs_limit, int) or isinstance(overs_limit, bool) or not 1 <= overs_limit <= 20:
        raise ValueError("Overs must be a whole number between 1 and 20.")
    return {"overs_limit": overs_limit, "balls": 0, "score": 0, "wickets": 0}


def is_match_over(match: dict) -> bool:
    return match["balls"] >= match["overs_limit"] * 6 or match["wickets"] >= 10


def format_overs(balls: int) -> str:
    return f"{balls // 6}.{balls % 6}"


def simulate_ball(
    match: dict,
    batting_style: str,
    bowling_style: str,
    pitch_modifiers: dict | None = None,
    rng=random.random,
) -> dict:
    if batting_style not in BATTING_STYLES:
        raise ValueError("Choose a valid batting style.")
    if bowling_style not in BOWLING_STYLES:
        raise ValueError("Choose a valid bowling style.")
    if is_match_over(match):
        raise ValueError("This innings is already over.")

    batting_weights = BATTING_STYLES[batting_style]["outcomes"]
    bowling_modifiers = BOWLING_STYLES[bowling_style]["modifiers"]
    pitch_modifiers = pitch_modifiers or {}
    weights = {
        outcome: weight * bowling_modifiers[outcome] * pitch_modifiers.get(outcome, 1)
        for outcome, weight in batting_weights.items()
    }
    draw = rng() * sum(weights.values())
    outcome = "dot"
    for candidate, weight in weights.items():
        draw -= weight
        if draw < 0:
            outcome = candidate
            break

    match["balls"] += 1
    match["score"] += RUNS[outcome]
    if outcome == "wicket":
        match["wickets"] += 1
    return {"outcome": outcome, "runs": RUNS[outcome], "over": format_overs(match["balls"])}


def simulate_over(
    match: dict,
    batting_style: str,
    bowling_style: str,
    pitch_modifiers: dict | None = None,
    rng=random.random,
) -> dict:
    if is_match_over(match):
        raise ValueError("This innings is already over.")

    over_number = (match["balls"] // 6) + 1
    balls_to_bowl = 6 - (match["balls"] % 6)
    deliveries = []
    over_runs = 0
    over_wickets = 0

    for _ in range(balls_to_bowl):
        if is_match_over(match):
            break
        result = simulate_ball(match, batting_style, bowling_style, pitch_modifiers, rng)
        deliveries.append(result)
        over_runs += result["runs"]
        if result["outcome"] == "wicket":
            over_wickets += 1

    return {
        "over_number": over_number,
        "deliveries": deliveries,
        "over_runs": over_runs,
        "over_wickets": over_wickets,
        "total_score": match["score"],
        "total_wickets": match["wickets"],
        "overs_completed": format_overs(match["balls"]),
    }


# --- Example Over-by-Over Simulation ---
if __name__ == "__main__":
    match = create_match(overs_limit=20)

    while not is_match_over(match):
        current_over = (match["balls"] // 6) + 1

        if current_over <= 6:
            bat_tactic, bowl_tactic = "aggressive", "aggressive"
        elif current_over <= 15:
            bat_tactic, bowl_tactic = "gaps", "variation"
        else:
            bat_tactic, bowl_tactic = "ultra", "knuckle"

        over_summary = simulate_over(match, bat_tactic, bowl_tactic)
        symbols = ["W" if d["outcome"] == "wicket" else str(d["runs"]) for d in over_summary["deliveries"]]

        print(
            f"Over {over_summary['over_number']:>2}: [{' '.join(symbols):<11}] "
            f"| Runs: {over_summary['over_runs']:>2}, Wkts: {over_summary['over_wickets']} "
            f"| Score: {over_summary['total_score']}/{over_summary['total_wickets']}"
        )
