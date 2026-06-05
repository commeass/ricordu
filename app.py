#!/usr/bin/env python3
"""
app.py — interface web LOCALE pour la pipeline de montage.
Sert un front épuré, pilote scan/ai_select/render, diffuse la progression (SSE),
gère la bibliothèque musicale et suggère des musiques selon l'événement.

Lancer :  ./ui.sh   (ou : .venv/bin/uvicorn app:app --port 8723)
Tout tourne en local. Aucune donnée ne sort de la machine.
"""
import os, sys, json, time, queue, threading, subprocess, io, re, shutil, asyncio
from urllib.parse import quote
from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles

BASE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(BASE, ".venv", "bin", "python")
RUN = os.path.join(BASE, "run")
PROJECTS = os.path.join(BASE, "projects")
os.makedirs(RUN, exist_ok=True)
os.makedirs(PROJECTS, exist_ok=True)
os.environ.setdefault("HF_HOME", "/Users/jules/Models")
ENV = dict(os.environ)
sys.path.insert(0, BASE)
import diaporama as D

app = FastAPI()

@app.middleware("http")
async def no_cache(request, call_next):
    resp = await call_next(request)
    p = request.url.path
    if p.startswith("/api") or p in ("/", "/index.html", "/app.js", "/style.css"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp

# ----------------------------------------------------------------- état du job
JOB = {"status": "idle", "stage": None, "progress": 0, "log": [], "reports": {},
       "q": queue.Queue(), "montage": None}

def emit(ev):
    JOB["q"].put(ev)

def job_busy():
    """Vrai seulement si un thread de rendu est RÉELLEMENT vivant (évite les états 'en cours' fantômes)."""
    t = JOB.get("thread")
    return JOB.get("status") == "running" and t is not None and t.is_alive()

def slug(s): return re.sub(r"[^a-zA-Z0-9_-]+", "_", s)[:60]

# ----------------------------------------------------------------- musique
PERSO = os.path.join(BASE, "music", "perso")   # musiques importées / YouTube (hors dépôt git)
_AUDIO_EXT = (".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg")

def _probe_dur(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", path], capture_output=True, text=True)
    try: return round(float(r.stdout.strip()))
    except Exception: return 0

def _safe_name(name):
    base = os.path.splitext(os.path.basename(name or ""))[0]
    base = re.sub(r"[^\w\- ]+", "", base, flags=re.U).strip().replace(" ", "_")
    return (base[:60] or "musique")

def _track_entry(path):
    rel = os.path.relpath(path, BASE)
    return {"title": os.path.splitext(os.path.basename(path))[0], "file": rel,
            "mood": "perso", "bpm": None, "duration": _probe_dur(path), "perso": True}

def perso_tracks():
    if not os.path.isdir(PERSO): return []
    return [_track_entry(os.path.join(PERSO, fn)) for fn in sorted(os.listdir(PERSO))
            if fn.lower().endswith(_AUDIO_EXT) and not fn.startswith(".")]

def catalog():
    p = os.path.join(BASE, "music", "catalog.json")
    base = json.load(open(p)) if os.path.exists(p) else []
    return perso_tracks() + base   # les musiques perso d'abord

# mots-clés d'événement -> ambiances classées par pertinence
MOOD_RULES = [
    (("anniversaire", "birthday", "louison", "enfant", "kids", "gouter"), ["joyeux", "fun", "tendre"]),
    (("mariage", "wedding", "noce"), ["tendre", "epique", "joyeux"]),
    (("voyage", "trip", "vacances", "travel", "rando", "montagne", "mer"), ["epique", "chill", "joyeux"]),
    (("noel", "christmas", "fete", "soiree", "party", "nouvel"), ["joyeux", "fun", "epique"]),
    (("bebe", "naissance", "baby", "famille", "family"), ["tendre", "joyeux", "chill"]),
    (("sport", "match", "course", "race"), ["epique", "fun"]),
]
def suggest_moods(event_name):
    n = (event_name or "").lower()
    for kws, moods in MOOD_RULES:
        if any(k in n for k in kws):
            return moods
    return ["joyeux", "tendre", "chill"]

def build_music_mix(files, out):
    """Concatène plusieurs pistes (fondu enchaîné) en un seul lit musical."""
    files = [f if os.path.isabs(f) else os.path.join(BASE, f) for f in files]
    files = [f for f in files if os.path.exists(f)]
    if not files: return None
    if len(files) == 1: return files[0]
    inp = []
    for f in files: inp += ["-i", f]
    n = len(files)
    chain = "[0:a]"
    parts = []
    for i in range(1, n):
        nl = f"a{i}"
        parts.append(f"{chain}[{i}:a]acrossfade=d=2[{nl}]")
        chain = f"[{nl}]"
    fc = ";".join(parts)
    cmd = ["ffmpeg", "-y"] + inp + ["-filter_complex", fc, "-map", chain,
           "-c:a", "libmp3lame", "-q:a", "3", out]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out if os.path.exists(out) else files[0]

# ----------------------------------------------------------------- pipeline
def stream_cmd(cmd, on_line):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1, env=ENV, cwd=BASE)
    for line in p.stdout:
        on_line(line.rstrip("\n"))
    p.wait()
    return p.returncode

def scan_report(sb_path):
    sb = json.load(open(sb_path))
    from datetime import datetime
    def ts(c):
        for k in ("date_exif", "date_creation"):
            if c.get(k):
                try: return datetime.fromisoformat(c[k]).timestamp()
                except Exception: pass
        return c.get("mtime", 0)
    cl = sb["clips"]; ph = [c for c in cl if c["type"] == "photo"]; vd = [c for c in cl if c["type"] == "video"]
    dts = [ts(c) for c in cl if ts(c)]
    span = ""
    if dts:
        span = f"{datetime.fromtimestamp(min(dts)):%d/%m %H:%M} → {datetime.fromtimestamp(max(dts)):%H:%M}"
    return {"photos": len(ph), "videos": len(vd),
            "rush": round(sum(v.get("src_duration", 0) for v in vd)), "span": span,
            "title": sb["settings"].get("title", "")}

def selection_report(sb_path, kept_line):
    sb = json.load(open(sb_path))
    inc = [c for c in sb["clips"] if c.get("include")]
    photos = [c for c in sb["clips"] if c["type"] == "photo"]
    from collections import Counter
    hist = Counter(round(c.get("ai_score", 0)) for c in photos)
    moments = [{"caption": c.get("ai_caption", ""), "dur": c.get("duration"),
                "file": c["file"]} for c in inc if c["type"] == "video"]
    highlights = sorted([c for c in inc if c["type"] == "photo"],
                        key=lambda c: -(c.get("ai_score", 0)))[:10]
    m = re.search(r"gardé (\d+)/(\d+)", kept_line or "")
    kept, scored = (int(m.group(1)), int(m.group(2))) if m else (len(inc), len(inc))
    return {"kept": kept, "scored": scored,
            "photos": sum(1 for c in inc if c["type"] == "photo"),
            "videos": len(moments), "dropped": max(0, scored - kept),
            "hist": {str(k): hist[k] for k in sorted(hist)},
            "moments": moments,
            "highlights": [{"file": c["file"], "score": c.get("ai_score"),
                            "caption": c.get("ai_caption", "")} for c in highlights]}

def final_report(sb_path, montage, music):
    import glob, bisect, statistics as st
    sys.path.insert(0, BASE)
    import diaporama as D
    def ffdur(p):
        r = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                            "-of", "csv=p=0", p], capture_output=True, text=True)
        try: return float(r.stdout.strip())
        except Exception: return 0.0
    rep = {"duration": round(ffdur(montage), 1)}
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height", "-of", "csv=p=0", montage],
                       capture_output=True, text=True)
    rep["resolution"] = r.stdout.strip().replace(",", "×")
    try:
        tempo, beats = D.analyze_beats(music)
        segs = sorted(glob.glob(os.path.join(RUN, "work", "seg_*.mp4")))
        durs = [ffdur(s) for s in segs]
        starts = [0.0]
        for d in durs[:-1]: starts.append(starts[-1] + d)
        errs = []
        for o in starts[1:]:
            j = bisect.bisect_left(beats, o); cand = [beats[k] for k in (j-1, j) if 0 <= k < len(beats)]
            if cand: errs.append(min(abs(o - b) for b in cand) * 1000)
        rep["bpm"] = round(tempo)
        rep["sync_ms"] = round(st.median(errs)) if errs else None
        rep["sync_max"] = round(max(errs)) if errs else None
    except Exception as e:
        rep["sync_ms"] = None
    return rep

