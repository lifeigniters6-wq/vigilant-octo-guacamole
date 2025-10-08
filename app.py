import os
import json
import logging
import requests
from datetime import datetime
from collections import deque
from flask import Flask, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

# ==================== CONFIG ====================
API_1M = "https://wingo-unified-api.gt.tc//api/wingo.php?type=1min"
HISTORY_FILE = "history.json"
CHECK_INTERVAL = 60

# ==================== STATE ====================
state = {
    "trends": deque(maxlen=1000),
    "predictions": deque(maxlen=200)
}

# ==================== SAFE FILE HANDLING ====================
def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r') as f:
                data = json.load(f)
                state["trends"] = deque(data.get("trends", []), maxlen=1000)
                state["predictions"] = deque(data.get("predictions", []), maxlen=200)
            logging.info(f"Loaded {len(state['trends'])} trends from disk.")
        except Exception as e:
            logging.warning(f"History file corrupted or invalid. Starting fresh. Error: {e}")
            state["trends"] = deque(maxlen=1000)
            state["predictions"] = deque(maxlen=200)

def save_history():
    try:
        with open(HISTORY_FILE, 'w') as f:
            json.dump({
                "trends": list(state["trends"]),
                "predictions": list(state["predictions"])
            }, f)
    except Exception as e:
        logging.error(f"Failed to save history: {e}")

# ==================== HELPERS ====================
def get_bs(num):
    return 'B' if num >= 5 else 'S'

def detect_alternating(seq):
    if len(seq) < 3:
        return None
    for i in range(1, len(seq)):
        if seq[i] == seq[i-1]:
            return None
    return 'S' if seq[-1] == 'B' else 'B'

def count_streak(seq):
    if not seq:
        return {'value': None, 'count': 0}
    last = seq[-1]
    count = 1
    for i in range(len(seq)-2, -1, -1):
        if seq[i] == last:
            count += 1
        else:
            break
    return {'value': last, 'count': count}

def has_two_consecutive_losses(predictions):
    recent = [p for p in predictions if p.get('result') != 'P'][:2]
    return len(recent) == 2 and all(p['result'] == 'Lose' for p in recent)

# ==================== FULL PATTERN LIST ====================
STREAK_BREAK_PATTERNS = [
    "BBBS", "BBSB", "BSBB", "SBBB", "SSSB", "SSBS", "SBSS", "BSSS",
    "BBSS", "BSSB", "SSBB", "SBBS", "BSBS", "SBSB", "BBBB", "SSSS",
    "BBBBBS", "BBBBSB", "BBBSBB", "BBSBBB", "BSBBBB", "SBBBBB",
    "SSSSSB", "SSSSBS", "SSSBSS", "SSBSSS", "SBSSSS", "BSSSSS",
    "BBBSSB", "BBSSBB", "BSSBBB", "SSBBBS", "SBBBSS", "BBBSSS",
    "BBSSSB", "BSSSBB", "SSSBBB", "SSBBBS", "SBBBSS", "BBSSBS",
    "BSSBBS", "SSBBSS", "SBBSSB", "BBSSBB", "BSSBSS", "SSBBSB",
    "BBSBSB", "SBBSBB", "BSBSBS", "SBSBSB", "BBSSBB", "SSBBSS",
    "BSSBSS", "SBBSSB", "BBSSBS", "SBSSBB", "BSSBBB", "SBBSBS",
    "SSSBBS", "BBSSSB", "SBBBSB", "BBBBBBB", "SSSSSSS", "BBBBBBS", "SSSSSSB",
    "BBBBSBB", "SSSBSSS", "BBBSBBB", "BBSBBBB", "BSBBBBB", "SSBBSSB", "SBSSBBS", "SSBBSBS",
    "BBSBBSB", "SBSBSBS", "BSSBSSB", "SSBBSSS", "BBSSSSS", "SSBBBSS", "BSSBBBS", "SSBSSBB",
    "BBBBBB", "SSSSSS", "BBBSSB", "SSBBSS", "BBSBSB", "SBBSBB", "BSBSBS", "SBSBSB",
    "BBSSBB", "SSBBSS", "BSSBSS", "SBBSSB", "BBSSBS", "SBSSBB", "BSSBBB", "SBBSBS",
    "SSSBBS", "BBSSSB", "SBBBSB", "BBSSB", "SSBBS", "BSBBS", "SBSBB",
    "BBBSS", "SSSBB", "SBSSB", "BSSSB", "SBBSB", "SSBSS"
]

