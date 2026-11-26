"""Veille automatique : flux RSS -> sélection et résumé par Gemini -> fichiers dans veille/."""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests

THEME = "la cybersécurité et l'évolution du métier d'administrateur réseau dans les infrastructures actuelles"

FEEDS = {
    "CERT-FR Alertes": "https://www.cert.ssi.gouv.fr/alerte/feed/",
    "CERT-FR Actualités": "https://www.cert.ssi.gouv.fr/actualite/feed/",
    "CERT-FR Menaces": "https://www.cert.ssi.gouv.fr/cti/feed/",
    "BleepingComputer": "https://www.bleepingcomputer.com/feed/",
    "Krebs on Security": "https://krebsonsecurity.com/feed/",
}

MAX_AGE_HOURS = 72   # on ignore les articles plus vieux
PER_FEED = 8         # nombre max d'articles lus par flux
OUT = Path("veille")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")


def fetch_items(seen):
    limit = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    items = []
    for source, url in FEEDS.items():
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (veille-bts-sio)"}, timeout=30)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"[!] {source} : {e}")
            continue
        count = 0
        for e in feedparser.parse(r.content).entries:
            link = e.get("link")
            if not link or link in seen:
                continue
            seen.add(link)  # évite les doublons entre flux
            t = e.get("published_parsed") or e.get("updated_parsed")
            if t and datetime(*t[:6], tzinfo=timezone.utc) < limit:
                continue
            summary = re.sub(r"<[^>]+>", " ", e.get("summary", ""))
            summary = re.sub(r"\s+", " ", summary).strip()[:400]
            items.append({"source": source, "title": e.get("title", "").strip(), "link": link, "summary": summary})
            count += 1
            if count >= PER_FEED:
                break
        print(f"{source} : {count} article(s)")
    return items


def ask_gemini(items):
    lines = "\n".join(f"[{i}] ({it['source']}) {it['title']} : {it['summary']}" for i, it in enumerate(items))
    prompt = (
        f"Tu aides un étudiant en BTS SIO option SISR à faire sa veille technologique sur {THEME}.\n"
        f"Voici les articles récents, numérotés :\n\n{lines}\n\n"
        "Choisis les 3 à 5 articles les plus utiles pour ce thème. Pour chacun, écris en français :\n"
        '- "resume" : 2 phrases simples qui expliquent ce qui s\'est passé ;\n'
        '- "impact" : 1 phrase sur ce que cela change pour un administrateur réseau.\n'
        "Appuie-toi uniquement sur les informations fournies, n'invente rien.\n"
        'Réponds uniquement par un tableau JSON : [{"id": 0, "resume": "...", "impact": "..."}]'
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3},
    }
    headers = {"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"}
    for attempt in range(3):
        r = requests.post(url, headers=headers, json=body, timeout=90)
        if r.status_code in (429, 500, 503) and attempt < 2:
            print(f"Gemini a répondu {r.status_code}, nouvel essai dans 30 s")
            time.sleep(30)
            continue
        if not r.ok:
            sys.exit(f"Erreur Gemini {r.status_code} : {r.text[:300]}")
        break
    parts = r.json()["candidates"][0]["content"]["parts"]
    return json.loads("".join(p.get("text", "") for p in parts))


def main():
    OUT.mkdir(exist_ok=True)
    index_path = OUT / "index.json"
    index = json.loads(index_path.read_text("utf-8")) if index_path.exists() else []

    today = datetime.now(ZoneInfo("Europe/Paris")).date().isoformat()
    index = [e for e in index if e["date"] != today]  # permet de relancer le même jour
    seen = {a["link"] for e in index for a in e["articles"]}

    items = fetch_items(seen)
    if not items:
        print("Aucun nouvel article.")
        return

    articles = []
    for p in ask_gemini(items):
        if isinstance(p.get("id"), int) and 0 <= p["id"] < len(items):
            articles.append({**items[p["id"]], "resume": p["resume"], "impact": p["impact"]})
    if not articles:
        sys.exit("Gemini n'a retenu aucun article valide.")

    md = [f"# Veille du {today}", "", f"Thème : {THEME}.", ""]
    for a in articles:
        md += [
            f"## {a['title']}",
            f"*Source : {a['source']}* · [Lire l'article]({a['link']})",
            "",
            a["resume"],
            "",
            f"**Pour un administrateur réseau :** {a['impact']}",
            "",
        ]
    (OUT / f"{today}.md").write_text("\n".join(md), "utf-8")

    articles = [{k: a[k] for k in ("title", "source", "link", "resume", "impact")} for a in articles]
    index.insert(0, {"date": today, "file": f"{today}.md", "articles": articles})
    index.sort(key=lambda e: e["date"], reverse=True)
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), "utf-8")
    print(f"{len(articles)} article(s) enregistrés pour le {today}.")


if __name__ == "__main__":
    main()