def run_pipeline(opts):
    try:
        JOB.update(status="running", stage="scan", progress=2, log=[], reports={}, montage=None)
        sb = os.path.join(RUN, "storyboard.json")
        folder = opts["folder"]
        emit({"type": "stage", "stage": "scan", "label": "Lecture du dossier"})
        stream_cmd([PY, "diaporama.py", "scan", folder, "-o", sb], lambda l: emit({"type": "log", "line": l}))
        rep = scan_report(sb); JOB["reports"]["scan"] = rep
        emit({"type": "report", "name": "scan", "data": rep})

        # injecte les options dans le storyboard
        d = json.load(open(sb)); S = d["settings"]
        S["title"] = opts.get("title") or rep["title"] or os.path.basename(folder.rstrip("/"))
        S["target_duration"] = float(opts.get("target", 150))
        S["order"] = opts.get("order", "chrono")
        S["beats_per_clip"] = int(opts.get("beats_per_clip", 4))
        S["rhythm"] = opts.get("rhythm", "fixe")
        S["video_share"] = float(opts.get("video_share", 0.3))
        S["captions"] = "all" if opts.get("captions") else "none"
        S["hardware_accel"] = bool(opts.get("hardware_accel", False))
        S["video_audio"] = opts.get("video_audio", "cut")
        S["scene_threshold_ai"] = float(opts.get("scene_threshold_ai", 0.5))
        S["end_text"] = opts.get("end_text") or None
        S["end_photo"] = opts.get("end_photo") or None
        music_files = opts.get("music", [])
        S["music_tracks"] = list(music_files)   # pistes individuelles (mode module = changement de musique)
        mix = build_music_mix(music_files, os.path.join(RUN, "music_mix.mp3")) if music_files else None
        S["music"] = os.path.relpath(mix, BASE) if mix else None
        json.dump(d, open(sb, "w"), indent=2, ensure_ascii=False)

        JOB["stage"] = "ai"
        emit({"type": "stage", "stage": "ai", "label": "Analyse IA (notation locale)"})
        kept_line = {"v": ""}
        def on_ai(l):
            emit({"type": "log", "line": l})
            mp = re.search(r"\[progress\] (photos|videos) (\d+)/(\d+)", l)
            if mp:
                base = 5 if mp.group(1) == "photos" else 55
                frac = int(mp.group(2)) / max(1, int(mp.group(3)))
                JOB["progress"] = int(base + frac * (50 if mp.group(1) == "photos" else 15))
                emit({"type": "progress", "value": JOB["progress"], "detail": l.split("] ")[-1]})
            if "[ai] gardé" in l: kept_line["v"] = l
        stream_cmd([PY, "diaporama.py", "ai_select", sb], on_ai)
        rep = selection_report(sb, kept_line["v"]); JOB["reports"]["selection"] = rep
        emit({"type": "report", "name": "selection", "data": rep})

        render_stage(sb)
    except Exception as e:
        JOB.update(status="error")
        emit({"type": "error", "message": str(e)})

