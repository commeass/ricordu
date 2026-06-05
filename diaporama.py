#!/usr/bin/env python3
"""
diaporama.py — pipeline locale photo/vidéo -> montage narratif (style "Souvenirs" Apple).

3 commandes :
  scan       <dossier>            -> storyboard.json (inventaire + chronologie, sans IA)
  ai_select  storyboard.json      -> enrichit le storyboard (pré-filtre CV + notation VLM local + sélection)
  render     storyboard.json      -> montage.mp4 (FFmpeg : Ken Burns, fondus, audio, légendes, titre)

Tout est 100% local. Le modèle de vision est Qwen3.6-35B-A3B-4bit via MLX.
"""
import os, sys, json, argparse, subprocess, math, re, hashlib, tempfile, shutil
from datetime import datetime

# --- Modèles partagés ---
os.environ.setdefault("HF_HOME", "/Users/jules/Models")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
MODEL_DEFAULT = "mlx-community/Qwen3.6-35B-A3B-4bit"
SCORE_VER = "v4"   # bump -> invalide le cache de notation quand le prompt change (v4 : détection aérienne)

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}

DEFAULTS = {
    "title": None,
    "end_card": True,           # carton de fin (photo + texte) avant le fondu
    "end_text": None,           # texte de fin (défaut = le titre)
    "target_duration": 150.0,
    "order": "chrono",            # chrono | scene | highlights | narrative
    "captions": "none",           # none (style Souvenirs) | all
    "video_share": 0.30,          # part de la durée réservée aux moments vidéo
    "photo_duration": 3.4,
    "transition_duration": 0.3,
    "resolution": [1920, 1080],
    "fps": 30,
    "music": None,
    "music_volume": 1.0,
    "beat_sync": True,
    "beats_per_clip": 4,          # cut toutes les N beats (4 = sur le temps fort de la mesure)
    "hard_cuts": True,            # cuts FRANCS sur le beat (style Neistat) au lieu de fondus
    "music_under_video": False,   # (déprécié, voir video_audio)
    "video_audio": "cut",         # cut = couper la musique sous les vidéos | duck = la BAISSER (on garde le fond)
    "photo_fit": "auto",          # auto = fond flou pour les portraits (n'ampute pas les visages) | fill
    "color_coherence": 0.0,       # 0..1 : harmonise la colorimétrie des photos vers la médiane du lot (Reinhard)
    "scene_threshold_ai": 0.5,    # mode "scène" : finesse du clustering DINOv2 (+ bas = scènes + fines)
    "title_seconds": 2.0,
    "ducking": True,
    "ken_burns": True,
    "hardware_accel": False,      # h264_videotoolbox si True, sinon libx264 (qualité fiable)
    "ai": {
        "model": MODEL_DEFAULT,
        "blur_var_min": 60.0,     # variance Laplacien (sur image 1024px) en dessous = flou
        "phash_hamming_max": 8,
        "score_keep_min": 5.0,
        "score_floor": 6.0,       # score en dessous -> écarté de la sélection (best-of)
        "scene_threshold": 27.0,
        "video_fps_sample": 1.0,
        "subclip_min_s": 2.0,
        "subclip_max_s": 6.0,
        "subclip_pad_s": 0.4,
        "vlm_long_edge": 1024,
        # --- découpage audio + mouvement ---
        "speech_margin_db": 8.0,      # sensibilité parole (au-dessus du bruit de fond)
        "pause_intra_ms": 150,        # trous < ce seuil = pause dans une phrase (comblés)
        "max_subclips_per_video": 3,   # nb max AUTO-inclus par vidéo
        "max_candidates_per_video": 8, # nb max d'extraits PROPOSÉS (éditables) par vidéo
        "cand_percentile": 45,         # seuil de détection des moments (plus bas = plus de candidats)
        "motion_fps": 6, "motion_edge": 256,   # finesse du flux optique
        "w_pic": 3.0, "w_type": 1.5, "w_contrast": 1.5, "w_dur": 1.0, "w_motion": 1.5,
        # --- plans aériens / drone (plans d'ouverture, sans son, qui respirent) ---
        "aerial_hold_s": 4.5,   # durée minimale tenue pour un plan aérien (il doit respirer)
        "aerial_bonus": 1.2,    # bonus de score : un beau plan large mérite d'être gardé
    },
}

# ----------------------------------------------------------------------------- utils
def run(cmd, **kw):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)

def ffprobe_info(path):
    """Durée, dimensions, présence audio, date de création d'une vidéo."""
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format",
           "-show_streams", path]
    r = run(cmd)
    info = {"duration": 0.0, "width": 0, "height": 0, "has_audio": False, "creation_time": None}
    try:
        data = json.loads(r.stdout.decode("utf-8", "ignore"))
    except Exception:
        return info
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and not info["width"]:
            info["width"] = s.get("width", 0); info["height"] = s.get("height", 0)
            ct = s.get("tags", {}).get("creation_time")
            if ct: info["creation_time"] = ct
        if s.get("codec_type") == "audio":
            info["has_audio"] = True
    fmt = data.get("format", {})
    try: info["duration"] = float(fmt.get("duration", 0) or 0)
    except Exception: pass
    if not info["creation_time"]:
        info["creation_time"] = fmt.get("tags", {}).get("creation_time")
    # indice "drone" via les métadonnées caméra (DJI, Autel, Skydio, Parrot Anafi…)
    low = r.stdout.decode("utf-8", "ignore").lower()
    info["drone"] = any(b in low for b in
        ("dji", "mavic", "phantom", "autel", "skydio", "parrot", "anafi", "fimi", "hubsan"))
    return info

_EXIF_DATE_TAG = 36867  # DateTimeOriginal
def photo_exif(path):
    """Renvoie (datetime|None, (lat,lon)|None) depuis l'EXIF."""
    try:
        from PIL import Image
        try:
            import pillow_heif; pillow_heif.register_heif_opener()
        except Exception: pass
        img = Image.open(path)
        exif = img._getexif() or {}
    except Exception:
        return None, None
    dt = None
    raw = exif.get(_EXIF_DATE_TAG) or exif.get(306)
    if raw:
        try: dt = datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
        except Exception: dt = None
    gps = None
    g = exif.get(34853)
    if g:
        try:
            def dms(v): return v[0] + v[1]/60.0 + v[2]/3600.0
            lat = dms(g[2]); lon = dms(g[4])
            if g[1] in ("S", b"S"): lat = -lat
            if g[3] in ("W", b"W"): lon = -lon
            gps = [round(lat, 6), round(lon, 6)]
        except Exception: gps = None
    return dt, gps

_FR_MONTHS = ["", "janvier", "février", "mars", "avril", "mai", "juin", "juillet",
              "août", "septembre", "octobre", "novembre", "décembre"]
def fr_date(ts):
    dt = datetime.fromtimestamp(ts)
    return f"{dt.day} {_FR_MONTHS[dt.month]} {dt.year}"

def parse_iso(s):
    """Parse une date ISO. Si elle est en UTC (suffixe Z) ou avec offset, la convertit en HEURE LOCALE
    (les vidéos sont souvent en UTC, les photos EXIF en local -> sinon l'ordre chronologique casse)."""
    if not s: return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)   # -> heure locale système, naïve
        return dt
    except Exception:
        pass
    for f in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try: return datetime.strptime(s, f)
        except Exception: pass
    return None

def media_date(clip):
    """Date pour le tri. EXIF photo = autoritaire. Pour les vidéos le creation_time est souvent en UTC
    avec une horloge caméra mal réglée (mauvais fuseau) -> on prend le mtime fichier, fiable et cohérent
    avec le Finder (vérifié : mtime = EXIF pour les photos, = heure réelle pour les vidéos)."""
    v = clip.get("date_exif")
    if v:
        try: return datetime.fromisoformat(v).timestamp()
        except Exception: pass
    return clip.get("mtime", 0)

def file_key(path):
    try: st = os.stat(path)
    except Exception: return path
    return f"{os.path.realpath(path)}::{int(st.st_mtime)}::{st.st_size}"

# ----------------------------------------------------------------------------- SCAN
def scan(folder, out):
    folder = os.path.abspath(folder)
    clips = []
    for name in sorted(os.listdir(folder)):
        p = os.path.join(folder, name)
        if not os.path.isfile(p): continue
        ext = os.path.splitext(name)[1].lower()
        if ext in PHOTO_EXT:
            dt, gps = photo_exif(p)
            mtime = os.stat(p).st_mtime
            clips.append({
                "file": p, "type": "photo", "include": True,
                "date_exif": dt.isoformat() if dt else None,
                "mtime": mtime, "gps": gps, "caption": "",
                "duration": DEFAULTS["photo_duration"],
            })
        elif ext in VIDEO_EXT:
            info = ffprobe_info(p)
            ct = parse_iso(info["creation_time"])
            mtime = os.stat(p).st_mtime
            clips.append({
                "file": p, "type": "video", "include": True,
                "date_creation": ct.isoformat() if ct else None,
                "mtime": mtime, "gps": None, "caption": "",
                "src_duration": round(info["duration"], 3),
                "has_audio": info["has_audio"], "drone": info.get("drone", False),
                "width": info["width"], "height": info["height"],
                "trim_start": 0.0, "trim_end": 0.0,
            })
    clips.sort(key=media_date)
    photos = sum(1 for c in clips if c["type"] == "photo")
    vids = [c for c in clips if c["type"] == "video"]
    settings = dict(DEFAULTS)
    settings["title"] = os.path.basename(folder)
    sb = {"source_dir": folder, "settings": settings, "clips": clips}
    with open(out, "w") as f: json.dump(sb, f, indent=2, ensure_ascii=False)
    dates = [media_date(c) for c in clips if media_date(c)]
    span = ""
    if dates:
        span = f" | du {datetime.fromtimestamp(min(dates)):%d/%m %H:%M} au {datetime.fromtimestamp(max(dates)):%d/%m %H:%M}"
    vdur = sum(v.get("src_duration", 0) for v in vids)
    print(f"[scan] {photos} photos, {len(vids)} vidéos ({vdur:.0f}s de rush){span}")
    print(f"[scan] -> {out}  (titre par défaut: « {settings['title']} »)")

