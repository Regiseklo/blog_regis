"""Veille automatique : flux RSS -> sélection et résumé par Gemini -> fichiers dans veille/.

Deux modes :
- quotidien (par défaut) : articles des dernières 72 h ;
- rétrospective (variable BACKFILL_DAYS) : articles parus ces N derniers jours, avec leur vraie date.
"""
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

BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS") or 0)
MODE = "retrospective" if BACKFILL_DAYS > 0 else "daily"
MAX_AGE_HOURS = BACKFILL_DAYS * 24 if MODE == "retrospective" else 72
PER_FEED = 25 if MODE == "retrospective" else 8
OUT = Path("veille")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")


def fr(date_iso):
    return f"{date_iso[8:10]}/{date_iso[5:7]}/{date_iso[:4]}"


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
            t = e.get("published_parsed") or e.get("updated_parsed")
            if t is None and MODE == "retrospective":
                continue  # sans date de publication, on ne peut pas l'afficher honnêtement
            published = datetime(*t[:6], tzinfo=timezone.utc) if t else None
            if published and published < limit:
                continue
            seen.add(link)  # évite les doublons entre flux
            summary = re.sub(r"<[^>]+>", " ", e.get("summary", ""))
            summary = re.sub(r"\s+", " ", summary).strip()[:400]
            items.append({
                "source": source,
                "title": e.get("title", "").strip(),
                "link": link,
                "published": published.date().isoformat() if published else "",
                "summary": summary,
            })
            count += 1
            if count >= PER_FEED:
                break
        print(f"{source} : {count} article(s)")
    return items


def ask_gemini(items):
    lines = "\n".join(
        f"[{i}] ({it['source']}, {it['published'] or 'date inconnue'}) {it['title']} : {it['summary']}"
        for i, it in enumerate(items)
    )
    if MODE == "retrospective":
        intro = f"Voici des articles parus ces {BACKFILL_DAYS} derniers jours, numérotés :"
        choix = "Choisis les 8 à 12 articles les plus utiles pour ce thème, avec au maximum 4 articles par source."
    else:
        intro = "Voici les articles récents, numérotés :"
        choix = "Choisis les 3 à 5 articles les plus utiles pour ce thème, avec au maximum 2 articles par source."
    prompt = (
        f"Tu aides un étudiant en BTS SIO option SISR à faire sa veille technologique sur {THEME}.\n"
        f"{intro}\n\n{lines}\n\n"
        f"{choix} Pour chacun, écris en français :\n"
        '- "titre" : le titre traduit en français ;\n'
        '- "resume" : 2 phrases simples qui expliquent ce qui s\'est passé ;\n'
        '- "impact" : 1 phrase sur ce que cela change pour un administrateur réseau, '
        "avec une tournure différente d'un article à l'autre.\n"
        "Appuie-toi uniquement sur les informations fournies, n'invente rien.\n"
        'Réponds uniquement par un tableau JSON : [{"id": 0, "titre": "...", "resume": "...", "impact": "..."}]'
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3},
    }
    headers = {"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"}
    for attempt in range(3):
        r = requests.post(url, headers=headers, json=body, timeout=120)
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
    # permet de relancer le même jour sans doublon, sans toucher à l'autre mode
    index = [e for e in index if not (e["date"] == today and e.get("type", "daily") == MODE)]
    seen = {a["link"] for e in index for a in e["articles"]}

    items = fetch_items(seen)
    if not items:
        print("Aucun nouvel article.")
        return

    articles = []
    for p in ask_gemini(items):
        if isinstance(p.get("id"), int) and 0 <= p["id"] < len(items):
            articles.append({
                **items[p["id"]],
                "titre": p.get("titre") or items[p["id"]]["title"],
                "resume": p["resume"],
                "impact": p["impact"],
            })
    if not articles:
        sys.exit("Gemini n'a retenu aucun article valide.")
    articles.sort(key=lambda a: a["published"], reverse=True)

    entry = {"date": today, "type": MODE}
    if MODE == "retrospective":
        dates = [a["published"] for a in articles if a["published"]]
        entry.update({"du": min(dates), "au": max(dates)})
        filename = f"retrospective-{today}.md"
        md = [
            f"# Sélection rétrospective du {fr(today)}",
            "",
            f"Sélection réalisée le {fr(today)} parmi des articles parus entre le {fr(entry['du'])} "
            f"et le {fr(entry['au'])}. Chaque article porte sa date de publication réelle.",
            "",
            f"Thème : {THEME}.",
            "",
        ]
    else:
        filename = f"{today}.md"
        md = [f"# Veille du {fr(today)}", "", f"Thème : {THEME}.", ""]

    for a in articles:
        pub = f" · publié le {fr(a['published'])}" if a["published"] else ""
        md += [
            f"## {a['titre']}",
            f"*Source : {a['source']}{pub}* · [Lire l'article]({a['link']})",
            "",
            a["resume"],
            "",
            f"**Pour un administrateur réseau :** {a['impact']}",
            "",
        ]
    (OUT / filename).write_text("\n".join(md), "utf-8")

    entry["file"] = filename
    entry["articles"] = [
        {k: a[k] for k in ("titre", "title", "source", "link", "published", "resume", "impact")} for a in articles
    ]
    index.insert(0, entry)
    index.sort(key=lambda e: e["date"], reverse=True)
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), "utf-8")
    print(f"{len(articles)} article(s) enregistrés ({MODE}, {today}).")


if __name__ == "__main__":
    main()