def render_stage(sb_path):
    JOB["stage"] = "render"
    emit({"type": "stage", "stage": "render", "label": "Montage & rendu"})
    montage = os.path.join(RUN, "montage.mp4")
    def on_r(l):
        emit({"type": "log", "line": l})
        mp = re.search(r"\[progress\] segments (\d+)/(\d+)", l)
        if mp:
            frac = int(mp.group(1)) / max(1, int(mp.group(2)))
            JOB["progress"] = int(72 + frac * 25)
            emit({"type": "progress", "value": JOB["progress"], "detail": f"segment {mp.group(1)}/{mp.group(2)}"})
    stream_cmd([PY, "diaporama.py", "render", sb_path, "-o", montage], on_r)
    S = json.load(open(sb_path))["settings"]
    music_abs = os.path.join(BASE, S["music"]) if S.get("music") else None
    rep = final_report(sb_path, montage, music_abs) if os.path.exists(montage) else {}
    JOB["reports"]["final"] = rep; JOB["montage"] = montage
    JOB.update(status="done", progress=100)
    emit({"type": "report", "name": "final", "data": rep})
    emit({"type": "done", "montage": "/media/montage.mp4?t=" + str(int(time.time()))})

def run_reselect(includes, opts):
    """Re-RENDU seul (aucune re-analyse) : applique tous les réglages de rendu + la sélection."""
    try:
        JOB.update(status="running", stage="render", progress=60)
        sb_path = os.path.join(RUN, "storyboard.json")
        d = json.load(open(sb_path)); S = d["settings"]
        for k in ("title", "order", "rhythm"):
            if opts.get(k) is not None: S[k] = opts[k]
        if opts.get("beats_per_clip") is not None: S["beats_per_clip"] = int(opts["beats_per_clip"])
        if opts.get("end_text") is not None: S["end_text"] = opts["end_text"] or None
        if opts.get("end_photo") is not None: S["end_photo"] = opts["end_photo"] or None
        if "captions" in opts: S["captions"] = "all" if opts["captions"] else "none"
        if "hardware_accel" in opts: S["hardware_accel"] = bool(opts["hardware_accel"])
        if opts.get("video_audio"): S["video_audio"] = opts["video_audio"]
        if opts.get("scene_threshold_ai") is not None: S["scene_threshold_ai"] = float(opts["scene_threshold_ai"])
        if "music" in opts:                       # changement de musique -> rebuild du mix, pas de re-analyse
            mf = opts["music"] or []
            S["music_tracks"] = list(mf)
            mix = build_music_mix(mf, os.path.join(RUN, "music_mix.mp3")) if mf else None
            S["music"] = os.path.relpath(mix, BASE) if mix else None
        for c in d["clips"]:
            cid = str(c.get("id"))
            if cid in includes: c["include"] = bool(includes[cid])
        if opts.get("order") == "manual" and opts.get("order_ids"):     # ordre manuel (glisser-déposer)
            rank = {str(i): n for n, i in enumerate(opts["order_ids"])}
            for c in d["clips"]:
                c["manual_rank"] = rank.get(str(c.get("id")), 10**6)
        elif opts.get("order") is not None:                             # retour à un ordre auto -> on efface le manuel
            for c in d["clips"]:
                c.pop("manual_rank", None)
        json.dump(d, open(sb_path, "w"), indent=2, ensure_ascii=False)
        render_stage(sb_path)
    except Exception as e:
        JOB.update(status="error")
        emit({"type": "error", "message": str(e)})

