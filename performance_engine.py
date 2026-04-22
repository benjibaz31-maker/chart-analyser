"""
performance_engine.py
─────────────────────
Moteur de performance pour ChartAnalyzer.
3 fonctionnalités :
  1. update_results()  — vérifie si TP ou SL a été touché sur les trades en attente
  2. mini_backtest()   — avant de trader, vérifie l'historique similaire
  3. analyze_losses()  — identifie les patterns perdants + recommande MIN_SCORE optimal
  4. run()             — appelé depuis main() à chaque scan

Auteur : ChartAnalyzer
"""

import json, base64
import traceback
from datetime import datetime, timezone, timedelta

import requests
import yfinance as yf
import pandas as pd


# ─────────────────────────────────────────────────────────────
#  1. HISTORIQUE GITHUB
# ─────────────────────────────────────────────────────────────

def _read_history(gh_token, gh_repo):
    """Lit signals_history.json depuis GitHub. Retourne (list, sha)."""
    if not gh_token or not gh_repo:
        return [], None
    url  = f"https://api.github.com/repos/{gh_repo}/contents/signals_history.json"
    hdrs = {"Authorization": f"token {gh_token}",
            "Accept":        "application/vnd.github.v3+json"}
    try:
        r = requests.get(url, headers=hdrs, timeout=10)
        if r.status_code == 200:
            d = r.json()
            return json.loads(base64.b64decode(d["content"]).decode()), d["sha"]
        if r.status_code == 404:
            return [], None          # Fichier pas encore créé
    except Exception as e:
        print(f"    [perf] Lecture historique : {e}")
    return [], None


def _write_history(history, sha, gh_token, gh_repo, label="update"):
    """Écrit signals_history.json sur GitHub."""
    if not gh_token or not gh_repo:
        return
    url  = f"https://api.github.com/repos/{gh_repo}/contents/signals_history.json"
    hdrs = {"Authorization": f"token {gh_token}",
            "Accept":        "application/vnd.github.v3+json"}
    # Garder seulement les 300 derniers pour limiter la taille
    to_save = history[-300:]
    content = base64.b64encode(json.dumps(to_save, indent=2, ensure_ascii=False).encode()).decode()
    body = {
        "message": f"[perf] {label} {datetime.now(timezone.utc).strftime('%H:%M')} UTC",
        "content": content
    }
    if sha:
        body["sha"] = sha
    try:
        r = requests.put(url, headers=hdrs, json=body, timeout=15)
        if r.status_code in (200, 201):
            print(f"    [perf] Historique sauvegardé ({len(to_save)} trades)")
        else:
            print(f"    [perf] Erreur écriture : {r.status_code}")
    except Exception as e:
        print(f"    [perf] Exception écriture : {e}")