# ----------------------------------------------------------------------------- CV pré-filtre
def _load_rgb_1024(path, long_edge=1024):
    from PIL import Image, ImageOps
    try:
        import pillow_heif; pillow_heif.register_heif_opener()
    except Exception: pass
    img = Image.open(path); img = ImageOps.exif_transpose(img).convert("RGB")
    w, h = img.size; s = long_edge / max(w, h)
    if s < 1: img = img.resize((max(1, int(w*s)), max(1, int(h*s))))
    return img

def blur_score(pil_img):
    import cv2, numpy as np
    g = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2GRAY)
    return float(cv2.Laplacian(g, cv2.CV_64F).var())

def phash_of(pil_img):
    import imagehash
    return imagehash.phash(pil_img)

# --- Cohérence colorimétrique : Reinhard (Lab) vers la médiane du lot, chrominance prioritaire ---
def lab_stats(path, ds=512):
    """Moyenne/écart-type en Lab (miniature, masque cramés/sombres). Réf: Reinhard 2001."""
    try:
        import cv2, numpy as np
        img = _load_rgb_1024(path, ds)
        lab = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2LAB).astype(np.float32).reshape(-1, 3)
        L = lab[:, 0]; m = (L > 12) & (L < 245)
        sel = lab[m] if m.sum() > 200 else lab
        return sel.mean(axis=0).tolist(), (sel.std(axis=0) + 1e-3).tolist()
    except Exception:
        return None, None

def grade_photo(path, ref, alpha, out_path):
    """Harmonise la photo vers la référence couleur (Reinhard Lab ; L peu touché -> préserve l'expo)."""
    try:
        import cv2, numpy as np
        from PIL import Image
        rgb = np.asarray(_load_rgb_1024(path, 4096)).astype(np.uint8)
        lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
        flat = lab.reshape(-1, 3); mu_i = flat.mean(axis=0); sg_i = flat.std(axis=0) + 1e-3
        mu_r = np.asarray(ref["mu"], np.float32); sg_r = np.asarray(ref["sigma"], np.float32)
        w = np.asarray([0.25, 1.0, 1.0], np.float32)        # L peu touché, a*/b* pleins
        out = lab.copy()
        for c in range(3):
            ratio = float(np.clip(sg_r[c] / sg_i[c], 0.6, 1.6))
            corr = (lab[..., c] - mu_i[c]) * ratio + mu_r[c]
            out[..., c] = lab[..., c] * (1 - w[c]*alpha) + corr * (w[c]*alpha)
        out = np.clip(out, 0, 255).astype(np.uint8)
        Image.fromarray(cv2.cvtColor(out, cv2.COLOR_LAB2RGB)).save(out_path, quality=93)
        return out_path
    except Exception:
        return path

# --- Reconnaissance de SCÈNES : embeddings DINOv2 (ONNX, Meta 2023) + signature couleur Lab + temps ---
_DINO = None
def _dino_session():
    global _DINO
    if _DINO is None:
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        p = hf_hub_download("sefaburak/dinov2-small-onnx", "dinov2_vits14.onnx")
        prov = [x for x in ("CoreMLExecutionProvider", "CPUExecutionProvider") if x in ort.get_available_providers()]
        _DINO = ort.InferenceSession(p, providers=prov)
    return _DINO

def dino_embed(path):
    """Embedding sémantique de scène DINOv2 ViT-S/14 (384-d, L2-normalisé)."""
    import numpy as np
    try:
        img = _load_rgb_1024(path, 256).resize((224, 224))
        x = (np.asarray(img).astype(np.float32)/255.0 - np.array([0.485, 0.456, 0.406], np.float32)) \
            / np.array([0.229, 0.224, 0.225], np.float32)
        x = x.transpose(2, 0, 1)[None].astype(np.float32)
        e = _dino_session().run(None, {"input": x})[0][0].astype(np.float32)
        return e / (np.linalg.norm(e) + 1e-8)
    except Exception:
        return None

def lab_signature(path):
    """Signature colorimétrique : histogramme a*,b* (8x8) + L (4 bins), racine (Hellinger)."""
    import cv2, numpy as np
    try:
        lab = cv2.cvtColor(np.asarray(_load_rgb_1024(path, 256)), cv2.COLOR_RGB2LAB)
        ab, _, _ = np.histogram2d(lab[..., 1].ravel(), lab[..., 2].ravel(), bins=8, range=[[0, 255], [0, 255]])
        hl, _ = np.histogram(lab[..., 0].ravel(), bins=4, range=(0, 255))
        v = np.concatenate([ab.ravel(), hl]).astype(np.float32); v /= (v.sum() + 1e-8)
        return np.sqrt(v)
    except Exception:
        return None

def scene_cluster(clips, S, cache_path):
    """Regroupe les plans en SCÈNES (DINOv2 + couleur Lab + temps -> HDBSCAN). Écrit scene_id sur chaque clip."""
    import numpy as np
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    tmpd = tempfile.mkdtemp(prefix="scene_")
    times = [media_date(c) for c in clips]
    tmin = min(times) if times else 0.0; tspan = max(1.0, (max(times) if times else 1.0) - tmin)
    ws, wc, wt = S.get("scene_w_sem", 0.50), S.get("scene_w_color", 0.20), S.get("scene_w_time", 0.30)
    feats, valid = [], []
    for c, t in zip(clips, times):
        key = file_key(c["file"]) + (f"::{c.get('trim_start','')}" if c.get("is_subclip") else "")
        ent = cache.get(key)
        if ent is None:
            src = c["file"]
            if c["type"] == "video":
                tt = c.get("trim_start", 0) + c.get("duration", 2) / 2
                fp = os.path.join(tmpd, "f.jpg")
                src = fp if extract_frame(c["file"], tt, fp, 256) else None
            emb = dino_embed(src) if src else None
            col = lab_signature(src) if src else None
            if emb is None or col is None: continue
            ent = {"emb": emb.tolist(), "col": col.tolist()}; cache[key] = ent
        emb = np.asarray(ent["emb"], np.float32); col = np.asarray(ent["col"], np.float32)
        tn = (t - tmin) / tspan
        v = np.concatenate([np.sqrt(ws)*emb, np.sqrt(wc)*col, [np.sqrt(wt)*tn*2.0]]).astype(np.float32)
        feats.append(v / (np.linalg.norm(v) + 1e-8)); valid.append(c)
    json.dump(cache, open(cache_path, "w"))
    shutil.rmtree(tmpd, ignore_errors=True)
    if len(feats) < 4:
        for c in valid: c["scene_id"] = 0
        return
    from sklearn.cluster import AgglomerativeClustering
    thr = float(S.get("scene_threshold_ai", 0.5))     # plus haut = scènes plus larges
    labels = AgglomerativeClustering(n_clusters=None, distance_threshold=thr,
                                     metric="cosine", linkage="average").fit_predict(np.asarray(feats, np.float32))
    for c, l in zip(valid, labels): c["scene_id"] = int(l)
    print(f"[render] {len(set(int(l) for l in labels))} scènes reconnues (DINOv2+couleur+temps) sur {len(valid)} plans", flush=True)

# ----------------------------------------------------------------------------- VLM
class VLM:
    def __init__(self, model_id):
        self.ok = False
        try:
            from mlx_vlm import load, generate
            from mlx_vlm.prompt_utils import apply_chat_template
            from mlx_vlm.utils import load_config
            self._generate = generate
            self._apply = apply_chat_template
            print(f"[vlm] chargement {model_id} ...", flush=True)
            self.model, self.processor = load(model_id)
            self.config = load_config(model_id)
            self.ok = True
            print("[vlm] prêt.", flush=True)
        except Exception as e:
            print(f"[vlm] indisponible ({e}) -> fallback CV-only", flush=True)

    PROMPT = ("You are a STRICT photo editor keeping only the very best shots for a short montage. "
              "Answer with ONLY one minified JSON object, no prose, no markdown:\n"
              '{"score":0-10,"sharpness":0-10,"composition":0-10,"faces":0-10,"moment":0-10,'
              '"relevance":0-10,"beauty":0-10,"aerial":true|false,"caption":"<=7 words present tense no period","tags":["..."]}\n'
              "aerial = TRUE only for a bird's-eye / drone / high-altitude shot looking down on landscape, coast or city, with no close subject; otherwise false.\n"
              "Use the FULL 0-10 range and be HARSH. Calibrate strictly: "
              "9-10 = exceptional (tack-sharp, strong emotion/expression, beautiful composition, a special moment); "
              "7-8 = good; 5-6 = ordinary snapshot; 3-4 = weak (soft focus, cluttered, subject looking away, awkward framing); "
              "0-2 = bad (blurry, motion blur, very dark or blown out, back of heads, eyes closed, boring or empty). "
              "MOST party snapshots are 4-7. Do NOT cluster around 8 — spread the scores honestly. caption in French.")

    def score(self, img_path):
        try:
            formatted = self._apply(self.processor, self.config, self.PROMPT,
                                    num_images=1, enable_thinking=False)
        except TypeError:
            formatted = self._apply(self.processor, self.config, self.PROMPT, num_images=1)
        try:
            out = self._generate(self.model, self.processor, formatted, image=[img_path],
                                  max_tokens=160, temperature=0.0, verbose=False)
        except TypeError:
            out = self._generate(self.model, self.processor, formatted, [img_path],
                                 max_tokens=160, temperature=0.0, verbose=False)
        text = out.text if hasattr(out, "text") else str(out)
        return parse_score_json(text)