# ----------------------------------------------------------------- API
@app.get("/api/catalog")
def api_catalog():
    return {"tracks": catalog()}

def _to_mp3(src, dst):
    """Transcode n'importe quel audio en MP3 192k (compatible navigateur + pipeline)."""
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src, "-vn",
                    "-c:a", "libmp3lame", "-q:a", "3", dst],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return os.path.exists(dst)

def _unique_mp3(name):
    os.makedirs(PERSO, exist_ok=True)
    dst = os.path.join(PERSO, f"{name}.mp3"); i = 1
    while os.path.exists(dst):
        dst = os.path.join(PERSO, f"{name}_{i}.mp3"); i += 1
    return dst

@app.post("/api/upload-music")
async def api_upload_music(file: UploadFile = File(...)):
    """Importer un fichier audio depuis l'ordinateur -> music/perso/ (transcodé en mp3)."""
    os.makedirs(PERSO, exist_ok=True)
    tmp = os.path.join(PERSO, ".tmp_upload")
    with open(tmp, "wb") as f:
        shutil.copyfileobj(file.file, f)
    dst = _unique_mp3(_safe_name(file.filename))
    ok = _to_mp3(tmp, dst)
    try: os.remove(tmp)
    except Exception: pass
    if not ok:
        return JSONResponse({"error": "fichier audio illisible"}, status_code=400)
    return {"track": _track_entry(dst)}