# ==================== DRAGON-X ENGINE ====================
def dragonx_engine(recent_numbers, recent_bs, predictions):
    if len(recent_numbers) < 10:
        avg = sum(recent_numbers) / len(recent_numbers) if recent_numbers else 5
        return {
            'bs': 'B' if avg >= 5 else 'S',
            'num': 5,
            'confidence': 55,
            'logic': 'STATISTICAL_FALLBACK',
            'bias': 'NEUTRAL'
        }

    natural_order = list(reversed(recent_bs))
    streak = count_streak(natural_order)

    # 🔥 DRAGON RISK SKIP
    if streak['count'] >= 6:
        recent_losses = [p for p in predictions if p.get('result') == 'Lose'][:3]
        flip_losses = sum(
            1 for p in recent_losses
            if (streak['value'] == 'B' and p['bs'] == 'S') or
               (streak['value'] == 'S' and p['bs'] == 'B')
        )
        if flip_losses >= 2:
            return {
                'bs': 'SKIP',
                'num': '-',
                'confidence': 0,
                'logic': '🔥 HIGH-RISK DRAGON DETECTED! SKIP ZONE! 🔥',
                'bias': 'DRAGON_RISK'
            }

    # === BUILD PATTERN MAPS (5 to 10 digits) ===
    maps = []
    for length in range(10, 4, -1):
        pattern_map = {}
        for p in STREAK_BREAK_PATTERNS:
            if len(p) == length:
                pattern_map[p] = 'S' if p[0] == 'B' else 'B'
        maps.append((length, pattern_map, f"{length}-digit"))

    prediction = None
    used_pattern = None
    for length, pattern_map, name in maps:
        if len(natural_order) >= length:
            key = ''.join(natural_order[-length:])
            if key in pattern_map:
                prediction = pattern_map[key]
                used_pattern = f"{name} MATCH: {key} → {prediction}"
                break

    # === FALLBACK LOGIC ===
    if not prediction:
        if 4 <= streak['count'] < 6:
            prediction = 'S' if streak['value'] == 'B' else 'B'
            used_pattern = f"SHORT_STREAK_REVERSAL ({streak['count']}x)"
        else:
            alt = detect_alternating(natural_order[-10:])
            if alt:
                prediction = alt
                used_pattern = "ALTERNATING"
            else:
                window = natural_order[-30:]
                big_ratio = window.count('B') / len(window) if window else 0.5
                if big_ratio >= 0.72:
                    prediction = 'B'
                    used_pattern = f"BIG DOMINANCE ({round(big_ratio*100)}%)"
                elif big_ratio <= 0.28:
                    prediction = 'S'
                    used_pattern = f"SMALL DOMINANCE ({round((1-big_ratio)*100)}%)"
                else:
                    prediction = natural_order[-1]
                    used_pattern = "MOMENTUM"

    # === CONFIDENCE ===
    base_conf = 60
    if "10-digit" in used_pattern: base_conf = 98
    elif "9-digit" in used_pattern: base_conf = 97
    elif "8-digit" in used_pattern: base_conf = 96
    elif "7-digit" in used_pattern: base_conf = 94
    elif "6-digit" in used_pattern: base_conf = 90
    elif "DOMINANCE" in used_pattern: base_conf = 85
    elif "MOMENTUM" in used_pattern: base_conf = 75
    elif "SHORT_STREAK" in used_pattern: base_conf = 80
    elif "ALTERNATING" in used_pattern: base_conf = 82

    recent_preds = [p for p in predictions if p.get('result') != 'P'][:10]
    loss_rate = sum(1 for p in recent_preds if p.get('result') == 'Lose') / len(recent_preds) if recent_preds else 0
    if loss_rate > 0.6:
        base_conf = max(55, base_conf - 15)
    confidence = min(98, max(55, base_conf))

    # === SMART NUMBER SELECTION ===
    pool = list(range(5, 10)) if prediction == 'B' else list(range(0, 5))
    counts = [0]*10
    for n in recent_numbers[-60:]:
        counts[n] += 1

    last_10_nums = recent_numbers[-10:]
    even_bias = sum(1 for n in last_10_nums if n % 2 == 0) > 5

    weighted = []
    for n in pool:
        score = 1 + counts[n]
        if n in last_10_nums:
            score += 1.2
        if (even_bias and n % 2 == 0) or (not even_bias and n % 2 == 1):
            score += 0.7
        weighted.append((n, score))

    if predictions and predictions[0].get('result') == 'Lose':
        last_num = predictions[0]['num']
        weighted = [(n, s) for n, s in weighted if n != last_num]

    weighted.sort(key=lambda x: x[1], reverse=True)
    final_num = weighted[0][0] if weighted else pool[0]

    # === BIAS ===
    bias = "NEUTRAL"
    if "DOMINANCE" in used_pattern or "MOMENTUM" in used_pattern:
        bias = "MOMENTUM_BIAS"
    elif "STREAK" in used_pattern:
        bias = "REVERSAL_BIAS"
    elif "digit" in used_pattern:
        bias = "PATTERN_BIAS"
    elif "ALTERNATING" in used_pattern:
        bias = "CHOP_BIAS"

    return {
        'bs': prediction,
        'num': final_num,
        'confidence': confidence,
        'logic': used_pattern,
        'bias': bias
    }