def parse_score_json(text):
    # retire un bloc <think>...</think> éventuel, prend le dernier {...} parseable
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    cands = re.findall(r"\{[^{}]*\}", text, flags=re.S)
    for c in reversed(cands):
        try:
            d = json.loads(c)
            if "score" in d:
                d["score"] = float(d.get("score", 0))
                return d
        except Exception: continue
    return {"score": 0.0, "caption": "", "tags": [], "reason": "parse_error"}

def cv_only_score(pil_img, blur):
    """Note de secours sans VLM : netteté + luminosité + visages."""
    import cv2, numpy as np
    arr = np.array(pil_img)
    bright = float(arr.mean()) / 255.0
    expo = 1.0 - abs(bright - 0.5) * 1.4          # pénalise trop sombre/cramé
    sharp = min(1.0, blur / 300.0)
    faces = 0
    try:
        cc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        g = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        faces = len(cc.detectMultiScale(g, 1.1, 5))
    except Exception: pass
    score = 4.0*sharp + 3.0*max(0, expo) + min(3.0, faces*1.5)
    return {"score": round(score, 2), "caption": "", "tags": [],
            "faces": faces, "sharpness": round(sharp*10, 1)}

# ----------------------------------------------------------------------------- vidéo
def extract_frame(video, t, dst, long_edge=1024):
    run(["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", video, "-frames:v", "1",
         "-vf", f"scale={long_edge}:-1", "-q:v", "3", dst])
    return os.path.exists(dst) and os.path.getsize(dst) > 0

def detect_shots(video, threshold):
    try:
        from scenedetect import detect, ContentDetector
        scenes = detect(video, ContentDetector(threshold=threshold))
        return [(s.get_seconds(), e.get_seconds()) for s, e in scenes]
    except Exception:
        return []

def decode_audio(path, sr=16000):
    """Décode l'audio en PCM mono via ffmpeg (fiable, contrairement à audioread/soundfile sur MP4/AAC)."""
    try:
        import numpy as np
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1",
                            "-ar", str(sr), "-f", "f32le", "-"], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL)
        y = np.frombuffer(r.stdout, dtype=np.float32)
        return y if len(y) >= sr * 0.3 else None
    except Exception:
        return None

def audio_energy_curve(video):
    """RMS au cours du temps (pour caler les coupes sur des creux audio)."""
    try:
        import librosa, numpy as np
        y = decode_audio(video, 16000)
        if y is None: return [], []
        rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=512)[0]
        t = librosa.frames_to_time(range(len(rms)), sr=16000, hop_length=512)
        return list(t), list(map(float, rms))
    except Exception:
        return [], []

# ----------------------------------------------------------------------------- découpage audio + mouvement
def audio_features(path):
    """RMS(dB), ZCR, centroïde, flatness sur une grille (16 kHz, hop 512 ~32 ms)."""
    try:
        import librosa, numpy as np
        sr = 16000; y = decode_audio(path, sr)
        if y is None: return None
        hop = 512
        rms = librosa.feature.rms(y=y, hop_length=hop)[0]
        rms_db = 20 * np.log10(rms + 1e-9)
        zcr = librosa.feature.zero_crossing_rate(y, hop_length=hop)[0]
        cen = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=hop)[0]
        flat = librosa.feature.spectral_flatness(y=y, hop_length=hop)[0]
        t = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop)
        return {"t": t, "rms_db": rms_db, "zcr": zcr, "centroid": cen, "flatness": flat}
    except Exception:
        return None

def _classify_unit(F, a, b):
    import numpy as np
    cen = float(np.mean(F["centroid"][a:b+1])); flat = float(np.mean(F["flatness"][a:b+1]))
    zcr = float(np.mean(F["zcr"][a:b+1])); rms = F["rms_db"][a:b+1]
    dur = F["t"][b] - F["t"][a]; var = float(np.std(rms))
    if flat > 0.30 and zcr > 0.14: return "applause"
    if cen > 2600 and var > 4.5: return "laugh"
    if dur < 1.0 and (float(np.max(rms)) - float(np.mean(rms))) > 6: return "exclaim"
    return "speech"

def sound_units(path, A):
    """Découpe l'audio en UNITÉS sonores propres (bornes sur silences ≥120 ms)."""
    F = audio_features(path)
    if not F: return []
    import numpy as np
    from scipy import ndimage
    t = F["t"]; rms_db = F["rms_db"]
    if len(t) < 4: return []
    dt = float(t[1] - t[0])
    noise = float(np.percentile(rms_db, 15))
    thr = noise + A.get("speech_margin_db", 8.0)
    active = rms_db > thr
    close_n = max(1, int(A.get("pause_intra_ms", 150) / 1000 / dt))
    open_n = max(1, int(0.12 / dt))
    active = ndimage.binary_closing(active, structure=np.ones(close_n))
    active = ndimage.binary_opening(active, structure=np.ones(open_n))
    idx = np.where(active)[0]
    if len(idx) == 0: return []
    units = []
    for g in np.split(idx, np.where(np.diff(idx) > 1)[0] + 1):
        a, b = int(g[0]), int(g[-1])
        s, e = float(t[a]), float(t[b])
        if e - s < 0.5: continue
        seg = rms_db[a:b+1]; peak = a + int(np.argmax(seg))
        units.append({"start": s, "end": e, "peak_t": float(t[peak]), "type": _classify_unit(F, a, b),
                      "contrast": float(np.max(seg) - noise), "peak_db": float(np.max(seg))})
    return units

def motion_curve(path, fps=6, edge=256):
    """Énergie de mouvement (flux optique Farneback) z-scorée. (times, values)."""
    try:
        import cv2, numpy as np
        cap = cv2.VideoCapture(path)
        if not cap.isOpened(): return [], []
        vfps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        step = max(1, int(round(vfps / max(1, fps))))
        times, motion, prev, i = [], [], None, 0
        while True:
            if not cap.grab(): break
            if i % step == 0:
                ok, fr = cap.retrieve()
                if not ok: break
                h = max(1, int(edge * fr.shape[0] / fr.shape[1]))
                g = cv2.cvtColor(cv2.resize(fr, (edge, h)), cv2.COLOR_BGR2GRAY)
                if prev is not None:
                    flow = cv2.calcOpticalFlowFarneback(prev, g, None, 0.5, 3, 15, 3, 5, 1.2, 0)
                    mag = np.sqrt(flow[..., 0] ** 2 + flow[..., 1] ** 2)
                    motion.append(float(mag.mean())); times.append(i / vfps)
                prev = g
            i += 1
        cap.release()
        if not motion: return [], []
        m = np.array(motion); m = (m - m.mean()) / (m.std() + 1e-6)
        return times, [float(x) for x in m]
    except Exception:
        return [], []

def motion_units(times, vals, dur, A):
    """Fallback sans audio : intervalles où le mouvement dépasse le seuil."""
    import numpy as np
    if not times:
        return [{"start": 0.0, "end": min(dur, A.get("subclip_max_s", 6.0)),
                 "peak_t": min(dur, 2.0), "type": "motion", "contrast": 1.0, "peak_db": -20}]
    v = np.array(vals); idx = np.where(v > 0.3)[0]
    if len(idx) == 0:
        c = times[int(np.argmax(v))]
        return [{"start": max(0, c-2), "end": min(dur, c+2), "peak_t": c, "type": "motion", "contrast": 1.0, "peak_db": -20}]
    units = []
    for g in np.split(idx, np.where(np.diff(idx) > 1)[0] + 1):
        a, b = int(g[0]), int(g[-1])
        if times[b] - times[a] < 0.5: continue
        pk = a + int(np.argmax(v[a:b+1]))
        units.append({"start": times[a], "end": times[b], "peak_t": times[pk], "type": "motion",
                      "contrast": float(np.max(v[a:b+1])), "peak_db": -20})
    return units

def find_moments(path, dur, mtimes, mvals, A):
    """Détecte les MOMENTS forts = pics d'un signal 'momentness' (audio fort + mouvement), bornés sur les creux."""
    import numpy as np
    F = audio_features(path)
    rms_db = None
    if F is not None and len(F["t"]) > 4:
        t = np.asarray(F["t"]); rms_db = F["rms_db"]
        a = np.clip(rms_db - np.percentile(rms_db, 15), 0, None); a = a / (a.max() + 1e-6)
    elif mtimes:
        t = np.asarray(mtimes); a = np.zeros(len(t))
    else:
        return []
    if mtimes:
        m = np.interp(t, mtimes, mvals); m = np.clip((m - m.min()) / (m.max() - m.min() + 1e-6), 0, 1)
    else:
        m = np.zeros_like(a)
    moment = 0.6 * a + 0.4 * m
    dt = float(t[1] - t[0]) if len(t) > 1 else 0.1
    from scipy.ndimage import uniform_filter1d
    moment = uniform_filter1d(moment, max(1, int(0.5 / dt)))
    from scipy.signal import find_peaks
    thr = max(float(np.percentile(moment, A.get("cand_percentile", 45))), 0.12)
    peaks, _ = find_peaks(moment, height=thr, distance=max(1, int(A.get("subclip_min_s", 2.0) * 0.7 / dt)))
    if len(peaks) == 0: peaks = [int(np.argmax(moment))]
    units = []
    for p in peaks:
        lo = moment[p] * 0.55
        l = p; r = p
        while l > 0 and moment[l] > lo: l -= 1
        while r < len(moment) - 1 and moment[r] > lo: r += 1
        units.append({"start": float(t[l]), "end": float(t[r]), "peak_t": float(t[p]),
                      "type": _classify_unit(F, l, r) if rms_db is not None else "motion",
                      "contrast": float(moment[p] * 12),
                      "peak_db": float(rms_db[p]) if rms_db is not None else -18})
    return units