def _yt_download(url):
    """Télécharge l'audio d'une URL (YouTube…) en mp3 dans music/perso/. Renvoie le chemin ou lève."""
    import yt_dlp
    os.makedirs(PERSO, exist_ok=True)
    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(PERSO, ".ytdl_%(id)s.%(ext)s"),
        "noplaylist": True, "quiet": True, "no_warnings": True,
        "ffmpeg_location": shutil.which("ffmpeg") or "ffmpeg",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    produced = None
    rd = info.get("requested_downloads") or []
    if rd and rd[0].get("filepath"):
        produced = rd[0]["filepath"]
    if not (produced and os.path.exists(produced)):
        produced = os.path.splitext(ydl.prepare_filename(info))[0] + ".mp3"
    if not (produced and os.path.exists(produced)):
        raise RuntimeError("audio introuvable après téléchargement")
    dst = _unique_mp3(_safe_name(info.get("title", "youtube")))
    os.replace(produced, dst)
    return dst

@app.post("/api/youtube-music")
async def api_youtube_music(req: Request):
    """Ajouter une musique depuis un lien YouTube (audio uniquement) -> music/perso/."""
    body = await req.json()
    url = (body.get("url") or "").strip()
    if not re.match(r"https?://", url):
        return JSONResponse({"error": "lien invalide"}, status_code=400)
    try:
        import yt_dlp  # noqa
    except Exception:
        return JSONResponse({"error": "yt-dlp non installé (pip install yt-dlp)"}, status_code=500)
    try:
        dst = await asyncio.to_thread(_yt_download, url)
    except Exception as e:
        return JSONResponse({"error": f"téléchargement impossible : {str(e)[:140]}"}, status_code=400)
    return {"track": _track_entry(dst)}

@app.post("/api/suggest")
async def api_suggest(req: Request):
    body = await req.json()
    moods = suggest_moods(body.get("event", ""))
    cat = catalog()
    ranked = sorted(cat, key=lambda t: moods.index(t["mood"]) if t["mood"] in moods else 99)
    return {"moods": moods, "suggested": [t["file"] for t in ranked[:3]]}

@app.post("/api/pick-folder")
def api_pick_folder():
    try:
        r = subprocess.run(["osascript", "-e",
            'POSIX path of (choose folder with prompt "Choisis le dossier de l\'événement")'],
            capture_output=True, text=True, timeout=120)
        path = r.stdout.strip()
        return {"path": path.rstrip("/") if path else None}
    except Exception as e:
        return {"path": None, "error": str(e)}

@app.post("/api/run")
async def api_run(req: Request):
    if job_busy():
        return JSONResponse({"error": "un montage est déjà en cours"}, status_code=409)
    opts = await req.json()
    if not opts.get("folder") or not os.path.isdir(opts["folder"]):
        return JSONResponse({"error": "dossier introuvable"}, status_code=400)
    JOB["q"] = queue.Queue()
    t = threading.Thread(target=run_pipeline, args=(opts,), daemon=True); t.start(); JOB["thread"] = t
    return {"ok": True}

@app.post("/api/cancel")
def api_cancel():
    JOB["status"] = "idle"; JOB["thread"] = None
    return {"ok": True}

@app.get("/api/events")
def api_events():
    def gen():
        yield "data: " + json.dumps({"type": "hello", "status": JOB["status"]}) + "\n\n"
        while True:
            try:
                ev = JOB["q"].get(timeout=20)
                yield "data: " + json.dumps(ev) + "\n\n"
                if ev["type"] in ("done", "error"):
                    break
            except queue.Empty:
                yield ": keepalive\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")