# ==================== MAIN JOB ====================
def prediction_job():
    global state
    try:
        res = requests.get(API_1M, timeout=10)
        res.raise_for_status()
        data = res.json()

        if not isinstance(data, list) or len(data) == 0:
            raise ValueError("Invalid 1-minute API response")

        latest = data[0]['content']
        period = latest['issueNumber']
        number = latest['number']
        bs = get_bs(number)

        state["trends"].appendleft({"period": period, "num": number, "bs": bs})

        if state["predictions"]:
            last_pred = state["predictions"][0]
            if last_pred["period"] == period:
                if last_pred["bs"] == bs and last_pred["num"] == number:
                    last_pred["result"] = "Jackpot"
                elif last_pred["bs"] == bs:
                    last_pred["result"] = "Win"
                else:
                    last_pred["result"] = "Lose"

        next_period = str(int(period) + 1)
        if not any(p["period"] == next_period for p in state["predictions"]):
            recent_nums = [t["num"] for t in list(state["trends"])]
            recent_bs = [t["bs"] for t in list(state["trends"])]

            pred = dragonx_engine(recent_nums, recent_bs, list(state["predictions"]))

            state["predictions"].appendleft({
                "period": next_period,
                "bs": pred["bs"],
                "num": pred["num"],
                "confidence": pred["confidence"],
                "logic": pred["logic"],
                "bias": pred["bias"],
                "result": "P"
            })

        save_history()

    except Exception as e:
        logging.error(f"Prediction job failed: {e}")

# ==================== FLASK APP ====================
app = Flask(__name__)

@app.route('/health')
def health():
    return {
        "status": "alive",
        "mode": "1-minute",
        "trends_count": len(state["trends"]),
        "predictions_count": len(state["predictions"]),
        "last_update": datetime.utcnow().isoformat()
    }

@app.route('/ping')
def ping():
    """Keep-alive endpoint for cron-job.org"""
    return {"status": "alive", "message": "DRAGON-X AI is running!"}

@app.route('/predict')
def get_prediction():
    if not state["predictions"]:
        return jsonify({"error": "No prediction yet. Wait for first cycle."}), 404
    return jsonify(state["predictions"][0])

@app.route('/history')
def get_history():
    return jsonify({
        "trends": list(state["trends"])[:50],
        "predictions": list(state["predictions"])[:50]
    })

# ==================== BOOTSTRAP ====================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    load_history()

    # ⚡ Run first prediction immediately
    logging.info("Running initial prediction job...")
    prediction_job()

    # 🔁 Start background scheduler for continuous updates
    scheduler = BackgroundScheduler()
    scheduler.add_job(prediction_job, 'interval', seconds=CHECK_INTERVAL)
    scheduler.start()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
