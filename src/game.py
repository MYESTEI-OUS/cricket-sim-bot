"""Cricket match engine: ball-by-ball outcome simulation."""

import random

OUTCOMES = ("wicket", "dot", "one", "two", "four", "six")
OUTCOME_RUNS = {"wicket": 0, "dot": 0, "one": 1, "two": 2, "four": 4, "six": 6}

# Multipliers applied to the base outcome weights.
BATTING_STYLES = {
    "aggressive": {"label": "Aggressive", "mods": {"wicket": 1.25, "dot": 0.8, "one": 0.9, "two": 1.0, "four": 1.35, "six": 1.4}},
    "defensive": {"label": "Defensive", "mods": {"wicket": 0.6, "dot": 1.35, "one": 1.0, "two": 0.9, "four": 0.6, "six": 0.4}},
    "ultra": {"label": "Ultra aggressive", "mods": {"wicket": 1.7, "dot": 0.7, "one": 0.7, "two": 0.9, "four": 1.6, "six": 2.2}},
    "gaps": {"label": "Hit gaps", "mods": {"wicket": 0.9, "dot": 0.8, "one": 1.2, "two": 1.4, "four": 1.3, "six": 0.6}},
    "rotate": {"label": "Rotate strike", "mods": {"wicket": 0.8, "dot": 0.7, "one": 1.6, "two": 1.3, "four": 0.7, "six": 0.5}},
}
BOWLING_STYLES = {
    "aggressive": {"label": "Aggressive", "mods": {"wicket": 1.4, "dot": 1.0, "one": 0.9, "two": 0.9, "four": 1.15, "six": 1.1}},
    "defensive": {"label": "Defensive", "mods": {"wicket": 0.7, "dot": 1.3, "one": 1.1, "two": 0.9, "four": 0.8, "six": 0.7}},
    "variation": {"label": "Variation", "mods": {"wicket": 1.15, "dot": 1.15, "one": 1.0, "two": 1.0, "four": 0.95, "six": 0.95}},
    "knuckle": {"label": "Knuckle ball", "mods": {"wicket": 1.2, "dot": 1.25, "one": 1.0, "two": 0.95, "four": 0.9, "six": 0.85}},
}
BASE_WEIGHTS = {"wicket": 0.05, "dot": 0.30, "one": 0.30, "two": 0.07, "four": 0.15, "six": 0.07}


def format_overs(balls: int) -> str:
    return f"{balls // 6}.{balls % 6}"


def create_match(overs_limit: int) -> dict:
    if isinstance(overs_limit, bool) or not isinstance(overs_limit, int) or not 1 <= overs_limit <= 20:
        raise ValueError("Overs must be a whole number from 1 to 20.")
    return {"overs_limit": overs_limit, "balls": 0, "score": 0, "wickets": 0}


def is_match_over(match: dict) -> bool:
    return match["wickets"] >= 10 or match["balls"] >= match["overs_limit"] * 6


def simulate_ball(match, batting_style, bowling_style, pitch_modifiers=None, rng=random.random) -> dict:
    if batting_style not in BATTING_STYLES:
        raise ValueError(f"Unknown batting style: {batting_style}")
    if bowling_style not in BOWLING_STYLES:
        raise ValueError(f"Unknown bowling style: {bowling_style}")
    if is_match_over(match):
        raise ValueError("The innings is already over.")

    pitch_modifiers = pitch_modifiers or {}
    bat = BATTING_STYLES[batting_style]["mods"]
    bowl = BOWLING_STYLES[bowling_style]["mods"]
    weights = {
        outcome: BASE_WEIGHTS[outcome] * bat[outcome] * bowl[outcome] * pitch_modifiers.get(outcome, 1.0)
        for outcome in OUTCOMES
    }
    pick = rng() * sum(weights.values())
    outcome = OUTCOMES[-1]
    for name in OUTCOMES:  # wicket is first, so rng() == 0 always gives a wicket
        pick -= weights[name]
        if pick < 0:
            outcome = name
            break

    runs = OUTCOME_RUNS[outcome]
    match["balls"] += 1
    match["score"] += runs
    if outcome == "wicket":
        match["wickets"] += 1
    return {"outcome": outcome, "runs": runs, "over": format_overs(match["balls"])}