@app.get("/api/thumb")
def api_thumb(file: str, w: int = 280):
    if not os.path.exists(file): return Response(status_code=404)
    try:
        from PIL import Image, ImageOps
        try:
            import pillow_heif; pillow_heif.register_heif_opener()
        except Exception: pass
        img = ImageOps.exif_transpose(Image.open(file)).convert("RGB")
        img.thumbnail((w, w))
        buf = io.BytesIO(); img.save(buf, "JPEG", quality=82)
        return Response(buf.getvalue(), media_type="image/jpeg")
    except Exception:
        return Response(status_code=404)

@app.get("/media/montage.mp4")
def media_montage(t: str = ""):
    p = os.path.join(RUN, "montage.mp4")
    return FileResponse(p) if os.path.exists(p) else Response(status_code=404)

@app.get("/api/selection")
def api_selection():
    sb_path = os.path.join(RUN, "storyboard.json")
    if not os.path.exists(sb_path): return {"clips": []}
    d = json.load(open(sb_path))
    clips = [c for c in d["clips"] if not (c["type"] == "video" and not c.get("is_subclip"))]
    if any("manual_rank" in c for c in clips):     # l'éditeur reflète l'ordre manuel s'il existe
        clips.sort(key=lambda c: c.get("manual_rank", 10**6))
    else:
        clips.sort(key=lambda c: (D.media_date(c), c.get("trim_start", 0)))
    out = []
    for c in clips:
        md = D.media_date(c)
        it = {"id": c.get("id"), "type": c["type"], "include": bool(c.get("include")), "file": c["file"],
              "score": round(c.get("ai_score", 0), 1), "caption": c.get("ai_caption", ""),
              "when": time.strftime("%d/%m %Hh%M", time.localtime(md)) if md else ""}
        if c["type"] == "photo":
            it["thumb"] = "/api/thumb?file=" + quote(c["file"])
        else:
            mid = c.get("trim_start", 0) + c.get("duration", 0) / 2
            it["thumb"] = f"/api/vthumb?file={quote(c['file'])}&t={mid:.2f}"
            it["range"] = f"{c.get('trim_start',0):.0f}–{c.get('trim_start',0)+c.get('duration',0):.0f}s"
            it["src"] = "/api/srcvideo?file=" + quote(c["file"])
            it["start"] = round(c.get("trim_start", 0), 2); it["dur"] = round(c.get("duration", 0), 2)
        out.append(it)
    return {"clips": out, "title": d["settings"].get("title", ""),
            "end_text": d["settings"].get("end_text") or "", "end_photo": d["settings"].get("end_photo") or ""}

@app.get("/api/srcvideo")
def api_srcvideo(file: str):
    return FileResponse(file) if os.path.exists(file) else Response(status_code=404)

@app.post("/api/add-clip")
async def api_add_clip(req: Request):
    """Ajoute un extrait vidéo découpé manuellement (start/end en secondes dans la source)."""
    body = await req.json()
    f = body.get("file"); start = float(body.get("start", 0)); end = float(body.get("end", 0))
    if not f or not os.path.exists(f) or end <= start + 0.3:
        return JSONResponse({"error": "extrait invalide (durée trop courte ?)"}, status_code=400)
    sb_path = os.path.join(RUN, "storyboard.json")
    if not os.path.exists(sb_path): return JSONResponse({"error": "aucun projet"}, status_code=400)
    d = json.load(open(sb_path))
    orig = next((c for c in d["clips"] if c.get("file") == f and c["type"] == "video"
                 and not c.get("is_subclip")), None)
    info = D.ffprobe_info(f)
    srcdur = (orig.get("src_duration") if orig else None) or info["duration"] or end
    end = min(end, srcdur)
    nv = {"file": f, "type": "video", "is_subclip": True, "include": True, "manual": True,
          "trim_start": round(start, 2), "trim_end": round(srcdur - end, 2), "duration": round(end - start, 2),
          "ai_score": 8.5, "ai_caption": "extrait manuel", "ai_type": "manuel",
          "has_audio": (orig.get("has_audio") if orig else info["has_audio"]),
          "mtime": (orig.get("mtime") if orig else 0),
          "date_creation": (orig.get("date_creation") if orig else None)}
    nv["id"] = max((c.get("id", 0) for c in d["clips"]), default=0) + 1
    d["clips"].append(nv)
    json.dump(d, open(sb_path, "w"), indent=2, ensure_ascii=False)
    return {"ok": True, "id": nv["id"]}