def _write_performance_json(perf, gh_token, gh_repo):
    """Écrit performance.json sur GitHub pour le dashboard."""
    if not gh_token or not gh_repo:
        return
    url  = f"https://api.github.com/repos/{gh_repo}/contents/performance.json"
    hdrs = {"Authorization": f"token {gh_token}",
            "Accept":        "application/vnd.github.v3+json"}
    sha  = None
    try:
        r = requests.get(url, headers=hdrs, timeout=10)
        if r.status_code == 200:
            sha = r.json().get("sha")
    except Exception:
        pass

    perf["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    content = base64.b64encode(json.dumps(perf, indent=2, ensure_ascii=False).encode()).decode()
    body = {"message": f"[perf] performance.json {perf['updated_at']}", "content": content}
    if sha:
        body["sha"] = sha
    try:
        requests.put(url, headers=hdrs, json=body, timeout=15)
        print("    [perf] performance.json mis à jour")
    except Exception as e:
        print(f"    [perf] Exception performance.json : {e}")


# ─────────────────────────────────────────────────────────────
#  2. MISE À JOUR DES RÉSULTATS (TP / SL touché ?)
# ─────────────────────────────────────────────────────────────

def _fetch_candles_for_trade(pair_mt5, created_at_str):
    """
    Télécharge les bougies 15m sur les 8h suivant le signal.
    Retourne un DataFrame ou None.
    """
    try:
        created = datetime.fromisoformat(created_at_str.replace("Z","")).replace(tzinfo=timezone.utc)
        end_dt  = created + timedelta(hours=8)

        # Pas encore 8h de passées → on ne vérifie pas encore
        if end_dt > datetime.now(timezone.utc):
            return None

        # Choisir le ticker Yahoo Finance
        if pair_mt5.startswith("XAU"):
            ticker = "GC=F"   # Or en USD
        elif pair_mt5.startswith("XAG"):
            ticker = "SI=F"   # Argent
        else:
            ticker = pair_mt5[:3] + pair_mt5[3:] + "=X"

        start_str = (created - timedelta(hours=1)).strftime("%Y-%m-%d")
        end_str   = (end_dt  + timedelta(days=1)).strftime("%Y-%m-%d")

        df = yf.download(ticker, start=start_str, end=end_str,
                         interval="15m", auto_adjust=True, progress=False)

        if df is None or df.empty:
            return None

        # Aplatir MultiIndex si présent
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # Normaliser les noms de colonnes en minuscules
        df.columns = [c.lower() for c in df.columns]
        df.index   = pd.to_datetime(df.index, utc=True)

        # Filtrer sur la période du trade
        mask = (df.index >= created) & (df.index <= end_dt)
        df   = df[mask]

        return df if not df.empty else None

    except Exception as e:
        print(f"    [perf] fetch_candles {pair_mt5} : {e}")
        return None


def update_results(history):
    """
    Parcourt l'historique et met à jour les trades 'pending'
    en vérifiant si TP ou SL a été touché sur les données réelles.
    Retourne le nombre de trades mis à jour.
    """
    updated = 0
    for trade in history:
        # Ignorer les trades déjà résolus
        if trade.get("result") in ("TP", "SL"):
            continue

        try:
            pair_mt5    = (trade.get("pair") or "XAUUSD").replace("/", "")
            action      = trade.get("signal", "BUY")
            entry       = float(trade.get("entry") or 0)
            sl_val      = float(trade.get("sl")    or 0)
            tp_val      = float(trade.get("tp")    or 0)
            created_str = (trade.get("created_at")
                           or trade.get("date","2024-01-01") + "T"
                           + trade.get("time","00:00") + ":00Z")

            # Données invalides → on passe
            if entry == 0 or sl_val == 0 or tp_val == 0:
                continue

            df = _fetch_candles_for_trade(pair_mt5, created_str)
            if df is None:
                continue

            result = "OPEN"
            for _, row in df.iterrows():
                h = float(row.get("high", 0) or 0)
                l = float(row.get("low",  0) or 0)
                if h == 0 or l == 0:
                    continue

                if action == "BUY":
                    if l <= sl_val:
                        result = "SL"
                        break
                    if h >= tp_val:
                        result = "TP"
                        break
                else:  # SELL
                    if h >= sl_val:
                        result = "SL"
                        break
                    if l <= tp_val:
                        result = "TP"
                        break

            if result in ("TP", "SL"):
                trade["result"]          = result
                trade["result_checked"]  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                updated += 1
                icon = "✅" if result == "TP" else "❌"
                print(f"    [perf] {icon} {action} {pair_mt5} → {result}")

        except Exception as e:
            print(f"    [perf] update_results trade : {e}")

    return updated


# ─────────────────────────────────────────────────────────────
#  3. MINI BACKTEST (avant de trader)
# ─────────────────────────────────────────────────────────────

def mini_backtest(pair, action, score_h1, history, lookback=15):
    """
    Avant de passer un ordre, vérifie l'historique des signaux similaires.

    Critères de similarité :
      - Même paire
      - Même direction (BUY/SELL)
      - Score H1 dans un range de ±20 points
      - Résultat connu (TP ou SL)

    Retourne : (win_rate: int|None, nb_trades: int, reco: str)
      reco = 'GO'        → win rate ≥ 60 % → tradings conseillé
      reco = 'CAUTION'   → win rate 45-59 % → prudence, lot 50 %
      reco = 'SKIP'      → win rate < 45 % → passer son tour
      reco = 'NO_DATA'   → moins de 3 trades similaires
    """
    pair_clean = pair.replace("/", "")
    resolved   = [t for t in history
                  if t.get("result") in ("TP", "SL")]

    similar = [
        t for t in resolved
        if (t.get("pair","").replace("/","")) == pair_clean
        and t.get("signal") == action
        and abs(int(t.get("score_h1") or 0) - score_h1) <= 20
    ][-lookback:]

    nb = len(similar)
    if nb < 3:
        return None, nb, "NO_DATA"

    tps = sum(1 for t in similar if t.get("result") == "TP")
    wr  = round(tps / nb * 100)

    if   wr >= 60: reco = "GO"
    elif wr >= 45: reco = "CAUTION"
    else:          reco = "SKIP"

    return wr, nb, reco


# ─────────────────────────────────────────────────────────────
#  4. ANALYSE DES PERTES + RECOMMANDATIONS
# ─────────────────────────────────────────────────────────────

def analyze_losses(history):
    """
    Analyse les patterns perdants pour améliorer la stratégie.
    Retourne un dict d'insights exploitables.
    """
    from collections import Counter

    resolved = [t for t in history if t.get("result") in ("TP", "SL")]
    tps = [t for t in resolved if t.get("result") == "TP"]
    sls = [t for t in resolved if t.get("result") == "SL"]

    if not resolved:
        return {}

    insights = {
        "total_resolved": len(resolved),
        "tp": len(tps),
        "sl": len(sls),
        "win_rate": round(len(tps) / len(resolved) * 100) if resolved else 0
    }

    # ── Score moyen TP vs SL ──
    if tps:
        insights["avg_score_tp"] = round(
            sum(int(t.get("score_h1") or 0) for t in tps) / len(tps)
        )
    if sls:
        insights["avg_score_sl"] = round(
            sum(int(t.get("score_h1") or 0) for t in sls) / len(sls)
        )

    # ── Heure la plus perdante ──
    if sls:
        sl_hours = Counter(
            int((t.get("time") or "00:00").split(":")[0]) for t in sls
        )
        worst_hour, worst_count = sl_hours.most_common(1)[0]
        # Vérifier si cette heure est aussi perdante chez les TP
        tp_hours = Counter(
            int((t.get("time") or "00:00").split(":")[0]) for t in tps
        )
        # Heure à éviter = beaucoup de SL et peu de TP
        if worst_count >= 2 and tp_hours.get(worst_hour, 0) < worst_count:
            insights["worst_hour"]        = worst_hour
            insights["worst_hour_sl"]     = worst_count
            insights["worst_hour_tp"]     = tp_hours.get(worst_hour, 0)

    # ── Paire la plus perdante ──
    if sls:
        sl_pairs = Counter(
            (t.get("pair") or "?").replace("/","") for t in sls
        )
        tp_pairs = Counter(
            (t.get("pair") or "?").replace("/","") for t in tps
        )
        worst_pair, wpc = sl_pairs.most_common(1)[0]
        if wpc >= 2:
            insights["worst_pair"]        = worst_pair
            insights["worst_pair_sl"]     = wpc
            insights["worst_pair_tp"]     = tp_pairs.get(worst_pair, 0)

    # ── Recommandation MIN_SCORE ──
    avg_tp = insights.get("avg_score_tp", 0)
    avg_sl = insights.get("avg_score_sl", 0)
    if avg_tp > 0 and avg_sl > 0 and avg_tp > avg_sl:
        # Seuil optimal = mi-chemin entre score moyen TP et SL
        recommended = round((avg_tp + avg_sl) / 2)
        recommended = max(55, min(80, recommended))  # Entre 55 et 80
        if abs(recommended - int(insights.get("current_min_score", recommended))) >= 5:
            insights["recommended_min_score"] = recommended

    return insights


# ─────────────────────────────────────────────────────────────
#  5. ENREGISTREMENT D'UN SIGNAL
# ─────────────────────────────────────────────────────────────

def save_signal(consensus, pair, gh_token, gh_repo):
    """
    Enregistre le signal dans signals_history.json sur GitHub.
    Effectue un mini_backtest et note le résultat.
    Retourne (history, sha) pour réutilisation.
    """
    history, sha = _read_history(gh_token, gh_repo)

    sltp     = consensus["r1"].get("sltp", {})
    now      = datetime.now(timezone.utc)
    pair_mt5 = pair.replace("/", "")

    # Mini backtest
    wr_hist, nb, reco = mini_backtest(
        pair_mt5, consensus["signal"],
        consensus["score_h1"], history
    )

    if wr_hist is not None:
        icon = "✅" if reco == "GO" else "⚠️" if reco == "CAUTION" else "❌"
        print(f"    [perf] Backtest {pair} {consensus['signal']}: "
              f"WR={wr_hist}% sur {nb} trades → {icon} {reco}")
    else:
        print(f"    [perf] Backtest {pair}: pas assez de données ({nb} trades)")

    entry = {
        "date":            now.strftime("%Y-%m-%d"),
        "time":            now.strftime("%H:%M"),
        "created_at":      now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pair":            pair_mt5,
        "signal":          consensus["signal"],
        "score_h1":        consensus["score_h1"],
        "score_h4":        consensus["score_h4"],
        "partial":         consensus.get("partial", False),
        "entry":           sltp.get("entree",    "0"),
        "sl":              sltp.get("sl",         "0"),
        "tp":              sltp.get("tp",         "0"),
        "lot":             sltp.get("lot_micro",  "0.01"),
        "rr":              sltp.get("rr",         "1:2"),
        "result":          "pending",
        "backtest_wr":     wr_hist,
        "backtest_n":      nb,
        "backtest_reco":   reco,
    }
    history.append(entry)
    _write_history(history, sha, gh_token, gh_repo, label="new signal")
    return history, sha


# ─────────────────────────────────────────────────────────────
#  6. POINT D'ENTRÉE PRINCIPAL
# ─────────────────────────────────────────────────────────────

def run(gh_token, gh_repo, current_min_score):
    """
    Appelé depuis main() à chaque scan.
    Étapes :
      1. Lit l'historique
      2. Met à jour les résultats en attente (TP/SL réels)
      3. Analyse les pertes
      4. Sauvegarde performance.json pour le dashboard
    """
    print("\n  ── Performance Engine ──")
    history, sha = _read_history(gh_token, gh_repo)

    if not history:
        print("  Historique vide — rien à analyser")
        return history, sha

    print(f"  {len(history)} signals dans l'historique")

    # Étape 1 — Mettre à jour les résultats en attente
    n_updated = update_results(history)
    if n_updated > 0:
        print(f"  {n_updated} trade(s) mis à jour avec résultats réels")
        _write_history(history, sha, gh_token, gh_repo, label="results update")
        # Relire le SHA après écriture
        _, sha = _read_history(gh_token, gh_repo)
        # Relire l'historique mis à jour pour la suite
        history, sha = _read_history(gh_token, gh_repo)
    else:
        print("  Aucun trade en attente à mettre à jour")

    # Étape 2 — Analyser les pertes
    insights = analyze_losses(history)
    insights["current_min_score"] = current_min_score

    resolved = insights.get("total_resolved", 0)
    if resolved >= 5:
        print(f"  Win Rate réel : {insights.get('win_rate')}% "
              f"({insights.get('tp')} TP / {insights.get('sl')} SL)")

        if insights.get("worst_pair"):
            print(f"  ⚠️  Paire à surveiller : {insights['worst_pair']} "
                  f"({insights['worst_pair_sl']} SL vs {insights['worst_pair_tp']} TP)")

        if insights.get("worst_hour") is not None:
            print(f"  ⚠️  Heure à risque : {insights['worst_hour']}h UTC "
                  f"({insights['worst_hour_sl']} SL vs {insights['worst_hour_tp']} TP)")

        if insights.get("recommended_min_score"):
            print(f"  💡 MIN_SCORE recommandé : {insights['recommended_min_score']} "
                  f"(actuel : {current_min_score})")

        # Étape 3 — Sauvegarder performance.json
        pnl = insights.get("tp", 0) * 20 - insights.get("sl", 0) * 10
        perf = {
            "win_rate":             insights.get("win_rate"),
            "total_resolved":       resolved,
            "tp":                   insights.get("tp"),
            "sl":                   insights.get("sl"),
            "pnl_simule":           pnl,
            "avg_score_tp":         insights.get("avg_score_tp"),
            "avg_score_sl":         insights.get("avg_score_sl"),
            "worst_pair":           insights.get("worst_pair"),
            "worst_hour":           insights.get("worst_hour"),
            "recommended_min_score": insights.get("recommended_min_score"),
            "current_min_score":    current_min_score,
        }
        _write_performance_json(perf, gh_token, gh_repo)
    else:
        print(f"  Pas assez de trades résolus ({resolved}/5 minimum) pour analyser")

    print("  ── Fin Performance Engine ──")
    return history, sha