_TYPE_BONUS = {"laugh": 3.0, "exclaim": 2.5, "applause": 2.0, "speech": 1.0, "motion": 1.0}
def unit_score(u, A):
    pic = min(1.0, max(0.0, (u.get("peak_db", -30) + 40) / 30))
    bonus = _TYPE_BONUS.get(u.get("type"), 1.0)
    contrast = min(1.0, u.get("contrast", 0) / 25.0)
    d = u["end"] - u["start"]; dscore = 1.0 if 1.5 <= d <= 5 else (0.6 if d < 1.5 else 0.7)
    mvt = max(0.0, u.get("motion", 0.0))
    return (A.get("w_pic", 3.0) * pic + A.get("w_type", 1.5) * bonus +
            A.get("w_contrast", 1.5) * contrast + A.get("w_dur", 1.0) * dscore +
            A.get("w_motion", 1.5) * mvt)

# ----------------------------------------------------------------------------- AI_SELECT
def ai_select(sb_path, target, order, model, force, no_vlm):
    sb = json.load(open(sb_path))
    S = sb["settings"]; A = S["ai"]
    # idempotence : repartir des médias d'origine (retire d'anciens sous-clips vidéo)
    sb["clips"] = [c for c in sb["clips"] if not c.get("is_subclip")]
    for c in sb["clips"]:
        if c.get("prefilter_reason") == "split_into_subclips":
            c["prefilter_reason"] = None; c["include"] = True
    if target: S["target_duration"] = float(target)
    if order: S["order"] = order
    cache_path = os.path.join(os.path.dirname(os.path.abspath(sb_path)), ".ai_cache.json")
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}

    vlm = None if no_vlm else VLM(model or A["model"])
    tmpd = tempfile.mkdtemp(prefix="diapo_")
    photos = [c for c in sb["clips"] if c["type"] == "photo"]
    videos = [c for c in sb["clips"] if c["type"] == "video"]

    # ---- PHOTOS : pré-filtre + score ----
    print(f"[ai] pré-filtre + notation de {len(photos)} photos ...", flush=True)
    seen_hashes = []
    for i, c in enumerate(photos, 1):
        key = file_key(c["file"]) + "::" + SCORE_VER
        if not force and key in cache:
            c.update(cache[key]);
        else:
            try:
                img = _load_rgb_1024(c["file"], A["vlm_long_edge"])
            except Exception as e:
                c.update({"ai_score": 0.0, "prefilter_reason": f"open_error", "ai_include": False}); continue
            bvar = blur_score(img)
            reason = None
            if bvar < A["blur_var_min"]: reason = "blur"
            ph = phash_of(img)
            if reason is None:
                for prev in seen_hashes:
                    if (ph - prev) <= A["phash_hamming_max"]: reason = "dup"; break
            if reason is None: seen_hashes.append(ph)
            if reason:
                res = {"ai_score": 1.0, "prefilter_reason": reason}
            elif vlm and vlm.ok:
                rp = os.path.join(tmpd, "p.jpg"); img.save(rp, quality=90)
                d = vlm.score(rp)
                res = {"ai_score": float(d.get("score", 0)), "ai_caption": d.get("caption", ""),
                       "ai_tags": d.get("tags", []), "ai_faces": d.get("faces"),
                       "aerial": bool(d.get("aerial")),
                       "prefilter_reason": None, "ai_sub": {k: d.get(k) for k in
                       ("sharpness","composition","faces","moment","relevance","beauty")}}
            else:
                d = cv_only_score(img, bvar)
                res = {"ai_score": d["score"], "ai_caption": "", "ai_tags": [], "prefilter_reason": None}
            res["blur_var"] = round(bvar, 1)
            c.update(res); cache[key] = res
        c["ai_include"] = c.get("prefilter_reason") is None
        if i % 2 == 0 or i == len(photos): print(f"[progress] photos {i}/{len(photos)}", flush=True)

    # ---- VIDÉOS : bornes pilotées par l'AUDIO + scoring par le MOUVEMENT ----
    import bisect as _bisect
    new_clips = []
    for vi, v in enumerate(videos, 1):
        dur = v.get("src_duration", 0)
        if dur <= 0: continue
        print(f"[ai] vidéo {os.path.basename(v['file'])} ({dur:.0f}s) : analyse audio+mouvement ...", flush=True)
        print(f"[progress] videos {vi}/{len(videos)}", flush=True)
        has_audio = bool(v.get("has_audio"))
        has_drone = bool(v.get("drone"))
        shots = detect_shots(v["file"], A["scene_threshold"]) or [(0.0, dur)]
        bounds = sorted({b for sh in shots for b in sh})
        mtimes, mvals = motion_curve(v["file"], A.get("motion_fps", 6), A.get("motion_edge", 256))
        def motion_at(tt):
            if not mtimes: return 0.0
            return mvals[min(_bisect.bisect_left(mtimes, tt), len(mvals) - 1)]
        units = find_moments(v["file"], dur, mtimes, mvals, A)
        if not units:
            units = motion_units(mtimes, mvals, dur, A)
        for u in units:
            u["motion"] = motion_at(u["peak_t"]); u["score"] = unit_score(u, A)
        atimes, arms = audio_energy_curve(v["file"]) if has_audio else ([], [])
        def snap_silence(tt, win=0.35):
            cand = [(rr, t2) for t2, rr in zip(atimes, arms) if abs(t2 - tt) <= win]
            return min(cand)[1] if cand else tt
        def snap_scene(tt, win=0.4):
            if not bounds: return tt
            b = min(bounds, key=lambda x: abs(x - tt))
            return b if abs(b - tt) <= win else tt
        kept = []
        for u in sorted(units, key=lambda x: -x.get("score", 0)):
            s0 = max(0.0, u["start"]); s1 = min(dur, u["end"])
            if has_audio: s0, s1 = snap_silence(s0), snap_silence(s1)
            s0, s1 = snap_scene(s0), snap_scene(s1)
            s0 = max(0.0, s0); s1 = min(dur, s1)
            if s1 - s0 < A["subclip_min_s"]:
                cc = (s0 + s1) / 2; s0 = max(0.0, cc - A["subclip_min_s"]/2); s1 = min(dur, s0 + A["subclip_min_s"])
            s1 = min(s1, s0 + A["subclip_max_s"])
            s0, s1 = round(max(0.0, s0), 2), round(min(dur, s1), 2)
            if s1 - s0 < 1.0: continue
            if any(not (s1 <= k0 or s0 >= k1) for (k0, k1, _) in kept): continue   # chevauchement
            kept.append((s0, s1, u))
            if len(kept) >= A.get("max_candidates_per_video", 8): break
        for (s0, s1, u) in kept:                # VLM en re-ranker : 1 frame au pic
            pk = min(max(u.get("peak_t", (s0 + s1) / 2), s0), s1)
            fp = os.path.join(tmpd, f"vf_{int(pk*100)}.jpg")
            vlm_s, cap, aerial = 6.0, "", False
            if extract_frame(v["file"], pk, fp, A["vlm_long_edge"]):
                if vlm and vlm.ok:
                    d = vlm.score(fp); vlm_s = float(d.get("score", 6)); cap = d.get("caption", "")
                    # aérien = le VLM voit un plan large/plongeant, OU métadonnées drone sans visage net
                    aerial = bool(d.get("aerial")) or (has_drone and float(d.get("faces", 5) or 5) <= 2)
                else:
                    from PIL import Image
                    vlm_s = cv_only_score(Image.open(fp).convert("RGB"), 200)["score"]
                    aerial = has_drone            # sans VLM : on se fie aux métadonnées drone
            if aerial:                            # un plan d'ouverture doit RESPIRER -> on l'allonge
                hold = A.get("aerial_hold_s", 4.5)
                if s1 - s0 < hold:
                    s1 = min(dur, s1 + (hold - (s1 - s0)))      # d'abord vers l'avant
                    if s1 - s0 < hold:                          # puis vers l'arrière si besoin
                        s0 = max(0.0, s0 - (hold - (s1 - s0)))
            score_final = u.get("score", 5) * (0.5 + vlm_s / 20.0)
            if aerial: score_final += A.get("aerial_bonus", 1.2)
            nv = dict(v)
            nv.update({"trim_start": s0, "trim_end": round(dur - s1, 2), "duration": round(s1 - s0, 2),
                       "ai_score": round(score_final, 2), "ai_caption": cap, "ai_type": u.get("type"),
                       "aerial": aerial, "has_audio": v.get("has_audio") and not aerial,
                       "ai_include": True, "prefilter_reason": None, "is_subclip": True})
            new_clips.append(nv)
        v["include"] = False; v["ai_include"] = False; v["prefilter_reason"] = "split_into_subclips"

    json.dump(cache, open(cache_path, "w"), indent=2, ensure_ascii=False)

    # ---- Sélection best-of (vidéos + photos) vers la durée cible ----
    def composite(c):
        sub = c.get("ai_sub")
        if not sub: return c.get("ai_score", 7.0)   # vidéos : score du span
        def g(k, d=7.0):
            v = sub.get(k); return float(v) if isinstance(v, (int, float)) else d
        # pondère les dimensions qui DISCRIMINENT (netteté, composition)
        return 0.35*g("sharpness") + 0.30*g("composition") + 0.15*g("beauty") + 0.10*g("relevance") + 0.10*g("faces")
    floor = A.get("score_floor", 0.0)
    pace_budget = 2.6
    target = S["target_duration"]
    photo_pool = [c for c in photos if c.get("ai_include")]
    n_scored = len(photo_pool) + len(new_clips)
    # le VLM tasse tout (5-8) -> on ÉTALE les scores photos en percentile du composite (2..10) :
    # dispersion visible dans l'UI + sélection best-of nette
    if len(photo_pool) > 3:
        import bisect as _bs
        comps = sorted(composite(c) for c in photo_pool)
        nn = max(1, len(comps) - 1)
        for c in photo_pool:
            c["ai_score"] = round(2 + (_bs.bisect_left(comps, composite(c)) / nn) * 8, 1)
    # vidéos : auto-inclut les meilleurs (plafond par source) ; le RESTE reste éditable dans l'UI
    vid_ranked = sorted(new_clips, key=lambda c: -c.get("ai_score", 0))
    cap = A.get("max_subclips_per_video", 3); per_src = {}
    chosen, used = [], 0.0
    for v in vid_ranked:
        if used >= target * S.get("video_share", 0.30): break
        if per_src.get(v["file"], 0) >= cap: continue
        chosen.append(v); per_src[v["file"]] = per_src.get(v["file"], 0) + 1
        used += v.get("duration", 3.0)
    # photos : best-of pour le reste, dédup perceptuelle (pHash) + sémantique (légende)
    strong = [c for c in photo_pool if c.get("ai_score", 0) >= floor]
    ranked_p = sorted(strong if len(strong) >= 6 else photo_pool, key=lambda c: -composite(c))
    seen_hash, seen_cap = [], set()
    for c in ranked_p:
        if used >= target: break
        cap = " ".join((c.get("caption") or c.get("ai_caption") or "").lower().split())
        if cap and cap in seen_cap: continue
        try:
            ph = phash_of(_load_rgb_1024(c["file"], 256))
            if any((ph - p) <= A["phash_hamming_max"] for p in seen_hash): continue
            seen_hash.append(ph)
        except Exception: pass
        if cap: seen_cap.add(cap)
        chosen.append(c); used += pace_budget
    if not chosen: chosen = ranked_p[:10]

    # ---- Ordre ----
    chosen = order_clips(chosen, S["order"])

    # ---- Write-back : on garde TOUT (photos + tous les sous-clips) pour la ré-sélection UI ----
    chosen_ids = {id(c) for c in chosen}
    for c in chosen:
        c["include"] = True
        if not c.get("caption"): c["caption"] = c.get("ai_caption", "")
    extra_subclips = [c for c in new_clips if id(c) not in chosen_ids]
    other_photos = [c for c in photos if id(c) not in chosen_ids]
    originals = [c for c in sb["clips"] if c["type"] == "video"]   # sources (remplacées par les sous-clips)
    for c in extra_subclips + other_photos + originals: c["include"] = False
    final = chosen
    allc = chosen + extra_subclips + other_photos + originals
    for i, c in enumerate(allc): c["id"] = i
    sb["clips"] = allc
    sb["settings"] = S
    json.dump(sb, open(sb_path, "w"), indent=2, ensure_ascii=False)
    shutil.rmtree(tmpd, ignore_errors=True)

    nph = sum(1 for c in final if c["type"]=="photo")
    nvi = sum(1 for c in final if c["type"]=="video")
    nblur = sum(1 for c in photos if c.get("prefilter_reason")=="blur")
    ndup = sum(1 for c in photos if c.get("prefilter_reason")=="dup")
    print(f"[ai] gardé {len(final)}/{n_scored} plans (~{used:.0f}s) : {nph} photos + {nvi} sous-clips vidéo | "
          f"écartés : {nblur} flous, {ndup} doublons, {max(0,n_scored-len(final)-nvi)} plus faibles.")
    print(f"[ai] ordre = {S['order']}. Édite {sb_path} puis : render")