@app.get("/api/vthumb")
def api_vthumb(file: str, t: float = 0.0, w: int = 300):
    if not os.path.exists(file): return Response(status_code=404)
    tmp = os.path.join(RUN, "_vt.jpg")
    subprocess.run(["ffmpeg", "-y", "-ss", f"{t}", "-i", file, "-frames:v", "1",
                    "-vf", f"scale={w}:-1", "-q:v", "4", tmp],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return Response(open(tmp, "rb").read(), media_type="image/jpeg") if os.path.exists(tmp) else Response(status_code=404)

@app.post("/api/reselect")
async def api_reselect(req: Request):
    if job_busy():
        return JSONResponse({"error": "un montage est déjà en cours"}, status_code=409)
    body = await req.json()
    JOB["q"] = queue.Queue()
    t = threading.Thread(target=run_reselect, args=(body.get("includes", {}), body), daemon=True); t.start(); JOB["thread"] = t
    return {"ok": True}

@app.get("/api/projects")
def api_projects():
    out = []
    for f in sorted(os.listdir(PROJECTS)):
        if not f.endswith(".json"): continue
        try:
            d = json.load(open(os.path.join(PROJECTS, f)))
            out.append({"name": d.get("_name", f[:-5]), "saved": d.get("_saved", ""),
                        "title": d.get("settings", {}).get("title", ""),
                        "clips": sum(1 for c in d.get("clips", []) if c.get("include"))})
        except Exception: pass
    out.sort(key=lambda p: p.get("saved", ""), reverse=True)
    return {"projects": out}

@app.post("/api/save-project")
async def api_save_project(req: Request):
    body = await req.json()
    name = (body.get("name") or "").strip() or "projet"
    sb_path = os.path.join(RUN, "storyboard.json")
    if not os.path.exists(sb_path):
        return JSONResponse({"error": "aucun montage à sauvegarder"}, status_code=400)
    d = json.load(open(sb_path)); d["_name"] = name; d["_saved"] = time.strftime("%d/%m/%Y %H:%M")
    json.dump(d, open(os.path.join(PROJECTS, slug(name) + ".json"), "w"), indent=2, ensure_ascii=False)
    return {"ok": True, "name": name}

@app.post("/api/delete-project")
async def api_delete_project(req: Request):
    body = await req.json()
    p = os.path.join(PROJECTS, slug(body.get("name", "")) + ".json")
    if os.path.exists(p): os.remove(p); return {"ok": True}
    return JSONResponse({"error": "projet introuvable"}, status_code=404)

@app.post("/api/load-project")
async def api_load_project(req: Request):
    body = await req.json()
    p = os.path.join(PROJECTS, slug(body.get("name", "")) + ".json")
    if not os.path.exists(p):
        return JSONResponse({"error": "projet introuvable"}, status_code=404)
    d = json.load(open(p))
    json.dump(d, open(os.path.join(RUN, "storyboard.json"), "w"), indent=2, ensure_ascii=False)
    S = d.get("settings", {})
    return {"ok": True, "settings": {k: S.get(k) for k in
            ("title", "end_text", "end_photo", "target_duration", "beats_per_clip", "rhythm", "order", "video_share", "music", "captions")}}

app.mount("/music", StaticFiles(directory=os.path.join(BASE, "music")), name="music")
app.mount("/", StaticFiles(directory=os.path.join(BASE, "web"), html=True), name="web")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8723)