def order_clips(clips, mode):
    if mode == "manual":          # ordre défini à la main dans l'éditeur (glisser-déposer)
        return sorted(clips, key=lambda c: c.get("manual_rank", 10**6))
    if mode == "highlights":
        return sorted(clips, key=lambda c: -c.get("ai_score", 0))
    if mode == "narrative":
        s = sorted(clips, key=lambda c: c.get("ai_score", 0))
        # calme -> pic -> fin douce : alterne autour du pic
        left, right = [], []
        for i, c in enumerate(s):
            (left if i % 2 == 0 else right).append(c)
        return left + right[::-1]
    if mode == "scene":
        return scene_chapters(clips)
    # chrono (défaut) : par date, puis trim_start (extraits d'une même vidéo dans l'ordre)
    return sorted(clips, key=lambda c: (media_date(c), c.get("trim_start", 0)))

def scene_chapters(clips):
    """Regroupe par SCÈNE. Utilise scene_id (DINOv2+couleur) s'il existe, sinon repli temps/GPS.
    Scènes ordonnées par horodatage médian, chronologique à l'intérieur (préserve ~la chrono globale)."""
    if any("scene_id" in c for c in clips):
        from collections import defaultdict
        g = defaultdict(list)
        for c in clips: g[c.get("scene_id", -99)].append(c)
        scenes = sorted(g.values(), key=lambda s: sorted(media_date(x) for x in s)[len(s)//2])
        out = []
        for s in scenes:   # plan aérien en tête de scène (établit le lieu), puis chrono
            out += sorted(s, key=lambda x: (not x.get("aerial"), media_date(x)))
        return out
    cl = sorted(clips, key=media_date)
    chapters, cur = [], []
    def gap(a, b):
        return abs(media_date(b) - media_date(a))
    for c in cl:
        if not cur: cur = [c]; continue
        new = gap(cur[-1], c) > 20*60        # >20 min -> nouveau chapitre
        g1, g2 = cur[-1].get("gps"), c.get("gps")
        if g1 and g2:
            d = math.hypot(g1[0]-g2[0], g1[1]-g2[1])
            if d > 0.003: new = True          # ~300 m
        if new: chapters.append(cur); cur = [c]
        else: cur.append(c)
    if cur: chapters.append(cur)
    out = []
    for ch in chapters:   # plan aérien en tête de chapitre, puis chrono
        out += sorted(ch, key=lambda x: (not x.get("aerial"), media_date(x)))
    return out

def find_climax(clips):
    """Repère le MOMENT FORT (gâteau/bougies/cadeau/applaudissements/forte émotion), biaisé vers la fin du déroulé."""
    KW = ("gâteau", "gateau", "bougie", "cake", "candle", "souffl", "cadeau", "gift",
          "applaud", "anniversaire", "surprise", "blow")
    n = max(1, len(clips)); best, bi = -1e9, (2 * n) // 3
    for i, c in enumerate(clips):
        txt = (str(c.get("ai_caption", "")) + " " + " ".join(c.get("ai_tags", []) or [])).lower()
        s = sum(2.5 for k in KW if k in txt)
        if c.get("ai_type") in ("exclaim", "applause", "laugh"): s += 2.5
        if c.get("aerial"): s -= 3.0      # un plan large/drone respire, ce n'est pas le climax émotionnel
        s += c.get("ai_score", 0) * 0.2
        s += 1.5 * (1 - abs(i / max(1, n - 1) - 0.68))   # le climax est souvent vers 2/3 du déroulé
        if s > best: best, bi = s, i
    return bi

def lead_with_aerial(clips):
    """Met un beau plan aérien en OUVERTURE (plan d'établissement), s'il y en a un dans la 1re moitié."""
    half = max(1, len(clips) // 2)
    cands = [(i, c) for i, c in enumerate(clips[:half]) if c.get("aerial")]
    if not cands: return clips
    i = max(cands, key=lambda ic: ic[1].get("ai_score", 0))[0]
    return [clips[i]] + clips[:i] + clips[i+1:]

# ----------------------------------------------------------------------------- RENDER
# --- Texte via Pillow -> PNG -> overlay ffmpeg (ce build de ffmpeg n'a pas drawtext) ---
_FONT_CANDS = ["/System/Library/Fonts/Avenir.ttc",
               "/System/Library/Fonts/Helvetica.ttc",
               "/System/Library/Fonts/Supplemental/Arial.ttf",
               "/System/Library/Fonts/SFNS.ttf"]
def load_font(size):
    from PIL import ImageFont
    for f in _FONT_CANDS:
        try: return ImageFont.truetype(f, size)
        except Exception: continue
    return ImageFont.load_default()

def _wrap(draw, text, font, maxw):
    words = text.split(); lines = []; cur = ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= maxw: cur = t
        else:
            if cur: lines.append(cur)
            cur = w
    if cur: lines.append(cur)
    return lines

def make_caption_png(text, W, H, path):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0)); d = ImageDraw.Draw(img)
    font = load_font(int(H * 0.044))
    lines = _wrap(d, text, font, int(W * 0.86))
    lh = int(H * 0.056); block = lh * len(lines)
    y0 = int(H * 0.88) - block
    # dégradé sombre en bas pour la lisibilité
    top = y0 - int(H * 0.04)
    for yy in range(top, H):
        a = int(160 * (yy - top) / max(1, H - top))
        d.line([(0, yy), (W, yy)], fill=(0, 0, 0, min(160, a)))
    for i, ln in enumerate(lines):
        tw = d.textlength(ln, font=font); x = (W - tw) // 2; y = y0 + i * lh
        d.text((x + 2, y + 2), ln, font=font, fill=(0, 0, 0, 170))
        d.text((x, y), ln, font=font, fill=(255, 255, 255, 240))
    img.save(path); return path

def face_count(path):
    import cv2, numpy as np
    try:
        from PIL import Image, ImageOps
        try:
            import pillow_heif; pillow_heif.register_heif_opener()
        except Exception: pass
        img = ImageOps.exif_transpose(Image.open(path)).convert("RGB"); img.thumbnail((900, 900))
        g = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
        cc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        return len(cc.detectMultiScale(g, 1.1, 5, minSize=(28, 28)))
    except Exception:
        return 0

def pick_hero(clips):
    """Photo la plus représentative pour le titre : la plus 'de groupe' (max visages), puis meilleur score."""
    photos = sorted([c for c in clips if c["type"] == "photo"],
                    key=lambda c: -c.get("ai_score", 0))[:30]
    if not photos: return None
    best = max(photos, key=lambda c: (face_count(c["file"]), c.get("ai_score", 0)))
    return best["file"]

def make_title_card_png(title, subtitle, W, H, path, bg=None):
    from PIL import Image, ImageDraw, ImageOps, ImageFilter
    if bg and os.path.exists(bg):
        try:
            try:
                import pillow_heif; pillow_heif.register_heif_opener()
            except Exception: pass
            im = ImageOps.exif_transpose(Image.open(bg)).convert("RGB")
            im = ImageOps.fit(im, (W, H), method=Image.LANCZOS)          # remplit le cadre (group photo)
            im = im.filter(ImageFilter.GaussianBlur(2))
            img = Image.blend(im, Image.new("RGB", (W, H), (0, 0, 0)), 0.42)   # assombri pour le texte
        except Exception:
            img = Image.new("RGB", (W, H), (15, 18, 22))
    else:
        img = Image.new("RGB", (W, H), (15, 18, 22))
    d = ImageDraw.Draw(img)
    ts = int(H * 0.095); tf = load_font(ts)
    while d.textlength(title, font=tf) > W * 0.9 and ts > 24:    # réduit la police si le texte déborde
        ts -= 4; tf = load_font(ts)
    sf = load_font(int(H * 0.038))
    ty = int(H * 0.40)
    tw = d.textlength(title, font=tf)
    d.text(((W - tw) // 2 + 2, ty + 3), title, font=tf, fill=(0, 0, 0))
    d.text(((W - tw) // 2, ty), title, font=tf, fill=(247, 247, 249))
    lw = int(W * 0.05); lx = (W - lw) // 2; ly = ty + int(H * 0.125)
    d.line([(lx, ly), (lx + lw, ly)], fill=(235, 235, 240), width=3)
    if subtitle:
        sw = d.textlength(subtitle, font=sf)
        d.text(((W - sw) // 2 + 1, ly + int(H * 0.03) + 1), subtitle, font=sf, fill=(0, 0, 0))
        d.text(((W - sw) // 2, ly + int(H * 0.03)), subtitle, font=sf, fill=(225, 228, 235))
    img.save(path); return path

def build_segment(clip, idx, settings, workdir, dur_override=None):
    W, H = settings["resolution"]; fps = settings["fps"]
    dur = float(dur_override if dur_override else clip.get("duration", settings["photo_duration"]))
    out = os.path.join(workdir, f"seg_{idx:03d}.mp4")
    cap = (clip.get("caption") or "").strip() if settings.get("captions", "none") == "all" else ""
    cap_png = None
    if cap:
        cap_png = make_caption_png(cap, W, H, os.path.join(workdir, f"cap_{idx:03d}.png"))

    if clip["type"] == "photo":
        pfile = clip["file"]
        cc = float(settings.get("color_coherence", 0.0))
        if cc > 0 and settings.get("_color_ref"):
            pfile = grade_photo(clip["file"], settings["_color_ref"], cc,
                                os.path.join(workdir, f"grade_{idx:03d}.jpg"))
        frames = max(1, int(dur*fps))
        try:
            from PIL import Image
            iw, ih = Image.open(pfile).size
        except Exception:
            iw, ih = 16, 9
        ar = iw / max(1, ih)
        zin = (idx % 2 == 0); zf = 0.06        # zoom doux (max 1.06), alterne avant/arrière
        z = (f"min({1+zf:.3f},1.0+{zf}*on/{frames})" if zin else f"max(1.0,{1+zf:.3f}-{zf}*on/{frames})")
        cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"      # zoom CENTRÉ -> visages préservés
        SW, SH = 2*W, 2*H                      # sur-échantillonnage 2x -> supprime le tremblement du zoompan
        zp = f"zoompan=z='{z}':d={frames}:x='{cx}':y='{cy}':s={W}x{H}:fps={fps}"
        if settings.get("photo_fit", "auto") == "auto" and ar < 1.3:
            # PORTRAIT / CARRÉ : photo entière sur fond flou (aucun rognage = aucun visage coupé)
            chain = (f"[0:v]split=2[s0][s1];"
                     f"[s0]scale={SW}:{SH}:force_original_aspect_ratio=increase,crop={SW}:{SH},"
                     f"scale=420:-2,boxblur=12,scale={SW}:{SH},eq=brightness=-0.05[bg];"   # flou rapide (via downscale)
                     f"[s1]scale={SW}:{SH}:force_original_aspect_ratio=decrease[fg];"
                     f"[bg][fg]overlay=(W-w)/2:(H-h)/2,{zp},setsar=1,format=yuv420p[v]")
        elif settings.get("ken_burns", True):
            # PAYSAGE : remplissage + Ken Burns doux centré (zoom faible)
            chain = (f"[0:v]scale={SW}:{SH}:force_original_aspect_ratio=increase,"
                     f"crop={SW}:{SH},{zp},setsar=1,format=yuv420p[v]")
        else:
            chain = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
                     f"crop={W}:{H},fps={fps},setsar=1,format=yuv420p[v]")
        inputs = ["-loop", "1", "-t", f"{dur}", "-i", clip["file"],
                  "-f", "lavfi", "-t", f"{dur}", "-i",
                  "anullsrc=channel_layout=stereo:sample_rate=48000"]
        amap = "1:a"; cap_idx = 2
    else:
        ss = float(clip.get("trim_start", 0))
        chain = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                 f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p[v]")
        if clip.get("has_audio"):
            inputs = ["-ss", f"{ss}", "-t", f"{dur}", "-i", clip["file"]]
            amap = "0:a?"; cap_idx = 1
        else:
            inputs = ["-ss", f"{ss}", "-t", f"{dur}", "-i", clip["file"],
                      "-f", "lavfi", "-t", f"{dur}", "-i",
                      "anullsrc=channel_layout=stereo:sample_rate=48000"]
            amap = "1:a"; cap_idx = 2

    if cap_png:
        inputs += ["-loop", "1", "-i", cap_png]
        fc = chain + f";[v][{cap_idx}:v]overlay=0:0:format=auto[vo]"
    else:
        fc = chain[:-3] + "[vo]"   # renomme [v] final en [vo]
    cmd = (["ffmpeg", "-y"] + inputs + ["-filter_complex", fc, "-map", "[vo]", "-map", amap,
           "-shortest", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-c:a", "aac", "-ar", "48000", "-r", str(fps), "-pix_fmt", "yuv420p", out])
    r = run(cmd)
    if not os.path.exists(out):
        print(f"[render] !! échec segment {idx}: {r.stderr.decode('utf-8','ignore')[-400:]}")
        return None, 0.0
    return out, probe_duration(out)

def make_title(settings, workdir, subtitle="", dur=2.6, bg=None):
    W, H = settings["resolution"]; fps = settings["fps"]
    out = os.path.join(workdir, "seg_000_title.mp4")
    png = make_title_card_png(settings.get("title") or "Souvenirs", subtitle, W, H,
                              os.path.join(workdir, "title.png"), bg=bg)
    cmd = ["ffmpeg", "-y", "-loop", "1", "-t", f"{dur}", "-i", png,
           "-f", "lavfi", "-t", f"{dur}", "-i",
           "anullsrc=channel_layout=stereo:sample_rate=48000",
           "-filter_complex", f"[0:v]scale={W}:{H},setsar=1,fps={fps},format=yuv420p[v]",
           "-map", "[v]", "-map", "1:a", "-shortest",
           "-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-c:a", "aac", "-ar", "48000", "-r", str(fps), out]
    run(cmd)
    return out, probe_duration(out)

def make_end(settings, workdir, subtitle="", dur=2.8, bg=None):
    """Carton de FIN : une photo + le texte de fin (par défaut le titre), qui se fondra au noir."""
    W, H = settings["resolution"]; fps = settings["fps"]
    out = os.path.join(workdir, "seg_zzz_end.mp4")
    text = settings.get("end_text") or settings.get("title") or "Fin"
    png = make_title_card_png(text, subtitle, W, H, os.path.join(workdir, "end.png"), bg=bg)
    cmd = ["ffmpeg", "-y", "-loop", "1", "-t", f"{dur}", "-i", png,
           "-f", "lavfi", "-t", f"{dur}", "-i",
           "anullsrc=channel_layout=stereo:sample_rate=48000",
           "-filter_complex", f"[0:v]scale={W}:{H},setsar=1,fps={fps},format=yuv420p[v]",
           "-map", "[v]", "-map", "1:a", "-shortest",
           "-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-c:a", "aac", "-ar", "48000", "-r", str(fps), out]
    run(cmd)
    return out, probe_duration(out)

def probe_duration(path):
    r = run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", path])
    try: return float(r.stdout.decode().strip())
    except Exception: return 0.0

def xfade_concat(segments, settings, out, end_fade=True):
    """segments = [(path, duration)]. Fondus enchaînés vidéo+audio."""
    D = settings["transition_duration"]
    inputs = []
    for p, _ in segments: inputs += ["-i", p]
    fc = []
    # vidéo
    vlab = "0:v"; off = segments[0][1] - D
    for i in range(1, len(segments)):
        nl = f"v{i}"
        fc.append(f"[{vlab}][{i}:v]xfade=transition=fade:duration={D}:offset={off:.3f}[{nl}]")
        vlab = nl; off += segments[i][1] - D
    # audio
    alab = "0:a"
    for i in range(1, len(segments)):
        nl = f"a{i}"
        fc.append(f"[{alab}][{i}:a]acrossfade=d={D}[{nl}]")
        alab = nl
    vmap = f"[{vlab}]" if len(segments) > 1 else "0:v"
    amap = f"[{alab}]" if len(segments) > 1 else "0:a"
    total = sum(d for _, d in segments) - D * (len(segments) - 1); fs = max(0.1, total - 1.6)
    vfade = "fade=t=in:st=0:d=0.5" + (f",fade=t=out:st={fs:.2f}:d=1.6" if end_fade else "")
    fc.append(f"{vmap}{vfade}[vf]"); vmap = "[vf]"
    if end_fade:
        fc.append(f"{amap}afade=t=out:st={fs:.2f}:d=1.6[af]"); amap = "[af]"
    filt = ";".join(fc)
    if settings.get("hardware_accel"):
        venc = ["-c:v", "h264_videotoolbox", "-b:v", "10M"]
    else:
        venc = ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]
    cmd = (["ffmpeg", "-y"] + inputs + ["-filter_complex", filt,
           "-map", vmap, "-map", amap] + venc +
           ["-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p", out])
    r = run(cmd)
    if not os.path.exists(out):
        print("[render] !! échec xfade:", r.stderr.decode("utf-8","ignore")[-600:])
        return False
    return True

def analyze_beats(path):
    import librosa, numpy as np
    y, sr = librosa.load(path, sr=22050, mono=True)
    tempo, beats = librosa.beat.beat_track(y=y, sr=sr)
    tempo = float(np.atleast_1d(tempo)[0])
    bt = [float(x) for x in librosa.frames_to_time(beats, sr=sr)]
    return tempo, bt

def beat_strengths(path, beats):
    """Énergie normalisée [0,1] par temps (onset + RMS) : sert à caler le RYTHME des coupes
    sur l'intensité réelle du morceau (intro calme -> coupes lentes ; refrain/drop -> coupes rapides)."""
    import librosa, numpy as np
    if not beats or len(beats) < 2: return []
    y, sr = librosa.load(path, sr=22050, mono=True)
    hop = 512
    onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    rms = librosa.feature.rms(y=y, hop_length=hop)[0]
    times = librosa.frames_to_time(np.arange(len(onset)), sr=sr, hop_length=hop)
    period = beats[1] - beats[0]
    def agg(arr):
        out = []
        for i, b in enumerate(beats):
            e = beats[i+1] if i+1 < len(beats) else b + period
            lo = int(np.searchsorted(times, b)); hi = max(lo+1, int(np.searchsorted(times, e)))
            seg = arr[lo:hi]
            out.append(float(np.mean(seg)) if len(seg) else 0.0)
        return np.asarray(out)
    def norm(a):
        lo, hi = np.percentile(a, 10), np.percentile(a, 90)
        return np.clip((a - lo) / (hi - lo + 1e-6), 0, 1)
    e = 0.6 * norm(agg(onset)) + 0.4 * norm(agg(rms))
    if len(e) >= 3:                                   # lissage : pas de sauts beat-à-beat
        e = np.convolve(e, np.array([0.25, 0.5, 0.25]), mode="same")
    return [float(x) for x in e]

def _beat_grid(beats, period, upto):
    g = list(beats)
    if not g:
        t = 0.0
        while t < upto: g.append(t); t += period
        return g
    while g[-1] < upto: g.append(g[-1] + period)
    return g

def assign_beat_durations(clips, beats, S, hard=True):
    """Cale chaque cut sur un beat. hard=True -> cuts francs (pas de recouvrement). Renvoie la durée du titre."""
    import bisect
    D = 0.0 if hard else S["transition_duration"]
    if len(beats) < 8: return None
    diffs = sorted(beats[i+1]-beats[i] for i in range(len(beats)-1))
    period = diffs[len(diffs)//2]
    grid = _beat_grid(beats, period, 2400)
    B = max(1, int(S.get("beats_per_clip", 4)))
    dynamic = S.get("rhythm") == "dynamique"
    recit = S.get("rhythm") == "recit"
    song = S.get("rhythm") == "song"
    energy = S.get("_beat_energy") or []
    cidx = int(S.get("_climax_idx", len(clips)//2))
    edgeB = max(3, B)                     # base aux extrémités pour un vrai contraste
    n = len(clips)
    ci = bisect.bisect_left(grid, max(1.0, S.get("title_seconds", 2.0)))
    cut_prev = grid[ci]; title_dur = cut_prev + D
    for i, c in enumerate(clips):
        if c["type"] == "video":
            nb = max(2, round(c.get("duration", 2.0) / period))
        elif recit:
            if i == cidx:
                nb = 6                                   # TIENT le moment fort (~3-4 s)
            elif i < cidx:
                d = (cidx - i) / max(1, cidx)            # 1 au début -> 0 près du climax
                nb = max(1, round(1 + 3 * d))            # accélère 4 -> 1 en MONTANT vers le climax
            else:
                d = (i - cidx) / max(1, n - 1 - cidx)    # 0 juste après -> 1 à la fin
                nb = max(1, round(2 + 2 * d))            # RELÂCHE 2 -> 4 en douceur
        elif song and energy:
            # ÉPOUSE LA CHANSON : coupes lentes quand le morceau est calme, rapides quand il monte
            bi = min(bisect.bisect_left(beats, cut_prev), len(energy) - 1)
            e = energy[bi]                            # 0 = calme, 1 = intense
            SLOW, FAST = 6, 1
            nb = max(1, round(SLOW - (SLOW - FAST) * e))
        elif dynamic:
            # accélère vers "chaque beat" au MILIEU du montage, relâche aux extrémités
            p = i / max(1, n - 1)
            closeness = 1 - abs(p - 0.5) * 2          # 0 aux bords -> 1 au centre
            nb = max(1, round(edgeB - (edgeB - 1) * closeness))
        else:
            nb = B
        ni = min(ci + nb, len(grid) - 1)
        c["_rdur"] = max(0.4, (grid[ni] - cut_prev) + D)
        ci = ni; cut_prev = grid[ni]
    return title_dur

def _venc(S):
    return (["-c:v", "h264_videotoolbox", "-b:v", "10M"] if S.get("hardware_accel")
            else ["-c:v", "libx264", "-preset", "medium", "-crf", "18"])

def concat_hard(segments, S, out, end_fade=True):
    """Concatène les segments en cuts FRANCS. end_fade=False -> pas de fondu final (géré par le carton)."""
    inputs = []
    for p, _ in segments: inputs += ["-i", p]
    n = len(segments)
    streams = "".join(f"[{i}:v][{i}:a]" for i in range(n))
    total = sum(d for _, d in segments); fs = max(0.1, total - 1.6)
    vf = "fade=t=in:st=0:d=0.5" + (f",fade=t=out:st={fs:.2f}:d=1.6" if end_fade else "")
    af = f"afade=t=out:st={fs:.2f}:d=1.6" if end_fade else "anull"
    fc = f"{streams}concat=n={n}:v=1:a=1[vc][ac];[vc]{vf}[v];[ac]{af}[a]"
    cmd = (["ffmpeg", "-y"] + inputs + ["-filter_complex", fc, "-map", "[v]", "-map", "[a]"]
           + _venc(S) + ["-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p", out])
    r = run(cmd)
    if not os.path.exists(out):
        print("[render] !! échec concat:", r.stderr.decode('utf-8', 'ignore')[-500:]); return False
    return True

def finish_with_endcard(body, endcard, S, out):
    """Enchaîne le corps vers le carton de fin par un FONDU ENCHAÎNÉ doux + long fondu au noir."""
    bd = probe_duration(body); ed = probe_duration(endcard)
    D = 0.9; total = bd + ed - D; fo = max(0.1, total - 2.2)
    fc = (f"[0:v][1:v]xfade=transition=fade:duration={D}:offset={bd-D:.3f}[xv];"
          f"[xv]fade=t=out:st={fo:.2f}:d=2.2[v];"
          f"[0:a][1:a]acrossfade=d={D}[xa];[xa]afade=t=out:st={fo:.2f}:d=2.2[a]")
    cmd = (["ffmpeg", "-y", "-i", body, "-i", endcard, "-filter_complex", fc,
            "-map", "[v]", "-map", "[a]"] + _venc(S) +
           ["-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p", out])
    run(cmd)
    return os.path.exists(out)

def module_sections(clips, n):
    """Découpe les clips en n sections contiguës (chronologiques) ~égales."""
    n = max(1, min(n, len(clips)))
    sz = len(clips) / n
    return [clips[int(i*sz):int((i+1)*sz)] for i in range(n)]

def section_rhythm(i, n):
    """Rythme (beats/plan) par section -> variations : calme aux bords, rapide au milieu."""
    presets = {1: [2], 2: [3, 1], 3: [3, 1, 2], 4: [4, 2, 1, 3]}
    pat = presets.get(n, [3, 2, 1, 2, 3])
    return pat[i % len(pat)]

def build_music_bed(tracks, piece_lens, S, workdir):
    """Assemble un lit musical multi-pistes : une piste par section, enchaînées en fondu (changement de musique)."""
    X = 1.8
    pieces = []
    for i, (tr, plen) in enumerate(zip(tracks, piece_lens)):
        pl = max(0.5, plen + (X if i < len(tracks) - 1 else 2.5))
        pj = os.path.join(workdir, f"_bedp{i}.wav")
        run(["ffmpeg", "-y", "-v", "error", "-stream_loop", "-1", "-i", tr, "-t", f"{pl}",
             "-ac", "2", "-ar", "48000", pj])
        if os.path.exists(pj): pieces.append(pj)
    if not pieces: return None
    if len(pieces) == 1: return pieces[0]
    inp = []
    for p in pieces: inp += ["-i", p]
    chain = "[0:a]"; parts = []
    for i in range(1, len(pieces)):
        nl = f"b{i}"; parts.append(f"{chain}[{i}:a]acrossfade=d={X}[{nl}]"); chain = f"[{nl}]"
    out = os.path.join(workdir, "_bed.mp3")
    run(["ffmpeg", "-y", "-v", "error"] + inp + ["-filter_complex", ";".join(parts),
         "-map", chain, "-c:a", "libmp3lame", "-q:a", "3", out])
    return out if os.path.exists(out) else pieces[0]

def mix_music(base_video, music, intervals, S, out):
    """Musique en fond, COUPÉE pendant les intervalles (clips sonores), fondu in/out, puis mixée."""
    total = probe_duration(base_video)
    vol = S.get("music_volume", 0.7); r = 0.15
    mode = "full" if S.get("music_under_video") else S.get("video_audio", "cut")
    if intervals and mode in ("cut", "duck"):
        floor = 0.12 if mode == "duck" else 0.0    # duck = baisse à 12% (-18 dB) : on entend nettement la vidéo, musique en fond léger
        terms = [f"clip(min((t-({a:.3f}-{r}))/{r},(({b:.3f}+{r})-t)/{r}),0,1)" for a, b in intervals]
        inside = terms[0]
        for tm in terms[1:]: inside = f"max({inside},{tm})"
        gate = f"{vol}*(1-{1-floor:.2f}*({inside}))"
    else:
        gate = f"{vol}"
    fo = max(0.1, total - 2.5)
    fc = (f"[1:a]volume='{gate}':eval=frame,"
          f"afade=t=in:st=0:d=1.5,afade=t=out:st={fo:.2f}:d=2.5[m];"
          f"[0:a][m]amix=inputs=2:normalize=0:duration=first[a]")
    cmd = ["ffmpeg", "-y", "-i", base_video, "-stream_loop", "-1", "-i", music,
           "-filter_complex", fc, "-map", "0:v", "-map", "[a]",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", out]
    rr = run(cmd)
    if not os.path.exists(out):
        print("[render] !! échec musique:", rr.stderr.decode('utf-8','ignore')[-500:]); return False
    return True

def render(sb_path, out, music=None, no_beat=False):
    sb = json.load(open(sb_path)); S = sb["settings"]
    if music: S["music"] = music
    music = S.get("music")
    workdir = os.path.join(os.path.dirname(os.path.abspath(sb_path)), "work")
    os.makedirs(workdir, exist_ok=True)
    order = S.get("order", "chrono")
    if S.get("rhythm") == "recit" and order != "manual":   # Récit force la chrono, sauf si ordre manuel imposé
        order = "chrono"
    included = [c for c in sb["clips"] if c.get("include")]
    if order == "scene":
        try: scene_cluster(included, S, os.path.join(workdir, ".scene_cache.json"))
        except Exception as e: print(f"[render] reconnaissance de scènes indispo ({e})")
    clips = order_clips(included, order)
    if order in ("chrono", "scene"):
        clips = lead_with_aerial(clips)       # plan d'ouverture aérien si dispo
    if not clips:
        print("[render] aucun clip inclus."); return
    n_aerial = sum(1 for c in clips if c.get("aerial"))
    if n_aerial: print(f"[render] {n_aerial} plan(s) aérien(s) : ouverture/transitions, tenus plus longtemps, musique non coupée")
    d0 = media_date(clips[0]); sub = fr_date(d0) if d0 else ""
    if S.get("rhythm") == "recit":
        S["_climax_idx"] = find_climax(clips)
        print(f"[render] mode RÉCIT : moment fort au plan {S['_climax_idx']+1}/{len(clips)}")

    if float(S.get("color_coherence", 0.0)) > 0:        # référence couleur = médiane du lot
        import numpy as np
        stats = [s for s in (lab_stats(c["file"]) for c in clips if c["type"] == "photo") if s[0]]
        if len(stats) >= 3:
            mus = np.array([s[0] for s in stats]); sgs = np.array([s[1] for s in stats])
            S["_color_ref"] = {"mu": np.median(mus, axis=0).tolist(), "sigma": np.median(sgs, axis=0).tolist()}
            print(f"[render] cohérence couleur {int(float(S['color_coherence'])*100)}% (réf médiane sur {len(stats)} photos)")

    title_dur = 2.6
    tracks = [t for t in S.get("music_tracks", []) if t]
    module = S.get("rhythm") == "module" and len(tracks) >= 2 and not no_beat
    beat = bool(music) and S.get("beat_sync", True) and not no_beat
    hard = (beat or module) and S.get("hard_cuts", True)
    sec_durs = []

    if module:
        K = min(len(tracks), 4)
        sections = module_sections(clips, K)
        for si, sec in enumerate(sections):
            Ssec = dict(S); Ssec["beats_per_clip"] = section_rhythm(si, len(sections)); Ssec["rhythm"] = "fixe"
            Ssec["title_seconds"] = S.get("title_seconds", 2.0) if si == 0 else 0.25
            try:
                tempo, beats = analyze_beats(tracks[si])
                td = assign_beat_durations(sec, beats, Ssec, hard=True)
                if si == 0 and td: title_dur = td
            except Exception:
                for c in sec: c["_rdur"] = c.get("duration", S["photo_duration"])
            sec_durs.append(sum(c.get("_rdur", S["photo_duration"]) for c in sec))
        print(f"[render] MODULE : {len(sections)} sections, rythmes {[section_rhythm(i,len(sections)) for i in range(len(sections))]}, musiques alternées")
    elif beat:
        try:
            tempo, beats = analyze_beats(music)
            if S.get("rhythm") == "song":
                try:
                    S["_beat_energy"] = beat_strengths(music, beats)
                    print(f"[render] mode SUR LA MUSIQUE : rythme calé sur l'énergie du morceau")
                except Exception as e:
                    print(f"[render] énergie du morceau indispo ({e})")
            td = assign_beat_durations(clips, beats, S, hard)
            if td: title_dur = td; print(f"[render] synchro {tempo:.0f} BPM, {S.get('beats_per_clip',4)} beats/plan")
            else: beat = hard = False
        except Exception as e:
            print(f"[render] beat sync indispo ({e})"); beat = hard = False

    segments = []; meta = []
    hero = pick_hero(clips) if S.get("title_bg", True) else None
    t, dttl = make_title(S, workdir, sub, title_dur, bg=hero)
    if t: segments.append((t, dttl)); meta.append(False)
    print(f"[render] {len(clips)} plans -> segments ...", flush=True)
    use_rdur = beat or module
    for i, c in enumerate(clips, 1):
        rd = c.get("_rdur") if use_rdur else None
        seg, dur = build_segment(c, i, S, workdir, rd)
        if seg:
            segments.append((seg, dur)); meta.append(c["type"] == "video" and bool(c.get("has_audio")) and not c.get("aerial"))
        print(f"[progress] segments {i}/{len(clips)}", flush=True)
    end_seg = None
    if S.get("end_card", True):                       # carton de fin séparé -> enchaînement DOUX
        ep = S.get("end_photo")
        end_bg = ep if (ep and os.path.exists(ep)) else \
            next((c["file"] for c in reversed(clips) if c["type"] == "photo"), hero)
        es, ed = make_end(S, workdir, sub, bg=end_bg)
        if es: end_seg = es

    print(f"[render] {len(segments)} segments, assemblage ...", flush=True)
    body = os.path.join(workdir, "_body.mp4")
    ok = (concat_hard if hard else xfade_concat)(segments, S, body, end_fade=(end_seg is None))
    if not ok: return
    mid = body
    if end_seg:
        soft = os.path.join(workdir, "_soft.mp4")
        if finish_with_endcard(body, end_seg, S, soft): mid = soft

    Dov = 0.0 if hard else S["transition_duration"]; intervals = []; tcur = 0.0
    for (p, d), is_moment in zip(segments, meta):
        if is_moment: intervals.append((tcur, tcur + d))
        tcur += d - Dov
    if intervals: print(f"[render] musique coupée sur {len(intervals)} moment(s) sonore(s)")

    if module and sec_durs:
        piece_lens = [title_dur + sec_durs[0]] + sec_durs[1:]
        bed = build_music_bed(tracks[:len(sec_durs)], piece_lens, S, workdir)
        if not (bed and mix_music(mid, bed, intervals, S, out)): os.replace(mid, out)
    elif music:
        if not mix_music(mid, music, intervals, S, out): os.replace(mid, out)
    else:
        os.replace(mid, out)
    print(f"[render] OK -> {out}  ({probe_duration(out):.1f}s, {len(segments)} segments dans {workdir}/)")

# ----------------------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="Montage photo/vidéo local (style Souvenirs).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("scan"); p1.add_argument("folder"); p1.add_argument("-o", default="storyboard.json")
    p2 = sub.add_parser("ai_select"); p2.add_argument("storyboard")
    p2.add_argument("--target", type=float, default=None)
    p2.add_argument("--order", choices=["chrono","scene","highlights","narrative"], default=None)
    p2.add_argument("--model", default=None); p2.add_argument("--force", action="store_true")
    p2.add_argument("--no-vlm", action="store_true")
    p3 = sub.add_parser("render"); p3.add_argument("storyboard"); p3.add_argument("-o", default="montage.mp4")
    p3.add_argument("--music", default=None); p3.add_argument("--no-beat", action="store_true")
    a = ap.parse_args()
    if a.cmd == "scan": scan(a.folder, a.o)
    elif a.cmd == "ai_select": ai_select(a.storyboard, a.target, a.order, a.model, a.force, a.no_vlm)
    elif a.cmd == "render": render(a.storyboard, a.o, a.music, a.no_beat)

if __name__ == "__main__":
    main()
